# data_ingestion_engine/utils/ingestion_rows.py
import os
from urllib.parse import urlparse

import pandas as pd


def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def read_ingestor_rows(local_path):
    ext = os.path.splitext(local_path)[1].lower()

    if ext == ".csv":
        df = pd.read_csv(local_path)
    else:
        try:
            df = pd.read_excel(local_path, "Ingestor")
        except ValueError:
            # 'Ingestor' sheet missing — fall back to the first sheet.
            df = pd.read_excel(local_path, 0)

    return df.to_dict(orient="records")


def read_ingestor_rows_dial_global(local_path):
    # Both CSV and Excel exports of the DIAL Global tracking workbook are
    # accepted — the data team produces either.
    ext = os.path.splitext(local_path)[1].lower()
    if ext == ".csv":
        # utf-8-sig strips a BOM if Excel wrote one; latin-1 is the last resort
        # so an accented company name ("Atkins Réalis", "Türk Telekom") fails
        # over to something readable instead of raising.
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                df = pd.read_csv(local_path, encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"Could not decode {local_path} as CSV")
        df = df.dropna(how="all")
        return df.to_dict(orient="records")

    # sheet_name=None makes pandas return a dict of {sheet_name: DataFrame} for
    # every sheet/tab in the workbook.
    all_sheets = pd.read_excel(local_path, sheet_name=None)

    combined_rows = []
    for sheet_name, df in all_sheets.items():
        # Drop fully-empty rows (blank rows some interns may have left in).
        df = df.dropna(how="all")
        combined_rows.extend(df.to_dict(orient="records"))

    return combined_rows


# Headers are looked up by alias so a rename in the tracking workbook cannot
# silently produce a column of nulls. First non-empty match wins, in the order
# listed. Header whitespace is stripped before lookup, which is load-bearing —
# one export shipped "Document Type " with a trailing space.
DIAL_GLOBAL_COLUMN_ALIASES = {
    "company_name": ["Company Name", "Company"],
    "document_type": ["Document Type", "Type of Document", "Type of Report"],
    "year": ["Year"],
    "document_url": [
        "Document URL (Google drive)",
        "Document URL (Google Drive)",
        "Document URL",
        "Link",
        "URL",
    ],
}


def normalise_row_keys(row):
    """Strip whitespace from every header so 'Document Type ' matches."""
    return {str(k).strip(): v for k, v in row.items()}


def get_row_value(row_norm, field):
    """
    Read one logical field from a whitespace-normalised row by alias.

    Returns None when every alias is missing or blank, so the caller can tell
    "the column is absent" from "the cell is empty" the same way.
    """
    for alias in DIAL_GLOBAL_COLUMN_ALIASES.get(field, []):
        if alias not in row_norm:
            continue
        value = row_norm[alias]
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("nan", "none"):
            return text
    return None


DIAL_GLOBAL_DOCUMENT_TYPE_MAP = {
    "annual report": "Annual Report",
    "sustainability": "Sustainability Report",
    "sustainability report": "Sustainability Report",
    "dei report": "DEI Report",
    "dei": "DEI Report",
}


def normalize_dial_global_document_type(raw_value):
    if raw_value is None:
        return None
    key = str(raw_value).strip().lower()
    return DIAL_GLOBAL_DOCUMENT_TYPE_MAP.get(key, str(raw_value).strip())
