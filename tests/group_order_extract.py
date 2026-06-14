"""Standalone test: DoorDash web groupCart GraphQL (www.doordash.com).

Uses consumer *web* session cookies (ddweb_token, cf_clearance, etc.) —
NOT the static DOORDASH_AUTHORIZATION header (that is identity/risk-bff only).

Usage:
    python tests/test_group_cart.py
    python tests/test_group_cart.py --cart-id <uuid>
    python tests/test_group_cart.py --cookies-file config/doordash_cookies.txt
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from doordash.constants import IMPERSONATE

GRAPHQL_URL = "https://www.doordash.com/graphql/groupCart?operation=groupCart"
DEFAULT_CART_ID = "5dc45ad0-8715-4950-b7c2-89d3275a7209"
DEFAULT_COOKIES_FILE = ROOT / "config" / "doordash_cookies.txt"
QUERY_FILE = ROOT / "graphql" / "group_cart.graphql"

# Must match curl_cffi impersonate target (chrome120), not a random Chrome version.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def generate_csrf_token(length: int = 43) -> str:
    """DoorDash web sets csrf_token client-side; a random 43-char value works if cookie+header match."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or part.startswith("#") or "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def load_cookies(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Cookie file not found: {path}\n"
            f"Copy {ROOT / 'config' / 'doordash_cookies.example.txt'} and paste your cookies."
        )
    raw = path.read_text(encoding="utf-8")
    cookies = parse_cookie_string(raw)
    if not cookies:
        raise ValueError(f"No cookies parsed from {path}")
    return cookies


def load_query() -> str:
    return QUERY_FILE.read_text(encoding="utf-8").strip()


def _sec_fetch_navigate() -> dict[str, str]:
    return {
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
    }


