# app/schemas/esg_report_full.py
# This file contains Pydantic models that mirror the entire ESG Reporting JSON schema.
# These schemas are used to validate the raw JSON data upon ingestion.

from typing_extensions import Annotated
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import date

# --- Reusable Definitions ($defs) ---
# These are the base building blocks used throughout the schema.


class YearlyMetricSchema(BaseModel):
    """Schema for a numeric metric reported for a specific year, with a defined unit."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., description="The numeric value of the metric.")
    unit: str = Field(..., description="The unit of measurement for the value (e.g., 'tonnes_co2e', 'GJ', 'ml', 'currency').")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year for the metric.")


class PercentageMetricSchema(BaseModel):
    """Schema for a percentage metric, with a value between 0 and 100, reported for a specific year."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., ge=0, le=100,
                         description="The percentage value of the metric.")
    unit: Literal["percentage"] = Field(
        ..., description="The unit of measurement, fixed to 'percentage'.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year for the metric.")


class PolicyDetailsSchema(BaseModel):
    """Common properties for various company policies."""
    model_config = ConfigDict(from_attributes=True)
    exists: bool = Field(..., description="Indicates if the policy exists.")
    implementation_date: Optional[date] = Field(
        None, description="The date the policy was implemented.")


class PersonDetailsSchema(BaseModel):
    """Common properties for individual personnel details."""
    model_config = ConfigDict(from_attributes=True)
    name: Optional[str] = Field(None, description="Name of the individual.")
    title: Optional[str] = Field(
        None, description="Title or position of the individual.")
    committee_memberships: Optional[List[str]] = Field(
        None, description="List of committees the individual is a member of.")
    dimension_focus: Optional[List[str]] = Field(
        None, description="Specific DEI or ESG dimensions this person focuses on, e.g. gender, ethnicity, or sustainability.")


# --- Environmental Data Schemas ---

class GHGEmissionsScope1Schema(YearlyMetricSchema):
    """Direct GHG emissions from sources owned or controlled by the company."""
    unit: str = Field(
        ..., description="The unit of measurement for Scope 1 emissions.")


class GHGEmissionsScope2Schema(BaseModel):
    """Indirect GHG emissions from the generation of purchased electricity, steam, heating, or cooling."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(...,
                         description="The primary reported value for Scope 2 emissions.")
    unit: str = Field(
        ..., description="The unit of measurement for Scope 2 emissions.")
    market_based_value: Optional[float] = Field(
        None, description="Scope 2 emissions calculated using the market-based method.")
    location_based_value: Optional[float] = Field(
        None, description="Scope 2 emissions calculated using the location-based method.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year for Scope 2 emissions.")


class GHGEmissionsIntensitySchema(BaseModel):
    """GHG emissions intensity, linking emissions to a business metric."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(...,
                         description="The numeric value of emissions intensity.")
    unit: str = Field(..., description="The unit of emissions intensity (e.g., 'tonnes CO2e per MWh produced').")
    calculation_basis: Literal["revenue", "employees", "production"] = Field(
        ..., description="The business metric used as the denominator for intensity calculation.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year for emissions intensity.")


class GHGReductionTargetsProgressSchema(BaseModel):
    """Information on progress towards GHG emission reduction targets."""
    model_config = ConfigDict(from_attributes=True)
    description: str = Field(
        ..., description="A narrative description of the GHG emission reduction target.")
    percentage_reduction: float = Field(..., ge=0, le=100,
                                        description="The percentage reduction achieved towards the target.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The year the reported reduction was measured against the baseline.")


class GHGEmissionsSchema(BaseModel):
    """Greenhouse Gas (GHG) emissions data, typically reported in CO2 equivalent tonnes."""
    model_config = ConfigDict(from_attributes=True)
    scope_1: Optional[GHGEmissionsScope1Schema] = None
    scope_2: Optional[GHGEmissionsScope2Schema] = None
    intensity: Optional[GHGEmissionsIntensitySchema] = None
    reduction_targets_progress: Optional[GHGReductionTargetsProgressSchema] = None


class EnergyTotalConsumptionSchema(YearlyMetricSchema):
    """Total energy consumed by the organization."""
    unit: str = Field(...,
                      description="The unit of measurement for energy consumption.")


class EnergyIntensitySchema(BaseModel):
    """Energy intensity, linking energy consumption to a business metric."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(...,
                         description="The numeric value of energy intensity.")
    unit: str = Field(..., description="The unit of energy intensity (e.g., 'GJ per unit of production').")
    calculation_basis: Literal["revenue", "production"] = Field(
        ..., description="The business metric used as the denominator for intensity calculation.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year for energy intensity.")


class EnergySchema(BaseModel):
    """Energy consumption and renewable energy usage data."""
    model_config = ConfigDict(from_attributes=True)
    total_consumption: Optional[EnergyTotalConsumptionSchema] = None
    intensity: Optional[EnergyIntensitySchema] = None


class WaterTotalWithdrawalSchema(YearlyMetricSchema):
    """Total volume of water withdrawn from all sources."""
    unit: str = Field(...,
                      description="The unit of measurement for water withdrawal.")


class WaterSchema(BaseModel):
    """Water management data, including withdrawal."""
    model_config = ConfigDict(from_attributes=True)
    total_withdrawal: Optional[WaterTotalWithdrawalSchema] = None


class WasteDivertedFromDisposalPercentageSchema(BaseModel):
    """Percentage of generated waste that is diverted from landfill or incineration."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., ge=0, le=100,
                         description="The percentage of waste diverted from disposal.")
    unit: Literal["percentage"] = Field(
        ..., description="The unit of measurement, fixed to 'percentage'.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year.")
    recovery_method: Optional[List[Literal["recycling", "reuse", "other"]]] = Field(
        None, description="Methods used to divert waste from disposal.")


