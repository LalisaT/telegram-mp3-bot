"""
MP3 Tools Bot
=============
A Telegram bot that mimics the core features of @mp3toolsbot:

  - Convert audio / video / voice notes to MP3
  - Trim (cut) an audio file to a start/end timestamp
  - Merge two or more audio files into one
  - Change bitrate (128/192/256/320 kbps)
  - Change volume (louder / quieter)
  - Change playback speed (without changing pitch)
  - Edit ID3 tags (title, artist, album)
  - Set / replace album cover art

Built with python-telegram-bot v21 (async) + ffmpeg (via subprocess) + mutagen (ID3 tags).

Run:
    export BOT_TOKEN="123456:ABC-your-bot-father-token"
    pip install -r requirements.txt
    python bot.py

See README.md for full setup instructions.
"""

import os
import re
import glob
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WORK_DIR = Path("bot_files")
WORK_DIR.mkdir(exist_ok=True)

MAX_TELEGRAM_FILE_MB = 50  # bots can only download files up to 20MB via getFile
                            # and send up to 50MB by default (higher with local bot API server)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mp3toolsbot")

# Conversation states
(
    CHOOSING_ACTION,
    WAITING_TRIM,
    WAITING_BITRATE,
    WAITING_VOLUME,
    WAITING_SPEED,
    WAITING_TAG_FIELD,
    WAITING_TAG_VALUE,
    WAITING_COVER,
    MERGE_COLLECTING,
) = range(9)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def user_dir(user_id: int) -> Path:
    d = WORK_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def clean_user_dir(user_id: int):
    d = user_dir(user_id)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)


