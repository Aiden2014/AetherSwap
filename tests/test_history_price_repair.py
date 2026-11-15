from datetime import datetime, timezone

from bs4 import BeautifulSoup

from app.history_price_repair import (
    _extract_sold_at,
    _median_and_mad,
    _select_listing_for_sold,
)


def test_extract_sold_timestamp_from_steam_history_text():
    html = """
    <div class="market_listing_row" id="history_row_123_456">
      <div class="market_listing_listed_date_combined">Sold on Aug 01 2026 01: +0</div>
    </div>
    """
    row = BeautifulSoup(html, "html.parser").find("div", class_="market_listing_row")
    status = row.find("div", class_="market_listing_listed_date_combined")
    timestamp = _extract_sold_at(row, status)
    expected = datetime(2026, 8, 1, 1, tzinfo=timezone.utc).timestamp()
    assert timestamp == expected


def test_extract_sold_timestamp_returns_none_without_real_date():
    html = '<div class="market_listing_row"><div class="market_listing_listed_date_combined">Sold</div></div>'
    row = BeautifulSoup(html, "html.parser").find("div", class_="market_listing_row")
    assert _extract_sold_at(row, row.find("div")) is None


def test_listing_match_uses_latest_listing_before_sold():
    listings = [
        {"at": 100.0, "price": 20.0},
        {"at": 120.0, "price": 21.0},
        {"at": 140.0, "price": 22.0},
    ]
    match = _select_listing_for_sold(130.0, listings)
    assert match["at"] == 120.0
    assert match["price"] == 21.0


def test_listing_match_rejects_future_or_ambiguous_unknown_time():
    listings = [
        {"at": 140.0, "price": 22.0},
        {"at": 150.0, "price": 23.0},
    ]
    assert _select_listing_for_sold(130.0, listings) is None
    assert _select_listing_for_sold(None, listings) is None


def test_median_and_mad_resist_single_outlier():
    median, dispersion = _median_and_mad([5.0, 5.02, 4.98, 9.0])
    assert median == 5.01
    assert dispersion < 1.0
