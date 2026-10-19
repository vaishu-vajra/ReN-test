# data_ingestion_engine/utils/ren/document_selection.py
#
# Which of a ReN company's documents feed a company-year extraction.
#
# Unlike DIAL Global — where the tracking workbook is already a curated set and
# every document is used — the canonical company_documents table holds
# everything ever ingested for a company. This module picks the subset the
# CITIES rubric actually needs.
#
# There are two selection rules here, for two pipelines:
#
#   select_ren_documents()      the ORIGINAL rule. Four report types, at most one
#                               document per type, all of them read together in a
#                               single model call. Left exactly as it was —
#                               tasks/ren/extract.py still depends on it.
#
#   select_all_ren_documents()  the PROGRESSIVE rule. Eleven report types, EVERY
#                               document of every type, ordered so the broadest
#                               source is read first. The progressive engine
#                               reads them one call at a time, carrying the
#                               still-unanswered questions forward, so breadth
#                               costs latency rather than context window.
#
# The four-type rule was dropping most of a company's evidence: the stored
# report_type values include GRI Index, Speakup/Whistleblower Policy, Supplier
# Code of Conduct, Sustainability Policy, Employee Information Report, TCFD Index
# and "Ethinicity Pay Gap Report" (sic), none of which the old patterns matched.
# All 1,113 ReN rows in the last problem-companies export were scored on exactly
# one document, which is the same fact seen from the other end.

import datetime
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ANNUAL_REPORT = "Annual Report"
DEI_REPORT = "DEI Report"
SUSTAINABILITY_REPORT = "Sustainability Report"
PAY_GAP_REPORT = "Gender Pay Gap Report"
ETHNICITY_PAY_GAP_REPORT = "Ethnicity Pay Gap Report"
EMPLOYEE_INFORMATION_REPORT = "Employee Information Report"
GRI_INDEX = "GRI Index"
TCFD_INDEX = "TCFD Index"
SUSTAINABILITY_POLICY = "Sustainability Policy"
SUPPLIER_CODE_OF_CONDUCT = "Supplier Code of Conduct"
SPEAK_UP = "Speak up/Whistleblower"

# Added 2026-08-26 from the classifier audit: 2,144 documents were being dropped
# with report_type "Others" (or a label no pattern matched) despite carrying
# evidence the rubric asks about directly. Each of these was chosen against a
# specific question block, not because it looked report-shaped:
#
#   Proxy Statement       Q37-Q39. For US filers the DEF 14A is where board and
#                         executive composition actually lives; the annual
#                         report often just cross-references it.
#   Remuneration Report   Q61 (remuneration ratio) and Q36 (inclusion linked to
#                         executive accountability). Equity is the weakest
#                         category in the cohort and this is a pay document.
#   Corporate Governance  Q23 (inclusive leadership behaviours defined in
#                         governance frameworks — answered 0.5% of the time),
#                         Q25, Q70, Q71, Q76.
#   Code of Conduct       Q18-Q22 (values, respectful workplace) and Q27.
#                         Distinct from Supplier Code of Conduct, which is
#                         matched first and is about the supply chain.
#   Modern Slavery        Q56, Q57 — supplier social screening and negative
#                         supply-chain impacts, both answered under 3%.
#   Human Rights Policy   Q57, Q60.
PROXY_STATEMENT = "Proxy Statement"
REMUNERATION_REPORT = "Remuneration Report"
CORPORATE_GOVERNANCE = "Corporate Governance Report"
CODE_OF_CONDUCT = "Code of Conduct"
MODERN_SLAVERY_STATEMENT = "Modern Slavery Statement"
HUMAN_RIGHTS_POLICY = "Human Rights Policy"

# ---------------------------------------------------------------------------
# The ORIGINAL four-type rule — unchanged
# ---------------------------------------------------------------------------
#
# A company-year is extracted when it holds ANY of the four report types. Every
# one of them can anchor a year on its own, including a Gender Pay Gap report:
# a pay gap report answers few of the 64 questions, so a year anchored on one
# alone scores low on evidence it genuinely does not contain. The company
# published something and it gets read; the low score is the finding.

