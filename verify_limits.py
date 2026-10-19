import logging
import unittest
from unittest.mock import patch, MagicMock
from rest_framework.exceptions import PermissionDenied

# Adjust python path to find modules
import sys
import os
sys.path.append("/Users/erlindshpata/Programming/DEI/data-ingestion-engine")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")

from data_ingestion_engine.utils.permissions import check_ai_access

# Configure logging
logging.basicConfig(level=logging.INFO)

class TestLimitCheck(unittest.TestCase):
    @patch('data_ingestion_engine.utils.permissions.requests.get')
    def test_access_granted(self, mock_get):
        print("\nTesting Access Granted:")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"has_ai_features": True}
        mock_get.return_value = mock_response

        # Should not raise exception
        result = check_ai_access("dummy_token")
        self.assertTrue(result)
        print("✅ Access granted for user with has_ai_features=True")

    @patch('data_ingestion_engine.utils.permissions.requests.get')
    def test_access_denied_limit_reached(self, mock_get):
        print("\nTesting Access Denied (Limit Reached):")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"has_ai_features": False}
        mock_get.return_value = mock_response

        with self.assertRaises(PermissionDenied) as cm:
            check_ai_access("dummy_token")
        
        print(f"✅ Correctly raised PermissionDenied: {cm.exception}")

    @patch('data_ingestion_engine.utils.permissions.requests.get')
    def test_access_denied_unauthorized(self, mock_get):
        print("\nTesting Access Denied (Unauthorized):")
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        with self.assertRaises(PermissionDenied):
            check_ai_access("dummy_token")
        print("✅ Correctly raised PermissionDenied on 401")

if __name__ == '__main__':
    unittest.main()
