"""
URL configuration for data_ingestion_engine project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path

from data_ingestion_engine.api.views.extract_all_env_gov import extract_env_gov_for_all_companies
from data_ingestion_engine.api.views.extract_env_gov_paginated import extract_env_gov_page
from data_ingestion_engine.api.views.failures import get_failed_urls
from data_ingestion_engine.api.views.reingest_failed_urls import reingest_failed_urls
from data_ingestion_engine.api.views.gemini import (
    extract_data_points_gemini, 
    summarise_document_gemini, 
    risk_analysis, 
    explain_widget,
    priorities_and_sentiment_analysis,
    gap_analysis,
    earnings_delta
)
from data_ingestion_engine.api.views.health import health_check, test_route
from data_ingestion_engine.api.views.ingestion import download_xls_urls
from data_ingestion_engine.api.views.maintenance import update_page_num
from data_ingestion_engine.api.views.summarise import extract_data_points, summarise_document
from data_ingestion_engine.api.views.upload_excel import upload_excel_to_gcs

# ---> DIAL GLOBAL <---
from data_ingestion_engine.api.views.dial_global.ingestion import download_xls_urls_dial_global
from data_ingestion_engine.api.views.dial_global.extract_all import extract_dial_global_for_all_companies
from data_ingestion_engine.api.views.dial_global.extract_progressive import extract_dial_global_progressive
from data_ingestion_engine.api.views.dial_global.extract_paginated import extract_dial_global_page
from data_ingestion_engine.api.views.dial_global.leaderboard import dial_global_leaderboard
from data_ingestion_engine.api.views.ren.extract_all import extract_ren_for_all_companies
from data_ingestion_engine.api.views.ren.extract_progressive import extract_ren_progressive


urlpatterns = [
    path('admin/', admin.site.urls),
    # path('download-pdfs/', download_pdf_urls, name='download_pdf_urls'),
    path('', health_check, name='health_check'),
    path('health/', health_check, name='health_check'),
    path('failed-urls/', get_failed_urls, name='get_failed_and_invalid_urls'),
    path('reingest-failed-urls/', reingest_failed_urls, name='reingest_failed_urls'),
    
    path('download-xls-urls/', download_xls_urls, name='download_xls_urls'),
    # ---> DIAL GLOBAL <---
    path('dial-global/download-xls-urls/', download_xls_urls_dial_global,
         name='dial_global_download_xls_urls'),
    path('dial-global/extract/all/', extract_dial_global_for_all_companies,
         name='extract_dial_global_for_all_companies'),
    path('dial-global/extract/progressive/', extract_dial_global_progressive,
         name='extract_dial_global_progressive'),
    path('dial-global/extract/page/', extract_dial_global_page,
         name='extract_dial_global_page'),
    path('dial-global/leaderboard/', dial_global_leaderboard,
         name='dial_global_leaderboard'),
    path('ren/extract/all/', extract_ren_for_all_companies,
         name='extract_ren_for_all_companies'),
    path('ren/extract/progressive/', extract_ren_progressive,
         name='extract_ren_progressive'),

    path('summarise/', summarise_document, name='summarise_document'),
    path('extract-data-points/', extract_data_points, name='extract_data_points'),
    path('summariseV2/', summarise_document_gemini,
         name='summarise_document_gemini'),
    path('extract-data-pointsV2/', extract_data_points_gemini,
         name='extract_data_points_gemini'),
    path('update-page-num/', update_page_num, name='update_page_num'),
    path('test/', test_route, name='test_route'),
    path("extract-env-gov/all/", extract_env_gov_for_all_companies,
         name="extract_env_gov_for_all_companies"),
    path("extract-env-gov/page/", extract_env_gov_page,
         name="extract_env_gov_page"),
    path("upload/excel-to-gcs/", upload_excel_to_gcs),
    path("risk-analysis/", risk_analysis, name="risk_analysis"),
    path("priorities-and-sentiment-analysis/", priorities_and_sentiment_analysis, name="priorities_and_sentiment_analysis"),
    path("gap-analysis/", gap_analysis, name="gap_analysis"),
    path("earnings-delta/", earnings_delta, name="earnings_delta"),
    path("explain/", explain_widget, name="explain_widget"),
]
