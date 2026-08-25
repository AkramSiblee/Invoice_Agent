"""
Sends a one-way email alert when an invoice needs human review. The human
resolves it by editing the Invoice Log sheet directly (e.g. setting
approve_vendor = TRUE), not by replying to this email.
"""
import base64
from email.mime.text import MIMEText

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import GOOGLE_APPLICATION_CREDENTIALS, NOTIFY_EMAIL, SHEET_ID, require

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def send_review_email(record: dict, issues: list[str]) -> None:
    require(NOTIFY_EMAIL, "NOTIFY_EMAIL")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}" if SHEET_ID else "(sheet not configured)"

    subject = f"Invoice needs review: {record.get('vendor', 'unknown vendor')}"
    body = (
        f"Invoice {record.get('invoice_number', '(no number)')} from "
        f"{record.get('vendor', 'unknown vendor')} needs a decision:\n\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + f"\n\nOpen the sheet to review and resolve: {sheet_url}\n"
        + "To approve a new vendor, set approve_vendor = TRUE on that row and re-run the agent."
    )

    message = MIMEText(body)
    message["to"] = NOTIFY_EMAIL
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    creds = Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS, scopes=SCOPES)
    service = build("gmail", "v1", credentials=creds)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
