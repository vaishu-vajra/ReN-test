# data_ingestion_engine/schemas/dial_global_schemas.py
#
# Schemas for the DIAL Global two-pass engine (DIAL-2).
#
# Pass 1 (DialGlobalExtractionSchema) — extracts Q1-Q79 from the source
# document: organisation metadata + baseline metrics (Q1-Q15) and 64 binary
# evaluation items (Q16-Q79), each scored 1 or null with a unified
# justification string.
#
# Pass 2 (DialGlobalScoringSchema) — scores the pass-1 output against the
# CITIES rubric: a 1-5 level per category, weighted contributions, a weighted
# total, and uncertainty flags for human review. Pass 2 reads the pass-1 JSON
# only; it does not need the source PDF.
#
# Deliberately separate from schemas/section_schemas.py: these are registered in
# DIAL_GLOBAL_SCHEMA_MAP and are never reachable through SCHEMA_MAP or
# store_data()'s section dispatcher.

import logging
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums for the organisation profile (Q4-Q7)
# ---------------------------------------------------------------------------


class Region(str, Enum):
    """Q4 primary region of operation."""
    NAMER = "NAMER"
    WEUR = "WEUR"
    CEER = "CEER"
    LATAM = "LATAM"
    APAC = "APAC"
    MEAF = "MEAF"


class OwnershipType(str, Enum):
    """Q5 ownership type."""
    PRIVATE = "Private"
    PUBLIC = "Public"


class Sector(str, Enum):
    """Q6 GICS sector."""
    ENERGY = "Energy"
    MATERIALS = "Materials"
    INDUSTRIALS = "Industrials"
    CONSUMER_DISCRETIONARY = "Consumer Discretionary"
    CONSUMER_STAPLES = "Consumer Staples"
    HEALTH_CARE = "Health Care"
    FINANCIALS = "Financials"
    INFORMATION_TECHNOLOGY = "Information Technology"
    COMMUNICATION_SERVICES = "Communication Services"
    UTILITIES = "Utilities"
    REAL_ESTATE = "Real Estate"


# Q7 GICS industry. Kept as a plain list and turned into an Enum functionally —
# 74 hand-written members would be an easy place to typo a value, and the value
# strings are what the model is actually constrained to.
INDUSTRY_VALUES = [
    "Energy Equipment & Services",
    "Oil, Gas & Consumable Fuels",
    "Chemicals",
    "Construction Materials",
    "Containers & Packaging",
    "Metals & Mining",
    "Paper & Forest Products",
    "Aerospace & Defense",
    "Building Products",
    "Construction & Engineering",
    "Electrical Equipment",
    "Industrial Conglomerates",
    "Machinery",
    "Trading Companies & Distributors",
    "Commercial Services & Supplies",
    "Professional Services",
    "Air Freight & Logistics",
    "Passenger Airlines",
    "Marine Transportation",
    "Ground Transportation",
    "Transportation Infrastructure",
    "Automobile Components",
    "Automobiles",
    "Household Durables",
    "Leisure Products",
    "Textiles, Apparel & Luxury Goods",
    "Hotels, Restaurants & Leisure",
    "Diversified Consumer Services",
    "Distributors",
    "Broadline Retail",
    "Specialty Retail",
    "Consumer Staples Distribution & Retail",
    "Beverages",
    "Food Products",
    "Tobacco",
    "Household Products",
    "Personal Care Products",
    "Health Care Equipment & Supplies",
    "Health Care Providers & Services",
    "Health Care Technology",
    "Biotechnology",
    "Pharmaceuticals",
    "Life Sciences Tools & Services",
    "Banks",
    "Financial Services",
    "Consumer Finance",
    "Capital Markets",
    "Mortgage Real Estate Investment Trusts (REITs)",
    "Insurance",
    "IT Services",
    "Software",
    "Communications Equipment",
    "Technology Hardware, Storage & Peripherals",
    "Electronic Equipment, Instruments & Components",
    "Semiconductors & Semiconductor Equipment",
    "Diversified Telecommunication Services",
    "Wireless Telecommunication Services",
    "Media",
    "Entertainment",
    "Interactive Media & Services",
    "Electric Utilities",
    "Gas Utilities",
    "Multi-Utilities",
    "Water Utilities",
    "Independent Power and Renewable Electricity Producers",
    "Diversified REITs",
    "Industrial REITs",
    "Hotel & Resort REITs",
    "Office REITs",
    "Health Care REITs",
    "Residential REITs",
    "Retail REITs",
    "Specialized REITs",
    "Real Estate Management & Development",
]