async def run_ffmpeg(args: list[str]) -> tuple[bool, str]:
    """Run an ffmpeg command asynchronously. Returns (success, stderr_tail)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    ok = proc.returncode == 0
    tail = stderr.decode(errors="ignore")[-1200:]
    return ok, tail


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎵 Convert to MP3", callback_data="act:convert")],
        [InlineKeyboardButton("✂️ Trim", callback_data="act:trim"),
         InlineKeyboardButton("🔗 Merge", callback_data="act:merge")],
        [InlineKeyboardButton("📻 Bitrate", callback_data="act:bitrate"),
         InlineKeyboardButton("🔊 Volume", callback_data="act:volume")],
        [InlineKeyboardButton("⏩ Speed", callback_data="act:speed"),
         InlineKeyboardButton("🏷 Edit Tags", callback_data="act:tags")],
        [InlineKeyboardButton("🖼 Set Cover", callback_data="act:cover")],
    ]
    return InlineKeyboardMarkup(rows)


TIME_RE = re.compile(r"^(\d{1,2}:)?\d{1,2}:\d{2}$|^\d+$")


def parse_time(t: str) -> str | None:
    """Accept 'SS', 'MM:SS' or 'HH:MM:SS' and return an ffmpeg-friendly timestamp."""
    t = t.strip()
    if not TIME_RE.match(t):
        return None
    return t if ":" in t else f"00:00:{int(t):02d}"


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clean_user_dir(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text(
        "🎧 *MP3 Tools Bot*\n\n"
        "Send me an audio, voice note, or video file and I'll show you what I can do with it:\n\n"
        "• Convert to MP3\n"
        "• Trim / cut\n"
        "• Merge multiple files\n"
        "• Change bitrate, volume or speed\n"
        "• Edit ID3 tags & cover art\n\n"
        "Just send a file to get started, or use /merge to combine several files.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send a new file whenever you're ready.",
                                     reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How to use me:\n\n"
        "1️⃣ Send an audio/video/voice file.\n"
        "2️⃣ Tap a button to choose an action (trim, bitrate, tags, etc).\n"
        "3️⃣ Follow the prompt (e.g. send start/end time for trimming).\n\n"
        "To merge files: /merge, then send each file, then /done.\n"
        "/cancel anytime to abort the current action."
    )


# --------------------------------------------------------------------------
# Receiving a file -> show action menu
# --------------------------------------------------------------------------

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    tg_file = msg.audio or msg.voice or msg.video or msg.document
    if tg_file is None:
        await msg.reply_text("Please send an audio, voice, or video file.")
        return CHOOSING_ACTION

    # If we're in merge-collection mode, route there instead
    if context.user_data.get("merging"):
        return await merge_collect_file(update, context)

    size = getattr(tg_file, "file_size", 0) or 0
    if size and size > MAX_TELEGRAM_FILE_MB * 1024 * 1024:
        await msg.reply_text(f"That file is too large (> {MAX_TELEGRAM_FILE_MB}MB). "
                              f"Please send a smaller file.")
        return ConversationHandler.END

    await msg.chat.send_action(ChatAction.TYPING)
    file = await tg_file.get_file()
    uid = update.effective_user.id
    src_path = user_dir(uid) / f"input_{tg_file.file_unique_id}"
    await file.download_to_drive(custom_path=src_path)

    context.user_data["src_path"] = str(src_path)
    context.user_data["orig_name"] = getattr(tg_file, "file_name", None) or "audio"

    await msg.reply_text("Got it! What would you like to do?", reply_markup=main_menu_keyboard())
    return CHOOSING_ACTION


# --------------------------------------------------------------------------
# Action: Convert to MP3
# --------------------------------------------------------------------------

async def act_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    src = context.user_data.get("src_path")
    if not src:
        await query.edit_message_text("Session expired, please resend the file.")
        return ConversationHandler.END

    await query.edit_message_text("🔄 Converting to MP3...")
    out_path = Path(src).with_suffix(".mp3")
    ok, err = await run_ffmpeg(["-i", src, "-vn", "-ar", "44100", "-ac", "2",
                                 "-b:a", "192k", str(out_path)])
    if not ok:
        await query.message.reply_text(f"❌ Conversion failed:\n`{err[-500:]}`",
                                        parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await send_result(query.message, out_path, caption="✅ Converted to MP3")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Action: Trim
# --------------------------------------------------------------------------

async def act_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✂️ Send the start and end time like this:\n`00:10 00:45`\n"
        "(formats: `SS`, `MM:SS`, or `HH:MM:SS`)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_TRIM


async def do_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("Please send exactly two times, e.g. `00:10 00:45`",
                                         parse_mode=ParseMode.MARKDOWN)
        return WAITING_TRIM

    start_t, end_t = parse_time(parts[0]), parse_time(parts[1])
    if not start_t or not end_t:
        await update.message.reply_text("I couldn't parse those times. Try `SS`, `MM:SS`, or `HH:MM:SS`.")
        return WAITING_TRIM

    src = context.user_data.get("src_path")
    if not src:
        await update.message.reply_text("Session expired, please resend the file.")
        return ConversationHandler.END

    status = await update.message.reply_text("✂️ Trimming...")
    out_path = Path(src).with_name(Path(src).stem + "_trimmed.mp3")
    ok, err = await run_ffmpeg(["-i", src, "-ss", start_t, "-to", end_t,
                                 "-vn", "-b:a", "192k", str(out_path)])
    if not ok:
        await status.edit_text(f"❌ Trim failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await status.delete()
    await send_result(update.message, out_path, caption=f"✅ Trimmed {start_t} → {end_t}")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Action: Bitrate
# --------------------------------------------------------------------------

def bitrate_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(b, callback_data=f"br:{b}")
             for b in ("128k", "192k", "256k", "320k")]]
    return InlineKeyboardMarkup(rows)


async def act_bitrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📻 Choose the target bitrate:", reply_markup=bitrate_keyboard())
    return WAITING_BITRATE


async def do_bitrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    bitrate = query.data.split(":")[1]
    src = context.user_data.get("src_path")
    if not src:
        await query.edit_message_text("Session expired, please resend the file.")
        return ConversationHandler.END

    await query.edit_message_text(f"📻 Re-encoding at {bitrate}...")
    out_path = Path(src).with_name(Path(src).stem + f"_{bitrate}.mp3")
    ok, err = await run_ffmpeg(["-i", src, "-vn", "-b:a", bitrate, str(out_path)])
    if not ok:
        await query.message.reply_text(f"❌ Failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await send_result(query.message, out_path, caption=f"✅ Bitrate set to {bitrate}")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Action: Volume
# --------------------------------------------------------------------------

def volume_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(v, callback_data=f"vol:{v}")
             for v in ("0.5x", "1.5x", "2x", "3x")]]
    return InlineKeyboardMarkup(rows)


async def act_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔊 Choose a volume multiplier:", reply_markup=volume_keyboard())
    return WAITING_VOLUME


async def do_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    factor = query.data.split(":")[1].rstrip("x")
    src = context.user_data.get("src_path")
    if not src:
        await query.edit_message_text("Session expired, please resend the file.")
        return ConversationHandler.END

    await query.edit_message_text(f"🔊 Adjusting volume x{factor}...")
    out_path = Path(src).with_name(Path(src).stem + f"_vol{factor}.mp3")
    ok, err = await run_ffmpeg(["-i", src, "-vn", "-filter:a", f"volume={factor}",
                                 "-b:a", "192k", str(out_path)])
    if not ok:
        await query.message.reply_text(f"❌ Failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await send_result(query.message, out_path, caption=f"✅ Volume x{factor}")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Action: Speed
# --------------------------------------------------------------------------

def speed_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(s, callback_data=f"spd:{s}")
             for s in ("0.5x", "0.75x", "1.25x", "1.5x", "2x")]]
    return InlineKeyboardMarkup(rows)


async def act_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏩ Choose a playback speed (pitch is preserved):",
                                   reply_markup=speed_keyboard())
    return WAITING_SPEED


async def do_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    factor = float(query.data.split(":")[1].rstrip("x"))
    src = context.user_data.get("src_path")
    if not src:
        await query.edit_message_text("Session expired, please resend the file.")
        return ConversationHandler.END

    await query.edit_message_text(f"⏩ Changing speed to {factor}x...")
    out_path = Path(src).with_name(Path(src).stem + f"_spd{factor}.mp3")
    # atempo only supports 0.5-2.0 per filter instance; chain if needed
    remaining = factor
    filters_chain = []
    while remaining < 0.5 or remaining > 2.0:
        step = 2.0 if remaining > 2.0 else 0.5
        filters_chain.append(f"atempo={step}")
        remaining /= step
    filters_chain.append(f"atempo={remaining:.4f}")
    atempo_filter = ",".join(filters_chain)

    ok, err = await run_ffmpeg(["-i", src, "-vn", "-filter:a", atempo_filter,
                                 "-b:a", "192k", str(out_path)])
    if not ok:
        await query.message.reply_text(f"❌ Failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await send_result(query.message, out_path, caption=f"✅ Speed {factor}x")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Action: Edit tags
# --------------------------------------------------------------------------

def tags_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Title", callback_data="tag:title"),
         InlineKeyboardButton("Artist", callback_data="tag:artist")],
        [InlineKeyboardButton("Album", callback_data="tag:album")],
    ]
    return InlineKeyboardMarkup(rows)


async def act_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🏷 Which tag do you want to set?", reply_markup=tags_keyboard())
    return WAITING_TAG_FIELD


async def choose_tag_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":")[1]
    context.user_data["tag_field"] = field
    await query.edit_message_text(f"Send the new *{field}*:", parse_mode=ParseMode.MARKDOWN)
    return WAITING_TAG_VALUE


async def do_tag_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    src = context.user_data.get("src_path")
    field = context.user_data.get("tag_field")
    value = update.message.text.strip()
    if not src or not field:
        await update.message.reply_text("Session expired, please resend the file.")
        return ConversationHandler.END

    status = await update.message.reply_text("🏷 Ensuring MP3 + writing tag...")
    src_path = Path(src)
    # Make sure we're working with an mp3 (ID3 tags require it)
    mp3_path = src_path if src_path.suffix.lower() == ".mp3" else src_path.with_suffix(".mp3")
    if mp3_path == src_path or not mp3_path.exists():
        ok, err = await run_ffmpeg(["-i", str(src_path), "-vn", "-b:a", "192k", str(mp3_path)])
        if not ok:
            await status.edit_text(f"❌ Failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

    try:
        try:
            tags = EasyID3(mp3_path)
        except ID3NoHeaderError:
            tags = EasyID3()
            tags.save(mp3_path)
            tags = EasyID3(mp3_path)
        tags[field] = value
        tags.save(mp3_path)
    except Exception as e:
        await status.edit_text(f"❌ Couldn't write tag: {e}")
        return ConversationHandler.END

    context.user_data["src_path"] = str(mp3_path)
    await status.delete()
    await send_result(update.message, mp3_path, caption=f"✅ {field.capitalize()} set to: {value}")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Action: Set cover art
# --------------------------------------------------------------------------

async def act_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🖼 Send me the image (as a photo or file) to use as cover art.")
    return WAITING_COVER


async def do_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    src = context.user_data.get("src_path")
    if not src:
        await update.message.reply_text("Session expired, please resend the audio file first.")
        return ConversationHandler.END

    photo_file = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        photo_file = await update.message.document.get_file()
    else:
        await update.message.reply_text("That doesn't look like an image. Please send a photo or image file.")
        return WAITING_COVER

    uid = update.effective_user.id
    img_path = user_dir(uid) / "cover.jpg"
    await photo_file.download_to_drive(custom_path=img_path)

    status = await update.message.reply_text("🖼 Applying cover art...")
    src_path = Path(src)
    mp3_path = src_path if src_path.suffix.lower() == ".mp3" else src_path.with_suffix(".mp3")
    if mp3_path == src_path or not mp3_path.exists():
        ok, err = await run_ffmpeg(["-i", str(src_path), "-vn", "-b:a", "192k", str(mp3_path)])
        if not ok:
            await status.edit_text(f"❌ Failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

    try:
        try:
            id3 = ID3(mp3_path)
        except ID3NoHeaderError:
            id3 = ID3()
        id3.delall("APIC")
        with open(img_path, "rb") as f:
            id3.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=f.read()))
        id3.save(mp3_path)
    except Exception as e:
        await status.edit_text(f"❌ Couldn't set cover: {e}")
        return ConversationHandler.END

    context.user_data["src_path"] = str(mp3_path)
    await status.delete()
    await send_result(update.message, mp3_path, caption="✅ Cover art updated")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Merge flow (separate from the single-file conversation)
# --------------------------------------------------------------------------

async def merge_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    clean_user_dir(uid)
    context.user_data.clear()
    context.user_data["merging"] = True
    context.user_data["merge_files"] = []
    await update.message.reply_text(
        "🔗 Send me two or more audio/video files, one at a time.\n"
        "When you're done, send /done. Send /cancel to abort."
    )
    return MERGE_COLLECTING


async def merge_collect_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    tg_file = msg.audio or msg.voice or msg.video or msg.document
    if tg_file is None:
        await msg.reply_text("Please send an audio or video file, or /done to finish.")
        return MERGE_COLLECTING

    await msg.chat.send_action(ChatAction.TYPING)
    file = await tg_file.get_file()
    uid = update.effective_user.id
    idx = len(context.user_data["merge_files"]) + 1
    dest = user_dir(uid) / f"merge_{idx}_{tg_file.file_unique_id}"
    await file.download_to_drive(custom_path=dest)
    context.user_data["merge_files"].append(str(dest))

    await msg.reply_text(f"Added file #{idx}. Send another, or /done to merge.")
    return MERGE_COLLECTING


async def merge_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    files = context.user_data.get("merge_files", [])
    if len(files) < 2:
        await update.message.reply_text("I need at least two files to merge. Send another file or /cancel.")
        return MERGE_COLLECTING

    status = await update.message.reply_text(f"🔗 Merging {len(files)} files...")
    uid = update.effective_user.id
    ud = user_dir(uid)

    # Normalize each input to a temp mp3 first (handles mixed formats/codecs)
    normalized = []
    for i, f in enumerate(files):
        norm_path = ud / f"norm_{i}.mp3"
        ok, err = await run_ffmpeg(["-i", f, "-vn", "-ar", "44100", "-ac", "2",
                                     "-b:a", "192k", str(norm_path)])
        if not ok:
            await status.edit_text(f"❌ Failed to process file #{i+1}:\n`{err[-500:]}`",
                                    parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END
        normalized.append(norm_path)

    concat_list = ud / "concat_list.txt"
    with open(concat_list, "w") as f:
        for p in normalized:
            f.write(f"file '{p.resolve()}'\n")

    out_path = ud / "merged.mp3"
    ok, err = await run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list),
                                 "-c", "copy", str(out_path)])
    if not ok:
        await status.edit_text(f"❌ Merge failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await status.delete()
    await send_result(update.message, out_path, caption=f"✅ Merged {len(files)} files")
    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Output helper
# --------------------------------------------------------------------------

async def send_result(message, path: Path, caption: str = ""):
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_TELEGRAM_FILE_MB:
        await message.reply_text(
            f"{caption}\n\n⚠️ The result is {size_mb:.1f}MB, which is too large to send via Telegram."
        )
        return
    with open(path, "rb") as f:
        await message.reply_audio(audio=f, caption=caption, filename=path.name)


# --------------------------------------------------------------------------
# Wire everything up
# --------------------------------------------------------------------------

def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("Please set the BOT_TOKEN environment variable (see README.md).")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    file_filter = filters.AUDIO | filters.VOICE | filters.VIDEO | filters.Document.AUDIO

    main_conv = ConversationHandler(
        entry_points=[MessageHandler(file_filter, receive_file)],
        states={
            CHOOSING_ACTION: [
                CallbackQueryHandler(act_convert, pattern="^act:convert$"),
                CallbackQueryHandler(act_trim, pattern="^act:trim$"),
                CallbackQueryHandler(act_bitrate, pattern="^act:bitrate$"),
                CallbackQueryHandler(act_volume, pattern="^act:volume$"),
                CallbackQueryHandler(act_speed, pattern="^act:speed$"),
                CallbackQueryHandler(act_tags, pattern="^act:tags$"),
                CallbackQueryHandler(act_cover, pattern="^act:cover$"),
                MessageHandler(file_filter, receive_file),
            ],
            WAITING_TRIM: [MessageHandler(filters.TEXT & ~filters.COMMAND, do_trim)],
            WAITING_BITRATE: [CallbackQueryHandler(do_bitrate, pattern="^br:")],
            WAITING_VOLUME: [CallbackQueryHandler(do_volume, pattern="^vol:")],
            WAITING_SPEED: [CallbackQueryHandler(do_speed, pattern="^spd:")],
            WAITING_TAG_FIELD: [CallbackQueryHandler(choose_tag_field, pattern="^tag:")],
            WAITING_TAG_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, do_tag_value)],
            WAITING_COVER: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, do_cover)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        name="main_conversation",
    )

    merge_conv = ConversationHandler(
        entry_points=[CommandHandler("merge", merge_start)],
        states={
            MERGE_COLLECTING: [
                CommandHandler("done", merge_done),
                MessageHandler(file_filter, merge_collect_file),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="merge_conversation",
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(merge_conv)
    app.add_handler(main_conv)

    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("MP3 Tools Bot starting (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
