# Pipeline reference — validations, conditions, and design notes

A single consolidated reference for everything this agent checks, tolerates, or
enforces, and why. Detail for any one section also lives in `SKILL.md` /
`references/`; this file exists so it's all in one place to skim later.

## 1. Pipeline order

1. **Intake** — `intake_drive.py`, `intake_gmail.py`: find new files
2. **Extract** — `extract_invoice.py`: Claude reads the file, returns structured JSON
3. **Log** — `sheets_client.py::append_invoice_row`: one row per invoice in `Invoice Log`
4. **Validate** — `validate_invoice.py`: 4 checks, described below
5. **Notify** — `notify.py`: emails a human if any check failed
6. **Approve** — `main.py::apply_approved_vendors`: on the *next* run, picks up rows where a human set `approve_vendor = TRUE`, adds the vendor, re-validates

Run with `python scripts/main.py`. Meant to run on a schedule (cron / Task Scheduler) — each run only processes what's new since last time.

## 2. Validation checks (`validate_invoice.py`)

All checks run independently — a row can fail more than one, and every failure is recorded, not just the first. Result is `{"status": "verified" | "needs_review", "issues": [...]}`.

| # | Check | Condition | Issue code |
|---|---|---|---|
| 1 | **Vendor match** | Normalize (lowercase, strip to letters/digits) extracted vendor name; compare against `vendor_name` + every `aliases` entry in Vendor Master. Exact match, or fuzzy match via `difflib.get_close_matches` at cutoff **0.8**. | `vendor_not_found` |
| 2 | **Math check** | `sum(line_items[].amount)` must equal `subtotal` within **$0.02** (skipped if no line items). `subtotal + tax` must equal `total` within **$0.02**. | `math_mismatch` |
| 3 | **PO check** | If `po_number` present, must match `^[A-Za-z0-9\-]+$` (letters/digits/hyphens). **Missing PO is not a failure** — retail receipts legitimately have none. Optional `po_list` param (not wired up yet) would additionally check membership in an open-PO list. | `po_malformed` / `po_not_found` |
| 4 | **Duplicate check** | If `invoice_number` present: match on normalized `vendor` + normalized `invoice_number` against every row in Invoice Log (including rows added earlier in the same run). If no `invoice_number`: fall back to `vendor` + `invoice_date` + `total` (within the $0.02 tolerance). A match **flags but does not skip** — two different invoices can coincidentally share a number, so a human decides. | `duplicate_invoice` |

**Tunable constants** (top of `validate_invoice.py`):
- `AMOUNT_TOLERANCE = 0.02`
- `FUZZY_MATCH_CUTOFF = 0.8` — loosen if legit vendors get flagged too often; tighten if unrelated vendors match each other

## 3. Duplicate protection (3 layers, catching what the one before can't)

| Layer | Mechanism | Catches | Behavior on match |
|---|---|---|---|
| 1. Per-source ID ledger | `state/processed_gmail_ids.json`, `state/processed_drive_ids.json` — Gmail message ID / Drive file ID already handled | Re-seeing the exact same message/file from the exact same source | Silent skip, no row, no notification |
| 2. Content-hash ledger | `state/processed_content_hashes.json` via `dedup.py` (SHA-256 of raw bytes) | Same file emailed **and** dropped in Drive; same file under different filenames/IDs; sent in two different emails | Silent skip, no row, no notification |
| 3. `duplicate_invoice` validation check | Extracted vendor + invoice_number (or vendor + date + total) matches an existing Invoice Log row | Bytes differ but it's the same real invoice (rescan, re-export) | **Logged and flagged `needs_review`** — not skipped; human makes the call |

Written incrementally (one item at a time, not batched) so a mid-run crash can't cause an already-finished item to replay next run.

## 4. Gmail intake specifics (`intake_gmail.py`)

