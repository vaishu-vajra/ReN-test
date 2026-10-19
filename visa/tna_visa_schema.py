from typing import List, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict

# TNA Visa Application Evaluation Schemas 

class IdentificationSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    first_name: Optional[str] = None
    last_name: Optional[str] = None

class AboutTheBusinessSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    industries: Optional[List[str]] = Field(
        default=None, 
        max_length=3,
        description="Select up to 3 industries from the approved list." # [cite: 2, 3, 4, 5, 6, 7]
    )
    team_size: Optional[Literal["1", "2-5", "6-10", "11-20", "21-30", "31-50", "50+"]] = None # [cite: 7]
    industry_other_description: Optional[str] = Field(
        default=None,
        description="Required if 'Other: please describe' is selected in industries." # [cite: 8, 9, 10]
    )

class TractionSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    total_raised_nok: Optional[str] = Field(default=None, description="The amount of funding raised by the applicant.") # [cite: 11]
    total_raised_nok_score: Optional[str] = Field(default=None, description="Score based on total funding raised: 5: >5m; 4: 1m-5m; 3: 250k-1m; 2: 100k-250k; 1: 0-100k.") # [cite: 12]
    revenue_2025_nok: Optional[str] = None
    revenue_2025_nok_score: Optional[str] = Field(default=None, description="Score based on 2025 Revenue: 5: >5m; 4: 1m-5m; 3: 250k-1m; 2: 100k-250k; 1: 0-100k.") # [cite: 13]
    revenue_2024_nok: Optional[str] = None
    revenue_2024_nok_score: Optional[str] = Field(default=None, description="Score based on 2024 Revenue: 5: >5m; 4: 1m-5m; 3: 250k-1m; 2: 100k-250k; 1: 0-100k.") # [cite: 14]
    number_of_users: Optional[str] = None
    number_of_users_score: Optional[str] = Field(default=None, description="Score based on user count: 5: 501+; 4: 101-500; 3: 26-100; 2: 6-25; 1: 0-5.") # [cite: 15]
    number_of_customers: Optional[str] = None
    number_of_customers_score: Optional[str] = Field(default=None, description="Score based on customer count: 5: 151+; 4: 21-150; 3: 11-20; 2: 1-10; 1: 0.") # [cite: 16]
    yoy_growth_rate: Optional[str] = None
    yoy_growth_rate_score: Optional[str] = Field(default=None, description="Score based on growth %: 5: 51%+; 4: 21-50%; 3: 11-20%; 2: 6-10%; 1: 0-5%.") # [cite: 17]
    growth_basis: Optional[Literal["Revenue", "Users"]] = None # [cite: 17]
    number_of_partnerships: Optional[str] = None
    number_of_partnerships_score: Optional[str] = Field(default=None, description="Score based on partnerships: 5: 21+; 4: 11-20; 3: 6-10; 2: 1-5; 1: 0.") # [cite: 18]
    number_of_pilot_projects: Optional[str] = None
    number_of_pilot_projects_score: Optional[str] = Field(default=None, description="Score based on pilots: 5: 5+; 3: 1-5; 1: 0.") # [cite: 19]
    notable_traction_elaboration: Optional[str] = None
    notable_traction_elaboration_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Provide a detailed justification for the score. Reference specific evidence of partnerships or pilots from the narrative.") # [cite: 20]
    notable_traction_elaboration_score: Optional[str] = Field(default=None, description="5: Exceptional (High-Leverage/Global: major international partner/household name); 4: Strong (Significant/Industry: clear win with industry player or validated pilot); 3: Average (Standard/Local: small business or local-only partnerships); 2: Weak (Soft/Informal: feedback/social media only); 1: Poor (None/Subjective).") # [cite: 21]
    biggest_achievement: Optional[str] = None
    biggest_achievement_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Justify the score by explaining the scale and business impact of the cited achievement.") # [cite: 22]
    biggest_achievement_score: Optional[str] = Field(default=None, description="5: Exceptional (Quantified Breakthrough: major milestone with data); 4: Strong (Tangible Business Win: clear milestone like seed round/competition finalist); 3: Average (Operational Milestone: hiring/office opening); 2: Weak (Tactical/Survival: LLC setup/first year); 1: Poor (Subjective/Personal: no business impact).") # [cite: 23]
    business_goals_3_years: Optional[str] = None
    business_goals_3_years_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Evaluate goals against SMART criteria and justify the assigned rating.") # [cite: 24]
    business_goals_3_years_score: Optional[str] = Field(default=None, description="5: Exceptional (Full SMART Compliance for all 3 goals); 4: Strong (Partial SMART Compliance); 3: Average (Directional: clear path but lacks KPIs/deadlines); 2: Weak (Vague/General); 1: Poor (Aspirational Only: unrealistic/no strategy).") # [cite: 25]

class ProblemSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    problem_statement: Optional[str] = None
    problem_statement_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Analyze if the statement includes a defined niche, quantified impact, and urgency.") # [cite: 26]
    problem_statement_score: Optional[str] = Field(default=None, description="5: Exceptional (Highly Specific: contains niche, quantified impact, and urgency); 4: Strong (Specific but Qualitative: includes all 3 but impact is descriptive); 3: Average (Generalist: who/what identified but audience broad/urgency missing); 2: Weak (Vague: missing who/how); 1: Poor (Missing/Mismatched).") # [cite: 27]

class SolutionSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    solution_description: Optional[str] = None
    solution_description_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Explain how directly the solution addresses the specific pain points mentioned in the Problem Section.") # [cite: 28]
    solution_description_score: Optional[str] = Field(default=None, description="5: Exceptional (Direct Bridge: maps features to pain points with clear user journey); 4: Strong (Strong Fit: solves problem clearly but user journey less detailed); 3: Average (Functional: generic app/standard solution); 2: Weak (Feature-Heavy: lists what it is, not how it solves pain); 1: Poor (Mismatched: does not address stated problem).") # [cite: 29]

class InnovationSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    innovation_and_differentiation: Optional[str] = None
    innovation_and_differentiation_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Identify the specific technical or structural 'moat' described by the applicant.") # [cite: 30]
    innovation_and_differentiation_score: Optional[str] = Field(default=None, description="5: Exceptional (Hard Moat: IP/Patent, new category, or Platform Effect); 4: Strong (Significant Advantage: unique AI/automation); 3: Average (Meaningful Pivot: superior USP/niche focus); 2: Weak (Incremental: soft differentiators like 'better service'); 1: Poor (Me-too Business: no differentiation).") # [cite: 31]

class ImpactSectionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    grant_impact: Optional[str] = None
    grant_impact_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Justify the score based on how well the funds and coaching address specific business bottlenecks.") # [cite: 32]
    grant_impact_score: Optional[str] = Field(default=None, description="5: Exceptional (High Leverage: identifies bottleneck, provides KPI for cash, specific coaching needs); 4: Strong (Clear & Tangible: growth-oriented funds and goal-aligned coaching); 3: Average (Sensible/Standard: general marketing/leadership); 2: Weak (Survival/OPEX: rent/general expenses); 1: Poor (No Plan: vague request for cash).") # [cite: 33]

class TNAVisaApplicationEvaluationSchema(BaseModel):
    """Root Schema for the TNA Visa Application Evaluation [cite: 1]"""
    model_config = ConfigDict(from_attributes=True)
    identification_section: Optional[IdentificationSectionSchema] = None
    about_the_business_section: Optional[AboutTheBusinessSectionSchema] = None
    traction_section: Optional[TractionSectionSchema] = None
    problem_section: Optional[ProblemSectionSchema] = None
    solution_section: Optional[SolutionSectionSchema] = None
    innovation_section: Optional[InnovationSectionSchema] = None
    impact_section: Optional[ImpactSectionSchema] = None