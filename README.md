# Myanmar Video Dubber (Gemini edition)

## Render Environment Variables
- `GEMINI_API_KEY`: key from Google AI Studio.
- `GOOGLE_APPLICATION_CREDENTIALS_JSON`: Google Cloud service-account JSON for Cloud Text-to-Speech.
- `GEMINI_MODEL`: optional, defaults to `gemini-2.5-flash`.

This starter uses Gemini for audio transcription and Burmese translation. Google Cloud Text-to-Speech is still used for Burmese voice generation, so a Google Cloud service account and enabled Text-to-Speech API are still required.

Note: sentence-level timestamps are estimated by Gemini and may need review. This is a starter project, not a production-ready service. Render free instances may sleep/restart and local temporary jobs can be lost.
