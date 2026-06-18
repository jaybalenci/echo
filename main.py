import asyncio
import io
import json
import os
import re
import traceback
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.logger import log
from core.map_image import generate_tracking_map, geocode
from core.price_order import run_price_order
from core.track_order import fetch_gift_tracking, fetch_drive_tracking, fetch_tracking, fetch_dasher_location
from core.user_settings import get_user_settings
from views.settings_views import (
    PriceCheckerSettingsModal,
    build_price_checker_settings_view,
    build_settings_view,
)
from views.tracking_views import build_tracking_view, tracking_gif_path
from views.order_views import (
    DD_GIF,
    PREPARING_GIF,
    build_breakdown_content,
    build_breakdown_view,
    build_error_view,
    build_loading_view,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN is not set in .env")

intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)

bot.tree.allowed_installs = app_commands.AppInstallationType(guild=True, user=True)
bot.tree.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)

_TERMINAL_STATUSES = {"delivered", "cancelled"}
_POLL_INTERVAL = 10            # seconds between updates
_POLL_MAX_CYCLES = 1440        # 4 hours max (1440 × 10s)
_POLL_MAX_FETCH_ERRORS = 20    # ~3 min of consecutive 403s before giving up
_POLL_MAX_EDIT_ERRORS = 5      # stop quickly if the message is gone/inaccessible

_active_polls: dict[str, asyncio.Task] = {}  # key → background task
_last_dasher_loc: dict[str, tuple[float, float]] = {}  # key → last known (lat, lng)

_POLLS_STATE_FILE = Path(__file__).resolve().parent / "config" / "active_polls.json"


def _read_poll_state() -> dict:
    try:
        return json.loads(_POLLS_STATE_FILE.read_text())
    except Exception:
        return {}


def _write_poll_state(state: dict) -> None:
    try:
        _POLLS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _POLLS_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log("persist", f"failed to write poll state: {exc}")


def _persist_poll(key: str, link_type: str, channel_id: int, message_id: int) -> None:
    state = _read_poll_state()
    state[key] = {"link_type": link_type, "channel_id": channel_id, "message_id": message_id}
    _write_poll_state(state)


def _unpersist_poll(key: str) -> None:
    state = _read_poll_state()
    if key in state:
        state.pop(key)
        _write_poll_state(state)


LOG_CHANNEL_ID = 1508897324255154216


async def _log_activity(
    user: discord.User | discord.Member,
    title: str,
    fields: list[tuple[str, str]],
    *,
    color: int = 0x5865F2,
) -> None:
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
        for name, value in fields:
            embed.add_field(name=name, value=value or "—", inline=False)
        await channel.send(embed=embed)
    except Exception as exc:
        log("log", f"failed to send activity log: {exc}")


def _city_state(address: str) -> str:
    """Extract 'City, ST' from a printable address string."""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        city = parts[1].strip()
        state = parts[2].strip().split()[0]
        return f"{city}, {state}" if city and state else city
    return ""


def _clean_store_name(name: str) -> str:
    """Remove parenthetical suffixes: 'Duran Market (Main St)' → 'Duran Market'"""
    return re.sub(r"\s*\([^)]*\)", "", name).strip()


