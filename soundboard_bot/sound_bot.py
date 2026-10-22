from collections.abc import Callable

import discord
from discord import app_commands

from .active_sounds_list import ActiveSoundsList


class SoundBot(discord.Client):
    def __init__(
        self,
        view_factory: Callable[[], discord.ui.View],
        state_file: str,
        load_state: Callable,
        active_sounds_lists: dict,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.view_factory = view_factory
        self.state_file = state_file
        self.load_state = load_state
        self.active_sounds_lists = active_sounds_lists

    async def setup_hook(self):
        await self.tree.sync()
        for guild_id, entry in self.load_state(self.state_file, {}).items():
            buttons_message = entry.get("buttons-message")
            if buttons_message is None and "message_id" in entry:
                buttons_message = entry
            if buttons_message:
                self.add_view(
                    self.view_factory(),
                    message_id=buttons_message["message_id"],
                )

            activity_message = entry.get("activity-message")
            if activity_message:
                try:
                    channel = self.get_channel(activity_message["channel_id"])
                    if channel is None:
                        channel = await self.fetch_channel(
                            activity_message["channel_id"]
                        )
                    activity_list = ActiveSoundsList()
                    activity_list.message = channel.get_partial_message(
                        activity_message["message_id"]
                    )
                    self.active_sounds_lists[int(guild_id)] = activity_list
                except discord.HTTPException as err:
                    print(f"Could not restore activity message: {err}")