def _enum_member_name(value: str) -> str:
    """Turn a GICS industry label into a valid Python enum member name."""
    name = "".join(ch if ch.isalnum() else "_" for ch in value).upper()
    while "__" in name:
        name = name.replace("__", "_")
    return name.strip("_")


Industry = Enum(
    "Industry",
    {_enum_member_name(v): v for v in INDUSTRY_VALUES},
    type=str,
)
Industry.__doc__ = "Q7 GICS industry."


# ---------------------------------------------------------------------------
# The binary evaluation item used by Q16-Q79
# ---------------------------------------------------------------------------


class EvaluationItem(BaseModel):
    """
    One binary disclosure question.

    score is 1 only when the document explicitly and unequivocally meets the
    question's criteria, and null for "No", missing, ambiguous, or
    partially-met. justification carries the synthesis, the verbatim quote(s)
    and the location citation(s) as a single unified string.
    """
    score: Optional[int] = Field(
        None,
        description="1 if the answer is Yes and the criteria are fully met; null if No, missing, or ambiguous.",
    )
    justification: Optional[str] = Field(
        None,
        description=(
            "Single unified string combining the factual synthesis, exact verbatim quote(s) in "
            "quotation marks, and the location citation(s) in parentheses — page numbers such as "
            "(p. 14) or (pp. 22-24), falling back to the exact section or table heading such as "
            "(Section: Corporate Governance > Board Diversity). Must be null when score is null."
        ),
    )

    @field_validator("score", mode="before")
    @classmethod
    def _only_one_or_null(cls, v):
        """The schema admits 1 and null only — anything else (0, 2, "yes") is null."""
        try:
            return 1 if v is not None and int(v) == 1 else None
        except (TypeError, ValueError):
            return None

    @model_validator(mode="after")
    def _null_score_means_null_justification(self):
        """Enforce the Null Rule: no score, no justification."""
        if self.score is None and self.justification is not None:
            self.justification = None
        return self


# ---------------------------------------------------------------------------
# Pass 1 — extraction (Q1-Q79)
# ---------------------------------------------------------------------------


class OrganisationSection(BaseModel):
    """Q1-Q17: organisation profile and baseline metrics."""
    q01_organisation_name: Optional[str] = Field(
        None, description="Q1: Organisation name.")
    q02_headquarters_city: Optional[str] = Field(
        None, description="Q2: Headquarters city.")
    q02_year: Optional[int] = Field(
        None,
        description=(
            "Reporting year. Do NOT populate this — it is inserted programmatically from the "
            "curated ingestion spreadsheet after extraction. Leave it null."
        ),
    )
    q03_primary_country_of_operation: Optional[str] = Field(
        None, description="Q3: Primary country of operation.")
    q04_primary_region_of_operation: Optional[Region] = Field(
        None, description="Q4: Primary region of operation.")
    q05_ownership_type: Optional[OwnershipType] = Field(
        None, description="Q5: Ownership — Private or Public.")
    q06_sector: Optional[Sector] = Field(None, description="Q6: Sector.")
    q07_industry: Optional[Industry] = Field(
        None, description="Q7: Industry.")
    q08_board_pct_women: Optional[str] = Field(
        None, description="Q8: Board % women, as reported (e.g. '40%').")
    q09_sr_leadership_pct_women: Optional[str] = Field(
        None, description="Q9: Senior leadership % women, as reported (e.g. '35.5%').")
    q10_board_pct_ethnic_minority: Optional[str] = Field(
        None, description="Q10: Board % ethnic minority, as reported (e.g. '20%').")
    q11_sr_leadership_pct_ethnic_minority: Optional[str] = Field(
        None, description="Q11: Senior leadership % ethnic minority, as reported (e.g. '18%').")
    q12_total_number_of_employees: Optional[int] = Field(
        None, description="Q12: Total number of employees.")
    q13_reporting_currency: Optional[str] = Field(
        None, description="Q13: Reporting currency code (e.g. USD, EUR, GBP).")
    q14_total_revenue: Optional[float] = Field(
        None, description="Q14: Total revenue.")
    q15_total_assets: Optional[float] = Field(
        None, description="Q15: Total assets.")
    q16_standalone_esg_report_publicly_available: EvaluationItem = Field(
        ..., description="Q16: Is a standalone ESG, Sustainability, or Talent report publicly available?")
    q17_esg_reporting_regulatory_requirement: EvaluationItem = Field(
        ..., description="Q17: Does the company operate in an industry or geography where ESG reporting is a regulatory requirement?")


