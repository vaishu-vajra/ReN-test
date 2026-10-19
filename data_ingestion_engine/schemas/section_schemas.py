# app/schemas/esg_report_sections.py
# This file adapts the comprehensive schemas from esg_report_full.py
# into section-based models for use with a SCHEMA_MAP, bridging the new
# schema with the existing data extraction workflow.

from typing import List, Optional
from typing_extensions import Annotated
from pydantic import BaseModel, ConfigDict, Field

# Import the detailed, validated schemas from the main schema file
from .base_schemas import (
    DocumentMetadataSchema,
    KeyPersonnelSchema,
    DEIPrioritiesSchema,
    DEIProgramSchema,
    BoardOfDirectorsSchema,
    EnvironmentalDataSchema,
    GovernanceDataSchema,
    SocialDataSchema
)

# --- Section-Specific Schemas ---
# These models are designed to be the expected output for a specific
# extraction task (e.g., "extract the overview section").


class HighlightsSchema(BaseModel):
    """A summary of the company's DEI initiatives and performance."""
    company_dei_overview: Optional[str] = Field(
        None, description="Overview of the company's DEI initiatives and performance.")
    key_metrics_summary: Optional[str] = Field(
        None, description="A brief summary or narrative of the company's key DEI metrics.")
    notable_achievements: Optional[List[str]] = Field(
        None, description="A list of notable achievements or milestones in the area of DEI.")


class OverviewSection(BaseModel):
    """Schema for extracting the high-level overview and DEI summary."""
    document_metadata: DocumentMetadataSchema
    highlights: HighlightsSchema


class KeyPersonnelSection(BaseModel):
    """Schema for extracting information about key individuals involved in DEI."""
    document_metadata: DocumentMetadataSchema
    key_personnel: Optional[List[KeyPersonnelSchema]] = Field(
        None, description="Information about key individuals responsible for or significantly involved in DEI initiatives.")


class KeyPrioritiesSection(BaseModel):
    """Schema for extracting strategic and aspirational DEI goals."""
    document_metadata: DocumentMetadataSchema
    dei_priorities: Optional[DEIPrioritiesSchema] = Field(
        None, description="Capture a comprehensive summary of the aspirational goal, explaining its significance to the company's long-term vision and its connection to core business values. Articulate the 'what' and the 'why' of the goal in 2-4 sentences, based on the provided text. For example: 'Our goal is to achieve full pay equity across all roles and demographics by 2028, reflecting our core value of fairness and our commitment to being an industry-leading employer.'"
    )


class KeyInitiativesSection(BaseModel):
    """Schema for extracting specific DEI programs and initiatives."""
    document_metadata: DocumentMetadataSchema
    dei_programs: Optional[List[DEIProgramSchema]] = Field(
        None, description="Extract the full, detailed narrative for each program, summarizing only if necessary to maintain clarity. Capture the program's core objectives, specific activities, target audience, and intended impact. Aim for a 30-60 word summary for each program if the source provides sufficient detail. For instance, if the text mentions a mentorship program, include who it's for (e.g., 'early-career women in engineering') and what it entails (e.g., 'monthly one-on-one sessions with senior leaders')."
    )


# To isolate just the board members, we create a minimal nested structure
class DEILeadershipForBoardSection(BaseModel):
    board_of_directors: Optional[BoardOfDirectorsSchema] = None


class DEIGovernanceForBoardSection(BaseModel):
    leadership: Optional[DEILeadershipForBoardSection] = None


class BoardMembersSection(BaseModel):
    """Schema for extracting details about the Board of Directors."""
    document_metadata: DocumentMetadataSchema
    dei_governance: Optional[DEIGovernanceForBoardSection] = None


class EnvironmentalSection(BaseModel):
    """Schema for extracting the entire environmental data pillar."""
    document_metadata: DocumentMetadataSchema
    environmental_data: Optional[EnvironmentalDataSchema] = None


class GovernanceSection(BaseModel):
    """Schema for extracting the entire governance data pillar."""
    document_metadata: DocumentMetadataSchema
    governance_data: Optional[GovernanceDataSchema] = None


class SocialSection(BaseModel):
    """Schema for extracting the entire social data pillar."""
    document_metadata: DocumentMetadataSchema
    social_data: Optional[SocialDataSchema] = None


