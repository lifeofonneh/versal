"""
Versal Digital Solutions — Lean Lead Machine v3
Platforms: Reddit, Facebook Groups, TikTok, Google Reviews, TrustPilot
Markets:   USA · UK · Canada
Leads → Slack (copy-paste ready) + Supabase + Make.com
"""

import asyncio, json, httpx, os, time, logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CREDENTIALS
# ═══════════════════════════════════════════════════════════
def _load_apify_tokens() -> list[str]:
    env_keys = [os.getenv(f"APIFY_TOKEN_{i}") for i in range(1, 8)]
    env_keys = [k for k in env_keys if k]
    if env_keys:
        return env_keys
    raise ValueError("No Apify tokens found. Set APIFY_TOKEN_1 ... APIFY_TOKEN_N as env vars.")

APIFY_TOKENS = _load_apify_tokens()
_apify_index = 0

def get_apify_token() -> str:
    global _apify_index
    token = APIFY_TOKENS[_apify_index % len(APIFY_TOKENS)]
    _apify_index += 1
    return token

def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise ValueError(f"Missing required environment variable: {name}")
    return val

SUPABASE_URL      = _require_env("SUPABASE_URL")
SUPABASE_KEY      = _require_env("SUPABASE_KEY")
MAKE_WEBHOOK_URL  = _require_env("MAKE_WEBHOOK_URL")
SLACK_WEBHOOK_URL = _require_env("SLACK_WEBHOOK_URL")
GEMINI_API_KEY    = _require_env("GEMINI_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ═══════════════════════════════════════════════════════════
# AGENCY CONTEXT
# ═══════════════════════════════════════════════════════════
AGENCY_CONTEXT = """
Agency: Versal Digital Solutions (versaldigitalsolutions.com)
Markets: USA, UK, Canada
Service: Done-for-you short-form video content management for restaurants
Platforms managed: TikTok, Instagram Reels, YouTube Shorts
Results: 3,400% reach increase, 1M+ views guaranteed, new customers within 90 days
Target clients: Pizza shops, burger joints, cafés, dessert bars, casual dining
Core promise: You just cook. We handle everything — filming plan, editing, posting,
              captions, hashtags, scheduling.
Free offer: Free mini-audit + 15-min strategy call (no commitment, 2 minutes to apply)
Tone: Peer-to-peer, genuinely helpful, NEVER salesy. Lead with a tip. End with the audit offer.
"""

# ═══════════════════════════════════════════════════════════
# TARGET LISTS
# ═══════════════════════════════════════════════════════════
KEYWORDS = [
    "staffing", "food costs", "hiring", "slow season", "losing money",
    "bad reviews", "social media help", "marketing help", "no customers",
    "need help", "going under", "about to close", "struggling", "restaurant owner",
    "views", "tiktok", "instagram", "content", "cant get customers",
    "delivery apps taking", "foot traffic", "empty tables", "reach", "visibility",
    "nobody sees", "algorithm", "reel", "short video", "grubhub", "uber eats",
    "doordash", "just eat", "skip the dishes",
]

# Facebook — verified public groups (manually curated, all confirmed open/public)
# To add more: find group on Facebook, check it's Public, paste URL here
FACEBOOK_GROUPS = [
    "https://www.facebook.com/groups/restaurantownersuk/",        # 246k members, public ✅
    "https://www.facebook.com/groups/ukrestaurantowners/",        # UK focused, public ✅
    "https://www.facebook.com/groups/hospitalityuk/",             # UK hospitality, public ✅
    "https://www.facebook.com/groups/foodserviceprofessionals/",  # USA focused, public ✅
    "https://www.facebook.com/groups/canadianrestaurantowners/",  # Canada focused, public ✅
    "https://www.facebook.com/groups/torontofoodbusiness/",       # Canada/Toronto, public ✅
]

# Reddit — seed terms for dynamic subreddit discovery
REDDIT_SEED_TERMS = [
    "restaurant owner",
    "cafe owner",
    "food business",
    "hospitality UK",
    "restaurant business Canada",
]

# TikTok hashtags
TIKTOK_HASHTAGS = [
    "restaurantowner", "smallrestaurant", "restaurantlife",
    "cafeowner", "foodbusiness", "restauranttok",
]
TIKTOK_MAX_VIEWS = 500

# Instagram hashtags
INSTAGRAM_HASHTAGS = [
    "restaurantowner", "cafeowner", "smallrestaurant",
    "restaurantlife", "foodbusiness", "pizzarestaurant", "burgerrestaurant",
]
INSTAGRAM_MAX_VIEWS    = 100
INSTAGRAM_MAX_AGE_DAYS = 3

# TrustPilot
TRUSTPILOT_CATEGORIES = [
    "https://uk.trustpilot.com/categories/restaurants_bars",
    "https://www.trustpilot.com/categories/restaurants_bars",
]

# Google Maps
GOOGLE_MAPS_QUERIES = [
    "pizza restaurant London", "burger restaurant Manchester", "cafe Birmingham",
    "pizza restaurant New York", "burger restaurant Los Angeles",
    "cafe Toronto", "pizza restaurant Vancouver",
]

# Yelp (add specific restaurant URLs to activate)
YELP_RESTAURANTS: list[str] = []

# ═══════════════════════════════════════════════════════════
# ACTIVE PLATFORMS
# ═══════════════════════════════════════════════════════════
ACTIVE_PLATFORMS = {
    "facebook":   True,
    "reddit":     True,
    "tiktok":     True,
    "instagram":  False,
    "google":     True,
    "trustpilot": False,
    "yelp":       True,
}

# ═══════════════════════════════════════════════════════════
# PYDANTIC MODEL
# ═══════════════════════════════════════════════════════════
class LeadOutput(BaseModel):
    source_platform: str
    source_url: str
    raw_text: str
    intent_score: int = Field(ge=0, le=100)
    is_restaurant_owner: bool
    problem_category: str
    pain_point_summary: str
    drafted_response: str
    free_resource_offered: str
    market: str
    tiktok_view_count: Optional[int] = None
    processed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    passed_threshold: bool

# ═══════════════════════════════════════════════════════════
# GEMINI AI ENGINE
# ═══════════════════════════════════════════════════════════
def ask_gemini(prompt: str) -> dict:
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=900),
        )
        text = response.text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return {}

