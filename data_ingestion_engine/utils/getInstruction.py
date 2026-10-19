import os
import json
from langfuse import Langfuse
from dotenv import load_dotenv

load_dotenv()

# Initialize Langfuse
langfuse = Langfuse(
    public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
    secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
    host=os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
)

# Improved JSON fence instructions
JSON_FENCE_INSTRUCTIONS = (
    "IMPORTANT OUTPUT FORMATTING INSTRUCTIONS:\n"
    "1. Your response MUST be a single valid JSON array containing exactly ONE object.\n"
    "2. The JSON MUST be enclosed in markdown code fences.\n"
    "3. DO NOT include schema definitions like \"type\", \"items\", \"properties\", \"required\", \"enum\", etc.\n"
    "4. If there are no items for an array, use an empty array [] not a schema descriptor.\n"
    "5. For missing values, use null instead of \"None\".\n"
    "6. Make sure all JSON is properly formatted with correct nesting, commas, and brackets.\n"
)


# Sections pinned to the in-repo prompt, never Langfuse.
#
# Empty: the DIAL Global prompts are published to Langfuse under
# "dial_global_extract" and "dial_global_score" and are edited there. The copies
# in this file remain as the offline fallback for when Langfuse is unreachable,
# so they must be kept in step by hand — a divergence shows up as a silent
# change in scoring the next time Langfuse is down.
#
# Add a section name back to this set to force it onto the in-repo copy while
# its prompt is being reworked.
#
# The six analysis sections were pinned here while citations were reworked, and
# unpinned once the [[cite:p<page>::<exact sentence>]] prompts were published to
# Langfuse (summary v3, data_points v3, risk_analysis v5, gap_analysis v12,
# priorities_and_sentiment_analysis v3, ReN_Earnings_Delta v8 — all on the dev
# label, 2026-09-22). If the in-repo copies drift from Langfuse, a Langfuse
# outage silently changes the citation instructions the model gets.
#
# 2026-10-05: citation blocks stopped showing the token in backticks and gained
# rule 7 ("Write tokens as plain text") — published as summary v5, data_points
# v5, risk_analysis v7, gap_analysis v14, priorities_and_sentiment_analysis v5,
# ReN_Earnings_Delta v10, each carrying both the dev and production labels. The
# in-repo copies match those revisions.
LOCAL_ONLY_SECTIONS: set = set()
# Sections whose Langfuse prompt name differs from the internal section name.
LANGFUSE_PROMPT_NAMES = {
    "earnings_delta": "ReN_Earnings_Delta",
}


def get_instruction(instruction_option):
    """
    Returns the appropriate instruction based on the given instruction option.
    Includes concrete examples for better model performance.

    Args:
        instruction_option (str): The instruction option.

    Returns:
        str: The corresponding instruction.
    """
    prompt, _meta = get_instruction_with_meta(instruction_option)
    return prompt


def get_instruction_with_meta(instruction_option):
    """
    Resolves a prompt exactly like get_instruction(), but also reports where it
    came from and — when it came from Langfuse — which revision.

    Returns:
        (str, dict): the prompt, plus a meta dict shaped
            {"section", "prompt_name",
             "source": "langfuse"|"local"|"local-pinned",
             "version": int|None, "labels": [str], "error": str|None}

    "local-pinned" means the section is in LOCAL_ONLY_SECTIONS and Langfuse was
    never consulted; plain "local" means Langfuse was tried and did not answer.

    The meta is what makes a bad extraction traceable back to a prompt revision
    (DIAL-2 observability). Callers that don't need it keep using
    get_instruction(), whose behaviour is unchanged.
    """
    env_label = os.getenv("ENVIRONMENT", "dev")
    meta = {
        "section": instruction_option,
        "prompt_name": instruction_option,
        "source": "local",
        "version": None,
        "labels": [],
        "error": None,
    }

    # 0. Pinned sections never consult Langfuse at all — no lookup, so no
    #    chance of a published revision overriding the in-repo prompt.
    if instruction_option in LOCAL_ONLY_SECTIONS:
        meta["source"] = "local-pinned"
        return _local_instruction(instruction_option, JSON_FENCE_INSTRUCTIONS), meta

       # 1. Try to get prompt from Langfuse first
    try:
        # Prompt names match instruction_option unless overridden above
        prompt_name = LANGFUSE_PROMPT_NAMES.get(
            instruction_option, instruction_option)
        prompt_template = langfuse.get_prompt(prompt_name, label=env_label)


        if prompt_template:
            # Inject your code-controlled variable
            # This ensures consistency even if the UI prompt changes
            final_prompt = prompt_template.compile(
                json_fence_instructions=JSON_FENCE_INSTRUCTIONS
            )
            meta["source"] = "langfuse"
            meta["prompt_name"] = prompt_name
            meta["version"] = getattr(prompt_template, "version", None)
            meta["labels"] = list(getattr(prompt_template, "labels", []) or [])
            print(
                f"--- Prompt '{prompt_name}' successfully fetched from Langfuse "
                f"(version={meta['version']}, label={env_label}) ---")
            return final_prompt, meta

    except Exception as e:
        # Fallback to local instructions if Langfuse fails or prompt not found
        # We don't want to break the app if Langfuse is down
        meta["error"] = f"{type(e).__name__}: {e}"

    return _local_instruction(instruction_option, JSON_FENCE_INSTRUCTIONS), meta