class CultureSection(BaseModel):
    """Q18-Q28: culture and workplace environment."""
    q18_psychological_safety_statement: EvaluationItem = Field(
        ..., description="Q18: Is there a publicly available statement referencing psychological safety or respectful workplace commitments?")
    q19_discrimination_incidents_disclosed: EvaluationItem = Field(
        ..., description="Q19: Does the organisation disclose the total number of discrimination incidents and actions taken?")
    q20_employee_voice_mechanisms_disclosed: EvaluationItem = Field(
        ..., description="Q20: Does the organisation disclose employee voice mechanisms (e.g. surveys, whistleblowing channels)?")
    q21_employee_engagement_belonging_metrics_reported: EvaluationItem = Field(
        ..., description="Q21: Are employee engagement or belonging metrics publicly reported?")
    q22_organisational_values_defined: EvaluationItem = Field(
        ..., description="Q22: Are organisational values publicly defined and accessible?")
    q23_inclusive_leadership_behaviours_defined: EvaluationItem = Field(
        ..., description="Q23: Are inclusive leadership behaviours formally defined in governance or leadership frameworks?")
    q24_executive_sponsorship_ergs: EvaluationItem = Field(
        ..., description="Q24: Is there documented executive sponsorship of employee resource groups (ERGs)?")
    q25_culture_metrics_reviewed_board_level: EvaluationItem = Field(
        ..., description="Q25: Are culture or people metrics reviewed at board or governance level?")
    q26_employment_legal_cases_disclosed: EvaluationItem = Field(
        ..., description="Q26: Does the organisation disclose the number of employment-related legal cases, disputes, or tribunal claims during the reporting period?")
    q27_grievance_whistleblowing_outcomes_reported: EvaluationItem = Field(
        ..., description="Q27: Does the organisation publicly report employee grievance, whistleblowing, or misconduct case volumes and outcomes?")
    q28_sick_leave_absence_metrics_disclosed: EvaluationItem = Field(
        ..., description="Q28: Does the organisation publicly disclose workforce absence or sick leave metrics (e.g. mental health or stress-related absence)?")


class InclusionSection(BaseModel):
    """Q29-Q39: inclusion strategy and demographic composition."""
    q29_inclusion_diversity_strategy_available: EvaluationItem = Field(
        ..., description="Q29: Is there a publicly available inclusion or diversity strategy?")
    q30_workforce_composition_gender_disclosed: EvaluationItem = Field(
        ..., description="Q30: Does the organisation disclose workforce composition by gender?")
    q31_workforce_composition_age_disclosed: EvaluationItem = Field(
        ..., description="Q31: Does the organisation disclose workforce composition by age category?")
    q32_other_demographic_categories_disclosed: EvaluationItem = Field(
        ..., description="Q32: Are other demographic categories (e.g. ethnicity, disability) publicly disclosed?")
    q33_inclusion_targets_with_timeframes_disclosed: EvaluationItem = Field(
        ..., description="Q33: Are inclusion targets publicly disclosed with timeframes?")
    q34_inclusive_leadership_training_referenced: EvaluationItem = Field(
        ..., description="Q34: Is inclusive leadership training publicly referenced?")
    q35_accessibility_commitments_documented: EvaluationItem = Field(
        ..., description="Q35: Are accessibility commitments (digital and/or physical) documented?")
    q36_inclusion_performance_linked_executive_accountability: EvaluationItem = Field(
        ..., description="Q36: Is inclusion performance linked to executive accountability?")
    q37_gender_composition_board_disclosed: EvaluationItem = Field(
        ..., description="Q37: Does the organisation disclose the gender composition of its board of directors?")
    q38_gender_composition_executive_csuite_disclosed: EvaluationItem = Field(
        ..., description="Q38: Does the organisation disclose the gender composition of its executive or C-suite leadership team?")
    q39_ethnic_racial_diversity_leadership_workforce_disclosed: EvaluationItem = Field(
        ..., description="Q39: Does the organisation disclose ethnic or racial diversity data for leadership or workforce populations (where legally permitted)?")


