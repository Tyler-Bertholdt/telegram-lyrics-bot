import io
import os
import re
from contextlib import asynccontextmanager
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response, status
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

LRCLIB_API_URL = "https://lrclib.net/api/search"

# Initialize Telegram Application
telegram_app = Application.builder().token(BOT_TOKEN).build()


def parse_search_query(query_text: str) -> dict:
    """Parses raw user input into LRCLIB query parameters and output preferences."""
    is_text_only = bool(re.search(r"\$lyrics\b", query_text, re.IGNORECASE))
    is_plain = bool(re.search(r"\$plain\b", query_text, re.IGNORECASE))
    is_synced = bool(re.search(r"\$synced\b", query_text, re.IGNORECASE))

    # Default lyric preference is synced unless explicitly specified as plain
    lyric_type = "plain" if is_plain else "synced"

    # Remove boolean flags
    clean_text = re.sub(
        r"\$(lyrics|synced|plain)\b", "", query_text, flags=re.IGNORECASE
    ).strip()

    # Extract key-value flags
    artist_match = re.search(
        r"\$artist\s+[" "']?([^"'$\n]+)[" '']?", clean_text, re.IGNORECASE
    )
    album_match = re.search(
        r"\$album\s+[" "']?([^"'$\n]+)[" '']?", clean_text, re.IGNORECASE
    )
    duration_match = re.search(
        r"\$duration\s+[" "']?([^"'$\n]+)[" '']?", clean_text, re.IGNORECASE
    )

    artist = artist_match.group(1).strip() if artist_match else None
    album = album_match.group(1).strip() if album_match else None
    duration_str = duration_match.group(1).strip() if duration_match else None

    duration_sec = None
    if duration_str:
        if ":" in duration_str:
            parts = duration_str.split(":")
            try:
                duration_sec = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                pass
        elif duration_str.isdigit():
            duration_sec = int(duration_str)

    # Clean track title by stripping command and remaining flag patterns
    track = re.sub(
        r"\$(artist|album|duration)\s+[" "']?[^"'$\n]+[" '']?",
        "",
        clean_text,
        flags=re.IGNORECASE,
    )
    track = re.sub(r"^/search\b", "", track, flags=re.IGNORECASE).strip().strip("\"'")

    return {
        "track_name": track if track else None,
        "artist_name": artist,
        "album_name": album,
        "duration": duration_sec,
        "is_text_only": is_text_only,
        "lyric_type": lyric_type,
    }


