import os, uuid, json, time, threading, subprocess, shutil
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from openai import OpenAI
from google.cloud import texttospeech

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024  # 150 MB upload limit for this starter
ROOT = Path("/tmp/myanmar_dubber")
ROOT.mkdir(parents=True, exist_ok=True)
JOBS = {}
LOCK = threading.Lock()

PAGE = r"""
<!doctype html><html lang="my"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Myanmar Video Dubber</title>
<style>
body{font-family:Arial,sans-serif;background:#f4f6fb;color:#172033;margin:0;padding:18px}
main{max-width:720px;margin:auto;background:white;padding:22px;border-radius:16px;box-shadow:0 5px 24px #0001}
h1{font-size:25px}p{line-height:1.6}.hint{color:#566175;font-size:14px}
input,button{font-size:16px;margin:10px 0;width:100%;box-sizing:border-box}
button{padding:13px;border:0;border-radius:10px;background:#2155d6;color:white;font-weight:bold}
#status{white-space:pre-wrap;background:#f2f4f8;border-radius:10px;padding:12px;margin-top:14px}
</style></head><body><main>
<h1>မြန်မာ Video Dubber</h1>
<p>၅ မိနစ်အောက် MP4 တင်ပါ။ မြန်မာအသံနှင့် မြန်မာစာတန်းထိုးပါတဲ့ MP4 ထုတ်ပေးရန် စမ်းသပ်ဗားရှင်းဖြစ်ပါတယ်။</p>
<p class="hint">မှတ်ချက် — AI API အသုံးပြုခနှင့် Google Cloud TTS ကုန်ကျစရိတ် ရှိနိုင်ပါတယ်။ ကိုယ်ပိုင် သို့မဟုတ် အသုံးပြုခွင့်ရှိတဲ့ ဗီဒီယိုများကိုသာ တင်ပါ။</p>
<form id="f"><input type="file" id="video" accept="video/mp4,video/*" required><button id="go">ဘာသာပြန်ပြီး MP4 ထုတ်မယ်</button></form>
<div id="status">အဆင်သင့်ဖြစ်ပါပြီ။</div><div id="download"></div>
</main><script>
const form=document.getElementById('f'), statusBox=document.getElementById('status'), download=document.getElementById('download'), btn=document.getElementById('go');
form.addEventListener('submit', async e=>{
 e.preventDefault(); download.innerHTML=''; const file=document.getElementById('video').files[0];
 if(!file){statusBox.textContent='Video ဖိုင်ရွေးပေးပါ။';return}
 if(file.size>150*1024*1024){statusBox.textContent='ဖိုင်အရွယ်အစား 150 MB ထက် မကျော်ရပါ။';return}
 btn.disabled=true; statusBox.textContent='Video တင်နေပါသည်...';
 try{
  const fd=new FormData();fd.append('video',file);
  const r=await fetch('/upload',{method:'POST',body:fd});const j=await r.json();
  if(!r.ok) throw new Error(j.error||'Upload မအောင်မြင်ပါ');
  const id=j.job_id; statusBox.textContent='အလုပ်စတင်ပါပြီ...';
  while(true){
   await new Promise(res=>setTimeout(res,3000));
   const sr=await fetch('/status/'+id);const s=await sr.json();
   statusBox.textContent=s.message||s.status;
   if(s.status==='done'){download.innerHTML='<a href="/download/'+id+'">မြန်မာအသံ + စာတန်းထိုး MP4 ကို Download လုပ်ရန်</a>';break}
   if(s.status==='error') throw new Error(s.message||'လုပ်ဆောင်မှု မအောင်မြင်ပါ');
  }
 }catch(err){statusBox.textContent='အမှား: '+err.message}
 finally{btn.disabled=false}
});
</script></body></html>
"""

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout.strip()

def get_clients():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY ကို Render Environment Variables မှာ ထည့်ပါ။")
    cred = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if cred:
        cred_path = Path("/tmp/google-tts-service-account.json")
        cred_path.write_text(cred, encoding="utf-8")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)
    elif not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS_JSON ကို Render Environment Variables မှာ ထည့်ပါ။")
    return OpenAI(api_key=key), texttospeech.TextToSpeechClient()