class TalentSection(BaseModel):
    """Q40-Q50: talent acquisition, development and retention."""
    q40_inclusive_recruitment_policies_documented: EvaluationItem = Field(
        ..., description="Q40: Are inclusive recruitment policies publicly documented?")
    q41_structured_bias_mitigation_hiring_processes: EvaluationItem = Field(
        ..., description="Q41: Is there evidence of structured or bias-mitigation hiring processes?")
    q42_demographic_hiring_statistics_reported: EvaluationItem = Field(
        ..., description="Q42: Are demographic hiring statistics publicly reported?")
    q43_new_hire_turnover_rates_disclosed: EvaluationItem = Field(
        ..., description="Q43: Does the organisation disclose new hire and turnover rates?")
    q44_development_mentoring_programmes_documented: EvaluationItem = Field(
        ..., description="Q44: Are development or mentoring programmes publicly documented?")
    q45_average_training_hours_disclosed: EvaluationItem = Field(
        ..., description="Q45: Are average training hours per employee disclosed?")
    q46_promotion_rates_by_demographic_reported: EvaluationItem = Field(
        ..., description="Q46: Are promotion rates reported by demographic category?")
    q47_retention_data_by_demographic_reported: EvaluationItem = Field(
        ..., description="Q47: Is retention data reported by demographic category?")
    q48_talent_data_reviewed_executive_board_level: EvaluationItem = Field(
        ..., description="Q48: Is talent data reviewed at executive or board level?")
    q49_turnover_attrition_by_demographic_disclosed: EvaluationItem = Field(
        ..., description="Q49: Does the organisation disclose employee turnover or attrition rates by demographic group?")
    q50_employee_wellbeing_burnout_metrics_reported: EvaluationItem = Field(
        ..., description="Q50: Does the organisation report initiatives or metrics relating to employee wellbeing, burnout, or workload risk?")


class ImpactSection(BaseModel):
    """Q51-Q60: impact tracking, KPIs and assurance."""
    q51_inclusion_related_kpis_disclosed: EvaluationItem = Field(
        ..., description="Q51: Are inclusion-related KPIs publicly disclosed?")
    q52_measurable_outcomes_reported: EvaluationItem = Field(
        ..., description="Q52: Are measurable outcomes reported (beyond policy statements)?")
    q53_improvements_tracked_yoy: EvaluationItem = Field(
        ..., description="Q53: Are improvements tracked year-on-year with comparative data?")
    q54_impact_benchmarking_external_frameworks: EvaluationItem = Field(
        ..., description="Q54: Is impact benchmarking against external frameworks referenced?")
    q55_impact_metrics_independently_verified: EvaluationItem = Field(
        ..., description="Q55: Are impact metrics independently verified or assured?")
    q56_supplier_social_screening_percentages: EvaluationItem = Field(
        ..., description="Q56: Does the organisation disclose supplier social screening percentages?")
    q57_negative_social_impacts_supply_chain: EvaluationItem = Field(
        ..., description="Q57: Does the organisation disclose negative social impacts in the supply chain and corrective actions?")
    q58_community_impact_outcomes_quantified: EvaluationItem = Field(
        ..., description="Q58: Are community impact outcomes quantified?")
    q59_social_inclusion_integrated_esg_reporting: EvaluationItem = Field(
        ..., description="Q59: Is social or inclusion impact integrated into ESG reporting?")
    q60_material_social_workforce_risks_disclosed: EvaluationItem = Field(
        ..., description="Q60: Does the organisation publicly disclose material social or workforce-related risks within its ESG or sustainability reporting?")


