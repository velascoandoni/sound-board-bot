from collections.abc import Awaitable, Callable

import discord


class SoundButton(discord.ui.Button):
    def __init__(
        self,
        key: str,
        label: str,
        play_sound: Callable[[discord.Interaction, str], Awaitable[None]],
    ):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label[:80],
            emoji=key,
            custom_id=f"sb:play:{key}",
        )
        self.key = key
        self._play_sound = play_sound

    async def callback(self, interaction: discord.Interaction):
        await self._play_sound(interaction, self.key)