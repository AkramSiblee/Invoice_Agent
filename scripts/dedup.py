"""
Shared content-hash ledger so the same file bytes are only ever processed
once, no matter which intake source they came from, what they're named, or
who put them there. This is what catches the cases the per-source ledgers
in intake_gmail.py / intake_drive.py can't:

- the same PDF sent as an attachment in two different emails
- the same invoice emailed AND separately dropped in the watched Drive folder
- two different people saving the same file into the Drive folder under
  different filenames

The per-source ledgers only stop the exact same Gmail message or Drive file
ID from being reprocessed — a different, narrower guarantee that doesn't
help when the duplicate arrives under a different ID.

This is a purely mechanical check (identical bytes). It intentionally does
NOT try to catch near-duplicates (a rescanned copy, a re-exported PDF of the
same invoice) — that's what the `duplicate_invoice` check in
validate_invoice.py is for, using the extracted vendor/invoice_number/date
instead of raw bytes, and it flags for human review rather than skipping.
"""
import hashlib
import json
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_content_hashes.json"


def _load() -> set:
    if LEDGER_PATH.exists():
        return set(json.loads(LEDGER_PATH.read_text()))
    return set()


def _save(hashes: set) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(sorted(hashes)))


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def already_seen(data: bytes) -> bool:
    return hash_bytes(data) in _load()


def mark_seen(data: bytes) -> None:
    hashes = _load()
    hashes.add(hash_bytes(data))
    _save(hashes)