class EquitySection(BaseModel):
    """Q61-Q67: pay equity, structural bias and remuneration."""
    q61_gender_pay_gap_remuneration_ratio_disclosed: EvaluationItem = Field(
        ..., description="Q61: Does the organisation disclose gender pay gap or remuneration ratio data?")
    q62_formal_pay_equity_analysis_referenced: EvaluationItem = Field(
        ..., description="Q62: Is a formal pay equity analysis publicly referenced?")
    q63_promotion_equity_structural_bias_audits_disclosed: EvaluationItem = Field(
        ..., description="Q63: Are promotion equity audits or structural bias audits publicly disclosed?")
    q64_reporting_inequities_and_actions_taken: EvaluationItem = Field(
        ..., description="Q64: Is there public reporting on inequities identified and actions taken?")
    q65_equity_metrics_tracked_multi_year: EvaluationItem = Field(
        ..., description="Q65: Are equity metrics tracked across multiple reporting years?")
    q66_accountability_mechanisms_equity_defined: EvaluationItem = Field(
        ..., description="Q66: Are accountability mechanisms for equity outcomes publicly defined?")
    q67_policy_reform_corrective_action_equity: EvaluationItem = Field(
        ..., description="Q67: Is there evidence of policy reform or corrective action following equity analysis?")


class SocialSection(BaseModel):
    """Q68-Q79: social impact, community engagement and governance."""
    q68_community_engagement_social_investment_strategy: EvaluationItem = Field(
        ..., description="Q68: Is there a documented community engagement or social investment strategy?")
    q69_diverse_supplier_metrics_disclosed: EvaluationItem = Field(
        ..., description="Q69: Are diverse supplier metrics publicly disclosed?")
    q70_social_impact_governance_defined: EvaluationItem = Field(
        ..., description="Q70: Is social impact governance defined at executive or board level?")
    q71_board_oversight_social_impact_referenced: EvaluationItem = Field(
        ..., description="Q71: Is board-level oversight of social impact explicitly referenced?")
    q72_societal_outcome_metrics_quantified: EvaluationItem = Field(
        ..., description="Q72: Are societal outcome metrics quantified?")
    q73_external_stakeholder_engagement_transparency: EvaluationItem = Field(
        ..., description="Q73: Is transparency in external stakeholder engagement publicly described?")
    q74_political_contributions_disclosed: EvaluationItem = Field(
        ..., description="Q74: Does the organisation disclose political contributions?")
    q75_product_marketing_non_compliance_incidents: EvaluationItem = Field(
        ..., description="Q75: Does the organisation disclose product or marketing non-compliance incidents?")
    q76_social_workforce_risk_oversight_board_committee: EvaluationItem = Field(
        ..., description="Q76: Is responsibility for social or workforce-related risk oversight assigned to a board committee or senior leadership function?")
    q77_reputational_ethics_trust_incidents_disclosed: EvaluationItem = Field(
        ..., description="Q77: Does the organisation publicly disclose material reputational, ethics, or trust-related incidents and how they were addressed?")
    q78_reputational_public_trust_risk_management_referenced: EvaluationItem = Field(
        ..., description="Q78: Is reputational or public-trust risk management explicitly referenced or disclosed?")
    q79_approach_managing_reputational_crises_described: EvaluationItem = Field(
        ..., description="Q79: Does the organisation publicly describe its approach to managing reputational or public trust crises?")


