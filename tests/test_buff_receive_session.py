import pytest


@pytest.fixture(autouse=True)
def _isolated_checkout_guard(monkeypatch, tmp_path):
    from app.services import buff_checkout_guard

    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")


def test_buff_trade_poll_uses_shared_client_and_parses_pending_items(monkeypatch):
    from app.receive_flow import fetch_buff_steam_trade

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("BUFF receive polling must not use raw requests")

    monkeypatch.setattr("app.receive_flow.requests.get", unexpected_request)

    class Client:
        def __init__(self):
            self.calls = 0

        def get_steam_trades(self):
            self.calls += 1
            return [
                {
                    "state": 1,
                    "tradeofferid": "offer-1",
                    "created_at": 123,
                    "items_to_trade": [{"assetid": "asset-1", "goods_id": "42"}],
                    "goods_infos": {
                        "42": {
                            "name": "测试物品",
                            "market_hash_name": "Test Item",
                        }
                    },
                }
            ]

    client = Client()
    ok, pending, error = fetch_buff_steam_trade(client)

    assert ok is True
    assert error == ""
    assert client.calls == 1
    assert pending == [
        {
            "tradeofferid": "offer-1",
            "created_at": 123,
            "items": [
                {
                    "assetid": "asset-1",
                    "name": "测试物品",
                    "market_hash_name": "Test Item",
                    "goods_id": 42,
                }
            ],
        }
    ]


def test_buff_trade_poll_propagates_request_circuit():
    from app.receive_flow import fetch_buff_steam_trade
    from buff import BuffRateLimited

    class Client:
        def get_steam_trades(self):
            raise BuffRateLimited(30)

    with pytest.raises(BuffRateLimited):
        fetch_buff_steam_trade(Client())


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"status": "running", "step": "CHECKING_STABILITY"}, False),
        ({"status": "idle", "step": "CHECKOUT_PENDING"}, False),
        ({"status": "error", "step": "BUFF_AUTH_EXPIRED", "buff_auth_expired": True}, False),
        ({"status": "error", "step": "BUFF_VERIFICATION_REQUIRED", "buff_verification_required": True}, False),
        ({"status": "error", "step": "BUFF_WRITE_RESULT_UNKNOWN"}, False),
        ({"status": "error", "step": "BUFF_ORDER_CREATED_PENDING"}, False),
        ({"status": "error", "step": "PIPELINE_UNEXPECTED_ERROR"}, False),
        ({"status": "idle", "step": ""}, True),
    ],
)
def test_buff_background_poll_only_runs_while_fully_idle(monkeypatch, status, expected):
    from app.services import workers

    monkeypatch.setattr(workers, "get_status", lambda: status)
    assert workers._buff_background_request_is_safe() is expected
