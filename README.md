# MP3 Tools Bot

A Telegram bot inspired by @mp3toolsbot, built with Python. Send it an audio,
voice note, or video file and it will let you:

- 🎵 Convert to MP3
- ✂️ Trim / cut to a start-end time
- 🔗 Merge multiple files into one
- 📻 Change bitrate (128k / 192k / 256k / 320k)
- 🔊 Change volume
- ⏩ Change playback speed (pitch-preserved)
- 🏷 Edit ID3 tags (title, artist, album)
- 🖼 Set / replace album cover art

## 1. Prerequisites

- Python 3.10+
- **ffmpeg** installed and on your system `PATH`
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: download from ffmpeg.org and add the `bin` folder to PATH
- A Telegram bot token from **@BotFather**:
  1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram
  2. Send `/newbot` and follow the prompts
  3. Copy the token it gives you (looks like `123456789:AA...`)

## 2. Install

```bash
cd mp3toolsbot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure

Set your bot token as an environment variable:

```bash
export BOT_TOKEN="123456789:AA-your-token-here"    # Windows (PowerShell): $env:BOT_TOKEN="..."
```

## 4. Run

```bash
python bot.py
```

You should see `MP3 Tools Bot starting (polling)...` in the console. Open your
bot in Telegram and send `/start`.

## How it works

- Files are downloaded to a per-user folder under `bot_files/<user_id>/`.
- All audio processing is done with `ffmpeg` via subprocess calls.
- ID3 tags and cover art are handled with `mutagen`.
- The main flow uses a `ConversationHandler`: send a file → tap an action
  button → follow the prompt → receive the result back as an MP3.
- Merging is a separate flow: `/merge` → send files one by one → `/done`.

## Limits & notes

- Telegram bots can only **download** files up to 20MB via the standard Bot
  API, and this bot's `send_audio` calls assume a 50MB **upload** cap unless
  you run your own [local Bot API server](https://github.com/tdlib/telegram-bot-api),
  which raises both limits to 2GB. If you expect larger files, set up a local
  Bot API server and point `ApplicationBuilder().base_url(...)` at it in `bot.py`.
- The bot currently supports single-user, single-session state per user
  (stored in-memory via `context.user_data`). For production use with many
  concurrent users, this is fine since PTB keeps per-chat state separately —
  but if you restart the bot mid-conversation, users will need to resend
  their file.
- Speed changes use ffmpeg's `atempo` filter chained automatically for
  factors outside its native 0.5x–2.0x range.

## Extending it

Ideas for more features, in roughly increasing difficulty:
- Silence trimming/detection (`ffmpeg -af silenceremove`)
- Audio format conversion beyond MP3 (WAV, OGG, FLAC, M4A)
- Waveform/spectrogram image generation
- Splitting one long file into multiple chunks
- Text-to-speech or speech-to-text integration

Each of these can be added as another `act:` callback + conversation state,
following the same pattern as `act_trim` / `do_trim` in `bot.py`.
