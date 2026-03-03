import os, subprocess, logging, re, time, asyncio
from flask import Flask, request
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 8000))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)
application = Application.builder().token(TELEGRAM_TOKEN).build()

SUPPORTED_DOMAINS = ["youtube.com","youtu.be","instagram.com","twitter.com","x.com","tiktok.com","facebook.com","fb.watch","vimeo.com","reddit.com","twitch.tv","dailymotion.com","streamable.com"]

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
    return "video"

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
            try: os.remove(p)
            except: pass

VIBE_PROMPTS = {
    "general": "most exciting, emotional, surprising, or valuable parts",
    "sports": "best goals, dunks, big plays, crowd eruptions, clutch moments",
    "podcast": "most insightful, controversial, or quotable statements",
    "gaming": "most clutch plays, funniest moments, big kills, epic fails",
    "education": "key explanations, aha moments, and important takeaways",
}

async def analyze_and_send(update, video_path, vibe="general"):
    uploaded = None
    clip_paths = []
    vibe_desc = VIBE_PROMPTS.get(vibe, VIBE_PROMPTS["general"])
    try:
        await update.message.reply_text("Uploading to Gemini AI...")
        uploaded = genai.upload_file(path=video_path, mime_type="video/mp4")
        for _ in range(60):
            if uploaded.state.name != "PROCESSING": break
            time.sleep(5)
            uploaded = genai.get_file(uploaded.name)
        if uploaded.state.name == "FAILED":
            raise RuntimeError("Gemini failed to process the video.")
        await update.message.reply_text("Analyzing for high-signal moments...")
        response = model.generate_content([uploaded, f"""Watch this entire video carefully.
Find the TOP 3 HIGH-SIGNAL moments: {vibe_desc}.
Rules: each clip 15-90 seconds, real timestamps in seconds, no overlapping.
Reply ONLY in this exact format:
CLIP1: 12-57 | Reason: crowd erupts after goal
CLIP2: 103-145 | Reason: emotional moment
CLIP3: 201-260 | Reason: key announcement"""])
        raw = response.text.strip()
        clips = parse_clips(raw)
        if not clips:
            await update.message.reply_text(f"Could not parse timestamps.\nGemini said:\n{raw[:500]}")
            return
        await update.message.reply_text("Cutting clips...\n" + "\n".join(f"Clip {i+1}: {r} ({e-s}s)" for i,(s,e,r) in enumerate(clips)))
        sent = 0
        for i, (start, end, reason) in enumerate(clips[:3]):
            clip_path = f"clip_{i+1}.mp4"
            clip_paths.append(clip_path)
            res = subprocess.run(["ffmpeg","-y","-i",video_path,"-ss",str(start),"-t",str(end-start),"-c:v","libx264","-crf","23","-c:a","aac","-preset","fast","-movflags","+faststart",clip_path], capture_output=True, text=True)
            if res.returncode != 0:
                await update.message.reply_text(f"Could not cut clip {i+1} - skipping.")
                continue
            if os.path.getsize(clip_path) > 50*1024*1024:
                await update.message.reply_text(f"Clip {i+1} too large - skipping.")
                continue
            with open(clip_path, "rb") as f:
                await update.message.reply_video(f, caption=f"Clip {i+1}: {reason}", supports_streaming=True)
            sent += 1
        await update.message.reply_text(f"Done! Sent {sent} clip(s).\n/vibe - change mode\n/help - tips" if sent else "No clips could be sent.")
    except Exception as e:
        logger.error(f"analyze error: {e}")
        await update.message.reply_text(f"Something went wrong: {str(e)[:300]}")
    finally:
        cleanup(video_path, *clip_paths)
        if uploaded:
            try: genai.delete_file(uploaded.name)
            except: pass

USER_VIBES = {}

async def cmd_start(update, context):
    await update.message.reply_text("🎬 High-Signal Clipper Bot\n\nSend a video link or file!\n\nSupported: YouTube, Instagram, TikTok, Twitter/X, Facebook, Vimeo, Reddit, Twitch\n\nDirect upload: max 20MB\nLinks: no size limit!\n\n/vibe - set detection style\n/help - tips")

async def cmd_help(update, context):
    await update.message.reply_text("How to use:\n1. Send a link or upload a video\n2. Wait 1-3 min\n3. Get your top 3 clips!\n\nLimits:\n- Upload max 20MB (Telegram limit)\n- For bigger files use a link\n- Gemini free: ~50 videos/day\n\nUse /vibe to tune for your content.")

async def cmd_vibe(update, context):
    chat_id = update.effective_chat.id
    current = USER_VIBES.get(chat_id, "general")
    options = ["general","sports","podcast","gaming","education"]
    lines = "\n".join(f"{'>' if v==current else ' '} /vibe_{v}" for v in options)
    await update.message.reply_text(f"Current vibe: {current}\n\n{lines}")

async def cmd_set_vibe(update, context):
    chat_id = update.effective_chat.id
    vibe = update.message.text.replace("/vibe_","").strip().lower()
    if vibe not in VIBE_PROMPTS:
        await update.message.reply_text(f"Unknown vibe. Options: {', '.join(VIBE_PROMPTS)}")
        return
    USER_VIBES[chat_id] = vibe
    await update.message.reply_text(f"Vibe set to {vibe}!")

async def handle_video(update, context):
    chat_id = update.effective_chat.id
    if update.message.video.file_size > 20*1024*1024:
        await update.message.reply_text("File over 20MB - Telegram limit.\nUpload to YouTube/TikTok (even unlisted) and send the link instead!")
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
        await update.message.reply_text("Send a video link or upload a file.\nSupported: YouTube, Instagram, TikTok, Twitter/X, Facebook, Vimeo, Reddit, Twitch")
        return
    platform = detect_platform(text)
    await update.message.reply_text(f"Downloading from {platform}...")
    video_path = "input_video.mp4"
    try:
        with yt_dlp.YoutubeDL({"outtmpl": video_path, "format": "best[height<=720][ext=mp4]/best[height<=720]/best", "quiet": True, "no_warnings": True, "extractor_args": {"youtube": {"player_client": ["ios"]}}, "http_headers": {"User-Agent": "com.google.ios.youtube/19.16.3 (iPhone14,3; U; CPU iOS 17_4 like Mac OS X)"}}) as ydl:
            ydl.download([text])
        await update.message.reply_text(f"Downloaded from {platform}! Starting AI analysis...")
        await analyze_and_send(update, video_path, USER_VIBES.get(chat_id, "general"))
    except Exception as e:
        logger.error(f"{platform} error: {e}")
        await update.message.reply_text(f"Download from {platform} failed: {str(e)[:300]}\n\nTry a different link or upload the video directly.")
        cleanup(video_path)

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(CommandHandler("vibe", cmd_vibe))
application.add_handler(CommandHandler("vibe_general", cmd_set_vibe))
application.add_handler(CommandHandler("vibe_sports", cmd_set_vibe))
application.add_handler(CommandHandler("vibe_podcast", cmd_set_vibe))
application.add_handler(CommandHandler("vibe_gaming", cmd_set_vibe))
application.add_handler(CommandHandler("vibe_education", cmd_set_vibe))
application.add_handler(MessageHandler(filters.VIDEO, handle_video))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "ok", 200

@app.route("/health")
def health():
    return "alive", 200

async def setup_webhook():
    await application.initialize()
    if WEBHOOK_URL:
        wh = f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}"
        await application.bot.set_webhook(wh)
        logger.info(f"Webhook set: {wh}")

if __name__ == "__main__":
    asyncio.run(setup_webhook())
    app.run(host="0.0.0.0", port=PORT)
