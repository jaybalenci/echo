"""Extract a source group cart and rebuild it on a build account via addCartItem.

Usage:
    python tests/build_cart.py --source-cart-id <uuid>
    python tests/build_cart.py --extract-only
    python tests/build_cart.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curl_cffi.requests.exceptions import RequestException

from doordash.cart_extract import extract_cart_items, summarize_cart
from doordash.rebuild import rebuild_cart

from doordash.web_client import fetch_detailed_cart, load_cookies

DEFAULT_SOURCE_CART_ID = "5dc45ad0-8715-4950-b7c2-89d3275a7209"
DEFAULT_SOURCE_COOKIES = ROOT / "config" / "doordash_cookies.txt"
DEFAULT_BUILD_COOKIES = ROOT / "config" / "doordash_build_cookies.txt"


def print_extracted(summary: dict[str, Any]) -> None:
    print("=" * 60)
    print("SOURCE CART (extracted)")
    print("=" * 60)
    print(f"Cart ID:     {summary['cart_id']}")
    print(f"Restaurant:  {summary['restaurant']}")
    print(f"Store ID:    {summary['store_id']}")
    print(f"Menu ID:     {summary['menu_id']}")
    print(f"Subtotal:    {summary['subtotal']}")
    print(f"Total:       {summary['total']}")
    print(f"Items:       {summary['item_count']}")
    for idx, item in enumerate(summary["items"], 1):
        print(f"\n  {idx}. {item['quantity']}x {item['name']} (${item['unit_price'] / 100:.2f})")
        if item["options"] != "[]":
            print(f"     options: {item['options']}")
    print("=" * 60)


def print_built_cart(cart: dict[str, Any]) -> None:
    restaurant = cart.get("restaurant") or {}
    print("\n" + "=" * 60)
    print("REBUILT CART")
    print("=" * 60)
    print(f"Cart ID:     {cart.get('id')}")
    print(f"Restaurant:  {restaurant.get('name')}")
    print(f"Subtotal:    {cart.get('subtotal')}")
    print(f"Total:       {cart.get('total')} {cart.get('currencyCode') or ''}".strip())
    print(f"Short link:  {cart.get('shortenedUrl')}")
    for order in cart.get("orders") or []:
        for order_item in order.get("orderItems") or []:
            info = order_item.get("item") or {}
            print(
                f"  • {order_item.get('quantity')}x {info.get('name')} "
                f"— {order_item.get('priceDisplayString')}"
            )
    print("=" * 60)


def main() -> int:
    load_dotenv(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Extract and rebuild a DoorDash group cart")
    parser.add_argument(
        "--source-cart-id",
        default=DEFAULT_SOURCE_CART_ID,
        help="Group cart UUID to copy from",
    )
    parser.add_argument(
        "--source-cookies-file",
        type=Path,
        default=DEFAULT_SOURCE_COOKIES,
        help="Cookies to read the source cart",
    )
    parser.add_argument(
        "--build-cookies-file",
        type=Path,
        default=DEFAULT_BUILD_COOKIES,
        help="Cookies for the designated build account",
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Only fetch and print source cart items",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and show what would be added, without calling addCartItem",
    )
    args = parser.parse_args()

    try:
        source_cookies = load_cookies(args.source_cookies_file)
        source_data = fetch_detailed_cart(args.source_cart_id, source_cookies)
        source_cart = (source_data.get("data") or {}).get("orderCart")
        if not source_cart:
            print("No orderCart in source response:", json.dumps(source_data, indent=2))
            return 1

        summary = summarize_cart(source_cart)
        print_extracted(summary)
        specs = extract_cart_items(source_cart)

        if args.extract_only or args.dry_run:
            if args.dry_run:
                print("\nDry run — would add these items to build account.")
            return 0

        if not specs:
            print("No items to rebuild.", file=sys.stderr)
            return 1

        build_cookies = load_cookies(args.build_cookies_file)
        restaurant = source_cart.get("restaurant") or {}
        menu_id = str((source_cart.get("menu") or {}).get("id") or "")
        def on_item_added(added: list[str]) -> None:
            if added:
                print(f"  Added: {added[-1]} ({len(added)} line(s) in cart)")

        rebuilt, failed, _, _ = rebuild_cart(
            specs,
            restaurant,
            menu_id,
            build_cookies,
            on_item_added=on_item_added,
        )
        if failed:
            print(f"\nFailed to add {len(failed)} item(s):")
            for line in failed:
                print(f"  • {line}")
        if rebuilt:
            print_built_cart(rebuilt)
        if failed:
            return 1
        if not rebuilt:
            print("\nNo items were added to the build cart.", file=sys.stderr)
            return 1

    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