class DialGlobalExtractionSchema(BaseModel):
    """
    Pass-1 output: the full Q1-Q79 extraction for one company-year.

    One instance covers ALL of a company's documents for a single reporting
    year, not one document — a gap in the annual report is often filled by the
    sustainability report, so both are read together in a single call.
    """
    organisation: OrganisationSection
    culture: CultureSection
    inclusion: InclusionSection
    talent: TalentSection
    impact: ImpactSection
    equity: EquitySection
    social: SocialSection


# ---------------------------------------------------------------------------
# Pass-1 WIRE format
# ---------------------------------------------------------------------------
#
# DialGlobalExtractionSchema above is the CONTRACT — the shape that gets stored
# and that the scoring prompt reads. It is not what goes on the wire.
#
# Gemini's structured output compiles response_schema into a constrained-decoding
# automaton, and 64 separately-nested EvaluationItem objects overflow it:
#
#   400 INVALID_ARGUMENT: The specified schema produces a constraint that has
#   too many states for serving
#
# That is a structural limit, not a size one — stripping every description and
# enum still failed at 28KB. Asking instead for ONE array of answers defines the
# item shape once instead of 64 times: 4.8KB, accepted. assemble_extraction()
# folds the array back into the contract shape, so nothing downstream changes.


class OrganisationMetadataWire(BaseModel):
    """Q1-Q15 only. The Q16/Q17 evaluation items travel in `answers`."""
    q01_organisation_name: Optional[str] = Field(None, description="Organisation name.")
    q02_headquarters_city: Optional[str] = Field(None, description="Headquarters city.")
    q03_primary_country_of_operation: Optional[str] = Field(None, description="Primary country of operation.")
    q04_primary_region_of_operation: Optional[Region] = Field(None, description="Primary region of operation.")
    q05_ownership_type: Optional[OwnershipType] = Field(None, description="Private or Public.")
    q06_sector: Optional[Sector] = Field(None, description="Sector.")
    q07_industry: Optional[Industry] = Field(None, description="Industry.")
    q08_board_pct_women: Optional[str] = Field(None, description="Board % women, as reported.")
    q09_sr_leadership_pct_women: Optional[str] = Field(None, description="Senior leadership % women, as reported.")
    q10_board_pct_ethnic_minority: Optional[str] = Field(None, description="Board % ethnic minority, as reported.")
    q11_sr_leadership_pct_ethnic_minority: Optional[str] = Field(None, description="Senior leadership % ethnic minority, as reported.")
    q12_total_number_of_employees: Optional[int] = Field(None, description="Total number of employees.")
    q13_reporting_currency: Optional[str] = Field(None, description="Reporting currency code.")
    q14_total_revenue: Optional[float] = Field(None, description="Total revenue.")
    q15_total_assets: Optional[float] = Field(None, description="Total assets.")


class QuestionAnswer(BaseModel):
    """One answered question from the Q16-Q79 catalogue."""
    question_id: str = Field(
        ..., description="The exact question key from the catalogue, e.g. q18_psychological_safety_statement.")
    score: Optional[int] = Field(
        None, description="1 if the answer is Yes and the criteria are fully met; null otherwise.")
    justification: Optional[str] = Field(
        None,
        description=("Single unified string: factual synthesis, verbatim quote(s) in quotation marks, "
                     "and location citation(s) in parentheses. Null when score is null."))


class DialGlobalExtractionWire(BaseModel):
    """What pass 1 actually asks Gemini for."""
    organisation: OrganisationMetadataWire
    answers: List[QuestionAnswer] = Field(
        default_factory=list,
        description="One entry per question in the catalogue. Answer every question.")


def _build_question_catalogue() -> Dict[str, tuple]:
    """
    question_id -> (section_name, question_text), in schema order.

    Derived from the contract models so the catalogue in the prompt and the
    schema can never disagree — there is one source of truth for the questions.
    """
    catalogue = {}
    for section_name, section_field in DialGlobalExtractionSchema.model_fields.items():
        section_model = section_field.annotation
        for field_name, field in section_model.model_fields.items():
            if field.annotation is EvaluationItem:
                catalogue[field_name] = (section_name, field.description or field_name)
    return catalogue


QUESTION_CATALOGUE = _build_question_catalogue()


