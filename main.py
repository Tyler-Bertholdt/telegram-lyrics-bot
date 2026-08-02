import os
import re
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response, status

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
LRCLIB_API_URL = "https://lrclib.net/api"
HEADERS = {"User-Agent": "LRCLIBTelegramBot/1.0 (https://github.com/lrclib-bot)"}

app = FastAPI()


# ------------------------------------------------------------------
# Telegram API Helper Functions
# ------------------------------------------------------------------
async def reply_telegram(chat_id: int, text: str, reply_markup: dict = None):
    """Sends a text message via Telegram REST API."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)


async def send_telegram_document(
    chat_id: int, file_bytes: bytes, filename: str, caption: str
):
    """Sends a downloadable .lrc or .txt document via Telegram REST API."""
    files = {"document": (filename, file_bytes, "text/plain")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(f"{TELEGRAM_API_URL}/sendDocument", data=data, files=files)


async def answer_callback_query(callback_query_id: str):
    """Acknowledges an inline button click to dismiss the loading spinner."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{TELEGRAM_API_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id},
        )


# ------------------------------------------------------------------
# Query Parser
# ------------------------------------------------------------------
def parse_search_query(query_text: str) -> dict:
    is_text_only = bool(re.search(r"\$lyrics\b", query_text, re.IGNORECASE))
    is_plain = bool(re.search(r"\$plain\b", query_text, re.IGNORECASE))
    is_synced = bool(re.search(r"\$synced\b", query_text, re.IGNORECASE))

    lyric_type = "plain" if is_plain else "synced"

    clean_text = re.sub(
        r"\$(lyrics|synced|plain)\b", "", query_text, flags=re.IGNORECASE
    ).strip()

    artist_match = re.search(
        r'\$artist\s+["\']?([^"\'$\n]+)["\']?', clean_text, re.IGNORECASE
    )
    album_match = re.search(
        r'\$album\s+["\']?([^"\'$\n]+)["\']?', clean_text, re.IGNORECASE
    )
    duration_match = re.search(
        r'\$duration\s+["\']?([^"\'$\n]+)["\']?', clean_text, re.IGNORECASE
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

    track = re.sub(
        r'\$(artist|album|duration)\s+["\']?[^"\'$\n]+["\']?',
        "",
        clean_text,
        flags=re.IGNORECASE,
    )
    track = re.sub(
        r"^/(search|find)\b", "", track, flags=re.IGNORECASE
    ).strip().strip("\"'")

    return {
        "track_name": track if track else None,
        "artist_name": artist,
        "album_name": album,
        "duration": duration_sec,
        "is_text_only": is_text_only,
        "lyric_type": lyric_type,
    }


# ------------------------------------------------------------------
# Handlers & Search Processors
# ------------------------------------------------------------------
async def send_welcome(chat_id: int):
    welcome_text = (
        "<b>🎵 Welcome to LRCLIB Lyrics Bot!</b>\n\n"
        "Tap any template below to copy it, then edit and send:\n\n"
        "<b>1️⃣ Synced Search (Default):</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead"</code>\n\n'
        "<b>2️⃣ Full Search:</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $album "In Rainbows" $duration "04:02"</code>\n\n'
        "<b>3️⃣ Plain Lyrics Search:</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $plain</code>\n\n'
        "<b>4️⃣ Direct Chat Lyrics (No File):</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $lyrics</code>\n\n'
        "<b>💡 Commands:</b>\n"
        "/search - Search lyrics with flags\n"
        "/searchagain - Reset state\n"
        "/help - Full guide"
    )
    await reply_telegram(chat_id, welcome_text)


async def send_help(chat_id: int):
    help_text = (
        "<b>📖 LRCLIB Bot Instructions</b>\n\n"
        "<b>Available Flags:</b>\n"
        "• <code>$artist \"Name\"</code> : Target artist\n"
        "• <code>$album \"Name\"</code> : Target album\n"
        "• <code>$duration \"MM:SS\"</code> : Target duration\n"
        "• <code>$synced</code> : Filter synced lyrics (.lrc) [Default]\n"
        "• <code>$plain</code> : Filter plain lyrics\n"
        "• <code>$lyrics</code> : Direct chat output\n\n"
        "<b>Example:</b>\n"
        '<code>/search "Nude" $artist "Radiohead" $lyrics</code>'
    )
    await reply_telegram(chat_id, help_text)


async def process_search(chat_id: int, query_text: str):
    parsed = parse_search_query(query_text)

    if not parsed["track_name"] and not parsed["artist_name"]:
        await reply_telegram(
            chat_id,
            "❌ Please provide a track or query.\nExample: <code>/search \"Bodysnatchers\" $artist \"Radiohead\"</code>",
        )
        return

    params = {}
    if parsed["track_name"] and parsed["artist_name"]:
        params["track_name"] = parsed["track_name"]
        params["artist_name"] = parsed["artist_name"]
        if parsed["album_name"]:
            params["album_name"] = parsed["album_name"]
    else:
        terms = [
            t
            for t in [
                parsed["track_name"],
                parsed["artist_name"],
                parsed["album_name"],
            ]
            if t
        ]
        params["q"] = " ".join(terms)

    if parsed["duration"]:
        params["duration"] = parsed["duration"]

    async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
        resp = await client.get(f"{LRCLIB_API_URL}/search", params=params)
        results = resp.json() if resp.status_code == 200 else []

    if not results:
        await reply_telegram(chat_id, "❌ No results found on LRCLIB.")
        return

    target_type = parsed["lyric_type"]
    primary = [
        r
        for r in results
        if (
            r.get("syncedLyrics")
            if target_type == "synced"
            else r.get("plainLyrics")
        )
    ]
    secondary = [r for r in results if r not in primary]
    sorted_results = (primary + secondary)[:5]

    response_text = f"<b>🔍 Top Results ({target_type.upper()} Mode):</b>\n\n"
    keyboard = []

    mode_flag = "text" if parsed["is_text_only"] else "file"

    for idx, item in enumerate(sorted_results):
        has_synced = "🟢 Synced" if item.get("syncedLyrics") else "🔴 Plain"
        dur_val = item.get("duration", 0) or 0
        dur_fmt = (
            f"{int(dur_val) // 60}:{int(dur_val) % 60:02d}" if dur_val else "N/A"
        )

        track_title = item.get("trackName", "Unknown")
        artist_name = item.get("artistName", "Unknown")
        album_name = item.get("albumName", "Unknown")
        item_id = item.get("id")

        response_text += (
            f"<b>{idx+1}. {track_title}</b>\n"
            f"👤 {artist_name} | 💿 {album_name}\n"
            f"⏱️ {dur_fmt} | {has_synced}\n"
            f"🔗 https://lrclib.net/db/single/{item_id}\n\n"
        )

        # Encode track_id and output mode into callback_data
        keyboard.append(
            [
                {
                    "text": f"Get #{idx+1}: {track_title[:20]}",
                    "callback_data": f"dl_{item_id}_{mode_flag}",
                }
            ]
        )

    reply_markup = {"inline_keyboard": keyboard}
    await reply_telegram(chat_id, response_text, reply_markup=reply_markup)


async def process_callback(callback_data: str, chat_id: int, callback_id: str):
    await answer_callback_query(callback_id)

    parts = callback_data.split("_")
    if len(parts) < 3 or parts[0] != "dl":
        return

    track_id = parts[1]
    mode_flag = parts[2]

    # Directly fetch track lyrics by ID from LRCLIB
    async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
        resp = await client.get(f"{LRCLIB_API_URL}/get/{track_id}")
        if resp.status_code != 200:
            await reply_telegram(chat_id, "❌ Error fetching track from LRCLIB.")
            return
        item = resp.json()

    synced = item.get("syncedLyrics")
    plain = item.get("plainLyrics")

    if synced:
        lyrics_content = synced
        file_ext = "lrc"
    elif plain:
        lyrics_content = plain
        file_ext = "txt"
    else:
        await reply_telegram(chat_id, "❌ No lyrics available for this track.")
        return

    track_name = item.get("trackName", "Track")
    artist_name = item.get("artistName", "Artist")

    if mode_flag == "text":
        header = f"📝 <b>{track_name} - {artist_name}</b>\n\n"
        full_text = header + lyrics_content
        if len(full_text) > 4000:
            full_text = full_text[:3900] + "\n\n...[Truncated]"
        await reply_telegram(chat_id, full_text)
    else:
        filename = f"{track_name} - {artist_name}.{file_ext}"
        caption = f"🎵 <b>{track_name}</b> - {artist_name}"
        await send_telegram_document(
            chat_id, lyrics_content.encode("utf-8"), filename, caption
        )


# ------------------------------------------------------------------
# FastAPI App Startup & Webhook Route
# ------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    """Automatically registers Webhook with Telegram on server startup."""
    if WEBHOOK_URL and BOT_TOKEN:
        webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook"
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(
                f"{TELEGRAM_API_URL}/setWebhook",
                params={"url": webhook_endpoint},
            )


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()

        # Handle regular user text message
        if "message" in data:
            msg = data["message"]
            chat_id = msg["chat"]["id"]
            text = (msg.get("text", "") or msg.get("caption", "")).strip()

            if not text:
                return Response(status_code=status.HTTP_200_OK)

            if text.lower().startswith(("/start", "/help")):
                background_tasks.add_task(send_welcome, chat_id)
                return Response(status_code=status.HTTP_200_OK)

            if text.lower().startswith("/searchagain"):
                background_tasks.add_task(
                    reply_telegram,
                    chat_id,
                    "🔄 Search state reset! Send your next query with /search.",
                )
                return Response(status_code=status.HTTP_200_OK)

            background_tasks.add_task(process_search, chat_id, text)
            return Response(status_code=status.HTTP_200_OK)

        # Handle inline button selections
        if "callback_query" in data:
            cb = data["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            callback_id = cb["id"]
            callback_data = cb.get("data", "")

            background_tasks.add_task(
                process_callback, callback_data, chat_id, callback_id
            )
            return Response(status_code=status.HTTP_200_OK)

    except Exception as e:
        print(f"Error handling webhook: {e}")

    return Response(status_code=status.HTTP_200_OK)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "LRCLIB REST Telegram Bot"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=PORT)