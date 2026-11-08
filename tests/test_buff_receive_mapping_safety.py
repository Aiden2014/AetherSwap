import requests

from app import receive_flow


ITEM_NAME = "Danger Zone Case"


def test_accept_timeout_is_unknown_and_non_idempotent_post_is_not_retried(
    monkeypatch,
):
    calls = []

    class ProxyManager:
        def get_proxies_for_request(self, **_kwargs):
            return {}

    def timeout_once(*_args, **_kwargs):
        calls.append(True)
        raise requests.Timeout("response lost")

    monkeypatch.setattr(
        "utils.proxy_manager.get_proxy_manager",
        lambda: ProxyManager(),
    )
    monkeypatch.setattr(receive_flow.requests, "post", timeout_once)

    result = receive_flow.accept_steam_trade_offer(
        "offer-1",
        {"sessionid": "session"},
    )

    assert result is None
    assert calls == [True]


def test_exact_goods_id_match_beats_older_same_name_fallback():
    pending = [
        {
            "_db_id": 1,
            "goods_id": 999,
            "name": ITEM_NAME,
            "at": 1.0,
        },
        {
            "_db_id": 2,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 2.0,
        },
    ]
    incoming = {
        "goods_id": 42,
        "market_hash_name": ITEM_NAME,
    }

    matched = receive_flow._match_purchase_for_item(incoming, pending, set())

    assert matched is not None
    assert matched["_db_id"] == 2


def test_ambiguous_name_only_match_is_rejected():
    pending = [
        {"_db_id": 1, "name": ITEM_NAME, "at": 1.0},
        {"_db_id": 2, "name": ITEM_NAME, "at": 2.0},
    ]
    incoming = {"market_hash_name": ITEM_NAME}

    assert (
        receive_flow._match_purchase_for_item(incoming, pending, set()) is None
    )


def test_known_bills_never_fall_back_to_product_or_oldest_purchase():
    rows = [
        {"_db_id":1, "buff_order_id":"bill-old", "goods_id":42, "name":ITEM_NAME, "at":1},
        {"_db_id":2, "buff_order_id":"bill-new", "goods_id":42, "name":ITEM_NAME, "at":2},
        {"_db_id":3, "goods_id":42, "name":ITEM_NAME, "at":0},
    ]
    incoming = {"goods_id":42,"market_hash_name":ITEM_NAME}
    assert receive_flow._match_purchase_for_item(incoming, rows, set()) is None
    incoming["buff_order_id"] = "bill-new"
    assert receive_flow._match_purchase_for_item(incoming, rows, set())["_db_id"] == 2
    incoming["buff_order_id"] = "unknown-bill"
    assert receive_flow._match_purchase_for_item(incoming, rows, set()) is None


def test_later_purchase_delivered_first_updates_its_exact_bill(monkeypatch):
    rows = [
        {"_db_id":1,"buff_order_id":"bill-old","goods_id":42,"name":ITEM_NAME,"at":1,"pending_receipt":True},
        {"_db_id":2,"buff_order_id":"bill-new","goods_id":42,"name":ITEM_NAME,"at":2,"pending_receipt":True},
    ]
    orders = [
        {"id":"bill-old","goods_id":42,"buyer_steamid":"7656","state":"TO_DELIVER", "tradeofferid":"other-offer",
         "asset_info":{"assetid":"seller-old","classid":"100","paintwear":"0.1"}},
        {"id":"bill-new","goods_id":42,"buyer_steamid":"7656","state":"TO_DELIVER", "tradeofferid":"new-offer",
         "asset_info":{"assetid":"seller-new","classid":"100","paintwear":"0.25"}},
    ]
    client = type("Client", (), {"get_bill_orders":lambda _self, ids: [o for o in orders if o["id"] in ids]})()
    task = {"tradeofferid":"new-offer", "items":[{
        "assetid":"seller-new","goods_id":42,"market_hash_name":ITEM_NAME,
    }]}
    monkeypatch.setattr(receive_flow,"fetch_buff_steam_trade",lambda _client, **_kwargs:(True,[task],""))
    accepted=[]
    monkeypatch.setattr(receive_flow,"accept_steam_trade_offer",lambda offer,_cookies:accepted.append(offer) or True)
    monkeypatch.setattr(receive_flow,"jittered_sleep",lambda *_a:None)

    def scan():
        items = ([{"assetid":"receiver-new","classid":"100","paintwear":"0.25","market_hash_name":ITEM_NAME}]
                 if accepted else [])
        return True,items,""

    def update(db_id, patch):
        next(p for p in rows if p["_db_id"] == db_id).update(patch)
        return True

    received=receive_flow.try_receive_once(
        lambda:[dict(row) for row in rows],lambda *_a:False,lambda:client,
        lambda:{"steam_id":"7656","cookies":"steamLoginSecure=secure; sessionid=session"},
        scan_inventory=scan,update_purchase_by_id=update,
    )
    assert received == 1
    assert accepted == ["new-offer"]
    assert rows[0]["pending_receipt"] is True
    assert not rows[0].get("assetid")
    assert rows[1]["pending_receipt"] is False
    assert rows[1]["assetid"] == "receiver-new"