def score_intent(text: str, platform: str) -> dict:
    return ask_gemini(f"""You are an expert analyst for Versal Digital Solutions,
a done-for-you short-form content agency for restaurants in the USA, UK, and Canada.

Analyse this {platform} post/comment/video. Return ONLY valid JSON.

Is the author a restaurant OWNER/OPERATOR (not a diner, not staff)?
Intent score 0-100 — urgency of need for social media / marketing / visibility help.

Detect market: "USA", "UK", "Canada", or "Unknown".

Return ONLY: {{"is_restaurant_owner": true/false, "intent_score": 0-100,
"market": "USA|UK|Canada|Unknown", "reasoning": "one sentence"}}

Text: {text[:1200]}""")

def classify_problem(text: str) -> dict:
    return ask_gemini(f"""UK/USA/Canada restaurant business consultant.
Classify this restaurant owner's primary problem. Return ONLY valid JSON.

Categories: Social Media/Visibility, Profitability/Margins, Labor/Hiring,
Tech/POS, Reputation/Reviews, Foot Traffic, Delivery App Dependency, Other

Return ONLY: {{"problem_category": "category",
"pain_point_summary": "one sentence max 15 words"}}

Text: {text[:800]}""")

def draft_response(text: str, category: str, pain_point: str,
                   platform: str, market: str,
                   tiktok_views: Optional[int] = None) -> dict:
    market_note = {
        "UK":     "Use British spelling. Reference UK platforms (Just Eat, Deliveroo) where relevant.",
        "USA":    "Use American spelling. Reference US platforms (DoorDash, Grubhub, Uber Eats) where relevant.",
        "Canada": "Use Canadian context. Reference Skip The Dishes / DoorDash Canada where relevant.",
    }.get(market, "")

    tiktok_note = ""
    if platform == "tiktok" and tiktok_views is not None:
        tiktok_note = f"\nThis is a TikTok video with only {tiktok_views} views."

    platform_tone = {
        "reddit":     "casual Reddit reply, peer-to-peer, no formality",
        "facebook":   "friendly Facebook comment, warm and helpful",
        "tiktok":     "short friendly TikTok comment (max 2 sentences), then DM offer",
        "yelp":       "empathetic cold outreach email to a restaurant owner",
        "trustpilot": "empathetic cold outreach email to a struggling restaurant owner",
        "google":     "empathetic cold outreach email referencing their Google presence",
    }.get(platform, "friendly helpful message")

    return ask_gemini(f"""You represent Versal Digital Solutions.

{AGENCY_CONTEXT}
{market_note}{tiktok_note}

Write a {platform_tone} reply to a restaurant owner struggling with: {pain_point}

Rules:
- Max 3 sentences. Sound like a helpful peer, NOT an agency.
- Lead with ONE specific actionable tip related to their exact problem.
- End naturally by offering Versal's free mini-audit (no commitment, 2 mins to apply).
- Never mention pricing. Never be pushy.

Return ONLY: {{"drafted_response": "the reply text",
"free_resource_offered": "Free Versal Mini-Audit + 15-min Strategy Call"}}

Their post: {text[:400]}""")