# Any one of these makes a company-year worth extracting.
REN_ANCHOR_TYPES = [ANNUAL_REPORT, DEI_REPORT, SUSTAINABILITY_REPORT,
                    PAY_GAP_REPORT]

# Nothing is supplementary any more — kept so importers of this name still work.
REN_SUPPLEMENTARY_TYPES: List[str] = []

# The order documents are handed to the model in — primary filing first.
REN_DOCUMENT_ORDER = [ANNUAL_REPORT, DEI_REPORT, SUSTAINABILITY_REPORT,
                      PAY_GAP_REPORT]

# The instruction reads "DEI report if present, otherwise ESG/Sustainability",
# but Annual+DEI+ESG is also listed as a valid combination — so by default both
# are taken when both exist, on the same reasoning that drives company-year
# batching in the first place: a gap in one document is often filled by another.
# Flip this to True to make Sustainability a strict fallback used only when the
# company published no DEI report that year.
REN_SUSTAINABILITY_ONLY_IF_NO_DEI = False


# ---------------------------------------------------------------------------
# The PROGRESSIVE eleven-type rule
# ---------------------------------------------------------------------------

# Round order for the progressive engine, broadest coverage first.
#
# Every one of these documents is read either way, so the order does not decide
# WHAT is read — it decides how big a residual question catalogue each later
# round has to carry, and which document gets credited with a question answered
# in more than one place. Broadest-first minimises both.
#
# Annual and Sustainability reports lead because they are the only two that
# routinely carry the Q1-Q15 organisation metrics and touch every CITIES
# category. The index documents and single-topic policies trail because they
# answer a handful of questions each: GRI/TCFD are cross-reference tables
# (Q54, Q59, Q60), the Supplier Code covers supply-chain screening (Q56, Q57,
# Q69), and Speak Up covers grievance channels (Q20, Q27, Q77).
REN_ROUND_ORDER = [
    ANNUAL_REPORT,
    SUSTAINABILITY_REPORT,
    DEI_REPORT,
    EMPLOYEE_INFORMATION_REPORT,
    PROXY_STATEMENT,
    PAY_GAP_REPORT,
    ETHNICITY_PAY_GAP_REPORT,
    REMUNERATION_REPORT,
    CORPORATE_GOVERNANCE,
    CODE_OF_CONDUCT,
    MODERN_SLAVERY_STATEMENT,
    HUMAN_RIGHTS_POLICY,
    GRI_INDEX,
    TCFD_INDEX,
    SUSTAINABILITY_POLICY,
    SUPPLIER_CODE_OF_CONDUCT,
    SPEAK_UP,
]

# Every recognised type. Anything a document cannot be classified as is dropped —
# Modern Slavery Statement, Human Rights Policy, plain Code of Conduct and
# Remuneration Policy all exist in the data and are deliberately NOT here.
REN_ALL_TYPES = list(REN_ROUND_ORDER)

# How many documents of one type feed a single company-year. 0 means unlimited,
# which is the default: a company that files UK, Ireland and France gender pay
# gap reports separately should have all three read, and with one document per
# call there is no context window reason to pick between them.
REN_MAX_DOCS_PER_TYPE = int(os.getenv("REN_MAX_DOCS_PER_TYPE", "0"))

# Hard ceiling on documents per company-year. Rounds run sequentially, so this
# is the wall-clock guard: an archive-heavy company-year could otherwise turn
# into fifty back-to-back model calls inside one Celery task.
# Raised from 20 to 35 on 2026-08-27. The original value was set when a round
# was one DOCUMENT and 27 documents meant 27 sequential model calls inside one
# Celery task. Once a round became one report TYPE the sequential chain became
# bounded by the type count instead, so the per-year figure stopped being a
# wall-clock guard and started being pure evidence loss — the DGI cohort has 12
# company-years above 20 documents and one at 35.
#
# The cap that still binds is DIAL_GLOBAL_MAX_DOCS_PER_ROUND (6 documents in a
# single call), which is the real context-window guard.
REN_MAX_DOCS_PER_YEAR = int(os.getenv("REN_MAX_DOCS_PER_YEAR", "35"))