def test_offer_bill_index_rejects_wrong_account_and_duplicate_references():
    valid={"id":"bill","goods_id":42,"buyer_steamid":"7656","state":"TO_DELIVER",
           "tradeofferid":"offer","asset_info":{"assetid":"seller"}}
    assert receive_flow._offer_bill_items([dict(valid,buyer_steamid="other")],"7656") == {}
    assert receive_flow._offer_bill_items([dict(valid,state="SUCCESS")],"7656") == {}
    assert len(receive_flow._offer_bill_items([valid,valid],"7656")[("offer","seller")]) == 2


def test_partial_inventory_visibility_never_persists_seller_assetid(
    monkeypatch,
):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        },
        {
            "_db_id": 2,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 2.0,
            "pending_receipt": True,
        },
    ]
    task = {
        "tradeofferid": "offer-1",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset-1",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
            {
                "assetid": "seller-old-asset-2",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
        ],
    }
    accepted = {"value": False}

    def get_purchases():
        return [dict(row) for row in purchases]

    def update_purchase_by_id(db_id, data):
        row = next(row for row in purchases if row["_db_id"] == db_id)
        row.update(data)
        return True

    def accept_offer(*_args, **_kwargs):
        accepted["value"] = True
        return True

    def scan_inventory():
        if not accepted["value"]:
            return True, [], ""
        # Steam inventory propagation is incomplete: only one of the two
        # received assets is visible so far.
        return (
            True,
            [
                {
                    "assetid": "receiver-new-asset-1",
                    "market_hash_name": ITEM_NAME,
                }
            ],
            "",
        )

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client, **_kwargs: (True, [task], ""),
    )
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer", accept_offer)
    monkeypatch.setattr(
        receive_flow,
        "jittered_sleep",
        lambda *_args, **_kwargs: None,
    )

    receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assigned = [row for row in purchases if row.get("assetid")]
    still_pending = [
        row
        for row in purchases
        if row.get("pending_receipt") and not row.get("assetid")
    ]

    assert [row["assetid"] for row in assigned] == ["receiver-new-asset-1"]
    assert [row["_db_id"] for row in still_pending] == [2]
    assert not {
        "seller-old-asset-1",
        "seller-old-asset-2",
    }.intersection(row.get("assetid") for row in purchases)


