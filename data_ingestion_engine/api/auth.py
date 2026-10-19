import os
import json
import logging
from types import SimpleNamespace

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

# Endpoints that power customer-facing features (ReN Portal summaries / data points /
# Explain, and PRIE's risk, gap and sentiment analysis). Any signed-in user may call them.
CUSTOMER_PATHS = frozenset({
    "/summariseV2",
    "/extract-data-pointsV2",
    "/risk-analysis",
    "/gap-analysis",
    "/priorities-and-sentiment-analysis",
    "/explain",
})

# Every other authenticated endpoint is an internal ingestion / pipeline operation and is
# restricted to staff: a verified email on one of these company domains. Firebase sign-up
# is open, so the domain alone is not proof of identity; email_verified is required too.
STAFF_EMAIL_DOMAINS = ("@myrenx.ai", "@diversityeconomics.ai")


def is_staff(decoded_token):
    email = (decoded_token.get("email") or "").lower()
    return bool(decoded_token.get("email_verified")) and email.endswith(STAFF_EMAIL_DOMAINS)

# Load env and init Firebase once
load_dotenv()
firebase_creds = os.getenv("FIREBASE_CREDS")
if not firebase_creds:
    raise RuntimeError(
        "Firebase credentials not found in environment variable.")

cred = credentials.Certificate(json.loads(firebase_creds))
try:
    firebase_admin.get_app()
except ValueError:
    firebase_admin.initialize_app(cred)


class FirebaseAuthentication(BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        if not auth_header:
            raise AuthenticationFailed("Unauthorized: No token provided")
        parts = auth_header.split()
        if parts[0].lower() != "bearer" or len(parts) != 2:
            raise AuthenticationFailed("Invalid Authorization header format")
        token = parts[1]
        try:
            decoded_token = firebase_auth.verify_id_token(
                token, check_revoked=True)
        except Exception as e:
            logging.error(f"Auth error: {e}")
            raise AuthenticationFailed(f"Unauthorized: {e}")
        if request.path.rstrip("/") not in CUSTOMER_PATHS and not is_staff(decoded_token):
            logging.warning(
                f"Staff-only endpoint {request.path} refused for uid={decoded_token.get('uid')}")
            raise PermissionDenied("This endpoint is restricted to staff accounts.")
        user = SimpleNamespace(**decoded_token)
        setattr(user, "is_authenticated", True)
        return (user, token)