def process_single_lead(text: str, platform: str, url: str,
                        threshold: int = 75,
                        tiktok_views: Optional[int] = None) -> Optional[LeadOutput]:
    intent = score_intent(text, platform)
    if not intent: return None

    score    = intent.get("intent_score", 0)
    is_owner = intent.get("is_restaurant_owner", False)
    market   = intent.get("market", "Unknown")
    logger.info(f"  Score:{score} | Owner:{is_owner} | Market:{market} | {intent.get('reasoning','')[:80]}")

    if not is_owner or score < threshold:
        return None

    classification = classify_problem(text)
    if not classification: return None
    category   = classification.get("problem_category", "Other")
    pain_point = classification.get("pain_point_summary", "")

    draft = draft_response(text, category, pain_point, platform, market, tiktok_views)
    if not draft: return None

    return LeadOutput(
        source_platform=platform, source_url=url, raw_text=text,
        intent_score=score, is_restaurant_owner=is_owner,
        problem_category=category, pain_point_summary=pain_point,
        drafted_response=draft.get("drafted_response", ""),
        free_resource_offered=draft.get("free_resource_offered", "Free Versal Mini-Audit"),
        market=market, tiktok_view_count=tiktok_views, passed_threshold=True,
    )

# ═══════════════════════════════════════════════════════════
# DEDUP
# ═══════════════════════════════════════════════════════════
async def load_seen_urls() -> set[str]:
    try:
        async with httpx.AsyncClient() as client:
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            seen, offset = set(), 0
            while True:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/leads",
                    headers=headers,
                    params={"select": "source_url", "limit": 1000, "offset": offset},
                    timeout=15,
                )
                rows = r.json()
                if not rows:
                    break
                seen.update(row["source_url"] for row in rows if row.get("source_url"))
                if len(rows) < 1000:
                    break
                offset += 1000
            logger.info(f"Dedup: loaded {len(seen)} previously seen URLs")
            return seen
    except Exception as e:
        logger.error(f"Dedup load error: {e}")
        return set()

def is_new(url: str, seen: set[str]) -> bool:
    if not url or url in seen:
        return False
    seen.add(url)
    return True

