import os
import re
import json
import shutil
import asyncio
import subprocess
from datetime import datetime


import discord
from discord import app_commands
from dotenv import load_dotenv

from soundboard_bot.active_sounds_list import ActiveSoundsList
from soundboard_bot.sound_bot import SoundBot
from soundboard_bot.sound_button import SoundButton
from soundboard_bot.sound_mixer import SoundMixer
from soundboard_bot.stop_button import StopButton

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
            return [*v, e]
    return None


def board_text():
    return (
        "**🔊 Soundboard**\n"
        "Join a voice channel, then press a button to play a sound.\n"
        f"Press {STOP_EMOJI} **Stop** to silence everything."
    )


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


# ---------------------------------------------------------------------------
# Bot + buttons
# ---------------------------------------------------------------------------
mixers = {}
voice_locks = {}


async def get_mixer(guild, vc):
    mixer = mixers.get(guild.id)

    if mixer is None or mixer.closed:
        mixer = SoundMixer(FFMPEG_PATH or "ffmpeg")
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
    activity_list = active_sounds_lists.get(guild.id)
    if activity_list is None:
        return await interaction.followup.send(
            "Run /setup before playing sounds.", ephemeral=True
        )

    try:
        async with voice_locks.setdefault(guild.id, asyncio.Lock()):
            vc = guild.voice_client
            if vc is None:
                vc = await member.voice.channel.connect()
            elif vc.channel != member.voice.channel:
                await vc.move_to(member.voice.channel)
            mixer = await get_mixer(guild, vc)
        duration_seconds = await get_audio_duration(path)
        sound_id= await activity_list.add_and_update(sound,duration_seconds,member.id)
        loop = asyncio.get_running_loop()

        async def on_finished():
            await activity_list.remove_and_update(sound_id)
        def schedule_on_finished():
            asyncio.run_coroutine_threadsafe(on_finished(), loop)
        await asyncio.to_thread(mixer.add_sound, path,on_finished=schedule_on_finished)

    except (OSError, RuntimeError, asyncio.TimeoutError, discord.ClientException) as err:
        print(f"Could not play sound: {err}")
        await interaction.followup.send("Could not play that sound.", ephemeral=True)


async def stop_sounds(interaction: discord.Interaction):
    mixer = mixers.get(interaction.guild.id)
    if mixer:
        mixer.stop_all()
    activity_list = active_sounds_lists.get(interaction.guild.id)
    if activity_list:
        await activity_list.remove_all_and_update()
    await interaction.response.defer()


def build_view():
    view = discord.ui.View(timeout=None)  # timeout=None -> buttons never expire
    view.add_item(StopButton(stop_sounds, STOP_EMOJI))
    for key, sound_data in SOUNDS.items():
        view.add_item(SoundButton(key, sound_data[0], play_sound))
    return view


active_sounds_lists = {}
bot = SoundBot(build_view, STATE_FILE, load_json, active_sounds_lists)


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
        buttons_message = entry.get("buttons-message")
        if buttons_message is None and "message_id" in entry:
            buttons_message = entry
        if not buttons_message or buttons_message["message_id"] == skip_message_id:
            continue
        try:
            await update_board(buttons_message)
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

    old = state.get(str(guild.id), {})
    buttons_message = old.get("buttons-message")
    if buttons_message is None and "channel_id" in old:
        buttons_message = {
            "channel_id": old["channel_id"],
            "message_id": old["message_id"],
        }

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

    activity_message = old.get("activity-message")
    activity_channel = (
        guild.get_channel(activity_message["channel_id"])
        if activity_message
        else None
    )
    activity_list = active_sounds_lists.get(guild.id)
    if activity_channel is not None and activity_message:
        try:
            activity_list = activity_list or ActiveSoundsList()
            activity_list.message = await activity_channel.fetch_message(
                activity_message["message_id"]
            )
        except discord.HTTPException:
            activity_list = ActiveSoundsList()
            activity_post = await activity_list.send(channel)
            activity_message = {
                "channel_id": channel.id,
                "message_id": activity_post.id,
            }
    else:
        activity_list = ActiveSoundsList()
        activity_post = await activity_list.send(channel)
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
    buttons_message = entry.get("buttons-message") if entry else None
    if buttons_message is None and entry and "message_id" in entry:
        buttons_message = entry
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

    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Left the voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


bot.run(TOKEN)