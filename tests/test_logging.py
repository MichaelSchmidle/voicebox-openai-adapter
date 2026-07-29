from __future__ import annotations

import json
import logging

from voicebox_openai_adapter.logging_config import JsonFormatter, configure_logging


def test_json_formatter_emits_only_structured_metadata() -> None:
    record = logging.LogRecord(
        name="voicebox_openai_adapter",
        level=logging.INFO,
        pathname="not-emitted",
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "synthetic-request-id"
    record.route = "/v1/audio/speech"
    record.status = 200
    record.duration_ms = 12.5

    event = json.loads(JsonFormatter().format(record))

    assert event["event"] == "request_completed"
    assert event["level"] == "INFO"
    assert event["request_id"] == "synthetic-request-id"
    assert event["route"] == "/v1/audio/speech"
    assert event["status"] == 200
    assert event["duration_ms"] == 12.5
    assert "pathname" not in event


def test_configure_logging_installs_non_propagating_json_handler() -> None:
    logger = logging.getLogger("voicebox_openai_adapter")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    try:
        configure_logging("WARNING")

        assert logger.level == logging.WARNING
        assert not logger.propagate
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JsonFormatter)
    finally:
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
