import requests
import logging
from rest_framework.exceptions import PermissionDenied
import os
import dotenv

dotenv.load_dotenv()    

logger = logging.getLogger(__name__)

USAGE_API_URL = os.getenv("USAGE_API_URL")

def check_ai_access(token):
    """
    Checks if the user has access to AI features by querying the usage service.
    
    Args:
        token (str): The Firebase ID token.
        
    Raises:
        PermissionDenied: If the user does not have access or the check fails.
    """
    if not token:
        raise PermissionDenied("No authentication token provided.")

    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        response = requests.get(USAGE_API_URL, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            # The external service returns "has_ai_features": boolean
            if not data.get("has_ai_features", False):
                raise PermissionDenied("You have reached your plan limits for AI features.")
            return True
            
        elif response.status_code in [401, 403]:
             logger.warning(f"Usage service denied access: {response.status_code} - {response.text}")
             raise PermissionDenied("Unauthorized to access AI features.")
        else:
             logger.error(f"Usage service returned unexpected status: {response.status_code} - {response.text}")
             # Fail closed
             raise PermissionDenied("Unable to verify usage limits. Please try again later.")
             
    except requests.RequestException as e:
        logger.error(f"Error connecting to usage service: {e}")
        # Fail closed
        raise PermissionDenied("Unable to verify usage limits. Service unavailable.")