# ═══════════════════════════════════════════════════════════
# LAST RUN DATE
# ═══════════════════════════════════════════════════════════
async def get_last_run_date() -> str:
    fallback = (datetime.now(timezone.utc) - timedelta(days=50)).strftime("%Y-%m-%d")
    try:
        async with httpx.AsyncClient() as client:
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/leads",
                headers=headers,
                params={"select": "processed_at", "order": "processed_at.desc", "limit": 1},
                timeout=10,
            )
            rows = r.json()
            if rows and rows[0].get("processed_at"):
                last_date = rows[0]["processed_at"][:10]
                logger.info(f"📅 Existing leads found — scraping since: {last_date}")
                return last_date
            logger.info(f"🆕 First ever run — scraping last 50 days since: {fallback}")
            return fallback
    except Exception as e:
        logger.error(f"Could not fetch last run date, using 50-day fallback: {e}")
        return fallback

# ═══════════════════════════════════════════════════════════
# DELIVERY
# ═══════════════════════════════════════════════════════════
MARKET_FLAG = {"USA": "🇺🇸", "UK": "🇬🇧", "Canada": "🇨🇦", "Unknown": "🌍"}

def urgency_label(score: int) -> str:
    if score >= 90: return "🔥 HIGH INTENT"
    if score >= 80: return "⚡ STRONG INTENT"
    return "👀 MODERATE INTENT"

async def send_to_slack(client: httpx.AsyncClient, lead: LeadOutput) -> bool:
    platform_emoji = {
        "reddit": "🟠 Reddit", "facebook": "🔵 Facebook", "tiktok": "🎵 TikTok",
        "yelp": "⭐ Yelp", "trustpilot": "🟩 TrustPilot", "google": "📍 Google Reviews",
    }.get(lead.source_platform, lead.source_platform.upper())

    flag = MARKET_FLAG.get(lead.market, "🌍")
    tiktok_text = f"\n*👁 TikTok Views:* {lead.tiktok_view_count}" if lead.tiktok_view_count is not None else ""

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{urgency_label(lead.intent_score)} — Score {lead.intent_score}/100  {flag} {lead.market}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Platform:*\n{platform_emoji}"},
            {"type": "mrkdwn", "text": f"*Problem:*\n{lead.problem_category}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Pain Point:*\n{lead.pain_point_summary}{tiktok_text}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*📋 Copy-paste this reply:*\n```{lead.drafted_response}```"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🎁 Offer:* {lead.free_resource_offered}"}},
        {"type": "actions", "elements": [{"type": "button",
            "text": {"type": "plain_text", "text": "View Original Post →"},
            "url": lead.source_url, "style": "primary"}]},
        {"type": "divider"},
    ]
    try:
        r = await client.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=15)
        return r.status_code < 300
    except Exception as e:
        logger.error(f"Slack error: {e}")
        return False

async def send_to_make(client: httpx.AsyncClient, lead: LeadOutput) -> bool:
    payload = {
        "urgency": f"{urgency_label(lead.intent_score)} — {lead.intent_score}",
        "platform": lead.source_platform.upper(),
        "market": lead.market,
        "link": lead.source_url,
        "problem_category": lead.problem_category,
        "pain_point": lead.pain_point_summary,
        "reply_to_post": lead.drafted_response,
        "free_offer": lead.free_resource_offered,
        "tiktok_views": lead.tiktok_view_count,
        "processed_at": lead.processed_at,
    }
    try:
        r = await client.post(MAKE_WEBHOOK_URL, json=payload, timeout=15)
        return r.status_code < 300
    except Exception as e:
        logger.error(f"Make.com error: {e}")
        return False