async def _make_tracking_message(
    details: dict, link_type: str, key: str, *, fetch_loc: bool = True
) -> tuple[discord.ui.LayoutView, list[discord.File]]:
    """Build the view + file list for a tracking message, including map if available."""
    gif_path = tracking_gif_path(details.get("status_code") or "confirmed")
    files: list[discord.File] = [discord.File(gif_path, filename=gif_path.name)]
    map_png = None

    if link_type == "gift":
        store_lat = details.get("store_lat")
        store_lng = details.get("store_lng")
        delivery_lat = details.get("delivery_lat")
        delivery_lng = details.get("delivery_lng")

        status_code = details.get("status_code")
        if (
            store_lat is not None and store_lng is not None
            and delivery_lat is not None and delivery_lng is not None
            and status_code not in _TERMINAL_STATUSES
            and details.get("dasher_name")  # don't show map until dasher is assigned
        ):
            dasher_lat = dasher_lng = None
            if fetch_loc:
                loc = await asyncio.to_thread(fetch_dasher_location, key)
                if loc:
                    _last_dasher_loc[key] = loc
            cached = _last_dasher_loc.get(key)
            if cached:
                dasher_lat, dasher_lng = cached

            picked_up = status_code == "en_route"
            try:
                map_png = await asyncio.to_thread(
                    generate_tracking_map,
                    store_lat=store_lat, store_lng=store_lng,
                    delivery_lat=delivery_lat, delivery_lng=delivery_lng,
                    dasher_lat=dasher_lat, dasher_lng=dasher_lng,
                    picked_up=picked_up,
                )
            except Exception as exc:
                log("map", f"generation failed: {exc}")

    if map_png:
        files.append(discord.File(io.BytesIO(map_png), filename="tracking_map.png"))

    view = build_tracking_view(details, link_type, key, map_png=map_png)
    return view, files


async def _poll_tracking(
    link_type: str, key: str, message: discord.Message,
    *, followup: discord.Webhook | None = None,
) -> None:
    """Edit the tracking message every 10 seconds until delivered or cancelled."""
    fetch_errors = 0
    edit_errors = 0
    try:
        for cycle in range(_POLL_MAX_CYCLES):
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                if link_type == "gift":
                    details = await asyncio.to_thread(fetch_gift_tracking, key)
                else:
                    details = await asyncio.to_thread(fetch_drive_tracking, key)
                fetch_errors = 0
            except Exception as exc:
                fetch_errors += 1
                log("poll", f"fetch error ({fetch_errors}/{_POLL_MAX_FETCH_ERRORS}) for {key}: {exc}")
                if fetch_errors >= _POLL_MAX_FETCH_ERRORS:
                    break
                continue

            # Only call fetch_dasher_location every 3rd cycle (every 30s) to reduce API load
            fetch_loc = cycle % 3 == 0
            try:
                view, files = await _make_tracking_message(details, link_type, key, fetch_loc=fetch_loc)
            except Exception as exc:
                log("poll", f"render error for {key}: {exc}")
                continue  # skip this cycle, try again next

            try:
                await message.edit(view=view, attachments=files)
                edit_errors = 0
            except discord.NotFound:
                break  # message deleted
            except discord.Forbidden as exc:
                # 50001 = bot token has no channel access (user-install / private channel context).
                # Fall back to the interaction followup webhook for as long as its token is valid.
                if exc.code == 50001 and followup is not None:
                    try:
                        await followup.edit_message(message.id, view=view, attachments=files)
                        edit_errors = 0
                    except discord.NotFound:
                        break  # message deleted
                    except Exception as exc2:
                        edit_errors += 1
                        log("poll", f"edit error ({edit_errors}/{_POLL_MAX_EDIT_ERRORS}) for {key}: {exc2}")
                        if edit_errors >= _POLL_MAX_EDIT_ERRORS:
                            break
                else:
                    edit_errors += 1
                    log("poll", f"edit error ({edit_errors}/{_POLL_MAX_EDIT_ERRORS}) for {key}: {exc}")
                    if edit_errors >= _POLL_MAX_EDIT_ERRORS:
                        break
            except Exception as exc:
                edit_errors += 1
                log("poll", f"edit error ({edit_errors}/{_POLL_MAX_EDIT_ERRORS}) for {key}: {exc}")
                if edit_errors >= _POLL_MAX_EDIT_ERRORS:
                    break

            if details.get("status_code") in _TERMINAL_STATUSES:
                break
    finally:
        _active_polls.pop(key, None)
        _last_dasher_loc.pop(key, None)
        _unpersist_poll(key)


def _start_poll(
    link_type: str, key: str, message: discord.Message,
    *, followup: discord.Webhook | None = None,
) -> None:
    if key in _active_polls:
        _active_polls[key].cancel()
    _persist_poll(key, link_type, message.channel.id, message.id)
    task = asyncio.create_task(_poll_tracking(link_type, key, message, followup=followup))
    _active_polls[key] = task