- **No `is:unread`** in `GMAIL_QUERY` — deliberate. Already-read mail is included; the per-source ID ledger (not read/unread status) is what prevents reprocessing.
- Scope is `gmail.readonly` — nothing here ever modifies a message. If write behavior is ever added back, scope needs to change to `gmail.modify`.
- **Search window** (`_search_query()`): first-ever run searches from `EMAIL_CHECK_START_DATE` (`.env`, defaults to **today** if unset — so a first run doesn't crawl entire mailbox history). Every run after that resumes from `state/gmail_last_checked.json`, a rolling unix-second checkpoint.
- Checkpoint only advances **after every matched message in the run finishes without error** — so a mid-run crash re-covers the same window next time (safe, since the ID ledger skips anything already handled instantly).
- A plain service account **cannot** read a personal Gmail inbox — needs either Workspace domain-wide delegation or a per-user OAuth flow. See the note at the top of `intake_gmail.py`.

## 5. Drive intake specifics (`intake_drive.py`)

- Paginated via `nextPageToken` (page size 1000) — folders over ~100 files won't silently get truncated.
- "New" = Drive file ID not already in `state/processed_drive_ids.json`.
- Downloaded file is hash-checked against `dedup.py`; if already seen, the local copy is deleted immediately after download (not skipped pre-download, since Drive doesn't expose content hashes cheaply via this call).

## 6. Extraction (`extract_invoice.py`)

- Model: `claude-sonnet-5` (swap the `MODEL` constant for cost/accuracy tradeoffs)
- Accepted file types: `application/pdf`, `image/jpeg`, `image/png`, `image/webp` — anything else raises `ValueError`
- Extraction schema (`EXTRACTION_PROMPT`): `vendor`, `invoice_number` (nullable — falls back to receipt/transaction number), `invoice_date` (`YYYY-MM-DD`), `line_items[]` (`description`, `quantity`, `unit_price`, `amount`), `subtotal`, `tax`, `total`, `po_number` (nullable), `currency`
- Explicit instruction: **never invent a value not visibly on the document** — use `null`/`0` for genuinely absent fields
- If line items aren't itemized (simple receipt), Claude returns one synthetic line item with the total amount

## 7. Concurrency (`main.py::process_new_invoices`)

- Step 2 (Claude extraction) runs through a `ThreadPoolExecutor`, bounded by `MAX_CONCURRENT_EXTRACTIONS` (`.env`, default **4**)
- Safe because each extraction call is stateless — no shared conversation/context, so token cost per invoice is unaffected by concurrency; only wall-clock time drops
- Steps 3–5 (validate/append/notify) stay **single-threaded**, one invoice at a time, in the order extractions finish — deliberate, because the duplicate check reads/appends to `existing_rows` in memory and interleaving those writes across threads would race
- Same "load once, append locally" pattern applies to `master_vendors` in `apply_approved_vendors()`, to avoid a full-sheet re-read per pending row
- **If volume grows into hundreds-per-run**: raising `MAX_CONCURRENT_EXTRACTIONS` further will hit Anthropic per-minute rate limits before it helps — switch to the Message Batches API instead (async, ~half per-token cost, sidesteps rate limits) rather than raising the pool size indefinitely

## 8. The one design rule that matters

**The agent never writes to the Vendor Master spreadsheet on its own.** A flagged invoice only adds a vendor after a human sets `approve_vendor = TRUE` on its row in Invoice Log, and only takes effect on the *next* run (`apply_approved_vendors`). This is what stops one bad OCR read from silently polluting the reference vendor list. If asked to "just auto-add new vendors," push back — flag the trade-off before making that change, and only if the user explicitly insists.

Known gap: `add_vendor()` always inserts a brand-new `vendor_name` row — it doesn't check whether the approved name is a near-match for an existing row (e.g. approving "Costco Wholesale" when "COSTCO WHOLESALE" is already present) and merge it as an alias instead. Worth fixing if duplicate-ish vendor rows start piling up.

## 9. Sheet schema

**Invoice Log** (`INVOICE_SHEET_ID`, tab must be named exactly `Invoice Log`) — one row per invoice:

`date_received, logged_at (set once at append, never touched again), source (drive/email), file_name, vendor, invoice_number, invoice_date, subtotal, tax, total, po_number (blank if none), line_items (plain text, e.g. "Widget ($12.50); Gadget (-$2.00)" — never JSON), status (verified/needs_review), issue (";"-separated failed checks), approve_vendor (bool, human-set)`

**Vendor Master** (`VENDOR_MASTER_SHEET_ID`, **separate spreadsheet**, first tab read regardless of name):

`vendor_name, aliases (comma-separated), default_gl_code (optional), date_added`

Two spreadsheets, not two tabs — so a non-technical vendor-list owner can hold/edit their file without touching the invoice log.

## 10. Config / `.env` parameters

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for extraction |
| `GOOGLE_APPLICATION_CREDENTIALS` | `./credentials/service-account.json` | |
| `INVOICE_SHEET_ID` | — | required |
| `VENDOR_MASTER_SHEET_ID` | — | required, must be a **separate** spreadsheet |
| `DRIVE_WATCH_FOLDER_ID` | — | required for Drive intake |
| `GMAIL_QUERY` | `has:attachment label:Invoices` | intentionally no `is:unread` |
| `EMAIL_CHECK_START_DATE` | today (if unset) | only used on the very first run, before a checkpoint exists |
| `NOTIFY_EMAIL` | — | required for review alerts |
| `MAX_CONCURRENT_EXTRACTIONS` | `4` | raise cautiously, watch Anthropic rate limits |

`config.py` raises `RuntimeError` on a malformed `EMAIL_CHECK_START_DATE` (must be `YYYY-MM-DD`) and on any required value missing at the point it's actually used (`require()`).

## 11. Debugging a flagged row

Check the `issue` column first — it names exactly which check(s) failed:

- `vendor_not_found` — check the actual **Vendor Master** spreadsheet (not Invoice Log); the extracted string must match a `vendor_name`/`aliases` entry within the 0.8 fuzzy cutoff. A legitimate vendor with wording too different from what's stored (e.g. "Costco Wholesale" vs. stored "COSTCO") will flag even though it isn't really wrong.
- `math_mismatch` — line items / subtotal / tax / total don't reconcile within $0.02
- `po_malformed` — PO text doesn't look like a real identifier
- `po_not_found` — PO not on the open-PO list (only active once `po_list` is wired up)
- `duplicate_invoice` — matches vendor+invoice_number, or vendor+date+total, of an existing row

## 12. What's real vs. what needs credentials

- Fully working offline, no external deps: `validate_invoice.py`, `dedup.py` (covered by `tests/test_validate_invoice.py`)
- Complete and correct against real APIs, but need your own credentials to run: `extract_invoice.py`, `sheets_client.py`, `intake_drive.py`, `intake_gmail.py`, `notify.py`
