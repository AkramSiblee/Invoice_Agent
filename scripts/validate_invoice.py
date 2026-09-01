"""
Validation logic for extracted invoice records. Pure functions, no external
dependencies, so this is fully testable offline (see tests/test_validate_invoice.py).
"""
import difflib
import re

from config import AP_CATEGORIES

AMOUNT_TOLERANCE = 0.02
FUZZY_MATCH_CUTOFF = 0.8


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def resolve_vendor_id(vendor: str, master_vendors: list[dict]) -> str | None:
    """Matches the extracted vendor name against vendor_name + aliases in
    the AP Agent's Vendor_Master, returning its vendor_id — the join key
    everything downstream (matching, DOA, tax, scheduling) keys on."""
    if not vendor:
        return None
    normalized = _normalize(vendor)
    candidates = {}  # normalized name/alias -> vendor_id
    for row in master_vendors:
        vendor_id = row.get("vendor_id")
        if not vendor_id:
            continue
        name = _normalize(row.get("vendor_name", ""))
        if name:
            candidates[name] = vendor_id
        for alias in (row.get("aliases") or "").split(","):
            alias = _normalize(alias)
            if alias:
                candidates[alias] = vendor_id

    if normalized in candidates:
        return candidates[normalized]
    match = difflib.get_close_matches(normalized, candidates.keys(), n=1, cutoff=FUZZY_MATCH_CUTOFF)
    return candidates[match[0]] if match else None


def _category_issue(category) -> str | None:
    """Category drives which Matching_Rules tolerance applies downstream —
    a value outside AP's fixed 7-category list can't be matched to a rule,
    and no category at all (routine for a retail receipt with no natural
    fit, e.g. groceries) means a human needs to assign one before this
    invoice can flow into AP's matching/payment path."""
    if not category:
        return "category_unresolved: no category could be assigned"
    if category not in AP_CATEGORIES:
        return f"category_invalid: '{category}' is not one of {AP_CATEGORIES}"
    return None


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
        return True  # missing PO is fine, e.g. retail receipts (AP's own non_po path)
    return bool(re.match(r"^[A-Za-z0-9\-]+$", str(po_number)))


def _find_duplicate(record: dict, existing_rows: list[dict], vendor_id: str | None) -> dict | None:
    """Matches on vendor_id (or vendor name, if not yet resolved) +
    invoice_number when a number is present; falls back to vendor +
    invoice_date + total for receipts with no formal number."""
    invoice_number = _normalize(str(record.get("invoice_number") or ""))
    vendor_norm = _normalize(record.get("vendor", ""))
    if not vendor_id and not vendor_norm:
        return None

    for row in existing_rows:
        if vendor_id and row.get("vendor_id"):
            if row.get("vendor_id") != vendor_id:
                continue
        elif _normalize(row.get("vendor_name") or row.get("vendor") or "") != vendor_norm:
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
    """Returns {"status": "verified" | "needs_review", "issues": [str, ...],
    "vendor_id": str | None}.

    `existing_rows` is optional so callers that don't have the Invoice Log
    loaded (e.g. the vendor-approval re-validation pass) don't need to fake it."""
    issues = []

    vendor_id = resolve_vendor_id(record.get("vendor", ""), master_vendors)
    if vendor_id is None:
        issues.append(f"vendor_not_found: '{record.get('vendor')}' is not in the master vendor list")

    category_issue = _category_issue(record.get("category"))
    if category_issue:
        issues.append(category_issue)

    if not _math_checks_out(record):
        issues.append("math_mismatch: line items / subtotal / tax / total don't reconcile")

    if not _po_looks_valid(record.get("po_number")):
        issues.append(f"po_malformed: '{record.get('po_number')}'")

    if po_list is not None and record.get("po_number") and record["po_number"] not in po_list:
        issues.append(f"po_not_found: '{record.get('po_number')}' is not an open PO")

    if existing_rows:
        dup = _find_duplicate(record, existing_rows, vendor_id)
        if dup is not None:
            issues.append(
                f"duplicate_invoice: matches an existing row for vendor '{record.get('vendor')}'"
                + (f", invoice_number '{record.get('invoice_number')}'" if record.get("invoice_number")
                   else " (same invoice_date and total, no invoice_number to key on)")
            )

    return {"status": "verified" if not issues else "needs_review", "issues": issues, "vendor_id": vendor_id}
