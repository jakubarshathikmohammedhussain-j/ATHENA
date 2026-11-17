import os

import httpx


def send(to, subject, html_body):
    """Sends through Brevo's API. MAIL_FROM must be a sender you verified in Brevo."""
    key, sender = os.getenv("BREVO_API_KEY"), os.getenv("MAIL_FROM")
    if not key or not sender:
        raise RuntimeError("Email is not configured (BREVO_API_KEY, MAIL_FROM).")
    r = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": key, "accept": "application/json", "content-type": "application/json"},
        json={"sender": {"name": "Athena", "email": sender}, "to": [{"email": to}],
              "subject": subject, "htmlContent": html_body},
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Brevo {r.status_code}: {r.text[:300]}")
        