# Reporting years outside this window are treated as bad data, not as years.
#
# The stored `year` is not always a reporting year: a mastersheet once ingested
# a whole batch as 1900, and this cohort turned up documents dated 2030, 2048,
# 2081, 2082, 2085, 2090 and 2093. Left alone those are not merely noise —
# selection keeps the most recent `years`, so a document dated 2093 DISPLACES a
# real year, and the company-year that runs is built from whatever happens to
# carry the bad date.
#
# The upper bound is deliberately "next year", not "this year": a fiscal year
# ending in early 2027 is legitimately reported as 2027 during 2026.
REN_MIN_REPORTING_YEAR = int(os.getenv("REN_MIN_REPORTING_YEAR", "2000"))
REN_MAX_REPORTING_YEAR = int(
    os.getenv("REN_MAX_REPORTING_YEAR", str(datetime.date.today().year + 1)))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
#
# Matched in order — the FIRST pattern that hits wins, so this list is ordered
# narrow to broad and the order is the whole contract. Reordering it silently
# changes what every document is classified as.
#
# The values being matched are messy on purpose. Real stored report_type values
# include "10-K", "Combined Pay Gap Report", "Sustainability Policy/framework",
# "Speakup/Whistleblower Policy" and "Ethinicity Pay Gap Report" — the typo is in
# the source data, not here.
_TYPE_PATTERNS = [
    # EEO-1 style workforce filings. "Employee Policy" must NOT land here, hence
    # the required "information".
    (EMPLOYEE_INFORMATION_REPORT, re.compile(
        r"employ(?:ee|er)\s*information|\beeo-?1\b|"
        r"consolidated\s*eeo", re.I)),

    # Before the gender pattern, so "Gender Ethnicity Disability Pay Gap" and
    # the misspelled "Ethinicity Pay Gap Report" both land here rather than
    # being swallowed by the broader pay-gap match. Lookaheads rather than a
    # sequence because the two words appear in either order.
    (ETHNICITY_PAY_GAP_REPORT, re.compile(
        r"(?=.*(?:ethnic|ethinic|racial|\brace\b))(?=.*pay)", re.I | re.S)),

    (PAY_GAP_REPORT, re.compile(
        r"pay\s*gap|paygap|gender\s*pay|remuneration\s*ratio", re.I)),

    # Before the generic code-of-conduct idea: a plain "Code of Conduct" is not
    # one of the eleven and must stay unclassified, but a supplier/vendor one is.
    (SUPPLIER_CODE_OF_CONDUCT, re.compile(
        r"(?=.*(?:supplier|vendor|third[\s-]*party|business\s*partner))"
        r"(?=.*(?:code|conduct))", re.I | re.S)),

    (SPEAK_UP, re.compile(
        r"speak[\s-]*up|whistle[\s-]*blow", re.I)),

    # The substantive DEF 14A only. "Proxy Form", "Form of Proxy" and "Proxy
    # Results" are voting paperwork with no disclosure in them, and there are
    # ~55 of those in the corpus — matching them would spend a round per company
    # on a ballot.
    (PROXY_STATEMENT, re.compile(
        r"proxy\s*statement|\bdef\s*14-?a\b|"
        r"notice\s*of\s*annual\s*meeting\s*(?:and|&)\s*proxy", re.I)),

    (MODERN_SLAVERY_STATEMENT, re.compile(
        r"modern\s*slavery|human\s*traffick|forced\s*labou?r", re.I)),

    # Before the generic policy patterns: "Human Rights Policy" must not be
    # swallowed by Sustainability Policy's `responsib`/`policy` lookaheads.
    (HUMAN_RIGHTS_POLICY, re.compile(r"human\s*rights", re.I)),

    # Remuneration REPORT and POLICY both land here — the disclosure and the
    # framework answer the same questions. Guarded against the pay-gap patterns
    # above, which are matched first and are the more specific reading.
    (REMUNERATION_REPORT, re.compile(
        r"remuneration|compensation\s*(?:report|policy|discussion)|"
        r"\bsay[\s-]*on[\s-]*pay\b", re.I)),

    # Governance reports, guidelines, statements and board committee charters.
    # Before Sustainability Report — "corporate governance" would otherwise be
    # at risk from that pattern's `corporate\s*responsib` branch on titles that
    # carry both words.
    (CORPORATE_GOVERNANCE, re.compile(
        r"corporate\s*governance|governance\s*(?:report|guideline|statement|"
        r"framework)|(?:board|audit|nomination|compensation|remuneration)\s*"
        r"committee\s*charter", re.I)),

    # AFTER Supplier Code of Conduct, which is matched further up: a supplier
    # code is about the supply chain, this one is about the workforce.
    (CODE_OF_CONDUCT, re.compile(
        r"code\s*of\s*(?:conduct|ethics|business\s*conduct)|"
        r"business\s*principles|code\s*of\s*practice", re.I)),

    (GRI_INDEX, re.compile(r"\bgri\b", re.I)),
    (TCFD_INDEX, re.compile(r"\btcfd\b", re.I)),

    # Before DEI, so an "Annual Diversity Report" is filed as the annual report
    # it is. Universal Registration Document is the French annual filing;
    # "Annual Financial Report" is the same document under the title the LSE
    # uses, and 114 of them were being dropped for want of the optional word.
    (ANNUAL_REPORT, re.compile(
        r"annual\s*(?:financial\s*|integrated\s*)?report|"
        r"annual\s*(?:&|and)\s*accounts|10-?k\b|20-?f\b|"
        r"universal\s*registration\s*document", re.I)),

    (DEI_REPORT, re.compile(
        r"\bdei\b|\bdi\b|d\s*&\s*i|diversity|inclusion|equity|belonging", re.I)),

    # Before the sustainability REPORT pattern — "Sustainability
    # Policy/framework" is a stored value and must not fall through to Report.
    (SUSTAINABILITY_POLICY, re.compile(
        r"(?=.*(?:sustainab|\besg\b|environment|climate|responsib))"
        r"(?=.*(?:policy|framework|principle|commitment))", re.I | re.S)),

    (SUSTAINABILITY_REPORT, re.compile(
        r"sustainab|\besg\b|\bcsr\b|corporate\s*responsib|impact\s*report|"
        r"citizenship|non-?financial|integrated\s*report", re.I)),
]