def render_question_catalogue(only: Optional[Iterable[str]] = None) -> str:
    """
    The catalogue as prompt text, grouped by section.

    `only` restricts it to the given question ids, keeping schema order and
    dropping any section left empty. That is what lets the progressive engine
    hand each round just the questions still unanswered — a question already
    settled by an earlier document never appears again, so the model cannot
    re-answer it and the prompt shrinks as the rounds go on.

    Unknown ids in `only` are ignored; None means the whole catalogue.
    """
    wanted = None if only is None else set(only)
    lines = []
    current = None
    for question_id, (section_name, text) in QUESTION_CATALOGUE.items():
        if wanted is not None and question_id not in wanted:
            continue
        if section_name != current:
            current = section_name
            lines.append(f"\n[{section_name.upper()}]")
        lines.append(f"{question_id} :: {text}")
    return "\n".join(lines).strip()


def assemble_extraction(wire: DialGlobalExtractionWire) -> DialGlobalExtractionSchema:
    """
    Fold the wire format back into the contract shape.

    Answers are matched to the catalogue by question_id. Anything the model
    invented is dropped; anything it skipped becomes a null score, so the result
    always carries all 64 questions. EvaluationItem's validators then apply as
    normal, including the Null Rule.
    """
    answered = {}
    unknown = []
    for answer in wire.answers:
        key = (answer.question_id or "").strip()
        if key in QUESTION_CATALOGUE:
            # Last one wins if the model repeats a question.
            answered[key] = {"score": answer.score,
                             "justification": answer.justification}
        elif key:
            unknown.append(key)

    if unknown:
        logger.warning(
            "[dial_global] Dropped %s answer(s) with unrecognised question_id: %s",
            len(unknown), ", ".join(sorted(set(unknown))[:10]))

    missing = [q for q in QUESTION_CATALOGUE if q not in answered]
    if missing:
        logger.info(
            "[dial_global] %s of %s questions went unanswered; recorded as null",
            len(missing), len(QUESTION_CATALOGUE))

    sections: Dict[str, Dict[str, Any]] = {}
    for question_id, (section_name, _text) in QUESTION_CATALOGUE.items():
        sections.setdefault(section_name, {})[question_id] = answered.get(
            question_id, {"score": None, "justification": None})

    payload: Dict[str, Any] = {name: dict(fields)
                               for name, fields in sections.items()}
    payload.setdefault("organisation", {}).update(
        wire.organisation.model_dump(mode="json"))

    return DialGlobalExtractionSchema.model_validate(payload)


def stamp_reporting_year(
    extraction: DialGlobalExtractionSchema,
    reporting_year: int,
) -> DialGlobalExtractionSchema:
    """
    Insert the curated reporting year into the extraction JSON as q02_year.

    The year is never the model's to decide — it comes from the "Year" column of
    the hand-curated ingestion spreadsheet, carried through to
    dial_global_reports.year. Whatever the model put in q02_year is overwritten.

    Mutates and returns the same instance.
    """
    extraction.organisation.q02_year = int(reporting_year)
    return extraction


# ---------------------------------------------------------------------------
# Pass 2 — CITIES scoring over the pass-1 output
# ---------------------------------------------------------------------------

# Category -> weight percent. The single source of truth for the weighting,
# used both in the prompt and to recompute the arithmetic after the model runs.
CATEGORY_WEIGHTS = {
    "culture": 12.0,
    "inclusion": 22.0,
    "talent": 18.0,
    "impact": 20.0,
    "equity": 16.0,
    "social": 12.0,
}

MAX_CATEGORY_SCORE = 5


