"""
visa_extraction.py

Usage:
    python scripts/visa_extraction.py <path_to_excel_file.xlsx>
"""

import sys
import os
import json
import re

import pandas as pd
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# ---------------------------------------------------------------------------

class IdentificationSectionSchema(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None

class AboutTheBusinessSectionSchema(BaseModel):
    industries: Optional[List[str]] = Field(default=None, description="Select up to 3 industries from the approved list.")
    team_size: Optional[Literal["1", "2-5", "6-10", "11-20", "21-30", "31-50", "50+"]] = None
    industry_other_description: Optional[str] = Field(default=None, description="Required if 'Other: please describe' is selected in industries.")

class TractionSectionSchema(BaseModel):
    total_raised_nok: Optional[str] = Field(default=None, description="The amount of funding raised by the applicant.")
    total_raised_nok_score: Optional[str] = Field(default=None, description="Score based on total funding raised: 5: >5m; 4: 1m-5m; 3: 250k-1m; 2: 100k-250k; 1: 0-100k.")
    revenue_2025_nok: Optional[str] = None
    revenue_2025_nok_score: Optional[str] = Field(default=None, description="Score based on 2025 Revenue: 5: >5m; 4: 1m-5m; 3: 250k-1m; 2: 100k-250k; 1: 0-100k.")
    revenue_2024_nok: Optional[str] = None
    revenue_2024_nok_score: Optional[str] = Field(default=None, description="Score based on 2024 Revenue: 5: >5m; 4: 1m-5m; 3: 250k-1m; 2: 100k-250k; 1: 0-100k.")
    number_of_users: Optional[str] = None
    number_of_users_score: Optional[str] = Field(default=None, description="Score based on user count: 5: 501+; 4: 101-500; 3: 26-100; 2: 6-25; 1: 0-5.")
    number_of_customers: Optional[str] = None
    number_of_customers_score: Optional[str] = Field(default=None, description="Score based on customer count: 5: 151+; 4: 21-150; 3: 11-20; 2: 1-10; 1: 0.")
    yoy_growth_rate: Optional[str] = None
    yoy_growth_rate_score: Optional[str] = Field(default=None, description="Score based on growth %: 5: 51%+; 4: 21-50%; 3: 11-20%; 2: 6-10%; 1: 0-5%.")
    growth_basis: Optional[Literal["Revenue", "Users"]] = None
    number_of_partnerships: Optional[str] = None
    number_of_partnerships_score: Optional[str] = Field(default=None, description="Score based on partnerships: 5: 21+; 4: 11-20; 3: 6-10; 2: 1-5; 1: 0.")
    number_of_pilot_projects: Optional[str] = None
    number_of_pilot_projects_score: Optional[str] = Field(default=None, description="Score based on pilots: 5: 5+; 3: 1-5; 1: 0.")
    notable_traction_elaboration: Optional[str] = None
    notable_traction_elaboration_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Provide a detailed justification for the score. Reference specific evidence of partnerships or pilots from the narrative.")
    notable_traction_elaboration_score: Optional[str] = Field(default=None, description="5: Exceptional (High-Leverage/Global: major international partner/household name); 4: Strong (Significant/Industry: clear win with industry player or validated pilot); 3: Average (Standard/Local: small business or local-only partnerships); 2: Weak (Soft/Informal: feedback/social media only); 1: Poor (None/Subjective).")
    biggest_achievement: Optional[str] = None
    biggest_achievement_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Justify the score by explaining the scale and business impact of the cited achievement.")
    biggest_achievement_score: Optional[str] = Field(default=None, description="5: Exceptional (Quantified Breakthrough: major milestone with data); 4: Strong (Tangible Business Win: clear milestone like seed round/competition finalist); 3: Average (Operational Milestone: hiring/office opening); 2: Weak (Tactical/Survival: LLC setup/first year); 1: Poor (Subjective/Personal: no business impact).")
    business_goals_3_years: Optional[str] = None
    business_goals_3_years_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Evaluate goals against SMART criteria and justify the assigned rating.")
    business_goals_3_years_score: Optional[str] = Field(default=None, description="5: Exceptional (Full SMART Compliance for all 3 goals); 4: Strong (Partial SMART Compliance); 3: Average (Directional: clear path but lacks KPIs/deadlines); 2: Weak (Vague/General); 1: Poor (Aspirational Only: unrealistic/no strategy).")

class ProblemSectionSchema(BaseModel):
    problem_statement: Optional[str] = None
    problem_statement_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Analyze if the statement includes a defined niche, quantified impact, and urgency.")
    problem_statement_score: Optional[str] = Field(default=None, description="5: Exceptional (Highly Specific: contains niche, quantified impact, and urgency); 4: Strong (Specific but Qualitative: includes all 3 but impact is descriptive); 3: Average (Generalist: who/what identified but audience broad/urgency missing); 2: Weak (Vague: missing who/how); 1: Poor (Missing/Mismatched).")

class SolutionSectionSchema(BaseModel):
    solution_description: Optional[str] = None
    solution_description_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Explain how directly the solution addresses the specific pain points mentioned in the Problem Section.")
    solution_description_score: Optional[str] = Field(default=None, description="5: Exceptional (Direct Bridge: maps features to pain points with clear user journey); 4: Strong (Strong Fit: solves problem clearly but user journey less detailed); 3: Average (Functional: generic app/standard solution); 2: Weak (Feature-Heavy: lists what it is, not how it solves pain); 1: Poor (Mismatched: does not address stated problem).")

class InnovationSectionSchema(BaseModel):
    innovation_and_differentiation: Optional[str] = None
    innovation_and_differentiation_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Identify the specific technical or structural 'moat' described by the applicant.")
    innovation_and_differentiation_score: Optional[str] = Field(default=None, description="5: Exceptional (Hard Moat: IP/Patent, new category, or Platform Effect); 4: Strong (Significant Advantage: unique AI/automation); 3: Average (Meaningful Pivot: superior USP/niche focus); 2: Weak (Incremental: soft differentiators like 'better service'); 1: Poor (Me-too Business: no differentiation).")

class ImpactSectionSchema(BaseModel):
    grant_impact: Optional[str] = None
    grant_impact_score_justification: Optional[str] = Field(default=None, description="AI Instruction: Justify the score based on how well the funds and coaching address specific business bottlenecks.")
    grant_impact_score: Optional[str] = Field(default=None, description="5: Exceptional (High Leverage: identifies bottleneck, provides KPI for cash, specific coaching needs); 4: Strong (Clear & Tangible: growth-oriented funds and goal-aligned coaching); 3: Average (Sensible/Standard: general marketing/leadership); 2: Weak (Survival/OPEX: rent/general expenses); 1: Poor (No Plan: vague request for cash).")

class TNAVisaApplicationEvaluationSchema(BaseModel):
    identification_section: Optional[IdentificationSectionSchema] = None
    about_the_business_section: Optional[AboutTheBusinessSectionSchema] = None
    traction_section: Optional[TractionSectionSchema] = None
    problem_section: Optional[ProblemSectionSchema] = None
    solution_section: Optional[SolutionSectionSchema] = None
    innovation_section: Optional[InnovationSectionSchema] = None
    impact_section: Optional[ImpactSectionSchema] = None

# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert AI Grant Application Analyst. Your sole function is to process a single grant application and return a populated JSON object that strictly conforms to the required response schema.

**Primary Directive:**
Analyze the provided application data and generate a complete JSON object. The specific scoring logic and criteria are detailed within the `description` fields of the response schema you have been instructed to use.

**Critical Instructions:**

1.  **Schema as a Single Source of Truth:** Your primary instructions for scoring are located in the `description` of each field in the required JSON output schema. Adhere to this logic meticulously (e.g., "5: Exceptional...", "4: Strong...").

2.  **Mandatory Justification Policy:** For every `_score_justification` field, you MUST provide a concise justification that **directly quotes** relevant text from the applicant's response to support the assigned score. This is a non-negotiable requirement.
    *   **Example Justification:** "Score of 4 assigned because the applicant states a 'Tangible Business Win' by having 'Launched MVP to 500 users'."

3.  **Input Data:** The input you receive represents one application. The field names in the input directly correspond to the fields you need to evaluate."""


# ---------------------------------------------------------------------------
def sanitise_filename(first: str, last: str) -> str:
    name = f"{first}_{last}".lower()
    name = re.sub(r"[^a-z0-9_]", "", name)
    return f"{name}.json" if name else "unknown.json"


def row_to_dict(row: pd.Series) -> dict:
    return {k: str(v) for k, v in row.items() if pd.notna(v) and str(v).strip()}


def call_gemini(row_dict: dict) -> dict:
    
    full_prompt = (
        SYSTEM_PROMPT
        + "\n\nINPUT APPLICATION DATA:\n"
        + json.dumps(row_dict, ensure_ascii=False, indent=2)
    )

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TNAVisaApplicationEvaluationSchema,  # Pydantic schema here
        )
    )

    return json.loads(response.text)


# ---------------------------------------------------------------------------
def main(excel_path: str):
    df = pd.read_excel(excel_path, header=1)
    print(f"Loaded {len(df)} applications from: {excel_path}")

    os.makedirs("responses", exist_ok=True)

    for idx, row in df.iterrows():
        row_dict = row_to_dict(row)

        first = str(row_dict.get("first_name", f"app{idx}")).strip()
        last = str(row_dict.get("last_name", "")).strip()
        filename = sanitise_filename(first, last)
        output_path = os.path.join("responses", filename)

        print(f"[{idx+1}/{len(df)}] Processing: {first} {last} -> {filename}")

        try:
            result = None
            for attempt in range(3):
                try:
                    result = call_gemini(row_dict)
                    break
                except Exception as retry_err:
                    print(f"  Retry {attempt+1}/3: {retry_err}")
            if result is None:
                raise Exception("Failed after 3 retries")

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"  OK Saved: {output_path}")

        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"\nDone. responses/")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/visa_extraction.py <path_to_excel.xlsx>")
        sys.exit(1)

    main(sys.argv[1])