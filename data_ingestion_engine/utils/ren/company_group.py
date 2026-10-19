# data_ingestion_engine/utils/ren/company_group.py
#
# A ReN company is not always one row in the canonical companies table.
#
# The same business is often ingested several times under separate entities —
# a listing, an ADR line, a renamed successor — and its filings scatter across
# them. Barclays' 2025 annual report can sit under one entity while 2023 and
# 2024 sit under another, so resolving to a single company_id and reading only
# its documents silently loses years that were ingested perfectly well.
#
# This module treats every company sharing a normalised name as one group and
# reads all of their documents together. Selection and year-grouping downstream
# are unchanged: they still see a flat document list and still pick at most one
# document per type per year.

import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# DataX company search. Verified against prod: the parameter is `query` — `q`
# returns 422 "Field required" — and the response is a bare JSON list of
# companies carrying company_id, name, symbol, exchange, isin and sector.
# Env-overridable so the path can be corrected without a code change.
COMPANY_SEARCH_PATH = os.getenv("DATAX_COMPANY_SEARCH_PATH", "/search/v2")

# Server-side maximum. Requesting more returns 422 "Input should be less than
# or equal to 50", and the default without it is 20 — too few for a common name
# where the exact-match entities may rank below near-matches.
COMPANY_SEARCH_LIMIT = int(os.getenv("DATAX_COMPANY_SEARCH_LIMIT", "50"))

# Upper bound on how many same-name entities are pulled into one group. A
# generic name ("Holdings") could otherwise sweep in hundreds of unrelated
# companies and turn one extraction into a crawl of the whole table.
MAX_GROUP_MEMBERS = int(os.getenv("REN_MAX_GROUP_MEMBERS", "12"))

GROUP_FETCH_WORKERS = int(os.getenv("REN_GROUP_FETCH_WORKERS", "8"))

# Everything that is not a letter or digit. Removed outright rather than
# replaced with a space: "N.V." must equal "NV", which it does not if the dots
# become spaces ("n v" != "nv").
_NON_ALNUM = re.compile(r"[^0-9a-z]+", re.UNICODE)

# Letters that are NOT accented forms, so NFKD leaves them intact and the
# alphanumeric filter would delete them outright — turning "Møller" into
# "mller" rather than "moller". The cohort is full of Nordic and German names,
# and these are exactly the companies whose entities get spelled both ways.
_TRANSLITERATE = str.maketrans({
    "ø": "o", "æ": "ae", "å": "a", "ð": "d", "þ": "th",
    "ß": "ss", "œ": "oe", "ł": "l", "đ": "d", "ħ": "h", "ı": "i",
})


def normalise_company_name(name: Optional[str]) -> str:
    """
    Casefold and strip every separator, so only letters and digits remain.

    "ABN AMRO Bank N.V." and "abn amro bank nv" are the same company and must
    group; "Barclays PLC" and "Barclays Bank UK PLC" are not and must not.

    Deliberately does NOT strip legal suffixes. Dropping "PLC"/"Ltd"/"Holdings"
    would merge a parent with its subsidiary and attribute one company's
    disclosure to another — a worse error than missing a document.
    """
    if not name:
        return ""
    # Decompose accents and drop the combining marks, so "Société Générale"
    # equals "Societe Generale". The cohort is mostly European listings and the
    # two spellings appear interchangeably across entities.
    folded = str(name).casefold().translate(_TRANSLITERATE)
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", stripped)


def find_companies_by_name(name: str, uid: str) -> List[Dict[str, Any]]:
    """
    Every canonical company whose normalised name equals `name`.

    Returns [] on any failure — a search that does not answer must degrade to
    today's single-company behaviour rather than fail an extraction.
    """
    from data_ingestion_engine.utils.store_data import DATAX_INTERNAL, _headers, _session

    target = normalise_company_name(name)
    if not target:
        return []

    try:
        with _session() as session:
            response = session.get(
                f"{DATAX_INTERNAL}{COMPANY_SEARCH_PATH}",
                headers=_headers(uid),
                # limit is capped at 50 server-side; anything higher is a 422.
                params={"query": name, "limit": COMPANY_SEARCH_LIMIT},
                timeout=30,
            )
        if response.status_code != 200:
            logger.warning("[ren] company search %s -> %s", COMPANY_SEARCH_PATH,
                           response.status_code)
            return []
        payload = response.json()
    except Exception as e:
        logger.warning("[ren] company search failed for %r: %s", name, e)
        return []

    # Accept either a bare list or a paginated envelope.
    if isinstance(payload, dict):
        payload = (payload.get("results") or payload.get("items")
                   or payload.get("companies") or payload.get("data") or [])
    if not isinstance(payload, list):
        return []

    matches = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        candidate = row.get("name") or row.get("company_name")
        if normalise_company_name(candidate) != target:
            continue
        company_id = row.get("company_id") or row.get("id")
        if company_id is None:
            continue
        matches.append({"company_id": int(company_id), "name": candidate})
    return matches


