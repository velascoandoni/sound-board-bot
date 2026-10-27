import os
import re
import json
import array
import queue
import shutil
import asyncio
import threading
import subprocess
 
 
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
 
 
import discord
from discord import app_commands
from dotenv import load_dotenv
 
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()
 
# Second bot token, used ONLY for the sound-activity embed so its edits count
# against that bot's rate limits instead of the main bot's.
# If it is missing, the activity feature is disabled entirely (no updates at all).
ACTIVITY_TOKEN = os.getenv("ACTIVITY_BOT_TOKEN", "").strip()
ACTIVITY_ENABLED = bool(ACTIVITY_TOKEN)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
 
DEFAULTS_DIR = os.path.join(BASE_DIR, "defaults")
DATA_DIR = os.path.join(BASE_DIR, os.getenv("DATA_DIR", "data"))
 
SOUNDS_DIR = os.path.join(DATA_DIR, "sounds")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
SOUNDS_FILE = os.path.join(DATA_DIR, "sounds.json")
 
ADD_CHANNEL = "add-sounds"
MAX_SOUNDS = 24  # a message holds 25 buttons max; one slot is the Stop button
MAX_BYTES = 8 * 1024 * 1024
EXTENSIONS = (".mp3", ".wav", ".ogg")
 
STOP_EMOJI = "✖️"
VOICE_IDLE_TIMEOUT = 5
 
# First run: seed data/ from defaults/ (only if the sound list doesn't exist yet)
if not os.path.exists(SOUNDS_FILE):
    shutil.copytree(DEFAULTS_DIR, DATA_DIR, dirs_exist_ok=True)
os.makedirs(SOUNDS_DIR, exist_ok=True)
 
 
def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default
 
 
def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
 
 
def norm(e: str) -> str:
    return e.replace("\ufe0f", "")
 
 
SOUNDS = load_json(SOUNDS_FILE, {})  # emoji -> [label, filename]
 
 
def find_sound(emoji: str):
    for e, v in SOUNDS.items():
        if norm(e) == norm(emoji):
            return [*v, e]
    return None
 
 
def board_text():
    return (
        "**🔊 Soundboard**\n"
        "Join a voice channel, then press a button to play a sound.\n"
        f"Press {STOP_EMOJI} **Stop** to silence everything."
    )
 
 
