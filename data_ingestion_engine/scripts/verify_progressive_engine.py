#!/usr/bin/env python
"""
Offline checks for the progressive ReN pipeline. No network, no model calls.

Covers the three things that are easy to get wrong and expensive to discover
during a 797-company-year run:

  1. CLASSIFICATION — _TYPE_PATTERNS is ordered narrow-to-broad and first match
     wins, so reordering it silently reclassifies documents. Every case here is
     a real stored value from the ingestion mastersheet or the handoff notes.

  2. SELECTION — that the progressive rule takes every document of every type in
     round order, respects both caps, and that the original four-type rule is
     byte-for-byte unchanged by the expansion.

  3. RESIDUAL MONOTONICITY — the property the whole design rests on: a document
     can only ever ADD an answer. A round returning all-null must leave the
     residual untouched; a round returning score=1 must remove exactly those
     ids; and no round may overwrite an answer an earlier round established.

Usage:
    python -m data_ingestion_engine.scripts.verify_progressive_engine
"""

import os
import sys
from collections import OrderedDict

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")

from data_ingestion_engine.schemas.dial_global_schemas import (  # noqa: E402
    QUESTION_CATALOGUE,
    DialGlobalExtractionWire,
    OrganisationMetadataWire,
    QuestionAnswer,
    assemble_extraction,
    render_question_catalogue,
)
from data_ingestion_engine.utils.ren.document_selection import (  # noqa: E402
    ANNUAL_REPORT,
    CODE_OF_CONDUCT,
    CORPORATE_GOVERNANCE,
    DEI_REPORT,
    HUMAN_RIGHTS_POLICY,
    MODERN_SLAVERY_STATEMENT,
    PROXY_STATEMENT,
    REMUNERATION_REPORT,
    EMPLOYEE_INFORMATION_REPORT,
    ETHNICITY_PAY_GAP_REPORT,
    GRI_INDEX,
    PAY_GAP_REPORT,
    REN_ROUND_ORDER,
    SPEAK_UP,
    SUPPLIER_CODE_OF_CONDUCT,
    SUSTAINABILITY_POLICY,
    SUSTAINABILITY_REPORT,
    TCFD_INDEX,
    canonical_report_type,
    classify_document,
    normalise_ren_documents,
    select_all_documents_for_year,
    select_documents_for_year,
)

FAILURES = []
CHECKS = [0]


def check(label, actual, expected):
    CHECKS[0] += 1
    if actual != expected:
        FAILURES.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------
#
# Every left-hand value below is a string that actually appears in the data.
# "Ethinicity" is the source data's typo, not one here.

