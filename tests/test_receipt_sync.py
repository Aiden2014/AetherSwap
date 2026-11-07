"""Reconcile external receipts without accepting offers or guessing assets."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import receipt_sync
from app.pipeline_context import PipelineContext
from app.state import State
from buff import BuffRateLimited
from buff.buyer import API_BILL_ORDER_INFO, BuffBuyer
from buff.request_policy import BuffRequestPolicy


@pytest.fixture(autouse=True)
def idle_app(monkeypatch, tmp_path):
    from app import pipeline, state
    from app.services import buff_checkout_guard

    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")
    monkeypatch.setattr(pipeline, "is_shutdown_pending", lambda: False)
    status = {"status": "idle", "step": ""}
    monkeypatch.setattr(state, "get_status", lambda: dict(status))
    return status


def purchase(db_id=1, **fields):
    return dict(_db_id=db_id, buff_order_id=f"bill-{db_id}", goods_id=42,
                name="Test Item", at=float(db_id), pending_receipt=True, **fields)


def completed(bill="bill-1", **fields):
    result = dict(id=bill, state="SUCCESS", buyer_steamid="7656", goods_id=42,
                  asset_info={"assetid": "seller-asset"})
    result.update(fields)
    return result


class Store:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def get(self):
        return deepcopy(self.rows)

    def update(self, db_id, patch):
        self.updates.append((db_id, patch))
        for row in self.rows:
            if row["_db_id"] == db_id:
                row.update(patch)
                return True
        return False


def reconcile(store, orders, **kwargs):
    calls = []

    def query(ids, game):
        calls.append((ids, game))
        return orders(ids) if callable(orders) else orders

    count = receipt_sync.sync_received_purchases(
        SimpleNamespace(get_bill_orders=query), store.get, store.update,
        "7656", **kwargs,
    )
    return count, calls


def test_completed_bill_clears_pending_without_assigning_seller_asset():
    store = Store([purchase()])
    count, calls = reconcile(store, [completed()], scan_inventory=lambda: (
        True, [{"assetid": "old-personal-item", "market_hash_name": "Test Item"}], "",
    ))
    assert count == 1
    assert calls == [(["bill-1"], "csgo")]
    assert store.updates == [(1, {"pending_receipt": False})]
    assert "assetid" not in store.rows[0]
    assert reconcile(store, [completed()])[0] == 0


@pytest.mark.parametrize("orders", [
    None, [], {}, [None], [completed("other-bill")],
    [completed(state="TO_DELIVER")], [completed(state="FAIL")],
    [completed(progress=105)], [completed(progress="106")],
    [completed(buyer_steamid="another-account")],
    [completed(buyer_steamid=None)], [completed(goods_id=99)],
    [completed(), completed()],
])
def test_unconfirmed_or_unrelated_orders_preserve_pending(orders):
    store = Store([purchase()])
    assert reconcile(store, orders)[0] == 0
    assert store.updates == []


@pytest.mark.parametrize("inventory", [None, [], [
    {"assetid": "seller-asset", "market_hash_name": "Other Item"},
]])
def test_missing_or_mismatched_inventory_does_not_block_receipt(inventory):
    store = Store([purchase()])
    assert reconcile(store, [completed()], scan_inventory=lambda: (
        inventory is not None, inventory or [], "unavailable",
    ))[0] == 1
    assert store.updates == [(1, {"pending_receipt": False})]


def test_inventory_exception_does_not_discard_confirmed_receipt():
    def unavailable():
        raise RuntimeError("Steam is unavailable")

    store = Store([purchase()])
    assert reconcile(store, [completed()], scan_inventory=unavailable)[0] == 1
    assert store.updates == [(1, {"pending_receipt": False})]


def test_later_pass_can_attach_exact_inventory_asset():
    row = purchase()
    row["pending_receipt"] = False
    store = Store([row])
    assert reconcile(store, [completed()], scan_inventory=lambda: (
        True, [{"assetid": "seller-asset", "market_hash_name": "Test Item"}], "",
    ))[0] == 1
    assert store.updates == [(1, {"assetid": "seller-asset"})]


def test_new_receiver_asset_is_matched_by_full_wear_not_same_name_order():
    store = Store([purchase(), purchase(2)])
    orders = [completed(asset_info={"assetid": "old-1", "classid": "100", "paintwear": "0.3150864839553833"}),
              completed("bill-2", asset_info={"assetid": "old-2", "classid": "100", "paintwear": "0.31521809101104736"})]
    items = [{"assetid": "new-2", "market_hash_name": "Test Item", "classid": "100", "paintwear": "0.315218091011047363"},
             {"assetid": "new-1", "market_hash_name": "Test Item", "classid": "100", "paintwear": "0.315086483955383301"}]
    assert reconcile(store, orders, scan_inventory=lambda: (True, items, ""))[0] == 2
    assert [row["assetid"] for row in store.rows] == ["new-1", "new-2"]


@pytest.mark.parametrize("ambiguity", ["inventory", "orders", "wrong_class", "rounded_wear", "nan", "no_class"])
def test_ambiguous_or_weak_wear_match_is_never_assigned(ambiguity):
    store = Store([purchase()])
    asset = {"assetid": "old", "classid": "100", "paintwear": "0.3150864839553833"}
    orders = [completed(asset_info=asset)]
    items = [{"assetid": "new", "market_hash_name": "Test Item", "classid": "100", "paintwear": asset["paintwear"]}]
    if ambiguity == "inventory":
        items.append(dict(items[0], assetid="other-new"))
    elif ambiguity == "orders":
        store.rows.append(purchase(2))
        orders.append(completed("bill-2", asset_info=dict(asset, assetid="other-old")))
    elif ambiguity == "wrong_class":
        items[0]["classid"] = "200"
    elif ambiguity == "rounded_wear":
        items[0]["paintwear"] = "0.3150865"
    elif ambiguity == "nan":
        items[0]["paintwear"] = asset["paintwear"] = "nan"
    else:
        items[0].pop("classid")
    reconcile(store, orders, scan_inventory=lambda: (True, items, ""))
    assert not any(row.get("assetid") for row in store.rows)


def test_wear_match_never_reuses_an_already_linked_inventory_asset():
    store = Store([purchase(), purchase(2, assetid="new")])
    order = completed(asset_info={"assetid":"old", "classid":"100", "paintwear":"0.25"})
    item = {"assetid":"new", "classid":"100", "paintwear":"0.25", "market_hash_name":"Test Item"}
    reconcile(store, [order], scan_inventory=lambda: (True, [item], ""))
    assert not store.rows[0].get("assetid")


def test_existing_asset_is_never_assigned_to_another_purchase():
    store = Store([purchase(), purchase(2, assetid="seller-asset")])
    assert reconcile(store, [completed()], scan_inventory=lambda: (
        True, [{"assetid": "seller-asset", "market_hash_name": "Test Item"}], "",
    ))[0] == 1
    assert store.updates == [(1, {"pending_receipt": False})]


def test_duplicate_bill_references_are_never_reconciled():
    row = purchase(2)
    row["buff_order_id"] = "bill-1"
    store = Store([purchase(), row])
    assert reconcile(store, [completed()]) == (0, [])


@pytest.mark.parametrize("action", ["delete", "replace_bill", "change_goods", "sold"])
def test_row_changed_during_query_is_not_overwritten(action):
    store = Store([purchase()])

    def query(_ids):
        if action == "delete":
            store.rows.clear()
        elif action == "replace_bill":
            store.rows[0]["buff_order_id"] = "replacement"
        elif action == "change_goods":
            store.rows[0]["goods_id"] = 99
        else:
            store.rows[0]["sale_price"] = 10
        return [completed()]

    assert reconcile(store, query)[0] == 0
    assert store.updates == []


@pytest.mark.parametrize("status,checkpoint,allowed", [
    ({"status": "running", "step": "CHECKOUT_PENDING"}, True, False),
    ({"status": "running", "step": "CHECKING_STABILITY"}, True, False),
    ({"status": "running", "step": "SYNCING_RECEIPTS"}, False, False),
    ({"status": "running", "step": "SYNCING_RECEIPTS"}, True, True),
    ({"status": "idle", "buff_auth_expired": True}, False, False),
    ({"status": "idle", "buff_verification_required": True}, False, False),
])
def test_running_pipeline_only_allows_its_own_checkpoint(idle_app, status, checkpoint, allowed):
    idle_app.update(status)
    store = Store([purchase()])
    count, calls = reconcile(store, [completed()], pipeline_checkpoint=checkpoint)
    assert count == int(allowed)
    assert bool(calls) is allowed


def test_unresolved_checkout_prevents_any_query(monkeypatch):
    monkeypatch.setattr(receipt_sync, "get_unresolved_checkout", lambda: {"unresolved": True})
    assert reconcile(Store([purchase()]), [completed()]) == (0, [])


def test_orders_are_batched_without_starving_recent_purchases():
    store = Store([purchase(i) for i in range(1, 104)])
    count, calls = reconcile(store, lambda ids: [completed(bill) for bill in ids])
    assert count == 103
    assert [len(ids) for ids, _game in calls] == [50, 50, 3]
    assert not any(row["pending_receipt"] for row in store.rows)


def test_request_circuit_propagates_without_updates():
    def blocked(_ids):
        raise BuffRateLimited(30)

    store = Store([purchase()])
    with pytest.raises(BuffRateLimited):
        reconcile(store, blocked)
    assert store.updates == []


def test_bill_query_uses_shared_request_policy_and_filters_other_ids(monkeypatch):
    buyer = BuffBuyer("", request_policy=BuffRequestPolicy(min_interval=0, persist=False))
    calls = []

    def query(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK", "data": {"items": [completed(), completed("other"), None]}}

    monkeypatch.setattr(buyer, "_make_request", query)
    try:
        assert buyer.get_bill_orders(["bill-1", "bill-1"]) == [completed()]
        assert calls[0][:2] == ("GET", API_BILL_ORDER_INFO)
        assert calls[0][2]["params"]["bill_orders"] == "bill-1"
    finally:
        buyer.close()


def test_retry_wait_runs_synchronous_checkpoints_and_respects_stop(monkeypatch):
    state = State()
    ctx = PipelineContext(state, "test")
    calls = []

    def sync():
        calls.append(True)
        state.request_stop()

    ctx.receipt_sync_hook = sync
    monkeypatch.setattr("app.pipeline_context.time.sleep", lambda _seconds: None)
    assert ctx.wait_retry(5) is True
    assert calls == [True]
    assert state.get_status()["status"] == "stopped"


def test_checkpoint_rate_limit_during_wait_keeps_explicit_pipeline_status(monkeypatch):
    from app import pipeline

    state = State()
    monkeypatch.setattr(pipeline, "get_state", lambda: state)

    def wait(_config):
        ctx = PipelineContext(state, "test")

        def blocked():
            raise BuffRateLimited(30)

        ctx.receipt_sync_hook = blocked
        ctx.wait_retry(60)

    monkeypatch.setattr(pipeline, "_run_pipeline", wait)
    pipeline._run_pipeline_guarded({})
    assert state.get_status()["step"] == "BUFF_RATE_LIMITED"
