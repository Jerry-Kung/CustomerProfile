from __future__ import annotations

import logging

from src.config.observability import LogContextFilter, log_context, mask_phone, safe_text


def test_mask_phone_hides_middle_digits():
    assert mask_phone("13800138000") == "138****8000"
    assert "13800138000" not in mask_phone("13800138000")


def test_log_context_filter_sets_and_resets_fields():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    filter_ = LogContextFilter()

    with log_context(request_id="req-1", task_id=42, task_type="customer_profile"):
        assert filter_.filter(record)
        assert record.request_id == "req-1"
        assert record.task_id == "42"
        assert record.task_type == "customer_profile"

    reset_record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    filter_.filter(reset_record)
    assert reset_record.request_id == "-"
    assert reset_record.task_id == "-"


def test_safe_text_removes_newlines_and_truncates():
    assert safe_text("first\nsecond", 20) == "first second"
    assert safe_text("abcdefgh", 4) == "abcd...(truncated)"
