# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Celery application factory for Retriva v2 ingestion.

When ``settings.celery_broker_url`` is set, this module provides a configured
Celery app that serves as both the broker client (in the API process) and the
worker entrypoint (via ``celery -A retriva.ingestion_api.celery_app worker``).

When the broker URL is empty, ``celery_enabled()`` returns False and the API
falls back to FastAPI BackgroundTasks.
"""

from __future__ import annotations

from typing import Optional

from retriva.config import settings
from retriva.logger import get_logger

logger = get_logger(__name__)

# Celery is an optional dependency.  Import lazily so that environments without
# Redis/Celery (e.g. unit tests, simple deployments) don't fail at import time.
_celery_app = None
_celery_imported = False


def _try_import_celery():
    """Import Celery once, lazily. Returns the Celery class or None."""
    global _celery_imported
    if _celery_imported:
        try:
            from celery import Celery
            return Celery
        except ImportError:
            return None
    _celery_imported = True
    try:
        from celery import Celery
        return Celery
    except ImportError:
        logger.info("celery not installed — async job queue disabled")
        return None


def celery_enabled() -> bool:
    """Return True if Celery + Redis are configured and available."""
    if not settings.celery_broker_url:
        return False
    return _try_import_celery() is not None


def get_celery_app():
    """Return the singleton Celery app, or None if Celery is not configured.

    The app is created on first call.  Subsequent calls return the cached
    instance.
    """
    global _celery_app

    if _celery_app is not None:
        return _celery_app

    Celery = _try_import_celery()
    if Celery is None or not settings.celery_broker_url:
        return None

    broker = settings.celery_broker_url
    backend = settings.celery_result_backend or broker

    app = Celery(
        "retriva_ingestion",
        broker=broker,
        backend=backend,
    )

    # ── Configuration ────────────────────────────────────────────────────
    app.conf.update(
        # Serialization
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # Time limits (0 = disabled)
        task_soft_time_limit=settings.celery_task_soft_time_limit or None,
        task_time_limit=settings.celery_task_time_limit or None,
        # Reliability: acknowledge after completion so a killed worker's
        # task is re-queued.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Don't prefetch more than one task at a time — OCR jobs are heavy.
        worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
        worker_concurrency=settings.celery_worker_concurrency,
        # Max retries for tasks that raise RetryableError
        task_default_max_retries=settings.celery_task_max_retries,
        # Keep results for 7 days
        result_expires=7 * 24 * 3600,
        # Task routing: all v2 ingestion tasks go to the 'ingestion' queue
        task_routes={
            "retriva.ingestion_api.tasks.process_document_task": {
                "queue": "ingestion",
            },
            "retriva.ingestion_api.tasks.process_mediawiki_task": {
                "queue": "ingestion",
            },
        },
        task_default_queue="ingestion",
    )

    # Auto-discover tasks module
    app.autodiscover_tasks(["retriva.ingestion_api"], "tasks")

    # ── Unified log format ──────────────────────────────────────────────
    # Match Retriva's standard format:
    #   [20260625 19:21:12] [INFO/ForkPoolWorker-1] message
    app.conf.worker_log_format = "[%(asctime)s] [%(levelname)s/%(processName)s] %(message)s"
    app.conf.worker_task_log_format = "[%(asctime)s] [%(levelname)s/%(processName)s] %(message)s"
    app.conf.log_date_format = "%Y%m%d %H:%M:%S"

    # Celery's ColorFormatter.__init__ accepts (fmt, use_color) but ignores
    # datefmt — it calls super().__init__(fmt) without passing datefmt.  This
    # means log_date_format config has no effect.  Monkey-patch it to accept
    # and forward datefmt so our %Y%m%d format is actually applied.
    import logging
    from celery.utils.log import ColorFormatter

    _original_init = ColorFormatter.__init__

    def _patched_init(self, fmt=None, use_color=True, datefmt=None):
        _original_init(self, fmt, use_color)
        if datefmt:
            self.datefmt = datefmt

    # Only patch if not already patched
    if not getattr(ColorFormatter.__init__, "_retriva_patched", False):
        _patched_init._retriva_patched = True
        ColorFormatter.__init__ = _patched_init

    # Also patch setup_handlers to pass datefmt from config
    from celery.app.log import Logging

    _original_setup_handlers = Logging.setup_handlers

    def _patched_setup_handlers(self, logger, logfile, format, colorize,
                                formatter=ColorFormatter, **kwargs):
        if self._is_configured(logger):
            return logger
        handler = self._detect_handler(logfile)
        datefmt = self.app.conf.log_date_format
        handler.setFormatter(formatter(format, use_color=colorize, datefmt=datefmt))
        logger.addHandler(handler)
        return logger

    if not getattr(Logging.setup_handlers, "_retriva_patched", False):
        _patched_setup_handlers._retriva_patched = True
        Logging.setup_handlers = _patched_setup_handlers

    _celery_app = app
    logger.info(f"Celery app created: broker={broker}, backend={backend}")
    return app