# ---------------------------------------------------------------------------
# Audio mixer (lets several sounds play at the same time)
# ---------------------------------------------------------------------------
class SoundMixer(discord.AudioSource):
    FRAME_BYTES = 3840  # 20 ms of 48 kHz stereo, 16-bit PCM
 
    def __init__(self):
        self.lock = threading.Lock()
        self.sources = []
        self.closed = False
 
    def add_sound(self, path, on_finished=None):
        if self.closed:
            raise RuntimeError("Mixer is closed")
 
        process = subprocess.Popen(
            [
                FFMPEG_PATH or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel", "error",
                "-i", str(path),
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
 
        source = {
            "process": process,
            "frames": queue.Queue(maxsize=10),
            "on_finished": on_finished,
        }
 
        with self.lock:
            if self.closed:
                process.kill()
                process.wait()
                raise RuntimeError("Mixer is closed")
            self.sources.append(source)
 
        def read_audio():
            frames = source["frames"]
            try:
                while not self.closed:
                    chunk = process.stdout.read(self.FRAME_BYTES)
                    if not chunk:
                        break
 
                    chunk = chunk.ljust(self.FRAME_BYTES, b"\0")
 
                    while not self.closed:
                        try:
                            frames.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            except (OSError, ValueError):
                pass
            finally:
                while not self.closed:
                    try:
                        frames.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        threading.Thread(target=read_audio, daemon=True).start()
 
    def read(self):
        if self.closed:
            return b""
 
        with self.lock:
            sources = list(self.sources)
 
        mixed = array.array("h", [0] * (self.FRAME_BYTES // 2))
        finished = []
 
        for source in sources:
            try:
                frame = source["frames"].get_nowait()
            except queue.Empty:
                continue
 
            if frame is None:
                finished.append(source)
                continue
 
            samples = array.array("h")
            samples.frombytes(frame)
 
            for i, sample in enumerate(samples):
                mixed[i] = max(-32768, min(32767, mixed[i] + sample))
 
        with self.lock:
            for source in finished:
                if source in self.sources:
                    self.sources.remove(source)
                    process = source["process"]
 
                    try:
                        process.stdout.close()
                    except (OSError, ValueError):
                        pass
 
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
 
                    callback = source.get("on_finished")
                    if callback:
                        callback()
 
        return mixed.tobytes()
 
    def is_opus(self):
        return False
 
    def stop_all(self):
        with self.lock:
            sources = list(self.sources)
            self.sources.clear()
 
        for source in sources:
            try:
                source["process"].kill()
            except OSError:
                pass
 
    def cleanup(self):
        with self.lock:
            self.closed = True
            sources = list(self.sources)
            self.sources.clear()
 
        for source in sources:
            try:
                source["process"].kill()
            except OSError:
                pass
 
 
# ---------------------------------------------------------------------------
# Playing Sounds message
# ---------------------------------------------------------------------------
 
async def get_audio_duration(path: str) -> float:
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())
 
 
def disabled_embed() -> discord.Embed:
    """Static notice shown instead of the live activity list."""
    return discord.Embed(
        title="🔊 Sound Activity (disabled)",
        description=(
            "Live sound activity is turned off because no "
            "`ACTIVITY_BOT_TOKEN` is configured."
        ),
        color=0x747F8D,
    )
 
 
@dataclass
class SoundRow:
    """Represents one instance of a sound playback."""
 
    id: str
    name: str
    user_id: int
    duration_seconds: float
    emoji: str = "🔊"
 
    def to_string(self) -> str:
        return (
            f"{self.emoji} **{self.name}**\n"
            f"└  Played by <@{self.user_id}>"
        )
 
 
class ActiveSoundsList:
    """Manages active sound rows and a persistent Discord embed."""
 
    def __init__(
        self,
        title: str = "🔊 Sound Activity",
        color: int = 0x5865F2,
    ):
        self.title = title
        self.color = color

        # Unique playback ID -> SoundRow
        self.sounds: dict[str, SoundRow] = {}
 
        # Message bound to the ACTIVITY bot's HTTP state, so edits use its token.
        self.message = None
 
    def add_sound(
        self,
        sound_id: str,
        name: str,
        user_id: int,
        duration_seconds: float,
        emoji: str = "🔊",
    ) -> SoundRow:
        """Add a sound to the active list."""
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
        """Remove a playback. Returns True if it existed."""
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
 
            # Embed descriptions have a 4096-character limit.
            suffix = "\n\n… More sounds omitted."
            if len(description) > 4096:
                description = description[: 4096 - len(suffix)] + suffix
 
            embed.description = description
        else:
            embed.description = "_No sounds are currently playing._"
 
        return embed
 
    async def send(self, channel: discord.abc.Messageable):
        """Create the dashboard message."""
        self.message = await channel.send(embed=self.create_embed())
        return self.message
 
    async def update(self):
        """Refresh the existing dashboard message."""
        if not ACTIVITY_ENABLED:
            return None  # never touch the API when disabled
        if self.message is None:
            raise RuntimeError(
                "Dashboard not sent yet. Call send(channel) first."
            )
 
        await self.message.edit(embed=self.create_embed())
        return self.message
 
    async def _safe_update(self):
        """Update, but never let a failed edit break sound playback."""
        try:
            await self.update()
        except discord.HTTPException as err:
            print(f"Could not update activity message: {err}")
 
    async def add_and_update(
        self,
        sound,
        duration_seconds: float,
        user_id: int,
    ) -> str:
        sound_id = str(datetime.now().timestamp()) + sound[0]
        self.add_sound(sound_id, sound[0], user_id, duration_seconds, sound[2])
        await self._safe_update()
        return sound_id
 
    async def remove_and_update(self, sound_id: str) -> bool:
        removed = self.remove_sound(sound_id)
 
        if removed:
            await self._safe_update()
 
        return removed
 
    async def remove_all_and_update(self) -> bool:
        had_sounds = bool(self.sounds)
        self.sounds.clear()
        if had_sounds:
            await self._safe_update()
        return had_sounds
 
 
# ---------------------------------------------------------------------------
# Bot + buttons
# ---------------------------------------------------------------------------
mixers = {}
voice_locks = {}
voice_idle_tasks = {}
active_sounds_lists = {}
 
# REST-only client for the activity embed. It never opens a gateway
# connection; login() just sets up the HTTP session with its own token,
# so its requests count against its own rate limits.
activity_bot = discord.Client(intents=discord.Intents.none())
 
 
def activity_messageable(channel_id: int):
    """A channel handle bound to the activity bot's token."""
    return activity_bot.get_partial_messageable(channel_id)
 
 
async def get_mixer(guild, vc):
    mixer = mixers.get(guild.id)
 
    if mixer is None or mixer.closed:
        mixer = SoundMixer()
        mixers[guild.id] = mixer
        vc.play(mixer)
 
    return mixer
 
 
async def play_sound(interaction: discord.Interaction, key: str):
 
    sound = find_sound(key)
    if sound is None:
        return await interaction.response.send_message(
            "That sound no longer exists.", ephemeral=True)
 
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        return await interaction.response.send_message(
            "Join a voice channel first.", ephemeral=True)
 
    path = os.path.join(SOUNDS_DIR, sound[1])
    if not os.path.isfile(path):
        print(f"Missing file: {path}")
        return await interaction.response.send_message(
            "The file for that sound is missing.", ephemeral=True)
 
    # Acknowledge right away: connecting to voice can take longer than 3 seconds
    await interaction.response.defer()
 
    guild = interaction.guild
 
    # With the activity bot disabled there is no list; sounds still play.
    activity_list = active_sounds_lists.get(guild.id) if ACTIVITY_ENABLED else None
    if ACTIVITY_ENABLED and activity_list is None:
        return await interaction.followup.send(
            "Run /setup before playing sounds.", ephemeral=True
        )
 
    sound_id = None
    try:
        async with voice_locks.setdefault(guild.id, asyncio.Lock()):
            vc = guild.voice_client
            if vc is None:
                vc = await member.voice.channel.connect()
            elif vc.channel != member.voice.channel:
                await vc.move_to(member.voice.channel)
            mixer = await get_mixer(guild, vc)
 
        on_finished_cb = None
        if activity_list is not None:
            duration_seconds = await get_audio_duration(path)
            sound_id = await activity_list.add_and_update(
                sound, duration_seconds, member.id)
            loop = asyncio.get_running_loop()
            finished_id = sound_id
 
            def on_finished_cb():
                asyncio.run_coroutine_threadsafe(
                    activity_list.remove_and_update(finished_id), loop)
 
        await asyncio.to_thread(mixer.add_sound, path, on_finished=on_finished_cb)
 
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError,
            asyncio.TimeoutError, discord.ClientException) as err:
        print(f"Could not play sound: {err}")
        if activity_list is not None and sound_id is not None:
            await activity_list.remove_and_update(sound_id)
        await interaction.followup.send("Could not play that sound.", ephemeral=True)
 
 
class SoundButton(discord.ui.Button):
    def __init__(self, key: str, label: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label[:80],
            emoji=key,
            custom_id=f"sb:play:{key}",
        )
        self.key = key
 
    async def callback(self, interaction: discord.Interaction):
        await play_sound(interaction, self.key)
 
 
class StopButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Stop",
            emoji=STOP_EMOJI,
            custom_id="sb:stop",
        )
 
    async def callback(self, interaction: discord.Interaction):
        mixer = mixers.get(interaction.guild.id)
        if mixer:
            mixer.stop_all()
        activity_list = active_sounds_lists.get(interaction.guild.id)
        if activity_list:
            await activity_list.remove_all_and_update()
        await interaction.response.defer()  # silent acknowledge
 
 
def build_view():
    view = discord.ui.View(timeout=None)  # timeout=None -> buttons never expire
    view.add_item(StopButton())
    for key, sound_data in SOUNDS.items():
        view.add_item(SoundButton(key, sound_data[0]))
    return view
 
 
def get_buttons_message(entry):
    """Read the buttons-message record, supporting the old flat state format."""
    if not entry:
        return None
    buttons_message = entry.get("buttons-message")
    if buttons_message is None and "message_id" in entry:
        buttons_message = entry
    return buttons_message
 
 
class SoundBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read uploads
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
 
    async def setup_hook(self):
        if ACTIVITY_ENABLED:
            await activity_bot.login(ACTIVITY_TOKEN)
        else:
            print("ACTIVITY_BOT_TOKEN not set: sound activity is disabled.")
 
        await self.tree.sync()
        await self.sync_activity_messages()
        for guild_id, entry in load_json(STATE_FILE, {}).items():
            buttons_message = get_buttons_message(entry)
            if buttons_message:
                self.add_view(
                    build_view(),
                    message_id=buttons_message["message_id"],
                )
 
            # Restore the activity list only when the feature is enabled.
            # Partial objects only: no API calls on startup.
            activity_message = entry.get("activity-message")
            if ACTIVITY_ENABLED and activity_message:
                activity_list = ActiveSoundsList()
                activity_list.message = activity_messageable(
                    activity_message["channel_id"]
                ).get_partial_message(activity_message["message_id"])
                active_sounds_lists[int(guild_id)] = activity_list
 
    async def sync_activity_messages(self):
        """Make saved activity messages match the current token state.
 
        No token: edit each activity message ONCE to the disabled notice.
        Token added later: replace the disabled notice with a live message.
        Neither case needs /setup to be run again.
        """
        state = load_json(STATE_FILE, {})
        changed = False
 
        for guild_id, entry in state.items():
            am = entry.get("activity-message")
            if not am:
                continue
            try:
                if not ACTIVITY_ENABLED and not am.get("disabled"):
                    await (self.get_partial_messageable(am["channel_id"])
                           .get_partial_message(am["message_id"])
                           .edit(embed=disabled_embed()))
                    am["disabled"] = True
                    changed = True
                elif ACTIVITY_ENABLED and am.get("disabled"):
                    activity_list = ActiveSoundsList()
                    post = await activity_list.send(
                        activity_messageable(am["channel_id"]))
                    entry["activity-message"] = {
                        "channel_id": am["channel_id"],
                        "message_id": post.id,
                    }
                    changed = True
            except discord.HTTPException as err:
                print(f"Could not sync activity message for {guild_id}: {err}")
 
        if changed:
            save_json(STATE_FILE, state)
 
    async def close(self):
        if ACTIVITY_ENABLED:
            await activity_bot.close()
        await super().close()
 
 
bot = SoundBot()
 
 
async def update_board(entry):
    """Edit one soundboard message so its buttons match SOUNDS. Raises HTTPException on failure."""
    ch = bot.get_channel(entry["channel_id"])
    if ch is None:
        return
    await ch.get_partial_message(entry["message_id"]).edit(
        content=board_text(), view=build_view())
 
 
async def refresh_boards(skip_message_id=None):
    """Update the soundboard message in every server."""
    for entry in load_json(STATE_FILE, {}).values():
        buttons_message = get_buttons_message(entry)
        if not buttons_message or buttons_message["message_id"] == skip_message_id:
            continue
        try:
            await update_board(buttons_message)
        except discord.HTTPException as err:
            print(f"Could not refresh board: {err}")
 
 
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
 
 
@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return
 
    guild = member.guild
    vc = guild.voice_client
    if vc is None or (before.channel != vc.channel and after.channel != vc.channel):
        return
 
    idle_task = voice_idle_tasks.get(guild.id)
    if any(not channel_member.bot for channel_member in vc.channel.members):
        if idle_task is not None:
            idle_task.cancel()
            voice_idle_tasks.pop(guild.id, None)
        return
 
    if idle_task is not None and not idle_task.done():
        return
 
    async def disconnect_when_idle(channel):
        try:
            await asyncio.sleep(VOICE_IDLE_TIMEOUT)
            async with voice_locks.setdefault(guild.id, asyncio.Lock()):
                current_vc = guild.voice_client
                if (
                    current_vc is not None
                    and current_vc.channel == channel
                    and not any(
                        not channel_member.bot
                        for channel_member in current_vc.channel.members
                    )
                ):
                    mixer = mixers.pop(guild.id, None)
                    if mixer:
                        mixer.cleanup()
                    await current_vc.disconnect()
        except asyncio.CancelledError:
            raise
        finally:
            if voice_idle_tasks.get(guild.id) is asyncio.current_task():
                voice_idle_tasks.pop(guild.id, None)
 
    voice_idle_tasks[guild.id] = asyncio.create_task(
        disconnect_when_idle(vc.channel)
    )
 
 
async def show_disabled_notice(old_entry, channel):
    """Post (or reuse) a static 'activity disabled' message. Sent once by the
    main bot; it is never edited again while disabled."""
    embed = disabled_embed()
    if old_entry:
        if old_entry.get("disabled"):
            return old_entry  # already showing the notice: no API call
        old_channel = bot.get_channel(old_entry["channel_id"])
        if old_channel is not None:
            try:
                await old_channel.get_partial_message(
                    old_entry["message_id"]).edit(embed=embed)
                return {**old_entry, "disabled": True}
            except discord.HTTPException:
                pass  # old message gone or not ours; post a new one
    msg = await channel.send(embed=embed)
    return {"channel_id": channel.id, "message_id": msg.id, "disabled": True}
 
 
@bot.tree.command(name="setup", description="Create the soundboard and add-sounds channels")
@app_commands.default_permissions(manage_channels=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    state = load_json(STATE_FILE, {})
 
    add_ch = discord.utils.get(guild.text_channels, name=ADD_CHANNEL)
    if not add_ch:
        add_ch = await guild.create_text_channel(
            ADD_CHANNEL,
            topic="Upload an mp3/wav/ogg. Write an emoji (and optional name) in the message.",
        )
 
    old = state.get(str(guild.id), {})
    buttons_message = get_buttons_message(old)
 
    channel = (
        guild.get_channel(buttons_message["channel_id"])
        if buttons_message
        else None
    )
    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True),
        }
        if ACTIVITY_ENABLED and activity_bot.user:
            try:
                helper = (guild.get_member(activity_bot.user.id)
                          or await guild.fetch_member(activity_bot.user.id))
                overwrites[helper] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True,
                    embed_links=True, read_message_history=True)
            except discord.HTTPException:
                print("Activity bot is not in this server; invite it first.")
        channel = await guild.create_text_channel(
            "soundboard", overwrites=overwrites,
            topic="Join a voice channel, then press a button to play a sound.")
 
    if buttons_message and buttons_message["channel_id"] == channel.id:
        try:
            await update_board(buttons_message)
            old_buttons = await channel.fetch_message(buttons_message["message_id"])
            await old_buttons.clear_reactions()
        except discord.HTTPException as err:
            print(f"Could not update existing board: {err}")
            board = await channel.send(board_text(), view=build_view())
            buttons_message = {
                "channel_id": channel.id,
                "message_id": board.id,
            }
    else:
        board = await channel.send(board_text(), view=build_view())
        buttons_message = {
            "channel_id": channel.id,
            "message_id": board.id,
        }
 
    # ---- Activity message ------------------------------------------------
    activity_message = old.get("activity-message")
 
    if not ACTIVITY_ENABLED:
        # Feature off: static notice only, no live list, no further edits.
        active_sounds_lists.pop(guild.id, None)
        activity_message = await show_disabled_notice(activity_message, channel)
    else:
        activity_list = active_sounds_lists.get(guild.id) or ActiveSoundsList()
 
        restored = False
        if activity_message:
            try:
                msg = await activity_messageable(
                    activity_message["channel_id"]
                ).fetch_message(activity_message["message_id"])
                # Only the activity bot can edit its own messages. If the message
                # was posted by someone else (e.g. the main bot, or the disabled
                # notice), post a fresh one instead.
                if msg.author.id == activity_bot.user.id:
                    activity_list.message = msg
                    restored = True
            except discord.HTTPException:
                pass
 
        if not restored:
            try:
                activity_post = await activity_list.send(
                    activity_messageable(channel.id))
            except discord.Forbidden:
                return await interaction.followup.send(
                    "The activity bot can't post in the soundboard channel. "
                    "Invite it to this server and give it access to the channel.")
            activity_message = {
                "channel_id": channel.id,
                "message_id": activity_post.id,
            }
        active_sounds_lists[guild.id] = activity_list
 
    state[str(guild.id)] = {
        "buttons-message": buttons_message,
        "activity-message": activity_message,
    }
    save_json(STATE_FILE, state)
 
    note = "" if ACTIVITY_ENABLED else "\nSound activity is disabled (no `ACTIVITY_BOT_TOKEN` set)."
    await interaction.followup.send(
        f"Soundboard: {channel.mention} | Uploads: {add_ch.mention}{note}")
 
 
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.channel.name != ADD_CHANNEL or not message.attachments:
        return
 
    att = next((a for a in message.attachments if a.filename.lower().endswith(EXTENSIONS)), None)
    if not att:
        return await message.reply("Attach an .mp3, .wav or .ogg file.")
    if att.size > MAX_BYTES:
        return await message.reply("File too large (max 8 MB).")
    if len(SOUNDS) >= MAX_SOUNDS:
        return await message.reply(f"Limit of {MAX_SOUNDS} sounds reached. Use /removesound first.")
 
    entry = load_json(STATE_FILE, {}).get(str(message.guild.id))
    buttons_message = get_buttons_message(entry)
    if not buttons_message:
        return await message.reply("Run /setup first.")
 
    parts = message.content.strip().split(maxsplit=1)
    if not parts:
        return await message.reply("Put an emoji first in the message, e.g. `🦆 Quack`.")
 
    emoji = parts[0]
    label = (parts[1] if len(parts) > 1 else os.path.splitext(att.filename)[0])[:40]
    used = {norm(e) for e in SOUNDS} | {norm(STOP_EMOJI)}
    if norm(emoji) in used:
        return await message.reply(f"{emoji} is already used.")
 
    filename = re.sub(r"[^\w\-.]", "_", att.filename)
    if os.path.exists(os.path.join(SOUNDS_DIR, filename)):
        filename = f"{len(SOUNDS)}_{filename}"
    path = os.path.join(SOUNDS_DIR, filename)
    await att.save(path)
 
    # Add it and try to update the board; if Discord rejects the emoji, undo everything
    SOUNDS[emoji] = [label, filename]
    try:
        await update_board(buttons_message)
    except discord.HTTPException:
        SOUNDS.pop(emoji, None)
        try:
            os.remove(path)
        except OSError:
            pass
        return await message.reply(
            "That doesn't look like a valid emoji. Put it first in the message.")
 
    save_json(SOUNDS_FILE, SOUNDS)
    await refresh_boards(skip_message_id=buttons_message["message_id"])  # other servers
    await message.add_reaction("✅")
 
 
