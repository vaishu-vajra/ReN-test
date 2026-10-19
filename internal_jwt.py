# common/auth/internal_jwt.py
import os
import time
import jwt
from typing import Optional, Dict, Any

INTERNAL_JWT_SECRET = os.environ["INTERNAL_JWT_SECRET"]
# datax-backend validates the audience (and the signature) but not the issuer.
# The default must match its INTERNAL_JWT_AUD or every call returns 401.
INTERNAL_JWT_AUD = os.getenv("INTERNAL_JWT_AUD", "datax-internal")
INTERNAL_JWT_ISS = os.getenv("INTERNAL_JWT_ISS", "ingestion-engine")


def mint_internal_jwt(sub_uid: str, extra: Optional[Dict[str, Any]] = None, ttl_seconds: int = 900) -> str:
    """Mint a short-lived internal JWT used by ingestion-engine to call DATAx."""
    now = int(time.time())
    payload: Dict[str, Any] = {
        "iss": INTERNAL_JWT_ISS,
        "aud": INTERNAL_JWT_AUD,
        "iat": now,
        "exp": now + ttl_seconds,
        "sub": sub_uid,  # user on whose behalf the job runs
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, INTERNAL_JWT_SECRET, algorithm="HS256")
