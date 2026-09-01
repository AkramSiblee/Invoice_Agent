# Invoice processing agent

An agent that watches for incoming invoices (Gmail attachments or a Google Drive folder), extracts structured data using Claude, logs everything to the `Invoice_Log` Google Sheet, validates it against the `Vendor_Master` Google Sheet, and asks for human confirmation before touching reference data.

**This agent's file schema is matched to a separate Accounts Payable Agent's**, so rows can be copied across by a human — but it keeps its own `Vendor_Master`/`Invoice_Log` Google Sheets, in an "Agent Data" subfolder inside the watched `Invoice_Automation` Drive folder (`DRIVE_WATCH_FOLDER_ID` in `.env`), and never writes into the AP Agent's files directly. See `references/sheet_schema.md` for the full schema and why they're kept separate.

## Pipeline

1. **Intake** — new files from a watched Gmail label or Drive folder; exact-duplicate bytes are skipped here regardless of which source or filename they arrive under (see `scripts/dedup.py`). Gmail attachments are also archived directly into the watched Drive folder's `Attachments from Gmail` subfolder — no external forwarding automation (e.g. Zapier) needed.
2. **Extract** — Claude reads the PDF/image and returns structured JSON, including a best-effort category from a fixed taxonomy (null if nothing genuinely fits, e.g. a grocery receipt). Extraction calls for a batch run concurrently (bounded by `MAX_CONCURRENT_EXTRACTIONS` in `.env`) since each one is independent — see "Concurrency" in `SKILL.md`
3. **Log** — every invoice is appended to the `Invoice_Log` sheet
4. **Validate** — vendor match (resolves a `vendor_id`), category validity, math check, PO check, duplicate check
5. **Review loop** — failures get flagged and emailed; a human approves fixes (e.g. a new vendor) by editing the sheet, and the agent re-validates on the next run

Three layers guard against logging the same invoice twice — see "Duplicate protection" in `SKILL.md` for the full picture.

See `SKILL.md` for the full behavioral spec — this is what Claude Code reads to work on this project — and `references/` for the exact schemas and validation rules.

## Setup

### 1. Google Cloud

- Create (or reuse) a Google Cloud project
- Enable the **Gmail API**, **Drive API**, and **Sheets API**
- Create an **OAuth client** (Desktop app), save its JSON to `credentials/oauth-client.json`, and run `python scripts/authorize.py` once for the browser consent

> Gmail intake needs a real OAuth consent, not a service account — see the note at the top of `scripts/intake_gmail.py` if you're using Gmail as an intake source rather than only Drive.

### 2. Google Sheets

Nothing to create — the `Vendor_Master` and `Invoice_Log` Google Sheets are created automatically (empty, with just the header row) the first time the agent runs, inside a new "Agent Data" subfolder of the Drive folder pointed to by `DRIVE_WATCH_FOLDER_ID`. Column layout matches the Accounts Payable Agent's own schema so rows can be copied across by hand, but this is a separate copy — this agent never writes into that other project's files directly. See `references/sheet_schema.md` for why.

### 3. Environment

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, DRIVE_WATCH_FOLDER_ID, NOTIFY_EMAIL, etc.
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
- `extract_invoice.py`, `sheets_client.py`, `intake_drive.py`, `intake_gmail.py`, and `notify.py` are complete, correct code against the real Anthropic and Google APIs, but need *your* API key, OAuth consent, and folder ID to actually run — those are yours to create, I can't generate them for you.

## The one design rule to keep

The agent never writes to the `Vendor_Master` sheet on its own beyond a minimal `Pending`-status placeholder row. A flagged invoice only gets a vendor added after a human sets `approve_vendor = TRUE` on its row in the invoice log — and even then, a human still has to complete the vendor's onboarding fields (country, tax ID, payment terms, etc.) directly in the sheet before the AP Agent's own `Active`-only gate will let it flow through payment. This is what keeps one bad OCR read from quietly polluting shared reference data — see `SKILL.md` for more on this.
