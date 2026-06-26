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
Celery worker entrypoint for Retriva v2 ingestion.

Run inside the retriva-ingestion Docker image with:
    python -m retriva.ingestion_api.worker
or via celery CLI:
    celery -A retriva.ingestion_api.celery_app worker --loglevel=info -Q ingestion
"""

import sys

from retriva.config import settings
from retriva.logger import setup_logging, get_logger

logger = get_logger(__name__)


def main():
    setup_logging()

    if not settings.celery_broker_url:
        print("ERROR: CELERY_BROKER_URL is not set. The worker cannot start.")
        sys.exit(1)

    from retriva.ingestion_api.celery_app import get_celery_app
    app = get_celery_app()

    if app is None:
        print("ERROR: Could not create Celery app (celery not installed?)")
        sys.exit(1)

    # Register tasks explicitly (autodiscover may not find them in all setups)
    from retriva.ingestion_api.tasks import _register_tasks
    _register_tasks(app)

    print(f"##### Retriva Ingestion Worker #####")
    print(f"  Broker URL:      {settings.celery_broker_url}")
    print(f"  Result backend:  {settings.celery_result_backend or '(same as broker)'}")
    print(f"  Concurrency:     {settings.celery_worker_concurrency}")
    print(f"  Max retries:     {settings.celery_task_max_retries}")
    print()

    worker = app.Worker(
        loglevel="info",
        queues=["ingestion"],
        concurrency=settings.celery_worker_concurrency,
    )
    worker.start()


if __name__ == "__main__":
    main()