CLASSIFICATION_CASES = [
    # --- the four the old rule already handled ---
    ("Annual Report", ANNUAL_REPORT),
    ("10-K", ANNUAL_REPORT),
    ("20-F", ANNUAL_REPORT),
    ("Sustainability Report", SUSTAINABILITY_REPORT),
    ("DEI Report", DEI_REPORT),
    ("Gender Pay Gap Report", PAY_GAP_REPORT),
    ("Combined Pay Gap Report", PAY_GAP_REPORT),

    # --- the seven the expansion adds ---
    ("Employee Information Report", EMPLOYEE_INFORMATION_REPORT),
    ("EEO-1", EMPLOYEE_INFORMATION_REPORT),
    ("Ethinicity Pay Gap Report", ETHNICITY_PAY_GAP_REPORT),
    ("Ethnicity Pay Gap Report", ETHNICITY_PAY_GAP_REPORT),
    ("GRI Index", GRI_INDEX),
    ("TCFD Index", TCFD_INDEX),
    ("S&P Global TCFD Report", TCFD_INDEX),
    ("Speakup/Whistleblower Policy", SPEAK_UP),
    ("Supplier Code of Conduct", SUPPLIER_CODE_OF_CONDUCT),
    ("Sustainability Policy", SUSTAINABILITY_POLICY),
    ("Universal Registration Document", ANNUAL_REPORT),

    # --- ordering traps: the reason _TYPE_PATTERNS is ordered, not a dict ---
    # Would fall through to Sustainability Report if Policy came second.
    ("Sustainability Policy/framework", SUSTAINABILITY_POLICY),
    # A combined report naming ethnicity must not be swallowed by the broader
    # pay-gap pattern. Either label reads it; what matters is that it classifies.
    ("Gender Ethnicity Disability Pay Gap", ETHNICITY_PAY_GAP_REPORT),
    # Supplier code before the DEI pattern's "conduct"-adjacent language.
    ("Supplier Code of Conduct and Business Ethics", SUPPLIER_CODE_OF_CONDUCT),
    # "Employee Policy" must NOT read as an Employee Information Report.
    ("Employee Policy", None),

    # --- the six added 2026-08-26 from the classifier audit ---
    ("Proxy Statement", PROXY_STATEMENT),
    ("DEF 14A", PROXY_STATEMENT),
    ("Modern Slavery Statement", MODERN_SLAVERY_STATEMENT),
    ("Human Rights Policy", HUMAN_RIGHTS_POLICY),
    ("Remuneration Report", REMUNERATION_REPORT),
    ("Remuneration Policy", REMUNERATION_REPORT),
    ("Code of Conduct", CODE_OF_CONDUCT),
    ("Code of Ethics", CODE_OF_CONDUCT),
    ("Code of Business Conduct and Ethics", CODE_OF_CONDUCT),
    ("Code Of Business Principles And Code Policies", CODE_OF_CONDUCT),
    ("Corporate Governance Report", CORPORATE_GOVERNANCE),
    ("Corporate Governance Guidelines", CORPORATE_GOVERNANCE),
    ("Corporate Governance Statement", CORPORATE_GOVERNANCE),
    ("Audit Committee Charter", CORPORATE_GOVERNANCE),
    ("Compensation Committee Charter", CORPORATE_GOVERNANCE),
    # 114 of these were dropped for want of the optional word.
    ("Annual Financial Report", ANNUAL_REPORT),

    # --- collisions the ORDER exists to resolve ---
    # A supplier code is supply chain, a plain code of conduct is workforce.
    ("Supplier Code of Conduct", SUPPLIER_CODE_OF_CONDUCT),
    ("Code of Conduct for Suppliers", SUPPLIER_CODE_OF_CONDUCT),
    # Must not be swallowed by Sustainability Policy's policy/responsib lookaheads.
    ("Human Rights and Environmental Policy", HUMAN_RIGHTS_POLICY),
    # Pay gap is the more specific reading and is matched first.
    ("Gender Pay Gap Report", PAY_GAP_REPORT),
    # Voting paperwork carries no disclosure — must NOT become a round.
    ("Proxy Form", None),
    ("Form of Proxy", None),

    # --- still deliberately out of scope ---
    ("Health and Safety Policy", None),
    ("Articles of Association", None),
    ("Interim Results", None),
    ("Notification of Major Holdings", None),
    ("Transaction in Own Shares", None),
    ("Net Asset Value(s)", None),
    ("", None),
    (None, None),
]

for value, expected in CLASSIFICATION_CASES:
    check(f"canonical_report_type({value!r})", canonical_report_type(value), expected)

# The title fallback: a row typed "Others" whose title names the document.
check("classify_document falls back to document_title",
      classify_document({"report_type": "Others", "document_title": "GRI Index"}),
      GRI_INDEX)
check("classify_document prefers report_type over title",
      classify_document({"report_type": "Annual Report",
                         "document_title": "Sustainability Report"}),
      ANNUAL_REPORT)
check("classify_document returns None when nothing classifies",
      classify_document({"report_type": "Others", "document_title": "Board Minutes"}),
      None)


# ---------------------------------------------------------------------------
# 2. Selection
# ---------------------------------------------------------------------------

def doc(report_id, doc_type, year=2024):
    return {"report_id": report_id, "document_type": doc_type, "year": year,
            "document_url": f"gs://bucket/{report_id}.pdf"}


# Two annual reports plus one of several other types — the case the user asked
# about directly. Progressive must read BOTH annual reports.
YEAR_DOCS = [
    doc(1, ANNUAL_REPORT), doc(2, ANNUAL_REPORT),
    doc(3, SUSTAINABILITY_REPORT),
    doc(4, SPEAK_UP),
    doc(5, GRI_INDEX),
]

progressive = select_all_documents_for_year(YEAR_DOCS)
check("progressive reads both annual reports",
      [d["report_id"] for d in progressive if d["document_type"] == ANNUAL_REPORT],
      [2, 1])  # newest report_id first within a type
check("progressive emits REN_ROUND_ORDER sequence",
      [d["document_type"] for d in progressive],
      [ANNUAL_REPORT, ANNUAL_REPORT, SUSTAINABILITY_REPORT, GRI_INDEX, SPEAK_UP])
check("progressive reads every document", len(progressive), 5)

# The engine groups the selection into rounds by type, in arrival order. Both
# annual reports must land in ONE round, not two — that is the whole point of
# making the report type the unit of work rather than the document.
grouped = OrderedDict()
for d in progressive:
    grouped.setdefault(d["document_type"], []).append(d)
