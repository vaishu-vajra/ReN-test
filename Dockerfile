
FROM python:3.10-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        ca-certificates \
        ghostscript \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-liberation \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN playwright install chromium
COPY . /app/
EXPOSE 80
# stage 2: web
FROM base AS web
CMD ["gunicorn", "--timeout", "120", "data_ingestion_engine.wsgi:application", "--bind", "0.0.0.0:80", "--workers", "3"]
# stage 3: celery
FROM base AS celery
CMD ["celery", "-A", "data_ingestion_engine", "worker", "--loglevel=info", "--concurrency=4"]