# Where a document's type is read from, in descending confidence. report_type is
# the curated label; document_title is the fallback that recovers the rows typed
# "Others" whose title says EEO-1 or GRI Index (12 of 161 in the last ingestion
# mastersheet); document_type is the storage bucket — report/filing/policy/
# others/dei-page — and carries almost no signal, so it is tried last.
_TYPE_FIELDS = ("report_type", "document_title", "document_type")


def canonical_report_type(value: Optional[str]) -> Optional[str]:
    """
    Map one stored report_type / title / document_type string onto a label.

    Returns None for anything that is not one of the eleven — those documents
    are not part of the CITIES evidence base and are dropped.
    """
    text = (value or "").strip()
    if not text:
        return None
    for label, pattern in _TYPE_PATTERNS:
        if pattern.search(text):
            return label
    return None


def classify_document(doc: Dict[str, Any]) -> Optional[str]:
    """
    A document's canonical type, read from the best field that answers.

    Tries report_type, then document_title, then document_type — see
    _TYPE_FIELDS. Returns None when no field classifies.
    """
    for field in _TYPE_FIELDS:
        label = canonical_report_type(doc.get(field))
        if label:
            return label
    return None


def _doc_sort_key(doc: Dict[str, Any]):
    """
    Newest first within a type, so the most recent edition wins a tie.

    report_id is coerced rather than trusted: it arrives as an int from the
    metadata endpoint but as a string from a CSV or a differently-shaped
    payload, and an uncaught TypeError here fails the whole company-year during
    selection — before a single document has been read.
    """
    try:
        return -int(doc.get("report_id") or 0)
    except (TypeError, ValueError):
        return 0


