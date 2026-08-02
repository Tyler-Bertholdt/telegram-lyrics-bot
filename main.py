import io
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
# LRC Time Shift & Formatting Engine
# ------------------------------------------------------------------
def shift_lrc_timestamp(match, shift_ms: int) -> str:
    """Adjusts a single [mm:ss.xx] timestamp by shift_ms milliseconds."""
    m = int(match.group(1))
    s = int(match.group(2))
    frac_str = match.group(3)

    ms = int(frac_str) * 10 if len(frac_str) == 2 else int(frac_str)
    total_ms = (m * 60 + s) * 1000 + ms + shift_ms

    if total_ms < 0:
        total_ms = 0

    new_m = total_ms // 60000
    rem_ms = total_ms % 60000
    new_s = rem_ms // 1000
    new_frac = (rem_ms % 1000) // 10

    return f"[{new_m:02d}:{new_s:02d}.{new_frac:02d}]"


def apply_time_shift(lrc_text: str, shift_ms: int) -> str:
    """Finds all LRC timestamps in text and applies shift_ms offset."""
    pattern = r"\[(\d{1,2}):(\d{2})\.(\d{2,3})\]"
    return re.sub(pattern, lambda m: shift_lrc_timestamp(m, shift_ms), lrc_text)


def parse_shift_offset(text_arg: str) -> int:
    """Parses offsets like '-300', '-300ms', '+500', '-0.3s', '+1.5s' into integer ms."""
    text_arg = text_arg.strip().lower()

    if text_arg.endswith("s") and not text_arg.endswith("ms"):
        try:
            return int(float(text_arg[:-1]) * 1000)
        except ValueError:
            return 0

    text_arg = text_arg.replace("ms", "")
    try:
        return int(float(text_arg))
    except ValueError:
        return 0


def extract_lyrics_parts(raw_text: str):
    """
    Strips old headers and HTML tags to cleanly extract (title, lyrics_body).
    Prevents header stacking!
    """
    clean_text = re.sub(r"</?(code|pre|b|i|a)[^>]*>", "", raw_text).strip()
    lines = clean_text.split("\n")

    title = ""
    body_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("📝"):
            title = stripped.replace("📝", "").strip()
        elif stripped.startswith("⏱️") or "Adjusted Timestamps" in stripped:
            continue  # Discard old timestamp shift header lines
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return title, body


def format_lyrics_message(title: str, lyrics_body: str, shift_status: str = None) -> str:
    """
    Formats the message so the lyrics are enclosed in a <code> block.
    Tapping the lyrics in Telegram copies ONLY the lyrics to clipboard!
    """
    parts = []
    if title:
        parts.append(f"📝 <b>{title}</b>")
    if shift_status:
        parts.append(f"⏱️ <i>{shift_status}</i>")

    # Ensure clean body inside <code> tag
    clean_body = re.sub(r"</?(code|pre|b|i|a)[^>]*>", "", lyrics_body).strip()

    if len(clean_body) > 3800:
        clean_body = clean_body[:3700] + "\n\n...[Truncated]"

    parts.append(f"<code>{clean_body}</code>")
    return "\n\n".join(parts)


# ------------------------------------------------------------------
# Telegram API Helper Functions
# ------------------------------------------------------------------
async def reply_telegram(chat_id: int, text: str, reply_markup: dict = None):
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


async def edit_telegram_message(
    chat_id: int, message_id: int, text: str, reply_markup: dict = None
):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{TELEGRAM_API_URL}/editMessageText", json=payload)


