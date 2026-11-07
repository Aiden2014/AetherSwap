"""Reconcile receipt state after the user accepts an offer outside this app."""
from collections import Counter
import math
import struct
from typing import Callable, Optional

from app.services.buff_auth import get_buff_auth_lock
from app.services.buff_checkout_guard import buff_activity_guard, get_unresolved_checkout


def _order_id(row: dict) -> str:
    return str(row.get("buff_order_id") or row.get("bill_order_id") or "").strip()


def needs_receipt_sync(row: dict) -> bool:
    return bool(row.get("_db_id") and _order_id(row)
                and row.get("sale_price") is None and not row.get("listing")
                and (row.get("pending_receipt") or
                     (row.get("pending_receipt") is False and not row.get("assetid"))))


def _wear_identity(name: str, item: dict) -> Optional[tuple]:
    """Compare the full CS2 float32 wear value, never display rounding."""
    try:
        wear = float(item["paintwear"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not name or not item.get("classid") or not math.isfinite(wear) or not 0 <= wear <= 1:
        return None
    return name.strip(), str(item["classid"]), struct.pack("!f", wear)


def sync_received_purchases(
    buff_client,
    get_purchases: Callable[[], list],
    update_purchase_by_id: Callable[[int, dict], bool],
    steam_id: str,
    *,
    game: str = "csgo",
    scan_inventory: Optional[Callable] = None,
    log_fn: Optional[Callable] = None,
    pipeline_checkpoint: bool = False,
) -> int:
    """Confirm exact completed bills for the current Steam account.

    The pipeline may call this synchronously between checkouts and while
    waiting. Background callers retain the existing exclusion of a running
    pipeline. No Steam offer is accepted and no BUFF write is sent here.
    """
    from app.pipeline import is_shutdown_pending
    from app.state import get_status

    if not steam_id or not callable(getattr(buff_client, "get_bill_orders", None)):
        return 0
    with get_buff_auth_lock():
        with buff_activity_guard():
            status = get_status() or {}
            if (is_shutdown_pending() or get_unresolved_checkout() is not None
                    or status.get("buff_auth_expired") or status.get("buff_verification_required")
                    or status.get("step") == "CHECKOUT_PENDING"
                    or (status.get("status") == "running" and
                        (not pipeline_checkpoint or status.get("step") != "SYNCING_RECEIPTS"))):
                return 0
            purchases = get_purchases()
            candidates = [row for row in purchases if needs_receipt_sync(row)]
            if not candidates:
                return 0
            # Duplicate local bill references cannot be reconciled safely.
            counts = Counter(_order_id(row) for row in purchases if _order_id(row))
            candidates = [row for row in candidates if counts[_order_id(row)] == 1]
            completed = {}
            # Query in batches; do not starve newer orders behind old bills
            # that never reach SUCCESS.
            candidates.sort(key=lambda row: (not bool(row.get("pending_receipt")), row.get("at") or 0))
            for offset in range(0, len(candidates), 50):
                batch = candidates[offset:offset + 50]
                expected = {_order_id(row): row for row in batch}
                orders = buff_client.get_bill_orders(list(expected), game)
                if orders is None:
                    if log_fn:
                        log_fn("[收货同步] BUFF 订单查询失败，保留本地状态", "warn")
                    return 0
                if not isinstance(orders, list):
                    return 0
                duplicates = Counter(str(order.get("id") or "") for order in orders if isinstance(order, dict))
                for order in orders:
                    if not isinstance(order, dict):
                        continue
                    bill = str(order.get("id") or "")
                    row = expected.get(bill)
                    if (row is None or duplicates[bill] != 1 or order.get("state") != "SUCCESS"
                            or str(order.get("progress")) in {"104", "105", "106"}
                            or str(order.get("buyer_steamid") or "") != str(steam_id)
                            or not row.get("goods_id")
                            or str(order.get("goods_id") or "") != str(row.get("goods_id") or "")):
                        continue
                    completed[bill] = order
            if not completed:
                return 0
            # BUFF usually retains the seller's asset ID after delivery. Only
            # attach it if that exact ID and name exist in our own inventory.
            inventory = {}
            if scan_inventory and any(not row.get("assetid") for row in candidates
                                      if _order_id(row) in completed):
                try:
                    ok, items, _error = scan_inventory()
                    if ok:
                        inventory = {str(item.get("assetid")): item for item in items
                                     if item.get("assetid")}
                except Exception:
                    # Receipt is established by the exact completed bill;
                    # Steam inventory visibility is only optional enrichment.
                    pass
            updated = linked = 0
            # Received assets get new Steam IDs. An immutable full wear value
            # plus name/class can recover the relationship only when both
            # sides are unique. Identical copies must remain unresolved.
            order_identities = {}
            for row in candidates:
                order = completed.get(_order_id(row)) or {}
                asset = order.get("asset_info") or {}
                if isinstance(asset, dict):
                    identity = _wear_identity(row.get("name") or "", asset)
                    if identity:
                        order_identities[_order_id(row)] = identity
            identity_counts = Counter(order_identities.values())
            inventory_identities = {}
            for asset_id, item in inventory.items():
                identity = _wear_identity(item.get("market_hash_name") or "", item)
                if identity:
                    inventory_identities.setdefault(identity, []).append(asset_id)
            for candidate in candidates:
                bill = _order_id(candidate)
                order = completed.get(bill)
                if order is None:
                    continue
                # Re-read stable primary keys so deletes/edits cannot make a
                # positional index update a different purchase.
                current_rows = get_purchases()
                current = next((row for row in current_rows
                                if row.get("_db_id") == candidate["_db_id"]), None)
                if (current is None or not needs_receipt_sync(current) or _order_id(current) != bill
                        or str(current.get("goods_id")) != str(candidate.get("goods_id"))):
                    continue
                patch = {"pending_receipt": False} if current.get("pending_receipt") else {}
                asset = order.get("asset_info") or {}
                asset_id = str(asset.get("assetid") or "") if isinstance(asset, dict) else ""
                inv = inventory.get(asset_id)
                used = {str(row.get("assetid")) for row in current_rows if row.get("assetid")}
                if not inv and not current.get("assetid"):
                    identity = order_identities.get(bill)
                    matches = inventory_identities.get(identity, [])
                    if identity_counts.get(identity) == 1 and len(matches) == 1:
                        asset_id = matches[0]
                        inv = inventory[asset_id]
                if (not current.get("assetid") and inv and asset_id not in used
                        and (inv.get("market_hash_name") or "").strip() == current.get("name")):
                    patch["assetid"] = asset_id
                if patch and update_purchase_by_id(current["_db_id"], patch):
                    updated += 1
                    linked += int("assetid" in patch)
            if updated and log_fn:
                log_fn(f"[收货同步] 已核对并更新 {updated} 条记录，精确匹配库存 ID {linked} 条；未匹配的库存 ID 保留为空", "info")
            return updated
