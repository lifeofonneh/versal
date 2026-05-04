# Versal Digital Solutions — Lean Lead Machine v3

Scrapes Reddit, Facebook Groups, TikTok, Google Reviews, and TrustPilot for
restaurant owners in the USA, UK, and Canada who need social media help.
Qualified leads are delivered to Slack (copy-paste ready), Make.com, and Supabase.

---

## Setup

### 1. Clone & install
```bash
pip install -r requirements.txt
```

### 2. Add your secrets
```bash
cp .env.example .env
# Open .env and fill in your real API keys
```

### 3. Run in test mode (no Apify credits used)
```bash
python versal_pipeline.py
```
Test mode is on by default (`test_mode=True` at the bottom of the file).
Change to `test_mode=False` for a live run.

---

## Deploying to Render

1. Push this repo to GitHub (**never commit `.env`** — it's in `.gitignore`)
2. Create a new **Background Worker** on [render.com](https://render.com)
3. Set all environment variables from `.env.example` in the Render **Environment** tab
4. Set the start command: `python versal_pipeline.py`
5. Add a Render Cron Job to run 3×/week

---

## Environment Variables

| Variable | Where to get it |
|---|---|
| `APIFY_TOKEN_1` … `_7` | [apify.com](https://console.apify.com/account/integrations) |
| `SUPABASE_URL` | Supabase project → Settings → API |
| `SUPABASE_KEY` | Supabase project → Settings → API → service_role key |
| `MAKE_WEBHOOK_URL` | Make.com scenario → webhook trigger → copy URL |
| `SLACK_WEBHOOK_URL` | Slack → Apps → Incoming Webhooks |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com/app/apikey) |

---

## Estimated Monthly Cost (Apify credits)

| Platform | Frequency | ~Credits | ~Cost |
|---|---|---|---|
| Facebook Groups | 3×/week | 360K | $36 |
| Reddit | 3×/week | 180K | $18 |
| TikTok | 3×/week | 120K | $12 |
| Google Reviews | 3×/week | 120K | $12 |
| TrustPilot | 1×/week | 60K | $6 |
| **Total** | | **~840K** | **~$84** |

> **Tip:** Start with Facebook + Reddit only (~$54/month) to validate before enabling the rest.
