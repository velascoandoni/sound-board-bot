import os
import re
import json
import discord
from discord import app_commands
from dotenv import load_dotenv
import array
import queue
import threading
import subprocess
import asyncio

import shutil

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__)) 

DEFAULTS_DIR = os.path.join(BASE_DIR, "defaults")
DATA_DIR = os.path.join(BASE_DIR, "data")

SOUNDS_DIR = os.path.join(DATA_DIR, "sounds")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
SOUNDS_FILE = os.path.join(DATA_DIR, "sounds.json")


ADD_CHANNEL = "add-sounds"
MAX_SOUNDS = 20
MAX_BYTES = 8 * 1024 * 1024
EXTENSIONS = (".mp3", ".wav", ".ogg")



if not os.path.isdir(SOUNDS_FILE):
    shutil.copytree(DEFAULTS_DIR,DATA_DIR,dirs_exist_ok=True)

# RED X EMOJI is used as a stop button for the soundboard
STOP_EMOJI = "❌"

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


SOUNDS = load_json(SOUNDS_FILE, None)  # emoji -> [label, filename]


def find_sound(emoji: str):
    for e, v in SOUNDS.items():
        if norm(e) == norm(emoji):
            return v
    return None


def board_text():
    lines = [f"{e}  {v[0]}" for e, v in SOUNDS.items()]
    return ("**🔊 Soundboard**\nJoin a voice channel, then click a reaction to play a sound.\n\n"
            +f"{norm(STOP_EMOJI)}  Stop the sound\n\n"
            + "\n".join(lines))



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

        for i, sample in enumerate(mixed):
            mixed[i] = max(-32768, min(32767, sample))

        

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
            process = source["process"]
            try:
                process.kill()
            except OSError:
                pass

    def cleanup(self):
        with self.lock:
            self.closed = True
            sources = list(self.sources)
            self.sources.clear()

        for source in sources:
            process = source["process"]
            try:
                process.kill()
            except OSError:
                pass



class SoundBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read uploads
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = SoundBot()

mixers = {}


async def get_mixer(guild, vc):
    mixer = mixers.get(guild.id)

    if mixer is None or mixer.closed:
        mixer = SoundMixer()
        mixers[guild.id] = mixer
        vc.play(mixer)

    return mixer
async def add_reactions(msg: discord.Message):
    """Add reactions to a message, ignoring any that are invalid or already present."""
    await msg.add_reaction(STOP_EMOJI)
    for e in SOUNDS:
        await msg.add_reaction(e)

async def refresh_boards():
    """Update the soundboard message and reactions in every server."""
    for entry in load_json(STATE_FILE, {}).values():
        ch = bot.get_channel(entry["channel_id"])
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(entry["message_id"])
            await msg.edit(content=board_text())

            for reaction in list(msg.reactions):
                async for user in reaction.users():
                    if user.id != bot.user.id:
                        await msg.remove_reaction(reaction.emoji, user)


            #await add_reactions(msg)

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
        return await interaction.followup.send(f"Soundboard: {ch.mention} | Uploads: {add_ch.mention}")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, read_message_history=True,
            send_messages=False, add_reactions=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, add_reactions=True,
            manage_messages=True, read_message_history=True),
    }
    channel = await guild.create_text_channel(
        "soundboard", overwrites=overwrites,
        topic="Join a voice channel, then click a reaction to play a sound.")
    
    msg = await channel.send(board_text())
    await add_reactions(msg)

    state[str(guild.id)] = {"channel_id": channel.id, "message_id": msg.id}
    save_json(STATE_FILE, state)
    await interaction.followup.send(f"Soundboard: {channel.mention} | Uploads: {add_ch.mention}")


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
    label = (parts[1] if len(parts) > 1 else os.path.splitext(att.filename)[0])[:40]
    used = {norm(e) for e in SOUNDS}

    if parts:
        emoji = parts[0]
    if not emoji:
        return await message.reply("No emoji, please specify one.")
    if norm(emoji) in used:
        return await message.reply(f"{emoji} is already used by another sound.")

    # Test the emoji on the soundboard message before saving anything
    try:
        board_ch = bot.get_channel(entry["channel_id"])
        board_msg = await board_ch.fetch_message(entry["message_id"])
        await board_msg.add_reaction(emoji)
    except discord.HTTPException:
        return await message.reply("That doesn't look like a valid emoji. Put it first in the message.")

    filename = re.sub(r"[^\w\-.]", "_", att.filename)
    if os.path.exists(os.path.join(SOUNDS_DIR, filename)):
        filename = f"{len(SOUNDS)}_{filename}"
    await att.save(os.path.join(SOUNDS_DIR, filename))

    SOUNDS[emoji] = [label, filename]
    save_json(SOUNDS_FILE, SOUNDS)
    await refresh_boards()
    await message.add_reaction("✅")


def schedule_reaction_reset(channel_id, message_id, user_id, emoji):
    async def reset():
        channel = bot.get_channel(channel_id)
        if channel is None:
            return

        try:
            message = await channel.fetch_message(message_id)
            await message.remove_reaction(
                emoji, discord.Object(id=user_id)
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as err:
            print(f"Could not reset reaction {emoji}: {err}")

    bot.loop.create_task(reset())


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return

    entry = load_json(STATE_FILE, {}).get(str(payload.guild_id))
    if not entry or payload.message_id != entry["message_id"]:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id) or payload.member

    channel = bot.get_channel(payload.channel_id)

    
    if str(payload.emoji) == STOP_EMOJI:
        mixer = mixers.get(guild.id)
        if mixer:
            mixer.stop_all()
            await refresh_boards()
            await channel.send(
                "Stopped all sounds.", delete_after=5
            )
        return

    sound = find_sound(str(payload.emoji))
    if not sound or not member or not member.voice:
        return

    path = os.path.join(SOUNDS_DIR, sound[1])
    if not os.path.isfile(path):
        print(f"Missing file: {path}")
        return

    vc = guild.voice_client

    if vc is None:
        vc = await member.voice.channel.connect()
    elif vc.channel != member.voice.channel:
        await vc.move_to(member.voice.channel)

    try:
        mixer = await get_mixer(guild, vc)

        emoji = str(payload.emoji)
        channel_id = payload.channel_id
        message_id = payload.message_id
        user_id = payload.user_id

        def on_finished():

            schedule_reaction_reset(
                channel_id, message_id, user_id, emoji
            )

        await asyncio.to_thread(
            mixer.add_sound, path, on_finished
        )

    except (OSError, RuntimeError) as err:
        print(f"Could not play sound: {err}")



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
        await interaction.response.send_message(
            "Left the voice channel.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "I'm not in a voice channel.", ephemeral=True
        )



bot.run(TOKEN)