async def send_telegram_document(
    chat_id: int, file_bytes: bytes, filename: str, caption: str
):
    files = {"document": (filename, file_bytes, "text/plain")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(f"{TELEGRAM_API_URL}/sendDocument", data=data, files=files)


async def answer_callback_query(callback_query_id: str, text: str = ""):
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{TELEGRAM_API_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
        )


# ------------------------------------------------------------------
# Query Parser
# ------------------------------------------------------------------
def parse_search_query(query_text: str) -> dict:
    is_file = bool(re.search(r"\$file\b", query_text, re.IGNORECASE))
    is_plain = bool(re.search(r"\$plain\b", query_text, re.IGNORECASE))
    is_synced = bool(re.search(r"\$synced\b", query_text, re.IGNORECASE))

    lyric_type = "plain" if is_plain else "synced"
    is_text_only = not is_file  # Default is text in chat

    clean_text = re.sub(
        r"\$(file|lyrics|synced|plain)\b", "", query_text, flags=re.IGNORECASE
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


def build_editor_keyboard() -> dict:
    """Builds interactive inline shift & conversion buttons."""
    return {
        "inline_keyboard": [
            [
                {"text": "⏪ -500ms", "callback_data": "shift_-500"},
                {"text": "◀️ -100ms", "callback_data": "shift_-100"},
                {"text": "▶️ +100ms", "callback_data": "shift_+100"},
                {"text": "⏩ +500ms", "callback_data": "shift_+500"},
            ],
            [
                {"text": "📁 Get as .lrc File", "callback_data": "convert_file"},
                {
                    "text": "✏️ Custom Shift (Reply /shift)",
                    "callback_data": "shift_help",
                },
            ],
        ]
    }


# ------------------------------------------------------------------
# Handlers & Processors
# ------------------------------------------------------------------
async def send_welcome(chat_id: int):
    welcome_text = (
        "<b>🎵 Welcome to LRCLIB Lyrics Bot!</b>\n\n"
        "Tap any template below to copy it, then edit and send:\n\n"
        "<b>1️⃣ Default Search (In Chat Text):</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead"</code>\n\n'
        "<b>2️⃣ Downloadable .lrc File Mode:</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $file</code>\n\n'
        "<b>3️⃣ Full Search with Duration:</b>\n"
        '<code>/search "Bodysnatchers" $artist "Radiohead" $album "In Rainbows" $duration "04:02"</code>\n\n'
        "<b>🎛️ One-Tap Copy & Time Editor:</b>\n"
        "• Tap lyrics text once to copy <b>only</b> lyrics to clipboard!\n"
        "• Tap ⏪ / ⏩ buttons or reply to any message with <code>/shift -300</code> to edit!"
    )
    await reply_telegram(chat_id, welcome_text)


async def handle_shift_command(chat_id: int, msg: dict):
    """Handles /shift command when replying to any previous lyrics message."""
    text = (msg.get("text", "") or msg.get("caption", "")).strip()

    parts = text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await reply_telegram(
            chat_id,
            "ℹ️ <b>Usage:</b> Reply to any previous lyrics message with:\n"
            "<code>/shift -300</code> (300ms earlier)\n"
            "<code>/shift +500ms</code> (500ms later)\n"
            "<code>/shift -0.5s</code> (0.5s earlier)",
        )
        return

    shift_ms = parse_shift_offset(parts[1])
    if shift_ms == 0:
        await reply_telegram(
            chat_id,
            "⚠️ Invalid shift amount. Example: <code>/shift -300</code> or <code>/shift +500ms</code>",
        )
        return

    reply_to = msg.get("reply_to_message")
    if not reply_to:
        await reply_telegram(
            chat_id, "⚠️ Please reply directly to a lyrics message or document!"
        )
        return

    target_text = reply_to.get("text", "") or reply_to.get("caption", "")
    if not target_text or "[" not in target_text:
        await reply_telegram(
            chat_id, "❌ No timestamped lyrics found in the replied message."
        )
        return

    title, body = extract_lyrics_parts(target_text)
    shifted_body = apply_time_shift(body, shift_ms)
    sign = "+" if shift_ms > 0 else ""

    formatted_msg = format_lyrics_message(
        title, shifted_body, f"Adjusted by {sign}{shift_ms}ms"
    )

    await reply_telegram(
        chat_id, formatted_msg, reply_markup=build_editor_keyboard()
    )


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


async def process_callback(cb: dict):
    callback_id = cb["id"]
    callback_data = cb.get("data", "")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    # Handle Conversion to .lrc File Button
    if callback_data == "convert_file":
        current_text = message.get("text", "")
        if current_text:
            title, body = extract_lyrics_parts(current_text)
            filename = f"{title}.lrc" if title else "lyrics.lrc"

            await send_telegram_document(
                chat_id,
                body.strip().encode("utf-8"),
                filename,
                f"🎵 <b>{title if title else 'Lyrics'}</b>",
            )
            await answer_callback_query(callback_id, "Sent as .lrc file!")
        else:
            await answer_callback_query(callback_id, "No content to convert.")
        return

    # Handle Time Shift Button Clicks directly on THAT message
    if callback_data.startswith("shift_"):
        shift_action = callback_data.replace("shift_", "")
        if shift_action == "help":
            await answer_callback_query(
                callback_id,
                "Reply to this message with /shift -300 or /shift +500 to adjust!",
            )
            return

        shift_ms = parse_shift_offset(shift_action)
        current_text = message.get("text", "")

        if current_text and "[" in current_text:
            title, body = extract_lyrics_parts(current_text)
            shifted_body = apply_time_shift(body, shift_ms)
            sign = "+" if shift_ms > 0 else ""

            formatted_msg = format_lyrics_message(
                title, shifted_body, f"Adjusted by {sign}{shift_ms}ms"
            )

            await edit_telegram_message(
                chat_id, message_id, formatted_msg, reply_markup=build_editor_keyboard()
            )
            await answer_callback_query(callback_id, f"Adjusted {sign}{shift_ms}ms")
        else:
            await answer_callback_query(
                callback_id, "No timestamps found to shift."
            )
        return

    # Handle Initial Download/Selection Buttons
    await answer_callback_query(callback_id)
    parts = callback_data.split("_")
    if len(parts) < 3 or parts[0] != "dl":
        return

    track_id = parts[1]
    mode_flag = parts[2]

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
    title = f"{track_name} - {artist_name}"

    if mode_flag == "text":
        formatted_msg = format_lyrics_message(title, lyrics_content)
        await reply_telegram(
            chat_id, formatted_msg, reply_markup=build_editor_keyboard()
        )
    else:
        filename = f"{title}.{file_ext}"
        caption = f"🎵 <b>{title}</b>"
        await send_telegram_document(
            chat_id, lyrics_content.encode("utf-8"), filename, caption
        )


# ------------------------------------------------------------------
# FastAPI Webhook Router
# ------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
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

        if "message" in data:
            msg = data["message"]
            chat_id = msg["chat"]["id"]
            text = (msg.get("text", "") or msg.get("caption", "")).strip()

            if not text:
                return Response(status_code=status.HTTP_200_OK)

            if text.lower().startswith(("/start", "/help")):
                background_tasks.add_task(send_welcome, chat_id)
                return Response(status_code=status.HTTP_200_OK)

            if text.lower().startswith("/shift"):
                background_tasks.add_task(handle_shift_command, chat_id, msg)
                return Response(status_code=status.HTTP_200_OK)

            background_tasks.add_task(process_search, chat_id, text)
            return Response(status_code=status.HTTP_200_OK)

        if "callback_query" in data:
            cb = data["callback_query"]
            background_tasks.add_task(process_callback, cb)
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