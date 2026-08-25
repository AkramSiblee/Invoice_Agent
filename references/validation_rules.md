# Validation rules

Applied in `scripts/validate_invoice.py::validate_invoice()`. All three checks run independently — a row can fail more than one at once, and every failure reason is recorded, not just the first.

## 1. Vendor match

Normalize (lowercase, strip everything but letters/digits) the extracted vendor name and compare against `vendor_name` and every entry in `aliases` for each row in Master Vendor List. Falls back to fuzzy matching (`difflib.get_close_matches`, cutoff `0.8`) to catch near-misses like "Walmrt" vs "Walmart" or minor OCR noise.

No match at all → issue: `vendor_not_found`.

## 2. Math check

- `sum(line_items[].amount)` must equal `subtotal` within `$0.02` (only checked when line items are present — some receipts arrive with just a total)
- `subtotal + tax` must equal `total` within `$0.02`

Either failing → issue: `math_mismatch`.

## 3. PO check

- If `po_number` is present, it just needs to look like a real identifier (letters, digits, hyphens). A missing PO is **not** a failure by itself — retail receipts like Walmart legitimately have none.
- Malformed PO text → issue: `po_malformed`
- If you later wire up an open-PO list, pass it as `po_list=[...]` to `validate_invoice()` — the hook already exists — and a PO that isn't on that list will raise `po_not_found`.

## 4. Duplicate check

Guards against the same invoice arriving twice through different intake
paths — e.g. it's emailed AND someone also drops a copy in the watched
Drive folder. This is the last of three duplicate-protection layers (see
"Duplicate protection" in `SKILL.md` for the full picture); the earlier two
run before extraction even happens:

- The per-source intake ledgers (`state/processed_*_ids.json`) only dedup
  within one source by that source's native ID (Gmail message ID / Drive
  file ID) — they don't catch a duplicate arriving under a different ID.
- `scripts/dedup.py` catches exact-byte duplicates across sources (the
  same file, regardless of filename or ID) and skips them silently before
  they ever reach extraction or this check.

This check is what's left for when the bytes *aren't* identical — a
rescanned copy, a re-exported PDF of the same invoice — but the extracted
data still points at the same real-world invoice.

- If `invoice_number` is present: match on normalized `vendor` + normalized
  `invoice_number` against every row already in Invoice Log (including
  rows added earlier in the same run).
- If there's no `invoice_number` at all: fall back to `vendor` +
  `invoice_date` + `total` (within the same `$0.02` tolerance as the math
  check).

Match found → issue: `duplicate_invoice`. The row is still logged and
still flagged `needs_review`, not silently dropped — two genuinely
different invoices can coincidentally share a number, so a human makes
the call, same as every other check here.

`existing_rows` is an optional argument to `validate_invoice()`; omitting
it (as `apply_approved_vendors()` does when re-validating a single row)
skips this check rather than erroring.

## Tuning

`AMOUNT_TOLERANCE` and `FUZZY_MATCH_CUTOFF` are constants at the top of `validate_invoice.py`. Loosen the cutoff if legitimate vendors are getting flagged too often; tighten it if unrelated vendors are matching each other.
