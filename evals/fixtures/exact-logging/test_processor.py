import logging

from processor import process


def test_process_logs_at_info(caplog) -> None:
    with caplog.at_level(logging.INFO):
        assert process(" x ") == "x"
    assert "processing item" in caplog.text

