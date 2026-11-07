"""Inventory routes."""
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional
from app.state import get_inventory, is_steam_background_allowed, log, set_inventory
from app.inventory_cs2 import scan_cs2_inventory
from app.pipeline import run_sell_phase_on_inventory_update
from app.config_loader import get_steam_credentials, load_app_config_validated
from app.shared_market import get_steam_smart_price_cny, batch_fetch_prices
router = APIRouter()


class InventoryPurchasePreview(BaseModel):
    assetids: list[str] = Field(default_factory=list, max_length=500)
    sync_buff: bool = True


class InventoryPurchaseItem(BaseModel):
    assetid: str
    price: Optional[float] = None


class InventoryPurchaseImport(BaseModel):
    items: list[InventoryPurchaseItem] = Field(default_factory=list, max_length=500)


def _selected_inventory(assetids):
    inventory = {str(item.get("assetid") or ""): item for item in get_inventory()}
    ids = list(dict.fromkeys(str(value).strip() for value in assetids))
    if not ids or any(not value or value not in inventory for value in ids):
        raise ValueError("所选物品已不在库存中，请刷新库存后重试")
    return [inventory[value] for value in ids]


@router.post("/api/inventory/holdings/preview")
def api_inventory_holdings_preview(body: InventoryPurchasePreview):
    from app.state import get_purchases
    from app.inventory_holdings import match_buff_purchases, positive_price
    from buff import BuffAuthExpired, BuffRateLimited, BuffVerificationRequired, BuffRequestBlocked
    from buff.buyer import BuffPurchaseHistoryError
    from requests.exceptions import RequestException
    try:
        items = _selected_inventory(body.assetids)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    existing = {str(p.get("assetid") or "").strip() for p in get_purchases()}
    pending = [item for item in items if str(item["assetid"]) not in existing]
    matches, warning = {}, ""
    if body.sync_buff and pending:
        from app.config_loader import get_buff_credentials
        from app.services.buff_client import create_buff_client_from_config
        credentials = get_buff_credentials()
        if not credentials.get("cookies"):
            warning = "未配置 BUFF 登录，请手动填写购入价或登录后同步"
        else:
            client = None
            try:
                client = create_buff_client_from_config(credentials, load_app_config_validated())
                orders = []
                for page in range(1, 11):
                    data = client.get_purchase_history_page(page)
                    rows = data["items"]
                    orders.extend(rows)
                    total_pages = int(data.get("total_page") or 0)
                    if not rows or (total_pages > 0 and page >= total_pages) or (not total_pages and len(rows) < 50):
                        break
                else:
                    warning = f"已查询最近 {len(orders)} 条 BUFF 购入记录，较早的物品请手动填价"
                matches = match_buff_purchases(pending, orders, str(get_steam_credentials().get("steam_id") or ""), get_inventory())
            except BuffAuthExpired:
                warning = "BUFF 登录已过期，请重新登录后同步；也可直接手动填价"
            except BuffRateLimited as exc:
                warning = f"BUFF 请求频率受限，请约 {max(1, int(exc.retry_after))} 秒后重试；也可直接手动填价"
            except BuffVerificationRequired:
                warning = "BUFF 要求安全验证，请在 BUFF 页面完成验证后重试；也可直接手动填价"
            except BuffRequestBlocked:
                warning = "BUFF 请求暂被保护策略阻止，请稍后重试；也可直接手动填价"
            except BuffPurchaseHistoryError as exc:
                warning = f"{exc}；也可直接手动填价"
            except RequestException:
                warning = "BUFF 购入记录查询遇到网络错误，请稍后重试；也可直接手动填价"
            except Exception as exc:
                log(f"BUFF 购入记录处理失败（{type(exc).__name__}）", "warn", category="buff")
                warning = "BUFF 购入记录处理失败，请查看调试日志；也可直接手动填价"
            finally:
                if client is not None:
                    client.close()
    return {"ok": True, "warning": warning, "items": [
        {"assetid": str(item["assetid"]), "name": item.get("name") or item.get("market_hash_name"),
         "exists": str(item["assetid"]) in existing,
         "price": positive_price(matches[str(item["assetid"])].get("price")) if str(item["assetid"]) in matches else None,
         "source": "buff" if str(item["assetid"]) in matches else "manual"}
        for item in items
    ]}


@router.post("/api/inventory/holdings")
def api_inventory_add_holdings(body: InventoryPurchaseImport):
    import time
    from app.database import db_import_inventory_purchases
    from app.state import get_purchases
    from app.inventory_holdings import positive_price
    try:
        items = _selected_inventory([row.assetid for row in body.items])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    existing = {str(p.get("assetid") or "").strip() for p in get_purchases()}
    prices = {row.assetid.strip(): positive_price(row.price) for row in body.items}
    records = []
    for item in items:
        assetid = str(item["assetid"])
        if not prices[assetid] and assetid not in existing:
            return {"ok": False, "error": f"请为 assetid {assetid} 填写大于 0 的有效购入价"}
        name = item.get("market_hash_name") or item.get("name")
        if not name:
            return {"ok": False, "error": "库存物品缺少名称，请刷新库存"}
        records.append({"assetid": assetid, "name": name, "price": prices[assetid] or 0,
                        "at": time.time(), "pending_receipt": False})
    try:
        result = db_import_inventory_purchases(records)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}