def _group_by_type(docs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket a year's documents by canonical type, newest first within each."""
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs:
        doc_type = doc.get("document_type")
        if doc_type:
            by_type.setdefault(doc_type, []).append(doc)
    for bucket in by_type.values():
        bucket.sort(key=_doc_sort_key)
    return by_type


def select_documents_for_year(
    docs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Pick the documents for ONE company-year, or [] if the year is unusable.

    The ORIGINAL rule, used by the single-call pipeline. `docs` are that year's
    documents, each carrying a canonical `document_type`. Returns at most one
    document per type, ordered by REN_DOCUMENT_ORDER so the model reads the
    primary filing before the supporting ones.

    A year is unusable only when it holds nothing recognised at all — any one of
    the four anchor types is enough to anchor it.
    """
    by_type = _group_by_type(docs)

    if not any(t in by_type for t in REN_ANCHOR_TYPES):
        return []

    wanted = list(REN_DOCUMENT_ORDER)
    if REN_SUSTAINABILITY_ONLY_IF_NO_DEI and DEI_REPORT in by_type:
        wanted = [t for t in wanted if t != SUSTAINABILITY_REPORT]

    return [by_type[t][0] for t in wanted if t in by_type]


def select_all_documents_for_year(
    docs: List[Dict[str, Any]],
    max_per_type: int = None,
    max_per_year: int = None,
) -> List[Dict[str, Any]]:
    """
    Every recognised document for ONE company-year, in progressive round order.

    Unlike select_documents_for_year() there is no anchor requirement and no
    one-per-type limit: a year holding only a Speak Up policy is still a year
    with evidence in it, and three jurisdictional pay gap reports are three
    documents worth reading. The progressive engine reads them one call at a
    time, so breadth costs wall clock rather than context window.

    Ordered by REN_ROUND_ORDER, then newest report_id within a type.

    `max_per_type` (default REN_MAX_DOCS_PER_TYPE, 0 = unlimited) and
    `max_per_year` (default REN_MAX_DOCS_PER_YEAR) bound the round count. The
    per-year cap is applied last and in round order, so what it drops is always
    the lowest-value tail rather than an arbitrary slice.
    """
    if max_per_type is None:
        max_per_type = REN_MAX_DOCS_PER_TYPE
    if max_per_year is None:
        max_per_year = REN_MAX_DOCS_PER_YEAR

    by_type = _group_by_type(docs)

    ordered: List[Dict[str, Any]] = []
    for doc_type in REN_ROUND_ORDER:
        bucket = by_type.get(doc_type)
        if not bucket:
            continue
        if max_per_type and len(bucket) > max_per_type:
            logger.info("[ren] %s: keeping %s of %s documents (max_per_type)",
                        doc_type, max_per_type, len(bucket))
            bucket = bucket[:max_per_type]
        ordered.extend(bucket)

    if max_per_year and len(ordered) > max_per_year:
        dropped = [d.get("document_type") for d in ordered[max_per_year:]]
        logger.warning(
            "[ren] company-year holds %s documents; capping at %s. Dropped: %s",
            len(ordered), max_per_year, dropped)
        ordered = ordered[:max_per_year]

    return ordered


def normalise_ren_documents(payload: Any) -> List[Dict[str, Any]]:
    """
    Flatten a fetch_reports_for_company() response into engine-shaped documents.

    That endpoint returns an envelope rather than the flat list the DIAL Global
    tables give back, and labels the type on `report_type`. Documents that
    classify as none of the eleven CITIES labels, or that have no URL, are
    dropped here.

    The envelope has FOUR buckets, not two. `policies` is where Sustainability
    Policy, Speak Up/Whistleblower and Supplier Code of Conduct live, and
    `others` holds the rows whose report_type was recorded as "Others" — several
    of which classify perfectly well off their title. Reading only `reports` and
    `filings` silently discarded both.
    """
    if not payload:
        return []

    if isinstance(payload, dict):
        raw = [*(payload.get("reports") or []),
               *(payload.get("filings") or []),
               *(payload.get("policies") or []),
               *(payload.get("others") or [])]
    else:
        raw = list(payload)

    documents = []
    for doc in raw:
        url = doc.get("document_url")
        if not url:
            continue
        doc_type = classify_document(doc)
        if not doc_type:
            continue
        documents.append({
            "report_id": doc.get("report_id"),
            "year": doc.get("year"),
            "document_type": doc_type,
            "document_url": url,
            # The label as stored, kept alongside the canonical one so an
            # audit can see what a classification was actually made from.
            "report_type": doc.get("report_type"),
            "document_title": doc.get("document_title"),
            # Which canonical entity this document was filed under. A company
            # is read as a group of same-named entities, so one company-year
            # can be assembled from two of them — without this the stored
            # source_documents cannot say which.
            "source_company_id": doc.get("source_company_id"),
        })
    return documents


def _select_by_year(
    payload: Any,
    chooser: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    years: int = 2,
    year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Normalise, group by year, apply `chooser` per year, keep the most recent
    `years` company-years that yielded anything.

    Shared by both selection rules so the year arithmetic — which years count
    against the budget, and which are skipped for holding nothing usable — can
    never differ between the two pipelines.
    """
    documents = normalise_ren_documents(payload)

    by_year: Dict[int, List[Dict[str, Any]]] = {}
    for doc in documents:
        if doc.get("year") is None:
            continue
        try:
            doc_year = int(doc["year"])
        except (TypeError, ValueError):
            continue
        if year is not None and doc_year != int(year):
            continue
        if not (REN_MIN_REPORTING_YEAR <= doc_year <= REN_MAX_REPORTING_YEAR):
            # This applies to an explicit `year` request too, which is
            # deliberate: asking for year=2093 by name does not make 2093 a
            # reporting year, and the pair should surface as "no documents"
            # rather than quietly extracting whatever carried the bad date.
            logger.warning(
                "[ren] Ignoring report_id=%s dated %s — outside %s-%s, so it is "
                "a bad year rather than a reporting year",
                doc.get("report_id"), doc_year,
                REN_MIN_REPORTING_YEAR, REN_MAX_REPORTING_YEAR)
            continue
        by_year.setdefault(doc_year, []).append(doc)

    selected: List[Dict[str, Any]] = []
    kept_years = 0
    for doc_year in sorted(by_year, reverse=True):
        if year is None and kept_years >= years:
            break
        chosen = chooser(by_year[doc_year])
        if not chosen:
            # Nothing usable, so the year does not count against the `years`
            # budget either.
            logger.info(
                "[ren] Skipping year=%s: nothing usable among %s",
                doc_year,
                sorted({d.get("document_type") for d in by_year[doc_year]}))
            continue
        selected.extend(chosen)
        kept_years += 1

    return selected


def select_ren_documents(
    payload: Any,
    years: int = 2,
    year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    The ORIGINAL ReN selection: four types, at most one document per type.

    Args:
        payload: raw fetch_reports_for_company() response.
        years: how many reporting years to keep, most recent first.
        year: restrict to a single reporting year, ignoring `years`.

    Returns a flat document list ready for the engine, which regroups by year.
    """
    return _select_by_year(payload, select_documents_for_year, years, year)


def select_all_ren_documents(
    payload: Any,
    years: int = 2,
    year: Optional[int] = None,
    max_per_type: int = None,
    max_per_year: int = None,
) -> List[Dict[str, Any]]:
    """
    The PROGRESSIVE ReN selection: eleven types, every document, round-ordered.

    Same arguments as select_ren_documents(), plus the two caps described on
    select_all_documents_for_year(). Returns a flat document list; the engine
    regroups by year and preserves this order within each year.
    """
    def chooser(year_docs):
        return select_all_documents_for_year(year_docs, max_per_type, max_per_year)

    return _select_by_year(payload, chooser, years, year)