async def execute_lrclib_search(parsed_data: dict) -> list:
    """Executes search against LRCLIB API using extracted query parameters."""
    params = {}
    if parsed_data["track_name"] and parsed_data["artist_name"]:
        params["track_name"] = parsed_data["track_name"]
        params["artist_name"] = parsed_data["artist_name"]
        if parsed_data["album_name"]:
            params["album_name"] = parsed_data["album_name"]
    else:
        search_terms = [
            t
            for t in [
                parsed_data["track_name"],
                parsed_data["artist_name"],
                parsed_data["album_name"],
            ]
            if t
        ]
        params["q"] = " ".join(search_terms)

    if parsed_data["duration"]:
        params["duration"] = parsed_data["duration"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(LRCLIB_API_URL, params=params)
        if response.status_code == 200:
            return response.json()
        return []


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "<b>🎵 Welcome to LRCLIB Lyrics Bot!</b>\n\n"
        "Tap any template code block below to copy it, then fill in your song details:\n\n"
        "<b>1️⃣ Synced Search (Default):</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead"</code>\n\n'
        "<b>2️⃣ Full Detailed Search:</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $album "In Rainbows" $duration "04:02" $synced</code>\n\n'
        "<b>3️⃣ Plain Lyrics Search:</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $plain</code>\n\n'
        "<b>4️⃣ Direct Text in Chat (No File):</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $lyrics</code>\n\n'
        "<b>💡 Commands:</b>\n"
        "/search - Find lyrics with flags\n"
        "/searchagain - Reset and start new search\n"
        "/help - View full guide"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>📖 How to Use the Bot</b>\n\n"
        "<b>Available Flags:</b>\n"
        "• <code>$artist \"Name\"</code> : Filter by artist\n"
        "• <code>$album \"Name\"</code> : Filter by album\n"
        "• <code>$duration \"MM:SS\"</code> : Match track duration\n"
        "• <code>$synced</code> : Prefer synced (.lrc) lyrics [DEFAULT]\n"
        "• <code>$plain</code> : Prefer unsynced plain lyrics\n"
        "• <code>$lyrics</code> : Direct chat output (no file send)\n\n"
        "<b>Example:</b>\n"
        '<code>/search "Nude" $artist "Radiohead" $synced $lyrics</code>'
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def searchagain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🔄 Search state cleared. Send a new query with /search!"
    )


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text
    parsed = parse_search_query(raw_text)

    if not parsed["track_name"] and not parsed["artist_name"]:
        await update.message.reply_text(
            "❌ Please provide a song name or query. Example:\n<code>/search \"Bodysnatchers\" $artist \"Radiohead\"</code>",
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text("🔍 Searching LRCLIB...")
    results = await execute_lrclib_search(parsed)

    if not results:
        await msg.edit_text("❌ No results found on LRCLIB.")
        return

    target_type = parsed["lyric_type"]
    primary_results = [
        r
        for r in results
        if (
            r.get("syncedLyrics")
            if target_type == "synced"
            else r.get("plainLyrics")
        )
    ]
    secondary_results = [r for r in results if r not in primary_results]
    sorted_results = (primary_results + secondary_results)[:5]

    context.user_data["current_results"] = sorted_results
    context.user_data["is_text_only"] = parsed["is_text_only"]
    context.user_data["lyric_type"] = parsed["lyric_type"]

    response_text = f"<b>🔍 Top Results ({target_type.upper()} Mode):</b>\n\n"
    keyboard = []

    for idx, item in enumerate(sorted_results):
        has_synced = "🟢 Synced" if item.get("syncedLyrics") else "🔴 Plain"
        duration_val = item.get("duration", 0) or 0
        duration_fmt = (
            f"{int(duration_val) // 60}:{int(duration_val) % 60:02d}"
            if duration_val
            else "N/A"
        )
        track_title = item.get("trackName", "Unknown")
        artist_name = item.get("artistName", "Unknown")
        album_name = item.get("albumName", "Unknown")
        item_id = item.get("id")

        response_text += (
            f"<b>{idx+1}. {track_title}</b>\n"
            f"👤 {artist_name} | 💿 {album_name}\n"
            f"⏱️ {duration_fmt} | {has_synced}\n"
            f"🔗 https://lrclib.net/db/single/{item_id}\n\n"
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"Get #{idx+1}: {track_title[:20]}",
                    callback_data=f"select_{idx}",
                )
            ]
        )

    await msg.edit_text(
        response_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if "current_results" not in context.user_data:
        await query.message.reply_text(
            "⚠️ Session expired. Please perform search again using /search."
        )
        return

    idx = int(query.data.split("_")[1])
    results = context.user_data["current_results"]

    if idx >= len(results):
        return

    item = results[idx]
    is_text_only = context.user_data.get("is_text_only", False)
    lyric_type = context.user_data.get("lyric_type", "synced")

    if lyric_type == "synced" and item.get("syncedLyrics"):
        lyrics_content = item.get("syncedLyrics")
        file_ext = "lrc"
    elif item.get("plainLyrics"):
        lyrics_content = item.get("plainLyrics")
        file_ext = "txt"
    elif item.get("syncedLyrics"):
        lyrics_content = item.get("syncedLyrics")
        file_ext = "lrc"
    else:
        lyrics_content = None

    if not lyrics_content:
        await query.message.reply_text("❌ Selected item has no lyrics content available.")
        return

    track_name = item.get("trackName", "Track")
    artist_name = item.get("artistName", "Artist")

    if is_text_only:
        header = f"📝 <b>{track_name} - {artist_name}</b>\n\n"
        if len(header + lyrics_content) > 4000:
            lyrics_content = lyrics_content[:3900] + "\n\n...[Truncated]"
        await query.message.reply_text(f"{header}{lyrics_content}", parse_mode="HTML")
    else:
        file_bytes = io.BytesIO(lyrics_content.encode("utf-8"))
        file_name = f"{track_name} - {artist_name}.{file_ext}"
        file_bytes.name = file_name

        await query.message.reply_document(
            document=file_bytes,
            filename=file_name,
            caption=f"🎵 <b>{track_name}</b> - {artist_name}",
            parse_mode="HTML",
        )


# Register Handlers
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("searchagain", searchagain_command))
telegram_app.add_handler(CommandHandler("search", handle_search))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search)
)
telegram_app.add_handler(CallbackQueryHandler(button_callback))


# FastAPI Webhook Setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_URL:
        webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook"
        await telegram_app.bot.set_webhook(url=webhook_endpoint)

    yield

    if WEBHOOK_URL:
        await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    background_tasks.add_task(telegram_app.process_update, update)
    return Response(status_code=status.HTTP_200_OK)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "LRCLIB Telegram Bot Webhook"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=PORT)