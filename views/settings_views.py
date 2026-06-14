from __future__ import annotations

import discord

from core.user_settings import update_user_settings


def build_settings_view(avatar_url: str) -> discord.ui.LayoutView:
    class _SettingsView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "# Settings\n*Configure and customize your features.*\n\n"
                ),
                accessory=discord.ui.Thumbnail(avatar_url),
            ),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### Price Checker\n"
                    "> Upload your own accounts, adjust promotions, and manage service fees."
                ),
                accessory=discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    emoji=discord.PartialEmoji(
                        id=1515417805804142632, name="click", animated=True
                    ),
                    custom_id="settings_price_checker",
                ),
            ),
            discord.ui.Separator(visible=False),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### Keys\n"
                    "> View, create, and manage your API keys."
                ),
                accessory=discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    emoji=discord.PartialEmoji(
                        id=1515417805804142632, name="click", animated=True
                    ),
                    custom_id="settings_keys",
                    disabled=True,
                ),
            ),
        )

    return _SettingsView()


def build_price_checker_settings_view(
    avatar_url: str,
    service_fee: str = "$1.00",
    promotion: str = "Not Set",
) -> discord.ui.LayoutView:
    content = (
        "# Price Checker Settings\n\n"
        "Current Settings\n\n"
        f"> **Service Fee:** {service_fee}\n"
        f"> **Promotion:** {promotion}\n"
        "> -# Default: `YOUGOT40`\n\n"
        "> **Accounts:** Not Set\n"
        "> -# We provide accounts by default. Only set this when using a custom promo code."
    )

    class _View(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(content),
                accessory=discord.ui.Thumbnail(avatar_url),
            ),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
            discord.ui.ActionRow(
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="Change Settings",
                    custom_id="pc_change_settings",
                ),
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="Upload Accounts",
                    custom_id="pc_upload_accounts",
                ),
            ),
        )

    return _View()


class PriceCheckerSettingsModal(discord.ui.Modal, title="Price Checker Settings"):
    def __init__(
        self,
        *,
        message: discord.Message,
        avatar_url: str,
        current_fee: str,
        current_promo: str,
    ) -> None:
        super().__init__()
        self._message = message
        self._avatar_url = avatar_url
        self.fee_input = discord.ui.TextInput(
            label="Service Fee",
            placeholder="e.g. $1.00",
            default=current_fee,
            required=False,
        )
        self.promo_input = discord.ui.TextInput(
            label="Promotion",
            placeholder="e.g. YOUGOT40 — leave blank to clear",
            default="" if current_promo == "Not Set" else current_promo,
            required=False,
        )
        self.add_item(self.fee_input)
        self.add_item(self.promo_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        fee = self.fee_input.value.strip() or "$1.00"
        promo = self.promo_input.value.strip() or "Not Set"
        update_user_settings(interaction.user.id, service_fee=fee, promotion=promo)
        await interaction.response.defer()
        await self._message.edit(
            view=build_price_checker_settings_view(self._avatar_url, fee, promo)
        )
