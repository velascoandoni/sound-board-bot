# Discord Soundboard Bot

A soundboard bot: a channel with one message full of buttons, press a button to play a sound in your voice channel.
Upload new sounds by posting an audio file in `#add-sounds`.

## 1. Create the Discord bot

1. Go to <https://discord.com/developers/applications> and create a **New Application**.
2. **Bot** tab: click **Reset Token** and copy it. Turn on **Message Content Intent**.
   Turn **Public Bot** off if you want to keep it private.
3. **Installation** tab: set **Install Link** to **None** (required for private bots).
4. **OAuth2 > URL Generator**: scopes `bot` and `applications.commands`, permissions:
   View Channels, Send Messages, Read Message History, Connect, Speak, Use Voice Activity, Manage Channels.
5. Open the generated URL and add the bot to your server.

## 2. Run it (Docker)

```
git clone https://github.com/YOUR_USER/YOUR_REPO.git
cd YOUR_REPO
cp .env.example .env        # then edit .env and paste your token
docker compose up -d --build
```

Then run `/setup` in your Discord server once.

| Task            | Command                                   |
|-----------------|-------------------------------------------|
| View logs       | `docker compose logs -f`                  |
| Update          | `git pull && docker compose up -d --build`|
| Stop            | `docker compose down`                     |

Uploaded sounds and state are stored in the `soundbot-data` Docker volume, so they survive updates.

## 3. Using it

- `/setup` creates `#soundboard` and `#add-sounds` (needs Manage Channels).
- Upload an `.mp3`, `.wav` or `.ogg` in `#add-sounds` with an emoji first in the message, e.g. `🦆 Quack`.
- `/removesound` deletes a sound, `/leave` disconnects the bot from voice.
- Up to 24 sounds per server.
