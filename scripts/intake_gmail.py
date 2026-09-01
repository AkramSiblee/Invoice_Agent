"""
Finds candidate invoices in Gmail: downloads attachments locally for
extraction (and archives a copy into the watched Drive folder's
'Attachments from Gmail' subfolder — this agent writes there directly now,
so it no longer depends on an external Zapier automation to get Gmail
attachments into Drive), AND separately extracts each matched message's
plain-text body as its own candidate invoice — an invoice can arrive typed
or forwarded directly into an email with no attachment at all, which is why
GMAIL_QUERY matches on subject line alone (see `_search_query()`) rather
than requiring `has:attachment`.

NOTE: a plain service account cannot read a personal Gmail inbox on its own —
Gmail's API requires either (a) domain-wide delegation on a Google Workspace
account, or (b) a per-user OAuth flow (the user consents once, a refresh
token is stored). If you're on personal Gmail rather than Workspace, swap
the auth block below for google-auth-oauthlib's InstalledAppFlow — see
https://developers.google.com/gmail/api/quickstart/python for the current
OAuth quickstart, since the exact flow has shifted over time.
"""
import base64
import io
import json
import re
import time
from pathlib import Path

from auth import get_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import dedup
from config import GMAIL_QUERY, EMAIL_CHECK_START_DATE, DRIVE_WATCH_FOLDER_ID, require

# Read-only is enough for Gmail itself now that nothing here ever modifies a
# message (no more removing UNREAD — see fetch_new_attachments). If you
# re-add any write behavior against Gmail, this needs to go back to
# gmail.modify. Archiving to Drive uses the separate `drive` scope in auth.py.
LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_gmail_ids.json"
LAST_CHECKED_PATH = Path(__file__).resolve().parent.parent / "state" / "gmail_last_checked.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"

FOLDER_MIME = "application/vnd.google-apps.folder"
GMAIL_ATTACHMENTS_FOLDER_NAME = "Attachments from Gmail"

# Memoized within one process run, same "load once" pattern as
# sheets_client.py's _ids_cache.
_archive_folder_id_cache: str | None = None


def _service():
    return build("gmail", "v1", credentials=get_credentials())


def _drive_service():
    return build("drive", "v3", credentials=get_credentials())


def _get_archive_folder_id(drive) -> str:
    """Finds the existing 'Attachments from Gmail' subfolder inside the
    watched Invoice_Automation folder, creating it if it's somehow missing."""
    global _archive_folder_id_cache
    if _archive_folder_id_cache:
        return _archive_folder_id_cache
    require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")

    q = (
        f"'{DRIVE_WATCH_FOLDER_ID}' in parents and name = '{GMAIL_ATTACHMENTS_FOLDER_NAME}' "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    resp = drive.files().list(q=q, fields="files(id)", pageSize=10).execute()
    files = resp.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        folder = drive.files().create(
            body={
                "name": GMAIL_ATTACHMENTS_FOLDER_NAME,
                "mimeType": FOLDER_MIME,
                "parents": [DRIVE_WATCH_FOLDER_ID],
            },
            fields="id",
        ).execute()
        folder_id = folder["id"]
    _archive_folder_id_cache = folder_id
    return folder_id


def _archive_to_drive(drive, folder_id: str, filename: str, mime_type: str, data: bytes) -> None:
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type or "application/octet-stream", resumable=False)
    drive.files().create(body={"name": filename, "parents": [folder_id]}, media_body=media, fields="id").execute()


def _walk_body_parts(part: dict) -> tuple[str, str]:
    """Recursively finds a message's text/plain and text/html body parts —
    unlike the shallow top-level scan used for attachments, this has to
    recurse: the body text usually sits one or more levels down inside a
    multipart/alternative part, not as a direct child of the payload."""
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    plain = html = ""
    if mime == "text/plain" and "data" in body:
        plain = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
    elif mime == "text/html" and "data" in body:
        html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
    for sub in part.get("parts", []) or []:
        sub_plain, sub_html = _walk_body_parts(sub)
        plain = plain or sub_plain
        html = html or sub_html
    return plain, html


def _extract_body_text(payload: dict) -> str:
    """Best available plain-text rendering of a message body: prefers
    text/plain, falls back to a crude tag-strip of text/html if that's all
    the message has (some emails, especially marketing/transactional ones,
    have no text/plain alternative at all)."""
    plain, html = _walk_body_parts(payload)
    if plain.strip():
        return plain
    if html.strip():
        return re.sub(r"<[^>]+>", " ", html)
    return ""


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


def fetch_new_invoice_sources() -> list[dict]:
    """Returns candidate invoice jobs from new matching messages (read or
    unread), each one a dict:

      {"kind": "file", "path": "<local path>"}
      {"kind": "text", "text": "<body text>", "label": "<for logs/source_file>"}

    Every new attachment is downloaded locally AND archived into the watched
    Drive folder's 'Attachments from Gmail' subfolder (replacing the
    external Zapier automation that used to do that forwarding). Separately,
    every new matching message's body text is extracted as its own
    candidate, since GMAIL_QUERY matches on subject line alone — many real
    invoices arrive typed or forwarded directly into the email, not as an
    attachment. extract_invoice.py's `not_an_invoice` escape hatch is what
    filters out the ones that turn out to just mention the word.

    Two layers keep this from double-processing the same content:
    1. `processed` (this file's ledger) skips a Gmail message ID we've
       already handled in a previous run — this gates the whole message,
       attachments and body alike.
    2. `dedup.py` skips any attachment or body text whose exact bytes have
       already been processed under ANY source — this is what catches the
       same PDF being sent in two emails, or emailed and also dropped in
       the watched Drive folder. A byte-identical file already archived
       this way on a previous run is also what `intake_drive.py` will skip
       via this same layer if it re-walks the folder and sees it there.

    Which messages are even considered is narrowed by `_search_query()`'s
    `after:` bound (see there) — that's a search-window optimization, not a
    duplicate guard by itself: if the window ever overlaps a previous run
    (e.g. this run crashes before saving a new checkpoint), the id ledger
    above is what actually keeps an already-handled message from being
    reprocessed, not the date bound.
    """
    run_started_at = int(time.time())
    service = _service()
    drive = _drive_service()
    archive_folder_id = _get_archive_folder_id(drive)
    processed = _load_ledger()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    query = _search_query()
    messages = service.users().messages().list(userId="me", q=query).execute().get("messages", [])
    jobs = []

    for msg_meta in messages:
        if msg_meta["id"] in processed:
            continue
        msg = service.users().messages().get(userId="me", id=msg_meta["id"]).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject") or "(no subject)"

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
            jobs.append({"kind": "file", "path": str(local_path)})
            _archive_to_drive(drive, archive_folder_id, filename, part.get("mimeType"), data)

        body_text = _extract_body_text(msg.get("payload", {}))
        if body_text.strip():
            body_bytes = body_text.encode("utf-8")
            if not dedup.already_seen(body_bytes):
                dedup.mark_seen(body_bytes)
                jobs.append({
                    "kind": "text",
                    "text": body_text,
                    "label": f"email: {subject!r} (msg {msg_meta['id']})",
                })

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

    return jobs
