import os

# Must run before any project import: utils/store_data.py and utils/gc_service.py raise without these.
os.environ.setdefault("DATAX_API_URL", "http://datax.test")
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
