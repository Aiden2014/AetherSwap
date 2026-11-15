"""Preview, apply and rollback endpoints for historical sale-price calibration."""
import sqlite3
import time
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from app.database import (
    db_apply_history_price_repairs,
    db_get_history_price_repair_audits,
    db_rollback_history_price_repairs,
)
from app.history_price_repair import build_history_price_preview
from app.state import log

router = APIRouter()
_PREVIEW_CACHE = {}
_PREVIEW_TTL_SECONDS = 30 * 60


class ApplyHistoryPriceRepairBody(BaseModel):
    preview_id: str
    purchase_ids: List[int]


class RollbackHistoryPriceRepairBody(BaseModel):
    batch_id: str


def _create_database_backup() -> str:
    root = Path(__file__).resolve().parents[2]
    source_path = root / "config" / "app.db"
    if not source_path.exists():
        raise RuntimeError("找不到 config/app.db，无法在修改前创建数据库备份")
    backup_dir = root / "config" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    filename = "history-price-repair-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".db"
    target_path = backup_dir / filename
    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return str(target_path.relative_to(root))


def _clean_preview_cache() -> None:
    cutoff = time.time() - _PREVIEW_TTL_SECONDS
    for key in list(_PREVIEW_CACHE):
        if _PREVIEW_CACHE[key].get("created_at", 0) < cutoff:
            _PREVIEW_CACHE.pop(key, None)


@router.post("/api/history_price_repair/preview")
def api_history_price_repair_preview():
    try:
        preview = build_history_price_preview()
        preview_id = uuid.uuid4().hex
        _clean_preview_cache()
        _PREVIEW_CACHE[preview_id] = {
            "created_at": time.time(),
            "items": preview["items"],
            "stats": preview["stats"],
        }
        return {
            "ok": True,
            "preview_id": preview_id,
            "items": preview["items"],
            "stats": preview["stats"],
            "expires_in_seconds": _PREVIEW_TTL_SECONDS,
        }
    except Exception as exc:
        log(f"历史价格校准预览失败: {type(exc).__name__}: {exc}", "error", category="history_price_repair")
        return {"ok": False, "error": str(exc)[:240]}


@router.post("/api/history_price_repair/apply")
def api_history_price_repair_apply(body: ApplyHistoryPriceRepairBody):
    _clean_preview_cache()
    preview = _PREVIEW_CACHE.get(body.preview_id)
    if not preview:
        return {"ok": False, "error": "预览已过期或服务已重启，请重新生成预览"}
    selected_ids = {int(value) for value in body.purchase_ids if int(value) > 0}
    candidates = {
        int(item["purchase_id"]): item
        for item in preview["items"]
        if item.get("can_apply")
    }
    selected = [candidates[value] for value in sorted(selected_ids) if value in candidates]
    if not selected:
        return {"ok": False, "error": "没有选择可应用的高/中置信度修复项"}
    updates = []
    for item in selected:
        updates.append({
            "purchase_id": int(item["purchase_id"]),
            "expected_assetid": item.get("expected_purchase_assetid"),
            "expected_purchase_at": item.get("expected_purchase_at"),
            "expected_old_sale_price": item.get("current_sale_price"),
            "new_sale_price": float(item["suggested_sale_price"]),
            "metadata": {
                "name": item.get("name"),
                "assetid": item.get("assetid"),
                "raw_price": item.get("raw_price"),
                "raw_amount": item.get("raw_amount"),
                "currency_code": item.get("currency_code"),
                "sold_at": item.get("sold_at"),
                "listing_price_cny": item.get("listing_price_cny"),
                "listing_at": item.get("listing_at"),
                "implied_rate": item.get("implied_rate"),
                "calibration_samples": item.get("calibration_samples"),
                "dispersion_pct": item.get("dispersion_pct"),
                "confidence": item.get("confidence"),
                "evidence": item.get("evidence"),
            },
        })
    try:
        backup_path = _create_database_backup()
        batch_id = uuid.uuid4().hex
        updated = db_apply_history_price_repairs(batch_id, updates, backup_path)
        _PREVIEW_CACHE.pop(body.preview_id, None)
        log(
            f"历史价格校准已应用 {updated} 条，batch={batch_id}，备份={backup_path}",
            "info",
            category="history_price_repair",
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "updated": updated,
            "backup_path": backup_path,
        }
    except Exception as exc:
        log(f"历史价格校准应用失败: {type(exc).__name__}: {exc}", "error", category="history_price_repair")
        return {"ok": False, "error": str(exc)[:240]}


@router.get("/api/history_price_repair/audit")
def api_history_price_repair_audit():
    rows = db_get_history_price_repair_audits(200)
    batches = {}
    for row in rows:
        batch_id = row["batch_id"]
        batch = batches.setdefault(batch_id, {
            "batch_id": batch_id,
            "created_at": row["created_at"],
            "count": 0,
            "rolled_back_count": 0,
            "backup_path": row.get("backup_path") or "",
        })
        batch["count"] += 1
        batch.setdefault("items", []).append({
            "purchase_id": row["purchase_id"],
            "old_sale_price": row.get("old_sale_price"),
            "new_sale_price": row.get("new_sale_price"),
            "rolled_back_at": row.get("rolled_back_at"),
            "metadata": _safe_metadata(row.get("metadata_json")),
        })
        if row.get("rolled_back_at") is not None:
            batch["rolled_back_count"] += 1
    result = list(batches.values())
    result.sort(key=lambda row: row["created_at"], reverse=True)
    return {"ok": True, "batches": result[:20]}


def _safe_metadata(raw: str) -> dict:
    import json
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


@router.post("/api/history_price_repair/rollback")
def api_history_price_repair_rollback(body: RollbackHistoryPriceRepairBody):
    try:
        backup_path = _create_database_backup()
        restored = db_rollback_history_price_repairs(body.batch_id)
        log(
            f"历史价格校准回滚 {restored} 条，batch={body.batch_id}，备份={backup_path}",
            "info",
            category="history_price_repair",
        )
        return {"ok": True, "restored": restored, "backup_path": backup_path}
    except Exception as exc:
        log(f"历史价格校准回滚失败: {type(exc).__name__}: {exc}", "error", category="history_price_repair")
        return {"ok": False, "error": str(exc)[:240]}
