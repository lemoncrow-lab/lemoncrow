from lemoncrow.core.capabilities.reporting.dashboard import _dur


def test_dur_valid_dates():
    t0 = "2024-01-01T00:00:00"
    t1 = "2024-01-01T00:01:05"
    assert _dur(t0, t1) == "1m05s"


def test_dur_empty_t0():
    assert _dur("", "2024-01-01T00:01:05") == ""


def test_dur_empty_t1():
    assert _dur("2024-01-01T00:00:00", "") == ""


def test_dur_both_empty():
    assert _dur("", "") == ""
