# Athena: files and deployment

## Files to upload to GitHub (14)

Root of the repo (13 files):
main.py, db.py, mailer.py, report.py, radar.py, docs.py,
index.html, services.html, radar.html, documents.html, shared.css,
requirements.txt, SETUP.md

Scheduler (1 file, create it with Add file, then Create new file):
.github/workflows/schedule.yml

If you uploaded jobs.py earlier, delete it from the repo.

## Accounts and keys (all free tiers)

1. Gemini: Google AI Studio, then Get API key. Also copy the current Flash-Lite model ID from the model list.
2. Database: Neon (neon.com). Create a project, open Connect, copy the DIRECT connection string (host has no "-pooler").
   If it fails to connect, delete "&channel_binding=require" from the end.
3. Email: Brevo (brevo.com). Add and verify a sender address. Then SMTP and API, then API keys, then Generate a new API key.
   Free plan: about 300 emails a day (check Brevo's current limit).

## Render

New, then Web Service, connect your GitHub repo.
- Runtime: Python
- Build command: pip install -r requirements.txt
- Start command: uvicorn main:app --host 0.0.0.0 --port $PORT

Environment variables:
| Name | Value |
|---|---|
| GEMINI_API_KEY | your Gemini key |
| GEMINI_MODEL | your Flash-Lite model ID |
| DATABASE_URL | Neon direct connection string |
| BREVO_API_KEY | Brevo API key |
| MAIL_FROM | the sender address you verified in Brevo |
| CRON_SECRET | any long random text |

## GitHub secrets (for the scheduler)

Repo, then Settings, then Secrets and variables, then Actions, then New repository secret:
- CRON_SECRET: exactly the same value as on Render
- APP_URL: your Render link, no slash at the end

## Test

1. Open your-link/ and search a company.
2. Open /radar, run the sample suppliers.
3. Open /documents, upload a text PDF, ask a question.
4. Open /services, start a weekly briefing with your own email and confirm it.
5. GitHub, Actions, Athena scheduler, Run workflow. This sends anything due right away.
6. If something fails, open Render, then Logs, and read the last error.

## Keep secrets safe
Never put keys in the code or in screenshots. If a key leaks, delete it and make a new one.
