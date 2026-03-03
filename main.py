import os
import subprocess
import logging
import re
import time
import asyncio
from flask import Flask, request
import google.generativeai as genai
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
import yt_dlp

# ─── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT           = int(os.environ.get("PORT", 8000))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app         = Flask(__name__)
application = Application.builder().token(TELEGRAM_TOKEN).build()

# ─── Helpers ───────────────────────────────────────────────────────────────────

def parse_clips(text: str) -> list[tuple[int, int, str]]:
    """Extract (start_sec, end_sec, reason) from Gemini's response."""
    clips   = []
    pattern = r"CLIP\d+:\s*(\d+)-(\d+)\s*\|\s*Reason:\s*(.+)"
    for match in re.findall(pattern, text, re.IGNORECASE):
        start, end, reason = int(match[0]), int(match[1]), match[2].strip()
        if 0 <= start < end and (end - start) <= 300:
            clips.append((start, end, reason))
    return clips


def cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


async def analyze_and_send(update: Update, video_path: str, vibe: str = "general"):
    """Core logic: upload → Gemini analysis → FFmpeg clips → send."""
    chat_id     = update.effective_chat.id
    uploaded    = None
    clip_paths  = []

    VIBE_PROMPTS = {
        "general":    "most exciting, emotional, surprising, or valuable parts",
        "sports":     "best goals, dunks, big plays, crowd eruptions, clutch moments",
        "podcast":    "most insightful, controversial, or quotable statements",
        "gaming":     "most clutch plays, funniest moments, big kills, epic fails",
        "education":  "key explanations, 'aha moments', and important takeaways",
    }
    vibe_desc = VIBE_PROMPTS.get(vibe, VIBE_PROMPTS["general"])

    try:
        await update.message.reply_text("🤖 Uploading video to Gemini AI...")

        uploaded = genai.upload_file(path=video_path, mime_type="video/mp4")

        # Wait for Gemini to process the file
        for _ in range(60):                          # max 5 min wait
            if uploaded.state.name != "PROCESSING":
                break
            time.sleep(5)
            uploaded = genai.get_file(uploaded.name)

        if uploaded.state.name == "FAILED":
            raise RuntimeError("Gemini failed to process the video file.")

        await update.message.reply_text("🔍 Analyzing for high-signal moments...")

        response = model.generate_content([
            uploaded,
            f"""Watch this entire video carefully from start to finish.

Find the TOP 3 HIGH-SIGNAL moments — specifically: {vibe_desc}.

Rules:
- Each clip should be 15–90 seconds long
- Use real timestamps from the actual video (in seconds from the start)
- Don't overlap clips

Reply ONLY in this exact format — nothing else, no extra text:
CLIP1: 12-57 | Reason: crowd erupts after surprise goal
CLIP2: 103-145 | Reason: emotional breakdown caught on camera
CLIP3: 201-260 | Reason: key product announcement moment"""
        ])

        raw = response.text.strip()
        logger.info(f"Gemini response:\n{raw}")

        clips = parse_clips(raw)

        if not clips:
            await update.message.reply_text(
                f"⚠️ Gemini responded but I couldn't parse timestamps.\n\n"
                f"Raw response:\n{raw[:600]}\n\n"
                f"Try a different video or use /vibe to change mode."
            )
            return

        await update.message.reply_text(
            f"✂️ Found {len(clips)} moments! Cutting clips now...\n\n"
            + "\n".join(f"• Clip {i+1}: {r} ({e-s}s)" for i, (s, e, r) in enumerate(clips))
        )

        sent = 0
        for i, (start, end, reason) in enumerate(clips[:3]):
            clip_path = f"clip_{i+1}.mp4"
            clip_paths.append(clip_path)
            duration  = end - start

            result = subprocess.run([
                "ffmpeg", "-y",
                "-i", video_path,
                "-ss", str(start),
                "-t",  str(duration),
                "-c:v", "libx264", "-crf", "23",
                "-c:a", "aac",
                "-preset", "fast",
                "-movflags", "+faststart",
                clip_path
            ], capture_output=True, text=True)

            if result.returncode != 0:
                logger.error(f"FFmpeg error clip {i+1}: {result.stderr[-300:]}")
                await update.message.reply_text(f"⚠️ Couldn't cut clip {i+1} — skipping.")
                continue

            size_mb = os.path.getsize(clip_path) / 1024 / 1024
            if size_mb > 50:
                await update.message.reply_text(
                    f"⚠️ Clip {i+1} is {size_mb:.1f}MB — too large for Telegram (50MB limit). Skipping."
                )
                continue

            with open(clip_path, "rb") as f:
                await update.message.reply_video(
                    f,
                    caption=f"🔥 Clip {i+1}: {reason}",
                    supports_streaming=True,
                )
            sent += 1

        if sent:
            await update.message.reply_text(
                f"✅ Done! Sent {sent} clip(s).\n\n"
                f"📌 Commands:\n"
                f"/vibe — change detection mode (sports, podcast, gaming, education)\n"
                f"/help — usage tips"
            )
        else:
            await update.message.reply_text("❌ No clips could be sent. Try a shorter or smaller video.")

    except Exception as e:
        logger.error(f"analyze_and_send error: {e}")
        await update.message.reply_text(f"❌ Something went wrong: {str(e)[:300]}")

    finally:
        cleanup(video_path, *clip_paths)
        if uploaded:
            try:
                genai.delete_file(uploaded.name)
            except Exception:
                pass