def company_group_ids(company_id: int, uid: str,
                      name: Optional[str] = None) -> List[int]:
    """
    Every company id that shares `company_id`'s normalised name, itself included.

    `company_id` always comes first and is always present, so a failed or empty
    search leaves behaviour exactly as it was.
    """
    from data_ingestion_engine.utils.store_data import fetch_company

    if name is None:
        record = fetch_company(company_id, uid) or {}
        name = record.get("name") or record.get("company_name")
    if not name:
        return [int(company_id)]

    ids = [int(company_id)]
    for match in find_companies_by_name(name, uid):
        if match["company_id"] not in ids:
            ids.append(match["company_id"])

    if len(ids) > MAX_GROUP_MEMBERS:
        logger.warning(
            "[ren] name %r matched %s companies; capping the group at %s",
            name, len(ids), MAX_GROUP_MEMBERS)
        ids = ids[:MAX_GROUP_MEMBERS]

    if len(ids) > 1:
        logger.info("[ren] company_id=%s groups with %s (name=%r)",
                    company_id, ids[1:], name)
    return ids


def fetch_reports_for_company_group(
    company_id: int,
    uid: str,
    group: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """
    Documents for every company in `company_id`'s name group, merged.

    Shaped exactly like fetch_reports_for_company() so select_ren_documents()
    and the engine are unchanged. Each report keeps the entity it came from in
    `source_company_id`, so a row's provenance stays traceable when one
    company-year is assembled from two entities.

    De-duplicated on report_id, because the same filing is sometimes ingested
    under more than one entity.
    """
    from data_ingestion_engine.utils.store_data import (
        fetch_document_metadata, fetch_reports_for_company)

    ids = list(group) if group else company_group_ids(company_id, uid)

    # Preferred path: one lightweight call for the whole group. The metadata
    # endpoint selects 8 columns in SQL instead of every column including the
    # analysis text, so it is ~96% smaller AND takes every id at once — one
    # round trip for a group instead of up to twelve.
    #
    # No report_type filter: the stored values include 10-K, "Combined Pay Gap
    # Report" and "Sustainability Policy/framework", and filtering server-side
    # on the four CITIES labels drops 23.9% of usable documents. Canonicalising
    # client-side is what handles that.
    rows = fetch_document_metadata(ids, uid)

    if rows is None:
        # Endpoint not deployed, or the call failed. Fall back to the old
        # per-company route so behaviour degrades rather than breaks.
        logger.info("[ren] document metadata unavailable; falling back to "
                    "per-company reads for %s entities", len(ids))
        rows = []
        seen_ids = set()

        def fetch(cid):
            payload = fetch_reports_for_company(cid, uid) or {}
            # All FOUR buckets. `policies` is where Sustainability Policy, Speak
            # Up/Whistleblower and Supplier Code of Conduct live, and `others`
            # holds rows whose report_type was recorded as "Others" but whose
            # title classifies fine. Reading only reports+filings discarded both.
            found = [*(payload.get("reports") or []),
                     *(payload.get("filings") or []),
                     *(payload.get("policies") or []),
                     *(payload.get("others") or [])]
            for row in found:
                row.setdefault("company_id", cid)
            return cid, found

        with ThreadPoolExecutor(max_workers=min(GROUP_FETCH_WORKERS, len(ids))) as pool:
            for future in as_completed([pool.submit(fetch, cid) for cid in ids]):
                try:
                    cid, found = future.result()
                except Exception as e:
                    logger.warning("[ren] could not read documents for a group "
                                   "member: %s", e)
                    continue
                rows.extend(found)

    merged: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = row.get("report_id")
        if key is not None:
            if key in seen:
                # The same filing is sometimes ingested under two entities.
                continue
            seen.add(key)
        # Which entity this document was filed under, so a company-year
        # assembled from two of them stays traceable.
        row.setdefault("source_company_id", row.get("company_id"))
        merged.append(row)

    if len(ids) > 1:
        logger.info("[ren] company_id=%s: %s documents across %s entities",
                    company_id, len(merged), len(ids))
    return {"reports": merged, "filings": []}