# --- Schema Mapping ---
# This map connects the extraction keys to their corresponding Pydantic models,
# allowing the application to dynamically select the correct schema for validation.

# Constrained types for clarity/validation
PercentInt = Annotated[int, Field(
    ge=0, le=100, description="Percentage without '%' (0–100).")]
YearInt = Annotated[int, Field(
    ge=1900, le=2100, description="Four-digit year, e.g., 2026.")]


class DimensionsItemSchema(BaseModel):
    """
    One consolidated DEI dimensions block containing summaries, verbatim quotes,
    and quantitative targets. Designed to be placed inside a single-item array.
    """
    model_config = ConfigDict(from_attributes=True)

    # Summaries (prompt asks ≥120 words when present; enforced by prompt, not schema)
    gender_summary: Optional[str] = Field(
        None, description="Detailed summary of all gender-related content found in the document."
    )
    ethnicity_summary: Optional[str] = Field(
        None, description="Detailed summary of all ethnicity-related content found in the document."
    )
    other_dimensions_summary: Optional[str] = Field(
        None, description="Summary for other DEI dimensions (disability, LGBTQ+, neurodiversity, age, etc.)."
    )

    # Verbatim quotes (direct quotes only per prompt)
    gender_quotes: Optional[List[str]] = Field(
        None, description="Direct, verbatim quotes from the document mentioning gender."
    )
    ethnicity_quotes: Optional[List[str]] = Field(
        None, description="Direct, verbatim quotes from the document mentioning ethnicity."
    )
    other_dimensions_quotes: Optional[List[str]] = Field(
        None, description="Direct, verbatim quotes mentioning other DEI dimensions."
    )

    # Quantitative DEI targets (percent values without % sign) + target years where available
    board_gender_target: Optional[PercentInt] = Field(
        None, description="Target % for board gender representation."
    )
    board_gender_target_year: Optional[YearInt] = Field(
        None, description="Target year for board gender representation."
    )
    executive_gender_target: Optional[PercentInt] = Field(
        None, description="Target % for executive gender representation."
    )
    executive_gender_target_year: Optional[YearInt] = Field(
        None, description="Target year for executive gender representation."
    )
    all_employees_gender_target: Optional[PercentInt] = Field(
        None, description="Target % for company-wide gender representation."
    )
    all_employees_gender_target_year: Optional[YearInt] = Field(
        None, description="Target year for company-wide gender representation."
    )

    board_ethnicity_target: Optional[PercentInt] = Field(
        None, description="Target % for board ethnicity representation."
    )
    board_ethnicity_target_year: Optional[YearInt] = Field(
        None, description="Target year for board ethnicity representation."
    )
    executive_ethnicity_target: Optional[PercentInt] = Field(
        None, description="Target % for executive ethnicity representation."
    )
    executive_ethnicity_target_year: Optional[YearInt] = Field(
        None, description="Target year for executive ethnicity representation."
    )
    all_employees_ethnicity_target: Optional[PercentInt] = Field(
        None, description="Target % for company-wide ethnicity representation."
    )
    all_employees_ethnicity_target_year: Optional[YearInt] = Field(
        None, description="Target year for company-wide ethnicity representation."
    )


class DimensionsSummarySection(BaseModel):
    """
    Top-level container for the dimensions summary extraction.
    Matches the expected output shape:
    {
      "document_metadata": {...},
      "dimensions": [ { ... } ]   # typically a single item
    }
    """
    model_config = ConfigDict(from_attributes=True)

    document_metadata: DocumentMetadataSchema
    dimensions: Optional[List[DimensionsItemSchema]] = Field(
        None,
        description="DEI dimension summaries, quotes, and numeric targets. Typically a single-item array."
    )


SCHEMA_MAP = {
    "overview and key-metrics": OverviewSection,
    "key-personal": KeyPersonnelSection,
    "key-priorities": KeyPrioritiesSection,
    "key-initiatives": KeyInitiativesSection,
    "board-members": BoardMembersSection,
    "dimensions-summary": DimensionsSummarySection,
    "environmental": EnvironmentalSection,
    "governance": GovernanceSection,
    "social": SocialSection,
}