class WasteSchema(BaseModel):
    """Waste generation, disposal, and diversion data."""
    model_config = ConfigDict(from_attributes=True)
    diverted_from_disposal_percentage: Optional[WasteDivertedFromDisposalPercentageSchema] = None


class BiodiversitySitesNearProtectedAreasSchema(BaseModel):
    """Details about operational sites located in or adjacent to protected areas."""
    model_config = ConfigDict(from_attributes=True)
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year for this data.")
    sites_count: int = Field(
        ..., description="Number of operational sites located in or directly adjacent to protected areas.")
    total_area_hectares: float = Field(
        ..., description="Total area of operational sites in or adjacent to protected areas.")


class BiodiversitySchema(BaseModel):
    """Information on the company's impacts on biodiversity and ecosystems."""
    model_config = ConfigDict(from_attributes=True)
    sites_near_protected_areas: Optional[BiodiversitySitesNearProtectedAreasSchema] = None


class EnvironmentalProtectionExpendituresSchema(BaseModel):
    """Total expenditures and investments dedicated to environmental protection activities."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., description="The total amount of expenditures.")
    currency: str = Field(...,
                          description="The currency of the expenditure amount.")
    purpose: List[Literal["prevention", "remediation", "management"]] = Field(
        ..., description="The primary purposes of the environmental protection expenditures.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The reporting year.")


class EnvironmentalInvestmentsSchema(BaseModel):
    """Financial investments and expenditures related to environmental protection."""
    model_config = ConfigDict(from_attributes=True)
    environmental_protection_expenditures: Optional[EnvironmentalProtectionExpendituresSchema] = None


class EnvironmentalDataSchema(BaseModel):
    """The main schema for the entire 'environmental_data' object."""
    model_config = ConfigDict(from_attributes=True)
    company_environmental_overview: Optional[str] = Field(
        None, description="A one-paragraph summary of the company's overall approach to climate change and environmental management.")
    key_environmental_metrics: Optional[List[str]] = Field(
        None, description="A bulleted list of the most important environmental data points the company is tracking and reporting on (e.g., 'Scope 1 GHG emissions: The company has set a target to achieve 40% reduction in Scope 1 GHG emissions by 2027').")
    notable_environmental_achievements: Optional[List[str]] = Field(
        None, description="Extract a comprehensive, detailed summary for each significant achievement or initiative mentioned. Capture the specific metrics, dates, and impacts discussed in the text. For example, instead of 'Reduced GHG emissions,' extract 'Achieved a 40% reduction of GHG emissions in 2024 against a 2020 baseline, preventing 50,000 tonnes of CO2e.' Each item in the array should be a complete and descriptive sentence.")
    ghg_emissions: Optional[GHGEmissionsSchema] = None
    energy: Optional[EnergySchema] = None
    water: Optional[WaterSchema] = None
    waste: Optional[WasteSchema] = None
    biodiversity: Optional[BiodiversitySchema] = None
    investments: Optional[EnvironmentalInvestmentsSchema] = None


# --- Social Data Schemas ---

class DedicatedDEILeaderSchema(BaseModel):
    """Information about a designated leader for Diversity, Equity, and Inclusion."""
    model_config = ConfigDict(from_attributes=True)
    exists: bool = Field(...,
                         description="Indicates if a dedicated DEI leader position exists.")
    executive_member: Optional[bool] = Field(
        None, description="Indicates if the DEI leader is part of the executive management team.")
    name: Optional[str] = Field(
        None, description="Name of the dedicated DEI leader.")
    title: Optional[str] = Field(
        None, description="Official title of the DEI leader.")
    appointment_date: Optional[date] = Field(
        None, description="Date of the DEI leader's appointment.")
    reporting_line: Optional[str] = Field(
        None, description="To whom the DEI leader reports within the organizational structure.")
    scope_of_responsibility: Optional[str] = Field(
        None, description="A description of the key responsibilities and scope of the DEI leader's role.")


class DEICommitteeMemberSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str = Field(..., description="Name of the committee member.")
    role: str = Field(...,
                      description="Role of the member within the committee or company.")


class DEICommitteeSchema(BaseModel):
    """Information about a formal Diversity, Equity, and Inclusion committee."""
    model_config = ConfigDict(from_attributes=True)
    exists: bool = Field(...,
                         description="Indicates if a DEI committee exists.")
    established_date: Optional[date] = Field(
        None, description="The date the DEI committee was established.")
    members: Optional[List[DEICommitteeMemberSchema]] = Field(
        None, description="List of members of the DEI committee, including their roles.")


class BoardOfDirectorsMemberSchema(PersonDetailsSchema):
    name: str = Field(..., description="Name of the board member.")
    title: str = Field(...,
                       description="Title of the board member (e.g., 'Independent Director').")
    biography: Optional[str] = Field(
        None, description="Brief biography highlighting relevant experience.")


class BoardOfDirectorsSchema(BaseModel):
    """Details about the company's Board of Directors, with a focus on DEI aspects."""
    model_config = ConfigDict(from_attributes=True)
    exists: bool = Field(
        ..., description="Indicates if a Board of Directors exists for the company.")
    members: Optional[List[BoardOfDirectorsMemberSchema]] = Field(
        None, description="List of board members and their relevant details.")


