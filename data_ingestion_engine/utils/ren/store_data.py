# data_ingestion_engine/utils/ren/store_data.py
#
# Persistence for ReN CITIES results.
#
# ReN companies live in the canonical `companies` table, so their ids are from a
# different space than dial_global_companies. Writing a ReN company_id into
# dial_global_extractions.company_id would either fail the foreign key or —
# worse — succeed and attribute the score to whichever DIAL Global company
# happens to share that id.
#
# DataX solved this with a second nullable `ren_company_id` BIGINT FK to
# companies, plus a CHECK constraint that exactly one of the two company columns
# is set. Both universes therefore share one table, one leaderboard query and one
# export path, and a row can never be ambiguous about which company it describes.

import logging
from typing import Any, Dict, List, Optional

from data_ingestion_engine.utils.dial_global.store_data import (  # noqa: F401
    STATUS_SCORED,
    STATUS_UNSCORED,
    store_dial_global_extraction,
)

logger = logging.getLogger(__name__)

# The column ReN results hang off. company_id is the DGI universe and indexes
# dial_global_companies; writing a canonical id there would attribute the score
# to a different company entirely.
REN_COMPANY_FIELD = "ren_company_id"


def store_ren_extraction(
    company_id: int,
    year: int,
    extracted_data: Any,
    uid: str,
    source_report_ids: Optional[List[int]] = None,
    status: str = STATUS_SCORED,
) -> Optional[Dict[str, Any]]:
    """
    Persist one ReN company-year result.

    Identical upsert to the DIAL Global path — one POST, DataX resolves insert
    vs update on the (ren_company_id, year) partial unique index and refuses to
    let an unscored result overwrite a scored one — just keyed on the ReN
    company column.

    `company_id` here is a canonical companies.company_id.
    """
    return store_dial_global_extraction(
        company_id=company_id,
        year=year,
        extracted_data=extracted_data,
        uid=uid,
        source_report_ids=source_report_ids,
        status=status,
        company_field=REN_COMPANY_FIELD,
    )
