"""Central place for environment configuration. Everything reads from .env."""
import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS", "./credentials/service-account.json"
)

SHEET_ID = os.environ.get("INVOICE_SHEET_ID")
INVOICE_LOG_TAB = "Invoice Log"

# The vendor master lives in its own spreadsheet, not a second tab in
# SHEET_ID — see references/sheet_schema.md.
VENDOR_MASTER_SHEET_ID = os.environ.get("VENDOR_MASTER_SHEET_ID")

DRIVE_WATCH_FOLDER_ID = os.environ.get("DRIVE_WATCH_FOLDER_ID")
# No is:unread here on purpose — an invoice that arrived before this agent
# existed, or that a person already opened, is still an invoice. Already-read
# mail is included; scripts/intake_gmail.py bounds *how far back* it looks
# using EMAIL_CHECK_START_DATE / the rolling checkpoint below instead.
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "has:attachment label:Invoices")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL")

# The very first time intake_gmail.py runs (before it has a saved checkpoint
# in state/gmail_last_checked.json), it starts searching from this date.
# Every run after that starts from where the previous run left off instead —
# see EMAIL_CHECK_START_DATE's use in intake_gmail.py. Defaults to today if
# unset, so an agent that's never run before doesn't crawl your entire
# mailbox history on its first pass.
_email_check_start_date_raw = os.environ.get("EMAIL_CHECK_START_DATE")
if _email_check_start_date_raw:
    try:
        date.fromisoformat(_email_check_start_date_raw)
    except ValueError:
        raise RuntimeError(
            f"EMAIL_CHECK_START_DATE must be in YYYY-MM-DD format, got: {_email_check_start_date_raw!r}"
        )
    EMAIL_CHECK_START_DATE = _email_check_start_date_raw
else:
    EMAIL_CHECK_START_DATE = date.today().isoformat()

# How many invoices main.py extracts concurrently (each is an independent,
# stateless Claude call). Bound this to stay under your Anthropic rate limit
# rather than raising it without checking your tier's requests-per-minute.
MAX_CONCURRENT_EXTRACTIONS = int(os.environ.get("MAX_CONCURRENT_EXTRACTIONS", "4"))


def require(value, name):
    if not value:
        raise RuntimeError(f"Missing required config: {name}. Check your .env file.")
    return value
