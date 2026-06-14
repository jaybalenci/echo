"""Fetch checkout pricing for a rebuilt cart."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doordash.web_client import BROWSER_UA, DoorDashWebSession, load_query, request_kwargs

CHECKOUT_URL = "https://www.doordash.com/graphql/checkout?operation=checkout"
CHECKOUT_QUERY = Path(__file__).resolve().parent / "graphql" / "checkout.graphql"

APPLY_PROMO_URL = "https://www.doordash.com/unified-gateway/cx/ads/v1/apply_promotion"


def checkout_referer(cart_id: str, lat: float, lng: float) -> str:
    return (
        f"https://www.doordash.com/consumer/checkout/"
        f"?lat={lat}&lng={lng}&order_cart_id={cart_id}"
    )


def fetch_checkout(
    client: DoorDashWebSession,
    cart_id: str,
    *,
    lat: float,
    lng: float,
    should_apply_credits: bool = True,
) -> dict[str, Any]:
    referer = checkout_referer(cart_id, lat, lng)
    data = client.graphql(
        CHECKOUT_URL,
        "checkout",
        {
            "orderCartId": cart_id,
            "shouldApplyCredits": should_apply_credits,
        },
        load_query(CHECKOUT_QUERY),
        referer,
    )
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    cart = (data.get("data") or {}).get("orderCart")
    if not cart:
        raise RuntimeError("Checkout response missing orderCart.")
    return cart


def apply_promo_code(
    client: DoorDashWebSession,
    cart_id: str,
    promo_code: str,
    *,
    lat: float,
    lng: float,
) -> bool:
    """Apply a promo code via the unified-gateway REST endpoint."""
    referer = checkout_referer(cart_id, lat, lng)
    device_id = client.cookies.get("dd_device_id", "")
    session_id = client.cookies.get("dd_session_id", "")
    headers = {
        "accept": "*/*",
        "accept-language": "en-US",
        "content-type": "application/json",
        "origin": "https://www.doordash.com",
        "referer": referer,
        "user-agent": BROWSER_UA,
        "x-experience-id": "doordash",
        "x-unified-gateway-generated-source": "v1",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if device_id and session_id:
        import json as _json
        headers["dd-ids"] = _json.dumps({"dd_device_id": device_id, "dd_session_id": session_id})
    payload = {
        "cart_id": cart_id,
        "promotion_code": promo_code,
        "delivery_option_type": "NOT_SET",
    }
    try:
        resp = client.session.post(
            APPLY_PROMO_URL,
            headers=headers,
            cookies=client.cookies,
            json=payload,
            **request_kwargs(),
        )
        print(f"[promo] {resp.status_code}: {resp.text[:300]}")
        return resp.status_code < 400
    except Exception as exc:
        print(f"[promo] exception: {exc}")
        return False


def _line_item(cart: dict[str, Any], charge_id: str) -> dict[str, Any] | None:
    for entry in cart.get("lineItemsList") or []:
        if entry.get("chargeId") == charge_id:
            return entry
    return None


def _money_cents(line_item: dict[str, Any] | None) -> int:
    if not line_item:
        return 0
    money = line_item.get("finalMoney") or {}
    amount = money.get("unitAmount")
    return int(amount) if isinstance(amount, int) else 0


def _money_display(line_item: dict[str, Any] | None, fallback: str = "$0.00") -> str:
    if not line_item:
        return fallback
    money = line_item.get("finalMoney") or {}
    display = money.get("displayString")
    if isinstance(display, str) and display:
        return display.replace("US$", "$").replace("US $", "$")
    return fallback


@dataclass
class PriceBreakdown:
    subtotal_cents: int
    fees_tax_cents: int
    delivery_fee_display: str
    discounts_cents: int
    total_cents: int
    subtotal_display: str
    fees_tax_display: str
    discounts_display: str
    total_display: str

    @classmethod
    def from_cart(cls, cart: dict[str, Any]) -> PriceBreakdown:
        subtotal = int(cart.get("subtotal") or 0)
        total = int(cart.get("total") or 0)
        delivery_cents = _money_cents(_line_item(cart, "DELIVERY_FEE"))
        discounts = _money_cents(_line_item(cart, "PROMOTION_DISCOUNT"))
        fees_tax = max(total + discounts - subtotal - delivery_cents, 0)

        def fmt(cents: int) -> str:
            return f"${cents / 100:.2f}"

        discount_display = f"-{fmt(discounts)}" if discounts else fmt(0)

        return cls(
            subtotal_cents=subtotal,
            fees_tax_cents=fees_tax,
            delivery_fee_display=_money_display(_line_item(cart, "DELIVERY_FEE")),
            discounts_cents=discounts,
            total_cents=total,
            subtotal_display=fmt(subtotal),
            fees_tax_display=fmt(fees_tax),
            discounts_display=discount_display,
            total_display=fmt(total),
        )


def summarize_order_items(cart: dict[str, Any]) -> list[tuple[str, int]]:
    tallies: dict[str, int] = {}
    for order in cart.get("orders") or []:
        for order_item in order.get("orderItems") or []:
            name = (order_item.get("item") or {}).get("name") or "Item"
            qty = int(order_item.get("quantity") or 1)
            tallies[name] = tallies.get(name, 0) + qty
    return [(name, qty) for name, qty in tallies.items()]