async def setup_hook():
    synced = await bot.tree.sync()
    log("bot", f"synced {len(synced)} command(s): {[cmd.name for cmd in synced]}")


bot.setup_hook = setup_hook


async def _restore_polls() -> None:
    state = _read_poll_state()
    to_restore = [key for key in state if key not in _active_polls]
    if not to_restore:
        return
    log("restore", f"restoring {len(to_restore)} poll(s)...")
    for key in to_restore:
        info = state[key]
        try:
            # Use partial objects — no channel fetch needed, bot edits via its token
            channel = bot.get_partial_messageable(info["channel_id"])
            message = channel.get_partial_message(info["message_id"])
            _start_poll(info["link_type"], key, message)
            log("restore", f"resumed {info['link_type']} poll for {key}")
        except Exception as exc:
            log("restore", f"failed for {key}: {exc}")
            _unpersist_poll(key)


@bot.event
async def on_ready():
    log("bot", f"logged in as {bot.user} (ID: {bot.user.id})")
    await _restore_polls()


def _preparing_file() -> discord.File:
    return discord.File(PREPARING_GIF, filename="preparing.gif")


def _dd_file() -> discord.File:
    return discord.File(DD_GIF, filename="dd.gif")


@bot.tree.command(name="price", description="Get a price quote for an order")
@app_commands.describe(
    order_link="Link to the order",
    address="Delivery address",
)
async def price(interaction: discord.Interaction, order_link: str, address: str):
    # Defer immediately — Discord requires a response within 3 seconds.
    # Any work before this line risks a 10062 Unknown Interaction error.
    await interaction.response.defer()

    user_settings = get_user_settings(interaction.user.id)
    promo = user_settings.get("promotion", "Not Set")
    if not promo or promo == "Not Set":
        promo = "YOUGOT40"

    message = await interaction.followup.send(
        view=build_loading_view([]),
        files=[_preparing_file()],
        wait=True,
    )
    update_queue: asyncio.Queue[list[str] | str | dict | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def refresh_building_embed() -> None:
        added: list[str] = []
        status: str = ""
        info_store: str = ""
        info_address: str = ""

        def _apply(batch: list[str] | str | dict | None) -> bool:
            nonlocal added, status, info_store, info_address
            if batch is None:
                return True
            if isinstance(batch, dict):
                info_store = batch.get("store", info_store)
                info_address = batch.get("address", info_address)
            elif isinstance(batch, str):
                status = batch
            else:
                added = batch
                status = ""
            return False

        while True:
            if _apply(await update_queue.get()):
                return
            # Drain any updates that piled up while we were awaiting the Discord API
            while True:
                try:
                    if _apply(update_queue.get_nowait()):
                        return
                except asyncio.QueueEmpty:
                    break
            await message.edit(
                view=build_loading_view(added, status=status, store=info_store, address=info_address),
                attachments=[_preparing_file()],
            )

    refresh_task = asyncio.create_task(refresh_building_embed())

    def on_item_added(added_lines: list[str]) -> None:
        loop.call_soon_threadsafe(update_queue.put_nowait, list(added_lines))

    def on_status(msg: str) -> None:
        loop.call_soon_threadsafe(update_queue.put_nowait, msg)

    def on_info(key: str, value: str) -> None:
        loop.call_soon_threadsafe(update_queue.put_nowait, {key: value})

    try:
        result = await asyncio.to_thread(
            run_price_order,
            order_link,
            address,
            on_item_added=on_item_added,
            on_status=on_status,
            on_info=on_info,
            promo_code=promo,
        )
    except Exception as exc:
        await update_queue.put(None)
        await refresh_task
        err_text = str(exc) or exc.__class__.__name__
        log("error", traceback.format_exc().strip())
        await message.edit(
            view=build_error_view(err_text),
            attachments=[],
        )
        asyncio.create_task(_log_activity(
            interaction.user,
            "Price Check",
            [
                ("Order Link", order_link),
                ("Address", address),
                ("Promo", promo),
                ("Result", f"❌ {err_text[:300]}"),
            ],
            color=0xED4245,
        ))
        return

    await update_queue.put(None)
    await refresh_task

    user_settings = get_user_settings(interaction.user.id)
    service_fee = user_settings.get("service_fee", "$1.00")

    breakdown_text = build_breakdown_content(
        address=result.address,
        store=result.store,
        items=result.items,
        pricing=result.pricing,
        failures=result.failures or None,
        service_fee=service_fee,
    )
    await message.edit(
        view=build_breakdown_view(breakdown_text),
        attachments=[_dd_file()],
    )
    if result.cleanup_fn:
        result.cleanup_fn()
    asyncio.create_task(_log_activity(
        interaction.user,
        "Price Check",
        [
            ("Order Link", order_link),
            ("Address", address),
            ("Promo", promo),
            ("Store", result.store),
            ("Total", result.pricing.total_display),
        ],
        color=0x57F287,
    ))


@bot.tree.command(name="track", description="Track a DoorDash order")
@app_commands.describe(tracking_link="DoorDash gift or drive tracking link")
async def track(interaction: discord.Interaction, tracking_link: str):
    await interaction.response.defer()
    try:
        link_type, key, details = await asyncio.to_thread(fetch_tracking, tracking_link)
    except Exception as exc:
        err_text = str(exc) or exc.__class__.__name__
        log("error", traceback.format_exc().strip())
        await interaction.followup.send(view=build_error_view(err_text))
        asyncio.create_task(_log_activity(
            interaction.user,
            "Order Track",
            [
                ("Tracking Link", tracking_link),
                ("Result", f"❌ {err_text[:300]}"),
            ],
            color=0xED4245,
        ))
        return

    view, files = await _make_tracking_message(details, link_type, key)
    msg = await interaction.followup.send(view=view, files=files, wait=True)
    asyncio.create_task(_log_activity(
        interaction.user,
        "Order Track",
        [
            ("Tracking Link", tracking_link),
            ("Type", link_type),
            ("Status", details.get("status_code") or "unknown"),
        ],
        color=0x5865F2,
    ))

    if details.get("status_code") not in _TERMINAL_STATUSES:
        # PartialMessage.edit() uses the bot token (no expiry) — works in guild channels
        # and bot DMs. For user-install / private channel contexts the bot token gets
        # 50001, so we pass the followup webhook as a fallback (valid for ~15 min).
        channel = bot.get_partial_messageable(msg.channel.id)
        poll_msg = channel.get_partial_message(msg.id)
        _start_poll(link_type, key, poll_msg, followup=interaction.followup)


@bot.tree.command(name="settings", description="Configure your personal settings")
async def settings(interaction: discord.Interaction):
    if interaction.guild is not None:
        await interaction.response.send_message(
            "This command can only be used in DMs with the bot.",
            ephemeral=True,
        )
        return

    avatar_url = interaction.user.display_avatar.url
    await interaction.response.send_message(view=build_settings_view(avatar_url))


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")

    if custom_id == "settings_price_checker":
        user_settings = get_user_settings(interaction.user.id)
        fee = user_settings.get("service_fee", "$1.00")
        promo = user_settings.get("promotion", "Not Set")
        await interaction.response.edit_message(
            view=build_price_checker_settings_view(
                interaction.user.display_avatar.url, fee, promo
            )
        )

    elif custom_id == "pc_change_settings":
        user_settings = get_user_settings(interaction.user.id)
        modal = PriceCheckerSettingsModal(
            message=interaction.message,
            avatar_url=interaction.user.display_avatar.url,
            current_fee=user_settings.get("service_fee", "$1.00"),
            current_promo=user_settings.get("promotion", "Not Set"),
        )
        await interaction.response.send_modal(modal)

    elif custom_id == "pc_upload_accounts":
        await interaction.response.send_message(
            "Account upload coming soon!", ephemeral=True
        )


bot.run(TOKEN)
