"""Replay Steam's captured 429/null response through candidate selection."""

from types import SimpleNamespace
from unittest.mock import Mock

import requests
import pytest

from app import pipeline_steps as steps
from app.services import steam_client as service
from steam import client
from steam.request_policy import MarketCooldown
from utils import proxy_manager


@pytest.fixture
def environment(monkeypatch):
    reply = requests.Response()
    reply.status_code = 429
    reply._content = b"null"
    reply.headers.update({"Content-Type": "application/json", "Retry-After": "60"})
    now = [1000.0]
    send = Mock(return_value=reply)
    monkeypatch.setattr(client, "get_market", send)
    monkeypatch.setattr(service, "_history_cache", {})
    monkeypatch.setattr(service, "_history_cooldown", MarketCooldown(lambda: now[0]))
    monkeypatch.setattr(service.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(service, "get_steam_credentials", lambda: {"cookies": "sessionid=test"})
    monkeypatch.setattr(service, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: SimpleNamespace(
        get_proxies_for_request=lambda **_kwargs: {"https": "http://test-proxy:8080"},
        is_proxy_enabled=lambda: True,
    ))
    status = Mock()
    monkeypatch.setattr(steps, "set_status", status)
    monkeypatch.setattr(steps, "jittered_sleep", Mock())
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(steps.time, "sleep", sleep)
    messages = []
    config = {
        "pipeline": {},
        "stability": {"request_interval_seconds": 0},
        "_strategy_runtime": {"buy": {"enabled_modules": ["guard.history_data_window"]}},
    }
    items = [{"name": "Prisma Case", "goods_id": 1}, {"name": "Next Case", "goods_id": 2}]
    return SimpleNamespace(send=send, reply=reply, now=now, status=status,
                           sleeps=sleeps, messages=messages, config=config, items=items)


def test_rate_limited_candidate_is_not_rejected_as_missing_history(environment):
    selected, rejected = steps.pick_stable_item(
        environment.items, environment.config, service.SteamClient(), object(),
        is_stop_requested=lambda: environment.send.called,
        log_fn=lambda msg, _level="info": environment.messages.append(msg),
    )

    assert selected is None
    assert rejected == set(), "HTTP 429 must not classify a candidate as lacking price history"
    assert environment.send.call_count == 1
    assert not any("无历史数据或请求失败" in message for message in environment.messages)


def test_rate_limit_waits_then_retries_same_candidate(monkeypatch, environment):
    success = requests.Response()
    success.status_code = 200
    success._content = b'{"success":true,"prices":[["Oct 29 2026 01: +0",10.5,"2"]],"price_prefix":"$"}'
    success.headers["Content-Type"] = "application/json"
    environment.send.side_effect = [environment.reply, success]
    analyzer = SimpleNamespace(analyze=Mock(return_value={"valid": True, "is_stable": True}))
    monkeypatch.setattr(steps, "_fetch_steam_sell_data", Mock(return_value=None))
    monkeypatch.setattr(steps, "_passes_custom_buy_modules", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(steps, "_passes_final_buff_precheck", lambda *_args, **_kwargs: True)

    selected, rejected = steps.pick_stable_item(
        environment.items, environment.config, service.SteamClient(), analyzer,
        is_stop_requested=lambda: False,
        log_fn=lambda msg, _level="info": environment.messages.append(msg),
    )

    assert selected is environment.items[0]
    assert rejected == set()
    assert environment.send.call_count == 2
    assert sum(environment.sleeps) == 60
    assert all(0 < seconds <= 1 for seconds in environment.sleeps)
    assert any(call.kwargs.get("step") == "STEAM_COOLDOWN" for call in environment.status.call_args_list)
    assert all("Prisma%20Case" in call.args[0] for call in environment.send.call_args_list)


def test_stop_during_cooldown_does_not_make_another_request(environment):
    stopped = lambda: bool(environment.sleeps)
    selected, rejected = steps.pick_stable_item(
        environment.items, environment.config, service.SteamClient(), object(),
        is_stop_requested=stopped,
        log_fn=lambda msg, _level="info": environment.messages.append(msg),
    )

    assert selected is None
    assert rejected == set()
    assert environment.send.call_count == 1
    assert sum(environment.sleeps) <= 1


def test_existing_cooldown_waits_before_market_depth_requests(monkeypatch, environment):
    service._history_cooldown.defer(60)
    environment.config["_strategy_runtime"]["buy"]["enabled_modules"] += ["buy.steam_sell_depth", "guard.max_discount"]
    environment.config["pipeline"]["max_discount"] = 0.9
    market = Mock(return_value=(None, "unavailable"))
    monkeypatch.setattr(steps, "_fetch_steam_sell_data", market)
    selected, rejected = steps.pick_stable_item(
        environment.items, environment.config, service.SteamClient(), object(),
        is_stop_requested=lambda: bool(environment.sleeps),
    )

    assert selected is None
    assert rejected == set()
    market.assert_not_called()
    environment.send.assert_not_called()


def test_genuinely_empty_history_can_reject_candidate(environment):
    environment.reply.status_code = 200
    environment.reply._content = b'{"success":true,"prices":[]}'
    selected, rejected = steps.pick_stable_item(
        environment.items, environment.config, service.SteamClient(), object(),
        is_stop_requested=lambda: False,
        log_fn=lambda msg, _level="info": environment.messages.append(msg),
    )

    assert selected is None
    assert rejected == {1, 2}
    assert environment.send.call_count == 2
    assert not environment.sleeps


def test_market_depth_rate_limit_also_retries_same_candidate(monkeypatch, environment):
    environment.config["_strategy_runtime"]["buy"]["enabled_modules"] += ["buy.steam_sell_depth", "guard.max_discount"]
    environment.config["pipeline"]["max_discount"] = 0.95
    environment.items[0]["min_price"] = 8
    environment.reply.status_code = 200
    environment.reply._content = b'{"success":true,"prices":[["Oct 29 2026 01: +0",10.5,"2"]]}'
    names = []

    def depth(name, *_args, **_kwargs):
        names.append(name)
        if len(names) == 1:
            service._history_cooldown.defer(60)
            return None, "Steam 市场 HTTP 429"
        return {"sell_orders": [(10, 20)], "smart_price": 10}, None

    monkeypatch.setattr(steps, "_fetch_steam_sell_data", depth)
    monkeypatch.setattr(steps, "_adjust_ref_price_for_daily_high", lambda _name, price, *_args, **_kwargs: price)
    monkeypatch.setattr(steps, "_passes_custom_buy_modules", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(steps, "_passes_final_buff_precheck", lambda *_args, **_kwargs: True)
    analyzer = SimpleNamespace(analyze=Mock(return_value={"valid": True, "is_stable": True}))

    selected, rejected = steps.pick_stable_item(
        environment.items, environment.config, service.SteamClient(), analyzer,
        is_stop_requested=lambda: False,
    )

    assert selected is environment.items[0]
    assert rejected == set()
    assert names == ["Prisma Case", "Prisma Case"]
    assert sum(environment.sleeps) == 60


def test_persistent_429_stops_instead_of_retrying_forever(environment):
    # A fourth response ends the old implementation deterministically.
    with pytest.raises(RuntimeError, match="HTTP 429"):
        steps.pick_stable_item(
            environment.items, environment.config, service.SteamClient(), object(),
            is_stop_requested=lambda: environment.send.call_count >= 4,
        )

    assert environment.send.call_count == 3


def test_cooldown_countdown_preserves_candidate_progress(monkeypatch, environment):
    from app.state import State

    state = State()
    state.set_status("running", "STABILITY_CHECK", progress_total=45, progress_done=8,
                     progress_item="(9/45) Prisma Case", next_progress_item="(10/45) Next Case")
    monkeypatch.setattr(steps, "get_status", state.get_status, raising=False)
    monkeypatch.setattr(steps, "set_status", state.set_status)
    service._history_cooldown.defer(60)

    steps._wait_for_steam_cooldown(lambda: bool(environment.sleeps), None, "(9/45) Prisma Case")

    assert state.get_status()["progress_total"] == 45
    assert state.get_status()["progress_done"] == 8
    assert state.get_status()["next_progress_item"] == "(10/45) Next Case"


def test_buying_task_stops_with_explicit_rate_limit_status(monkeypatch, environment):
    from app import pipeline
    from app.pipeline_context import PipelineContext
    from app.state import State

    state = State()
    ctx = PipelineContext(state, "test-rate-limit")
    checkout = Mock()
    monkeypatch.setattr(pipeline, "lock_and_confirm_payment", checkout)
    failed = set()

    result = pipeline._process_deals_for_target(
        ctx, environment.items, environment.config, 100.0, 23.0, 2,
        service.SteamClient(), object(), object(), failed, set(), set(),
    )

    assert result == (23.0, 2, True)
    assert environment.send.call_count == 3
    assert state.get_status()["status"] == "error"
    assert state.get_status()["step"] == "STEAM_RATE_LIMITED"
    assert not failed
    checkout.assert_not_called()
