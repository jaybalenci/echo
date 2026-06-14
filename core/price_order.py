"""End-to-end /price flow: extract → rebuild → address → checkout."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from curl_cffi.requests.exceptions import RequestException

from core.account_pool import acquire
from doordash.address import set_delivery_address, validate_address
from doordash.cart_extract import extract_cart_items
from doordash.checkout import PriceBreakdown, apply_promo_code, fetch_checkout, summarize_order_items
from doordash.group_order import join_group_order
from doordash.rebuild import rebuild_cart
from doordash.web_client import DoorDashWebSession, cart_referer
from views.order_views import PriceBreakdownFields


@dataclass
class PriceOrderResult:
    address: str
    store: str
    items: list[tuple[str, int]]
    pricing: PriceBreakdownFields
    cart_id: str
    failures: list[str]


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
            _status("Validating address...")
            pre_client = DoorDashWebSession(cookies)
            try:
                pre_client.warm("https://www.doordash.com/")
            except RequestException as exc:
                if "403" in str(exc):
                    tried.add(idx)
                    continue  # release this account and try the next one
                raise

            validate_address(pre_client, address)

            _status("Fetching group order...")
            cart_id, _, source_cart = join_group_order(cookies, order_link)

            specs = extract_cart_items(source_cart)
            if not specs:
                raise RuntimeError("That cart has no items to rebuild.")

            restaurant = source_cart.get("restaurant") or {}
            menu_id = str((source_cart.get("menu") or {}).get("id") or "")

            rebuilt, failures, client, built_cart_id = rebuild_cart(
                specs,
                restaurant,
                menu_id,
                cookies,
                on_item_added=on_item_added,
                on_status=on_status,
            )
            if not built_cart_id:
                if failures:
                    lines = "\n".join(f"• {f}" for f in failures)
                    raise RuntimeError(f"Could not add any items to the cart:\n{lines}")
                raise RuntimeError("The cart appears to be empty. Make sure the group order has items before price-checking.")

            referer = cart_referer(built_cart_id)
            client.warm(referer)

            address_result = set_delivery_address(client, address, referer=referer)

            default_address = address_result.get("default_address") or {}
            printable_address = default_address.get("printableAddress") or address
            lat = float(default_address.get("lat") or 0)
            lng = float(default_address.get("lng") or 0)

            if promo_code and promo_code != "Not Set":
                _status("Applying promotion...")
                apply_promo_code(client, built_cart_id, promo_code, lat=lat, lng=lng)

            checkout_cart = fetch_checkout(
                client,
                built_cart_id,
                lat=lat,
                lng=lng,
            )

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

        return PriceOrderResult(
            address=printable_address,
            store=restaurant.get("name") or "Unknown Restaurant",
            items=items,
            pricing=pricing,
            cart_id=built_cart_id,
            failures=failures,
        )