async def save_to_supabase(client: httpx.AsyncClient, lead: LeadOutput) -> bool:
    headers = {
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    }
    record = {
        "source_platform": lead.source_platform,
        "source_url": lead.source_url,
        "raw_text": lead.raw_text[:2000],
        "intent_score": lead.intent_score,
        "is_restaurant_owner": lead.is_restaurant_owner,
        "problem_category": lead.problem_category,
        "pain_point_summary": lead.pain_point_summary,
        "drafted_response": lead.drafted_response,
        "free_resource_offered": lead.free_resource_offered,
        "market": lead.market,
        "tiktok_view_count": lead.tiktok_view_count,
        "passed_threshold": True,
        "delivered_to_make": True,
    }
    try:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/leads", headers=headers,
                              json=record, timeout=15)
        return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Supabase error: {e}")
        return False

async def deliver_lead(lead: LeadOutput):
    async with httpx.AsyncClient() as client:
        slack = await send_to_slack(client, lead)
        make  = await send_to_make(client, lead)
        supa  = await save_to_supabase(client, lead)
        logger.info(f"  Delivered → Slack:{slack} | Make:{make} | Supabase:{supa}")

# ═══════════════════════════════════════════════════════════
# SCRAPERS
# ═══════════════════════════════════════════════════════════

# ── FACEBOOK ──────────────────────────────────────────────
# ── FACEBOOK ──────────────────────────────────────────────
def scrape_facebook_groups(since_date: str) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    logger.info(f"Facebook: scraping {len(FACEBOOK_GROUPS)} verified public groups")
    try:
        run = client.actor("apify/facebook-groups-scraper").call(run_input={
            "startUrls": [{"url": u} for u in FACEBOOK_GROUPS],
            "maxPosts": 40,
            "maxComments": 0,
            "onlyPostsNewerThan": since_date,
        })
        for post in client.dataset(run["defaultDatasetId"]).iterate_items():
            text = post.get("text") or post.get("message", "")
            url  = post.get("url") or post.get("postUrl", "")
            if text and any(kw in text.lower() for kw in KEYWORDS):
                items.append({"platform": "facebook", "url": url, "text": text})
        logger.info(f"Facebook Groups: {len(items)} matching posts")
    except Exception as e:
        logger.error(f"Facebook scraper error: {e}")
    return items

# ── REDDIT ────────────────────────────────────────────────
# Keywords that must appear in the subreddit name or description to be included
REDDIT_RELEVANCE_KEYWORDS = [
    "restaurant", "cafe", "food", "hospitality", "kitchen", "chef",
    "bar", "diner", "barista", "small business", "entrepreneur", "canada business",
    "uk business", "pizza", "burger", "server", "waiter",
]

def discover_subreddits() -> list[str]:
    found, seen = [], set()
    headers = {"User-Agent": "VersalLeadBot/1.0"}
    for term in REDDIT_SEED_TERMS:
        try:
            r = httpx.get(
                f"https://www.reddit.com/subreddits/search.json?q={term}&limit=10&sort=relevance",
                headers=headers, timeout=15,
            )
            if r.status_code != 200:
                continue
            for child in r.json().get("data", {}).get("children", []):
                d    = child.get("data", {})
                name = d.get("display_name", "")
                subs = d.get("subscribers", 0)
                kind = d.get("subreddit_type", "")
                desc = (d.get("public_description", "") + " " + d.get("display_name", "")).lower()
                # Must be public, 1k+ subs, AND actually relevant to food/restaurant/business
                relevant = any(kw in desc for kw in REDDIT_RELEVANCE_KEYWORDS)
                if name and name not in seen and kind == "public" and subs >= 1000 and relevant:
                    found.append(name)
                    seen.add(name)
                    logger.info(f"  ✅ r/{name} ({subs:,} subscribers)")
                else:
                    if name and name not in seen:
                        logger.info(f"  ⛔ r/{name} — not relevant enough, skipping")
            time.sleep(1)
        except Exception as e:
            logger.error(f"Subreddit discovery error for '{term}': {e}")
    logger.info(f"Reddit discovery: {len(found)} subreddits found")
    return found

