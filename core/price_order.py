"""End-to-end /price flow: extract → rebuild → address → checkout."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from curl_cffi.requests.exceptions import RequestException

from core.account_pool import acquire
from doordash.address import set_delivery_address, validate_address
from doordash.cart_extract import extract_cart_items
from doordash.checkout import PriceBreakdown, apply_promo_code, fetch_checkout, summarize_order_items
from doordash.group_order import join_group_order
from doordash.rebuild import rebuild_cart, schedule_cart_cleanup
from doordash.web_client import DoorDashWebSession, store_referer
from views.order_views import PriceBreakdownFields


@dataclass
class PriceOrderResult:
    address: str
    store: str
    items: list[tuple[str, int]]
    pricing: PriceBreakdownFields
    cart_id: str
    failures: list[str]
    cleanup_fn: Callable[[], None] | None = field(default=None, repr=False)


def run_price_order(
    order_link: str,
    address: str,
    *,
    on_item_added: Callable[[list[str]], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    promo_code: str = "YOUGOT40",
) -> PriceOrderResult:
    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    tried: set[int] = set()

    while True:
        with acquire(exclude=frozenset(tried)) as (idx, cookies):
            _status("Checking order...")
            pre_client = DoorDashWebSession(cookies)

            # Phase 1: warm session + fetch group order at the same time
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_warm = pool.submit(pre_client.warm, "https://www.doordash.com/")
                f_join = pool.submit(join_group_order, cookies, order_link)
            # Both are guaranteed done when the pool exits

            try:
                f_warm.result()
            except RequestException as exc:
                if "403" in str(exc):
                    tried.add(idx)
                    continue
                raise

            validate_address(pre_client, address)
            cart_id, _, source_cart = f_join.result()

            specs = extract_cart_items(source_cart)
            if not specs:
                raise RuntimeError("That cart has no items to rebuild.")

            restaurant = source_cart.get("restaurant") or {}
            menu_id = str((source_cart.get("menu") or {}).get("id") or "")

            # Phase 2: rebuild cart + set delivery address at the same time.
            # set_delivery_address only updates the account's default address (no cart
            # knowledge needed), so pre_client can handle it while rebuild runs.
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_rebuild = pool.submit(
                    rebuild_cart,
                    specs, restaurant, menu_id, cookies,
                    on_item_added=on_item_added,
                    on_status=on_status,
                )
                f_address = pool.submit(
                    set_delivery_address,
                    pre_client, address,
                    referer="https://www.doordash.com/",
                )

            rebuilt, failures, client, built_cart_id = f_rebuild.result()
            address_result = f_address.result()

            if not built_cart_id:
                if failures:
                    lines = "\n".join(f"• {f}" for f in failures)
                    raise RuntimeError(f"Could not add any items to the cart:\n{lines}")
                raise RuntimeError(
                    "The cart appears to be empty. Make sure the group order has items before price-checking."
                )

            default_address = address_result.get("default_address") or {}
            printable_address = default_address.get("printableAddress") or address
            lat = float(default_address.get("lat") or 0)
            lng = float(default_address.get("lng") or 0)

            if promo_code and promo_code != "Not Set":
                _status("Applying promotion...")
                apply_promo_code(client, built_cart_id, promo_code, lat=lat, lng=lng)

            checkout_cart = fetch_checkout(client, built_cart_id, lat=lat, lng=lng)

        breakdown = PriceBreakdown.from_cart(checkout_cart)
        items = summarize_order_items(checkout_cart)
        if not items and rebuilt:
            items = summarize_order_items(rebuilt)

        pricing = PriceBreakdownFields(
            subtotal_display=breakdown.subtotal_display,
            fees_tax_display=breakdown.fees_tax_display,
            delivery_fee_display=breakdown.delivery_fee_display,
            discounts_display=breakdown.discounts_display,
            total_display=breakdown.total_display,
        )

        # Capture cleanup args in a closure — called by main.py AFTER the price
        # is shown to the user, not during the price check itself.
        _client, _cart_id, _rebuilt, _referer = client, built_cart_id, rebuilt, store_referer(restaurant, menu_id)

        return PriceOrderResult(
            address=printable_address,
            store=restaurant.get("name") or "Unknown Restaurant",
            items=items,
            pricing=pricing,
            cart_id=built_cart_id,
            failures=failures,
            cleanup_fn=lambda: schedule_cart_cleanup(_client, _cart_id, _rebuilt, _referer),
        )