class DEILeadershipSchema(BaseModel):
    """Details regarding key leadership roles and committees for DEI."""
    model_config = ConfigDict(from_attributes=True)
    dedicated_dei_leader: Optional[DedicatedDEILeaderSchema] = None
    dei_committee: Optional[DEICommitteeSchema] = None
    board_of_directors: Optional[BoardOfDirectorsSchema] = None


class DEIParticipationRateSchema(BaseModel):
    """The percentage of targeted employees who participated in DEI training."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., ge=0, le=100,
                         description="The participation rate percentage.")
    unit: Literal["percentage"] = Field(
        ..., description="Unit of measurement, fixed to 'percentage'.")
    period: str = Field(..., description="The reporting period for this participation rate (e.g., 'Fiscal Year 2023', 'Q2 2024').")


class DEIMetricsSchema(BaseModel):
    """Quantitative metrics used to measure the effectiveness or reach of DEI training."""
    model_config = ConfigDict(from_attributes=True)
    participation_rate: Optional[DEIParticipationRateSchema] = None
    completion_rate: Optional[DEIParticipationRateSchema] = Field(
        None, description="The percentage of participants who successfully completed the DEI training.")


class DEITrainingProgramSchema(BaseModel):
    """Details of the company's DEI training program for employees."""
    model_config = ConfigDict(from_attributes=True)
    exists: bool = Field(...,
                         description="Indicates if a DEI training program is in place.")
    implementation_date: Optional[date] = Field(
        None, description="Date the DEI training program was first implemented.")
    frequency: Optional[Literal["monthly", "quarterly", "annual", "other"]] = Field(
        None, description="How often the DEI training is conducted.")
    scope: Optional[str] = Field(
        None, description="The scope of the training (e.g., 'all employees', 'management only', 'new hires').")
    coverage: Optional[str] = Field(
        None, description="A description of the employee groups or departments covered by the training.")
    key_elements: Optional[List[str]] = Field(
        None, description="Key topics or modules covered within the DEI training program.")
    metrics: Optional[DEIMetricsSchema] = None


class DEITrainingSchema(BaseModel):
    """Information regarding Diversity, Equity, and Inclusion training programs."""
    model_config = ConfigDict(from_attributes=True)
    dei_training_program: Optional[DEITrainingProgramSchema] = None


class ParentalLeavePolicySchema(PolicyDetailsSchema):
    """Policy on leave provisions for new parents."""
    gender_equal: Optional[bool] = Field(
        None, description="Indicates if the parental leave policy offers equal benefits regardless of gender.")


class DEIPoliciesSchema(BaseModel):
    """Collection of company policies directly relevant to DEI and social aspects."""
    model_config = ConfigDict(from_attributes=True)
    remuneration_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy detailing employee compensation and benefits.")
    board_diversity_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy addressing diversity composition goals for the Board of Directors.")
    speak_up_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy for employees to voice concerns or report misconduct.")
    health_and_safety_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy for ensuring a safe and healthy workplace.")
    parental_leave_policy: Optional[ParentalLeavePolicySchema] = None
    dei_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="The overarching policy for Diversity, Equity, and Inclusion.")
    discrimination_and_harassment_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy against discrimination and harassment.")
    human_rights_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy on respecting and- upholding human rights.")
    sustainable_development_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy on commitment to sustainable development principles.")
    gender_transition_policy: Optional[PolicyDetailsSchema] = Field(
        None, description="Policy supporting employees undergoing gender transition.")


class RecognitionAwardSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str = Field(..., description="Name of the award received.")
    issuing_organization: str = Field(
        ..., description="The organization that conferred the award.")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The year the award was received.")
    description: Optional[str] = Field(
        None, description="A brief description of the award and what it recognizes.")
    category: Optional[str] = Field(
        None, description="The category the award falls under (e.g., 'Diversity & Inclusion', 'Workplace Culture').")
    scope: Optional[str] = Field(
        None, description="The geographical or industry scope of the award (e.g., 'national', 'global').")
    criteria: Optional[str] = Field(
        None, description="The key criteria or achievements for which the award was granted.")


class RecognitionCertificationSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str = Field(..., description="Name of the certification obtained.")
    issuing_body: str = Field(...,
                              description="The organization that issued the certification.")
    valid_from: date = Field(...,
                             description="The start date of the certification's validity.")
    valid_until: Optional[date] = Field(
        None, description="The end date of the certification's validity. Omit if ongoing.")
    scope: Optional[str] = Field(
        None, description="The scope of the certification (e.g., 'enterprise-wide', 'specific business unit').")


class RecognitionSchema(BaseModel):
    """External recognition received, such as awards and certifications, related to social performance."""
    model_config = ConfigDict(from_attributes=True)
    awards: Optional[List[RecognitionAwardSchema]] = Field(
        None, description="List of awards received by the company for its social or DEI efforts.")
    certifications: Optional[List[RecognitionCertificationSchema]] = Field(
        None, description="List of social-related certifications obtained by the company.")