def scrape_reddit(since_date: str) -> list[dict]:
    from apify_client import ApifyClient
    client     = ApifyClient(get_apify_token())
    items      = []
    subreddits = discover_subreddits()
    if not subreddits:
        logger.warning("No subreddits discovered — skipping Reddit scrape")
        return []
    try:
        for sub in subreddits:
            logger.info(f"  Reddit: r/{sub}")
            run = client.actor("trudax/reddit-scraper-lite").call(run_input={
                "startUrls": [{"url": f"https://www.reddit.com/r/{sub}/new/"}],
                "maxPostCount": 30, "maxCommentCount": 0,
                "afterDate": since_date,
            })
            for post in client.dataset(run["defaultDatasetId"]).iterate_items():
                combined = f"{post.get('title','')} {post.get('body','')}".lower()
                if any(kw in combined for kw in KEYWORDS):
                    items.append({
                        "platform": "reddit",
                        "url": post.get("url", f"https://reddit.com/r/{sub}"),
                        "text": f"{post.get('title','')} {post.get('body','')}",
                    })
            time.sleep(2)
        logger.info(f"Reddit: {len(items)} matching posts")
    except Exception as e:
        logger.error(f"Reddit scraper error: {e}")
    return items

# ── TIKTOK ────────────────────────────────────────────────
def scrape_tiktok() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        for hashtag in TIKTOK_HASHTAGS:
            logger.info(f"  TikTok: #{hashtag}")
            run = client.actor("clockworks/free-tiktok-scraper").call(run_input={
                "hashtags": [hashtag],
                "resultsPerPage": 30,
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
            })
            for video in client.dataset(run["defaultDatasetId"]).iterate_items():
                views   = video.get("playCount") or video.get("stats", {}).get("playCount", 9999)
                caption = video.get("text") or video.get("desc", "")
                url     = video.get("webVideoUrl") or video.get("url", "")
                author  = video.get("authorMeta", {})
                bio     = author.get("signature", "") or author.get("bio", "")
                if views > TIKTOK_MAX_VIEWS:
                    continue
                combined = (caption + bio).lower()
                restaurant_signals = [
                    "restaurant", "café", "cafe", "pizza", "burger", "diner",
                    "food", "kitchen", "chef", "cook", "menu", "hospitality",
                ]
                if not any(sig in combined for sig in restaurant_signals):
                    continue
                items.append({
                    "platform": "tiktok",
                    "url": url or f"https://tiktok.com/@{author.get('name','')}",
                    "text": f"TikTok video caption: {caption}\nAccount bio: {bio}\nViews: {views}",
                    "tiktok_views": views,
                })
            time.sleep(3)
        logger.info(f"TikTok: {len(items)} low-view restaurant videos found")
    except Exception as e:
        logger.error(f"TikTok scraper error: {e}")
    return items

# ── INSTAGRAM ─────────────────────────────────────────────
def scrape_instagram() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=INSTAGRAM_MAX_AGE_DAYS)
    try:
        for hashtag in INSTAGRAM_HASHTAGS:
            logger.info(f"  Instagram: #{hashtag}")
            run = client.actor("apify/instagram-hashtag-scraper").call(run_input={
                "hashtags": [hashtag],
                "resultsLimit": 40,
                "onlyPostsNewerThan": cutoff.strftime("%Y-%m-%d"),
            })
            for post in client.dataset(run["defaultDatasetId"]).iterate_items():
                views   = post.get("videoViewCount") or post.get("playCount") or 0
                caption = post.get("caption") or post.get("text", "")
                url     = post.get("url") or post.get("shortCode", "")
                if url and not url.startswith("http"):
                    url = f"https://www.instagram.com/p/{url}/"
                owner = post.get("ownerUsername") or post.get("owner", {}).get("username", "")
                bio   = post.get("ownerBio") or ""
                is_video = post.get("type") in ("Video", "Reel") or post.get("isVideo", False)
                if not is_video or views > INSTAGRAM_MAX_VIEWS:
                    continue
                combined = (caption + bio).lower()
                if not any(s in combined for s in ["restaurant","café","cafe","pizza","burger","food","kitchen","chef"]):
                    continue
                items.append({
                    "platform": "instagram",
                    "url": url,
                    "text": f"Instagram Reel caption: {caption}\nAccount: @{owner} | Bio: {bio}\nViews: {views}",
                    "ig_views": views,
                })
            time.sleep(3)
        logger.info(f"Instagram: {len(items)} low-view Reels found")
    except Exception as e:
        logger.error(f"Instagram scraper error: {e}")
    return items

