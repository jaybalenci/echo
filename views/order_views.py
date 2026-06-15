from __future__ import annotations

import re
from pathlib import Path

import discord

PREPARING_GIF = Path(__file__).resolve().parent.parent / "assets" / "preparing.gif"
DD_GIF = Path(__file__).resolve().parent.parent / "assets" / "dd.gif"

_CART   = "<:shoppingcart:1515255980651450488>"
_FRIES  = "<a:fries:1515255709502148618>"
_MONEY  = "<a:money:1515256070518472787>"
_CARD   = "<a:card:1515257513010790430>"
_WARN   = "<a:warning:1515257724005519420>"


def _loading_content(
    added_items: list[str],
    status: str = "",
    store: str = "",
    address: str = "",
) -> str:
    info_lines = []
    if store:
        info_lines.append(f"* Restaurant: **{store}**")
    if address:
        info_lines.append(f"* Address: **{address}**")
    info_block = "\n".join(info_lines) + "\n\n" if info_lines else ""

    items_block = ""
    if added_items:
        counts: dict[str, int] = {}
        for name in added_items:
            counts[name] = counts.get(name, 0) + 1
        lines = "\n".join(
            f"+ {name} ×{count}" if count > 1 else f"+ {name}"
            for name, count in counts.items()
        )
        items_block = f"### {_CART} Current Items\n```md\n{lines}\n```\n\n"

    status_line = f"Status: {status}" if status else ""

    return (
        "# Loading Your Order\n\n"
        f"{info_block}"
        f"{items_block}"
        f"{status_line}"
    ).rstrip()


def build_loading_view(
    added_items: list[str],
    status: str = "",
    store: str = "",
    address: str = "",
) -> discord.ui.LayoutView:
    content = _loading_content(added_items, status, store=store, address=address)

    class _LoadingOrderView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(content),
                accessory=discord.ui.Thumbnail("attachment://preparing.gif"),
            ),
        )

    return _LoadingOrderView()


def _parse_dollars(s: str) -> float:
    m = re.search(r"[\d.]+", s or "")
    try:
        return float(m.group()) if m else 0.0
    except ValueError:
        return 0.0


def build_breakdown_content(
    *,
    address: str,
    store: str,
    items: list[tuple[str, int]],
    pricing: "PriceBreakdownFields",
    failures: list[str] | None = None,
    service_fee: str = "$0.00",
) -> str:
    item_lines = []
    for name, qty in items:
        if qty > 1:
            item_lines.append(f"{name} ×{qty}")
        else:
            item_lines.append(name)
    items_block = "\n".join(item_lines) if item_lines else "No items"

    fee_dollars = _parse_dollars(service_fee)
    total_dollars = _parse_dollars(pricing.total_display)
    discount_dollars = _parse_dollars(pricing.discounts_display)

    adjusted = total_dollars + fee_dollars
    undiscounted = adjusted + discount_dollars

    delivery_line = f"Delivery Fee: `{pricing.delivery_fee_display}`"
    fee_line = f"Service Fee   `{service_fee}`\n" if fee_dollars > 0 else ""

    if discount_dollars > 0:
        total_line = f"{_CARD} Total: **${adjusted:.2f}** *~~${undiscounted:.2f}~~*"
    else:
        total_line = f"{_CARD} **Total: `${adjusted:.2f}`**"

    text = (
        f"# {_CART} Order Breakdown\n"
        f"**Store:** {store}\n"
        f"**Address:** *{address}*\n\n"
        f"{_FRIES} **Items:**\n"
        f"```\n{items_block}\n```\n"
        f"## {_MONEY} Price Breakdown\n"
        f"Subtotal      `{pricing.subtotal_display}`\n"
        f"Fees & Tax    `{pricing.fees_tax_display}`\n"
        f"{delivery_line}\n"
        f"Discounts     `{pricing.discounts_display}`\n"
        f"{fee_line}\n"
        f"{total_line}"
    )
    if failures:
        warn = "\n".join(f"- {line}" for line in failures)
        text += f"\n\n{_WARN} Some items could not be added:\n{warn}"
    return text


class PriceBreakdownFields:
    __slots__ = (
        "subtotal_display",
        "fees_tax_display",
        "delivery_fee_display",
        "discounts_display",
        "total_display",
    )

    def __init__(
        self,
        *,
        subtotal_display: str,
        fees_tax_display: str,
        delivery_fee_display: str,
        discounts_display: str,
        total_display: str,
    ) -> None:
        self.subtotal_display = subtotal_display
        self.fees_tax_display = fees_tax_display
        self.delivery_fee_display = delivery_fee_display
        self.discounts_display = discounts_display
        self.total_display = total_display


def build_breakdown_view(content: str) -> discord.ui.LayoutView:
    class _OrderBreakdownView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(content),
                accessory=discord.ui.Thumbnail("attachment://dd.gif"),
            ),
        )

    return _OrderBreakdownView()


def build_error_view(message: str) -> discord.ui.LayoutView:
    content = f"# Order Failed\n\n{message}"

    class _ErrorView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content),
        )

    return _ErrorView()
