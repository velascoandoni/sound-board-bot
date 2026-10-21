import os
import re
import json
import array
import queue
import shutil
import asyncio
import threading
import subprocess

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()

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
            return v
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
# Bot + buttons
# ---------------------------------------------------------------------------
mixers = {}
voice_locks = {}


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
    try:
        async with voice_locks.setdefault(guild.id, asyncio.Lock()):
            vc = guild.voice_client
            if vc is None:
                vc = await member.voice.channel.connect()
            elif vc.channel != member.voice.channel:
                await vc.move_to(member.voice.channel)
            mixer = await get_mixer(guild, vc)

        await asyncio.to_thread(mixer.add_sound, path,on_finished)

    except (OSError, RuntimeError, asyncio.TimeoutError, discord.ClientException) as err:
        print(f"Could not play sound: {err}")
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
        await interaction.response.defer()  # silent acknowledge


def build_view():
    view = discord.ui.View(timeout=None)  # timeout=None -> buttons never expire
    view.add_item(StopButton())
    for key, (label, _filename) in SOUNDS.items():
        view.add_item(SoundButton(key, label))
    return view


class SoundBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read uploads
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        # Re-attach the buttons of existing soundboard messages after a restart
        for entry in load_json(STATE_FILE, {}).values():
            self.add_view(build_view(), message_id=entry["message_id"])


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
        if entry["message_id"] == skip_message_id:
            continue
        try:
            await update_board(entry)
        except discord.HTTPException as err:
            print(f"Could not refresh board: {err}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


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

    old = state.get(str(guild.id))
    if old and guild.get_channel(old["channel_id"]):
        ch = guild.get_channel(old["channel_id"])
        # Convert an existing reaction-based board to buttons (and re-sync it)
        try:
            await update_board(old)
            msg = await ch.fetch_message(old["message_id"])
            await msg.clear_reactions()
        except discord.HTTPException as err:
            print(f"Could not update existing board: {err}")
        return await interaction.followup.send(
            f"Soundboard: {ch.mention} | Uploads: {add_ch.mention}")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True),
    }
    channel = await guild.create_text_channel(
        "soundboard", overwrites=overwrites,
        topic="Join a voice channel, then press a button to play a sound.")

    msg = await channel.send(board_text(), view=build_view())

    state[str(guild.id)] = {"channel_id": channel.id, "message_id": msg.id}
    save_json(STATE_FILE, state)
    await interaction.followup.send(
        f"Soundboard: {channel.mention} | Uploads: {add_ch.mention}")


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
    if not entry:
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
        await update_board(entry)
    except discord.HTTPException:
        SOUNDS.pop(emoji, None)
        try:
            os.remove(path)
        except OSError:
            pass
        return await message.reply(
            "That doesn't look like a valid emoji. Put it first in the message.")

    save_json(SOUNDS_FILE, SOUNDS)
    await refresh_boards(skip_message_id=entry["message_id"])  # other servers
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

    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Left the voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


bot.run(TOKEN)