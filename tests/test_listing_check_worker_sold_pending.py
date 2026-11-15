from unittest.mock import Mock

import pytest


def test_confirmed_sold_without_price_is_not_kept_as_holding(monkeypatch):
    from app import steam_listings
    from app.services import workers

    purchase = {
        "_db_id": 41,
        "assetid": "asset-123",
        "listing": True,
        "listing_status": None,
    }
    updates = []
    inventory_updates = []
    sleep_calls = [0]

    monkeypatch.setattr(
        workers,
        "load_app_config_validated",
        lambda: {"pipeline": {"listing_check_interval_seconds": 60}},
    )
    monkeypatch.setattr(workers, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(workers, "get_purchases", lambda: [purchase.copy()])
    monkeypatch.setattr(workers, "get_steam_credentials", lambda: {"cookies": "sessionid=x"})
    monkeypatch.setattr(
        steam_listings,
        "fetch_my_listings",
        lambda *_args, **_kwargs: (True, set(), "", {}),
    )

    def read_history(*_args, sold_assetids_out=None, **_kwargs):
        assert sold_assetids_out is not None
        sold_assetids_out.add("asset-123")
        return True, {}, ""

    monkeypatch.setattr(steam_listings, "fetch_my_history_sold", read_history)
    monkeypatch.setattr(
        workers,
        "update_purchase_by_id",
        lambda db_id, fields: updates.append((db_id, fields.copy())),
    )
    monkeypatch.setattr(
        workers,
        "scan_cs2_inventory",
        lambda: (True, [{"assetid": "remaining-asset"}], ""),
    )
    monkeypatch.setattr(workers, "set_inventory", lambda items: inventory_updates.append(items))
    monkeypatch.setattr(workers, "log", Mock())

    def stop_after_one_cycle(_seconds):
        sleep_calls[0] += 1
        if sleep_calls[0] > 1:
            raise KeyboardInterrupt()

    monkeypatch.setattr(workers.time, "sleep", stop_after_one_cycle)

    with pytest.raises(KeyboardInterrupt):
        workers.listing_check_worker()

    assert updates == [
        (41, {"listing": False, "listing_status": "sold_pending_price"})
    ]
    assert inventory_updates == [[{"assetid": "remaining-asset"}]]