def _get_steam_smart_price_cny(session, market_hash_name: str, app_id: int = 730):
    return get_steam_smart_price_cny(session, market_hash_name, app_id=app_id)
def _enrich_inventory_with_steam_prices(items: list, old_items: list) -> None:
    """Fill lowest_price on inventory items using shared batch_fetch_prices."""
    old_price_by_name: dict = {}
    for it in (old_items or []):
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        p = it.get("lowest_price")
        if name and p is not None and float(p) > 0:
            if name not in old_price_by_name:
                old_price_by_name[name] = float(p)
    for it in items:
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        it["lowest_price"] = old_price_by_name.get(name, 0)
    names = set()
    for it in items:
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        if name:
            names.add(name)
    if not names:
        return
    prices = batch_fetch_prices(names)
    for it in items:
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        if name in prices:
            it["lowest_price"] = prices[name]
def _try_steam_auto_relogin():
    from app.services.steam_auth import try_steam_auto_relogin
    return try_steam_auto_relogin()
@router.get("/api/inventory")
def api_inventory(refresh: bool = False):
    if refresh or not get_inventory():
        if not is_steam_background_allowed():
            return {"items": get_inventory(), "source": "cache"}
        ok, items, err = scan_cs2_inventory()
        if not ok and err and "登录已过期" in err:
            success, status, msg = _try_steam_auto_relogin()
            if status in ("busy", "auth_pending", "network_error", "account_changed"):
                return {"items": get_inventory(), "source": "cache", "auth_status": status, "error": msg}
            if success:
                import time as _time
                log("auto_relogin: 登录成功，等待 Steam 服务端会话生效 (8s)…", "info", category="steam")
                _time.sleep(8)
                ok, items, err = scan_cs2_inventory()
                if ok:
                    old = get_inventory()
                    _enrich_inventory_with_steam_prices(items, old)
                    set_inventory(items)
                    run_sell_phase_on_inventory_update(items)
                    return {"items": get_inventory(), "source": "live"}
                if err and "登录已过期" in err:
                    log("auto_relogin: 首次重试仍过期，再等 7 秒…", "info", category="steam")
                    _time.sleep(7)
                    ok, items, err = scan_cs2_inventory()
                    if ok:
                        old = get_inventory()
                        _enrich_inventory_with_steam_prices(items, old)
                        set_inventory(items)
                        run_sell_phase_on_inventory_update(items)
                        return {"items": get_inventory(), "source": "live"}
                log(f"auto_relogin: 登录成功但库存获取仍失败: {err}，返回缓存", "warn", category="steam")
                return {"items": get_inventory(), "source": "cache", "error": err}
            out = {"items": get_inventory(), "source": "cache", "error": msg or err,
                   "auth_expired": True, "auth_status": status}
            if status == "need_2fa":
                out["auth_expired_reason"] = "need_2fa"
                out["error"] = "需要二次验证（验证码），请到库存页手动重新登录 Steam"
            elif status == "no_creds":
                out["auth_expired_reason"] = "no_creds"
            return out
        if not ok:
            out = {"items": get_inventory(), "source": "cache", "error": err}
            if err and ("登录已过期" in err or "未配置" in err):
                out["auth_expired"] = True
            return out
        old = get_inventory()
        _enrich_inventory_with_steam_prices(items, old)
        set_inventory(items)
        run_sell_phase_on_inventory_update(items)
        return {"items": get_inventory(), "source": "live"}
    return {"items": get_inventory(), "source": "cache"}
@router.get("/api/market-prices")
def api_market_prices():
    """统一批量市场价查询接口.
    一次性查出库存（lowest_price）和持有饰品（current_market_price）所需的全部
    唯一物品名称，每个名称只发一次 Steam API 请求，然后返回给前端同时刷新两个视图。
    """
    if not is_steam_background_allowed():
        return {"prices": {}, "error": "Steam 后台请求不可用"}
    from app.state import get_purchases
    inv_items = get_inventory() or []
    inv_names = {
        (it.get("market_hash_name") or it.get("name") or "").strip()
        for it in inv_items
    }
    purchases = get_purchases() or []
    holdings_names = {
        (p.get("name") or "").strip()
        for p in purchases
        if not (p.get("sale_price") is not None and float(p.get("sale_price") or 0) > 0)
    }
    all_names = {n for n in (inv_names | holdings_names) if n}
    if not all_names:
        return {"prices": {}}
    prices = batch_fetch_prices(all_names)
    return {"prices": prices}
