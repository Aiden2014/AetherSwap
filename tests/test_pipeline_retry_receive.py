from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import pipeline, receive_flow
from app.pipeline_context import PipelineContext
from app.state import State
from buff import BuffRateLimited


@pytest.fixture
def retry_env(monkeypatch, tmp_path):
    from app import config_loader, inventory_cs2, state as state_module
    from app.services import buff_checkout_guard
    from steam.request_policy import steam_market_cooldown

    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")
    monkeypatch.setattr(pipeline, "is_shutdown_pending", lambda: False)
    monkeypatch.setattr(steam_market_cooldown, "remaining", lambda: 0)
    state = State()
    monkeypatch.setattr(state_module, "get_status", state.get_status)
    rows = [{"_db_id": 1, "goods_id": 42, "name": "Test Item", "pending_receipt": True}]
    monkeypatch.setattr(state, "get_purchases", lambda: deepcopy(rows))
    monkeypatch.setattr(state, "update_purchase", lambda *_args: pytest.fail("Use stable DB IDs"))

    def update(db_id, patch):
        assert db_id == 1
        rows[0].update(patch)
        return True

    monkeypatch.setattr(state, "update_purchase_by_id", update)
    monkeypatch.setattr(config_loader, "get_steam_credentials", lambda: {
        "steam_id": "7656", "cookies": "steamLoginSecure=test; sessionid=test",
    })
    accepted, polls, sleeps = [], [], []

    def poll():
        polls.append(state.get_status())
        return [{"state": 1, "tradeofferid": "offer-1", "items_to_trade": [
            {"assetid": "seller-asset", "goods_id": "42"},
        ], "goods_infos": {"42": {"market_hash_name": "Test Item"}}}]

    buyer = SimpleNamespace(get_steam_trades=poll)
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer",
                        lambda offer, _cookies: accepted.append(offer) or True)
    monkeypatch.setattr(receive_flow, "jittered_sleep", lambda *_args: None)
    monkeypatch.setattr(inventory_cs2, "scan_cs2_inventory", lambda: (
        True, [{"assetid": "our-asset", "market_hash_name": "Test Item"}] if accepted else [], "",
    ))
    monkeypatch.setattr("app.pipeline_context.time.sleep", sleeps.append)
    return SimpleNamespace(state=state, ctx=PipelineContext(state, "test"), buyer=buyer,
                           rows=rows, accepted=accepted, polls=polls, sleeps=sleeps)


def test_round_wait_accepts_offer_once_and_updates_inventory(retry_env):
    env = retry_env
    assert pipeline._wait_between_rounds(env.ctx, env.buyer, 300) is False
    assert len(env.polls) == 1
    assert env.polls[0]["status"] == "running"
    assert env.polls[0]["step"] == "WAITING_RETRY"
    assert env.accepted == ["offer-1"]
    assert env.rows[0]["pending_receipt"] is False
    assert env.rows[0]["assetid"] == "our-asset"
    assert env.sleeps == [1] * 300


@pytest.mark.parametrize("has_candidates", [False, True])
def test_pipeline_receives_between_rounds_on_both_retry_paths(retry_env, monkeypatch, has_candidates):
    env = retry_env
    fetches = []

    def fetch(*_args):
        fetches.append(True)
        if len(fetches) == 2:
            env.state.request_stop()
        return ([{"name": "Candidate"}] if has_candidates else []), False

    monkeypatch.setattr(pipeline, "get_state", lambda: env.state)
    monkeypatch.setattr(pipeline, "get_buff_credentials", lambda: {"cookies": "test"})
    monkeypatch.setattr(pipeline, "create_buff_client_from_config", lambda *_args: env.buyer)
    monkeypatch.setattr(pipeline, "apply_strategy_to_config", lambda cfg, _kind: cfg)
    monkeypatch.setattr(pipeline, "get_proxy_manager", lambda: SimpleNamespace(is_proxy_enabled=lambda: False))
    monkeypatch.setattr(pipeline, "get_network_checker", lambda: SimpleNamespace(report_success=lambda: None))
    monkeypatch.setattr(pipeline, "SteamClient", lambda: object())
    monkeypatch.setattr(pipeline, "StabilityAnalyzer", lambda **_kwargs: object())
    monkeypatch.setattr(pipeline, "_fetch_and_filter_deals", fetch)
    monkeypatch.setattr(pipeline, "_process_deals_for_target", lambda *_args, **_kwargs: (0, 0, False))

    pipeline._run_pipeline({"pipeline": {"retry_interval_seconds": 300}})
    assert len(fetches) == 2
    assert len(env.polls) == 1
    assert env.accepted == ["offer-1"]
    assert env.rows[0]["assetid"] == "our-asset"
    assert env.sleeps == [1] * 300
    assert env.state.get_status()["status"] == "stopped"


