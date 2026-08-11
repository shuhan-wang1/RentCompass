import json
import logging

from uk_rent_agent.logging_setup import configure_logging
from uk_rent_agent.observability import JsonFormatter, request_context


def test_json_logs_never_emit_raw_user_identity(monkeypatch):
    monkeypatch.setenv("LOG_ID_HMAC_KEY", "test-only-key")
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "event", (), None)

    with request_context("request-1", "raw-user-identity"):
        payload = json.loads(formatter.format(record))

    assert "user_id" not in payload
    assert payload["user_ref"] != "raw-user-identity"
    assert "raw-user-identity" not in json.dumps(payload)
    assert payload["request_id"] == "request-1"


def test_logging_setup_is_idempotent():
    root = logging.getLogger()
    before = sum(bool(getattr(h, "_rentcompass_structured", False)) for h in root.handlers)
    configure_logging()
    configure_logging()
    after = sum(bool(getattr(h, "_rentcompass_structured", False)) for h in root.handlers)

    assert after == max(1, before)
