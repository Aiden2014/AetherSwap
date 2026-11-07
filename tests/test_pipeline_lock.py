import threading

import pytest


@pytest.fixture(autouse=True)
def _isolated_buff_checkout(monkeypatch, tmp_path):
    import buff
    from app.services import buff_checkout_guard

    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")
    policy = buff.BuffRequestPolicy(min_interval=0, persist=False)
    monkeypatch.setattr(buff, "get_global_policy", lambda: policy)


def test_start_pipeline_rejects_duplicate_running_pipeline(monkeypatch):
    from app import pipeline

    started = threading.Event()
    release = threading.Event()

    def fake_run_pipeline(config):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(pipeline, "_run_pipeline", fake_run_pipeline)
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None

    try:
        assert pipeline.start_pipeline({"pipeline": {"target_balance": 1}}) is True
        assert started.wait(timeout=1)
        assert pipeline.is_pipeline_running() is True
        assert pipeline.start_pipeline({"pipeline": {"target_balance": 2}}) is False
    finally:
        with pipeline._pipeline_start_lock:
            thread = pipeline._pipeline_thread
        release.set()
        if thread is not None:
            thread.join(timeout=2)
        with pipeline._pipeline_start_lock:
            pipeline._pipeline_thread = None


def test_start_pipeline_allows_restart_after_thread_exits(monkeypatch):
    from app import pipeline

    calls = []

    def fake_run_pipeline(config):
        calls.append(config)

    monkeypatch.setattr(pipeline, "_run_pipeline", fake_run_pipeline)
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None

    assert pipeline.start_pipeline({"run": 1}) is True
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread is not None:
        thread.join(timeout=2)

    assert pipeline.is_pipeline_running() is False
    assert pipeline.start_pipeline({"run": 2}) is True
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread is not None:
        thread.join(timeout=2)

    assert calls == [{"run": 1}, {"run": 2}]
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None
