import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validate_invoice import validate_invoice

MASTER_VENDORS = [
    {"vendor_id": "V-1001", "vendor_name": "Walmart", "aliases": "WALMART #4521, Walmart Inc"},
    {"vendor_id": "V-1002", "vendor_name": "Acme Supplies", "aliases": ""},
]

CATEGORY = "Facilities & Utilities"  # any valid AP_CATEGORIES value


def test_known_vendor_clean_invoice_passes():
    record = {
        "vendor": "WALMART #4521",
        "category": CATEGORY,
        "line_items": [{"amount": 45.00}],
        "subtotal": 45.00,
        "tax": 3.60,
        "total": 48.60,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "verified", result["issues"]
    assert result["vendor_id"] == "V-1001"


def test_unknown_vendor_flags_for_review():
    record = {
        "vendor": "XYZ Corp",
        "category": CATEGORY,
        "line_items": [{"amount": 100.0}],
        "subtotal": 100.0,
        "tax": 0,
        "total": 100.0,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert result["vendor_id"] is None
    assert any("vendor_not_found" in issue for issue in result["issues"])


def test_missing_category_flags_for_review():
    """Routine for a retail receipt with no natural fit (e.g. groceries) —
    it should be held for a human to assign a category, not guessed at."""
    record = {
        "vendor": "Walmart",
        "category": None,
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("category_unresolved" in issue for issue in result["issues"])


def test_invalid_category_flags_for_review():
    record = {
        "vendor": "Walmart",
        "category": "Groceries",  # not one of AP_CATEGORIES
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("category_invalid" in issue for issue in result["issues"])


def test_math_mismatch_flags_for_review():
    record = {
        "vendor": "Acme Supplies",
        "category": CATEGORY,
        "line_items": [{"amount": 50.0}],
        "subtotal": 50.0,
        "tax": 4.0,
        "total": 60.0,  # wrong, should be 54.0
        "po_number": "PO-1001",
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("math_mismatch" in issue for issue in result["issues"])


def test_fuzzy_vendor_match_catches_typo():
    record = {
        "vendor": "Walmrt",  # typo, not an exact alias
        "category": CATEGORY,
        "line_items": [{"amount": 10.0}],
        "subtotal": 10.0,
        "tax": 0.8,
        "total": 10.8,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "verified", result["issues"]
    assert result["vendor_id"] == "V-1001"


def test_malformed_po_flags_for_review():
    record = {
        "vendor": "Acme Supplies",
        "category": CATEGORY,
        "line_items": [{"amount": 20.0}],
        "subtotal": 20.0,
        "tax": 1.6,
        "total": 21.6,
        "po_number": "!!not a po??",
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("po_malformed" in issue for issue in result["issues"])


def test_duplicate_invoice_number_flags_for_review():
    existing_rows = [
        {"vendor_id": "V-1002", "vendor_name": "Acme Supplies", "invoice_number": "INV-500",
         "invoice_date": "2026-08-01", "total": 54.0},
    ]
    record = {
        "vendor": "Acme Supplies",  # same vendor, same invoice_number, different source
        "category": CATEGORY,
        "invoice_number": "INV-500",
        "invoice_date": "2026-08-01",
        "line_items": [{"amount": 50.0}],
        "subtotal": 50.0,
        "tax": 4.0,
        "total": 54.0,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS, existing_rows=existing_rows)
    assert result["status"] == "needs_review"
    assert any("duplicate_invoice" in issue for issue in result["issues"])


def test_duplicate_without_invoice_number_matches_on_date_and_total():
    existing_rows = [
        {"vendor_id": "V-1001", "vendor_name": "Walmart", "invoice_number": "",
         "invoice_date": "2026-08-01", "total": 16.2},
    ]
    record = {
        "vendor": "Walmart",
        "category": CATEGORY,
        "invoice_number": "",  # receipt with no number, e.g. retail till slip
        "invoice_date": "2026-08-01",
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS, existing_rows=existing_rows)
    assert result["status"] == "needs_review"
    assert any("duplicate_invoice" in issue for issue in result["issues"])


def test_no_existing_rows_skips_duplicate_check():
    record = {
        "vendor": "Walmart",
        "category": CATEGORY,
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)  # no existing_rows passed at all
    assert result["status"] == "verified", result["issues"]


def test_missing_po_is_not_a_failure():
    record = {
        "vendor": "Walmart",
        "category": CATEGORY,
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "verified", result["issues"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
