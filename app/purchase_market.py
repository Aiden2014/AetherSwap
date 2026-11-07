"""Purchase-time Steam price references for inventory imports."""
import math
import time

from app.inventory_holdings import positive_price
from utils.money import apply_currency
from utils.time import parse_steam_history_date


def verified_wallet_currency(credentials):
    """A bare yen/yuan symbol cannot identify CNY versus JPY.

    Logged-in price history uses the wallet currency (ignoring a currency URL
    parameter). Only use recent verified metadata for this same Steam account.
    """
    from app.accounts import get_current_account
    account = get_current_account() or {}
    if (not account.get("region_check_ok") or not credentials.get("steam_id")
            or str(account.get("steam_id") or "") != str(credentials["steam_id"])):
        return None
    try:
        age = time.time() - float(account.get("currency_checked_at") or 0)
    except (ValueError, TypeError, OverflowError):
        return None
    if not 0 <= age <= 86400:
        return None
    code = str(account.get("currency_code") or "").strip().upper()
    return code or None


def history_price_at(raw, purchased_at):
    """Use the last historical bucket at/before purchase, at most 24h old.

    Steam publishes aggregated transaction prices, not past listing snapshots.
    Foreign-wallet prices use the configured current FX rate and are labelled.
    """
    if not isinstance(raw, dict) or not purchased_at:
        return {}
    currency = str(raw.get("currency") or "").strip().upper()
    if not currency:
        return {}
    found = None
    for row in raw.get("history") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        dt = parse_steam_history_date(row[0])
        try:
            price = float(row[1])
        except (ValueError, TypeError, OverflowError):
            continue
        if dt is None or not math.isfinite(price) or price <= 0:
            continue
        stamp = dt.timestamp()
        if 0 <= purchased_at - stamp <= 86400 and (found is None or stamp > found[0]):
            found = stamp, price
    if found is None:
        return {}
    prices, code = apply_currency([found[1]], currency)
    price = positive_price(prices[0]) if code == "CNY" else None
    if not price:
        return {}
    return {"market_price": price, "market_price_at": found[0],
            "market_price_note": "Steam 历史成交价参考" + ("（按当前汇率折算）" if currency != "CNY" else "")}


def enrich_purchase_market_prices(rows, names):
    """Reuse per-product history and the existing account cache/cooldown policy."""
    from app.config_loader import get_steam_credentials
    from app.services.steam_client import SteamClient

    targets = []
    for row in rows:
        if row["exists"]:
            continue
        targets.extend((names[row["assetid"]], candidate)
                       for candidate in [row, *row.get("purchase_candidates", [])]
                       if candidate.get("purchased_at"))
    if not targets:
        return ""
    credentials = get_steam_credentials()
    if not credentials.get("cookies"):
        return "未配置 Steam 登录，购入市场价暂留空，可手动填写"
    client = SteamClient(timeout_sec=8)
    wallet_currency = verified_wallet_currency(credentials)
    histories = {}
    # Keep a large import responsive. Cached/repeated products cost no requests.
    deadline = time.monotonic() + 20
    missing = False
    for name, target in targets:
        if name not in histories:
            if time.monotonic() >= deadline:
                missing = True
                continue
            try:
                histories[name] = client.fetch_history(name, return_currency=True)
                if isinstance(histories[name], dict) and not histories[name].get("currency") and wallet_currency:
                    histories[name] = {**histories[name], "currency": wallet_currency}
            except Exception:
                histories[name] = None
        reference = history_price_at(histories[name], target["purchased_at"])
        target.update(reference)
        missing = missing or not reference
    return "部分购入市场价未查到购买前 24 小时内的历史数据或接口暂不可用，已留空，可手动填写或重试" if missing else ""
