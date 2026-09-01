"""
Sends a one-way email alert when an invoice needs human review. The human
resolves it by editing the Invoice Log sheet directly (e.g. setting
approve_vendor = TRUE), not by replying to this email.
"""
import base64
from email.mime.text import MIMEText

from auth import get_credentials
from googleapiclient.discovery import build

from config import NOTIFY_EMAIL, require
from sheets_client import get_invoice_log_url


def send_review_email(record: dict, issues: list[str]) -> None:
    require(NOTIFY_EMAIL, "NOTIFY_EMAIL")

    subject = f"Invoice needs review: {record.get('vendor', 'unknown vendor')}"
    body = (
        f"Invoice {record.get('invoice_number', '(no number)')} from "
        f"{record.get('vendor', 'unknown vendor')} needs a decision:\n\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + f"\n\nOpen the log to review and resolve: {get_invoice_log_url()}\n"
        + "To approve a new vendor, set approve_vendor = TRUE on that row and re-run the agent."
    )

    message = MIMEText(body)
    message["to"] = NOTIFY_EMAIL
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=get_credentials())
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
