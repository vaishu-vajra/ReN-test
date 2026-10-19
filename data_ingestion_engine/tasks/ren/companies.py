# data_ingestion_engine/tasks/ren/companies.py
#
# The ReN companies the CITIES pipeline runs over.
#
# Kept as a literal list rather than a DataX query because this is a chosen
# cohort, not "every company we have" — the canonical companies table holds far
# more than we want to score.
#
# Either form works and the two can be mixed freely:
#   * an int  -> a canonical companies.company_id, used as-is
#   * a str   -> a company name, resolved to an id via the company-upsert
#                endpoint at dispatch time
#
# Names are resolved once per fan-out, not once per task, and anything that
# fails to resolve is reported in the endpoint's response rather than silently
# dropped.

from typing import List, Union

# Paste the cohort here.
REN_COMPANIES: List[Union[int, str]] = [
    # "Acme plc",
    # 1234,
]