@pytest.mark.parametrize("checkpoint,step,allowed", [
    (False, "WAITING_RETRY", False),
    (True, "WAITING_RETRY", True),
    (True, "CHECKOUT_PENDING", False),
    (True, "CHECKING_STABILITY", False),
])
def test_only_pipeline_checkpoint_can_poll_during_round_wait(retry_env, checkpoint, step, allowed):
    env = retry_env
    env.state.set_status("running", step)
    ok, _tasks, _error = receive_flow.fetch_buff_steam_trade(
        env.buyer, pipeline_checkpoint=checkpoint,
    )
    assert ok is allowed
    assert bool(env.polls) is allowed


@pytest.mark.parametrize("blocker", ["stop", "shutdown", "checkout", "auth", "verification", "cooldown", "empty"])
def test_round_wait_skips_receiving_when_blocked(retry_env, monkeypatch, blocker):
    from steam.request_policy import steam_market_cooldown

    env = retry_env
    if blocker == "stop":
        env.state.request_stop()
    elif blocker == "shutdown":
        monkeypatch.setattr(pipeline, "is_shutdown_pending", lambda: True)
    elif blocker == "checkout":
        monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: {"unresolved": True})
    elif blocker == "auth":
        env.state.set_buff_auth_expired(True)
    elif blocker == "verification":
        env.state.set_buff_verification_required(True, "test")
    elif blocker == "cooldown":
        monkeypatch.setattr(steam_market_cooldown, "remaining", lambda: 60)
    else:
        env.rows.clear()
    assert pipeline._wait_between_rounds(env.ctx, env.buyer, 300) is (blocker == "stop")
    assert env.polls == []
    assert env.accepted == []


def test_stop_after_inventory_scan_does_not_accept_offer(retry_env, monkeypatch):
    env = retry_env

    def scan():
        env.state.request_stop()
        return True, [], ""

    monkeypatch.setattr("app.inventory_cs2.scan_cs2_inventory", scan)
    assert pipeline._wait_between_rounds(env.ctx, env.buyer, 300) is True
    assert env.accepted == []
    assert env.sleeps == []
    assert env.rows[0]["pending_receipt"] is True


def test_receive_rate_limit_stops_pipeline_with_explicit_status(retry_env, monkeypatch):
    env = retry_env

    def limited():
        raise BuffRateLimited(30)

    env.buyer.get_steam_trades = limited
    monkeypatch.setattr(pipeline, "get_state", lambda: env.state)
    monkeypatch.setattr(pipeline, "_run_pipeline",
                        lambda _cfg: pipeline._wait_between_rounds(env.ctx, env.buyer, 300))
    pipeline._run_pipeline_guarded({})
    assert env.state.get_status()["step"] == "BUFF_RATE_LIMITED"
    assert env.accepted == []
    assert env.sleeps == []


def test_receive_failure_keeps_next_round_wait(retry_env):
    env = retry_env

    def unavailable():
        raise OSError("test connection failure")

    env.buyer.get_steam_trades = unavailable
    assert pipeline._wait_between_rounds(env.ctx, env.buyer, 300) is False
    assert env.sleeps == [1] * 300
    assert env.rows[0]["pending_receipt"] is True
