import asyncio
from datetime import datetime, timezone
from typing import Optional

import discord

from .sound_row import SoundRow


class ActiveSoundsList:
    """Manages active sound rows and a persistent Discord embed."""

    def __init__(
        self,
        title: str = "🔊 Sound Activity",
        color: int = 0x5865F2,
    ):
        self.title = title
        self.color = color
        self.sounds: dict[str, SoundRow] = {}
        self.message: Optional[discord.Message] = None
        self._refresh_task: Optional[asyncio.Task] = None

    async def _refresh_remaining(self):
        loop = asyncio.get_running_loop()
        interval = 1.0
        next_tick = loop.time() + interval

        try:
            while self.sounds:
                await asyncio.sleep(max(0, next_tick - loop.time()))

                if self.sounds:
                    await self.update()

                next_tick += interval
                now = loop.time()
                if next_tick <= now:
                    next_tick = now + interval
        finally:
            self._refresh_task = None

    def add_sound(
        self,
        sound_id: str,
        name: str,
        user_id: int,
        duration_seconds: float,
        emoji: str = "🔊",
    ) -> SoundRow:
        if sound_id in self.sounds:
            raise ValueError(f"Playback {sound_id!r} already exists.")

        row = SoundRow(
            id=sound_id,
            name=name,
            user_id=user_id,
            emoji=emoji,
            duration_seconds=duration_seconds,
        )
        self.sounds[sound_id] = row
        return row

    def remove_sound(self, sound_id: str) -> bool:
        return self.sounds.pop(sound_id, None) is not None

    def get_sound(self, sound_id: str) -> Optional[SoundRow]:
        return self.sounds.get(sound_id)

    def create_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            color=self.color,
            timestamp=datetime.now(timezone.utc),
        )

        if self.sounds:
            description = "\n\n".join(
                row.to_string() for row in self.sounds.values()
            )
            suffix = "\n\n… More sounds omitted."
            if len(description) > 4096:
                description = description[: 4096 - len(suffix)] + suffix
            embed.description = description
        else:
            embed.description = "_No sounds are currently playing._"

        return embed

    async def send(self, channel: discord.abc.Messageable):
        self.message = await channel.send(embed=self.create_embed())
        return self.message

    async def update(self):
        if self.message is None:
            raise RuntimeError(
                "Dashboard not sent yet. Call send(channel) first."
            )

        await self.message.edit(embed=self.create_embed())
        return self.message

    async def add_and_update(
        self,
        sound,
        duration_seconds: float,
        user_id: int,
    ) -> str:
        sound_id = str(datetime.now().timestamp()) + sound[0]
        self.add_sound(
            sound_id, sound[0], user_id, duration_seconds, sound[2]
        )
        await self.update()

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_remaining())
        return sound_id

    async def remove_and_update(self, sound_id: str) -> bool:
        removed = self.remove_sound(sound_id)
        if removed:
            await self.update()
        return removed

    async def remove_all_and_update(self) -> bool:
        self.sounds.clear()
        await self.update()