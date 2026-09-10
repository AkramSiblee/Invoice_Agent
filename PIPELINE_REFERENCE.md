# Pipeline reference — validations, conditions, and design notes

A single consolidated reference for everything this agent checks, tolerates, or
enforces, and why. Detail for any one section also lives in `SKILL.md` /
`references/`; this file exists so it's all in one place to skim later.

## 1. Pipeline order

1. **Intake** — `intake_drive.py`, `intake_gmail.py`: find new files (and, for Gmail, new email-body text — see §4)
2. **Extract** — `extract_invoice.py`: OpenAI reads the file or email body text, returns structured JSON (including a best-effort `category` from a fixed taxonomy), or `null` if body text turns out not to be a real invoice
3. **Validate** — `validate_invoice.py`: resolves `vendor_id`, 5 checks described below
4. **Log** — `sheets_client.py::append_invoice_row`: one row per invoice in the `Invoice_Log` Google Sheet — this agent's OWN sheet, schema-matched to the Accounts Payable Agent's but never the same file (see `references/sheet_schema.md`)
5. **Notify** — `notify.py`: emails a human if any check failed
6. **Approve** — `main.py::apply_approved_vendors`: on the *next* run, picks up rows where a human set `approve_vendor = TRUE`, adds a minimal `Pending`-status vendor row, writes the new `vendor_id` back onto the row, re-validates

Run with `python scripts/main.py`. Meant to run on a schedule (cron / Task Scheduler) — each run only processes what's new since last time.

## 2. Validation checks (`validate_invoice.py`)

All checks run independently — a row can fail more than one, and every failure is recorded, not just the first. Result is `{"status": "verified" | "needs_review", "issues": [...]}`.

