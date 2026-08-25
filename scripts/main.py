"""
Orchestrates the full pipeline: intake -> extract -> log -> validate -> notify,
plus a pass that applies any human-approved vendor additions.

Run with: python scripts/main.py
"""
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import MAX_CONCURRENT_EXTRACTIONS
from extract_invoice import extract_invoice_data
from validate_invoice import validate_invoice
from sheets_client import (
    get_master_vendors, get_invoice_log_rows, append_invoice_row, add_vendor,
    get_rows_pending_approval, update_row_status,
)
from notify import send_review_email
import intake_drive
import intake_gmail


def process_new_invoices():
    master_vendors = get_master_vendors()
    # Loaded once and appended to in-memory as we go, so two copies of the
    # same invoice arriving from different sources in the same run (e.g.
    # emailed AND dropped in the watched Drive folder) still catch each other,
    # not just invoices that were already in the sheet before this run.
    existing_rows = get_invoice_log_rows()

    jobs = [
        (source_name, file_path)
        for source_name, file_paths in (
            ("drive", intake_drive.fetch_new_files()),
            ("email", intake_gmail.fetch_new_attachments()),
        )
        for file_path in file_paths
    ]
    if not jobs:
        return

    # Extraction is the slow, token-spending step (one Claude call per file),
    # and each call is fully independent — no shared conversation, no state
    # carried between invoices — so token cost per invoice stays flat
    # regardless of batch size. That independence is exactly what makes it
    # safe to fan these calls out to a bounded pool of concurrent workers:
    # wall-clock time on a large batch drops from O(n) sequential API round
    # trips to roughly O(n / MAX_CONCURRENT_EXTRACTIONS), which is what
    # actually matters as invoice volume grows.
    #
    # Everything after extraction (validate, append, notify) stays
    # single-threaded on the main thread: it's cheap local logic, not an API
    # call, and the duplicate check depends on existing_rows being updated
    # one invoice at a time — parallelizing it would just add races for no
    # benefit.
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_EXTRACTIONS) as pool:
        futures = {
            pool.submit(extract_invoice_data, file_path): (source_name, file_path)
            for source_name, file_path in jobs
        }
        for future in as_completed(futures):
            source_name, file_path = futures[future]
            try:
                record = future.result()
                result = validate_invoice(record, master_vendors, existing_rows=existing_rows)
                append_invoice_row(record, source_name, result["status"], result["issues"])
                existing_rows.append(record)
                if result["status"] == "needs_review":
                    send_review_email(record, result["issues"])
                print(f"[{result['status']}] {file_path}")
            except Exception:
                print(f"[error] {file_path}")
                traceback.print_exc()


def apply_approved_vendors():
    """Second pass: pick up any rows a human marked approve_vendor = TRUE."""
    pending = get_rows_pending_approval()
    if not pending:
        return

    # Loaded once, then appended to locally as vendors are added — same
    # pattern as existing_rows above. Re-fetching the whole vendor sheet
    # inside the loop would mean one full-sheet read per pending row, which
    # gets worse the more approvals land in a single run.
    master_vendors = get_master_vendors()

    for row in pending:
        vendor_name = row.get("vendor", "")
        if not vendor_name:
            continue
        add_vendor(vendor_name)
        master_vendors.append({"vendor_name": vendor_name, "aliases": ""})
        record = {
            "vendor": vendor_name,
            "line_items": [],
            "subtotal": row.get("subtotal", 0),
            "tax": row.get("tax", 0),
            "total": row.get("total", 0),
            "po_number": row.get("po_number"),
        }
        result = validate_invoice(record, master_vendors)
        update_row_status(row["_row_number"], result["status"], result["issues"])
        print(f"[approved] {vendor_name} added to master list, row re-validated as {result['status']}")


if __name__ == "__main__":
    process_new_invoices()
    apply_approved_vendors()
