from typing import Optional
from urllib.parse import urlparse
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - dependency is declared in requirements
    curl_requests = None

MARKET_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


class _MarketReadAdapter(requests.adapters.HTTPAdapter):
    """Use curl_cffi Chrome impersonation for Steam market/inventory reads."""

    _READ_PREFIXES = (
        "/market/listings/", "/market/pricehistory/", "/market/itemordershistogram",
        "/market/orderbook", "/market/mylistings/", "/market/myhistory/", "/inventory/",
    )

    @classmethod
    def _is_market_read(cls, request) -> bool:
        parsed = urlparse(request.url)
        path = parsed.path
        return (
            request.method == "GET"
            and parsed.hostname == "steamcommunity.com"
            and (path in ("/market", "/market/") or path.startswith(cls._READ_PREFIXES))
        )

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if not self._is_market_read(request) or curl_requests is None:
            return super().send(
                request, stream=stream, timeout=timeout, verify=verify,
                cert=cert, proxies=proxies,
            )

        headers = {
            name: value for name, value in request.headers.items()
            if name.lower() not in {"host", "connection", "content-length", "accept-encoding"}
        }
        try:
            raw = curl_requests.get(
                request.url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                verify=verify,
                impersonate="chrome120",
            )
        except Exception as exc:
            if isinstance(exc, requests.RequestException):
                raise
            raise requests.ConnectionError(str(exc)) from exc

        response = requests.Response()
        response.status_code = raw.status_code
        response.url = raw.url
        response.headers.update(raw.headers)
        response.encoding = raw.encoding
        response._content = raw.content
        response._content_consumed = True
        response.request = request
        response.steam_transport = "curl_cffi"
        return response


class _SteamSession(requests.Session):
    """Apply the app's proxy policy to callers without their own routing loop."""

    def request(self, method, url, **kwargs):
        if urlparse(url).hostname == "steamcommunity.com":
            from steam.request_policy import check_steam_market_cooldown

            check_steam_market_cooldown()
        # Market order-book callers already select proxies and own their retries.
        if "proxies" in kwargs:
            return super().request(method, url, **kwargs)

        from utils.proxy_manager import get_proxy_manager

        manager = get_proxy_manager()
        proxies = manager.get_proxies_for_request()
        if manager.should_always_use_proxy() and not proxies:
            raise requests.exceptions.ProxyError("Steam 代理已启用，但没有可用节点")
        if proxies:
            # Pass per request so environment proxies cannot override this choice.
            kwargs["proxies"] = dict(proxies)
        try:
            return super().request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            # A failed write may have reached Steam. Never replay it implicitly.
            if method.upper() not in ("GET", "HEAD") or not manager.should_use_proxy_on_failure():
                raise
            fallback = manager.get_proxies_for_request(failed=True)
            if not fallback or fallback == proxies:
                raise
            kwargs["proxies"] = dict(fallback)
            return super().request(method, url, **kwargs)


def create_steam_session(*, verify: bool = False) -> requests.Session:
    session = _SteamSession()
    session.verify = verify
    session.mount("https://steamcommunity.com/market", _MarketReadAdapter())
    session.mount("https://steamcommunity.com/inventory/", _MarketReadAdapter())
    return session


def get_market(url, *, headers=None, cookies=None, proxies=None, timeout=15, verify=False):
    with create_steam_session(verify=verify) as session:
        if cookies:
            session.cookies.update(cookies)
        kwargs = {"headers": headers, "timeout": timeout}
        if proxies is not None:
            kwargs["proxies"] = proxies
        return session.get(url, **kwargs)


def parse_cookies(cookie_str: str) -> dict:
    out = {}
    for item in cookie_str.split(";"):
        s = item.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out
def create_market_session(
    cookies_raw: str,
    steam_id: str,
    *,
    headers: Optional[dict] = None,
    verify: bool = False,
) -> requests.Session:
    session = create_steam_session(verify=verify)
    session.cookies.update(parse_cookies(cookies_raw))
    h = {**MARKET_HEADERS, "Referer": f"https://steamcommunity.com/profiles/{steam_id}/inventory/"}
    if headers:
        h.update(headers)
    session.headers.update(h)
    return session
