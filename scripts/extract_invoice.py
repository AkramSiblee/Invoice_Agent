"""
Reads a PDF or image invoice/receipt and returns structured data using Claude.
Works for formal vendor invoices and plain retail receipts (Walmart, etc.) alike.
"""
import base64
import json
import mimetypes
from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY, require

# Swap this for whichever current model fits your cost/accuracy needs.
MODEL = "claude-sonnet-5"

EXTRACTION_PROMPT = """You are reading a vendor invoice or store receipt (it may be a PDF or a photo of a paper receipt, e.g. from Walmart or any other shop).

Extract the following fields and return ONLY a JSON object, no other text, no markdown fences:

{
  "vendor": string,
  "invoice_number": string or null (use the receipt/transaction number if there's no formal invoice number),
  "invoice_date": string in YYYY-MM-DD format,
  "line_items": [ { "description": string, "quantity": number, "unit_price": number, "amount": number } ],
  "subtotal": number,
  "tax": number,
  "total": number,
  "po_number": string or null,
  "currency": string, e.g. "USD"
}

If a field genuinely isn't present on the document, use null (or 0 for money you can compute from other fields). Never invent a value that isn't visibly on the document. If line items aren't itemized (e.g. a simple receipt with just a total), return a single line item with a reasonable description and the total amount.
"""


def _media_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        raise ValueError(f"Unsupported file type for {file_path}: {mime}")
    return mime


def extract_invoice_data(file_path: str) -> dict:
    """Send a PDF or image to Claude and return the extracted fields as a dict."""
    require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    media_type = _media_type(file_path)
    data = base64.standard_b64encode(Path(file_path).read_bytes()).decode("utf-8")
    block_type = "document" if media_type == "application/pdf" else "image"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data}},
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
    )

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        record = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude did not return valid JSON for {file_path}: {text[:200]}") from e

    record["source_file"] = Path(file_path).name
    return record


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python extract_invoice.py <path-to-invoice>")
        sys.exit(1)
    print(json.dumps(extract_invoice_data(sys.argv[1]), indent=2))
