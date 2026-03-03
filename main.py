import os
import subprocess
import logging
import re
import time
import asyncio
from threading import Thread

import google.generativeai as genai
import yt_dlp
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters
)

# ─── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PORT = int(os.environ.get("PORT", 8000))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Health Server (keeps Render happy) ────────────────────────────────────────

health_app = Flask(__name__)

@health_app.route("/health")
def health():
    return "alive", 200

@health_app.route("/")
def home():
    return "Bot is running!", 200

def run_health_server():
    health_app.run(host="0.0.0.0", port=PORT)

Thread(target=run_health_server, daemon=True).start()

# ─── Platform Detection ────────────────────────────────────────────────────────

SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be", "instagram.com",
    "twitter.com", "x.com", "tiktok.com",
    "facebook.com", "fb.watch", "vimeo.com",
    "reddit.com", "twitch.tv", "dailymotion.com", "streamable.com",
]

def is_supported_url(text):
    t = text.lower()
    return t.startswith("http") and any(d in t for d in SUPPORTED_DOMAINS)

def detect_platform(url):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "YouTube"
    if "instagram.com" in u: return "Instagram"
    if "twitter.com" in u or "x.com" in u: return "Twitter/X"
    if "tiktok.com" in u: return "TikTok"
    if "facebook.com" in u or "fb.watch" in u: return "Facebook"
    if "vimeo.com" in u: return "Vimeo"
    if "reddit.com" in u: return "Reddit"
    if "twitch.tv" in u: return "Twitch"
    if "dailymotion.com" in u: return "Dailymotion"
    return "video"

# ─── Helpers ───────────────────────────────────────────────────────────────────

def parse_clips(text):
    clips = []
    for m in re.findall(r"CLIP\d+:\s*(\d+)-(\d+)\s*\|\s*Reason:\s*(.+)", text, re.IGNORECASE):
        s, e, r = int(m[0]), int(m[1]), m[2].strip()
        if 0 <= s < e <= s + 300:
            clips.append((s, e, r))
    return clips

def cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

# ─── Vibe Prompts ──────────────────────────────────────────────────────────────

VIBE_PROMPTS = {
    "general":   "most exciting, emotional, surprising, or valuable parts",
    "sports":    "best goals, dunks, big plays, crowd eruptions, clutch moments",
    "podcast":   "most insightful, controversial, or quotable statements",
    "gaming":    "most clutch plays, funniest moments, big kills, epic fails",
    "education": "key explanations, aha moments, and important takeaways",
}

USER_VIBES = {}

# ─── Core Analysis ─────────────────────────────────────────────────────────────

async def analyze_and_send(update, video_path, vibe="general"):
    uploaded = None
    clip_paths = []
    vibe_desc = VIBE_PROMPTS.get(vibe, VIBE_PROMPTS["general"])

    try:
        await update.message.reply_text("Uploading to Gemini AI...")
        uploaded = genai.upload_file(path=video_path, mime_type="video/mp4")

        for _ in range(60):
            if uploaded.state.name != "PROCESSING":
                break
            time.sleep(5)
            uploaded = genai.get_file(uploaded.name)

        if uploaded.state.name == "FAILED":
            raise RuntimeError("Gemini failed to process the video.")

        await update.message.reply_text("Analyzing for high-signal moments...")

        response = model.generate_content([
            uploaded,
            f"""Watch this entire video carefully from start to finish.

Find the TOP 3 HIGH-SIGNAL moments: {vibe_desc}.

Rules:
- Each clip must be 15 to 90 seconds long
- Use real timestamps in seconds from the video start
- Do not overlap clips

Reply ONLY in this exact format, nothing else:
CLIP1: 12-57 | Reason: crowd erupts after goal
CLIP2: 103-145 | Reason: emotional breakdown moment
CLIP3: 201-260 | Reason: key announcement revealed"""
        ])

        raw = response.text.strip()
        logger.info(f"Gemini response: {raw}")
        clips = parse_clips(raw)

        if not clips:
            await update.message.reply_text(
                f"Could not find timestamps in Gemini response.\n\nGemini said:\n{raw[:500]}"
            )
            return

        await update.message.reply_text(
            "Cutting clips...\n" +
            "\n".join(f"Clip {i+1}: {r} ({e-s}s)" for i, (s, e, r) in enumerate(clips))
        )

        sent = 0
        for i, (start, end, reason) in enumerate(clips[:3]):
            clip_path = f"clip_{i+1}.mp4"
            clip_paths.append(clip_path)

            res = subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-ss", str(start), "-t", str(end - start),
                "-c:v", "libx264", "-crf", "23",
                "-c:a", "aac", "-preset", "fast",
                "-movflags", "+faststart", clip_path
            ], capture_output=True, text=True)

            if res.returncode != 0:
                logger.error(f"FFmpeg error: {res.stderr[-200:]}")
                await update.message.reply_text(f"Could not cut clip {i+1} - skipping.")
                continue

            if os.path.getsize(clip_path) > 50 * 1024 * 1024:
                await update.message.reply_text(f"Clip {i+1} too large for Telegram - skipping.")
                continue

            with open(clip_path, "rb") as f:
                await update.message.reply_video(
                    f, caption=f"Clip {i+1}: {reason}", supports_streaming=True
                )
            sent += 1

        if sent:
            await update.message.reply_text(
                f"Done! Sent {sent} clip(s).\n/vibe - change mode\n/help - tips"
            )
        else:
            await update.message.reply_text("No clips could be sent. Try a shorter video.")

    except Exception as e:
        logger.error(f"analyze error: {e}")
        await update.message.reply_text(f"Something went wrong: {str(e)[:300]}")
    finally:
        cleanup(video_path, *clip_paths)
        if uploaded:
            try:
                genai.delete_file(uploaded.name)
            except:
                pass