def translate_segments(client, segments):
    # Translate a small group at a time to avoid overly long prompts.
    translated = []
    for start in range(0, len(segments), 25):
        batch = segments[start:start+25]
        numbered = "\n".join(f'{i+1}. {s["text"]}' for i,s in enumerate(batch))
        prompt = (
            "Translate each numbered dialogue line into natural, concise Burmese (Myanmar language). "
            "Keep names and meaning. Return ONLY a JSON array of strings in the same order; no explanations. "
            "Do not merge lines. Input:\n" + numbered
        )
        response = client.chat.completions.create(
            model=os.environ.get("TRANSLATION_MODEL", "gpt-4o-mini"),
            messages=[{"role":"system","content":"You are a careful English-to-Burmese audiovisual translator."},
                      {"role":"user","content":prompt}],
            temperature=0.2
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removesuffix("```").strip()
        arr = json.loads(raw)
        if not isinstance(arr, list) or len(arr) != len(batch):
            raise RuntimeError("ဘာသာပြန် AI က မျှော်လင့်ထားတဲ့ပုံစံနဲ့ မပြန်ပေးပါ။")
        translated.extend(str(x).strip() for x in arr)
    return translated

def srt_time(seconds):
    ms = max(0, int(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def process_video(job_id, input_path):
    folder = ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with LOCK: JOBS[job_id].update(status="working", message="Video အသံကို ပြင်ဆင်နေပါသည်...")
        client, tts = get_clients()
        audio_path = folder / "audio.mp3"
        run(["ffmpeg","-y","-i",str(input_path),"-vn","-ac","1","-ar","16000","-b:a","64k",str(audio_path)])
        with open(audio_path, "rb") as af:
            tr = client.audio.transcriptions.create(
                model="whisper-1", file=af, response_format="verbose_json",
                timestamp_granularities=["segment"]
            )
        segments = []
        for s in getattr(tr, "segments", []) or []:
            txt = (s.get("text","") if isinstance(s,dict) else getattr(s,"text","")).strip()
            start = s.get("start",0) if isinstance(s,dict) else getattr(s,"start",0)
            end = s.get("end",0) if isinstance(s,dict) else getattr(s,"end",0)
            if txt and end > start:
                segments.append({"start":float(start),"end":float(end),"text":txt})
        if not segments:
            raise RuntimeError("စကားပြောစာသား မတွေ့ပါ။ Video အသံပါဝင်မှုကို စစ်ဆေးပါ။")
        with LOCK: JOBS[job_id].update(message=f"စကားပြော {len(segments)} ပိုင်းကို မြန်မာလို ဘာသာပြန်နေပါသည်...")
        translations = translate_segments(client, segments)
        client_cfg = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000,
            speaking_rate=1.0
        )
        # Make a timed voice track with silence between speech segments.
        wavs = []
        for idx, (seg, mytxt) in enumerate(zip(segments, translations)):
            if not mytxt:
                continue
            synthesis_input = texttospeech.SynthesisInput(text=mytxt)
            voice = texttospeech.VoiceSelectionParams(language_code="my-MM")
            result = tts.synthesize_speech(input=synthesis_input, voice=voice, audio_config=client_cfg)
            wav_path = folder / f"voice_{idx:04d}.wav"
            wav_path.write_bytes(result.audio_content)
            target = max(0.35, seg["end"] - seg["start"])
            # Adjust each translated phrase to roughly fit its original timing.
            adjusted = folder / f"adj_{idx:04d}.wav"
            probe = run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(wav_path)])
            actual = max(0.1, float(probe))
            tempo = max(0.65, min(1.6, actual / target))
            run(["ffmpeg","-y","-i",str(wav_path),"-filter:a",f"atempo={tempo:.4f},apad","-t",f"{target:.3f}",str(adjusted)])
            delayed = folder / f"delay_{idx:04d}.wav"
            delay_ms = int(seg["start"] * 1000)
            run(["ffmpeg","-y","-i",str(adjusted),"-af",f"adelay={delay_ms}|{delay_ms}","-ac","2",str(delayed)])
            wavs.append(delayed)
            with LOCK: JOBS[job_id]["message"] = f"မြန်မာအသံ ထုတ်နေပါသည်... {idx+1}/{len(segments)}"
        if not wavs:
            raise RuntimeError("မြန်မာအသံ မထုတ်နိုင်ပါ။")
        # Mix all delayed segments into one track.
        inputs = []
        for w in wavs: inputs += ["-i",str(w)]
        labels = "".join(f"[{i}:a]" for i in range(len(wavs)))
        mix_filter = f"{labels}amix=inputs={len(wavs)}:duration=longest:normalize=0,alimiter=limit=0.95[mix]"
        dub = folder / "dub.wav"
        run(["ffmpeg","-y",*inputs,"-filter_complex",mix_filter,"-map","[mix]","-ac","2",str(dub)])
        srt = folder / "burmese.srt"
        with srt.open("w",encoding="utf-8") as f:
            n=1
            for seg, txt in zip(segments, translations):
                f.write(f"{n}\n{srt_time(seg['start'])} --> {srt_time(seg['end'])}\n{txt}\n\n")
                n+=1
        output = folder / "myanmar_dubbed.mp4"
        # Keep original soundtrack low under the Burmese voice; embed selectable Burmese subtitles.
        run(["ffmpeg","-y","-i",str(input_path),"-i",str(dub),"-i",str(srt),
             "-filter_complex","[0:a]volume=0.18[bg];[1:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=first:dropout_transition=2[aout]",
             "-map","0:v:0","-map","[aout]","-map","2:0","-c:v","libx264","-preset","veryfast","-crf","23",
             "-c:a","aac","-b:a","160k","-c:s","mov_text","-metadata:s:s:0","language=bur",
             "-shortest",str(output)])
        with LOCK: JOBS[job_id].update(status="done",message="ပြီးပါပြီ။ မြန်မာအသံနဲ့ စာတန်းထိုးပါသော MP4 ကို Download လုပ်နိုင်ပါပြီ။",output=str(output))
    except Exception as e:
        with LOCK: JOBS[job_id].update(status="error",message=str(e))
    finally:
        try: input_path.unlink(missing_ok=True)
        except Exception: pass

