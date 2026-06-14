from __future__ import annotations

from pathlib import Path
from typing import Any

import discord

_ASSETS = Path(__file__).resolve().parent.parent / "assets"

_LOCATION = "<a:location:1515466533365878945>"

_STATUS_GIF: dict[str, str] = {
    "confirmed": "confirmed.gif",
    "preparing": "preparing.gif",
    "en_route": "en_route.gif",
    "delivered": "delivered.gif",
    "ready_for_pickup": "ready_for_pickup.gif",
    "cancelled": "dd.gif",
}


def tracking_gif_path(status_code: str) -> Path:
    return _ASSETS / _STATUS_GIF.get(status_code, "dd.gif")


def tracking_gif_filename(status_code: str) -> str:
    return _STATUS_GIF.get(status_code, "dd.gif")


def _build_main_text(details: dict[str, Any]) -> str:
    store = details.get("store") or "DoorDash Order"
    address = details.get("address") or "—"
    items: list[str] = details.get("items") or []
    dasher_name = details.get("dasher_name")
    dasher_phone = details.get("dasher_phone")
    recipient_name = details.get("recipient_name")

    lines = [
        f"# Tracking {_LOCATION}",
        "",
        "**Order Information:**",
        f"* Restaurant: **{store}**",
        f"* **Delivery Address:** `{address}`",
    ]
    if recipient_name:
        lines.append(f"* Customer: **{recipient_name}**")

    if dasher_name:
        lines += ["", "**Dasher Information**", f"Name: {dasher_name}"]
        if dasher_phone:
            lines.append(f"Number: {dasher_phone}")

    if items:
        items_block = "\n".join(f"• {item}" for item in items)
        lines += ["", "### Order Summary", f"```\n{items_block}\n```"]

    return "\n".join(lines)


def _build_status_text(details: dict[str, Any]) -> str:
    status = details.get("status") or "Tracking"
    status_message = details.get("status_message")
    eta = details.get("eta_window") or "—"

    lines = [f"### Status: {status}"]
    if status_message:
        lines.append(f"-# {status_message}")
    if eta and eta != "—":
        lines.append(f"-#  **ETA**: {eta}")
    return "\n".join(lines)


def _tracking_url(link_type: str, key: str) -> str:
    if link_type == "gift":
        return f"https://www.doordash.com/gifts/{key}"
    return f"https://www.doordash.com/orders/drive?urlCode={key}"


def build_tracking_view(
    details: dict[str, Any],
    link_type: str,
    key: str,
    map_png: bytes | None = None,
) -> discord.ui.LayoutView:
    main_text = _build_main_text(details)
    status_text = _build_status_text(details)
    gif_name = tracking_gif_filename(details.get("status_code") or "confirmed")
    url = _tracking_url(link_type, key)

    action_row = discord.ui.ActionRow(
        discord.ui.Button(
            style=discord.ButtonStyle.link,
            label="Track",
            emoji=discord.PartialEmoji(
                id=1515466533365878945, name="location", animated=True
            ),
            url=url,
        ),
    )

    if map_png:
        class _TrackingView(discord.ui.LayoutView):
            container = discord.ui.Container(
                discord.ui.Section(
                    discord.ui.TextDisplay(main_text),
                    accessory=discord.ui.Thumbnail(f"attachment://{gif_name}"),
                ),
                discord.ui.Separator(visible=False),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        media=discord.UnfurledMediaItem(url="attachment://tracking_map.png")
                    )
                ),
                discord.ui.TextDisplay(status_text),
                action_row,
            )
    else:
        full_text = main_text + "\n\n" + status_text

        class _TrackingView(discord.ui.LayoutView):
            container = discord.ui.Container(
                discord.ui.Section(
                    discord.ui.TextDisplay(full_text),
                    accessory=discord.ui.Thumbnail(f"attachment://{gif_name}"),
                ),
                action_row,
            )

    return _TrackingView()