class CategoryScoreItem(BaseModel):
    """One CITIES category's rubric level, weighting and uncertainty flags."""
    score: int = Field(...,
                       description="Rubric level 1-5 from holistic analysis of the binary scores and their justifications.")
    category_weight_percent: float = Field(...,
                                           description="Predefined category weight percent (Culture 12, Inclusion 22, Talent 18, Impact 20, Equity 16, Social 12).")
    weighted_score: float = Field(...,
                                  description="(score / 5) * category_weight_percent.")
    score_rationale: str = Field(...,
                                 description="Why this level was chosen over the adjacent levels, citing specific question responses and extracted evidence.")
    uncertainty_flagged: bool = Field(...,
                                      description="Confidence in this score, not a measure of how much was disclosed. True only when evidence that IS present is unsupported by its quote, self-contradictory, inferred rather than stated, unverified where the level demands assurance, straddles two levels, or is ambiguous in scope or period. Absent evidence is a confident low score, never uncertainty.")
    problematic_questions: List[str] = Field(
        default_factory=list,
        description="Exact keys of questions scored 1 whose evidence caused the uncertainty, e.g. ['q19_discrimination_incidents_disclosed']. Never list a null-scored question — nothing was disclosed for it to be uncertain about. Empty when uncertainty_flagged is false.")

    @field_validator("score", mode="before")
    @classmethod
    def _clamp_score(cls, v):
        """The rubric only defines levels 1-5."""
        try:
            return max(1, min(MAX_CATEGORY_SCORE, int(v)))
        except (TypeError, ValueError):
            return 1

    @model_validator(mode="after")
    def _clear_flags_when_not_flagged(self):
        """No flag, no findings — keeps the record unambiguous for reviewers."""
        if not self.uncertainty_flagged:
            self.problematic_questions = []
        return self


class ScoringCategories(BaseModel):
    culture: CategoryScoreItem
    inclusion: CategoryScoreItem
    talent: CategoryScoreItem
    impact: CategoryScoreItem
    equity: CategoryScoreItem
    social: CategoryScoreItem


class OverallScore(BaseModel):
    """The aggregate score."""
    total_score_percentage: float = Field(...,
                                          description="Sum of the six weighted_score values, out of 100.")


class DialGlobalScoringSchema(BaseModel):
    """Pass-2 output: CITIES scoring of one pass-1 extraction."""
    organisation_name: str = Field(...,
                                   description="Mapped from organisation.q01_organisation_name.")
    year: int = Field(...,
                      description="Reporting year of the document under evaluation. Supplied to you as curated data — copy it exactly, never infer it.")
    categories: ScoringCategories
    overall_score: OverallScore


def finalise_scoring(
    scoring: DialGlobalScoringSchema,
    reporting_year: int,
) -> DialGlobalScoringSchema:
    """
    Overwrite every field on a pass-2 result that should not be the model's call.

    The model supplies judgement only — the 1-5 rubric level per category, the
    rationale, and the uncertainty flags. Two other kinds of field are fixed up
    here rather than trusted from the model:

    Arithmetic, recomputed from the rubric levels:
      * category_weight_percent  <- CATEGORY_WEIGHTS
      * weighted_score           <- (score / 5) * weight
      * total_score_percentage   <- sum of the six weighted scores

    Curated data, stamped from the source of truth:
      * year <- `reporting_year`, which comes from the hand-curated DIAL Global
        tracking workbook via dial_global_reports.year. The model never gets to
        decide the reporting year: a report full of prior-year comparatives is
        exactly the kind of document an LLM misreads the year from.

    Mutates and returns the same instance.
    """
    total = 0.0

    for name, weight in CATEGORY_WEIGHTS.items():
        category = getattr(scoring.categories, name)
        category.category_weight_percent = weight
        category.weighted_score = round(
            (category.score / MAX_CATEGORY_SCORE) * weight, 4)
        total += category.weighted_score

    scoring.overall_score.total_score_percentage = round(total, 4)
    scoring.year = int(reporting_year)
    return scoring


# Section key -> schema class, mirroring SCHEMA_MAP's role for the env/gov
# engine. Deliberately a separate map: nothing here is registered in SCHEMA_MAP.
DIAL_GLOBAL_SCHEMA_MAP = {
    # Pass 1 asks for the WIRE shape — the contract shape overflows Gemini's
    # constrained-decoding limit. assemble_extraction() converts it back.
    "dial_global_extract": DialGlobalExtractionWire,
    "dial_global_score": DialGlobalScoringSchema,
}