@app.get("/")
def home():
    return render_template_string(PAGE)

@app.post("/upload")
def upload():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="Video ဖိုင်ကို ရွေးပေးပါ။"), 400
    ext = Path(secure_filename(f.filename)).suffix.lower()
    if ext not in [".mp4", ".mov", ".m4v", ".webm", ".mkv"]:
        return jsonify(error="MP4/MOV/M4V/WEBM/MKV ဖိုင်များကိုသာ လက်ခံပါသည်။"), 400
    job_id = uuid.uuid4().hex
    folder = ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)
    input_path = folder / ("input" + ext)
    f.save(input_path)
    with LOCK: JOBS[job_id] = {"status":"queued","message":"တန်းစီထားပါသည်။"}
    threading.Thread(target=process_video,args=(job_id,input_path),daemon=True).start()
    return jsonify(job_id=job_id)

@app.get("/status/<job_id>")
def status(job_id):
    with LOCK: data = JOBS.get(job_id)
    if not data: return jsonify(status="error",message="အလုပ်မတွေ့ပါ။ Server ပြန်စတင်သွားနိုင်ပါတယ်။"),404
    return jsonify(status=data["status"],message=data["message"])

@app.get("/download/<job_id>")
def download(job_id):
    with LOCK: data = JOBS.get(job_id)
    if not data or data.get("status") != "done" or not Path(data["output"]).exists():
        return "ဖိုင်မရသေးပါ သို့မဟုတ် Server ပြန်စတင်သွားပါပြီ။",404
    return send_file(data["output"],as_attachment=True,download_name="myanmar_dubbed.mp4")

@app.errorhandler(413)
def too_large(_):
    return jsonify(error="ဖိုင်အရွယ်အစား 150 MB ထက် ကျော်နေပါသည်။"),413

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
