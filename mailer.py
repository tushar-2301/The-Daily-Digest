"""
mailer.py
---------
Standalone helper — emails a PDF (or any file) as an attachment using
Gmail's SMTP server.

Does NOT touch any existing pipeline file. Reads its settings from the
same .env file that config.py already loads (via config._load_dotenv_if_present),
so you just need to add these lines to your existing .env:

    EMAIL_FROM=youraddress@gmail.com
    EMAIL_TO=youraddress@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

Note: GMAIL_APP_PASSWORD is NOT your normal Gmail password. Gmail blocks
plain-password SMTP logins. You must create a 16-character "App Password":
  1. Go to https://myaccount.google.com/security
  2. Enable 2-Step Verification (required before App Passwords appear)
  3. Go to https://myaccount.google.com/apppasswords
  4. Create an app password for "Mail" / "Other (Custom name)" -> "news-pipeline"
  5. Copy the 16-character password into .env as GMAIL_APP_PASSWORD

Usage:
    python mailer.py --file output/headlinesss_latest.pdf
"""

import argparse
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

# Reuses the same .env loader as the rest of the codebase, so EMAIL_FROM /
# EMAIL_TO / GMAIL_APP_PASSWORD just need to be added to the existing .env file.
import config  # noqa: F401  (importing triggers _load_dotenv_if_present())
import os

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


def send_pdf_email(
    pdf_path: str,
    subject: str = None,
    body: str = None,
    email_from: str = None,
    email_to: str = None,
    app_password: str = None,
) -> None:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Attachment not found: {pdf_path}")

    email_from = email_from or os.environ.get("EMAIL_FROM", "")
    email_to = email_to or os.environ.get("EMAIL_TO", "")
    app_password = app_password or os.environ.get("GMAIL_APP_PASSWORD", "")

    missing = [
        name for name, val in
        [("EMAIL_FROM", email_from), ("EMAIL_TO", email_to), ("GMAIL_APP_PASSWORD", app_password)]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required .env value(s): {', '.join(missing)}. "
            "Add them to your .env file (see mailer.py docstring)."
        )

    from datetime import datetime
    date_str = datetime.now().strftime("%B %d, %Y")
    day_str = datetime.now().strftime("%A")
    subject = subject or f"Your {day_str} News Digest is here — {date_str}"
    body = body or (
        f"Good morning!\n\n"
        f"Here's your freshly brewed news digest for {day_str}, {date_str} — "
        f"skimmed, sorted, and ready before your oats is. 😅\n\n"
        f"Open the attached PDF to catch up in a few minutes flat.\n\n"
        f"— Sent automatically, so you don't have to lift a finger."
)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    # EMAIL_TO can be a comma-separated list of recipients
    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    msg.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )

    with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(email_from, app_password)
        smtp.send_message(msg, from_addr=email_from, to_addrs=recipients)

    print(f"Email sent to {', '.join(recipients)} with attachment {pdf_path.name}")


def main():
    parser = argparse.ArgumentParser(description="Email a PDF via Gmail SMTP")
    parser.add_argument("--file", required=True, help="Path to the PDF to attach/send")
    parser.add_argument("--subject", default=None)
    args = parser.parse_args()

    try:
        send_pdf_email(args.file, subject=args.subject)
    except Exception as e:
        print(f"Failed to send email: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
