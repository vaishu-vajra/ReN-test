"""Settings for the financial extraction pipeline.

Importing this module must never raise; use require() where a value is mandatory.
"""
import os

# Dedicated Celery queue so financial jobs don't compete with other pipelines.
FINANCIAL_QUEUE = "financial"
# Same default as api/views/dial_global/extract_progressive.py.
DEFAULT_BATCH_SIZE = 20
# Same threshold as api/views/dial_global/extract_progressive.py.
CONFIRM_THRESHOLD = 25

# Gemini model used for extraction (same default as tasks/company_year_engine.py).
FINANCIAL_EXTRACT_MODEL = os.getenv("FINANCIAL_EXTRACT_MODEL", "gemini-3.8-flash")
# Upper bound on tokens the model may emit per extraction call.
FINANCIAL_EXTRACT_MAX_OUTPUT_TOKENS = int(os.getenv("FINANCIAL_EXTRACT_MAX_OUTPUT_TOKENS", "32768"))
# Caps how many documents go into one model call.
FINANCIAL_MAX_DOCS_PER_CALL = int(os.getenv("FINANCIAL_MAX_DOCS_PER_CALL", "6"))
# Celery rate limit for financial tasks.
FINANCIAL_RATE_LIMIT = os.getenv("FINANCIAL_RATE_LIMIT", "6/m")
# Celery soft time limit in seconds, so a job can clean up before the hard limit.
FINANCIAL_SOFT_TIME_LIMIT = int(os.getenv("FINANCIAL_SOFT_TIME_LIMIT", "1800"))
# Celery hard time limit in seconds, the longest a job may run.
FINANCIAL_TIME_LIMIT = int(os.getenv("FINANCIAL_TIME_LIMIT", "25000"))

# A lock must outlive the longest possible job.
FINANCIAL_LOCK_TTL_SECONDS = FINANCIAL_TIME_LIMIT + 600

# Optional Gemini media resolution for document inputs; None leaves the SDK default.
FINANCIAL_MEDIA_RESOLUTION = os.getenv("FINANCIAL_MEDIA_RESOLUTION")
# Name of the Langfuse prompt used for extraction.
FINANCIAL_PROMPT_NAME = os.getenv("FINANCIAL_PROMPT_NAME")
# Wait time to batch rapid triggers into one run.
FINANCIAL_DEBOUNCE_SECONDS = os.getenv("FINANCIAL_DEBOUNCE_SECONDS")
# How often the reconcile sweep runs.
FINANCIAL_RECONCILE_INTERVAL_SECONDS = os.getenv("FINANCIAL_RECONCILE_INTERVAL_SECONDS")
# JWT secret for verifying trigger requests (supplied by the client).
FINANCIAL_TRIGGER_JWT_SECRET = os.getenv("FINANCIAL_TRIGGER_JWT_SECRET")
# Expected JWT audience for trigger requests.
FINANCIAL_TRIGGER_JWT_AUD = os.getenv("FINANCIAL_TRIGGER_JWT_AUD")
# Expected JWT issuer for trigger requests.
FINANCIAL_TRIGGER_JWT_ISS = os.getenv("FINANCIAL_TRIGGER_JWT_ISS")


def require(name):
    """Return the setting called `name`, or raise if it is unset or empty."""
    value = globals().get(name)
    if value is None or value == "":
        raise RuntimeError(f"{name} is not set — see financial/config.py")
    return value