check("both annual reports share a single round",
      [r["report_id"] for r in grouped[ANNUAL_REPORT]], [2, 1])
check("five documents collapse to four rounds", len(grouped), 4)
check("round order follows REN_ROUND_ORDER",
      list(grouped), [ANNUAL_REPORT, SUSTAINABILITY_REPORT, GRI_INDEX, SPEAK_UP])
check("grouping is a partition — no document lost or duplicated",
      sorted(d["report_id"] for g in grouped.values() for d in g),
      sorted(d["report_id"] for d in progressive))

check("max_per_type=1 collapses to one document per type",
      [d["report_id"] for d in select_all_documents_for_year(YEAR_DOCS, max_per_type=1)],
      [2, 3, 5, 4])
check("max_per_year truncates the low-value tail, not an arbitrary slice",
      [d["document_type"] for d in select_all_documents_for_year(YEAR_DOCS, max_per_year=3)],
      [ANNUAL_REPORT, ANNUAL_REPORT, SUSTAINABILITY_REPORT])

# A year holding only a Speak Up policy has no anchor, so the ORIGINAL rule
# rejects it and the progressive rule accepts it. That difference is the point.
only_policy = [doc(9, SPEAK_UP)]
check("original rule rejects an anchorless year", select_documents_for_year(only_policy), [])
check("progressive rule accepts an anchorless year",
      [d["report_id"] for d in select_all_documents_for_year(only_policy)], [9])

# The original rule must be untouched by the expansion.
check("original rule still takes one document per type in REN_DOCUMENT_ORDER",
      [d["document_type"] for d in select_documents_for_year(YEAR_DOCS)],
      [ANNUAL_REPORT, SUSTAINABILITY_REPORT])

# All four envelope buckets are read.
envelope = {
    "reports": [{"report_id": 1, "report_type": "Annual Report", "year": 2024,
                 "document_url": "gs://b/1.pdf"}],
    "filings": [{"report_id": 2, "report_type": "10-K", "year": 2024,
                 "document_url": "gs://b/2.pdf"}],
    "policies": [{"report_id": 3, "report_type": "Sustainability Policy",
                  "year": 2024, "document_url": "gs://b/3.pdf"}],
    "others": [{"report_id": 4, "report_type": "Others",
                "document_title": "EEO-1", "year": 2024,
                "document_url": "gs://b/4.pdf"}],
}
check("normalise_ren_documents reads all four envelope buckets",
      sorted(d["report_id"] for d in normalise_ren_documents(envelope)),
      [1, 2, 3, 4])
check("a document with no URL is dropped",
      normalise_ren_documents({"reports": [{"report_id": 5,
                                            "report_type": "Annual Report"}]}),
      [])


# ---------------------------------------------------------------------------
# 3. Residual monotonicity
# ---------------------------------------------------------------------------
#
# Replays the merge exactly as _run_progressive_extraction_pass does, against
# stubbed model output. This is the property the design rests on: a later
# document can only add.

ALL_QUESTIONS = list(QUESTION_CATALOGUE)


def merge_round(residual, answers, provenance, wire_answers, report_id):
    """The merge step from _run_progressive_extraction_pass, verbatim in logic."""
    pending = set(residual)
    filled = []
    for answer in wire_answers:
        question_id = (answer.get("question_id") or "").strip()
        if question_id not in pending or question_id in answers:
            continue
        if answer.get("score") is None or int(answer["score"]) != 1:
            continue
        answers[question_id] = {"score": 1,
                                "justification": answer.get("justification")}
        provenance[question_id] = report_id
        filled.append(question_id)
    return [q for q in residual if q not in answers], filled


residual, answers, provenance = list(ALL_QUESTIONS), {}, {}

# Round 1: the annual report answers three questions.
round1_ids = ALL_QUESTIONS[:3]
residual, filled = merge_round(
    residual, answers, provenance,
    [{"question_id": q, "score": 1, "justification": f"annual: {q}"} for q in round1_ids]
    + [{"question_id": q, "score": None} for q in ALL_QUESTIONS[3:10]],
    report_id=101)
check("round 1 fills exactly the positives", filled, round1_ids)
check("round 1 removes exactly those from the residual",
      len(residual), len(ALL_QUESTIONS) - 3)
check("a null does not settle a question", ALL_QUESTIONS[3] in residual, True)

# Round 2: the sustainability report answers two NEW questions, and also claims
# the three the annual report already answered. The claims must be ignored.
round2_ids = ALL_QUESTIONS[10:12]
residual, filled = merge_round(
    residual, answers, provenance,
    [{"question_id": q, "score": 1, "justification": f"sustainability: {q}"}
     for q in round1_ids + round2_ids],
    report_id=202)
