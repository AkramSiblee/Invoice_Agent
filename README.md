# Invoice processing agent

An agent that watches for incoming invoices (Gmail attachments or a Google Drive folder), extracts structured data using Claude, logs everything to Google Sheets, validates it against your vendor master list, and asks for human confirmation before touching reference data.

## Pipeline

1. **Intake** — new files from a watched Gmail label or Drive folder; exact-duplicate bytes are skipped here regardless of which source or filename they arrive under (see `scripts/dedup.py`)
2. **Extract** — Claude reads the PDF/image and returns structured JSON. Extraction calls for a batch run concurrently (bounded by `MAX_CONCURRENT_EXTRACTIONS` in `.env`) since each one is independent — see "Concurrency" in `SKILL.md`
3. **Log** — every invoice is appended to the `Invoice Log` sheet
4. **Validate** — vendor match, math check, PO check, duplicate check
5. **Review loop** — failures get flagged and emailed; a human approves fixes (e.g. a new vendor) by editing the sheet, and the agent re-validates on the next run

Three layers guard against logging the same invoice twice — see "Duplicate protection" in `SKILL.md` for the full picture.

See `SKILL.md` for the full behavioral spec — this is what Claude Code reads to work on this project — and `references/` for the exact schemas and validation rules.

## Setup

### 1. Google Cloud

- Create (or reuse) a Google Cloud project
- Enable the **Gmail API**, **Drive API**, and **Sheets API**
- Create a **service account**, download its JSON key, save it to `credentials/service-account.json`
- Share your Drive intake folder and both Google Sheets (Invoice Log and Vendor Master) with the service account's email address (found inside the JSON key) as an Editor

> A plain service account can't read a personal Gmail inbox — see the note at the top of `scripts/intake_gmail.py` if you're using Gmail as an intake source rather than only Drive.

### 2. Google Sheets

Create **two separate spreadsheets**, not two tabs in one file:

- **Invoice Log** — one tab named exactly `Invoice Log`, matching the columns in `references/sheet_schema.md`
- **Vendor Master** — a single-tab spreadsheet (any tab name — the code reads the first tab regardless), also matching `references/sheet_schema.md`

Copy each spreadsheet's ID from its URL into `.env`.

### 3. Environment

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, INVOICE_SHEET_ID, VENDOR_MASTER_SHEET_ID, DRIVE_WATCH_FOLDER_ID, NOTIFY_EMAIL, etc.
pip install -r requirements.txt
```

### 4. Run it

```bash
python scripts/main.py
```

Run this on a schedule (cron, Task Scheduler, or a CI job) — each run only processes what's new since last time.

### 5. Run the tests

```bash
python tests/test_validate_invoice.py
```

## Using this with Claude Code in VS Code

Open this folder in VS Code and start Claude Code (`claude` in the integrated terminal). `SKILL.md` documents the whole pipeline, so you can ask things like "add a check for late fees" or "why did invoice #4471 fail validation" and it'll have the context it needs.

One caveat: Claude Code's exact convention for auto-loading a project-level skill may have changed since my training — if it doesn't pick up `SKILL.md` automatically, check docs.claude.com/docs/claude-code for where project skills currently need to live (commonly a `.claude/skills/` folder) and move or symlink it there.

## What's real vs. what needs your credentials

- `validate_invoice.py` and `dedup.py` are pure logic with no external dependencies — fully working right now; `validate_invoice.py` is covered by `tests/test_validate_invoice.py`.
- `extract_invoice.py`, `sheets_client.py`, `intake_drive.py`, `intake_gmail.py`, and `notify.py` are complete, correct code against the real Anthropic and Google APIs, but need *your* API key, service account, sheet ID, and folder ID to actually run — those are yours to create, I can't generate them for you.

## The one design rule to keep

The agent never writes to the Vendor Master spreadsheet on its own. A flagged invoice only updates it after a human sets `approve_vendor = TRUE` on the row in Invoice Log. This is what keeps one bad OCR read from quietly polluting your reference data — see `SKILL.md` for more on this.
