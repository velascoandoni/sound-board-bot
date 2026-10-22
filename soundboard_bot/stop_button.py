from collections.abc import Awaitable, Callable

import discord


class StopButton(discord.ui.Button):
    def __init__(
        self,
        stop_sounds: Callable[[discord.Interaction], Awaitable[None]],
        emoji: str,
    ):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Stop",
            emoji=emoji,
            custom_id="sb:stop",
        )
        self._stop_sounds = stop_sounds

    async def callback(self, interaction: discord.Interaction):
        await self._stop_sounds(interaction)