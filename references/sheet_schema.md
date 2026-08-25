# Sheet schema

Two SEPARATE spreadsheets, not two tabs in one file — `INVOICE_SHEET_ID`
and `VENDOR_MASTER_SHEET_ID` in `.env`. This split exists so a non-technical
vendor list owner can hold and edit their own file without touching the
invoice log, and vice versa.

## Invoice Log spreadsheet

One tab, named `Invoice Log` exactly (the code looks it up by name via
`INVOICE_LOG_TAB` in `config.py`). One row per invoice.

One row per invoice.

| Column | Type | Notes |
|---|---|---|
| date_received | date | when the file was picked up |
| logged_at | datetime | when this row was actually written to the sheet — set once, at append time; never touched again (e.g. by the vendor re-validation pass) |
| source | text | `drive` or `email` |
| file_name | text | original file name |
| vendor | text | as extracted |
| invoice_number | text | receipt/transaction number if there's no formal invoice number |
| invoice_date | date | |
| subtotal | number | |
| tax | number | |
| total | number | |
| po_number | text | blank if none |
| line_items | text | plain text, not JSON — non-technical readers shouldn't have to parse braces and quotes. Format: `description ($amount); description ($amount)`, e.g. `Widget ($12.50); Gadget (-$2.00)`. Built by `sheets_client.py::format_line_items()` from the extracted list of `{description, amount}` dicts — that structured form is still what `validate_invoice.py`'s math check operates on; only the sheet's rendering is plain text. |
| status | text | `verified` or `needs_review` |
| issue | text | blank, or a `;`-separated list of which checks failed |
| approve_vendor | boolean | human sets to `TRUE` to approve adding a new vendor found on this row |

## Vendor Master spreadsheet

A separate spreadsheet (`VENDOR_MASTER_SHEET_ID`). The code reads/writes
its **first tab regardless of what it's named** (`sheets_client.py` uses
`.sheet1`, not a name lookup) — this file belongs to whoever owns the
vendor list, so it shouldn't have to have a specifically-named tab to work.

| Column | Type | Notes |
|---|---|---|
| vendor_name | text | canonical name |
| aliases | text | comma-separated, e.g. `WALMART #4521, Walmart Inc` |
| default_gl_code | text | optional |
| date_added | date | |

This sheet is only ever written to after a human sets `approve_vendor = TRUE` on a flagged row in Invoice Log — the agent never adds a vendor on its own. See the design rule in `SKILL.md`.

Note: `add_vendor()` currently always adds a brand-new `vendor_name` row —
it doesn't check whether an approved name is a near-match for an existing
row (e.g. approving "Costco Wholesale" when "COSTCO WHOLESALE" is already
there) and add it as an alias instead. Worth fixing if duplicate-ish vendor
rows start piling up.
