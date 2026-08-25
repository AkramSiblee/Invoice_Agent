"""
Thin wrapper around gspread for reading/writing the two spreadsheets this
agent uses: the Invoice Log (SHEET_ID) and the Vendor Master
(VENDOR_MASTER_SHEET_ID) — two separate files, not two tabs in one file.
Requires a service account JSON key with edit access to both
(share each spreadsheet with the service account's email address).
"""
from datetime import date, datetime

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_APPLICATION_CREDENTIALS, SHEET_ID, INVOICE_LOG_TAB, VENDOR_MASTER_SHEET_ID, require

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

INVOICE_LOG_COLUMNS = [
    "date_received", "logged_at", "source", "file_name", "vendor", "invoice_number",
    "invoice_date", "subtotal", "tax", "total", "po_number", "line_items",
    "status", "issue", "approve_vendor",
]


def _format_amount(amount) -> str:
    amount = float(amount or 0)
    return f"-${abs(amount):.2f}" if amount < 0 else f"${amount:.2f}"


def format_line_items(line_items: list[dict]) -> str:
    """Plain-text rendering for the sheet, e.g. 'Widget ($12.50); Gadget (-$2.00)'
    — not JSON, since a non-technical person reading the sheet shouldn't have to
    parse braces and quotes to see what was on an invoice."""
    if not line_items:
        return ""
    return "; ".join(
        f"{item.get('description', '') or '(no description)'} ({_format_amount(item.get('amount', 0))})"
        for item in line_items
    )


def _client():
    creds = Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_sheet():
    require(SHEET_ID, "INVOICE_SHEET_ID")
    return _client().open_by_key(SHEET_ID)


def _open_vendor_master():
    require(VENDOR_MASTER_SHEET_ID, "VENDOR_MASTER_SHEET_ID")
    return _client().open_by_key(VENDOR_MASTER_SHEET_ID)


def get_master_vendors() -> list[dict]:
    # .sheet1, not a name lookup: this file is dedicated to the vendor
    # list, so we just read its first tab regardless of what it's named.
    ws = _open_vendor_master().sheet1
    return ws.get_all_records()


def add_vendor(vendor_name: str, aliases: str = "") -> None:
    ws = _open_vendor_master().sheet1
    ws.append_row([vendor_name, aliases, "", date.today().isoformat()])


def get_invoice_log_rows() -> list[dict]:
    ws = _open_sheet().worksheet(INVOICE_LOG_TAB)
    return ws.get_all_records()


def append_invoice_row(record: dict, source: str, status: str, issues: list[str]) -> None:
    """Appends a row to the Invoice Log tab.

    Deliberately does not read the sheet back to report a row number: a
    trailing get_all_values() here would re-fetch the entire (ever-growing)
    log on every single invoice, which gets slower every run as the log
    grows. Nothing needs that row number today — callers that later do
    (e.g. a row-specific update right after append) should track it locally
    instead of paying for a full-sheet read."""
    ws = _open_sheet().worksheet(INVOICE_LOG_TAB)
    row = [
        date.today().isoformat(),
        datetime.now().isoformat(timespec="seconds"),
        source,
        record.get("source_file", ""),
        record.get("vendor", ""),
        record.get("invoice_number", ""),
        record.get("invoice_date", ""),
        record.get("subtotal", ""),
        record.get("tax", ""),
        record.get("total", ""),
        record.get("po_number", ""),
        format_line_items(record.get("line_items", [])),
        status,
        "; ".join(issues),
        "",
    ]
    ws.append_row(row)


def get_rows_pending_approval() -> list[dict]:
    """Rows still needs_review where a human has set approve_vendor = TRUE."""
    ws = _open_sheet().worksheet(INVOICE_LOG_TAB)
    records = ws.get_all_records()
    pending = []
    for i, r in enumerate(records, start=2):  # row 1 is the header
        if r.get("status") == "needs_review" and str(r.get("approve_vendor", "")).upper() == "TRUE":
            r["_row_number"] = i
            pending.append(r)
    return pending


def update_row_status(row_number: int, status: str, issues: list[str]) -> None:
    ws = _open_sheet().worksheet(INVOICE_LOG_TAB)
    status_col = INVOICE_LOG_COLUMNS.index("status") + 1
    issue_col = INVOICE_LOG_COLUMNS.index("issue") + 1
    ws.update_cell(row_number, status_col, status)
    ws.update_cell(row_number, issue_col, "; ".join(issues))