class DiverseSupplierPercentageSchema(BaseModel):
    """The percentage of the total supplier base identified as diverse."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., ge=0, le=100,
                         description="The percentage of diverse suppliers.")
    unit: Literal["percentage"] = Field(
        ..., description="Unit of measurement, fixed to 'percentage'.")
    period: str = Field(...,
                        description="The reporting period for this metric.")


class DiverseSupplierSpendSchema(BaseModel):
    """The total monetary value spent with diverse suppliers."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(...,
                         description="The total amount spent with diverse suppliers.")
    unit: Literal["currency"] = Field(
        ..., description="Unit of measurement, fixed to 'currency'.")
    currency: str = Field(
        ..., description="The currency code used for the spend amount (e.g., 'USD', 'EUR').")
    period: str = Field(...,
                        description="The reporting period for this metric.")


class YearOverYearChangeSchema(BaseModel):
    """Year-over-year percentage change in diverse supplier engagement or spend."""
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(...,
                         description="The percentage change from the previous year.")
    unit: Literal["percentage"] = Field(
        ..., description="Unit of measurement, fixed to 'percentage'.")
    period: str = Field(...,
                        description="The reporting period for this change.")


class GrowthMetricsSchema(BaseModel):
    """Metrics illustrating the growth or change in supplier diversity over time."""
    model_config = ConfigDict(from_attributes=True)
    year_over_year_change: Optional[YearOverYearChangeSchema] = None


class SupplierDiversityMetricsSchema(BaseModel):
    """Quantitative metrics related to the supplier diversity program."""
    model_config = ConfigDict(from_attributes=True)
    diverse_supplier_percentage: Optional[DiverseSupplierPercentageSchema] = None
    diverse_supplier_spend: Optional[DiverseSupplierSpendSchema] = None
    supplier_categories: Optional[List[str]] = Field(
        None, description="Specific categories of diverse suppliers tracked (e.g., 'minority-owned').")
    growth_metrics: Optional[GrowthMetricsSchema] = None


class SupplierDiversityProgramSchema(BaseModel):
    """Details about the company's formal supplier diversity program."""
    model_config = ConfigDict(from_attributes=True)
    exists: bool = Field(...,
                         description="Indicates if a supplier diversity program is in place.")
    implementation_date: Optional[date] = Field(
        None, description="The date the supplier diversity program was implemented.")
    description: Optional[str] = Field(
        None, description="A narrative description of the supplier diversity program's objectives and activities.")
    scope: Optional[str] = Field(
        None, description="The scope of the supplier diversity program (e.g., 'global', 'domestic').")
    metrics: Optional[SupplierDiversityMetricsSchema] = None


class SupplierDiversitySchema(BaseModel):
    """Information on programs and policies promoting diversity within the supplier base."""
    model_config = ConfigDict(from_attributes=True)
    program: Optional[SupplierDiversityProgramSchema] = None
    policy: Optional[PolicyDetailsSchema] = Field(
        None, description="The formal policy specifically governing supplier diversity.")


class DemographicDetailsSchema(BaseModel):
    """Specific demographic groups or organizational levels related to this metric."""
    model_config = ConfigDict(from_attributes=True)
    category: Optional[Literal["black", "asian", "caucasian", "hispanic", "indigenous", "middle_eastern", "women",
                               "men", "other"]] = Field(None, description="The primary demographic category (e.g., 'black', 'women').")
    subcategory: Optional[str] = Field(
        None, description="A more granular demographic breakdown if applicable (e.g., 'African American' for 'black').")
    level: Optional[Literal["board", "executive", "middle_management", "overall"]] = Field(
        None, description="The organizational level this metric applies to.")


class DimensionMetricSchema(BaseModel):
    """A flexible metric for reporting on a specific diversity dimension."""
    model_config = ConfigDict(from_attributes=True)
    metric_category: Literal["workforce_representation", "leadership_representation", "hiring", "promotion", "retention", "pay_equity", "board_representation",
                             "executive_representation", "middle_management_representation"] = Field(..., description="The overarching category this specific metric belongs to.")
    metric_name: Optional[str] = Field(
        None, description="A specific, unique name for the metric.")
    value: float = Field(...,
                         description="The numeric value reported for this metric.")
    # MODIFIED: Changed from Literal to str to support validation logic
    unit: str = Field(
        ..., description="Unit of measurement. Must be 'percentage' if 'demographic_details' is provided. Otherwise, can be 'percentage', 'ratio', 'count', or 'boolean'.")
    description: Optional[str] = Field(
        None, description="A detailed explanation of the metric, how it's calculated, or its significance.")
    period: Optional[str] = Field(
        None, description="The reporting period for this specific metric (e.g., 'annual FY2023', 'Q1 2024').")
    demographic_details: Optional[DemographicDetailsSchema] = Field(
        None, description="Specific demographic groups or organizational levels related to this metric.")

    # NEW: Added model validator for if/then/else logic
    @model_validator(mode='after')
    def check_unit_based_on_demographics(self) -> 'DimensionMetricSchema':
        if self.demographic_details:
            if self.unit != "percentage":
                raise ValueError(
                    "For metrics with 'demographic_details', the 'unit' must be 'percentage'.")
            allowed_units = {"percentage"}
        else:
            allowed_units = {"percentage", "ratio", "count", "boolean"}

        if self.unit not in allowed_units:
            raise ValueError(
                f"Invalid unit '{self.unit}'. Allowed units are: {allowed_units}")
        return self