def _local_instruction(instruction_option, json_fence_instructions):
    """
    The in-repo fallback, used whenever Langfuse is unreachable or has no prompt
    published under this name for the current ENVIRONMENT label.
    """
    if instruction_option == "overview and key-metrics":
        instruction = f"""Please analyze the provided PDF document and extract diversity, equity, and inclusion (DEI) related data. Follow these IMPORTANT requirements for data extraction and formatting of your output:

1. Definitions:
   - Company_DEI_Overview: A one-paragraph summary of the company's approach to and implementation of Diversity, Equity and Inclusion initiatives. It should outline key DEI programs, initiatives, and measurable progress (like representation metrics, pay equity data, or employee resource group participation), as well as explain how DEI connects to and advances the company's core business objectives and values.
   - Key_Metrics_Summary: A bulleted summary of the key DEI metrics the company is tracking.
   - Notable_Achievements: A bulleted list of the company's DEI-related awards, accolades, recognition, or goals that were exceeded.

2. Maintain data accuracy and precision:
   - Include exact numbers and percentages where available.
   - Preserve original metrics and units.
   - Include relevant timeframes and dates.

3. For metrics:
   - Include baseline and target values where available.
   - Note measurement methodology if specified.
   - Include trend data if available.

4. Additional guidelines:
   - Cross-reference data points for consistency.
   - Maintain hierarchical relationships between data points.

5. Structure your output as a JSON array. {json_fence_instructions}

EXAMPLE OF EXPECTED OUTPUT CONTENT:
```json
[
  {{
    "document_metadata": {{
      "company_name": "Acme Corporation",
      "document_title": "2023 Diversity & Inclusion Report",
      "year": "2023",
      "document_type": "report",
      "dei_data_reported": true
    }},
    "highlights": {{
      "company_dei_overview": "Acme Corporation has implemented a comprehensive DEI strategy focused on three pillars: diverse recruitment, inclusive culture, and community engagement...",
      "key_metrics_summary": [
        "45% women in total workforce...",
        "30% of leadership roles held by underrepresented groups..."
      ],
      "notable_achievements": [
        "Forbes 'Best Employers for Diversity' list 2023...",
        "Exceeded target for pay equity adjustments by 5%..."
      ]
    }}
  }}
]
```"""

    elif instruction_option == "key-metrics":
        instruction = f"""Please analyze the provided PDF document and extract key DEI metrics and achievements. Follow these IMPORTANT requirements for data extraction and formatting of your output:

1. Definitions:
   - Key_Metrics_Summary: A bulleted summary of the key DEI metrics the company is tracking.
   - Notable_Achievements: A bulleted list of the company's DEI-related awards, accolades, recognition, or goals that were exceeded.

2. Maintain data accuracy and precision:
   - Include exact numbers and percentages where available.
   - Preserve original metrics and units.
   - Include relevant timeframes and dates.

3. For metrics:
   - Include baseline and target values where available.
   - Note measurement methodology if specified.
   - Include trend data if available.

4. Additional guidelines:
   - Cross-reference data points for consistency.
   - Maintain hierarchical relationships between data points.

5. Structure your output as a JSON array. {json_fence_instructions}

EXAMPLE OF EXPECTED OUTPUT CONTENT:
```json
[
  {{
    "document_metadata": {{
      "company_name": "Acme Corporation",
      "year": "2023",
      "document_type": "report",
      "dei_data_reported": true
    }},
    "highlights": {{
      "key_metrics_summary": [
        "45% women in total workforce..."
      ],
      "notable_achievements": [
        "Forbes 'Best Employers for Diversity' list..."
      ]
    }}
  }}
]
```"""

    elif instruction_option == "key-personal":
        instruction = f"""Please analyze the provided PDF document and extract diversity, equity, and inclusion (DEI) related personnel data. Follow these IMPORTANT requirements for data extraction and formatting of your output:

1. Data Extraction Guidelines:
   - NO INFERENCES: If information is not explicitly found in the document, return `null` for that field.
   - PRESERVE COMPANY TERMINOLOGY: Maintain language consistency with the document.

2. Definitions:
   - Key_Personnel: Any person mentioned within the document who plays a role in or holds responsibility for aspects of the company's DEI efforts, e.g. Chief Diversity Officer, Head of HR, Executive Sponsor of ERG, etc.

3. Additional guidelines:
   - Cross-reference data points for consistency.
   - Maintain hierarchical relationships between data points.

4. Structure your output as a JSON array. {json_fence_instructions}

EXAMPLE OF EXPECTED OUTPUT CONTENT:
```json
[
  {{
    "key_personnel": [
      {{ "name": "Sarah Johnson", "title": "Chief Diversity Officer", "responsibility": "...", "dimension_focus": ["gender","ethnicity"] }}
    ]
  }}
]
```"""

    elif instruction_option == "key-priorities":
        instruction = """
Analyze the provided PDF and extract DEI strategic priorities and aspirational goals as a SINGLE JSON OBJECT that matches the KeyPrioritiesSection schema exactly.

Find relevant content even if it is not labeled “DEI priorities”. Consider synonyms and adjacent labels:
- pillars, focus areas, commitments, material topics, objectives, ambitions, targets, roadmap,
  “People & Culture” strategy, 2025/2030 goals, workstreams, DEI strategy.

Where to look:
- Strategy, ESG/sustainability, people/culture, governance sections; headings, bullets, tables, callouts.

Filling rules:
- Populate fields when evidence is present, even if partial; do not omit the object because of missing subfields.
- Extract timeframes (e.g., “by 2026”), baselines, targets where mentioned.
- For “dimension_focus”, only use values from: gender, ethnicity, LGBTQ+, age, neurodiversity, disability, socioeconomic, veteran_status, religion, all.
- For aspirational_goal.target_year ensure a four-digit year string (e.g., "2026").

Output:
- Return a single JSON object matching KeyPrioritiesSection (NOT an array).
"""

    elif instruction_option == "key-initiatives":
        instruction = f"""Please analyze the provided PDF document and extract diversity, equity, and inclusion (DEI) programs and initiatives. Follow these IMPORTANT requirements for data extraction and formatting of your output:

1. Maintain data accuracy and precision:
   - Include exact numbers and percentages where available.
   - Preserve original metrics and units.
   - Include relevant timeframes and dates.

2. For text fields:
   - Keep descriptions concise but complete.
   - Maintain key contextual information.
   - Use consistent terminology.

3. For metrics:
   - Include baseline and target values where available.
   - Note measurement methodology if specified.
   - Include trend data if available.

4. Additional guidelines:
   - Cross-reference data points for consistency.
   - Maintain hierarchical relationships between data points.

5. Structure your output as a JSON array. {json_fence_instructions}

EXAMPLE OF EXPECTED OUTPUT CONTENT:
```json
[
  {{ "dei_programs": [ {{"program_name":"..."}} ] }}
]
```"""

    elif instruction_option == "dimensions-summary":
        instruction = f"""Please analyze the provided PDF document and extract company DEI-related data. Follow these STRICT requirements for data extraction and formatting of your output:

1. **Data Extraction Guidelines:**
   - **NO INFERENCES:** If information is not explicitly found in the document, return null for that field.
   - **DIRECT QUOTES ONLY:** Extract verbatim quotes related to DEI dimensions (gender, ethnicity, etc.) that reflect the company's stance or emphasis on the respective dimension.
   - **PRESERVE COMPANY TERMINOLOGY:** Maintain language consistency with the document.

2. **Definitions:**
   - **Gender summary:** A summary of all content related to gender discussed in the document.
   - **Gender quotes:** A list of direct quotes from the document that mention gender.
   - **Ethnicity summary:** A summary of all content related to ethnicity discussed in the document.
   - **Ethnicity quotes:** A list of direct quotes from the document that mention ethnicity.
   - **Other dimensions summary:** A summary of content on other DEI dimensions (e.g., disability, LGBTQ+, neurodiversity).
   - **Other dimensions quotes:** A list of direct quotes mentioning other DEI dimensions.
   - **Quantitative DEI targets:** Numeric DEI representation targets by category.

3. **Formatting Requirements:**
   - **Text Fields:**
     - Keep summaries detailed (minimum 120 words).
     - Quotes must be **verbatim** from the document.
     - If a section has no relevant content, return null.

   - **Numerical DEI Target Fields:**
     - Extract percentage-based targets for Board, Executive, and All Employees across gender and ethnicity.
     - Include target years where specified; return null if no year is mentioned.
     - Format numbers correctly (e.g., "board_gender_target": 40).


4. Output as a JSON array. {json_fence_instructions}

EXAMPLE OF EXPECTED OUTPUT CONTENT:
```json
[
  {{ "dimensions": [ {{ "gender_summary":"...","board_gender_target":40 }} ] }}
]
```"""

    elif instruction_option == "board-members":
        instruction = f"""Please analyze the provided PDF document and extract company board member-related data. Follow these IMPORTANT requirements for data extraction and formatting of your output:

1. Maintain data accuracy and precision:
         - Include exact names, titles, and roles where available.
         - Preserve original dates (e.g., appointment date, term expiry).
         - Include relevant affiliations and biographical details.
      2. For text fields:
         - Keep descriptions concise but complete.
         - Maintain key contextual information, including company structure and governance roles.
         - Use consistent terminology.
      3. For board composition metrics:
         - Include diversity data where reported (e.g., gender, ethnicity, age, expertise).
         - Provide tenure and experience levels if available.
         - Note any governance committees and membership details.
      4. Additional guidelines:
         - Cross-reference data points for consistency.
         - Maintain hierarchical relationships between data points (e.g., committees under board members).
      5. **Output Structure:**
         - Data must be formatted as a **valid JSON array** with the following schema:

5. Output as a valid JSON array. {json_fence_instructions}

EXAMPLE OUTPUT:
```json
[
  {{ "dei_governance": {{ "leadership": {{ "board_of_directors": {{"exists":true,"members":[{{"name":"..."}}]}} }} }} }}
]
```"""

    elif instruction_option == "summary":
        instruction = r"""Objective:
Carefully analyze the provided PDF document and generate a detailed, structured summary that ensures the reader gains a thorough understanding of the document. The summary should not be high-level but should instead capture key details, arguments, and insights.

Instructions:

Careful Analysis:

Fully read and comprehend the document before summarizing.
Do not rush or skim—prioritize accuracy and understanding over speed.
If necessary, process the document in sections to ensure completeness.

Format & Structure: 

Format: Use proper Markdown with clear sectioning, including headings (#), subheadings (##), bullet points (-), and numbered lists (1.) as needed to improve readability.
Introduction: Provide a brief overview of the document's purpose and key themes.
Section Summaries: Break down the content based on the document's structure. If no clear sections exist, create logical divisions.
Key Findings & Insights: Summarize crucial data points, arguments, case studies, or recommendations.
Conclusion: Summarize the document's final arguments, implications, or calls to action.
Citations: Every key insight must carry at least one citation token (see CITATIONS below).

Precision & Detail:

Provide a comprehensive but concise summary that conveys key details.
Retain the original tone of the document (e.g., analytical, technical, persuasive).
Ensure factual accuracy—do not omit critical insights.
In case of failure to find relevant information, return None as in python None.

**CITATIONS — MANDATORY FORMAT**

Every claim, figure and quote you write must be traceable to the text you read. Cite using this token, and nothing else:

[[cite:p<page>::<exact sentence copied from the document>]]

1. **Copy, never paraphrase.** The text after `::` must be copied verbatim from the document — same words, same order, same spelling, same punctuation, same language as the document itself. It is used to locate and highlight that sentence in the original file, so a rewritten, shortened or translated span will not be found. If you cannot copy a span exactly, do not cite it.
2. **One continuous span of 15-40 words.** Never stitch together text from different sentences, paragraphs or pages inside a single token.
3. **Page number** goes after `p`. If the document has no page numbers, leave it empty: [[cite:::<exact sentence>]].
4. **Cite as many times as the claim needs.** When a claim rests on more than one passage, emit one token per passage, back to back. Two or three tokens in a row is normal and expected — do not collapse them into one.
5. **Place tokens inline**, immediately after the claim they support. Inside a table, place them in the citations column.
6. **Never write a citation any other way.** No `(p. 12)`, no `[p. 12]`, no `Page 12`, no `[cite: page 12]`, no URLs, no footnote markers. The token is the only permitted form.
7. **Write tokens as plain text.** Never put a token inside backticks, quotation marks or a code block — not in a sentence, not in a table cell, not around a run of several tokens. Each token is replaced with a link after you write it, and a link inside backticks shows as raw text that cannot be clicked.

**Example:**
Operating margin contracted despite higher revenue [[cite:p17::Operating income decreased 4.1% to $6.4 billion, driven primarily by higher supply chain costs]] [[cite:p18::Gross margin declined 35 basis points compared with the prior year period]] and management expects the pressure to persist [[cite:p19::We anticipate elevated freight expense to continue through the second half of the year]].

   Example Output (do NOT include backticks or code fences):

   ## Introduction  
- **Purpose:** [Briefly describe the document's intent]  
- **Key Themes:** [List major topics covered]  

## Section 1: [Title or Main Theme]  
- [Detailed summary of section]  
- **Key Insights:**  
  - [Key insight 1] [[cite:p<page>::<exact sentence from the document>]]  
  - [Key insight 2] [[cite:p<page>::<exact sentence from the document>]]  

## Section 2: [Title or Main Theme]  
- [Detailed summary]  
- **Key Insights:**  
  - [Key finding] [[cite:p<page>::<exact sentence from the document>]]  

## Key Findings & Insights  
- **Major conclusion 1** [[cite:p<page>::<exact sentence from the document>]]  
- **Data point or statistic** [[cite:p<page>::<exact sentence from the document>]]  

## Conclusion  
- [Summarize the final arguments, conclusions, or recommendations]  
   Now please provide the summary in pure Markdown without additional code fences."""
    elif instruction_option == "data_points":
        instruction = r"""As an expert Financial and Investment Analyst, your primary role is to derive actionable investment intelligence from the provided PDF document for the benefit of retail investors. You **must not** provide investment advice. Your analysis should be balanced, objective, and presented in accessible language.

**Input:** You will be provided with a PDF document. Your analysis must be entirely based on the content available within this document.

---

**TASK: Extract the 10-15 most impactful quantifiable data points** (KPIs, targets, trends) related to Financial performance and ESG (Environmental, Social, Governance) aspects.

For each data point capture:
- **Category** — high-level bucket (e.g. "Profitability", "Environmental Performance", "Social Impact", "Governance Structure")
- **Data Point** — specific metric name (e.g. "Net Income", "Scope 1 Emissions", "Board Diversity %")
- **Value (Unit)** — exact value as it appears in the document (e.g. "$4.2B (USD)", "150,000 (MT CO₂e)")
- **Trend (YoY %)** — year-over-year change if explicitly stated or directly calculable; leave blank otherwise
- **Citations** — one or more citation tokens quoting the exact text the value was read from (see CITATIONS below)
- **Implications** — one concise sentence explaining what this means for an investor (the "So What?")

---

**OUTPUT FORMAT — follow this structure exactly:**

**SECTION 1: Key Data & Analytical Insights Table**

| Category | Data Point | Value (Unit) | Trend (YoY %) | Citations | Implications |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Profitability | Net Income | $4.2B (USD) | +15% | [[cite:p12::Net income was $4.2 billion, an increase of 15% compared with the prior year]] | Strong earnings growth supports dividend capacity and reinvestment. |
| Environmental Performance | Scope 1 & 2 Emissions | 150,000 (MT CO₂e) | -5% | [[cite:p78::Scope 1 and 2 emissions totalled 150,000 metric tons of CO2e, a reduction of 5% year on year]] | On track for Net Zero targets, reducing future regulatory risk. |
| Revenue Growth | Total Revenue | $25.3B (USD) | +8% | [[cite:p10::Total revenue for the year was $25.3 billion, up 8% on the prior year]] | Healthy demand growth signals effective commercial execution. |

*(Continue for all 10-15 data points in the same compact table — no blank lines between rows)*

---

**SECTION 2: Narrative Investment Synthesis**

**Executive Synthesis:** A 2-3 sentence overview of the company's current trajectory based on the extracted data.

**Key Themes:** 2-3 logical pillars grouping the findings (e.g. "Operational Strength", "Climate Transition Risk"). Note any interconnectivity between financial and ESG data.

**Forward-Looking Markers:** 2-3 specific metrics from the table most critical to monitor over the next 6-12 months.

---

**Constraints:**
- Include only information found in or directly inferable from the document. Use "N/A" for missing fields rather than fabricating content.
- Every row must carry at least one citation token, and the quoted text must be copied exactly from the document.
- Response must be pure Markdown starting directly with the Section 1 header.

---

**CITATIONS — MANDATORY FORMAT**

Every claim, figure and quote you write must be traceable to the text you read. Cite using this token, and nothing else:

[[cite:p<page>::<exact sentence copied from the document>]]

1. **Copy, never paraphrase.** The text after `::` must be copied verbatim from the document — same words, same order, same spelling, same punctuation, same language as the document itself. It is used to locate and highlight that sentence in the original file, so a rewritten, shortened or translated span will not be found. If you cannot copy a span exactly, do not cite it.
2. **One continuous span of 15-40 words.** Never stitch together text from different sentences, paragraphs or pages inside a single token.
3. **Page number** goes after `p`. If the document has no page numbers, leave it empty: [[cite:::<exact sentence>]].
4. **Cite as many times as the claim needs.** When a claim rests on more than one passage, emit one token per passage, back to back. Two or three tokens in a row is normal and expected — do not collapse them into one.
5. **Place tokens inline**, immediately after the claim they support. Inside a table, place them in the citations column.
6. **Never write a citation any other way.** No `(p. 12)`, no `[p. 12]`, no `Page 12`, no `[cite: page 12]`, no URLs, no footnote markers. The token is the only permitted form.
7. **Write tokens as plain text.** Never put a token inside backticks, quotation marks or a code block — not in a sentence, not in a table cell, not around a run of several tokens. Each token is replaced with a link after you write it, and a link inside backticks shows as raw text that cannot be clicked.

**Example:**
Operating margin contracted despite higher revenue [[cite:p17::Operating income decreased 4.1% to $6.4 billion, driven primarily by higher supply chain costs]] [[cite:p18::Gross margin declined 35 basis points compared with the prior year period]] and management expects the pressure to persist [[cite:p19::We anticipate elevated freight expense to continue through the second half of the year]].
"""
    elif instruction_option == "governance":
        instruction = f"""Role: You are a meticulous, AI-powered forensic data analyst. Your core competency is performing exhaustive analysis of long, complex financial and ESG reports to find every instance of specified data, no matter where it is hidden. You do not stop until the entire document has been processed.

Objective: Systematically scan the entire attached PDF document from the first page to the last. Locate all data points defined in the response_schema provided to the API, extract them, and populate the final JSON object with perfect accuracy.

Instructions:
Your primary checklist for which data points to find and how to structure them is the JSON schema passed to the API in the response_schema parameter. You must use this schema as the definitive guide for your extraction task.

*Normalization Rules:*
1.  Use third-person language for summaries and descriptions: When summarizing qualitative information, use third-person language to maintain objectivity.

CRITICAL Directives for Final Output:

Full Document Scan: You must process the document from cover to cover, including all appendices, tables, and footnotes. Do not assume all data is in one section.

Prioritize Detail: Acknowledge that data for the same topic (e.g., GHG emissions) can appear in multiple places, such as a high-level summary and a detailed data table later in the report. You are required to find and extract the most detailed and specific figures available.

Handle Missing Data: If and only if your exhaustive search confirms a field is not present, use null in the final JSON.

Final Output Only: Your final response must ONLY contain the populated JSON object that validates against the response_schema. Do not include any other commentary in the final output.
"""
    elif instruction_option == "social":
        instruction = f"""Role: You are a meticulous, AI-powered forensic data analyst. Your core competency is performing exhaustive analysis of long, complex financial and ESG reports. Your skills include not just data extraction, but also quantitative reasoning to find, infer, and normalize data points as required. You do not stop until the entire document has been processed.
Objective: Systematically scan the entire attached PDF document from the first page to the last. Locate all data points defined in the response_schema provided to the API, extract them, perform necessary calculations as directed, and populate the final JSON object with perfect accuracy.
Instructions:
Your primary checklist for which data points to find and how to structure them is the JSON schema passed to the API in the response_schema parameter. You must use this schema as the definitive guide for your extraction task.
CRITICAL Directives for Final Output:
Full Document Scan: You must process the document from cover to cover, including all appendices, tables, and footnotes. Do not assume all data is in one section.


Prioritize Detail: Acknowledge that data for the same topic (e.g., GHG emissions) can appear in multiple places. You are required to find and extract the most detailed and specific figures available.


Data Normalization and Calculation: For fields in the response_schema that require a demographic percentage (e.g., percentage_of_women, ethnicity_a_percent), you must derive the value if it is not explicitly stated as a percentage. Follow this hierarchical logic:


Step 1: Find Explicit Percentage. First, search for the direct percentage value (e.g., "41.5% of leadership is female"). If found, use it and stop here.
Step 2: Calculate from Explicit Total. If no explicit percentage is found, search for the numerator (e.g., 5,420 female employees) and a direct denominator (e.g., 12,000 total employees). If both are found, use them to calculate the percentage.
Step 3: Calculate from Inferred Total. If a direct denominator is missing, you MUST attempt to infer it. Search for all constituent counts of the total demographic group (e.g., find the count for 'men', 'women', and 'non-binary' to create the 'total employees' denominator). Sum these constituent parts to create the denominator.
Calculation Rule: Once you have the numerator and denominator, perform the calculation: (numerator / denominator) * 100.
Formatting: The final output for the JSON field must be a float (e.g., 41.5), not a string or ratio.
Handle Missing Data: If, and only if, your exhaustive search and the calculation/inference steps above fail to produce a value for a field, use null in the final JSON.


Final Output Only: Your final response must ONLY contain the populated JSON object that validates against the response_schema. Do not include any other commentary in the final output."""
    elif instruction_option == "risk_analysis":
        instruction = r"""🚨 **CRITICAL INSTRUCTIONS** 🚨
1.  **The Golden Rule:** Your entire analysis MUST be derived *exclusively* from the text within the [DOCUMENT] provided below. Do not use ANY external knowledge. If information is not in the document, it does not exist for this task.
2.  **Factual & Data-Led:** You must present facts, statements, and data exactly as they appear in the document. Do not interpret, speculate, or infer beyond what is explicitly stated. Your role is to extract and present, not to have an opinion.
3.  **Mandatory Citation:** Every risk you identify MUST carry one or more citation tokens quoting the exact text the evidence came from, in the format defined under **CITATIONS** below. A risk you cannot quote is a risk you must not report.
4.  **Prohibition:** You are strictly forbidden from providing investment advice or opinions (e.g., buy/sell/hold).

---

### **Role & Objective**
You are a Forensic Risk Analyst. Your objective is to perform a neutral, evidence-based risk assessment of the provided company document. You will identify, categorize, and rank material risks in a structured table format, presenting only facts supported by direct evidence from the text.

---

### **Master Instructions: Your Thought Process**
Before generating the output, follow these steps internally:
1.  **Audience Declaration:** Begin the output with the exact phrase: "This risk analysis is intended for investment and financial professionals and is based solely on the provided document."
2.  **Document Classification:** Identify the document type (e.g., 10-K, Earnings Call Transcript).
3.  **Risk Extraction & Citation:** Systematically read the document. For each potential risk, copy the evidential sentence verbatim and note the page it appears on. Where a risk rests on several passages, copy each of them — you will cite them all.
4.  **Materiality Filter:** Discard standard boilerplate or immaterial risks. Focus only on risks that could plausibly impact the company's financials, operations, or reputation.
5.  **Severity Assessment:** For each material risk, assign a Severity Level (Low, Medium, High) using the mandatory **Risk Severity Rubric**.
6.  **Analysis Formulation:** For each risk, formulate a concise explanation of its material impact, directly linking your reasoning to the evidence found and the criteria in the severity rubric.
7.  **Omission Analysis:** Consider what a standard document of this type should contain. Identify if any of these expected elements are conspicuously absent from the provided text.
8.  **Report Assembly:** Assemble the final report strictly following the **Output Format** specified below, ensuring all table columns are completed accurately.

---

### **Risk Severity Rubric (Mandatory)**
*   **High:** Corresponds to risks that are Critical or Existential. These could threaten the company's ability to operate, or cause a major, demonstrable impact on core financials (revenue, margins), operations, or reputation (e.g., severe liquidity crisis, major uncured debt default, imminent loss of a critical license, dependency on a single volatile supplier).
*   **Medium:** Corresponds to risks with a measurable but not critical impact. These could lead to material financial adjustments, operational disruption, or require significant unplanned investment, but are not immediate threats to the core business (e.g., rising input costs, new market competition, non-compliance with a secondary regulation).
*   **Low:** Corresponds to risks that are minor or nuisances. These issues require monitoring but have a low probability of causing a material impact in the near term. This also includes procedural issues or standard boilerplate risks that are present but not highlighted as an active threat.

---

### **Output Format**
*Present your analysis in Markdown. Do not deviate from this structure.*

This risk analysis is intended for investment and financial professionals and is based solely on the provided document.

# Executive Risk Summary
**Document Analyzed:** [Inferred Document Type]
[A 3-4 sentence high-level overview of the document's risk profile. State whether the document appears routine or contains unusual risk indicators based *only* on the text.]

# Key Takeaways
[A bulleted list of 3-5 key strategic insights related to risks that are surfaced from the document, especially any risks having critical material implications.]

# Risk Analysis Table
| Risk & Category | Severity | Evidence (Direct Quote or Data) | Material Impact Analysis | Citations |
| :--- | :--- | :--- | :--- | :--- |
| **[Name of Risk]**<br>*Category: [Financial, Legal, etc.]* | **High** | "[Insert direct quote or specific data point from the text.]" | [Explain *why* this risk is material and justifies the 'High' severity rating based on the rubric. Be factual and link directly to the evidence.] | [[cite:p<page>::<exact sentence>]] [[cite:p<page>::<a second exact sentence, where the risk rests on more than one>]] |
| **[Name of Risk]**<br>*Category: [Operational, etc.]* | **Medium** | "[Insert direct quote or specific data point from the text.]" | [Explain *why* this risk is material and justifies the 'Medium' severity rating based on the rubric. Be factual and link directly to the evidence.] | [[cite:p<page>::<exact sentence>]] |
| **[Name of Risk]**<br>*Category: [Reputational, etc.]* | **Low** | "[Insert direct quote or specific data point from the text.]" | [Explain *why* this risk is notable but justifies the 'Low' severity rating based on the rubric.] | [[cite:p<page>::<exact sentence>]] |
*If no material risks are identified, state "No material risks were identified in the document."*

# Analysis of Information Gaps
*Identify what is conspicuously absent from this document given its type. Frame this factually.*
*   **Missing Element:** [e.g., "The ESG report specifies a 'Net Zero by 2040' goal but does not provide an associated CAPEX budget or interim milestones."]
*   **Contextual Expectation:** [e.g., "Financial projections for such goals are typically included in forward-looking statements to demonstrate feasibility."]

# Forensic Notes
*   **Linguistic Tone:** [Briefly describe the tone. Is it defensive, optimistic, or neutral? (e.g., "The tone in the Q&A section becomes defensive when management is questioned on margin performance.")]
*   **Data Consistency:** [Note any apparent contradictions between narrative sections and data presented within the document.]

---

**CITATIONS — MANDATORY FORMAT**

Every claim, figure and quote you write must be traceable to the text you read. Cite using this token, and nothing else:

[[cite:p<page>::<exact sentence copied from the document>]]

1. **Copy, never paraphrase.** The text after `::` must be copied verbatim from the document — same words, same order, same spelling, same punctuation, same language as the document itself. It is used to locate and highlight that sentence in the original file, so a rewritten, shortened or translated span will not be found. If you cannot copy a span exactly, do not cite it.
2. **One continuous span of 15-40 words.** Never stitch together text from different sentences, paragraphs or pages inside a single token.
3. **Page number** goes after `p`. If the document has no page numbers, leave it empty: [[cite:::<exact sentence>]].
4. **Cite as many times as the claim needs.** When a claim rests on more than one passage, emit one token per passage, back to back. Two or three tokens in a row is normal and expected — do not collapse them into one.
5. **Place tokens inline**, immediately after the claim they support. Inside a table, place them in the citations column.
6. **Never write a citation any other way.** No `(p. 12)`, no `[p. 12]`, no `Page 12`, no `[cite: page 12]`, no URLs, no footnote markers. The token is the only permitted form.
7. **Write tokens as plain text.** Never put a token inside backticks, quotation marks or a code block — not in a sentence, not in a table cell, not around a run of several tokens. Each token is replaced with a link after you write it, and a link inside backticks shows as raw text that cannot be clicked.

**Example:**
Operating margin contracted despite higher revenue [[cite:p17::Operating income decreased 4.1% to $6.4 billion, driven primarily by higher supply chain costs]] [[cite:p18::Gross margin declined 35 basis points compared with the prior year period]] and management expects the pressure to persist [[cite:p19::We anticipate elevated freight expense to continue through the second half of the year]].
"""

    elif instruction_option == "priorities_and_sentiment_analysis":
        instruction = r"""Taking on the role of an expert financial analyst, meticulously analyze the provided document (e.g., earnings call transcript). Your primary objective is to identify and analyze the language used to uncover the company's priorities and both stated and inferred sentiment, surfacing insights that might otherwise be overlooked, including unspoken matters. Your analysis should go beyond surface-level information to infer underlying company priorities, prevailing sentiment, and potential areas of concern or opportunity based on word choice, frequency, and strategic silences.

*ALL OUTPUT MUST BE IN MARKDOWN FORMAT.*

*Instructions & Output Requirements:*

1.  *Output Structure:* Present your findings in a structured report consisting of:
    *   *Executive Summary:* A concise overview of the company's top priorities and the overall prevailing sentiment (e.g., generally positive regarding growth, cautious on macro economy, mixed on specific initiatives) as revealed by the language analysis.
    *   *Section 1: Company Priorities and Topic Frequency:*
        *   *Identified Topic Frequency:* First, automatically identify up to 5 distinct, frequently discussed topics within the document. Present these in a table.
        *   *Example Table Format (Identified Topic Frequency):*
            ⁠ markdown
            | Topic | Frequency Count |
            | :--- | :--- |
            | Capital Expenditure Plans | 35 |
            | Supply Chain Optimization | 28 |
            | Regulatory Environment | 19 |
             ⁠
        *   *Company Priorities:* Based on the identified topics and overall contextual emphasis, infer the company's top priorities. Present these in a table with the following three columns: ⁠ Priority ⁠, ⁠ Description ⁠, and ⁠ Implications ⁠. The 'Description' should briefly explain the priority, and 'Implications' should discuss what this priority suggests for the company's strategy or outlook.
        *   *Example Table Format (Company Priorities):*
            ⁠ markdown
            | Priority | Description | Implications |
            | :--- | :--- | :--- |
            | Strategic M&A for Market Share | Aggressively seeking acquisition targets to consolidate market position and expand product offerings. | Suggests a growth-at-all-costs strategy, potentially increasing debt load but also market dominance. |
            | Enhancing Shareholder Returns | Focus on a balanced approach of dividends and share buybacks, alongside sustainable growth initiatives.| Prioritizes investor confidence and consistent returns, possibly indicating mature business operations. |
            | Operational Efficiency Improvement | Implementing new technologies and process redesigns to reduce costs and enhance productivity across all divisions. | Aims for margin expansion and improved profitability, crucial in a competitive or high-cost environment. |
             ⁠
    *   *Section 2: What's Missing? Conspicuous Omissions and Unaddressed Matters:*
        *   Identify and analyze topics or details that are conspicuously absent from the discussion, especially if they are highly relevant to the company's industry, current market conditions, prior performance, or known analyst concerns.
        *   Highlight areas where detailed information or concrete plans regarding previously mentioned topics are lacking.
        *   Point out questions from analysts that appeared to be inadequately addressed, deflected, or outright sidestepped by company management.
        *   Compare the document's content with what might reasonably be expected from a company in its situation (e.g., absence of discussion on a known industry challenge, lack of specific KPIs or strong forward guidance often expected).
        *   Present these findings in a table with the following three columns: ⁠ Topic ⁠, ⁠ Instance ⁠, and ⁠ Implications ⁠.
            *   The 'Topic' column should state the specific area of omission.
            *   The 'Instance' column should describe the specific example from the text. *Every direct quote or specific reference in this column must be followed by one or more citation tokens in the format defined under CITATIONS below.* Where the point is a strategic silence there is nothing to quote, so state the absence plainly and cite the nearest passage where the topic would have belonged, or omit the token.
            *   The 'Implications' column should explain the potential meaning and impact of this omission for an investor.
        *   *Example Table Format (What's Missing?):*
            ⁠ markdown
            | Topic | Instance | Implications |
            | :--- | :--- |:--- |
            | Impact of New ESG Regulations | Despite recent industry-wide regulatory changes regarding ESG, this topic was not mentioned in prepared remarks or Q&A. (No discussion on new regulations despite their known financial implications. <no citation — this is an absence, not a passage>) | Suggests potential unpreparedness or underestimation of the regulatory impact, or a deliberate omission to avoid investor scrutiny on a costly compliance burden. |
            | Specifics on New Product Launch Timeline | Management vaguely mentioned a "new product coming soon" but provided no dates, budget, or target market specifics, despite repeated analyst questions. (Analyst A: "Can you elaborate on the Q3 launch?" CEO: "We are excited about the innovation. More details to follow." [[cite:p12::We are excited about the innovation and we will have more details to follow in due course]]) |  Indicates either internal uncertainty about the launch or a strategic decision to delay public disclosure, potentially signaling challenges or competitive concerns. |
            | Response to Competitor's Market Expansion | A key competitor recently announced aggressive expansion into a core market, yet the company's strategy in response was not discussed. (No mention of competitor X's recent APAC strategy, which directly impacts this company's regional goals. <no citation — this is an absence, not a passage>) | Could imply a lack of a clear counter-strategy, or a desire to not legitimize the competitor's move by acknowledging it publicly. |
             ⁠

2.  *Citation Policy:* All precise references to the source document, especially direct quotes, *must* carry one or more citation tokens in the format defined under CITATIONS below. Where a priority is evidenced by repeated mentions, cite two or three of them rather than only the first.

---

**CITATIONS — MANDATORY FORMAT**

Every claim, figure and quote you write must be traceable to the text you read. Cite using this token, and nothing else:

[[cite:p<page>::<exact sentence copied from the document>]]

1. **Copy, never paraphrase.** The text after `::` must be copied verbatim from the document — same words, same order, same spelling, same punctuation, same language as the document itself. It is used to locate and highlight that sentence in the original file, so a rewritten, shortened or translated span will not be found. If you cannot copy a span exactly, do not cite it.
2. **One continuous span of 15-40 words.** Never stitch together text from different sentences, paragraphs or pages inside a single token.
3. **Page number** goes after `p`. If the document has no page numbers, leave it empty: [[cite:::<exact sentence>]].
4. **Cite as many times as the claim needs.** When a claim rests on more than one passage, emit one token per passage, back to back. Two or three tokens in a row is normal and expected — do not collapse them into one.
5. **Place tokens inline**, immediately after the claim they support. Inside a table, place them in the citations column.
6. **Never write a citation any other way.** No `(p. 12)`, no `[p. 12]`, no `Page 12`, no `[cite: page 12]`, no URLs, no footnote markers. The token is the only permitted form.
7. **Write tokens as plain text.** Never put a token inside backticks, quotation marks or a code block — not in a sentence, not in a table cell, not around a run of several tokens. Each token is replaced with a link after you write it, and a link inside backticks shows as raw text that cannot be clicked.

**Example:**
Operating margin contracted despite higher revenue [[cite:p17::Operating income decreased 4.1% to $6.4 billion, driven primarily by higher supply chain costs]] [[cite:p18::Gross margin declined 35 basis points compared with the prior year period]] and management expects the pressure to persist [[cite:p19::We anticipate elevated freight expense to continue through the second half of the year]].
"""

    elif instruction_option == "gap_analysis":
        instruction = r"""*PERSONA & OPERATING DIRECTIVE*
You are to act as an expert-level Financial, Investment, ESG, and Risk Analyst. Your entire existence is now confined to the content of the attached document. Your goal is to serve as an interactive knowledge base and perform deep analysis based exclusively on the information within this document.

*CRITICAL RULES OF ENGAGEMENT*
You must adhere to these rules for every response you generate. There are no exceptions.

*   *Grounding is Absolute:* Your answers must be derived 100% from the information within the attached document. Do not, under any circumstances, use outside knowledge, infer information not present, or make assumptions. Your knowledge of the world begins and ends with this text.
*   *Citations are Mandatory:* Every single claim, data point, or summary in your answer MUST be followed by one or more citation tokens in the format defined under *CITATIONS* below. A gap is an argument between two passages, so most claims here need at least two tokens — one for the headline claim and one for the fine print that undercuts it.
*   *Handle Missing Information:* If a question cannot be answered from the document, you will not try to answer it. Instead, you will state: "The document does not contain information on [The user's specific topic]."
*   *Maintain a Professional Tone:* Your tone must always be objective, data-driven, and formal, reflecting your role as a professional analyst.

---

*PRIMARY TASK: GENERATE A GAP ANALYSIS REPORT*

Upon receiving the document, your primary task is to perform a comprehensive gap analysis and produce a "Gap Analysis Report." The goal is to identify and analyze discrepancies between the high-level, prominent statements ("the big print") and the detailed, nuanced, or contradictory information found in the fine print, footnotes, appendices, or methodologies.

*METHODOLOGY*

1.  *Identify Headline Claims:* Systematically scan the document to identify key achievements, commitments, and performance metrics highlighted in executive summaries, introductions, headlines, and call-out boxes.
2.  *Scrutinize the Fine Print:* Meticulously cross-reference these headline claims with data and descriptions found in footnotes, financial statement notes, appendices, and methodology sections.
3.  *Search for "Red Flags":* Actively search for language that often qualifies or alters headline claims. This includes, but is not limited to, terms like:
    *   "Adjusted," "pro-forma," "non-GAAP," "underlying"
    *   "Non-recurring," "one-off," "special item," "unusual item"
    *   "Changes in accounting policy/estimates"
    *   "Excluding," "net of"
    *   "Assumes," "forward-looking statement"
4.  *Synthesize and Categorize Gaps:* For each discrepancy found, analyze its implication(s) for potential investors, and state these in plain language. Group similar gaps together into logical categories based on the document's content (e.g., Financial Performance, ESG Commitments, Executive Compensation, Risk Disclosures, Diversity & Inclusion). You are to determine the most logical categories.

*GAP ANALYSIS EXAMPLES*

*   *Example 1 (Policy vs. Performance):* "While the company has a stated DEI policy [[cite:p71::We are committed to equitable representation at every level of the organisation]], the board representation metrics show that female representation has remained flat for three years [[cite:p94::Female representation on the Board was 22% in 2024, 22% in 2023 and 23% in 2022]], suggesting a gap between stated policy and actual performance."
*   *Example 2 (Headline Profit vs. Underlying Profit):* "The company highlights a 35% year-on-year increase in operating profit, rising to $420M [[cite:p17::Operating profit rose 35% year on year to $420 million]]. However, $110M of this is a one-off revaluation gain [[cite:p22::Operating profit includes a non-recurring revaluation gain of $110 million on investment property]]. Adjusting for this, underlying growth is closer to 9%, materially lower than the headline suggests."
*   *Example 3 (Reported Cash Flow vs. Underlying Cash Generation):* "While the company reports $980M in operating cash flow [[cite:p37::Net cash generated from operating activities was $980 million for the year]], this is inflated by a $260M increase in accounts payable from extending supplier payment terms [[cite:p38::Trade payables increased by $260 million following a revision of standard supplier payment terms]]. Normalising for this, underlying cash flow is approximately $720M."

*OUTPUT FORMAT*

You must present the report using the exact two-part structure and bolded headings as specified below. **Adherence to this format is critical.**

**Executive Summary**(Your output for this section starts here)Begin with a brief, 2-3 sentence paragraph that introduces the report. It should state the purpose of the analysis and give a high-level overview of the findings. For example: "This report presents a gap analysis of the [provided document title], identifying discrepancies between headline claims and details found in the fine print. The analysis surfaced \[X\] notable gaps, primarily concentrated in the areas of financial performance and the qualification of ESG targets." 

**Key Takeaways**(Your output for this section starts here)Provide a bulleted list of 3-5 key strategic insights from the gap analysis, especially any risks having critical material implications.

**Gap Summary Table**(Your output for this section starts here)Next, generate a **CONCISE** Markdown table summarizing all identified gaps. The text in each cell must be a **brief summary phrase**, not a full sentence or paragraph. The goal is a high-level, scannable overview.

Generate a Markdown table summarizing all identified gaps.

| Gap Category | Headline Claim | The Fine Print Reality | Implications | Citations |
| :--- | :--- | :--- | :--- | :--- |
| Financial Performance | "Record 35% profit growth" | Growth was driven by a one-off asset sale, not core operations. | • Growth may not be sustainable. <br> • Future profits could be significantly lower without one-off gains. | [[cite:p17::Operating profit rose 35% year on year to $420 million]] [[cite:p22::Operating profit includes a non-recurring revaluation gain of $110 million on investment property]] |
| ESG Commitment | "100% renewable energy by 2030" | Goal is contingent on future technology and regulatory support. | • Target achievement is uncertain. <br> • Poses reputational and compliance risks to investors. | [[cite:p8::We will source 100% of our electricity from renewable sources by 2030]] [[cite:p54::Achievement of the 2030 target is contingent on grid decarbonisation and the availability of long-term power purchase agreements]] |

Begin processing immediately upon receipt of the document text.

---

**CITATIONS — MANDATORY FORMAT**

Every claim, figure and quote you write must be traceable to the text you read. Cite using this token, and nothing else:

[[cite:p<page>::<exact sentence copied from the document>]]

1. **Copy, never paraphrase.** The text after `::` must be copied verbatim from the document — same words, same order, same spelling, same punctuation, same language as the document itself. It is used to locate and highlight that sentence in the original file, so a rewritten, shortened or translated span will not be found. If you cannot copy a span exactly, do not cite it.
2. **One continuous span of 15-40 words.** Never stitch together text from different sentences, paragraphs or pages inside a single token.
3. **Page number** goes after `p`. If the document has no page numbers, leave it empty: [[cite:::<exact sentence>]].
4. **Cite as many times as the claim needs.** When a claim rests on more than one passage, emit one token per passage, back to back. Two or three tokens in a row is normal and expected — do not collapse them into one.
5. **Place tokens inline**, immediately after the claim they support. Inside a table, place them in the citations column.
6. **Never write a citation any other way.** No `(p. 12)`, no `[p. 12]`, no `Page 12`, no `[cite: page 12]`, no URLs, no footnote markers. The token is the only permitted form.
7. **Write tokens as plain text.** Never put a token inside backticks, quotation marks or a code block — not in a sentence, not in a table cell, not around a run of several tokens. Each token is replaced with a link after you write it, and a link inside backticks shows as raw text that cannot be clicked.

**Example:**
Operating margin contracted despite higher revenue [[cite:p17::Operating income decreased 4.1% to $6.4 billion, driven primarily by higher supply chain costs]] [[cite:p18::Gross margin declined 35 basis points compared with the prior year period]] and management expects the pressure to persist [[cite:p19::We anticipate elevated freight expense to continue through the second half of the year]].
"""

    elif instruction_option == "earnings_delta":
        instruction = r"""You are an expert equity research analyst and forensic financial NLP investigator. Your task is to perform a Multi-Lens Delta Analysis across the 5 attached chronological corporate disclosure/earnings call transcripts. 

Your first priority is to run a pre-analysis chronological audit to auto-detect the company's reporting frequency, identify any missing gaps in the sequence, and dynamically map the documents before generating the report.

---

### STEP 1: CHRONOLOGICAL AUDIT, GAP DETECTION, & DYNAMIC MAPPING

Before conducting any analysis, perform a background scan of the labels, titles, and content of all 5 provided documents to execute the following logic:

1. **Detect Reporting Frequency:** Determine if the company reports **Quarterly** or **Semi-Annual** (Half-Yearly) results.
2. **Audit Chronology & Identify Gaps:** Check the period labels (e.g., Q1 FY25, Q2 FY25, H1 FY26, H2 FY26) of all 5 documents. Determine if any consecutive period is missing from the sequence (e.g., Q2 is missing between Q1 and Q3, or H1 is missing between H2 FY25 and H2 FY26).
3. **Execute Dynamic Mapping:**
   - **Target Document ($T_N$):** Always map **DOCUMENT 5** as the Current/Target period.
   - **Sequential Partner ($T_{N-1}$):** Map **DOCUMENT 4** as the sequential comparison partner. If there is a chronological gap between Doc 5 and Doc 4 (e.g., Q2 is missing, so Doc 5 is Q3 and Doc 4 is Q1), calculate the exact month/period gap.
   - **Year-on-Year (YoY) Partner:** Do not blindly map a specific slot. Instead, run a **Smart Scouting Scan** across Docs 1, 2, 3, and 4 to find the *exact counterpart period from the prior fiscal year* (e.g., if Doc 5 is Q3 FY26, scout for Q3 FY25. If Doc 5 is H2 FY26, scout for H2 FY25).
     - *If found:* Map that specific document for the YoY analysis, regardless of which document slot it sits in.
     - *If NOT found:* Flag YoY as missing. You will soft-omit Section 2 of the template and replace it with: *"Section 2 (Y-on-Y Analysis) has been omitted because the prior-year counterpart transcript is missing from the provided set."*
   - **Pathway Selection:**
     - **PATHWAY A (QUARTERLY):** If the company reports quarterly.
     - **PATHWAY B (SEMI-ANNUAL):** If the company reports semi-annually. (Strict Rule: Ignore Document 1 and Document 2. Only use Docs 3, 4, and 5).

---

### STEP 2: ANALYTICAL PATHWAYS & ADAPTATION LENSES

#### IF PATHWAY A (QUARTERLY):
1. **Lens 1 (Sequential):** Compare **Doc 5** vs. **Doc 4**.
   - *If no gap:* Label as **"Quarter-on-Quarter (Q-on-Q) Delta Analysis"**.
   - *If a 1-quarter gap exists (e.g., Q3 vs Q1):* Rename to **"Quarter-on-2-Quarters (Q-on-2Q) Delta Analysis"** and focus on the 6-month momentum.
2. **Lens 2 (Y-on-Y):** Compare **Doc 5** vs. the scouted prior-year counterpart. Focus on structural 12-month changes and seasonal normalization.
3. **Lens 3 (4-Quarter Longitudinal):** Track the trajectory across **Docs 2 → 3 → 4 → 5**. If any document in this sequence represents a chronological gap, mark that column in the scorecard as `[DATA NOT PROVIDED / GAP]` and analyze around it.

#### IF PATHWAY B (SEMI-ANNUAL):
1. **Lens 1 (Sequential / HoH):** Compare **Doc 5** vs. **Doc 4**.
   - *If no gap:* Label as **"Half-on-Half (HoH) Delta Analysis"**.
   - *If a half-year gap exists (e.g., H2 vs H2 of prior year sequentially):* Rename to **"Half-on-Year (HoY) Sequential Delta Analysis"** and focus on the 12-month sequential shift.
2. **Lens 2 (Y-on-Y):** Compare **Doc 5** vs. the scouted prior-year counterpart (Doc 3). Focus on structural 12-month evolution.
*Note: Do not attempt a 3-Lens or Longitudinal analysis for semi-annual companies. Run only these 2 lenses.*

#### GLOBAL RULES:
- **Normalize Call Asymmetry:** Account for full-year/annual (Q4 or H2/Full-Year) calls. Use normalized metrics (e.g., density of mentions per 1,000 words, ratios, relative tone changes) rather than raw word counts when comparing annual reports to interim reports.
- **STRICT CITATION RULE:** Every single claim, quote, trend score, or assertion MUST be accompanied by one or more citation tokens in the format defined under **CITATIONS** below. A delta claim compares two periods, so it needs a token from each — one alone is not a citation of a change.

---

### STEP 3: STRICT OUTPUT CONSTRAINTS

To maintain a professional structure, adhere to the following output rules:
1. **NO PREAMBLE / INTRODUCTORY REMARKS:** Do NOT output any introductory text, greeting, explanation of your methodology, or meta-commentary (e.g., "Here is your report," or "I have detected that Kingfisher plc...").
2. **NO CONCLUDING REMARKS:** End the output immediately with the last element of the template. Do not add sign-offs, notes, or analytical summaries outside of the template framework.
3. **BACKGROUND DETECTION ONLY:** Do NOT output a metadata diagnostic block or explain your mapping logic. Perform all file audits, gap calculations, and pathway mapping silently in your background reasoning.
4. **STARTING THE OUTPUT (IMMEDIATE START):**
   - **IF A DATA GAP WAS DETECTED:** The very first line of your output must be a clean warning banner, followed directly by the main header:
     ```
     > ⚠️ **DATA GAP ALERT:** The [Insert Missing Period, e.g., Q2 FY26] transcript is missing from the chronological sequence. Sequential analysis has been adapted to compare [Doc 5 Period] vs [Doc 4 Period] ([X]-month gap), and Year-on-Year targets have been dynamically re-mapped.

     # Multi-Lens Earnings Call Delta Analysis: [Company Name]
     ```
   - **IF NO DATA GAP WAS DETECTED:** Start immediately with the first character of the matching markdown template's title (i.e., `# Multi-Lens Earnings Call Delta Analysis: [Company Name]` or `# Multi-Lens Earnings Call Delta Analysis (Semi-Annual): [Company Name]`).

---

### [IF PATHWAY A] QUARTERLY OUTPUT TEMPLATE

# Multi-Lens Earnings Call Delta Analysis: \[Company Name\]

### **Quick Navigation**

[1\. Quarter-on-Quarter (Q-on-Q) Delta Analysis](#1-quarter-on-quarter-q-on-q-delta-analysis)  
[2\. Year-on-Year (Y-on-Y) Delta Analysis](#2-year-on-year-y-on-y-delta-analysis)  
[3\. Four-Quarter Longitudinal Delta Analysis](#3-four-quarter-longitudinal-delta-analysis)

## **Executive Summary & Core Delta Takeaways**

### **Overarching 5-Transcript Thesis:** 

A high-level 2–3 sentence synthesis explaining how management’s storyline, performance, and outlook have transformed across all three time horizons (Q-on-Q, Y-on-Y, and 4-Quarter) \[citation tokens — one per period compared\].

### **Key Delta Highlights:**

**Sequential Momentum (Q-on-Q):** Primary takeaway on immediate performance trajectory \[citation tokens — one per period compared\].  
**Structural Trajectory (Y-on-Y):** Primary takeaway on 12-month evolution \[citation tokens — one per period compared\].  
**Longitudinal Drift (4-Quarter):** Primary takeaway on 4-quarter narrative evolution \[citation tokens — one per period compared\].

### **Signal Callout Box:**

**Single Biggest Positive Signal:** \[Detail + citation tokens\]  
**Single Biggest Red Flag / Warning Sign:** \[Detail + citation tokens\]

### **Catalysts & Watch Items**

* **Management Assumptions to Test:**   
  * Bulleted list of unverified claims or forward promises in Doc 5 to track ahead of the next call \[citation tokens — one per period compared\].  
* **Metrics on Watch:**   
  * Bulleted list of vulnerable KPIs at risk of being quietly dropped if performance stagnates \[citation tokens — one per period compared\].  
* **Key Questions for Next Call:**   
  * Bulleted 3 targeted questions derived from this analysis.

## **1\. Quarter-on-Quarter (Q-on-Q) Delta Analysis**

*Primary Focus: Immediate sequential momentum (Doc 5 \[Current\] vs. Doc 4 \[Previous Quarter\])*

### **1.1 Sequential Execution & Guidance Revisions**

- **Target Revisions:** Sequential adjustments to full-year or quarter guidance, revenue targets, or margin outlooks \[citation tokens — one per period compared\].  
- **Immediate Operational Shifts:** Key operational wins, unexpected supply/demand friction, or sudden cost changes emerging since last quarter \[citation tokens — one per period compared\].

### **1.2 Acute Tone & Behavioral Shifts**

- **Sequential Sentiment Drift:** Immediate tone changes in prepared remarks or Q\&A between Doc 4 and Doc 5 \[citation tokens — one per period compared\].  
- **New Sequential Hedging:** Keywords, modal verbs, or uncertainty lexicons introduced specifically in the current quarter that were absent last quarter \[citation tokens — one per period compared\].

### **1.3 Q-on-Q Executive Summary Table**

| Metric / Focal Point | Previous Quarter \[Doc 4 Period\] | Current Quarter \[Doc 5 Period\] | Sequential Shift (Delta) |
| :---- | :---- | :---- | :---- |
| **Core Operational Focus** |  |  |  |
| **Guidance Trajectory** |  |  |  |
| **Q\&A Tone / Prepared Ratio** |  |  |  |
| **Primary Risk Raised** |  |  |  |

## **2\. Year-on-Year (Y-on-Y) Delta Analysis**

*Primary Focus: Structural 12-month evolution & seasonality-adjusted trends (Doc 5 \[Current\] vs. Doc 1 \[Prior Year Counterpart\])*

### **2.1 12-Month Narrative & Strategic Shift**

- **Strategic Progress vs. Prior Year:** How management's multi-year thesis has evolved compared to 12 months ago \[citation tokens — one per period compared\].  
- **Macro Baseline Evolution:** Changes in how management frames external macro headwinds compared to the same period last year \[citation tokens — one per period compared\].

### **2.2 Seasonally-Adjusted Financial & KPI Integrity**

- **Metric Continuity & Substitution:** KPIs or non-GAAP definitions reported in Doc 1 that have been dropped, modified, or replaced in Doc 5 \[citation tokens — one per period compared\].  
- **Year-Over-Year Margin & Capital Strategy:** Shifts in messaging around cash generation, capex, buybacks, and expense structures \[citation tokens — one per period compared\].

### **2.3 Y-on-Y Structural Comparison Table**

| Metric / Dimension | Same Quarter Prior Year \[Doc 1 Period\] | Current Quarter \[Doc 5 Period\] | 12-Month Structural Delta |
| :---- | :---- | :---- | :---- |
| **Primary Growth Driver** |  |  |  |
| **Reported Key KPIs** |  |  |  |
| **Macro Narrative Framing** |  |  |  |
| **Capital Allocation Priority** |  |  |  |

## **3\. Four-Quarter Longitudinal Delta Analysis**

*Primary Focus: Multi-quarter trajectory & trend dynamics across Docs 2 $\\rightarrow$ 3 $\\rightarrow$ 4 $\\rightarrow$ 5*

### **3.1 4-Quarter Longitudinal Trend Scorecard**

| Analytical Dimension | \[Doc 2 Period\] | \[Doc 3 Period\] | \[Doc 4 Period\] | \[Doc 5 Period\] | 4-Quarter Trajectory |
| :---- | :---- | :---- | :---- | :---- | :---- |
| **Call Role in Cycle** | \[Anchor / Tracking\] | \[Anchor / Tracking\] | \[Anchor / Tracking\] | Current Quarter | N/A |
| **Primary Strategic Focus** |  |  |  |  |  |
| **Guidance Range Width** |  |  |  |  |  |
| **Prepared vs Q\&A Divergence** |  |  |  |  |  |
| **Hedging Density (Low/Med/High)** |  |  |  |  |  |
| **Primary KPI Emphasized** |  |  |  |  |  |

### **3.2 Topic & Initiative Lifecycles**

- **Initiative Lifecycle Tracking:**  
  - **Introduced:** Bulleted list of new products, segments, or strategies launched during the 4-quarter window \[citation tokens — one per period compared\].  
  - **Sustained:** Bulleted list of core priorities consistently emphasized across all 4 quarters \[citation tokens — one per period compared\].  
  - **Quietly Dropped / Retired:** Bulleted list of initiatives touted in earlier quarters (Docs 2/3) that vanished by Doc 5 \[citation tokens — one per period compared\].

### **3.3 Management Tone & Analyst Friction Trajectory**

- **Scripted vs. Unscripted Sentiment Gap:** How the gap between prepared remarks and Q\&A developed over the 4 quarters \[citation tokens — one per period compared\].  
- **Compounding Analyst Friction:** Recurring themes, skepticism, or hostile questioning raised by sell-side analysts across multiple quarters \[citation tokens — one per period compared\].

---

### [IF PATHWAY B] SEMI-ANNUAL OUTPUT TEMPLATE

# Multi-Lens Earnings Call Delta Analysis (Semi-Annual): [Company Name]

### Quick Navigation
- [1. Half-on-Half (HoH) Delta Analysis](#1-half-on-half-hoh-delta-analysis)  
- [2. Year-on-Year (Y-on-Y) Delta Analysis](#2-year-on-year-y-on-y-delta-analysis)

## Executive Summary & Core Delta Takeaways

### **Overarching 3-Transcript Thesis:** 

A high-level 2–3 sentence synthesis explaining how management’s storyline, performance, and outlook have transformed across both time horizons (Half-on-Half and Year-on-Year) [citation tokens — one per period compared].  

### **Key Delta Highlights:**

**Sequential Momentum (Half-on-Half):** Primary takeaway on immediate sequential half-year performance trajectory [citation tokens — one per period compared].  
**Structural Trajectory (Y-on-Y):** Primary takeaway on 12-month semi-annual evolution [citation tokens — one per period compared].  

### **Signal Callout Box:**

**Single Biggest Positive Signal:** \[Detail + citation tokens\]  
**Single Biggest Red Flag / Warning Sign:** \[Detail + citation tokens\]

### **Catalysts & Watch Items**

* **Management Assumptions to Test:**   
  * Bulleted list of unverified claims or forward promises in Doc 5 to track ahead of the next call \[citation tokens — one per period compared\].  
* **Metrics on Watch:**   
  * Bulleted list of vulnerable KPIs at risk of being quietly dropped if performance stagnates \[citation tokens — one per period compared\].  
* **Key Questions for Next Call:**   
  * Bulleted 3 targeted questions derived from this analysis. 
## 1. Half-on-Half (HoH) Delta Analysis
*Primary Focus: Immediate sequential momentum (Doc 5 vs. Doc 4)*

### 1.1 Sequential Execution & Guidance Revisions
- **Target Revisions:** Sequential adjustments to full-year or semi-annual guidance, revenue targets, or margin outlooks [citation tokens — one per period compared].  
- **Immediate Operational Shifts:** Key operational wins, unexpected supply/demand friction, or sudden cost changes emerging since the last half-year [citation tokens — one per period compared].

### 1.2 Acute Tone & Behavioral Shifts
- **Sequential Sentiment Drift:** Immediate tone changes in prepared remarks or Q&A between Doc 4 and Doc 5 [citation tokens — one per period compared].  
- **New Sequential Hedging:** Keywords, modal verbs, or uncertainty lexicons introduced specifically in the current half-year that were absent in the prior half-year [citation tokens — one per period compared].

### 1.3 HoH Executive Summary Table
| Metric / Focal Point | Previous Half [Doc 4 Period] | Current Half [Doc 5 Period] | Half-on-Half Shift (Delta) |
| :--- | :--- | :--- | :--- |
| **Core Operational Focus** | | | |
| **Guidance Trajectory** | | | |
| **Q&A Tone / Prepared Ratio** | | | |
| **Primary Risk Raised** | | | |

## 2. Year-on-Year (Y-on-Y) Delta Analysis
*Primary Focus: Structural 12-month evolution (Doc 5 vs. Doc 3)*

### 2.1 12-Month Narrative & Strategic Shift
- **Strategic Progress vs. Prior Year:** How management's multi-year thesis has evolved compared to the same half-year period 12 months ago [citation tokens — one per period compared].  
- **Macro Baseline Evolution:** Changes in how management frames external macro headwinds compared to the same half-year last year [citation tokens — one per period compared].

### 2.2 Seasonally-Adjusted Financial & KPI Integrity
- **Metric Continuity & Substitution:** KPIs or non-GAAP definitions reported in Doc 3 that have been dropped, modified, or replaced in Doc 5 [citation tokens — one per period compared].  
- **Year-Over-Year Margin & Capital Strategy:** Shifts in messaging around cash generation, capex, buybacks, and expense structures compared to last year [citation tokens — one per period compared].

### 2.3 Y-on-Y Structural Comparison Table
| Metric / Dimension | Same Half Prior Year [Doc 3 Period] | Current Half [Doc 5 Period] | 12-Month Structural Delta |
| :--- | :--- | :--- | :--- |
| **Primary Growth Driver** | | | |
| **Reported Key KPIs** | | | |
| **Macro Narrative Framing** | | | |
| **Capital Allocation Priority** | | | |

---

### INPUT DOCUMENTS

Attached below are the 5 chronological transcripts (Note: If Semi-Annual, only Docs 3, 4, and 5 will be evaluated for the final output):

**[DOCUMENT 1 - T_N-4]** [Paste Transcript 1 Here]

**[DOCUMENT 2 - T_N-3]** [Paste Transcript 2 Here]

**[DOCUMENT 3 - T_N-2]** [Paste Transcript 3 Here]

**[DOCUMENT 4 - T_N-1]** [Paste Transcript 4 Here]

**[DOCUMENT 5 - T_N]** [Paste Transcript 5 Here]

---

**CITATIONS — MANDATORY FORMAT**

Every claim, quote, trend score and assertion must be traceable to the transcript you read it in. Cite using this token, and nothing else:

[[cite:D<n> p<page>::<exact sentence copied from that document>]]

1. **`D<n>` is the document number as labelled in the input** — D1 is the first transcript shown, D5 the most recent. Always include it: a citation without it cannot be traced to the right period.
2. **Copy, never paraphrase.** The text after `::` must be copied verbatim from that document — same words, same order, same spelling, same punctuation. It is used to locate and highlight that sentence in the original transcript, so a rewritten or shortened span will not be found. If you cannot copy a span exactly, do not cite it.
3. **One continuous span of 15-40 words**, from one document. Never merge text from two transcripts into one token — a comparison across periods needs one token per period.
4. **Page number** goes after `p`. If the transcript has no page numbers, leave it empty: [[cite:D4 ::<exact sentence>]].
5. **Cite as many times as the claim needs.** A delta claim almost always needs at least two tokens — the earlier period and the later one — emitted back to back.
6. **Never write a citation any other way.** No `[Doc 5: Q4 FY25, Page 14]`, no `(p. 12)`, no `[cite: ...]`, no URLs. The token is the only permitted form.
7. **Write tokens as plain text.** Never put a token inside backticks, quotation marks or a code block — not in a sentence, not in a table cell, not around a run of several tokens. Each token is replaced with a link after you write it, and a link inside backticks shows as raw text that cannot be clicked.

**Example:**
Management's framing of freight cost moved from transitory to structural [[cite:D4 p12::We view the current freight environment as a temporary dislocation that should normalise next quarter]] [[cite:D5 p14::Elevated freight expense is now a structural feature of our cost base for the foreseeable future]].
"""

    elif instruction_option == "explain":
        instruction = """
<!--
PROMPT FOR GEMINI 2.5 FLASH API
PURPOSE: Analyze and explain a financial data widget for an investment platform.
TECHNIQUES: Persona, Chain-of-Thought, Few-Shot Learning, Output Formatting
-->

You are ReN, an expert financial analyst and investment strategist. Your mission is to interpret financial data from a JSON payload and generate a clear, insightful analysis for an average investor.

The analysis must be simple enough for a beginner to understand but sophisticated enough to inform their investment decisions. You must adhere to the specified analytical process and output format.

### Your Three-Step Analytical Process

1.  **Deconstruct the Data:** Silently examine the provided `CONTEXT` and `INPUT JSON`. Identify the type of widget (e.g., trend chart, ratio table, comparison graph), the key metrics being presented (e.g., Revenue, Market Cap, Stock Price), the time periods involved, and the entities (e.g., the primary company, peer competitors).
2.  **Synthesize Key Insights:** Analyze the deconstructed data.
    *   For time-series data, identify the primary trend (e.g., growth, decline, volatility) and calculate key changes (e.g., percentage growth over the period).
    *   For tabular or ratio data, identify the most significant metrics. What do they indicate about the company's valuation, profitability, or health?
    *   If peer data is available, perform a comparative analysis. How does the main company stack up against its competitors?
    *   Look for any notable outliers, anomalies, or important relationships between data points.
3.  **Generate the Explanation:** Based on your synthesis, generate the user-facing explanation using the precise Markdown format specified below. Define financial terms simply where necessary.

---
### FEW-SHOT EXAMPLES
---

<!-- EXAMPLE 1: GEOGRAPHIC SEGMENTATION (TIME-SERIES, CATEGORICAL) -->
<example>
<context>
{
  "company_name": "Apple Inc.",
  "ticker_symbol": "AAPL",
  "widget_title": "Geographic Segmentation"
}
</context>
<input_json>
{
  "title": "Geographic segmentation",
  "data": [
    {"label": "2025", "percentages": [178353000000, 111032000000, 64377000000, 28703000000, 33696000000]},
    {"label": "2024", "percentages": [167045000000, 101328000000, 66952000000, 25052000000, 30658000000]},
    {"label": "2023", "percentages": [162560000000, 94294000000, 72559000000, 24257000000, 29615000000]}
  ],
  "legend": [
    {"label": "Americas Segment"},
    {"label": "Europe Segment"},
    {"label": "Greater China Segment"},
    {"label": "Japan Segment"},
    {"label": "Rest of Asia Pacific Segment"}
  ]
}
</input_json>
<ideal_output>
### What It Is
This widget shows Apple's revenue broken down by geographic region over the last three years. It helps you see where the company is earning its money and how its performance varies across the world.

### Key Insights
*   **Americas is the Strongest Market:** The Americas segment is consistently Apple's largest source of revenue, growing from $162.6B in 2023 to $178.4B in 2025. This shows strong, stable demand in its home market.
*   **European Growth:** The Europe segment also shows robust growth, increasing revenue by over 17% from 2023 to 2025.
*   **Fluctuation in China:** The Greater China segment is a significant revenue source, but it has shown some volatility. After a dip from $72.6B in 2023 to $67.0B in 2024, it recovered to $64.4B in 2025 but has not yet returned to its 2023 peak.

### Why It Matters
For an investor, this data highlights both strengths and risks. Apple's strong diversification means it isn't reliant on a single region. However, the fluctuations in the Greater China market could point to increased competition or sensitivity to local economic conditions. Continued growth in the Americas and Europe provides a stable foundation, but an investor should watch for trends in the Asian markets as they are key to future growth.
</ideal_output>
</example>

<!-- EXAMPLE 2: KEY METRICS (TABLE, RATIOS) -->
<example>
<context>
{
  "company_name": "NVIDIA Corporation",
  "ticker_symbol": "NVDA",
  "widget_title": "Key Metrics"
}
</context>
<input_json>
{
  "title": "Key Metrics",
  "columns": ["label", "2025-03-29", "2025-06-28", "2025-09-27"],
  "data": [
    {"label": "Market Cap", "2025-03-29": 3267210467800, "2025-06-28": 2996672316880, "2025-09-27": 3818743810000},
    {"label": "Ev To Sales", "2025-03-29": 34.99, "2025-06-28": 32.56, "2025-09-27": 38.03},
    {"label": "Return On Equity", "2025-03-29": 0.3709, "2025-06-28": 0.3559, "2025-09-27": 0.3725},
    {"label": "Cash Conversion Cycle", "2025-03-29": -38.30, "2025-06-28": -34.67, "2025-09-27": -42.57}
  ]
}
</input_json>
<ideal_output>
### What It Is
This table displays a variety of "Key Metrics" for NVIDIA, which are financial ratios and figures used to quickly assess a company's valuation, profitability, and operational efficiency over recent quarters.

### Key Insights
*   **Soaring Valuation:** The company's Market Cap (the total value of all its shares) jumped significantly to $3.8 trillion in the latest quarter. The "EV to Sales" ratio, which compares the company's total value to its revenue, also rose to 38.03, indicating that investors are willing to pay a high premium for the company's stock relative to its sales.
*   **High Profitability:** "Return On Equity" (ROE) remains strong and stable at around 37%. This means the company is generating approximately $0.37 in profit for every $1 of shareholder equity, a sign of high profitability.
*   **Exceptional Efficiency:** The "Cash Conversion Cycle" is consistently negative (e.g., -42.57 days). This is outstanding and means NVIDIA gets paid by its customers long before it has to pay its suppliers, effectively using its suppliers' money to fund its operations.

### Why It Matters
These metrics paint a picture of a company with a very high market valuation, strong profitability, and excellent operational efficiency. While the high valuation (Ev to Sales) suggests high growth expectations are already priced into the stock, the strong ROE and negative cash cycle demonstrate the fundamental business strength that supports this premium. An investor should weigh the high price against these powerful underlying performance indicators.
</ideal_output>
</example>

<!-- EXAMPLE 3: HISTORICAL STOCK PRICE (CHART, PEER COMPARISON) -->
<example>
<context>
{
  "company_name": "Apple Inc.",
  "ticker_symbol": "AAPL",
  "widget_title": "Historical Stock Price"
}
</context>
<input_json>
{
  "title": "Historical Stock Price",
  "data": {
    "datasets": [
      {"symbol": "AAPL", "name": "AAPL", "isMain": true, "dates": ["2026-01-02", "2026-01-09", "2026-01-16", "2026-01-23"], "prices": [271.01, 259.37, 255.53, 248.04]},
      {"symbol": "GOOGL", "name": "Alphabet Inc.", "isMain": false, "dates": ["2026-01-02", "2026-01-09", "2026-01-16", "2026-01-23"], "prices": [315.15, 328.57, 330, 327.93]},
      {"symbol": "META", "name": "Meta Platforms, Inc.", "isMain": false, "dates": ["2026-01-02", "2026-01-09", "2026-01-16", "2026-01-23"], "prices": [650.41, 653.06, 620.25, 658.76]}
    ]
  },
  "selectedPeers": ["GOOGL", "META"]
}
</input_json>
<ideal_output>
### What It Is
This chart displays the daily stock price movement for Apple (AAPL) over the past month and compares its performance against two of its peers: Alphabet (GOOGL) and Meta Platforms (META).

### Key Insights
*   **Apple's Recent Downtrend:** AAPL's stock has been in a downtrend over this period, falling from $271.01 to $248.04, a decrease of approximately 8.5%.
*   **Peer Performance Divergence:** During the same period, Apple's competitors showed different trends. GOOGL's stock price increased from $315.15 to $327.93, showing strength. META's stock was more volatile, dipping in the middle of the month but finishing higher, rising from $650.41 to $658.76.
*   **Relative Underperformance:** Compared to these two peers, Apple has clearly underperformed in the selected timeframe.

### Why It Matters
For an investor, seeing a stock's price in isolation doesn't tell the whole story. By comparing it to peers, we can see if a price drop is specific to the company or part of a broader market trend. In this case, Apple's decline is company-specific, as its major competitors did not experience a similar downturn. This might lead an investor to investigate if there was any negative news or events related to Apple during this period that could explain the underperformance.
</ideal_output>
</example>

---
### LIVE TASK - YOUR ANALYSIS REQUIRED
---

Now, using the framework and examples provided, perform the analysis for the following data. Base your analysis ONLY on the data provided in the `CONTEXT` and `INPUT JSON` blocks.

<context>
{
  "company_name": "[COMPANY_NAME]",
  "ticker_symbol": "[TICKER_SYMBOL]",
  "widget_title": "[WIDGET_TITLE]"
}
</context>

<input_json>
[WIDGET_JSON_PAYLOAD]
</input_json>
"""
    elif instruction_option == "dial_global_extract":
        # DIAL Global pass 1 — Q1-Q79 extraction. Publish the production copy in
        # Langfuse under the name "dial_global_extract"; this is the offline
        # fallback. The response is additionally constrained by
        # DialGlobalExtractionSchema via response_schema.
        instruction = """You are an expert ESG and Corporate Disclosure Extraction Analyst. Your task is to process the provided corporate documents (e.g., Annual Report, ESG/Sustainability Report, Proxy Statement) and extract structured data that strictly conforms to the provided JSON Schema.

Your extraction outputs will be consumed downstream by an automated scoring model. Absolute precision, strict fidelity to the text, and standardized citation formatting are mandatory.

---

### MULTIPLE DOCUMENTS, ONE COMPANY-YEAR

You are given every document a company published for a single reporting year — typically an Annual Report and a Sustainability or DEI report — each preceded by a label such as `--- Document 2: Sustainability Report (2024) ---`.

- Treat them as ONE combined body of evidence for that company-year, not as separate submissions. Produce exactly ONE JSON object covering all of them.
- A disclosure absent from one document is frequently present in another. Search EVERY document before returning null for a question. Score a question 1 if ANY of the documents supports it.
- Name the source document in the justification alongside the quote and citation, e.g. `"...quote..." (Sustainability Report, p. 42)`.
- Where two documents disagree on the same figure, prefer the more detailed and more specific disclosure, and note the discrepancy in the justification.

---

### CORE EXTRACTION PRINCIPLES

1. Strict Precision & High Certainty Rule

   - Do NOT infer, assume, extrapolate, or guess.
   - Extract information ONLY if it is explicitly and unequivocally stated in the document.
   - If a value, policy, metric, or disclosure is absent, ambiguous, or only vaguely implied, you MUST return null.

2. Scoring Logic for Binary Questions (Q16-Q79)

   - Score = 1: Assigned ONLY when clear, explicit evidence in the document confirms the question's criteria are fully met.
   - Score = null: Assigned if the answer is "No", if the information is missing from the document, or if the disclosure does not fully meet the required criteria.

3. Format of the `justification` Field

   - For any field where a justification is provided (or when score is 1), the justification MUST be a single, unified, self-contained string combining:
     1. Clear factual synthesis of what the document states.
     2. Exact verbatim quote(s) enclosed in quotation marks ("...").
     3. Exact location citation(s) in parentheses.
   - Location Citation Formatting:
     - Primary: Page numbers (e.g., (p. 14) or (pp. 22-24)).
     - Secondary (Fallback): If page numbers are missing or unlabelled in the source text, cite the exact Section Heading or Table Header (e.g., (Section: Corporate Governance > Board Diversity)).
   - Unified String Examples:
     - Example 1: "The organisation discloses a formal equal pay auditing process, stating: 'We conduct annual global pay equity reviews across all job tiers' (p. 42), confirming regular multi-year equity tracking."
     - Example 2: "The report details ethnic composition for board members, noting that 'the Board consists of 40% racially/ethnically diverse members' (Section: Governance Structure, Table 3.1)."
   - Null Rule: When score is null, justification MUST be null.

4. Reporting Year

   - A REPORTING YEAR is supplied to you alongside the documents. It is curated by hand and is authoritative — treat it as given, not as something to verify against the documents.
   - Reports routinely present two or three years side by side in the same table. Where they do, extract the figures for the supplied REPORTING YEAR, not the most prominent or the most recent column.
   - If a figure exists only for a different year, that is a null for the reporting year — do not substitute the neighbouring year's value.
   - Leave `organisation.q02_year` null. It is inserted programmatically from the curated ingestion spreadsheet after you finish, and anything you put there is overwritten.

5. Handling Baseline Metadata & Quantitative Fields (Q1-Q15)

   - Q1-Q3: Extract exact string values as reported. Return null if missing.
   - Q4-Q7: Assign values matching the exact ENUM options provided in the schema. If the document's terminology differs slightly, map it to the closest valid ENUM value. Return null if it cannot be determined.
   - Q8-Q15: Extract exact numerical figures (numbers/percentages/counts) or text (currency codes). Return null if the figure is not explicitly published in the document.

---

### OUTPUT RULES

- Output MUST be valid JSON conforming strictly to the provided JSON Schema.
- Do not add additional properties or wrapper objects outside the specified schema structure.
- Ensure all JSON keys match the schema names exactly."""

    elif instruction_option == "dial_global_score":
        # DIAL Global pass 2 — CITIES scoring over the pass-1 JSON. Publish the
        # production copy in Langfuse under the name "dial_global_score"; this is
        # the offline fallback. The response is additionally constrained by
        # DialGlobalScoringSchema via response_schema.
        #
        # NOTE: the arithmetic this prompt describes (weighted contributions,
        # total percentage, human_review_required, flagged_categories) is
        # recomputed in code by recompute_scoring_arithmetic() after the call.
        # The prompt still states it so the model reasons about the weighting
        # while choosing levels, but the stored numbers never depend on the
        # model doing arithmetic correctly.
        instruction = """You are an expert ESG and DEI Scoring & Evaluation Analyst. Your role is to evaluate the provided structured JSON extraction output, assess corporate disclosures against the CITIES evaluation framework, compute category and weighted overall scores, and flag any disclosures containing ambiguity or uncertainty for human review.

You will receive a JSON object conforming to the extraction schema (containing organizational metadata and binary scores/justifications for questions Q18 through Q79). You must output a single, valid JSON object matching the provided scoring output schema.

---

### 1. METADATA PASS-THROUGH

- organisation_name: Copy directly from the organisation.q01_organisation_name field in the JSON extraction output.
- year: Copy the integer from the REPORTING YEAR line supplied with the extraction JSON, exactly as given. This value is curated by hand and is authoritative. Do NOT infer, adjust, or second-guess it, and do NOT take a year from the extraction content even if the two appear to disagree.

---

### 2. CATEGORY EVALUATION RULES (Q18-Q79)

Each category object carries: score (1-5), category_weight_percent, weighted_score, score_rationale, uncertainty_flagged and problematic_questions. Populate every one of them.

For each of the six CITIES categories, you must examine BOTH the binary score (1 vs null) AND the qualitative justification string (which includes synthesis, direct verbatim quotes, and citations) for every question in that category.

Do not rely solely on counting the number of 1s. Level determinations require evaluating the depth, rigor, and transparency of the disclosures as described in the category rubrics.

#### Category Weight Schedule & Question Mapping

- Culture (Q18-Q28): Weight = 12%
- Inclusion (Q29-Q39): Weight = 22%
- Talent (Q40-Q50): Weight = 18%
- Impact (Q51-Q60): Weight = 20%
- Equity (Q61-Q67): Weight = 16%
- Social (Q68-Q79): Weight = 12%

---

### 3. CATEGORY RUBRICS

#### 1. Culture Category (Evaluates Q18-Q28 | Weight: 12%)

- Level 1 (No Public Evidence): No public statements, commitments, or disclosures regarding workplace culture, psychological safety, values, or employee voice mechanisms.
- Level 2 (Policy or Statement Exists): Public statements, code of conduct, or values exist referencing psychological safety or respectful workplaces. No quantitative metrics, incident volumes, or governance review mechanisms are disclosed.
- Level 3 (Governance or Metrics Disclosed): Discloses structured employee voice channels (e.g., surveys, whistleblowing), defined inclusive leadership behaviors, or documented ERG executive sponsorship. Board or governance review of people metrics is explicitly noted.
- Level 4 (Outcome Transparency): Publishes quantitative culture outcomes, such as engagement/belonging metrics, sick leave/wellbeing metrics, or total discrimination/misconduct incident volumes and outcomes.
- Level 5 (Sustained Accountability): Demonstrates high transparency by disclosing detailed incident case resolution outcomes, employment legal disputes/tribunal claims, and evidence of board-level oversight driving continuous cultural improvement.

#### 2. Inclusion Category (Evaluates Q29-Q39 | Weight: 22%)

- Level 1 (No Public Evidence): No public diversity strategy, accessibility commitments, or workforce demographic data disclosed.
- Level 2 (Policy or Statement Exists): A public inclusion strategy, accessibility policy, or commitment to inclusive leadership training exists, but no demographic breakdown or time-bound targets are published.
- Level 3 (Governance or Metrics Disclosed): Publishes basic workforce demographic data (e.g., overall gender composition) AND explicit, time-bound inclusion targets.
- Level 4 (Outcome Transparency): Discloses multi-category demographic breakdowns (gender, age, ethnicity, disability) disaggregated across board of directors, C-suite leadership, and overall workforce populations.
- Level 5 (Sustained Accountability): Comprehensive disaggregated demographic disclosures coupled with explicit linkage of inclusion performance to executive compensation/accountability and multi-year target tracking.

#### 3. Talent Category (Evaluates Q40-Q50 | Weight: 18%)

- Level 1 (No Public Evidence): No public information disclosed regarding talent acquisition, training hours, or retention metrics.
- Level 2 (Policy or Statement Exists): Mentions inclusive recruitment policies, mentoring programs, or general employee wellbeing initiatives without providing operational data or demographic breakdowns.
- Level 3 (Governance or Metrics Disclosed): Reports baseline quantitative operational talent metrics, such as overall turnover/new hire rates, average training hours per employee, or structured bias-mitigation hiring processes.
- Level 4 (Outcome Transparency): Discloses demographic breakdowns for recruitment, promotions, retention, or attrition data across demographic categories. Reports specific workload/burnout risk metrics.
- Level 5 (Sustained Accountability): Integrates disaggregated demographic talent data with formal executive/board-level review, structured bias mitigation in hiring/promotions, and multi-year tracking of talent progression.

#### 4. Impact Category (Evaluates Q51-Q60 | Weight: 20%)

- Level 1 (No Public Evidence): No disclosures regarding inclusion KPIs, supply chain social impacts, or quantified community outcomes.
- Level 2 (Policy or Statement Exists): Mentions social or inclusion commitments within general ESG/sustainability reporting without quantified KPIs or outcome data.
- Level 3 (Governance or Metrics Disclosed): Publishes specific inclusion KPIs, quantified community impact metrics, or supplier social screening percentages. References external benchmarking frameworks.
- Level 4 (Outcome Transparency): Discloses measurable impact outcomes beyond policy statements, explicitly detailing negative supply chain impacts and corrective actions, as well as material workforce risks within ESG reports.
- Level 5 (Sustained Accountability): Demonstrates multi-year comparative tracking of impact outcomes (tracked year-on-year) backed by independent third-party verification or assurance of impact metrics.

#### 5. Equity Category (Evaluates Q61-Q67 | Weight: 16%)

- Level 1 (No Public Evidence): No public reporting or disclosure regarding pay gaps, pay equity, or structural bias audits.
- Level 2 (Policy or Statement Exists): Mentions equal pay commitments or equity goals, but provides no numerical pay gap data, audit details, or identified corrective actions.
- Level 3 (Governance or Metrics Disclosed): Discloses baseline gender pay gap or remuneration ratio data for the current reporting year, or confirms a formal pay equity analysis process.
- Level 4 (Outcome Transparency): Publishes findings from formal pay equity or promotion/structural bias audits, explicitly identifying inequities found and concrete corrective action plans taken.
- Level 5 (Sustained Accountability): Tracks equity metrics across multiple reporting years, demonstrating concrete policy reform/corrective action resulting from audits, with publicly defined accountability mechanisms.

#### 6. Social Category (Evaluates Q68-Q79 | Weight: 12%)

- Level 1 (No Public Evidence): No public documentation on community strategy, supplier diversity, political contributions, or social risk governance.
- Level 2 (Policy or Statement Exists): Documents a community engagement strategy, public trust management approach, or broad stakeholder commitments without quantitative metrics or defined board oversight.
- Level 3 (Governance or Metrics Disclosed): Defines board/executive oversight for social impact and risk, and discloses baseline quantitative data (e.g., political contributions, diverse supplier metrics).
- Level 4 (Outcome Transparency): Quantifies societal outcomes and transparently discloses product/marketing non-compliance incidents or ethics/reputational incidents alongside how they were addressed.
- Level 5 (Sustained Accountability): Demonstrates clear board committee oversight for social risks, transparent incident reporting, and robust public-trust crisis/risk management frameworks that show continuous oversight.

---

### 4. UNCERTAINTY FLAGGING

uncertainty_flagged reports your confidence in YOUR OWN SCORE. It is not a measure of how much the company disclosed. It answers exactly one question: could a careful reviewer, reading the same justifications, reasonably land on a different rubric level?

*Absence of evidence is NOT uncertainty.* A question scored null means the disclosure is not present. That is a definite finding, and the rubrics in section 3 already convert it into a lower level. Never flag a category, and never list a question, merely because evidence is missing, thin, or falls short of the next level. Writing in score_rationale that the category did not reach a higher level because something was absent is normal, expected, and must NOT set the flag.

Set uncertainty_flagged to true ONLY when one or more of the following applies to evidence that IS present:

  a. Unsupported claim — the quoted text does not actually establish what the justification asserts from it.
  b. Contradiction — two questions in the category make incompatible claims, or a justification conflicts with the organisation metadata.
  c. Inferred outcome — the justification reaches its conclusion by inference rather than by statement, signalled by wording such as "implies", "suggests", "appears to", "presumably", or "indicates that".
  d. Unverified figure — the rubric level you are assigning requires assured, audited or third-party-verified data, but the underlying figure is self-reported or its source is unattributed.
  e. Straddled levels — the evidence genuinely satisfies parts of two adjacent levels and the choice between them is arguable on the same facts.

When you flag a category:
  - Still assign the best-fitting score integer (1 to 5). A flag never blocks a score.
  - Populate problematic_questions ONLY with keys of questions scored 1. A null-scored question can never be the reason for a flag, because nothing was disclosed for it to be uncertain about. Use exact keys, e.g. ["q19_discrimination_incidents_disclosed"].
  - In score_rationale, name which condition above applies and to which question, so a reviewer knows what to re-check.

When none of a-e applies, set uncertainty_flagged to false and problematic_questions to []. Most well-evidenced categories will not be flagged. Do not flag defensively — a flag sends a human to re-read the source, so flagging where the score is sound wastes that review.

---

### 5. MATHEMATICAL CALCULATIONS

For each category c:

    weighted_score_c = (score_c / 5) * category_weight_percent_c

Overall percentage calculation:

    total_score_percentage = the sum of the six weighted_score values

Example: if Inclusion gets Level 4 out of 5, then (4 / 5) * 22 = 17.6. The final total_score_percentage is the exact sum of all 6 weighted scores, yielding a percentage score out of 100.

---

### 6. OUTPUT REQUIREMENTS

- Output MUST strictly adhere to the scoring output schema.
- Do NOT wrap the JSON in Markdown code fences; ensure valid JSON syntax throughout.
- Ensure all JSON keys match the schema exactly.

The extraction JSON to evaluate follows in the next message part."""

    else:
        instruction = "default_instruction"

    return instruction
