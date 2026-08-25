"""
Validation logic for extracted invoice records. Pure functions, no external
dependencies, so this is fully testable offline (see tests/test_validate_invoice.py).
"""
import difflib
import re

AMOUNT_TOLERANCE = 0.02
FUZZY_MATCH_CUTOFF = 0.8


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _vendor_matches(vendor: str, master_vendors: list[dict]) -> bool:
    if not vendor:
        return False
    normalized = _normalize(vendor)
    candidates = []
    for row in master_vendors:
        candidates.append(_normalize(row.get("vendor_name", "")))
        for alias in (row.get("aliases") or "").split(","):
            candidates.append(_normalize(alias))
    candidates = [c for c in candidates if c]
    if normalized in candidates:
        return True
    return bool(difflib.get_close_matches(normalized, candidates, n=1, cutoff=FUZZY_MATCH_CUTOFF))


def _math_checks_out(record: dict) -> bool:
    line_items = record.get("line_items") or []
    items_sum = sum(float(item.get("amount", 0) or 0) for item in line_items)
    subtotal = float(record.get("subtotal", 0) or 0)
    tax = float(record.get("tax", 0) or 0)
    total = float(record.get("total", 0) or 0)

    if line_items and abs(items_sum - subtotal) > AMOUNT_TOLERANCE:
        return False
    if abs((subtotal + tax) - total) > AMOUNT_TOLERANCE:
        return False
    return True


def _po_looks_valid(po_number) -> bool:
    if po_number in (None, ""):
        return True  # missing PO is fine, e.g. retail receipts
    return bool(re.match(r"^[A-Za-z0-9\-]+$", str(po_number)))


def _find_duplicate(record: dict, existing_rows: list[dict]) -> dict | None:
    """Matches on vendor + invoice_number when a number is present; falls back
    to vendor + invoice_date + total for receipts with no formal number
    (extraction still puts a transaction/receipt number in invoice_number
    when there is one — see references/sheet_schema.md)."""
    vendor = _normalize(record.get("vendor", ""))
    if not vendor:
        return None
    invoice_number = _normalize(str(record.get("invoice_number") or ""))

    for row in existing_rows:
        if _normalize(row.get("vendor", "")) != vendor:
            continue
        if invoice_number:
            if _normalize(str(row.get("invoice_number") or "")) == invoice_number:
                return row
            continue
        same_date = str(row.get("invoice_date", "")) == str(record.get("invoice_date", ""))
        try:
            same_total = abs(float(row.get("total", 0) or 0) - float(record.get("total", 0) or 0)) <= AMOUNT_TOLERANCE
        except (TypeError, ValueError):
            same_total = False
        if same_date and same_total:
            return row
    return None


def validate_invoice(
    record: dict,
    master_vendors: list[dict],
    po_list: list[str] | None = None,
    existing_rows: list[dict] | None = None,
) -> dict:
    """Returns {"status": "verified" | "needs_review", "issues": [str, ...]}.

    `existing_rows` is optional so callers that don't have the Invoice Log
    loaded (e.g. the vendor-approval re-validation pass) don't need to fake it."""
    issues = []

    if not _vendor_matches(record.get("vendor", ""), master_vendors):
        issues.append(f"vendor_not_found: '{record.get('vendor')}' is not in the master vendor list")

    if not _math_checks_out(record):
        issues.append("math_mismatch: line items / subtotal / tax / total don't reconcile")

    if not _po_looks_valid(record.get("po_number")):
        issues.append(f"po_malformed: '{record.get('po_number')}'")

    if po_list is not None and record.get("po_number") and record["po_number"] not in po_list:
        issues.append(f"po_not_found: '{record.get('po_number')}' is not an open PO")

    if existing_rows:
        dup = _find_duplicate(record, existing_rows)
        if dup is not None:
            issues.append(
                f"duplicate_invoice: matches an existing row for vendor '{record.get('vendor')}'"
                + (f", invoice_number '{record.get('invoice_number')}'" if record.get("invoice_number")
                   else f" (same invoice_date and total, no invoice_number to key on)")
            )

    return {"status": "verified" if not issues else "needs_review", "issues": issues}
