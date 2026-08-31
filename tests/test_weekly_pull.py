"""Weekly-pull orchestrator tests: per-stage error isolation, skip list, and
the append-only run log."""

from __future__ import annotations

from grocery_optimizer.scripts import weekly_pull


def _fake_stages(calls):
    def ok():
        calls.append("ok_stage")

    def boom():
        raise RuntimeError("chain outage")

    def after():
        calls.append("after_stage")

    return [("ok_stage", ok), ("boom_stage", boom), ("after_stage", after)]


def test_failure_is_isolated_and_later_stages_still_run(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(weekly_pull, "_stages", lambda: _fake_stages(calls))
    monkeypatch.setattr(weekly_pull, "LOG_DIR", tmp_path)

    results = weekly_pull.run_weekly_pull()

    assert calls == ["ok_stage", "after_stage"]  # boom didn't block after
    assert results["ok_stage"] == "ok"
    assert results["boom_stage"].startswith("FAILED")
    assert "chain outage" in results["boom_stage"]
    assert results["after_stage"] == "ok"

    log = (tmp_path / "weekly_pull.log").read_text(encoding="utf-8")
    assert "boom_stage: FAILED" in log
    assert log.count("ok in") == 2


def test_skip_list(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(weekly_pull, "_stages", lambda: _fake_stages(calls))
    monkeypatch.setattr(weekly_pull, "LOG_DIR", tmp_path)

    results = weekly_pull.run_weekly_pull(skip={"boom_stage", "after_stage"})

    assert calls == ["ok_stage"]
    assert results["boom_stage"] == "skipped"
    assert results["after_stage"] == "skipped"