class DimensionGoalSchema(BaseModel):
    """Specific, measurable objectives or targets related to this diversity dimension."""
    model_config = ConfigDict(from_attributes=True)
    goal_name: str = Field(...,
                           description="The name or brief title of the goal.")
    description: str = Field(
        ..., description="A detailed explanation of the goal and its importance.")
    target_value: Optional[str] = Field(
        None, description="The desired outcome or target value for the goal (can be qualitative or quantitative).")
    timeline: Optional[str] = Field(
        None, description="The timeframe for achieving the goal (e.g., 'by 2025', 'ongoing').")
    status: Optional[Literal["not_started", "in_progress", "achieved", "delayed"]] = Field(
        None, description="The current status of the goal's progress.")


class SpecificMetricsGenderSchema(BaseModel):
    """Pre-defined metrics related to gender diversity."""
    model_config = ConfigDict(from_attributes=True)
    chair_woman: Optional[bool] = Field(
        None, description="Indicates if the company's highest governance body chair is a woman.")
    ceo_woman: Optional[bool] = Field(
        None, description="Indicates if the company's Chief Executive Officer (CEO) is a woman.")
    executive_ratio: Optional[float] = Field(
        None, ge=0, description="Ratio of female to male representation in executive leadership roles.")
    middle_management_percentage: Optional[float] = Field(
        None, ge=0, le=100, description="Percentage of women in middle management positions.")
    overall_percentage: Optional[float] = Field(
        None, ge=0, le=100, description="Overall percentage of women within the company's total workforce.")
    pay_gap: Optional[float] = Field(
        None, description="Reported gender pay gap (e.g., 0.10 for a 10% gap).")


class SpecificMetricsEthnicitySchema(BaseModel):
    """Pre-defined metrics related to ethnic diversity."""
    model_config = ConfigDict(from_attributes=True)
    chair_minority: Optional[bool] = Field(
        None, description="Indicates if the company's highest governance body chair is from a minority ethnic group.")
    ceo_minority: Optional[bool] = Field(
        None, description="Indicates if the company's CEO is from a minority ethnic group.")
    board_fill_rate: Optional[float] = Field(
        None, ge=0, le=100, description="Percentage of board seats filled by individuals from minority ethnic groups.")
    executive_fill_rate: Optional[float] = Field(
        None, ge=0, le=100, description="Percentage of executive positions filled by individuals from minority ethnic groups.")


class SpecificMetricsLGBTQSchema(BaseModel):
    """Pre-defined metrics and initiatives related to LGBTQ+ inclusion."""
    model_config = ConfigDict(from_attributes=True)
    non_discrimination_measures: Optional[bool] = Field(
        None, description="Indicates if explicit non-discrimination policies protecting LGBTQ+ individuals are in place.")
    support_initiatives: Optional[List[str]] = Field(
        None, description="A list of specific programs or initiatives supporting LGBTQ+ employees or the community.")


class SpecificMetricsNationalitySchema(BaseModel):
    """NEW: Pre-defined metrics related to national origin and citizenship diversity."""
    model_config = ConfigDict(from_attributes=True)
    overall_non_local_percentage: Optional[float] = Field(
        None, ge=0, le=100, description="Percentage of the total workforce that identifies as non-local or belonging to diverse nationalities different from the primary operating region.")
    executive_diverse_nationalities_percentage: Optional[float] = Field(
        None, ge=0, le=100, description="Percentage of executive positions held by individuals from diverse nationalities.")
    total_unique_nationalities: Optional[int] = Field(
        None, ge=0, description="The count of distinct nationalities represented across the entire workforce.")


class SpecificMetricsSchema(BaseModel):
    """Hardcoded, commonly requested metrics for specific diversity dimensions."""
    model_config = ConfigDict(from_attributes=True)
    gender: Optional[SpecificMetricsGenderSchema] = None
    ethnicity: Optional[SpecificMetricsEthnicitySchema] = None
    lgbtq: Optional[SpecificMetricsLGBTQSchema] = None
    # MODIFIED: Added new nationality schema
    nationality: Optional[SpecificMetricsNationalitySchema] = None


class DimensionSchema(BaseModel):
    """Represents a single diversity dimension and its associated metrics and goals."""
    model_config = ConfigDict(from_attributes=True)
    # MODIFIED: Added 'nationality' to Literal
    dimension_name: Literal["gender", "ethnicity", "LGBTQ+", "age", "neurodiversity", "disability", "socioeconomic",
                            "veteran_status", "religion", "nationality"] = Field(..., description="The name of the diversity dimension.")
    metrics: Optional[List[DimensionMetricSchema]] = Field(
        None, description="A flexible array for reporting various DEI-related metrics for this specific dimension.")
    goals: Optional[List[DimensionGoalSchema]] = Field(
        None, description="Specific, measurable objectives or targets related to this diversity dimension.")
    specific_metrics: Optional[SpecificMetricsSchema] = None


