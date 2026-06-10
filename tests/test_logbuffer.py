"""The in-memory log ring buffer behind /ui/logs."""

import logging

from wiki_server import logbuffer


def test_captures_logs_newest_first():
    logbuffer._BUFFER.clear()
    logbuffer.setup_logging()
    log = logging.getLogger("wiki_server.test")
    log.info("first")
    log.warning("second")
    recent = logbuffer.recent_logs()
    assert recent[0]["msg"] == "second" and recent[0]["level"] == "WARNING"
    assert recent[1]["msg"] == "first"
    assert recent[0]["name"] == "wiki_server.test"


def test_captures_exception_traceback():
    logbuffer._BUFFER.clear()
    logbuffer.setup_logging()
    log = logging.getLogger("wiki_server.test")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("it failed")
    msg = logbuffer.recent_logs()[0]["msg"]
    assert "it failed" in msg and "RuntimeError: boom" in msg


def test_buffer_is_bounded():
    logbuffer._BUFFER.clear()
    logbuffer.setup_logging()
    log = logging.getLogger("wiki_server.test")
    for i in range(600):
        log.info("m%d", i)
    assert len(logbuffer._BUFFER) == logbuffer._BUFFER.maxlen  # capped at 500
