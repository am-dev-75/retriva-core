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

"""Startup-surface secret-exposure regression tests.

Spec 034 §7 / acceptance B8 / Constitution §34: the worker startup output,
the Celery application log, the ingestion API settings banner and the graph
event bus must never emit credential-bearing URLs.  Assertions use exact
synthetic secrets and their encoded forms.
"""

import logging
from urllib.parse import quote

import pytest

from retriva.logger.redaction import REDACTED_URL_MARKER

SECRET = "S3cr3tToken123"
SECRET2 = "B4ck3ndS3cr3t456"
BROKER = f"redis://rtrv-broker:{SECRET}@redis:6379/0"
BACKEND = f"redis://rtrv-results:{SECRET2}@redis:6379/1"


def _assert_no_credential(text: str) -> None:
    for token in (SECRET, SECRET2, quote(SECRET, safe=""), quote(SECRET2, safe="")):
        assert token not in text, text


@pytest.fixture
def synthetic_settings(monkeypatch):
    from retriva.config import settings
    import retriva.ingestion_api.celery_app as ca

    monkeypatch.setattr(settings, "celery_broker_url", BROKER)
    monkeypatch.setattr(settings, "celery_result_backend", BACKEND)
    monkeypatch.setattr(ca, "_celery_app", None)
    return settings


class _FakeWorker:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        _FakeWorker.instances.append(self)

    def start(self):
        self.started = True


@pytest.fixture
def fake_worker(monkeypatch):
    import celery

    _FakeWorker.instances = []
    monkeypatch.setattr(celery.Celery, "Worker", _FakeWorker)
    return _FakeWorker


def test_worker_startup_output_has_no_credentials(
    synthetic_settings, fake_worker, monkeypatch, capsys
):
    import retriva.ingestion_api.worker as worker_mod

    # setup_logging() installs a StreamHandler on sys.stdout; clear the root
    # handlers so the captured stream is the one used by the worker output.
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    root.handlers = []
    try:
        worker_mod.main()
    finally:
        root.handlers = saved_handlers

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    _assert_no_credential(combined)
    # Diagnostics remain available, in redacted form.
    assert "Retriva Ingestion Worker" in combined
    assert "Broker URL" in combined and "redis://redis:6379/0" in combined
    assert "Result backend" in combined and "redis://redis:6379/1" in combined
    # Initialization behavior unchanged (Worker constructed and started with
    # exactly the previous arguments).
    assert len(fake_worker.instances) == 1
    instance = fake_worker.instances[0]
    assert instance.started is True
    assert instance.kwargs == {
        "loglevel": "info",
        "queues": ["ingestion"],
        "concurrency": synthetic_settings.celery_worker_concurrency,
    }


def test_worker_startup_log_records_have_no_credentials(
    synthetic_settings, fake_worker, caplog
):
    import retriva.ingestion_api.worker as worker_mod

    with caplog.at_level(logging.DEBUG):
        worker_mod.main()

    for record in caplog.records:
        _assert_no_credential(record.getMessage())
        # Logging-format arguments must not carry the raw value either.
        _assert_no_credential(str(record.args))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "redis://redis:6379/0" in text
    assert "redis://redis:6379/1" in text


def test_worker_internal_broker_settings_unchanged(synthetic_settings, fake_worker):
    import retriva.ingestion_api.worker as worker_mod
    import retriva.ingestion_api.celery_app as ca

    worker_mod.main()

    app = ca.get_celery_app()
    assert app is not None
    # The runtime still uses the credential-bearing URL internally.
    assert app.conf.broker_url == BROKER
    assert app.conf.result_backend == BACKEND
    # Task registration behavior unchanged.
    assert any("process_document_task" in name for name in app.tasks)


def test_celery_app_log_has_no_credentials(synthetic_settings, caplog):
    import retriva.ingestion_api.celery_app as ca

    with caplog.at_level(
        logging.DEBUG, logger="retriva.ingestion_api.celery_app"
    ):
        app = ca.get_celery_app()

    assert app is not None
    text = "\n".join(r.getMessage() for r in caplog.records)
    _assert_no_credential(text)
    assert "redis://redis:6379/0" in text
    assert "redis://redis:6379/1" in text


def test_celery_app_malformed_url_fails_closed(monkeypatch, caplog):
    from retriva.config import settings
    import retriva.ingestion_api.celery_app as ca

    malformed_secret = "SEKRET-malformed-789"
    monkeypatch.setattr(
        settings, "celery_broker_url", f"redis://user:{malformed_secret}@[::1"
    )
    monkeypatch.setattr(settings, "celery_result_backend", "")
    monkeypatch.setattr(ca, "_celery_app", None)

    with caplog.at_level(
        logging.DEBUG, logger="retriva.ingestion_api.celery_app"
    ):
        ca.get_celery_app()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert malformed_secret not in text
    assert REDACTED_URL_MARKER in text


def test_api_settings_banner_has_no_credentials(monkeypatch, capsys):
    from retriva import config
    import retriva.ingestion_api.__main__ as api_main

    monkeypatch.setattr(
        config.settings, "qdrant_url",
        f"http://qdrant-user:{SECRET}@qdrant:6333",
    )
    monkeypatch.setattr(
        config.settings, "embedding_base_url",
        f"https://token:{SECRET2}@api.example/v1",
    )
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["prog", "--host", "127.0.0.1", "--port", "65000"])

    api_main.main()

    out = capsys.readouterr().out
    _assert_no_credential(out)
    assert "http://qdrant:6333" in out
    assert "https://api.example/v1" in out


def test_openai_api_settings_banner_has_no_credentials(monkeypatch, capsys):
    from retriva import config
    import retriva.openai_api.__main__ as oai_main

    monkeypatch.setattr(
        config.settings, "qdrant_url",
        f"http://qdrant-user:{SECRET}@qdrant:6333",
    )
    monkeypatch.setattr(
        config.settings, "chat_base_url",
        f"https://token:{SECRET2}@api.example/v1",
    )
    monkeypatch.setattr(oai_main.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["prog", "--host", "127.0.0.1", "--port", "65001"])

    oai_main.main()

    out = capsys.readouterr().out
    _assert_no_credential(out)
    assert "http://qdrant:6333" in out
    assert "https://api.example/v1" in out


def test_event_bus_init_redis_log_has_no_credentials(caplog):
    from retriva.graph.event_bus import GraphEventBus

    GraphEventBus._reset()
    try:
        bus = GraphEventBus()
        with caplog.at_level(logging.DEBUG, logger="retriva.graph.event_bus"):
            bus.init_redis(f"redis://user:{SECRET}@127.0.0.1:1/0")
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET not in text
        assert "redis://127.0.0.1:1/0" in text
    finally:
        GraphEventBus._reset()