# ─── Bot Handlers ──────────────────────────────────────────────────────────────

USER_VIBES: dict[int, str] = {}   # chat_id → vibe


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *High-Signal Clipper Bot*\n\n"
        "Send me:\n"
        "• A *video file* (max 20MB via Telegram)\n"
        "• A *YouTube link*\n\n"
        "I'll use Gemini AI to find the best moments and send them back as clips.\n\n"
        "Commands:\n"
        "/vibe — set detection style\n"
        "/help — tips & limits",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use:*\n\n"
        "1️⃣ Send a YouTube link or upload a video\n"
        "2️⃣ Wait 1–3 minutes for analysis\n"
        "3️⃣ Receive your top 3 clips!\n\n"
        "⚠️ *Limits:*\n"
        "• Video files: max 20MB (Telegram limit)\n"
        "• YouTube videos: max ~30 min recommended\n"
        "• Gemini free tier: ~50 videos/day\n\n"
        "💡 *Tip:* Use /vibe to tune detection for your content type.",
        parse_mode="Markdown"
    )


async def cmd_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id     = update.effective_chat.id
    current     = USER_VIBES.get(chat_id, "general")
    options     = ["general", "sports", "podcast", "gaming", "education"]
    options_str = "\n".join(
        f"{'→' if v == current else '  '} /vibe_{v}" for v in options
    )
    await update.message.reply_text(
        f"🎯 *Current vibe:* `{current}`\n\n"
        f"Choose a new vibe:\n{options_str}",
        parse_mode="Markdown"
    )


async def cmd_set_vibe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    vibe    = update.message.text.lstrip("/vibe_").strip().lower()
    valid   = ["general", "sports", "podcast", "gaming", "education"]
    if vibe not in valid:
        await update.message.reply_text(f"Unknown vibe. Choose: {', '.join(valid)}")
        return
    USER_VIBES[chat_id] = vibe
    await update.message.reply_text(f"✅ Vibe set to *{vibe}*! Send a video to try it.", parse_mode="Markdown")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if update.message.video.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "⚠️ File is over 20MB — Telegram won't let me download it.\n"
            "Options:\n"
            "• Compress the video first\n"
            "• Upload to YouTube and send the link\n"
            "• Use a tool like HandBrake to reduce size"
        )
        return

    await update.message.reply_text("📥 Downloading your video...")

    try:
        video_path = "input_video.mp4"
        tg_file    = await context.bot.get_file(update.message.video.file_id)
        await tg_file.download_to_drive(video_path)
        vibe = USER_VIBES.get(chat_id, "general")
        await analyze_and_send(update, video_path, vibe)
    except Exception as e:
        logger.error(f"handle_video error: {e}")
        await update.message.reply_text(f"❌ Download failed: {str(e)[:200]}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not ("youtube.com" in text or "youtu.be" in text):
        await update.message.reply_text(
            "I only understand YouTube links right now.\n"
            "Send a link or upload a video file directly."
        )
        return

    await update.message.reply_text("⬇️ Downloading from YouTube... (this can take a minute)")

    video_path = "input_video.mp4"
    try:
        ydl_opts = {
            "outtmpl":      video_path,
            "format":       "best[height<=720][ext=mp4]/best[height<=720]/best",
            "quiet":        True,
            "no_warnings":  True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([text])

        await update.message.reply_text("✅ Downloaded! Starting AI analysis...")
        vibe = USER_VIBES.get(chat_id, "general")
        await analyze_and_send(update, video_path, vibe)

    except Exception as e:
        logger.error(f"YouTube error: {e}")
        await update.message.reply_text(
            f"❌ YouTube download failed: {str(e)[:300]}\n\n"
            "Make sure the video is public and not age-restricted."
        )
        cleanup(video_path)


# ─── Register Handlers ─────────────────────────────────────────────────────────

application.add_handler(CommandHandler("start",          cmd_start))
application.add_handler(CommandHandler("help",           cmd_help))
application.add_handler(CommandHandler("vibe",           cmd_vibe))
application.add_handler(CommandHandler("vibe_general",   cmd_set_vibe))
application.add_handler(CommandHandler("vibe_sports",    cmd_set_vibe))
application.add_handler(CommandHandler("vibe_podcast",   cmd_set_vibe))
application.add_handler(CommandHandler("vibe_gaming",    cmd_set_vibe))
application.add_handler(CommandHandler("vibe_education", cmd_set_vibe))
application.add_handler(MessageHandler(filters.VIDEO,                   handle_video))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ─── Flask Webhook ─────────────────────────────────────────────────────────────

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data   = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.run(application.process_update(update))
    return "ok", 200


@app.route("/health")
def health():
    return "alive", 200


# ─── Entry Point ───────────────────────────────────────────────────────────────

async def setup_webhook():
    await application.initialize()
    if WEBHOOK_URL:
        wh = f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}"
        await application.bot.set_webhook(wh)
        logger.info(f"Webhook set → {wh}")
    else:
        logger.warning("WEBHOOK_URL not set — webhook not registered with Telegram yet.")


if __name__ == "__main__":
    asyncio.run(setup_webhook())
    app.run(host="0.0.0.0", port=PORT)
