"""Explicit resume may recheck only the old, generic verification record."""
import json
import threading

import pytest
import requests

from app.services import buff_auth, buff_checkout_guard
from buff.buyer import API_USER_INFO, API_PAGE_PAY, API_BUY, BuffBuyer
from buff.request_policy import (
    LEGACY_VERIFICATION_MESSAGE, BuffRequestPolicy, BuffRateLimited,
    BuffRiskControlTriggered, BuffVerificationRequired, BuffRequestBlocked,
    account_fingerprint,
)


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    import buff

    policy = BuffRequestPolicy(min_interval=0, state_path=tmp_path / "policy.json")
    key = account_fingerprint({}, account_id="default")
    policy.trip_verification(key, LEGACY_VERIFICATION_MESSAGE)
    monkeypatch.setattr(buff, "get_global_policy", lambda: policy)
    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")
    state = {"status": "idle", "step": "", "buff_verification_required": True}
    credentials = {"cookies": "session=current; csrf_token=c", "user_agent": "Current Browser"}
    monkeypatch.setattr(buff_auth, "get_status", lambda: dict(state))
    monkeypatch.setattr(buff_auth, "get_pending_payment", lambda: None)
    monkeypatch.setattr(buff_auth, "get_buff_credentials", lambda: credentials.copy())
    monkeypatch.setattr(buff_auth, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(buff_auth, "set_buff_verification_required",
                        lambda value, reason="": state.update(buff_verification_required=value, buff_verification_reason=reason))
    monkeypatch.setattr(buff_auth, "set_buff_auth_expired", lambda value: state.update(buff_auth_expired=value))
    monkeypatch.setattr(buff_auth, "update_buff_creds", lambda value, **kwargs: credentials.update(cookies=value))
    return policy, key, state, credentials


def install_response(monkeypatch, payload=None, status=200, error=None):
    calls = []

    def request(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert (method, url) == ("GET", API_USER_INFO)
        if error:
            raise error
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.encoding = "utf-8"
        response._content = json.dumps(payload).encode()
        response.headers["Retry-After"] = "60"
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return calls


def test_resume_validates_saved_session_once_then_clears_only_legacy_flag(monkeypatch, recovery):
    policy, key, state, _ = recovery
    calls = install_response(monkeypatch, {"code": "OK", "data": {"user_info": {"id": "u", "risk_control": False}}})
    assert buff_auth.recheck_legacy_buff_verification() is True
    assert state["buff_verification_required"] is False
    policy.raise_if_blocked(key)
    assert buff_auth.recheck_legacy_buff_verification() is False
    assert len(calls) == 1
    assert key in policy._last_started
    restored = BuffRequestPolicy(state_path=policy.state_path)
    restored.raise_if_blocked(key)


@pytest.mark.parametrize("payload,status,error,expected", [
    ({"code": "FAIL", "msg": "captcha required"}, 200, None, BuffVerificationRequired),
    ({"code": "LOGIN_REQUIRED"}, 401, None, BuffVerificationRequired),
    ({"code": "OK", "data": {"user_info": {}}}, 200, None, BuffVerificationRequired),
    ({"code": "FAIL"}, 403, None, BuffRiskControlTriggered),
    ({"code": "FAIL"}, 412, None, BuffRiskControlTriggered),
    ({"code": "FAIL"}, 429, None, BuffRateLimited),
    ({"code": "OK", "data": {"user_info": {"id": "u"}}}, 500, None, BuffVerificationRequired),
    (None, 200, requests.Timeout("probe timed out"), BuffVerificationRequired),
])
def test_failed_probe_preserves_circuit_and_never_creates_order(monkeypatch, recovery, payload, status, error, expected):
    policy, key, state, _ = recovery
    calls = install_response(monkeypatch, payload, status, error)
    assert buff_auth.recheck_legacy_buff_verification() is False
    with pytest.raises(expected):
        policy.raise_if_blocked(key)
    assert len(calls) == 1
    if status == 401:
        assert state["buff_auth_expired"] is True


@pytest.mark.parametrize("blocker", ["captcha", "rate", "risk", "invalid", "checkout", "running", "pending", "expired"])
def test_other_blockers_never_allow_an_http_probe(monkeypatch, recovery, blocker):
    policy, key, state, _ = recovery
    if blocker == "captcha":
        policy.trip_verification(key, "captcha required")
    elif blocker == "rate":
        policy.trip_rate_limit(key, 60)
    elif blocker == "risk":
        policy.trip_risk_control(key, status_code=403)
    elif blocker == "invalid":
        policy._state_invalid = True
    elif blocker == "checkout":
        buff_checkout_guard.begin_checkout("single", 1)
    elif blocker == "running":
        state["status"] = "running"
    elif blocker == "pending":
        monkeypatch.setattr(buff_auth, "get_pending_payment", lambda: {"order_id": "bill-1"})
    elif blocker == "expired":
        state["buff_auth_expired"] = True
    calls = install_response(monkeypatch, {"code": "OK", "data": {"user_info": {"id": "u"}}})
    assert buff_auth.recheck_legacy_buff_verification() is False
    assert calls == []
    with pytest.raises(BuffRequestBlocked):
        policy.raise_if_blocked(key)


@pytest.mark.parametrize("method,url", [("POST", API_USER_INFO), ("POST", API_BUY), ("GET", API_PAGE_PAY)])
def test_legacy_probe_cannot_be_used_for_a_payment_or_write(monkeypatch, recovery, method, url):
    policy, key, _, credentials = recovery
    calls = install_response(monkeypatch)
    buyer = BuffBuyer(credentials["cookies"], account_id="default", request_policy=policy)
    try:
        with pytest.raises(BuffRequestBlocked):
            buyer._make_request(method, url, legacy_verification_probe=True)
        assert calls == []
    finally:
        buyer.close()


def test_probe_respects_global_pacing(monkeypatch, recovery):
    policy, key, _, _ = recovery
    sleeps = []
    policy.min_interval = 2
    policy._clock = lambda: 101
    policy._sleep = sleeps.append
    policy._last_started[key] = 100
    install_response(monkeypatch, {"code": "OK", "data": {"user_info": {"id": "u"}}})
    assert buff_auth.recheck_legacy_buff_verification() is True
    assert sleeps == [1]
    assert policy._last_started[key] == 101


def test_explicit_start_clears_legacy_flag_before_starting_thread(monkeypatch, recovery):
    from app import pipeline
    from app.routes import pipeline as routes

    policy, key, state, _ = recovery
    state_object = type("State", (), {
        "get_status": lambda self: dict(state),
        "set_status": lambda self, status, step="", **kw: state.update(status=status, step=step),
    })()
    monkeypatch.setattr(pipeline, "get_state", lambda: state_object)
    monkeypatch.setattr(pipeline, "_pipeline_thread", None)
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    ran = threading.Event()
    monkeypatch.setattr(pipeline, "_run_pipeline_guarded", lambda _: ran.set())
    calls = install_response(monkeypatch, {"code": "OK", "data": {"user_info": {"id": "u"}}})
    assert routes.api_pipeline_start(routes.ConfigBody(config={})) == {"ok": True}
    assert ran.wait(timeout=1)
    assert state["status"] == "running"
    assert state["buff_verification_required"] is False
    assert len(calls) == 1
