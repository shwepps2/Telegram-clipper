# 🎬 High-Signal Telegram Clipper Bot

Automatically finds the best moments in any video using Gemini AI and sends them back as clips.

---

## ✅ Before You Deploy — 3 Things You Need

| What | Where to get it | Free? |
|------|----------------|-------|
| `TELEGRAM_TOKEN` | Telegram → @BotFather → /newbot | ✅ |
| `GEMINI_API_KEY` | aistudio.google.com → Get API Key | ✅ |
| `WEBHOOK_URL` | Your Koyeb app URL (after deploy) | ✅ |

---

## 🚀 Deploy Steps

### 1. Push this folder to GitHub
```bash
git init
git add .
git commit -m "initial commit"
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/telegram-clipper.git
git push -u origin main
```

### 2. Deploy on Koyeb (free)
1. Go to **koyeb.com** → sign up free
2. Click **+ Create Service** → GitHub
3. Connect GitHub → select this repo
4. Builder: **Dockerfile** (auto-detected)
5. Port: **8000**
6. Add environment variables:
   - `TELEGRAM_TOKEN` = your BotFather token
   - `GEMINI_API_KEY` = your Gemini key
   - `WEBHOOK_URL` = leave blank for first deploy
7. Click **Deploy** → wait 2-3 min
8. Copy your Koyeb URL (e.g. `https://your-app-abc.koyeb.app`)
9. Go back → edit env vars → set `WEBHOOK_URL` to that URL
10. **Redeploy**

---

## 💬 Bot Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Tips and limits |
| `/vibe` | Change detection style |
| `/vibe_sports` | Sports mode |
| `/vibe_podcast` | Podcast mode |
| `/vibe_gaming` | Gaming mode |
| `/vibe_education` | Educational mode |
| `/vibe_general` | Default mode |

---

## ⚠️ Known Limits

- Video files via Telegram: max **20MB**
- For bigger videos: send a **YouTube link** instead
- Gemini free tier: ~50 video requests/day
- Best for videos under **30 minutes**

---

## 🛠 Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot not responding | Check Koyeb logs for errors |
| Gemini error | Verify GEMINI_API_KEY in env vars |
| YouTube download fails | yt-dlp may need updating — edit requirements.txt |
| Clips not sending | Video may be too large, try a shorter clip |
