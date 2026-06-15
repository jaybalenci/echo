"""Rebuild a source cart on the build account."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from curl_cffi.requests.exceptions import RequestException

from doordash.cart_extract import CartItemSpec
from doordash.item_options import build_nested_from_item_page, fetch_item_page
from doordash.web_client import (
    DoorDashWebSession,
    add_cart_item,
    list_detailed_carts,
    remove_cart_item,
    store_referer,
)


def _clear_existing_carts(
    client: DoorDashWebSession,
    store_id: str,
    referer: str,
) -> None:
    """Find and clear any active build-account carts for this store."""
    print(f"[clear_cart] Fetching active carts for store {store_id}")
    try:
        detailed = list_detailed_carts(client, referer=referer)
    except Exception as exc:
        print(f"[clear_cart] listDetailedCarts failed: {exc}")
        return

    print(f"[clear_cart] Found {len(detailed)} active cart(s) total")
    for entry in detailed:
        cart = entry.get("cart") or {}
        cart_id = str(cart.get("id") or "")
        restaurant = cart.get("restaurant") or {}
        cart_store_id = str(restaurant.get("id") or "")
        print(f"[clear_cart] Cart {cart_id} → store_id={cart_store_id}")

        if cart_store_id != store_id:
            print(f"[clear_cart] Skipping (different store)")
            continue

        item_ids = [
            str(order_item["id"])
            for order in (cart.get("orders") or [])
            for order_item in (order.get("orderItems") or [])
            if order_item.get("id")
        ]

        if not item_ids:
            print(f"[clear_cart] Cart {cart_id} already empty")
            continue

        print(f"[clear_cart] Removing {len(item_ids)} item(s) from cart {cart_id}: {item_ids}")
        for item_id in item_ids:
            try:
                resp = remove_cart_item(client, cart_id=cart_id, item_id=item_id, referer=referer)
                if resp.get("errors"):
                    print(f"[clear_cart] Error removing {item_id}: {resp['errors']}")
                else:
                    print(f"[clear_cart] Removed {item_id} OK")
            except Exception as exc:
                print(f"[clear_cart] Exception removing {item_id}: {exc}")


def rebuild_cart(
    specs: list[CartItemSpec],
    restaurant: dict[str, Any],
    menu_id: str,
    build_cookies: dict[str, str],
    *,
    on_item_added: Callable[[list[str]], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], list[str], DoorDashWebSession, str]:
    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    referer = store_referer(restaurant, menu_id)
    client = DoorDashWebSession(build_cookies)
    client.warm(referer)

    _status("Adding items...")

    cart_id = ""
    last_response: dict[str, Any] = {}
    last_successful_cart: dict[str, Any] = {}
    failed: list[str] = []
    added_lines: list[str] = []

    for spec in specs:
        nested_attempts = [spec.nested_options]
        if spec.fallback_nested_options and spec.fallback_nested_options != spec.nested_options:
            nested_attempts.append(spec.fallback_nested_options)
        if "[]" not in nested_attempts:
            nested_attempts.append("[]")

        added = False
        last_err = ""
        item_page_fetched = False
        print(f"[rebuild] Adding {spec.quantity}x {spec.item_name!r} (item_id={spec.item_id})")
        attempt_idx = 0
        while attempt_idx < len(nested_attempts):
            nested = nested_attempts[attempt_idx]
            print(f"  [attempt {attempt_idx}] nestedOptions={nested[:200]}")
            item_input = spec.to_add_cart_input()
            item_input["nestedOptions"] = nested
            try:
                last_response = add_cart_item(
                    client,
                    add_cart_item_input=item_input,
                    referer=referer,
                    cart_id=cart_id,
                )
            except RequestException as exc:
                last_err = str(exc)
                print(f"  [attempt {attempt_idx}] RequestException: {last_err}")
                attempt_idx += 1
                continue

            if last_response.get("errors"):
                last_err = json.dumps(last_response["errors"])[:300]
                print(f"  [attempt {attempt_idx}] GraphQL errors: {last_err}")

                # On "wrong level" error, fetch the item page and inject a
                # hierarchy-corrected attempt immediately after this one.
                if "wrong level" in last_err and not item_page_fetched:
                    item_page_fetched = True
                    print(f"  [item_page] fetching option hierarchy for item {spec.item_id}")
                    item_page = fetch_item_page(client, spec.store_id, spec.item_id, referer)
                    if item_page:
                        corrected = build_nested_from_item_page(nested, item_page)
                        if corrected and corrected not in nested_attempts:
                            nested_attempts.insert(attempt_idx + 1, corrected)
                attempt_idx += 1
                continue

            cart = (last_response.get("data") or {}).get("addCartItemV2")
            if not cart:
                last_err = "no addCartItemV2 in response"
                print(f"  [attempt {attempt_idx}] {last_err}")
                attempt_idx += 1
                continue

            cart_id = str(cart.get("id") or cart_id)
            last_successful_cart = cart
            added = True
            print(f"  [attempt {attempt_idx}] OK — cart_id={cart_id}")
            break

        if not added:
            print(f"[rebuild] FAILED {spec.item_name!r} after {len(nested_attempts)} attempts")
            if "Item is not available" in last_err:
                display_err = "Out of Stock"
            else:
                display_err = last_err
            failed.append(f"{spec.quantity}x {spec.item_name} — {display_err}")
            continue

        for _ in range(spec.quantity):
            added_lines.append(spec.item_name)
        if on_item_added:
            on_item_added(list(added_lines))

    return last_successful_cart, failed, client, cart_id


def schedule_cart_cleanup(
    client: DoorDashWebSession,
    cart_id: str,
    cart_data: dict[str, Any],
    referer: str,
) -> None:
    """Fire a daemon thread to remove all items from the cart after a price check.

    This keeps the account clean so the next price check skips the slow
    listDetailedCarts + removeCartItem loop inside _clear_existing_carts.
    """
    item_ids = [
        str(oi["id"])
        for order in (cart_data.get("orders") or [])
        for oi in (order.get("orderItems") or [])
        if oi.get("id")
    ]
    if not item_ids:
        return

    def _run() -> None:
        print(f"[cleanup] clearing {len(item_ids)} item(s) from cart {cart_id}")
        for item_id in item_ids:
            try:
                remove_cart_item(client, cart_id=cart_id, item_id=item_id, referer=referer)
            except Exception as exc:
                print(f"[cleanup] failed to remove {item_id}: {exc}")
        print(f"[cleanup] done — cart {cart_id} cleared")

    threading.Thread(target=_run, daemon=True).start()