class EmployeeResourceGroupItemSchema(BaseModel):
    """Details for a single Employee Resource Group (ERG)."""
    model_config = ConfigDict(from_attributes=True)
    name: str = Field(..., description="The official name of the ERG.")
    description: Optional[str] = Field(
        None, description="A brief description of the ERG's mission, purpose, or focus.")
    dimension: Optional[str] = Field(
        None, description="The primary diversity dimension or group the ERG serves.")
    # MODIFIED: Changed from int to float to match JSON schema type: "number"
    member_count: Optional[float] = Field(
        None, ge=0, description="The approximate number of active members in the ERG.")
    year_established: Optional[str] = Field(
        None, pattern=r"^\d{4}$", description="The year the ERG was established.")
    initiatives: Optional[List[str]] = Field(
        None, description="Key initiatives, events, or advocacy efforts undertaken by the ERG.")


class EmployeeResourceGroupsSchema(BaseModel):
    """Information on Employee Resource Groups (ERGs) or affinity groups within the company."""
    model_config = ConfigDict(from_attributes=True)
    has_ergs: bool = Field(
        ..., description="Indicates if the company has active Employee Resource Groups (ERGs).")
    groups: Optional[List[EmployeeResourceGroupItemSchema]] = Field(
        None, description="A list of individual ERGs and their details.")


class KeyPersonnelSchema(PersonDetailsSchema):
    """Information about key individuals responsible for or significantly involved in DEI initiatives."""
    exists: bool = Field(...,
                         description="Indicates if this personnel position exists.")
    title: str = Field(..., description="Title or position of the individual.")


class DEIProgramSuccessMetricSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    metric_name: str = Field(...,
                             description="The name of the success metric.")
    target_value: Optional[float] = Field(
        None, description="The desired target value for this metric.")
    current_value: Optional[float] = Field(
        None, description="The current reported value for this metric.")
    unit: Literal["percentage", "count", "ratio", "currency"] = Field(
        ..., description="The unit of measurement for the metric.")


class DEIProgramBudgetSchema(BaseModel):
    """Financial resources allocated to the program."""
    model_config = ConfigDict(from_attributes=True)
    amount: float = Field(..., description="The allocated budget amount.")
    currency: str = Field(..., description="The currency of the budget.")
    period: str = Field(
        ..., description="The period the budget covers (e.g., 'annual', 'project lifetime').")


class DEIProgramSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    program_name: str = Field(..., description="The name of the DEI program.")
    category: Literal["recruitment", "development", "retention", "awareness", "community_outreach", "mentorship",
                      "sponsorship", "other"] = Field(..., description="The primary category or focus area of the program.")
    status: Literal["planned", "active", "completed", "ongoing"] = Field(
        ..., description="The current status or lifecycle stage of the program.")
    start_date: Optional[date] = Field(
        None, description="The start date of the program.")
    end_date: Optional[date] = Field(
        None, description="The projected or actual end date of the program. Omit if ongoing.")
    description: Optional[str] = Field(
        None, description="A detailed description of the program's objectives, activities, and target impact.")
    target_audience: Optional[List[str]] = Field(
        None, description="Specify the intended recipients or group for the program.")
    success_metrics: Optional[List[DEIProgramSuccessMetricSchema]] = Field(
        None, description="Metrics used to evaluate the program's success.")
    budget: Optional[DEIProgramBudgetSchema] = None
    key_outcomes: Optional[List[str]] = Field(
        None, description="Significant results or impacts achieved by the program.")


# Reusable constrained strings
YearOrOngoing = Annotated[str, Field(
    pattern=r"^(?:\d{4}|ongoing)$", description='Four-digit year or "ongoing".')]
YearOnly = Annotated[str, Field(
    pattern=r"^\d{4}$", description="Four-digit year.")]


class StrategicPriorityTimeframeSchema(BaseModel):
    """Allow 'ongoing' where reports don’t give a concrete end year."""
    model_config = ConfigDict(from_attributes=True)
    start_year: Optional[YearOrOngoing] = None
    end_year:   Optional[YearOrOngoing] = None

    @field_validator("start_year", "end_year", mode="before")
    @classmethod
    def coerce_yearish(cls, v):
        # Accept ints or strings containing a year; pass through 'ongoing'
        if v is None:
            return v
        s = str(v).strip().lower()
        if s == "ongoing":
            return "ongoing"
        # extract a 4-digit year if embedded (e.g., "FY2025")
        import re
        m = re.search(r"(19|20)\d{2}", s)
        return m.group(0) if m else v


class StrategicPrioritySchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    priority_name: str = Field(...,
                               description="A concise name for the strategic priority.")
    description: str = Field(
        ..., description="A detailed explanation of the strategic priority, its rationale, and intended impact.")
    timeframe: StrategicPriorityTimeframeSchema
    dimension_focus: Optional[List[Literal["gender", "ethnicity", "LGBTQ+", "age", "neurodiversity", "disability", "socioeconomic", "veteran_status",
                                           "religion", "all"]]] = Field(None, description="Specific diversity dimensions that are the primary focus of this strategic priority.")


class AspirationalGoalBaselineSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    value: Optional[str] = None
    year:  Optional[YearOrOngoing] = None

    @field_validator("value", mode="before")
    @classmethod
    def noneify_nullish(cls, v):
        if isinstance(v, str) and v.strip().lower() in {"null", "n/a", "na", "-", "--", ""}:
            return None
        return v


class AspirationalGoalProgressIndicatorSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    indicator_name: str
    current_value: Optional[str] = None
    target_value: Optional[str] = None
    measurement_frequency: Optional[str] = Field(
        None, description="e.g., annual, quarterly")


class AspirationalGoalSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    goal_name: str = Field(...,
                           description="A concise name for the aspirational goal.")
    description: str = Field(...,
                             description="Detailed explanation of the aspirational goal.")
    # change to allow 'ongoing'
    target_year: YearOrOngoing = Field(...,
                                       description='Target year or "ongoing".')
    target_value: Optional[str] = Field(
        None, description="Desired target outcome/value.")
    baseline: Optional["AspirationalGoalBaselineSchema"] = None
    progress_indicators: Optional[List["AspirationalGoalProgressIndicatorSchema"]] = None
    status: Optional[Literal["not_started",
                             "in_progress", "achieved", "delayed"]] = None

    @field_validator("target_year", mode="before")
    @classmethod
    def coerce_target_year(cls, v):
        if v is None:
            return v
        s = str(v).strip().lower()
        if s == "ongoing":
            return "ongoing"
        import re
        m = re.search(r"(19|20)\d{2}", s)
        return m.group(0) if m else v


class DEIPrioritiesSchema(BaseModel):
    """Strategic direction and forward-looking goals for Diversity, Equity, and Inclusion."""
    model_config = ConfigDict(from_attributes=True)
    strategic_priorities: Optional[List[StrategicPrioritySchema]] = Field(
        None, description="The company's core strategic priorities driving its DEI efforts.")
    aspirational_goals: Optional[List[AspirationalGoalSchema]] = Field(
        None, description="Long-term, ambitious goals that represent the company's vision for DEI.")


class DEIGovernanceSchema(BaseModel):
    """Governance structures and leadership responsible for Diversity, Equity, and Inclusion initiatives."""
    model_config = ConfigDict(from_attributes=True)
    leadership: Optional[DEILeadershipSchema] = None
    training: Optional[DEITrainingSchema] = None
    policies: Optional[DEIPoliciesSchema] = None
    recognition: Optional[RecognitionSchema] = None
    supplier_diversity: Optional[SupplierDiversitySchema] = None


class SocialDataSchema(BaseModel):
    """The main schema for the entire 'social_data' object."""
    model_config = ConfigDict(from_attributes=True)
    dimensions: Optional[List[DimensionSchema]] = Field(
        None, description="Detailed representation of workforce diversity across various dimensions and associated metrics and goals.")
    employee_resource_groups: Optional[EmployeeResourceGroupsSchema] = Field(
        None, description="Information on Employee Resource Groups (ERGs) or affinity groups within the company.")

# Governance Data Schemas 


class GovernanceBodyCompositionPercentageSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    value: float = Field(..., ge=0, le=100)
    year: str = Field(..., pattern=r"^\d{4}$")


class GenderDiversitySchema(BaseModel):
    """Breakdown of gender diversity within the highest governance body."""
    model_config = ConfigDict(from_attributes=True)
    percentage_women: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of women on the highest governance body.")
    percentage_men: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of men on the highest governance body.")
    percentage_other: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of other genders on the highest governance body.")


class AgeDiversitySchema(BaseModel):
    """Breakdown of age diversity within the highest governance body."""
    model_config = ConfigDict(from_attributes=True)
    percentage_under_30: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of governance body members who are under 30 years old.")
    percentage_30_to_50: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of governance body members who are between 30 and 50 years old.")
    percentage_over_50: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of governance body members who are over 50 years old.")


class GovernanceBodyCompositionSchema(BaseModel):
    """Composition of the highest governance body (e.g., Board of Directors) and its committees."""
    model_config = ConfigDict(from_attributes=True)
    committees_description: Optional[str] = Field(
        None, description="Qualitative description of the highest governance body's various committees.")
    percentage_independent_members: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="Percentage of members of the highest governance body who are determined to be independent.")
    gender_diversity: Optional[GenderDiversitySchema] = None
    age_diversity: Optional[AgeDiversitySchema] = None


class WhistleblowingMechanismsSchema(BaseModel):
    """Mechanisms available for employees and stakeholders to raise concerns about unethical or illegal behavior."""
    model_config = ConfigDict(from_attributes=True)
    mechanisms_exist: bool = Field(
        ..., description="Indicates if formal mechanisms for seeking advice and raising concerns exist.")
    description: str = Field(
        ..., description="A description of the whistleblowing/ethics hotline mechanisms.")


class BoardOversightOfESGSchema(BaseModel):
    """Description of the highest governance body's role in overseeing the management of ESG topics."""
    model_config = ConfigDict(from_attributes=True)
    narrative_explanation: str = Field(
        ..., description="A narrative explaining how the board and its committees oversee the company's ESG strategy.")


class AntiCorruptionTrainingSchema(BaseModel):
    """Details on communication and training programs related to anti-corruption policies and procedures."""
    model_config = ConfigDict(from_attributes=True)
    percentage_governance_members_trained: GovernanceBodyCompositionPercentageSchema = Field(
        ..., description="Percentage of governance body members who have received anti-corruption training.")
    percentage_employees_trained: GovernanceBodyCompositionPercentageSchema = Field(
        ..., description="Percentage of all employees who have received anti-corruption training.")
    qualitative_details: Optional[str] = Field(
        None, description="Qualitative details on the content and frequency of anti-corruption training.")


