"""Payment detection must be scoped to the current order and bounded in time."""
import pytest
import requests

from app import pipeline_steps as steps
from app.state import State
from buff.buyer import API_BATCH_CHECK_STATE, API_BILL_ORDER_INFO, BuffBuyer
from buff.request_policy import BuffRateLimited, BuffRequestPolicy


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class ClockCondition:
    def __init__(self, clock):
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def wait(self, timeout):
        self.clock.now += timeout

    def notify_all(self):
        pass


@pytest.fixture
def payment_state(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("app.state.time.monotonic", clock.monotonic)
    state = State()
    state._confirm = ClockCondition(clock)
    state.set_pending_payment({"order_id": "bill-1"})
    return state, clock


@pytest.mark.parametrize("fields,paid", [
    ({"state": "PAYING", "progress": 102}, False),
    ({"state": "SUCCESS"}, True),
    ({"state": "TO_DELIVER", "mode": 2}, True),
    ({"state": "TO_DELIVER", "mode": "5"}, True),
    ({"mode": 5, "progress": 305}, True),
    ({"mode": 8, "progress": "309"}, True),
    ({"mode": 7, "progress": 202}, True),
    ({"state": "FAIL", "progress": 104}, False),
    ({"state": "FAIL", "progress": 105}, False),
    ({"state": "FAIL", "progress": 106}, False),
    ({"state": "TO_DELIVER", "mode": 999}, False),
    ({"state": "UNKNOWN"}, False),
    ({}, False),
])
def test_single_status_uses_cashier_contract_and_exact_bill(monkeypatch, fields, paid):
    buyer = BuffBuyer("", request_policy=BuffRequestPolicy(min_interval=0, persist=False))
    calls = []

    def query(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK", "data": {"items": [dict(id="bill-1", **fields)]}}

    monkeypatch.setattr(buyer, "_make_request", query)
    try:
        assert buyer.is_order_paid("bill-1") is paid
        assert calls[0][0:2] == ("GET", API_BILL_ORDER_INFO)
        assert calls[0][2]["params"]["bill_orders"] == "bill-1"
        assert buyer.is_order_paid("different-bill") is False
    finally:
        buyer.close()


@pytest.mark.parametrize("payload", [
    {"code": "ERROR", "data": {"items": [{"id": "bill-1", "state": "SUCCESS"}]}},
    {"code": "OK", "data": None},
    {"code": "OK", "data": {"items": []}},
    {"code": "OK", "data": {"items": [None]}},
    {"code": "OK", "data": {"items": [{"state": "SUCCESS"}]}},
    {"code": "OK", "data": {"items": [{"id": "bill-1", "state": "SUCCESS"}] * 2}},
])
def test_missing_or_ambiguous_bill_is_never_paid(monkeypatch, payload):
    buyer = BuffBuyer("", request_policy=BuffRequestPolicy(min_interval=0, persist=False))
    monkeypatch.setattr(buyer, "_make_request", lambda *_a, **_k: payload)
    try:
        assert buyer.is_order_paid("bill-1") is False
    finally:
        buyer.close()


@pytest.mark.parametrize("status,paid", [(1, False), (2, True), ("2", True), (3, False), (None, False)])
def test_batch_status_is_read_only_and_does_not_authorize_finalize(monkeypatch, status, paid):
    buyer = BuffBuyer("", request_policy=BuffRequestPolicy(min_interval=0, persist=False))
    buyer._batch_context = {"funding_id": "batch-1", "steamid": buyer.steam_id,
                            "goods_id": 42, "game": "csgo", "paid": False}
    calls = []

    def query(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK", "data": {"state": status}}

    monkeypatch.setattr(buyer, "_make_request", query)
    try:
        assert buyer.is_batch_paid("batch-1") is paid
        assert calls[0][:2] == ("GET", API_BATCH_CHECK_STATE)
        assert calls[0][2]["params"]["batch_buy_id"] == "batch-1"
        assert buyer._batch_context["paid"] is False
    finally:
        buyer.close()


def test_polling_stops_after_five_minutes_even_with_longer_wait(payment_state):
    state, clock = payment_state
    calls = []

    def check():
        calls.append(clock.now)
        return False

    assert state.wait_payment_confirm(timeout_seconds=3600, payment_checker=check) is False
    assert calls == list(range(0, 300, 5))
    assert clock.now == 300


def test_payment_is_detected_and_no_more_queries_are_scheduled(payment_state):
    state, clock = payment_state
    calls = []

    def check():
        calls.append(clock.now)
        return len(calls) == 3

    assert state.wait_payment_confirm(payment_checker=check) is True
    assert calls == [0, 5, 10]


def test_slow_reads_never_overlap_or_schedule_after_deadline(payment_state):
    state, clock = payment_state
    calls = []

    def check():
        calls.append(clock.now)
        clock.now += 2
        return False

    assert state.wait_payment_confirm(payment_checker=check) is False
    assert calls == list(range(0, 300, 7))
    assert clock.now == 300


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_cancel_or_stop_during_query_wins_over_payment_result(payment_state, action):
    state, _clock = payment_state

    def check():
        if action == "cancel":
            state.confirm_payment(False)
        else:
            state.request_stop()
        return True

    assert state.wait_payment_confirm(payment_checker=check) is False


def test_confirmation_before_wait_is_preserved_and_next_order_resets_it(payment_state):
    state, _clock = payment_state
    state.confirm_payment(True)
    assert state.wait_payment_confirm(timeout_seconds=0) is True
    state.set_pending_payment({"order_id": "bill-2"})
    assert state.wait_payment_confirm(timeout_seconds=0) is False


def wait_for_payment(state, checker, *, config=None, logs=None):
    return steps._do_payment_notify_and_wait(
        {"name": "item"}, config or {}, 10, 1, "https://pay.invalid", "alipay", "bill-1", 0,
        state.set_pending_payment, state.wait_payment_confirm, state.confirm_payment,
        state.is_stop_requested, (lambda msg, level: logs.append((msg, level))) if logs is not None else None,
        payment_checker=checker,
    )


def test_network_error_retries_then_continues_without_manual_confirmation(payment_state):
    state, clock = payment_state
    calls, logs = [], []

    def check():
        calls.append(clock.now)
        if len(calls) == 1:
            raise requests.Timeout("network")
        return True

    assert wait_for_payment(state, check, logs=logs) is True
    assert calls == [0, 5]
    assert state.get_pending_payment() is None
    assert any("自动继续" in msg for msg, _ in logs)


def test_rate_limit_stops_polling_and_clears_prompt(payment_state):
    state, _clock = payment_state
    calls = []

    def check():
        calls.append(1)
        raise BuffRateLimited("cooling down")

    with pytest.raises(BuffRateLimited):
        wait_for_payment(state, check)
    assert calls == [1]
    assert state.get_pending_payment() is None


def test_late_email_cannot_confirm_next_order(monkeypatch, payment_state):
    state, _clock = payment_state
    email_tasks = []

    class DeferredThread:
        def __init__(self, target, **_kwargs):
            email_tasks.append(target)

        def start(self):
            pass

    monkeypatch.setattr(steps.threading, "Thread", DeferredThread)
    monkeypatch.setattr(steps, "wait_email_command", lambda *_a, **_k: "success")
    assert wait_for_payment(state, lambda: True, config={"notify": {"email_user": "u", "email_pass": "p"}})
    state.set_pending_payment({"order_id": "bill-2"})
    email_tasks[0]()
    assert state.wait_payment_confirm(timeout_seconds=0) is False


def test_single_pipeline_records_and_prompts_shipping_after_automatic_detection(monkeypatch, payment_state):
    state, clock = payment_state
    calls, purchases = [], []

    class Client:
        def is_order_paid(self, order_id, game):
            calls.append(("check", order_id, game, clock.now))
            return clock.now >= 5

        def ask_seller_to_send(self, order_id, game):
            calls.append(("ship", order_id, game))
            assert len(purchases) == 1
            return True

    monkeypatch.setattr(steps, "update_checkout", lambda **_k: None)
    monkeypatch.setattr(steps, "resolve_checkout", lambda *_a, **_k: None)
    result = steps._do_wait_payment_and_append(
        Client(), {"name": "item"}, {}, 10, 1, 42, "https://pay.invalid", "alipay", "bill-1", "intent-1", 0,
        state.set_pending_payment, state.wait_payment_confirm, state.confirm_payment,
        state.is_stop_requested, purchases.append, None, "csgo", market_price=12,
    )
    assert result == 10
    assert calls == [("check", "bill-1", "csgo", 0), ("check", "bill-1", "csgo", 5), ("ship", "bill-1", "csgo")]
    assert purchases[0]["buff_order_id"] == "bill-1"


def test_batch_pipeline_detects_payment_before_finalizing(monkeypatch, payment_state):
    state, clock = payment_state
    calls, purchases = [], []

    class Client:
        def is_batch_paid(self, batch_id, game):
            calls.append(("check", batch_id, game, clock.now))
            return clock.now >= 5

        def batch_buy_find_and_finalize(self, goods_id, game, price, num, batch_id):
            calls.append(("finalize", batch_id))
            assert clock.now == 5
            return [{"id": "sell-1", "price": price, "bill_order_id": "bill-1"}]

        def ask_seller_to_send(self, bill_ids, game):
            assert len(purchases) == 1
            calls.append(("ship", bill_ids, game))
            return True

    monkeypatch.setattr(steps, "_ensure_checkout_identity_unchanged", lambda *_a: None)
    monkeypatch.setattr(steps, "update_checkout", lambda **_k: None)
    monkeypatch.setattr(steps, "resolve_checkout", lambda *_a, **_k: None)
    result = steps._do_batch_wait_finalize_and_append(
        Client(), {"name": "item"}, {}, 10, 1, 42, "batch-1", "intent-1", "csgo", "https://pay.invalid", 0,
        state.set_pending_payment, state.wait_payment_confirm, state.confirm_payment,
        state.is_stop_requested, purchases.append, None, market_price=12, pay_type="alipay",
    )
    assert result == 10
    assert calls == [("check", "batch-1", "csgo", 0), ("check", "batch-1", "csgo", 5),
                     ("finalize", "batch-1"), ("ship", ["bill-1"], "csgo")]
    assert purchases[0]["buff_order_id"] == "bill-1"