async def remove_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"{e} {v[0]}", value=e)
        for e, v in SOUNDS.items()
        if current.lower() in v[0].lower()
    ][:25]
 
 
@bot.tree.command(name="removesound", description="Delete a sound from the soundboard")
@app_commands.default_permissions(manage_channels=True)
@app_commands.autocomplete(sound=remove_autocomplete)
async def removesound(interaction: discord.Interaction, sound: str):
    await interaction.response.defer(ephemeral=True)
    key = next((e for e in SOUNDS if norm(e) == norm(sound)), None)
    if not key:
        return await interaction.followup.send("Sound not found.")
    label, filename = SOUNDS.pop(key)
    save_json(SOUNDS_FILE, SOUNDS)
    try:
        os.remove(os.path.join(SOUNDS_DIR, filename))
    except OSError:
        pass
    await refresh_boards()
    await interaction.followup.send(f"Removed **{label}**.")
 
 
@bot.tree.command(name="leave", description="Disconnect the bot from voice")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client
 
    mixer = mixers.pop(guild.id, None)
    if mixer:
        mixer.cleanup()
 
    activity_list = active_sounds_lists.get(guild.id)
    if activity_list:
        await activity_list.remove_all_and_update()
 
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Left the voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
 
 
bot.run(TOKEN)



