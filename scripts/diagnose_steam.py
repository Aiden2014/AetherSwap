"""Read-only Steam diagnostics. Never logs cookies or invokes trading routes."""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configured-proxy", action="store_true")
    parser.add_argument("--check", choices=("inventory", "session", "profile", "wallet", "history", "market"), default="inventory")
    args = parser.parse_args()

    from config import get_steam
    from app.services.steam_auth import steam_id_from_cookie_str

    credentials = get_steam()
    steam_id = credentials.get("steam_id", "")
    cookies = credentials.get("cookies", "")
    print(json.dumps({"check": args.check, "configured_proxy": args.configured_proxy,
                      "credentials_present": bool(steam_id and cookies),
                      "cookie_account_matches": steam_id_from_cookie_str(cookies) == steam_id}), flush=True)

    original_request = requests.Session.request
    original_send = requests.Session.send
    if args.configured_proxy:
        from utils.proxy_manager import get_proxy_manager

        proxies = get_proxy_manager().get_proxies_for_request()
        if not proxies:
            raise RuntimeError("No proxy selected by the configured policy")

        def request(self, method, url, **kwargs):
            kwargs["proxies"] = dict(proxies)
            return original_request(self, method, url, **kwargs)

        requests.Session.request = request

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        parts = parsed.path.strip("/").split("/")
        endpoint = parts[0]
        if endpoint == "inventory":
            endpoint += "/<account>/" + "/".join(parts[2:])
        record = {"host": parsed.hostname, "endpoint": endpoint,
                  "uses_https_proxy": bool((kwargs.get("proxies") or {}).get("https"))}
        started = time.monotonic()
        try:
            response = original_send(self, request, **kwargs)
            record.update(status=response.status_code,
                          content_type=response.headers.get("Content-Type"),
                          transport=getattr(response, "steam_transport", "requests"))
            if "json" in (response.headers.get("Content-Type") or ""):
                payload = response.json()
                if isinstance(payload, dict):
                    record.update(success=payload.get("success"),
                                  asset_count=len(payload.get("assets") or []),
                                  total_inventory_count=payload.get("total_inventory_count"))
            return response
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps(record), flush=True)

    requests.Session.send = send
    try:
        if args.check == "inventory":
            from app.inventory_cs2 import scan_cs2_inventory

            ok, items, error = scan_cs2_inventory()
            result = {"ok": ok, "item_count": len(items), "error": error}
        elif args.check == "session":
            from app.services.steam_auth import _verify_steam_cookies_valid

            valid = _verify_steam_cookies_valid(cookies, steam_id)
            result = {"ok": valid is True, "session_valid": valid}
        elif args.check == "profile":
            from app.services.steam_auth import fetch_steam_profile_via_api

            name, avatar = fetch_steam_profile_via_api(steam_id, cookies)
            result = {"ok": bool(name and avatar), "name_present": bool(name), "avatar_present": bool(avatar)}
        elif args.check == "history":
            from app.services.steam_client import SteamClient

            raw = SteamClient().fetch_history("Prisma Case", return_currency=True)
            result = {"ok": bool(raw and raw.get("history")),
                      "history_rows": len((raw or {}).get("history") or []),
                      "currency": (raw or {}).get("currency")}
        elif args.check == "market":
            from app.pipeline_steps import _fetch_steam_sell_data
            from app.config_loader import load_app_config_validated

            data, error = _fetch_steam_sell_data("Prisma Case", load_app_config_validated(), return_error=True)
            result = {"ok": bool(data and data.get("sell_orders")),
                      "order_levels": len((data or {}).get("sell_orders") or []), "error": error}
        else:
            from app.gift_engine import get_wallet_balance

            try:
                wallet = get_wallet_balance(cookies)
                result = {"ok": bool(wallet.get("currency_code")), "currency_code": wallet.get("currency_code")}
            except Exception as exc:
                result = {"ok": False, "error_type": type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["ok"] else 1
    finally:
        requests.Session.request = original_request
        requests.Session.send = original_send


if __name__ == "__main__":
    raise SystemExit(main())
