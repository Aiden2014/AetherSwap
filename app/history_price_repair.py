"""Build conservative, preview-only corrections for Steam sale prices.

The Sale table is deliberately read-only here: its CNY price, timestamp and
assetid are evidence anchors. Only Purchase.sale_price is repaired.
"""
import json
import math
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config_loader import get_steam_credentials
from app.state import get_purchases, get_sales
from app.steam_listings import (
    MYHISTORY_RENDER_URL,
    _HOVER_PATTERN,
    _cookies_to_dict,
    _currency_code_from_price_text,
    _get_with_retry,
    _history_account_currency,
    _parse_history_price_amount,
)
from utils.time import parse_steam_history_date

STEAM_FEE_FACTOR = 1.15
HISTORY_PAGE_SIZE = 500
HISTORY_MAX_PAGES = 40
CALIBRATION_WINDOW_SECONDS = 36 * 60 * 60
HISTORY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}
_DATE_PATTERN = re.compile(
    r"[A-Za-z]{3}\s+\d{1,2},?\s+\d{4}\s+\d{1,2}\s*:\s*[+-]\d{1,4}"
)


def _as_number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_from_value(value) -> Optional[float]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    numeric = _as_number(raw)
    if numeric is not None:
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        if numeric >= 1_000_000_000:
            return numeric
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_sold_at(row, status_div=None) -> Optional[float]:
    """Extract a real timestamp when Steam exposes one; never infer a date."""
    nodes = []
    if status_div is not None:
        nodes.append(status_div)
        nodes.extend(status_div.find_all(True))
    nodes.append(row)
    for node in nodes:
        for attr in ("data-timestamp", "data-time", "datetime", "title", "aria-label"):
            stamp = _timestamp_from_value(node.get(attr))
            if stamp is not None:
                return stamp
        text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
        for match in _DATE_PATTERN.finditer(text or ""):
            candidate = re.sub(r"\s+", " ", match.group(0).replace(",", " ")).strip()
            parsed = parse_steam_history_date(candidate)
            if parsed is not None:
                return parsed.timestamp()
    return None


def _history_total_count(data: dict) -> Optional[int]:
    for key in ("total_count", "totalCount", "total"):
        try:
            count = int(data.get(key))
            if count >= 0:
                return count
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _parse_history_page(data: dict, account_currency: Optional[str]) -> tuple[list, int]:
    from bs4 import BeautifulSoup

    row_to_assetid = {
        match.group(1): str(match.group(4))
        for match in _HOVER_PATTERN.finditer(data.get("hovers") or "")
    }
    soup = BeautifulSoup(data.get("results_html") or "", "html.parser")
    rows = [
        element for element in soup.find_all("div", class_="market_listing_row")
        if (element.get("id") or "").startswith("history_row_")
    ]
    sold_rows = []
    for row in rows:
        row_id = row.get("id") or ""
        assetid = row_to_assetid.get(row_id)
        if not assetid:
            raw_row = str(row)
            fallback = re.search(
                r"assetid[\"']?\s*[:=]\s*[\"']?(\d+)", raw_row, re.IGNORECASE
            )
            if fallback:
                assetid = fallback.group(1)
            else:
                link = row.find("a", href=re.compile(r"assetid=\d+"))
                if link and link.get("href"):
                    match = re.search(r"assetid=(\d+)", link["href"])
                    if match:
                        assetid = match.group(1)
        if not assetid:
            continue

        status_div = row.find("div", class_="market_listing_listed_date_combined")
        status_text = status_div.get_text(" ", strip=True) if status_div else ""
        status_lower = (status_text or "").lower()
        if not any(token in status_lower for token in ("sold", "已售出", "出售")):
            continue

        name_element = (
            row.find("span", class_="market_listing_item_name")
            or row.find("a", class_="market_listing_item_name_link")
        )
        name = name_element.get_text(" ", strip=True) if name_element else ""
        if not name:
            name_link = row.find("a", href=re.compile(r"listings/730/"))
            if name_link and name_link.get("href"):
                name_match = re.search(r"listings/730/(.+)$", name_link["href"])
                if name_match:
                    from urllib.parse import unquote
                    name = unquote(name_match.group(1)).strip()
        price_element = row.find("span", class_="market_listing_price")
        raw_price = price_element.get_text(" ", strip=True) if price_element else ""
        currency = _currency_code_from_price_text(raw_price, account_currency)
        amount = _parse_history_price_amount(raw_price) if raw_price and currency else None
        sold_rows.append({
            "assetid": str(assetid),
            "name": (name or "").strip(),
            "raw_price": raw_price or "",
            "raw_amount": amount,
            "currency_code": currency or "",
            "sold_at": _extract_sold_at(row, status_div),
        })
    return sold_rows, len(rows)


