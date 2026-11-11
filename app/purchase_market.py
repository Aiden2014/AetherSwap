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


def refresh_wallet_currency(credentials):
    """Resolve ambiguous history symbols using this account's live wallet."""
    from app.accounts import get_current_account
    from app.config_loader import get_steam_credentials
    from app.services.account_region import refresh_account_region_currency

    account = get_current_account() or {}
    current = get_steam_credentials()
    steam_id = str(credentials.get("steam_id") or "")
    if (not steam_id or not account.get("id")
            or str(account.get("steam_id") or "") != steam_id
            or str(current.get("steam_id") or "") != steam_id):
        return None
    try:
        result = refresh_account_region_currency(account["id"], cookies_raw=current.get("cookies"))
    except Exception:
        return None
    current = get_steam_credentials()
    if not result.get("ok") or str(current.get("steam_id") or "") != steam_id:
        return None
    return verified_wallet_currency(current)


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
    currency_refresh_attempted = False
    histories = {}
    # Keep a large import responsive. Cached/repeated products cost no requests.
    deadline = time.monotonic() + 20
    missing = False
    currency_missing = False
    for name, target in targets:
        if name not in histories:
            if time.monotonic() >= deadline:
                missing = True
                continue
            try:
                histories[name] = client.fetch_history(name, return_currency=True)
            except Exception:
                histories[name] = None
            raw = histories[name]
            if isinstance(raw, dict) and raw.get("history") and not raw.get("currency"):
                if not wallet_currency and not currency_refresh_attempted:
                    currency_refresh_attempted = True
                    wallet_currency = refresh_wallet_currency(credentials)
                if wallet_currency:
                    # Do not mutate the shared history cache; currency metadata
                    # can expire independently of a successful history request.
                    histories[name] = {**raw, "currency": wallet_currency}
        raw = histories[name]
        currency_unavailable = isinstance(raw, dict) and bool(raw.get("history")) and not raw.get("currency")
        if currency_unavailable:
            currency_missing = True
            target["market_price_note"] = "无法确认 Steam 历史价格币种，请验证 Steam 登录后重试"
        reference = history_price_at(histories[name], target["purchased_at"])
        target.update(reference)
        missing = missing or (not reference and not currency_unavailable)
    return "；".join(filter(None, [
        "部分购入市场价因无法确认 Steam 钱包币种而留空，请验证 Steam 登录后重试或手动填写" if currency_missing else "",
        "部分购入市场价未查到购买前 24 小时内的历史数据或接口暂不可用，已留空，可手动填写或重试" if missing else "",
    ]))
