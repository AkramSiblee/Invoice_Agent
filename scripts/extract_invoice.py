"""
Reads a PDF/image invoice/receipt, OR the plain-text body of an email, and
returns structured data using Claude. Works for formal vendor invoices and
plain retail receipts (Walmart, etc.) alike.
"""
import base64
import json
import mimetypes
from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY, AP_CATEGORIES, require

# Swap this for whichever current model fits your cost/accuracy needs.
MODEL = "claude-sonnet-5"

_CATEGORY_LIST = "\n".join(f'  - "{c}"' for c in AP_CATEGORIES)

_FIELDS_JSON = f"""{{
  "vendor": string,
  "invoice_number": string or null (use the receipt/transaction number if there's no formal invoice number),
  "invoice_date": string in YYYY-MM-DD format,
  "line_items": [ {{ "description": string, "quantity": number, "unit_price": number, "amount": number }} ],
  "subtotal": number,
  "tax": number,
  "total": number,
  "po_number": string or null,
  "po_line": integer or null (a line number on the PO, e.g. "line 2" or "item 2" — only if the document itself references one; never guess),
  "category": string or null, must be EXACTLY one of the following, or null if none genuinely fits:
{_CATEGORY_LIST}
  Do not force a fit — e.g. a grocery or general retail receipt (food, household goods) fits none of these; return null rather than picking the closest-sounding one.
  "currency": string, e.g. "USD"
}}"""

EXTRACTION_PROMPT = f"""You are reading a vendor invoice or store receipt (it may be a PDF or a photo of a paper receipt, e.g. from Walmart or any other shop).

Extract the following fields and return ONLY a JSON object, no other text, no markdown fences:

{_FIELDS_JSON}

If a field genuinely isn't present on the document, use null (or 0 for money you can compute from other fields). Never invent a value that isn't visibly on the document. If line items aren't itemized (e.g. a simple receipt with just a total), return a single line item with a reasonable description and the total amount.
"""

# Used for email bodies matched by subject line alone (see intake_gmail.py's
# GMAIL_QUERY — it no longer requires has:attachment), so plenty of matches
# won't actually contain a real invoice: a reply discussing one, a marketing
# email that just uses the word, a forwarded thread with no amounts. The
# not_an_invoice escape hatch lets Claude say so instead of hallucinating
# fields to fit the schema.
TEXT_EXTRACTION_PROMPT = f"""You are reading the plain-text body of an email. It may be a forwarded order confirmation, invoice, or receipt — possibly mixed in with quoted headers, signatures, or unrelated marketing boilerplate.

First decide: does this email body actually contain a real invoice, receipt, or order confirmation with concrete line items and a total amount — not just a mention of the word "invoice"? If it does NOT, return exactly this JSON and nothing else:
{{"not_an_invoice": true}}

If it DOES, extract the following fields and return ONLY a JSON object, no other text, no markdown fences:

{_FIELDS_JSON}

If a field genuinely isn't present, use null (or 0 for money you can compute from other fields). Never invent a value that isn't visibly in the text. If line items aren't itemized, return a single line item with a reasonable description and the total amount.
"""


def _media_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        raise ValueError(f"Unsupported file type for {file_path}: {mime}")
    return mime


def _run_extraction(content: list[dict], source_name: str) -> dict | None:
    """Shared Claude call + JSON parsing for both the file-based and
    text-based extraction paths below. Returns None if the model reports
    `not_an_invoice` (only possible via TEXT_EXTRACTION_PROMPT — the file
    path never sends that escape hatch, so it should never see it)."""
    require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,  # headroom for long itemized receipts (e.g. 20+ line items)
        messages=[{"role": "user", "content": content}],
    )

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        record = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude did not return valid JSON for {source_name}: {text[:200]}") from e

    if record.get("not_an_invoice"):
        return None

    record["source_file"] = source_name
    return record


def extract_invoice_data(file_path: str) -> dict:
    """Send a PDF or image to Claude and return the extracted fields as a dict."""
    media_type = _media_type(file_path)
    data = base64.standard_b64encode(Path(file_path).read_bytes()).decode("utf-8")
    block_type = "document" if media_type == "application/pdf" else "image"

    content = [
        {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data}},
        {"type": "text", "text": EXTRACTION_PROMPT},
    ]
    record = _run_extraction(content, Path(file_path).name)
    if record is None:
        raise ValueError(f"Unexpected not_an_invoice response for a file extraction: {file_path}")
    return record


def extract_invoice_data_from_text(text: str, source_name: str) -> dict | None:
    """Send an email body's plain text to Claude and return the extracted
    fields, or None if Claude determines the body doesn't actually contain
    a real invoice/receipt — expected and common now that Gmail intake
    matches on subject line alone (see intake_gmail.py), not on having a
    genuine invoice attached."""
    content = [
        {"type": "text", "text": f"EMAIL BODY:\n\n{text}"},
        {"type": "text", "text": TEXT_EXTRACTION_PROMPT},
    ]
    return _run_extraction(content, source_name)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python extract_invoice.py <path-to-invoice>")
        sys.exit(1)
    print(json.dumps(extract_invoice_data(sys.argv[1]), indent=2))