def _fetch_all_sold_history(cookies: dict) -> list:
    account_currency = _history_account_currency(cookies)
    all_rows = []
    start = 0
    total_count = None
    for _page in range(HISTORY_MAX_PAGES):
        params = {
            "query": "",
            "start": start,
            "count": HISTORY_PAGE_SIZE,
            "contextid": 2,
            "appid": 730,
        }
        response = _get_with_retry(
            MYHISTORY_RENDER_URL, params, HISTORY_HEADERS, cookies, None
        )
        if response is None:
            raise RuntimeError("Steam 历史记录请求没有返回响应")
        if response.status_code != 200:
            raise RuntimeError(f"Steam 历史记录 HTTP {response.status_code}")
        data = response.json() if response.text else {}
        if not data.get("success"):
            raise RuntimeError(str(data.get("message") or "Steam 历史记录请求失败")[:180])
        if total_count is None:
            total_count = _history_total_count(data)
        page_rows, row_count = _parse_history_page(data, account_currency)
        all_rows.extend(page_rows)
        if row_count <= 0:
            break
        start += HISTORY_PAGE_SIZE
        if total_count is not None and start >= total_count:
            break
        if total_count is None and row_count < HISTORY_PAGE_SIZE:
            break
    return all_rows


def _load_exchange_rates() -> dict:
    path = Path(__file__).resolve().parent.parent / "config" / "exchange_rate.json"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rates = payload.get("rates") if isinstance(payload, dict) else None
        if isinstance(rates, dict):
            return {
                str(key).strip().upper(): float(value)
                for key, value in rates.items()
                if _as_number(value) is not None and float(value) > 0
            }
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _normalize_name(value: str) -> str:
    from unicodedata import normalize

    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = normalize("NFC", text)
    replacements = (
        ("(Factory New)", "(FN)"),
        ("(Minimal Wear)", "(MW)"),
        ("(Field-Tested)", "(FT)"),
        ("(Well-Worn)", "(WW)"),
        ("(Battle-Scarred)", "(BS)"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text.casefold().strip()


def _select_listing_for_sold(sold_at: Optional[float], listings: list) -> Optional[dict]:
    """Pick the most recent listing before Sold; unknown time requires one unique row."""
    valid = [
        dict(row) for row in listings
        if _as_number(row.get("at")) is not None and float(row.get("at") or 0) > 0
        and _as_number(row.get("price")) is not None and float(row.get("price") or 0) > 0
    ]
    if sold_at is None:
        return valid[0] if len(valid) == 1 else None
    earlier = [row for row in valid if float(row["at"]) <= float(sold_at)]
    if not earlier:
        return None
    return max(earlier, key=lambda row: float(row["at"]))


def _median_and_mad(values: list) -> tuple[Optional[float], float]:
    clean = [float(value) for value in values if _as_number(value) is not None and float(value) > 0]
    if not clean:
        return None, 100.0
    median = statistics.median(clean)
    if median <= 0:
        return None, 100.0
    mad = statistics.median([abs(value - median) for value in clean]) / median * 100.0
    return median, mad


def _format_timestamp(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "时间未解析"
    try:
        return datetime.fromtimestamp(float(timestamp)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "时间未解析"


def build_history_price_preview() -> dict:
    """Fetch Steam Sold history and build conservative CNY repair suggestions."""
    credentials = get_steam_credentials() or {}
    cookies = _cookies_to_dict(credentials.get("cookies") or "")
    if not cookies.get("steamLoginSecure"):
        raise RuntimeError("未配置有效的 Steam Cookie，请先登录 Steam")

    sold_rows = _fetch_all_sold_history(cookies)
    purchases = list(get_purchases() or [])
    listing_records = list(get_sales() or [])
    exchange_rates = _load_exchange_rates()

    purchase_by_assetid = defaultdict(list)
    purchase_by_name = defaultdict(list)
    listings_by_assetid = defaultdict(list)
    for purchase in purchases:
        assetid = str(purchase.get("assetid") or "").strip()
        if assetid:
            purchase_by_assetid[assetid].append(purchase)
        normalized_name = _normalize_name(purchase.get("name") or "")
        if normalized_name:
            purchase_by_name[normalized_name].append(purchase)
    for listing in listing_records:
        assetid = str(listing.get("assetid") or "").strip()
        if assetid:
            listings_by_assetid[assetid].append(listing)

    prepared = []
    calibration_by_currency = defaultdict(list)
    listing_match_count = 0
    for sold in sold_rows:
        assetid = str(sold.get("assetid") or "").strip()
        currency = str(sold.get("currency_code") or "").strip().upper()
        amount = _as_number(sold.get("raw_amount"))
        sold_at = _as_number(sold.get("sold_at"))
        listing = _select_listing_for_sold(sold_at, listings_by_assetid.get(assetid, []))
        implied_rate = None
        if listing and amount and currency and sold_at is not None:
            listing_at = _as_number(listing.get("at"))
            listing_age = float(sold_at) - listing_at if listing_at is not None else None
            # A stale listing is not evidence of the final price or the FX
            # rate at sale time. Only use listings made within 7 days of sale.
            if listing_age is not None and 0 <= listing_age <= 7 * 24 * 60 * 60:
                implied_rate = float(listing["price"]) / (amount * STEAM_FEE_FACTOR)
                if math.isfinite(implied_rate) and implied_rate > 0:
                    calibration_by_currency[currency].append({
                        "rate": implied_rate,
                        "sold_at": sold_at,
                        "assetid": assetid,
                    })
                    listing_match_count += 1
                else:
                    implied_rate = None
        prepared.append({
            **sold,
            "assetid": assetid,
            "currency_code": currency,
            "raw_amount": amount,
            "sold_at": sold_at,
            "listing": listing,
            "own_implied_rate": implied_rate,
        })

    grouped_by_purchase = defaultdict(list)
    for sold in prepared:
        assetid = sold["assetid"]
        if not assetid:
            continue
        purchase_matches = purchase_by_assetid.get(assetid, [])
        match_method = "assetid"
        purchase = purchase_matches[0] if len(purchase_matches) == 1 else None
        if len(purchase_matches) > 1:
            continue
        if purchase is None and sold["listing"] and sold["sold_at"] is not None:
            normalized_name = _normalize_name(sold.get("name") or "")
            possible = [
                item for item in purchase_by_name.get(normalized_name, [])
                if _as_number(item.get("at")) is not None
                and float(item.get("at") or 0) <= float(sold["listing"]["at"]) <= float(sold["sold_at"])
            ]
            if len(possible) == 1:
                purchase = possible[0]
                match_method = "name_timeline"
        if purchase is None or not purchase.get("_db_id"):
            continue
        grouped_by_purchase[int(purchase["_db_id"])].append({
            "purchase": purchase,
            "sold": sold,
            "match_method": match_method,
        })

    suggestions = []
    ambiguous_purchase_count = 0
    for purchase_id, matches in grouped_by_purchase.items():
        if len(matches) != 1:
            ambiguous_purchase_count += 1
            continue
        match = matches[0]
        purchase = match["purchase"]
        sold = match["sold"]
        currency = sold["currency_code"]
        amount = sold["raw_amount"]
        if not currency or amount is None or amount <= 0:
            continue

        listing = sold["listing"]
        exact_purchase_asset = match["match_method"] == "assetid"
        direct_listing_anchor = listing is not None
        sample_rows = calibration_by_currency.get(currency, [])
        if currency == "CNY":
            rate = 1.0
            sample_count = len(sample_rows)
            dispersion_pct = 0.0
        else:
            if sold["sold_at"] is not None:
                nearby = [
                    row for row in sample_rows
                    if row["sold_at"] is not None
                    and row["assetid"] != sold["assetid"]
                    and abs(float(row["sold_at"]) - float(sold["sold_at"])) <= CALIBRATION_WINDOW_SECONDS
                ]
            else:
                nearby = []
            if sold["own_implied_rate"] is not None and sold["sold_at"] is not None:
                own_sample = {
                    "rate": sold["own_implied_rate"],
                    "sold_at": sold["sold_at"],
                    "assetid": sold["assetid"],
                }
                nearby.append(own_sample)
            # Count independent items, not repeated history rows for the
            # same asset. A single listing must never authorize a foreign-FX
            # repair by itself.
            unique_samples = {}
            for sample in nearby:
                unique_samples.setdefault(sample["assetid"], sample)
            nearby = list(unique_samples.values())
            rate, dispersion_pct = _median_and_mad([row["rate"] for row in nearby])
            sample_count = len(nearby)
            if rate is None:
                continue

        suggested_price = round(float(amount) * float(rate) * STEAM_FEE_FACTOR, 2)
        if suggested_price <= 0 or not math.isfinite(suggested_price):
            continue
        old_price = _as_number(purchase.get("sale_price"))
        if old_price is not None and abs(old_price - suggested_price) < 0.01:
            continue

        listing_gap_pct = None
        if listing:
            listing_price = _as_number(listing.get("price"))
            if listing_price and listing_price > 0:
                listing_gap_pct = abs(suggested_price - listing_price) / listing_price * 100.0

        if currency == "CNY" and exact_purchase_asset:
            # CNY history requires no FX inference. Missing values can use
            # the explicit Steam Sold amount; an existing positive value is
            # replaced only when a matching listing anchor corroborates it.
            if old_price is None:
                confidence = "高"
            elif direct_listing_anchor and listing_gap_pct is not None and listing_gap_pct <= 8.0:
                confidence = "高"
            else:
                confidence = "低"
        elif (
            direct_listing_anchor
            and exact_purchase_asset
            and sold["sold_at"] is not None
            and sample_count >= 3
            and dispersion_pct <= 3.0
            and (listing_gap_pct is None or listing_gap_pct <= 5.0)
        ):
            confidence = "高"
        elif (
            direct_listing_anchor
            and exact_purchase_asset
            and sold["sold_at"] is not None
            and sample_count >= 3
            and dispersion_pct <= 5.0
            and (listing_gap_pct is None or listing_gap_pct <= 10.0)
        ):
            confidence = "中"
        elif (
            old_price is None
            and exact_purchase_asset
            and sold["sold_at"] is not None
            and sample_count >= 3
            and dispersion_pct <= 3.0
        ):
            confidence = "中"
        else:
            confidence = "低"

        current_rate = exchange_rates.get(currency)
        current_estimate = (
            round(float(amount) * float(current_rate) * STEAM_FEE_FACTOR, 2)
            if current_rate and current_rate > 0 else None
        )
        evidence = []
        evidence.append("Steam 历史明确标记 Sold")
        evidence.append("AssetID 精确匹配操作记录" if exact_purchase_asset else "通过唯一名称与时间链关联")
        if listing:
            evidence.append("命中同 AssetID 人民币上架记录")
        if sold["sold_at"] is not None:
            evidence.append("Steam 成交时间已解析")
        else:
            evidence.append("Steam 成交时间未解析，未使用名称时间推断")
        evidence.append(f"{currency} 历史锚点 {sample_count} 条，中位汇率 {rate:.8g} CNY/{currency}")
        if dispersion_pct > 3.0:
            evidence.append(f"锚点离散度 {dispersion_pct:.2f}%")
        if listing_gap_pct is not None:
            evidence.append(f"估算价与上架价偏差 {listing_gap_pct:.2f}%")

        suggestions.append({
            "candidate_id": str(purchase_id),
            "purchase_id": purchase_id,
            "expected_purchase_assetid": str(purchase.get("assetid") or ""),
            "expected_purchase_at": _as_number(purchase.get("at")),
            "name": purchase.get("name") or sold.get("name") or "未知物品",
            "assetid": sold["assetid"],
            "current_sale_price": old_price,
            "suggested_sale_price": suggested_price,
            "delta": round(suggested_price - (old_price or 0.0), 2),
            "raw_price": sold.get("raw_price") or "未读取",
            "raw_amount": amount,
            "currency_code": currency,
            "sold_at": sold["sold_at"],
            "sold_at_label": _format_timestamp(sold["sold_at"]),
            "listing_price_cny": _as_number(listing.get("price")) if listing else None,
            "listing_at": _as_number(listing.get("at")) if listing else None,
            "listing_at_label": _format_timestamp(_as_number(listing.get("at")) if listing else None),
            "implied_rate": round(float(rate), 8),
            "current_rate": current_rate,
            "current_rate_estimate": current_estimate,
            "calibration_samples": sample_count,
            "dispersion_pct": round(float(dispersion_pct), 3),
            "listing_gap_pct": round(float(listing_gap_pct), 3) if listing_gap_pct is not None else None,
            "confidence": confidence,
            "evidence": "；".join(evidence),
            "can_apply": (
                confidence in ("高", "中")
                and exact_purchase_asset
                and (old_price is None or direct_listing_anchor)
            ),
        })

    suggestions.sort(key=lambda row: (row["confidence"] != "高", row["sold_at"] or 0), reverse=False)
    return {
        "items": suggestions,
        "stats": {
            "sold_rows": len(sold_rows),
            "purchases": len(purchases),
            "listing_matches": listing_match_count,
            "calibration_anchors": sum(len(rows) for rows in calibration_by_currency.values()),
            "ambiguous_purchases": ambiguous_purchase_count,
            "currencies": sorted(calibration_by_currency.keys()),
        },
    }