check("round 2 fills only the questions still open", filled, round2_ids)
check("round 2 CANNOT delete what round 1 found",
      [answers[q]["justification"] for q in round1_ids],
      [f"annual: {q}" for q in round1_ids])
check("round 1 keeps provenance for its own answers",
      [provenance[q] for q in round1_ids], [101, 101, 101])
check("round 2 owns provenance for its own answers",
      [provenance[q] for q in round2_ids], [202, 202])

# Round 3: an all-null round leaves everything exactly as it was.
before = (list(residual), dict(answers))
residual, filled = merge_round(
    residual, answers, provenance,
    [{"question_id": q, "score": None} for q in residual[:20]],
    report_id=303)
check("an all-null round fills nothing", filled, [])
check("an all-null round leaves the residual unchanged", residual, before[0])
check("an all-null round leaves the answers unchanged", answers, before[1])

# A round that emits the same question twice must count it once. `pending` is a
# snapshot taken before the merge loop, so without the `in answers` guard a model
# repeating itself inflates questions_filled and the round's trace claims more
# than it moved — the counts then stop reconciling with questions_answered.
dup_residual, dup_answers, dup_provenance = list(ALL_QUESTIONS), {}, {}
dup_id = ALL_QUESTIONS[0]
dup_residual, filled = merge_round(
    dup_residual, dup_answers, dup_provenance,
    [{"question_id": dup_id, "score": 1, "justification": "first"},
     {"question_id": dup_id, "score": 1, "justification": "second"}],
    report_id=505)
check("a repeated question_id is counted once", filled, [dup_id])
check("the FIRST emission of a repeated question wins",
      dup_answers[dup_id]["justification"], "first")
check("a repeated question leaves the residual consistent",
      len(dup_residual), len(ALL_QUESTIONS) - 1)

# An invented question_id is dropped rather than stored.
residual, filled = merge_round(
    residual, answers, provenance,
    [{"question_id": "q99_invented_question", "score": 1, "justification": "x"}],
    report_id=404)
check("an invented question_id is dropped", filled, [])
check("an invented question_id never reaches the answers",
      "q99_invented_question" in answers, False)

# The assembled extraction still carries all 64 questions.
wire = DialGlobalExtractionWire(
    organisation=OrganisationMetadataWire.model_validate({}),
    answers=[QuestionAnswer(question_id=q, score=v["score"],
                            justification=v["justification"])
             for q, v in answers.items()],
)
extraction = assemble_extraction(wire)
answered = sum(
    1 for section in extraction.model_dump().values() if isinstance(section, dict)
    for value in section.values()
    if isinstance(value, dict) and value.get("score") == 1)
check("assembled extraction carries every answer", answered, len(answers))

flat = {q: getattr(getattr(extraction, s), q)
        for q, (s, _) in QUESTION_CATALOGUE.items()}
check("assembled extraction carries all 64 questions", len(flat), 64)
check("unanswered questions assemble as null scores",
      flat[ALL_QUESTIONS[3]].score, None)
check("the Null Rule holds after assembly",
      flat[ALL_QUESTIONS[3]].justification, None)

# The residual catalogue actually shrinks, which is what makes later rounds
# cheaper as well as safer.
full = render_question_catalogue()
partial = render_question_catalogue(only=round1_ids)
check("a filtered catalogue is smaller than the full one", len(partial) < len(full), True)
check("a filtered catalogue holds only the requested questions",
      sorted(line.split(" :: ")[0] for line in partial.splitlines()
             if " :: " in line),
      sorted(round1_ids))
check("an empty filter renders nothing", render_question_catalogue(only=[]), "")
check("REN_ROUND_ORDER covers seventeen types", len(REN_ROUND_ORDER), 17)
check("REN_ROUND_ORDER has no duplicates",
      len(set(REN_ROUND_ORDER)), len(REN_ROUND_ORDER))
# Every label a pattern can emit must have a round slot, or its documents
# classify and then vanish between selection and the engine.
from data_ingestion_engine.utils.ren.document_selection import _TYPE_PATTERNS
check("every pattern label has a place in REN_ROUND_ORDER",
      sorted({label for label, _ in _TYPE_PATTERNS} - set(REN_ROUND_ORDER)), [])


# ---------------------------------------------------------------------------

print(f"\n{CHECKS[0]} checks run")
if FAILURES:
    print(f"\n{len(FAILURES)} FAILED:\n")
    for failure in FAILURES:
        print(f"  ✗ {failure}\n")
    sys.exit(1)
print("all passed")
