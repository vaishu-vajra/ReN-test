from celery import Celery
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE',
                      'data_ingestion_engine.settings')

app = Celery('data_ingestion_engine')

# Configure Celery to use Redis as the broker

# CELERY_BROKER_URL = os.getenv('REDIS_URL', 'redis://ingestion-engine-vyigy1.serverless.euw2.cache.amazonaws.com:6379/0')
# app.conf.broker_url = 'redis://localhost:6379/0'

app.config_from_object('django.conf:settings', namespace='CELERY')

# Set worker concurrency (adjust based on your EC2 instance's capacity)
app.conf.worker_concurrency = 4  # Example: 4 concurrent workers

# Set task time limits (soft)
app.conf.task_soft_time_limit = 800  # Soft limit: 13 minutes 20 seconds


# Optional: Set task rate limits
app.conf.task_annotations = {
    # 2 tasks per minute
    'data_ingestion_engine.tasks.process_file_task': {'rate_limit': '20/m'},
}
app.autodiscover_tasks()
