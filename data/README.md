This folder is gitignored (it holds personal files — resume, OAuth tokens, DB).

Put your resume PDF here as `resume.pdf` (export `Arjun_Khanna_CV_V4.docx` to PDF once).
The path is referenced from `config/settings.yaml` under `resume.pdf_path`.

For module 7 (sending), two more files live here, both gitignored:
- `gmail_credentials.json` — the OAuth client JSON downloaded from Google Cloud
  Console (APIs & Services -> Credentials -> Create Credentials -> OAuth client ID ->
  Desktop app).
- `gmail_token.json` — written automatically by `scripts/gmail_auth.py` after you
  approve access in the browser; nothing to create by hand.
