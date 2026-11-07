"""Match inventory assets to completed BUFF purchases without guessing by name."""
import math
from decimal import Decimal, InvalidOperation


def positive_price(value):
    try:
        price = round(float(value), 2)
        return price if math.isfinite(price) and price > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fingerprint(item):
    try:
        item = {**(item.get("info") or {}), **item}
        wear = Decimal(str(item.get("paintwear")))
        seed = int(item["paintseed"])
        if not wear.is_finite() or not 0 <= wear <= 1:
            return None
        # A float and seed together identify the individual skin across a trade.
        return float(wear), seed
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return None


def match_buff_purchases(items, orders, steam_id, all_inventory=None):
    matches = {}
    candidates = {}
    for item in items:
        assetid = str(item["assetid"])
        fingerprint = _fingerprint(item)
        unique_skin = fingerprint and sum(
            other.get("market_hash_name") == item.get("market_hash_name")
            and _fingerprint(other) == fingerprint
            for other in (all_inventory if all_inventory is not None else items)
        ) == 1
        exact, by_skin = {}, {}
        for order in orders:
            if not isinstance(order, dict) or order.get("state") != "SUCCESS":
                continue
            if str(order.get("progress")) in {"104", "105", "106"}:
                continue
            buyer = str(order.get("buyer_steamid") or "")
            if buyer and buyer != steam_id:
                continue
            asset = order.get("asset_info") or {}
            bill = str(order.get("id") or "").strip()
            if not isinstance(asset, dict) or not bill or not positive_price(order.get("price")):
                continue
            name = asset.get("market_hash_name") or (order.get("goods_info") or {}).get("market_hash_name")
            if name and name != item.get("market_hash_name"):
                continue
            ids = {str(value) for value in (asset.get("assetid"), asset.get("new_assetid"), order.get("new_assetid")) if value}
            if assetid in ids:
                exact[bill] = order
            elif name and unique_skin and fingerprint == _fingerprint(asset):
                by_skin[bill] = order
        found = exact or by_skin
        if len(found) == 1:
            candidates[assetid] = next(iter(found.values()))
    # One historical purchase cannot supply costs for two inventory assets.
    bills = [str(order["id"]) for order in candidates.values()]
    for assetid, order in candidates.items():
        if bills.count(str(order["id"])) == 1:
            matches[assetid] = order
    return matches
