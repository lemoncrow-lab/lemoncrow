from __future__ import annotations

from atelier.core.capabilities.tool_supervision.anomaly import ToolAnomalyDetector


def test_z_score_fires_on_sudden_spike() -> None:
    """A tool that is quiet then spikes should produce a non-None Z-score alert."""
    det = ToolAnomalyDetector()

    # Establish a calm baseline for "spike" across many batches: it appears
    # at most once per 5-call batch alongside steady background traffic.
    for _ in range(40):
        det.record("spike")
        for _ in range(4):
            det.record("background")

    # Now a burst of "spike" calls — should stand out vs its own history.
    for _ in range(20):
        det.record("spike")

    z = det.z_score("spike")
    assert z is not None
    assert z > 0

    alerts = det.detect_anomalies()
    assert any(a.tool == "spike" and a.z_score and a.z_score > 0 for a in alerts)


def test_summary_reports_total_calls_and_z_score() -> None:
    det = ToolAnomalyDetector()
    for _ in range(30):
        det.record("grep")
    summary = det.summary()
    assert summary["grep"]["total_calls"] == 30
    # Steady uniform traffic should not be a non-None outlier signal here,
    # but the key must exist and be either None or a float.
    assert "z_score" in summary["grep"]
