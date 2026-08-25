"""
Lists new files in a watched Drive folder and downloads them locally for
processing. "New" = not already present in a local processed-ids ledger.
"""
import io
import json
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import dedup
from config import GOOGLE_APPLICATION_CREDENTIALS, DRIVE_WATCH_FOLDER_ID, require

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_drive_ids.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


def _service():
    creds = Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _load_ledger() -> set:
    if LEDGER_PATH.exists():
        return set(json.loads(LEDGER_PATH.read_text()))
    return set()


def _save_ledger(ids: set) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(sorted(ids)))


def fetch_new_files() -> list[str]:
    """Downloads any new files in the watched folder and returns local paths.

    Two layers keep this from double-processing the same invoice:
    1. `processed` (this file's ledger) skips a Drive file ID we've already
       handled in a previous run.
    2. `dedup.py` skips a file whose exact byte content has already been
       processed under ANY source — this is what catches two people
       dropping the same PDF under different filenames, or a file that was
       already pulled in via Gmail.
    """
    require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")
    service = _service()
    processed = _load_ledger()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    new_paths = []
    page_token = None
    while True:
        # Drive's default page size is 100 and this call was previously
        # unpaginated — past ~100 files in the folder it would silently stop
        # seeing anything beyond the first page, forever, not just "next run".
        # Looping on nextPageToken is what makes this correct at any folder size.
        response = service.files().list(
            q=f"'{DRIVE_WATCH_FOLDER_ID}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()

        for f in response.get("files", []):
            if f["id"] in processed:
                continue
            local_path = DOWNLOAD_DIR / f["name"]
            request = service.files().get_media(fileId=f["id"])
            with io.FileIO(local_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

            data = local_path.read_bytes()
            if dedup.already_seen(data):
                local_path.unlink()  # exact same file already processed via this or another source
            else:
                dedup.mark_seen(data)
                new_paths.append(str(local_path))

            # Mark processed and save immediately, per file — not batched at
            # the end — so a crash partway through a run can't cause an
            # already-finished file to be replayed next time.
            processed.add(f["id"])
            _save_ledger(processed)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return new_paths