def test_inventory_poll_waits_for_all_new_assets_and_excludes_old_same_name(
    monkeypatch,
):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        },
        {
            "_db_id": 2,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 2.0,
            "pending_receipt": True,
        },
    ]
    task = {
        "tradeofferid": "offer-1",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset-1",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
            {
                "assetid": "seller-old-asset-2",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            },
        ],
    }
    accepted = {"value": False}
    post_scans = {"value": 0}

    def get_purchases():
        return [dict(row) for row in purchases]

    def update_purchase_by_id(db_id, data):
        row = next(row for row in purchases if row["_db_id"] == db_id)
        row.update(data)
        return True

    def accept_offer(*_args, **_kwargs):
        accepted["value"] = True
        return True

    def inventory_item(assetid):
        return {"assetid": assetid, "market_hash_name": ITEM_NAME}

    def scan_inventory():
        existing = inventory_item("personal-existing-same-name")
        if not accepted["value"]:
            return True, [existing], ""
        post_scans["value"] += 1
        visible = [existing, inventory_item("receiver-new-asset-1")]
        if post_scans["value"] >= 2:
            visible.append(inventory_item("receiver-new-asset-2"))
        return True, visible, ""

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client, **_kwargs: (True, [task], ""),
    )
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer", accept_offer)
    monkeypatch.setattr(
        receive_flow,
        "jittered_sleep",
        lambda *_args, **_kwargs: None,
    )

    received = receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assert received == 2
    assert post_scans["value"] == 2
    assert [row["assetid"] for row in purchases] == [
        "receiver-new-asset-1",
        "receiver-new-asset-2",
    ]


def test_failed_pre_accept_inventory_snapshot_leaves_offer_unaccepted(
    monkeypatch,
):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        }
    ]
    task = {
        "tradeofferid": "offer-1",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            }
        ],
    }
    accepted = []

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client, **_kwargs: (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda *_args, **_kwargs: accepted.append(True) or True,
    )

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=lambda: (False, [], "inventory unavailable"),
        update_purchase_by_id=lambda *_args, **_kwargs: True,
    )

    assert received == 0
    assert accepted == []
    assert purchases[0]["pending_receipt"] is True
    assert not purchases[0].get("assetid")


def test_unmatched_offer_is_never_accepted_or_scanned(monkeypatch):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 999,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        }
    ]
    task = {
        "tradeofferid": "offer-foreign",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            }
        ],
    }
    accepted = []
    scans = []

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client, **_kwargs: (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda *_args, **_kwargs: accepted.append(True) or True,
    )

    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=lambda: scans.append(True) or (True, [], ""),
        update_purchase_by_id=lambda *_args, **_kwargs: True,
    )

    assert received == 0
    assert accepted == []
    assert scans == []


def test_unknown_accept_result_is_reconciled_from_inventory(monkeypatch):
    purchases = [
        {
            "_db_id": 1,
            "goods_id": 42,
            "name": ITEM_NAME,
            "at": 1.0,
            "pending_receipt": True,
        }
    ]
    task = {
        "tradeofferid": "offer-unknown",
        "created_at": 100,
        "items": [
            {
                "assetid": "seller-old-asset",
                "goods_id": 42,
                "market_hash_name": ITEM_NAME,
            }
        ],
    }
    accepted = {"attempted": False}

    def get_purchases():
        return [dict(row) for row in purchases]

    def update_purchase_by_id(db_id, data):
        row = next(row for row in purchases if row["_db_id"] == db_id)
        row.update(data)
        return True

    def accept_unknown(*_args, **_kwargs):
        accepted["attempted"] = True
        return None

    def scan_inventory():
        if not accepted["attempted"]:
            return True, [], ""
        return (
            True,
            [
                {
                    "assetid": "receiver-new-asset",
                    "market_hash_name": ITEM_NAME,
                }
            ],
            "",
        )

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda _client, **_kwargs: (True, [task], ""),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        accept_unknown,
    )
    monkeypatch.setattr(
        receive_flow,
        "jittered_sleep",
        lambda *_args, **_kwargs: None,
    )

    received = receive_flow.try_receive_once(
        get_purchases=get_purchases,
        update_purchase=lambda *_args, **_kwargs: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=session",
        },
        scan_inventory=scan_inventory,
        update_purchase_by_id=update_purchase_by_id,
    )

    assert received == 1
    assert purchases[0]["assetid"] == "receiver-new-asset"
    assert purchases[0]["pending_receipt"] is False