def _sec_fetch_cors() -> dict[str, str]:
    return {
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def build_browser_headers(cart_id: str, csrf: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US",
        "origin": "https://www.doordash.com",
        "referer": f"https://www.doordash.com/cart/{cart_id}/",
        "user-agent": BROWSER_UA,
        "x-experience-id": "doordash",
        **_sec_fetch_navigate(),
    }
    if csrf:
        headers["x-csrftoken"] = csrf
    return headers


def build_graphql_headers(csrf: str, cart_id: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-US",
        "apollographql-client-name": "@doordash/app-consumer-production-ssr-client",
        "apollographql-client-version": "3.0",
        "content-type": "application/json",
        "origin": "https://www.doordash.com",
        "referer": f"https://www.doordash.com/cart/{cart_id}/",
        "user-agent": BROWSER_UA,
        "x-channel-id": "marketplace",
        "x-csrftoken": csrf,
        "x-experience-id": "doordash",
        **_sec_fetch_cors(),
    }


def _request_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {"impersonate": IMPERSONATE, "timeout": 30}
    proxy = os.getenv("DOORDASH_PROXY")
    if proxy:
        if "://" not in proxy:
            parts = proxy.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                proxy = f"http://{user}:{password}@{host}:{port}"
            else:
                proxy = f"http://{proxy}"
        kw["proxies"] = {"http": proxy, "https": proxy}
    return kw


def merge_session_cookies(
    base: dict[str, str],
    session: requests.Session,
) -> dict[str, str]:
    """Merge jar cookies without curl_cffi CookieConflict on duplicate domains."""
    merged = dict(base)
    for cookie in session.cookies.jar:
        domain = cookie.domain or ""
        if domain and "doordash.com" not in domain:
            continue
        merged[cookie.name] = cookie.value
    return merged


def resolve_csrf(cookies: dict[str, str]) -> str:
    existing = (cookies.get("csrf_token") or "").strip()
    if existing:
        return existing
    return generate_csrf_token()


def fetch_group_cart(
    cart_id: str,
    cookies: dict[str, str],
    *,
    warm: bool = True,
) -> tuple[dict[str, Any], str]:
    """Warm cart page then POST groupCart GraphQL on one session."""
    session = requests.Session()
    active_cookies = dict(cookies)

    if warm:
        warm_url = f"https://www.doordash.com/cart/{cart_id}/"
        warm_response = session.get(
            warm_url,
            headers=build_browser_headers(cart_id),
            cookies=active_cookies,
            **_request_kwargs(),
        )
        active_cookies = merge_session_cookies(active_cookies, session)
        if warm_response.status_code >= 400:
            raise RequestException(
                f"Warm-up GET failed HTTP {warm_response.status_code}: "
                f"{warm_response.text[:300]}"
            )

    csrf = resolve_csrf(active_cookies)
    active_cookies["csrf_token"] = csrf

    payload = {
        "operationName": "groupCart",
        "variables": {
            "id": cart_id,
            "shouldApplyAutocheckoutConfig": True,
        },
        "query": load_query(),
    }

    response = session.post(
        GRAPHQL_URL,
        headers=build_graphql_headers(csrf, cart_id),
        cookies=active_cookies,
        json=payload,
        **_request_kwargs(),
    )
    if response.status_code >= 400:
        raise RequestException(
            f"HTTP {response.status_code}: {response.text[:500]}"
        )
    return response.json(), csrf


def _money_str(money: dict[str, Any] | None) -> str:
    if not money:
        return "—"
    return money.get("displayString") or str(money.get("unitAmount", "—"))


def print_group_order_info(data: dict[str, Any]) -> None:
    if data.get("errors"):
        print("GraphQL errors:")
        print(json.dumps(data["errors"], indent=2))

    cart = (data.get("data") or {}).get("orderCart")
    if not cart:
        print("\nNo orderCart returned. Full response:")
        print(json.dumps(data, indent=2))
        return

    restaurant = cart.get("restaurant") or {}
    creator = cart.get("creator") or {}
    pre_checkout = cart.get("groupCartPreCheckoutDetails") or {}
    delivery = pre_checkout.get("deliveryAddress") or {}

    print("=" * 60)
    print("GROUP ORDER")
    print("=" * 60)
    print(f"Cart ID:        {cart.get('id')}")
    print(f"Cart type:      {cart.get('cartType')}")
    print(f"Group cart:     {cart.get('groupCart')}")
    print(f"Group type:     {cart.get('groupCartType')}")
    print(f"Source:         {cart.get('groupCartSource')}")
    print(f"Status:         {cart.get('cartStatusType')}")
    print(f"URL code:       {cart.get('urlCode')}")
    print(f"Short link:     {cart.get('shortenedUrl')}")
    print()
    print(f"Restaurant:     {restaurant.get('name')}")
    rest_addr = (restaurant.get("address") or {}).get("printableAddress")
    if rest_addr:
        print(f"Store address:  {rest_addr}")
    print()
    print(f"Creator:        {creator.get('firstName', '')} {creator.get('lastName', '')}".strip())
    if delivery.get("printableAddress"):
        print(f"Delivery:       {delivery['printableAddress']}")
    print()
    print(f"Subtotal:       {cart.get('subtotal')}")
    print(f"Total:          {cart.get('total')} {cart.get('currencyCode') or ''}".strip())

    line_items = cart.get("lineItemsList") or []
    if line_items:
        print("\nLine items:")
        for item in line_items:
            print(f"  • {item.get('label')}: {_money_str(item.get('finalMoney'))}")

    orders = cart.get("orders") or []
    if orders:
        print(f"\nParticipant orders ({len(orders)}):")
        for order in orders:
            consumer = order.get("consumer") or {}
            name = f"{consumer.get('firstName', '')} {consumer.get('lastName', '')}".strip() or "Unknown"
            finalized = "finalized" if order.get("isSubCartFinalized") else "open"
            print(f"\n  [{finalized}] {name} (order {order.get('id')})")
            for order_item in order.get("orderItems") or []:
                info = order_item.get("item") or {}
                qty = order_item.get("quantity", 1)
                price = order_item.get("priceDisplayString") or info.get("price")
                print(f"    {qty}x {info.get('name', 'Item')} — {price}")
                for opt in order_item.get("options") or []:
                    print(f"       + {opt.get('quantity', 1)}x {opt.get('name')}")
                if order_item.get("specialInstructions"):
                    print(f"       Note: {order_item['specialInstructions']}")

    print("=" * 60)


def main() -> int:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Test DoorDash groupCart GraphQL request")
    parser.add_argument("--cart-id", default=DEFAULT_CART_ID, help="Group cart UUID")
    parser.add_argument(
        "--cookies-file",
        type=Path,
        default=DEFAULT_COOKIES_FILE,
        help="Path to semicolon-separated DoorDash cookies",
    )
    parser.add_argument(
        "--no-warm",
        action="store_true",
        help="Skip GET warm-up to cart page before GraphQL POST",
    )
    args = parser.parse_args()

    try:
        cookies = load_cookies(args.cookies_file)
        data, csrf = fetch_group_cart(args.cart_id, cookies, warm=not args.no_warm)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        print(
            "Tips:\n"
            "  • Paste fresh cookies from DevTools → Network → groupCart → Cookie header "
            "(need ddweb_token + cf_clearance).\n"
            "  • cf_clearance is IP-bound — set DOORDASH_PROXY in .env to the same IP "
            "you used in the browser, or refresh cookies after solving CF in-browser.\n"
            "  • GraphQL POST requires sec-fetch-* headers (included in this script).",
            file=sys.stderr,
        )
        return 1

    print(f"CSRF token: {csrf}\n")
    print_group_order_info(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
