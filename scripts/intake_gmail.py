"""
Pulls invoice attachments from Gmail.

NOTE: a plain service account cannot read a personal Gmail inbox on its own —
Gmail's API requires either (a) domain-wide delegation on a Google Workspace
account, or (b) a per-user OAuth flow (the user consents once, a refresh
token is stored). If you're on personal Gmail rather than Workspace, swap
the auth block below for google-auth-oauthlib's InstalledAppFlow — see
https://developers.google.com/gmail/api/quickstart/python for the current
OAuth quickstart, since the exact flow has shifted over time.
"""
import base64
import json
import time
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import dedup
from config import GOOGLE_APPLICATION_CREDENTIALS, GMAIL_QUERY, EMAIL_CHECK_START_DATE

# Read-only is enough now that nothing here ever modifies a message (no more
# removing UNREAD — see fetch_new_attachments). If you re-add any write
# behavior, this needs to go back to gmail.modify.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_gmail_ids.json"
LAST_CHECKED_PATH = Path(__file__).resolve().parent.parent / "state" / "gmail_last_checked.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


def _service():
    creds = Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS, scopes=SCOPES)
    return build("gmail", "v1", credentials=creds)


def _load_ledger() -> set:
    if LEDGER_PATH.exists():
        return set(json.loads(LEDGER_PATH.read_text()))
    return set()


def _save_ledger(ids: set) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(sorted(ids)))


def _load_last_checked() -> int | None:
    if LAST_CHECKED_PATH.exists():
        return json.loads(LAST_CHECKED_PATH.read_text())
    return None


def _save_last_checked(unix_seconds: int) -> None:
    LAST_CHECKED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_CHECKED_PATH.write_text(json.dumps(unix_seconds))


def _search_query() -> str:
    """Builds the query's `after:` bound.

    First-ever run (no checkpoint yet): starts from EMAIL_CHECK_START_DATE
    (.env; defaults to today), at whole-day precision — that's all Gmail's
    date-only after: syntax supports.

    Every run after that: starts from the unix-timestamp checkpoint saved by
    the previous run, which gives second-level precision — e.g. if the last
    run started checking at 3pm, this run's query begins `after:` that exact
    moment instead of re-scanning from the original start date every time.
    """
    last_checked = _load_last_checked()
    if last_checked is not None:
        after_clause = f"after:{last_checked}"
    else:
        after_clause = f"after:{EMAIL_CHECK_START_DATE.replace('-', '/')}"
    return f"{GMAIL_QUERY} {after_clause}"


def fetch_new_attachments() -> list[str]:
    """Downloads new invoice attachments (read or unread) and returns local file paths.

    Two layers keep this from double-processing the same invoice:
    1. `processed` (this file's ledger) skips a Gmail message ID we've
       already handled in a previous run.
    2. `dedup.py` skips an attachment whose exact byte content has already
       been processed under ANY source — this is what catches the same
       PDF being sent in two emails, or emailed and also dropped in the
       watched Drive folder.

    Which messages are even considered is narrowed by `_search_query()`'s
    `after:` bound (see there) — that's a search-window optimization, not a
    duplicate guard by itself: if the window ever overlaps a previous run
    (e.g. this run crashes before saving a new checkpoint), the id ledger
    above is what actually keeps an already-handled message from being
    reprocessed, not the date bound.
    """
    run_started_at = int(time.time())
    service = _service()
    processed = _load_ledger()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    query = _search_query()
    messages = service.users().messages().list(userId="me", q=query).execute().get("messages", [])
    new_paths = []

    for msg_meta in messages:
        if msg_meta["id"] in processed:
            continue
        msg = service.users().messages().get(userId="me", id=msg_meta["id"]).execute()
        for part in msg.get("payload", {}).get("parts", []):
            filename = part.get("filename")
            if not filename or "attachmentId" not in part.get("body", {}):
                continue
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_meta["id"], id=part["body"]["attachmentId"]
            ).execute()
            data = base64.urlsafe_b64decode(att["data"])
            if dedup.already_seen(data):
                continue  # exact same file already processed via this or another source
            local_path = DOWNLOAD_DIR / filename
            local_path.write_bytes(data)
            dedup.mark_seen(data)
            new_paths.append(str(local_path))

        # Mark processed and save immediately, per message — not batched at
        # the end — so a crash partway through a run can't cause an
        # already-finished message to be replayed next time.
        processed.add(msg_meta["id"])
        _save_ledger(processed)

    # Only advance the checkpoint once every matched message above has been
    # handled without an exception escaping the loop. If this run crashes
    # partway through, next run's window should still cover the unfinished
    # messages rather than skip past them — already-finished ones just get
    # skipped instantly via the id ledger instead, which costs a slightly
    # wider search, never a missed invoice.
    _save_last_checked(run_started_at)

    return new_paths