# ── GOOGLE REVIEWS ────────────────────────────────────────
def scrape_google_reviews() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        run = client.actor("compass/google-maps-scraper").call(run_input={
            "searchStringsArray": GOOGLE_MAPS_QUERIES[:6],
            "maxReviews": 5,
            "reviewsSort": "newest",
            "language": "en",
        })
        for place in client.dataset(run["defaultDatasetId"]).iterate_items():
            rating = place.get("totalScore") or place.get("rating", 5)
            if rating and float(rating) <= 3.5:
                name    = place.get("title", "")
                url     = place.get("url") or place.get("website", "")
                reviews = place.get("reviews", [{}])
                review_text = reviews[0].get("text", "") if reviews else ""
                text = f"Google Maps: {name} ({rating}★). Recent review: {review_text}"
                if url:
                    items.append({"platform": "google", "url": url, "text": text})
        logger.info(f"Google Reviews: {len(items)} struggling restaurants")
    except Exception as e:
        logger.error(f"Google Reviews scraper error: {e}")
    return items

# ── TRUSTPILOT ────────────────────────────────────────────
def scrape_trustpilot() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        run = client.actor("apify/trustpilot-scraper").call(run_input={
            "startUrls": [{"url": u} for u in TRUSTPILOT_CATEGORIES],
            "maxReviews": 30,
            "ratingFilter": [1, 2],
        })
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            text = item.get("text") or item.get("reviewBody", "")
            url  = item.get("businessUrl") or item.get("url", "")
            if text:
                items.append({"platform": "trustpilot", "url": url,
                              "text": f"[TrustPilot low-star review] {text}"})
        logger.info(f"TrustPilot: {len(items)} reviews")
    except Exception as e:
        logger.error(f"TrustPilot scraper error: {e}")
    return items

# ── YELP ──────────────────────────────────────────────────
def scrape_yelp() -> list[dict]:
    if not YELP_RESTAURANTS:
        return []
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        run = client.actor("apify/yelp-scraper").call(run_input={
            "startUrls": [{"url": u} for u in YELP_RESTAURANTS], "maxReviews": 10,
        })
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            if item.get("rating", 5) in [1, 2] and item.get("text"):
                items.append({
                    "platform": "yelp",
                    "url": item.get("businessUrl", ""),
                    "text": f"[{item.get('rating')}★ Yelp Review] {item.get('text','')}",
                })
    except Exception as e:
        logger.error(f"Yelp scraper error: {e}")
    return items

# ═══════════════════════════════════════════════════════════
# MASTER PIPELINE
# ═══════════════════════════════════════════════════════════
_pipeline_lock = asyncio.Lock()

