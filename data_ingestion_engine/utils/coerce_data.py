import re
from typing import Optional, Any


def _coerce_int(v: Any, *, default: Optional[int] = None) -> Optional[int]:
    """Return int(v) if clean; otherwise default. Handles '-', '', 'None', 'N/A'."""
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s in {"", "-", "None", "none", "N/A", "n/a", "NA"}:
        return default
    try:
        return int(s)
    except Exception:
        return default


def _coerce_year(v: Any, *, fallback_text: str = "", default: int = 1900) -> int:
    """
    Try to coerce a 4-digit year. If v is '-', '', 'None', etc., try to find
    a 19xx/20xx in fallback_text (e.g., filename/title). Otherwise return default.
    """
    y = _coerce_int(v)
    if y and 1800 <= y <= 2100:
        return y
    # Try to sniff a year from text like "..._2021.pdf"
    m = re.search(r'(20\d{2}|19\d{2})', fallback_text)
    if m:
        try:
            y2 = int(m.group(1))
            if 1800 <= y2 <= 2100:
                return y2
        except Exception:
            pass
    return default


def _coerce_quarter(v: Any) -> Optional[str]:
    """
    Normalize quarter into 'Q1'..'Q4' or None. Accepts '1','Q1','q1','-',''.
    """
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in {"", "-", "NONE", "N/A"}:
        return None
    if s.startswith("Q") and len(s) == 2 and s[1] in "1234":
        return s
    if s in {"1", "2", "3", "4"}:
        return f"Q{s}"
    return None