# ─── Bot Handlers ──────────────────────────────────────────────────────────────

async def cmd_start(update, context):
    await update.message.reply_text(
        "🎬 High-Signal Clipper Bot\n\n"
        "Send a video link or upload a file!\n\n"
        "Supported: YouTube, Instagram, TikTok, Twitter/X, "
        "Facebook, Vimeo, Reddit, Twitch, Dailymotion\n\n"
        "Direct upload: max 20MB\n"
        "Links: no size limit!\n\n"
        "/vibe - set detection style\n"
        "/help - tips"
    )

async def cmd_help(update, context):
    await update.message.reply_text(
        "How to use:\n"
        "1. Send a link or upload a video\n"
        "2. Wait 1-3 min for AI analysis\n"
        "3. Get your top 3 clips!\n\n"
        "Limits:\n"
        "- Direct upload max 20MB (Telegram hard limit)\n"
        "- For bigger videos always use a link\n"
        "- Gemini free tier: ~50 videos per day\n\n"
        "Use /vibe to tune detection for your content type."
    )

async def cmd_vibe(update, context):
    chat_id = update.effective_chat.id
    current = USER_VIBES.get(chat_id, "general")
    options = ["general", "sports", "podcast", "gaming", "education"]
    lines = "\n".join(f"{'>' if v == current else ' '} /vibe_{v}" for v in options)
    await update.message.reply_text(f"Current vibe: {current}\n\n{lines}")

async def cmd_set_vibe(update, context):
    chat_id = update.effective_chat.id
    vibe = update.message.text.replace("/vibe_", "").strip().lower()
    if vibe not in VIBE_PROMPTS:
        await update.message.reply_text(f"Unknown vibe. Options: {', '.join(VIBE_PROMPTS)}")
        return
    USER_VIBES[chat_id] = vibe
    await update.message.reply_text(f"Vibe set to {vibe}!")

async def handle_video(update, context):
    chat_id = update.effective_chat.id
    if update.message.video.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "File over 20MB - Telegram will not let me download it.\n\n"
            "Solution: Upload to YouTube or TikTok (even as unlisted) "
            "and send me the link instead!"
        )
        return
    await update.message.reply_text("Downloading your video...")
    try:
        video_path = "input_video.mp4"
        tg_file = await context.bot.get_file(update.message.video.file_id)
        await tg_file.download_to_drive(video_path)
        await analyze_and_send(update, video_path, USER_VIBES.get(chat_id, "general"))
    except Exception as e:
        await update.message.reply_text(f"Download failed: {str(e)[:200]}")

async def handle_text(update, context):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not is_supported_url(text):
        await update.message.reply_text(
            "Send a video link or upload a file.\n\n"
            "Supported: YouTube, Instagram, TikTok, Twitter/X, "
            "Facebook, Vimeo, Reddit, Twitch, Dailymotion"
        )
        return

    platform = detect_platform(text)
    await update.message.reply_text(f"Downloading from {platform}...")

    video_path = "input_video.mp4"
    try:
        ydl_opts = {
            "outtmpl": video_path,
            "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["ios"]}},
            "http_headers": {
                "User-Agent": "com.google.ios.youtube/19.16.3 (iPhone14,3; U; CPU iOS 17_4 like Mac OS X)"
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([text])

        await update.message.reply_text(f"Downloaded from {platform}! Starting AI analysis...")
        await analyze_and_send(update, video_path, USER_VIBES.get(chat_id, "general"))

    except Exception as e:
        logger.error(f"{platform} error: {e}")
        await update.message.reply_text(
            f"Download from {platform} failed: {str(e)[:300]}\n\n"
            "Try a different link or upload the video directly."
        )
        cleanup(video_path)

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("vibe", cmd_vibe))
    bot_app.add_handler(CommandHandler("vibe_general", cmd_set_vibe))
    bot_app.add_handler(CommandHandler("vibe_sports", cmd_set_vibe))
    bot_app.add_handler(CommandHandler("vibe_podcast", cmd_set_vibe))
    bot_app.add_handler(CommandHandler("vibe_gaming", cmd_set_vibe))
    bot_app.add_handler(CommandHandler("vibe_education", cmd_set_vibe))
    bot_app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started with polling...")
    bot_app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