async def run_pipeline(test_mode: bool = False):
    if _pipeline_lock.locked():
        logger.warning("⚠️  Pipeline already running — skipping duplicate /run call")
        return {"skipped": True, "reason": "pipeline already in progress"}

    async with _pipeline_lock:
        start = time.monotonic()
        logger.info("=" * 60)
        logger.info("🚀 VERSAL DIGITAL SOLUTIONS — LEAN LEAD MACHINE v3")
        logger.info("   Platforms: Facebook · Reddit · TikTok · Google · TrustPilot")
        logger.info("   Markets:   🇺🇸 USA  🇬🇧 UK  🇨🇦 Canada")
        logger.info("=" * 60)

        if test_mode:
            logger.info("TEST MODE — sample data, no Apify credits used")
            raw_items = [
                {"platform": "facebook", "url": "https://facebook.com/groups/restaurantownersuk/test1",
                 "text": "I run a burger spot in Manchester. Been posting on Instagram for 2 years and still only getting 80-100 views per video. Revenue down 25% vs last year."},
                {"platform": "reddit", "url": "https://reddit.com/r/restaurantowners/test2",
                 "text": "Running a burger joint in Austin TX. DoorDash is killing our margins and we barely have an Instagram presence."},
                {"platform": "tiktok", "url": "https://tiktok.com/@leedscafemum/video/123",
                 "text": "TikTok video caption: Made 50 croissants today, trying to grow this account 😭\nAccount bio: Owner of The Corner Café, Leeds\nViews: 47",
                 "tiktok_views": 47},
                {"platform": "google", "url": "https://maps.google.com/?cid=test1",
                 "text": "Google Maps: Mario's Pizza NYC (2.8★). Recent review: no social media presence at all."},
            ]
        else:
            since_date = await get_last_run_date()
            logger.info(f"📅 Only scraping posts newer than: {since_date}")
            raw_items = []
            if ACTIVE_PLATFORMS.get("facebook"):   raw_items += scrape_facebook_groups(since_date)
            if ACTIVE_PLATFORMS.get("reddit"):     raw_items += scrape_reddit(since_date)
            if ACTIVE_PLATFORMS.get("tiktok"):     raw_items += scrape_tiktok()
            if ACTIVE_PLATFORMS.get("instagram"):  raw_items += scrape_instagram()
            if ACTIVE_PLATFORMS.get("google"):     raw_items += scrape_google_reviews()
            if ACTIVE_PLATFORMS.get("trustpilot"): raw_items += scrape_trustpilot()
            if ACTIVE_PLATFORMS.get("yelp"):       raw_items += scrape_yelp()

            # Dedup — skip URLs already in Supabase
            seen   = await load_seen_urls()
            before = len(raw_items)
            raw_items = [item for item in raw_items if is_new(item.get("url", ""), seen)]
            logger.info(f"Dedup: {before - len(raw_items)} already-seen skipped, {len(raw_items)} new")

        logger.info(f"Total raw items to process: {len(raw_items)}")

        qualified: list[LeadOutput] = []
        for item in raw_items:
            logger.info(f"\nAnalysing [{item['platform'].upper()}]: {item['text'][:80]}...")
            lead = process_single_lead(
                item["text"], item["platform"], item["url"],
                tiktok_views=item.get("tiktok_views"),
            )
            if lead:
                qualified.append(lead)
                logger.info(f"  ✅ QUALIFIED — Score:{lead.intent_score} | {lead.market} | {lead.problem_category}")
            time.sleep(0.5)

        logger.info(f"\n{'='*60}")
        logger.info(f"Qualified leads: {len(qualified)} / {len(raw_items)}")

        by_market, by_platform = {}, {}
        for lead in qualified:
            by_market.setdefault(lead.market, 0)
            by_market[lead.market] += 1
            by_platform.setdefault(lead.source_platform, 0)
            by_platform[lead.source_platform] += 1

        for lead in qualified:
            logger.info(f"\nDelivering {lead.market} score-{lead.intent_score} [{lead.source_platform}]...")
            await deliver_lead(lead)

        elapsed = round(time.monotonic() - start, 1)
        logger.info(f"\n✅ DONE — {len(qualified)} Versal leads delivered in {elapsed}s")
        return {
            "leads_delivered": len(qualified),
            "total_processed": len(raw_items),
            "by_market": by_market,
            "by_platform": by_platform,
            "seconds": elapsed,
        }


if __name__ == "__main__":
    result = asyncio.run(run_pipeline(test_mode=True))
    print("\n", json.dumps(result, indent=2))