| # | Check | Condition | Issue code |
|---|---|---|---|
| 1 | **Vendor match** | Normalize (lowercase, strip to letters/digits) extracted vendor name; compare against `vendor_name` + every `aliases` entry in the Vendor_Master sheet. Exact match, or fuzzy match via `difflib.get_close_matches` at cutoff **0.8**. On match, resolves and returns the row's `vendor_id` (the join key the AP Agent's own pipeline keys on). | `vendor_not_found` |
| 2 | **Category check** | `category` must be exactly one of `AP_CATEGORIES` (`config.py`) — the same 7 values AP's `Matching_Rules.xlsx` is keyed on. `null`/blank is common and expected for a retail receipt with no natural fit (e.g. groceries) — it's flagged, not guessed. | `category_unresolved` (blank) / `category_invalid` (present but not one of the 7) |
| 3 | **Math check** | `sum(line_items[].amount)` must equal `subtotal` within **$0.02** (skipped if no line items). `subtotal + tax` must equal `total` within **$0.02**. | `math_mismatch` |
| 4 | **PO check** | If `po_number` present, must match `^[A-Za-z0-9\-]+$` (letters/digits/hyphens). **Missing PO is not a failure** — retail receipts legitimately have none, and AP's own pipeline has a `non_po` path for exactly this. Optional `po_list` param (not wired up yet) would additionally check membership in an open-PO list. | `po_malformed` / `po_not_found` |
| 5 | **Duplicate check** | If `invoice_number` present: match on `vendor_id` (or normalized `vendor_name` if not yet resolved) + normalized `invoice_number` against every row in the invoice log (including rows added earlier in the same run). If no `invoice_number`: fall back to vendor + `invoice_date` + `total` (within the $0.02 tolerance). A match **flags but does not skip** — two different invoices can coincidentally share a number, so a human decides. | `duplicate_invoice` |

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
- Scope is `gmail.readonly` for Gmail itself — nothing here ever modifies a message. If write behavior is ever added back, scope needs to change to `gmail.modify`. (Drive writes below use the separate `drive` scope.)
- **Search window** (`_search_query()`): first-ever run searches from `EMAIL_CHECK_START_DATE` (`.env`, defaults to **today** if unset — so a first run doesn't crawl entire mailbox history). Every run after that resumes from `state/gmail_last_checked.json`, a rolling unix-second checkpoint.
- Checkpoint only advances **after every matched message in the run finishes without error** — so a mid-run crash re-covers the same window next time (safe, since the ID ledger skips anything already handled instantly).
- A plain service account **cannot** read a personal Gmail inbox — needs either Workspace domain-wide delegation or a per-user OAuth flow. See the note at the top of `intake_gmail.py`.
- **Drive archiving**: every new (non-duplicate) attachment is also uploaded, byte-for-byte, into the watched Drive folder's `Attachments from Gmail` subfolder (found by name under `DRIVE_WATCH_FOLDER_ID`, created if somehow missing) — this agent does that directly now rather than depending on an external Zapier automation to forward Gmail attachments into Drive. Since `intake_drive.py` also walks that same folder tree, an archived file is exactly the kind of "same bytes from two sources" case `dedup.py`'s content-hash ledger exists for — it gets silently skipped there, not reprocessed.
- **`GMAIL_QUERY` default is `subject:(invoice OR invoices)`, with no `has:attachment` requirement** — deliberate: a real invoice can also arrive typed or forwarded directly into the email body with no file attached. (An earlier version used `label:Invoices`; if you ever point `GMAIL_QUERY` at a `label:X` clause again, that label has to actually exist on the account or the search silently matches zero messages every run rather than erroring — check with `service.users().labels().list()`.)
- **Body-text extraction**: `fetch_new_invoice_sources()` returns one job per new attachment (`{"kind": "file", "path": ...}`) AND, separately, one job per new matched message's plain-text body (`{"kind": "text", "text": ..., "label": ...}`) — `_extract_body_text()` prefers `text/plain`, falling back to a tag-stripped `text/html` for messages with no plain-text part. `main.py` routes `"file"` jobs through `extract_invoice_data()` and `"text"` jobs through `extract_invoice_data_from_text()`. Because subject-line matching pulls in plenty of messages that just *mention* the word "invoice" (replies, marketing, forwarded threads with no amounts), `extract_invoice_data_from_text()` can return `None` — Claude's `not_an_invoice` escape hatch (`TEXT_EXTRACTION_PROMPT` in `extract_invoice.py`) — and `main.py` skips logging a row for those rather than hallucinating fields to fit the schema.

## 5. Drive intake specifics (`intake_drive.py`)

- Paginated via `nextPageToken` (page size 1000) — folders over ~100 files won't silently get truncated.
- "New" = Drive file ID not already in `state/processed_drive_ids.json`.
- Downloaded file is hash-checked against `dedup.py`; if already seen, the local copy is deleted immediately after download (not skipped pre-download, since Drive doesn't expose content hashes cheaply via this call).

## 6. Extraction (`extract_invoice.py`)

- Model: `gpt-4o` via the OpenAI **Responses API** (swap the `MODEL` constant for cost/accuracy tradeoffs) — reads PDFs natively (`input_file` content block, base64 data URL) and images (`input_image`) without a separate OCR/rasterize step
- **Structured Outputs** (`text.format = {"type": "json_schema", "strict": true, ...}`) enforce the schema server-side — `EXTRACTION_SCHEMA` / `TEXT_EXTRACTION_SCHEMA` in `extract_invoice.py` — instead of relying on prompt instructions + a fenced-code-block strip, so malformed JSON shouldn't happen in practice
- Accepted file types: `application/pdf`, `image/jpeg`, `image/png`, `image/webp` — anything else raises `ValueError`
- `max_output_tokens=4096` — headroom for long itemized receipts (raise further if a real invoice ever gets truncated mid-JSON)
- Extraction schema (`EXTRACTION_PROMPT` + `EXTRACTION_SCHEMA`): `vendor`, `invoice_number` (nullable — falls back to receipt/transaction number), `invoice_date` (`YYYY-MM-DD`), `line_items[]` (`description`, `quantity`, `unit_price`, `amount`), `subtotal`, `tax`, `total`, `po_number` (nullable), `po_line` (nullable, only if the document itself references one), `category` (nullable, must be exactly one of `AP_CATEGORIES` in `config.py` or `null`), `currency`
- Explicit instruction: **never invent a value not visibly on the document** — use `null`/`0` for genuinely absent fields, and never force `category` to the closest-sounding value when nothing genuinely fits
- If line items aren't itemized (simple receipt), the model returns one synthetic line item with the total amount
- The text-extraction path's schema (`TEXT_EXTRACTION_SCHEMA`) makes every field nullable, not just the ones nullable in the file-extraction schema — OpenAI's strict Structured Outputs mode requires every property to be present in the response even when `not_an_invoice` is `true` and there's nothing to fill in

## 7. Concurrency (`main.py::process_new_invoices`)

- Step 2 (Claude extraction) runs through a `ThreadPoolExecutor`, bounded by `MAX_CONCURRENT_EXTRACTIONS` (`.env`, default **4**)
- Safe because each extraction call is stateless — no shared conversation/context, so token cost per invoice is unaffected by concurrency; only wall-clock time drops
- Steps 3–5 (validate/append/notify) stay **single-threaded**, one invoice at a time, in the order extractions finish — deliberate, because the duplicate check reads/appends to `existing_rows` in memory and interleaving those writes across threads would race
- Same "load once, append locally" pattern applies to `master_vendors` in `apply_approved_vendors()`, to avoid a full-sheet re-read per pending row
- **If volume grows into hundreds-per-run**: raising `MAX_CONCURRENT_EXTRACTIONS` further will hit OpenAI per-minute rate limits before it helps — switch to the [Batch API](https://platform.openai.com/docs/guides/batch) instead (async, ~half per-token cost, sidesteps rate limits) rather than raising the pool size indefinitely

## 8. The one design rule that matters

**The agent never writes to the Vendor_Master sheet on its own beyond a minimal `Pending`-status placeholder row.** A flagged invoice only adds a vendor after a human sets `approve_vendor = TRUE` on its row in the invoice log, and only takes effect on the *next* run (`apply_approved_vendors`) — which writes just `vendor_id`, `vendor_name`, `aliases`, `status="Pending"`; every other onboarding field (country, tax ID, payment terms, criticality, bank details) is left blank because Claude has no way to know it from an invoice, and stays blank until a human completes it directly in the sheet. The AP Agent's own `Active`-only gate keeps a `Pending` vendor from flowing into payment until then. This is what stops one bad OCR read from silently polluting shared reference data. If asked to "just auto-add new vendors," push back — flag the trade-off before making that change, and only if the user explicitly insists.

Known gap: `add_vendor()` always mints a brand-new `vendor_id` — it doesn't check whether the approved name is a near-match for an existing vendor row (e.g. approving "Costco Wholesale" when "COSTCO WHOLESALE" is already present) and merge it as an alias instead. Worth fixing if duplicate-ish vendor rows start piling up.

## 9. File schema

**Invoice_Log** and **Vendor_Master**, both Google Sheets in an "Agent Data" subfolder inside the watched `Invoice_Automation` Drive folder (`DRIVE_WATCH_FOLDER_ID`) — this agent's OWN sheets, schema-matched to the Accounts Payable Agent's own files so rows can be copied across by hand, but never the same physical file (see the incident note in `references/sheet_schema.md` for why). Full column list: `references/sheet_schema.md`.

## 10. Config / `.env` parameters

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | required for extraction |
| `DRIVE_WATCH_FOLDER_ID` | — | required for Drive intake; also the parent of this agent's own "Agent Data" sheets folder |
| `GMAIL_QUERY` | `subject:(invoice OR invoices)` | intentionally no `is:unread`; matches subject line only, no attachment or label required — body text is extracted separately (§4) |
| `EMAIL_CHECK_START_DATE` | today (if unset) | only used on the very first run, before a checkpoint exists |
| `NOTIFY_EMAIL` | — | required for review alerts |
| `MAX_CONCURRENT_EXTRACTIONS` | `4` | raise cautiously, watch OpenAI rate limits |

`config.py` raises `RuntimeError` on a malformed `EMAIL_CHECK_START_DATE` (must be `YYYY-MM-DD`) and on any required value missing at the point it's actually used (`require()`).

## 11. Debugging a flagged row

Check the `issue` column first — it names exactly which check(s) failed:

- `vendor_not_found` — check **Vendor_Master** (not the invoice log); the extracted string must match a `vendor_name`/`aliases` entry within the 0.8 fuzzy cutoff. A legitimate vendor with wording too different from what's stored (e.g. "Costco Wholesale" vs. stored "COSTCO") will flag even though it isn't really wrong.
- `category_unresolved` / `category_invalid` — no category could be assigned, or it isn't one of AP's 7 values. Often correct, not a bug — a retail/grocery receipt genuinely has no home in that taxonomy.
- `math_mismatch` — line items / subtotal / tax / total don't reconcile within $0.02
- `po_malformed` — PO text doesn't look like a real identifier
- `po_not_found` — PO not on the open-PO list (only active once `po_list` is wired up)
- `duplicate_invoice` — matches vendor+invoice_number, or vendor+date+total, of an existing row

## 12. What's real vs. what needs credentials

- Fully working offline, no external deps: `validate_invoice.py`, `dedup.py` (covered by `tests/test_validate_invoice.py`)
- Complete and correct against real APIs (OpenAI + Google), but need your own credentials to run: `extract_invoice.py`, `sheets_client.py`, `intake_drive.py`, `intake_gmail.py`, `notify.py`