class ExternalAssuranceSchema(BaseModel):
    """Information regarding the external assurance of the sustainability report or specific ESG data points."""
    model_config = ConfigDict(from_attributes=True)
    has_external_assurance: bool = Field(
        ..., description="Indicates if the report or specific data has undergone external assurance.")
    assurance_statement: str = Field(
        ..., description="Reference or link to the external assurance statement.")
    assurance_scope: str = Field(
        ..., description="The defined scope of the assurance engagement (e.g., 'GHG emissions only').")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The year to which the external assurance engagement pertains.")
    assurance_level: str = Field(
        ..., description="The level of assurance provided (e.g., 'Limited Assurance', 'Reasonable Assurance').")


class RemunerationESGLinkSchema(BaseModel):
    """Description of how executive remuneration is linked to the organization's ESG performance."""
    model_config = ConfigDict(from_attributes=True)
    is_linked: bool = Field(
        ..., description="Indicates if executive remuneration includes performance indicators linked to ESG outcomes.")
    description: str = Field(
        ..., description="A detailed description of how sustainability performance metrics are linked to executive compensation.")
    percentage_linked: Optional[GovernanceBodyCompositionPercentageSchema] = Field(
        None, description="The percentage of executive remuneration tied to ESG targets.")


class TaxGovernanceSchema(BaseModel):
    """The organization's approach to tax governance, control, and risk management."""
    model_config = ConfigDict(from_attributes=True)
    narrative_description: str = Field(
        ..., description="A narrative description of the company's tax strategy and governance framework.")


class MaterialityAssessmentProcessSchema(BaseModel):
    """Details on the materiality assessment process and outcome."""
    model_config = ConfigDict(from_attributes=True)
    narrative_description: str = Field(
        ..., description="Narrative description of the materiality assessment process.")


class GovernanceDataSchema(BaseModel):
    """The main schema for the entire 'governance_data' object."""
    model_config = ConfigDict(from_attributes=True)
    company_governance_overview: Optional[str] = Field(
        None, description="A one-paragraph summary of the company's approach to governance.")
    key_governance_metrics: Optional[List[str]] = Field(
        None, description="A bulleted list of key data points the company is tracking in Governance.")
    notable_governance_achievements: Optional[List[str]] = Field(
        None, description="A bulleted list of key achievements in governance.")
    materiality_assessment_process: Optional[MaterialityAssessmentProcessSchema] = None
    governance_body_composition: Optional[GovernanceBodyCompositionSchema] = None
    whistleblowing_mechanisms: Optional[WhistleblowingMechanismsSchema] = None
    board_oversight_of_esg: Optional[BoardOversightOfESGSchema] = None
    anti_corruption_training: Optional[AntiCorruptionTrainingSchema] = None
    external_assurance: Optional[ExternalAssuranceSchema] = None
    remuneration_esg_link: Optional[RemunerationESGLinkSchema] = None
    tax_governance: Optional[TaxGovernanceSchema] = None


# --- Top-Level Schemas ---

class GlobalStandardsSchema(BaseModel):
    """Information on the company's compliance and alignment with various global ESG reporting standards."""
    model_config = ConfigDict(from_attributes=True)
    sasb_compliant: Optional[bool] = Field(
        None, description="Indicates if the company's reporting is compliant with SASB standards.")
    sdg_aligned: Optional[bool] = Field(
        None, description="Indicates if operations are aligned with UN Sustainable Development Goals (SDGs).")
    gri_compliant: Optional[bool] = Field(
        None, description="Indicates if reporting is compliant with GRI standards.")
    csrd_compliant: Optional[bool] = Field(
        None, description="Indicates if reporting is compliant with CSRD requirements.")
    tcfd_compliant: Optional[bool] = Field(
        None, description="Indicates if reporting is compliant with TCFD recommendations.")
    ifrs_aligned: Optional[bool] = Field(
        None, description="Indicates if reporting is aligned with IFRS Sustainability Disclosure Standards.")
    other_standards: Optional[List[str]] = Field(
        None, description="A list of other relevant global or regional ESG standards the company adheres to.")


class DocumentMetadataSchema(BaseModel):
    """Metadata about the reported document itself."""
    model_config = ConfigDict(from_attributes=True)
    company_name: str = Field(...,
                              description="The legal name of the reporting company.")
    document_title: Optional[str] = Field(
        None, description="The official title of the document (e.g., 'Annual Sustainability Report').")
    year: str = Field(..., pattern=r"^\d{4}$",
                      description="The primary reporting year covered by the document.")
    document_type: Literal["report", "filing", "policy"] = Field(
        ..., description="The type or category of the document.")
    environmental_data_reported: bool = Field(
        ..., description="Flag indicating whether environmental data is provided in this report.")
    social_data_reported: bool = Field(
        ..., description="Flag indicating whether social data is provided in this report.")
    governance_data_reported: bool = Field(
        ..., description="Flag indicating whether governance data is provided in this report.")
    global_standards: GlobalStandardsSchema


class MainSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    document_metadata: DocumentMetadataSchema
    environmental_data: Optional[EnvironmentalDataSchema] = None
    social_data: Optional[SocialDataSchema] = None
    governance_data: Optional[GovernanceDataSchema] = None


class ESGReportSchema(BaseModel):
    """
    The root Pydantic model for the entire ESG Reporting JSON object.
    This is the main schema you will use to validate the full JSON payload.
    """
    model_config = ConfigDict(from_attributes=True)
    validation_requirements: Optional[Dict[str, Any]] = None
    required_fields: Optional[List[str]] = None
    schema_: MainSchema = Field(..., alias="schema")

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
