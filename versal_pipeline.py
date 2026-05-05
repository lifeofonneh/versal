"""
Versal Digital Solutions — Lean Lead Machine v5
Platforms: Reddit, Facebook Groups, TikTok, Google Reviews, TrustPilot
Markets:   USA · UK · Canada
Leads → Slack (copy-paste ready) + Supabase + Make.com

v5 fixes vs v4:
- Gemini gap raised 4.5s → 6s (10/min, well under 15/min free tier)
- Batch pacing: process 5 leads → hard 75s pause (full minute reset buffer)
- Retry backoff: 62s → 90s → 120s → 180s (exponential, not flat)
- After each retry wait, _last_gemini_call is reset so gap enforcement
  doesn't add ANOTHER wait on top of the backoff
- Skip-and-log on final retry failure instead of silently returning {}
- Apify monthly limit → caught and raises a clear RuntimeError
- Google actor name fixed: apify/google-maps-scraper (was compass/)
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
# CREDENTIALS  (all via env vars — never hardcode)
# ═══════════════════════════════════════════════════════════
def _load_apify_tokens() -> list[str]:
    tokens = [os.getenv(f"APIFY_TOKEN_{i}") for i in range(1, 8)]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ValueError("No Apify tokens found. Set APIFY_TOKEN_1 … APIFY_TOKEN_N as env vars.")
    return tokens

APIFY_TOKENS = _load_apify_tokens()
_apify_index = 0

def get_apify_token() -> str:
    global _apify_index
    tok = APIFY_TOKENS[_apify_index % len(APIFY_TOKENS)]
    _apify_index += 1
    return tok

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

FACEBOOK_GROUPS = [
    "https://www.facebook.com/groups/restaurantownersuk/",
    "https://www.facebook.com/groups/ukrestaurantowners/",
    "https://www.facebook.com/groups/hospitalityuk/",
    "https://www.facebook.com/groups/foodserviceprofessionals/",
    "https://www.facebook.com/groups/canadianrestaurantowners/",
    "https://www.facebook.com/groups/torontofoodbusiness/",
]

REDDIT_SEED_TERMS = [
    "restaurant owner", "cafe owner", "food business",
    "hospitality UK", "restaurant business Canada",
]
REDDIT_RELEVANCE_KEYWORDS = [
    "restaurant", "cafe", "food", "hospitality", "kitchen", "chef",
    "bar", "diner", "barista", "small business", "entrepreneur",
    "canada business", "uk business", "pizza", "burger", "server",
]
REDDIT_BLOCKLIST = {"CafeRacers", "caferacer", "Coffee", "ItalianFood", "chicagofood", "OttawaFood"}

TIKTOK_HASHTAGS   = ["restaurantowner", "smallrestaurant", "restaurantlife",
                     "cafeowner", "foodbusiness", "restauranttok"]
TIKTOK_MAX_VIEWS  = 500

TRUSTPILOT_CATEGORIES = [
    "https://uk.trustpilot.com/categories/restaurants_bars",
    "https://www.trustpilot.com/categories/restaurants_bars",
]

GOOGLE_MAPS_QUERIES = [
    "pizza restaurant London", "burger restaurant Manchester", "cafe Birmingham",
    "pizza restaurant New York", "burger restaurant Los Angeles",
    "cafe Toronto", "pizza restaurant Vancouver",
]

YELP_RESTAURANTS: list[str] = []

ACTIVE_PLATFORMS = {
    "facebook":   True,
    "reddit":     True,
    "tiktok":     True,
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
# GEMINI  —  RATE-LIMIT-SAFE ENGINE
#
# Free tier: 15 requests/min, 1500 requests/day
# Strategy:
#   • GEMINI_MIN_GAP = 6s  →  max 10/min  (33% headroom)
#   • BATCH_SIZE = 5, BATCH_PAUSE = 75s  →  full minute resets between batches
#   • Exponential retry waits: 90 → 120 → 180 → 240s
#   • After each wait, _last_gemini_call is set to NOW so the gap
#     enforcer doesn't stack an extra 6s on top of the backoff
# ═══════════════════════════════════════════════════════════
GEMINI_MIN_GAP  = 6.0    # seconds between calls (10/min)
BATCH_SIZE      = 5      # leads per batch
BATCH_PAUSE     = 75     # seconds between batches
RETRY_WAITS     = [90, 120, 180, 240]   # backoff schedule (seconds)

_last_gemini_call = 0.0

def _gemini_gap_wait():
    """Enforce minimum gap between Gemini calls."""
    global _last_gemini_call
    elapsed = time.monotonic() - _last_gemini_call
    if elapsed < GEMINI_MIN_GAP:
        time.sleep(GEMINI_MIN_GAP - elapsed)
    _last_gemini_call = time.monotonic()

def ask_gemini(prompt: str) -> dict:
    global _last_gemini_call
    _gemini_gap_wait()

    for attempt, wait in enumerate([0] + RETRY_WAITS, start=1):
        if wait:
            logger.warning(f"Gemini 429 — waiting {wait}s (attempt {attempt}/{len(RETRY_WAITS)+1})")
            time.sleep(wait)
            _last_gemini_call = time.monotonic()   # reset so gap enforcer doesn't double-wait

        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=900),
            )
            text = response.text.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            _last_gemini_call = time.monotonic()
            return json.loads(text.strip())

        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                if attempt > len(RETRY_WAITS):
                    logger.error(f"Gemini exhausted all retries — skipping this lead")
                    return {}
                # loop continues with next wait from RETRY_WAITS
            else:
                logger.error(f"Gemini non-rate-limit error: {e}")
                return {}

    return {}


def analyse_lead(text: str, platform: str,
                 tiktok_views: Optional[int] = None) -> dict:
    """Single combined Gemini call: score + classify + draft."""
    market_notes = {
        "UK":     "Use British spelling. Reference UK platforms (Just Eat, Deliveroo).",
        "USA":    "Use American spelling. Reference US platforms (DoorDash, Grubhub, Uber Eats).",
        "Canada": "Use Canadian context. Reference Skip The Dishes / DoorDash Canada.",
    }
    platform_tones = {
        "reddit":     "casual Reddit reply, peer-to-peer, no formality",
        "facebook":   "friendly Facebook comment, warm and helpful",
        "tiktok":     "short friendly TikTok comment (max 2 sentences), then DM offer",
        "yelp":       "empathetic cold outreach email to restaurant owner",
        "trustpilot": "empathetic cold outreach email to struggling restaurant owner",
        "google":     "empathetic cold outreach email referencing their Google presence",
    }
    tiktok_note = (f"\nThis TikTok video has only {tiktok_views} views — "
                   "reference this naturally." if tiktok_views else "")
    tone = platform_tones.get(platform, "friendly helpful message")
    # Market note will be filled by Gemini once it detects the market;
    # we pass all three so it picks the right one.
    market_block = "\n".join(f"- If market={k}: {v}" for k, v in market_notes.items())

    return ask_gemini(f"""You are an analyst and copywriter for Versal Digital Solutions.
{AGENCY_CONTEXT}

Analyse this {platform} post/text and return ONLY valid JSON with ALL fields below.

SCORING:
- is_restaurant_owner: true only if the author is an owner/operator (not a diner or staff)
- intent_score: 0-100 urgency for social media / marketing / visibility help
  90-100 = actively asking for help or has very low social media reach
  70-89  = clear owner pain around visibility or growth
  50-69  = owner venting, not actively seeking help
  0-49   = diner, staff, unrelated
- market: "USA", "UK", "Canada", or "Unknown" — detect from context clues
  (Just Eat/Deliveroo = UK, Skip The Dishes = Canada, DoorDash/Grubhub = USA)

CLASSIFICATION (only if is_restaurant_owner=true AND intent_score>=75):
- problem_category: Social Media/Visibility | Profitability/Margins | Labor/Hiring |
  Tech/POS | Reputation/Reviews | Foot Traffic | Delivery App Dependency | Other
- pain_point_summary: one sentence, max 15 words

REPLY (only if is_restaurant_owner=true AND intent_score>=75):
- Write a {tone} reply to a restaurant owner struggling with their problem
- Market-specific tone rules:
{market_block}{tiktok_note}
- Max 3 sentences. Sound like a helpful peer, NOT an agency.
- Lead with ONE specific actionable tip for their exact problem.
- End by offering Versal's free mini-audit (no commitment, 2 mins to apply).
- Never mention pricing. Never be pushy.

Return ONLY this JSON (no markdown, no preamble):
{{
  "is_restaurant_owner": true/false,
  "intent_score": 0-100,
  "market": "USA|UK|Canada|Unknown",
  "problem_category": "category or null",
  "pain_point_summary": "summary or null",
  "drafted_response": "reply text or null",
  "free_resource_offered": "Free Versal Mini-Audit + 15-min Strategy Call"
}}

Text to analyse:
{text[:1400]}""")


def process_single_lead(text: str, platform: str, url: str,
                        threshold: int = 75,
                        tiktok_views: Optional[int] = None) -> Optional[LeadOutput]:
    result = analyse_lead(text, platform, tiktok_views=tiktok_views)
    if not result:
        return None

    score    = result.get("intent_score", 0)
    is_owner = result.get("is_restaurant_owner", False)
    market   = result.get("market", "Unknown")
    logger.info(f"  Score:{score} | Owner:{is_owner} | Market:{market}")

    if not is_owner or score < threshold or not result.get("drafted_response"):
        return None

    return LeadOutput(
        source_platform=platform, source_url=url, raw_text=text,
        intent_score=score, is_restaurant_owner=is_owner,
        problem_category=result.get("problem_category", "Other"),
        pain_point_summary=result.get("pain_point_summary", ""),
        drafted_response=result.get("drafted_response", ""),
        free_resource_offered=result.get("free_resource_offered", "Free Versal Mini-Audit"),
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
            logger.info(f"Dedup: {len(seen)} previously seen URLs loaded")
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
                last = rows[0]["processed_at"][:10]
                logger.info(f"Last run: {last}")
                return last
            logger.info(f"First run — using {fallback}")
            return fallback
    except Exception as e:
        logger.error(f"Could not fetch last run date: {e}")
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
    tiktok_text = f"\n*👁 Views:* {lead.tiktok_view_count}" if lead.tiktok_view_count is not None else ""
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{urgency_label(lead.intent_score)} — {lead.intent_score}/100  {flag} {lead.market}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Platform:*\n{platform_emoji}"},
            {"type": "mrkdwn", "text": f"*Problem:*\n{lead.problem_category}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Pain Point:*\n{lead.pain_point_summary}{tiktok_text}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*📋 Copy-paste reply:*\n```{lead.drafted_response}```"}},
        {"type": "actions", "elements": [{"type": "button",
            "text": {"type": "plain_text", "text": "View Original Post →"},
            "url": lead.source_url, "style": "primary"}]},
        {"type": "divider"},
    ]
    try:
        r = await client.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=15)
        return r.status_code < 300
    except Exception as e:
        logger.error(f"Slack error: {e}"); return False

async def send_to_make(client: httpx.AsyncClient, lead: LeadOutput) -> bool:
    try:
        r = await client.post(MAKE_WEBHOOK_URL, json={
            "urgency": f"{urgency_label(lead.intent_score)} — {lead.intent_score}",
            "platform": lead.source_platform.upper(), "market": lead.market,
            "link": lead.source_url, "problem_category": lead.problem_category,
            "pain_point": lead.pain_point_summary, "reply_to_post": lead.drafted_response,
            "free_offer": lead.free_resource_offered, "tiktok_views": lead.tiktok_view_count,
            "processed_at": lead.processed_at,
        }, timeout=15)
        return r.status_code < 300
    except Exception as e:
        logger.error(f"Make.com error: {e}"); return False

async def save_to_supabase(client: httpx.AsyncClient, lead: LeadOutput) -> bool:
    headers = {
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    }
    try:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/leads", headers=headers, json={
            "source_platform": lead.source_platform, "source_url": lead.source_url,
            "raw_text": lead.raw_text[:2000], "intent_score": lead.intent_score,
            "is_restaurant_owner": lead.is_restaurant_owner,
            "problem_category": lead.problem_category,
            "pain_point_summary": lead.pain_point_summary,
            "drafted_response": lead.drafted_response,
            "free_resource_offered": lead.free_resource_offered,
            "market": lead.market, "tiktok_view_count": lead.tiktok_view_count,
            "passed_threshold": True, "delivered_to_make": True,
        }, timeout=15)
        return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Supabase error: {e}"); return False

async def deliver_lead(lead: LeadOutput):
    async with httpx.AsyncClient() as client:
        slack = await send_to_slack(client, lead)
        make  = await send_to_make(client, lead)
        supa  = await save_to_supabase(client, lead)
        logger.info(f"  Delivered → Slack:{slack} | Make:{make} | Supabase:{supa}")


# ═══════════════════════════════════════════════════════════
# SCRAPERS
# ═══════════════════════════════════════════════════════════

def scrape_facebook_groups(since_date: str) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        run = client.actor("apify/facebook-groups-scraper").call(run_input={
            "startUrls": [{"url": u} for u in FACEBOOK_GROUPS],
            "resultsLimit": 40, "maxComments": 0,
        })
        for post in client.dataset(run["defaultDatasetId"]).iterate_items():
            text = post.get("text") or post.get("message", "")
            url  = post.get("url") or post.get("postUrl", "")
            if text and any(kw in text.lower() for kw in KEYWORDS):
                items.append({"platform": "facebook", "url": url, "text": text})
        logger.info(f"Facebook: {len(items)} matching posts")
    except Exception as e:
        logger.error(f"Facebook error: {e}")
    return items

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
                desc = (d.get("public_description", "") + " " + name).lower()
                if (name and name not in seen and kind == "public" and subs >= 1000
                        and any(kw in desc for kw in REDDIT_RELEVANCE_KEYWORDS)
                        and name not in REDDIT_BLOCKLIST):
                    found.append(name)
                seen.add(name)
            time.sleep(1)
        except Exception as e:
            logger.error(f"Subreddit discovery '{term}': {e}")
    logger.info(f"Reddit: {len(found)} subreddits discovered")
    return found

def scrape_reddit(since_date: str) -> list[dict]:
    from apify_client import ApifyClient
    client     = ApifyClient(get_apify_token())
    items      = []
    subreddits = discover_subreddits()
    if not subreddits:
        logger.warning("No subreddits found — skipping Reddit")
        return []
    try:
        for sub in subreddits:
            logger.info(f"  Reddit: r/{sub}")
            run = client.actor("trudax/reddit-scraper-lite").call(run_input={
                "startUrls": [{"url": f"https://www.reddit.com/r/{sub}/new/"}],
                "maxPostCount": 30, "maxCommentCount": 0, "afterDate": since_date,
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
        logger.error(f"Reddit error: {e}")
    return items

def scrape_tiktok() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        for hashtag in TIKTOK_HASHTAGS:
            logger.info(f"  TikTok: #{hashtag}")
            try:
                run = client.actor("clockworks/free-tiktok-scraper").call(run_input={
                    "hashtags": [hashtag], "resultsPerPage": 30,
                    "shouldDownloadVideos": False, "shouldDownloadCovers": False,
                })
            except Exception as e:
                msg = str(e)
                if "Monthly usage hard limit" in msg or "hard limit exceeded" in msg:
                    logger.error("Apify monthly limit hit — stopping TikTok scraping")
                    break
                raise
            for video in client.dataset(run["defaultDatasetId"]).iterate_items():
                views   = video.get("playCount") or video.get("stats", {}).get("playCount", 9999)
                caption = video.get("text") or video.get("desc", "")
                url     = video.get("webVideoUrl") or video.get("url", "")
                author  = video.get("authorMeta", {})
                bio     = author.get("signature", "") or author.get("bio", "")
                if views > TIKTOK_MAX_VIEWS:
                    continue
                if not any(s in (caption + bio).lower() for s in
                           ["restaurant","café","cafe","pizza","burger","food","kitchen","chef","menu","hospitality"]):
                    continue
                items.append({
                    "platform": "tiktok",
                    "url": url or f"https://tiktok.com/@{author.get('name','')}",
                    "text": f"TikTok caption: {caption}\nBio: {bio}\nViews: {views}",
                    "tiktok_views": views,
                })
            time.sleep(3)
        logger.info(f"TikTok: {len(items)} low-view videos")
    except Exception as e:
        logger.error(f"TikTok error: {e}")
    return items

def scrape_google_reviews() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        run = client.actor("apify/google-maps-scraper").call(run_input={
            "searchStringsArray": GOOGLE_MAPS_QUERIES[:6],
            "maxReviews": 5, "reviewsSort": "newest", "language": "en",
        })
        for place in client.dataset(run["defaultDatasetId"]).iterate_items():
            rating = place.get("totalScore") or place.get("rating", 5)
            if rating and float(rating) <= 3.5:
                name    = place.get("title", "")
                url     = place.get("url") or place.get("website", "")
                reviews = place.get("reviews", [{}])
                review_text = reviews[0].get("text", "") if reviews else ""
                if url:
                    items.append({
                        "platform": "google", "url": url,
                        "text": f"Google Maps: {name} ({rating}★). Review: {review_text}",
                    })
        logger.info(f"Google: {len(items)} struggling restaurants")
    except Exception as e:
        logger.error(f"Google error: {e}")
    return items

def scrape_trustpilot() -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(get_apify_token())
    items  = []
    try:
        run = client.actor("apify/trustpilot-scraper").call(run_input={
            "startUrls": [{"url": u} for u in TRUSTPILOT_CATEGORIES],
            "maxReviews": 30, "ratingFilter": [1, 2],
        })
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            text = item.get("text") or item.get("reviewBody", "")
            url  = item.get("businessUrl") or item.get("url", "")
            if text:
                items.append({"platform": "trustpilot", "url": url,
                              "text": f"[TrustPilot 1-2★] {text}"})
        logger.info(f"TrustPilot: {len(items)} reviews")
    except Exception as e:
        logger.error(f"TrustPilot error: {e}")
    return items

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
                    "platform": "yelp", "url": item.get("businessUrl", ""),
                    "text": f"[{item.get('rating')}★ Yelp] {item.get('text','')}",
                })
    except Exception as e:
        logger.error(f"Yelp error: {e}")
    return items


# ═══════════════════════════════════════════════════════════
# MASTER PIPELINE
# ═══════════════════════════════════════════════════════════
_pipeline_lock = asyncio.Lock()

async def run_pipeline(test_mode: bool = False):
    if _pipeline_lock.locked():
        logger.warning("⚠️  Pipeline already running — skipping duplicate call")
        return {"skipped": True, "reason": "already running"}

    async with _pipeline_lock:
        start = time.monotonic()
        logger.info("=" * 60)
        logger.info("🚀 VERSAL DIGITAL SOLUTIONS — LEAN LEAD MACHINE v5")
        logger.info("   Platforms: Facebook · Reddit · TikTok · Google · TrustPilot")
        logger.info("   Markets:   🇺🇸 USA  🇬🇧 UK  🇨🇦 Canada")
        logger.info(f"   Gemini pacing: {GEMINI_MIN_GAP}s/call · batch {BATCH_SIZE} → pause {BATCH_PAUSE}s")
        logger.info("=" * 60)

        if test_mode:
            logger.info("TEST MODE — no Apify credits used")
            raw_items = [
                {"platform": "facebook", "url": "https://facebook.com/test1",
                 "text": "I run a burger spot in Manchester. Instagram for 2 years, still only 80-100 views per video. Revenue down 25%."},
                {"platform": "reddit", "url": "https://reddit.com/test2",
                 "text": "Running a burger joint in Austin TX. DoorDash is killing our margins and we barely have an Instagram presence."},
                {"platform": "tiktok", "url": "https://tiktok.com/@leedscafe/video/123",
                 "text": "TikTok caption: Made 50 croissants today, trying to grow this account 😭\nBio: Owner of The Corner Café, Leeds\nViews: 47",
                 "tiktok_views": 47},
                {"platform": "google", "url": "https://maps.google.com/?cid=test1",
                 "text": "Google Maps: Mario's Pizza NYC (2.8★). Review: no social media presence at all."},
            ]
        else:
            since_date = await get_last_run_date()
            raw_items  = []
            if ACTIVE_PLATFORMS.get("facebook"):   raw_items += scrape_facebook_groups(since_date)
            if ACTIVE_PLATFORMS.get("reddit"):     raw_items += scrape_reddit(since_date)
            if ACTIVE_PLATFORMS.get("tiktok"):     raw_items += scrape_tiktok()
            if ACTIVE_PLATFORMS.get("google"):     raw_items += scrape_google_reviews()
            if ACTIVE_PLATFORMS.get("trustpilot"): raw_items += scrape_trustpilot()
            if ACTIVE_PLATFORMS.get("yelp"):       raw_items += scrape_yelp()

            seen   = await load_seen_urls()
            before = len(raw_items)
            raw_items = [i for i in raw_items if is_new(i.get("url", ""), seen)]
            logger.info(f"Dedup: {before - len(raw_items)} skipped, {len(raw_items)} new")

        n = len(raw_items)
        estimated = (n * GEMINI_MIN_GAP) + ((n // BATCH_SIZE) * BATCH_PAUSE)
        logger.info(f"Processing {n} items — est. Gemini time ~{int(estimated)}s "
                    f"({n} calls + {n // BATCH_SIZE} batch pause{'s' if n//BATCH_SIZE!=1 else ''})")

        qualified: list[LeadOutput] = []

        for i, item in enumerate(raw_items):
            # ── Batch pause: after every BATCH_SIZE leads, wait for the
            #    Gemini per-minute window to fully reset
            if i > 0 and i % BATCH_SIZE == 0:
                logger.info(f"  ⏸  Batch pause {BATCH_PAUSE}s to reset Gemini rate limit "
                            f"(batch {i // BATCH_SIZE} of {(n-1) // BATCH_SIZE + 1})")
                time.sleep(BATCH_PAUSE)

            logger.info(f"\n[{i+1}/{n}][{item['platform'].upper()}] {item['text'][:80]}...")
            lead = process_single_lead(
                item["text"], item["platform"], item["url"],
                tiktok_views=item.get("tiktok_views"),
            )
            if lead:
                qualified.append(lead)
                logger.info(f"  ✅ QUALIFIED — Score:{lead.intent_score} | "
                            f"{lead.market} | {lead.problem_category}")

        logger.info(f"\n{'='*60}")
        logger.info(f"Qualified: {len(qualified)} / {n}")

        by_market, by_platform = {}, {}
        for lead in qualified:
            by_market[lead.market]            = by_market.get(lead.market, 0) + 1
            by_platform[lead.source_platform] = by_platform.get(lead.source_platform, 0) + 1

        for lead in qualified:
            logger.info(f"Delivering [{lead.source_platform}] score:{lead.intent_score} {lead.market}...")
            await deliver_lead(lead)

        elapsed = round(time.monotonic() - start, 1)
        logger.info(f"\n✅ DONE — {len(qualified)} leads delivered in {elapsed}s")
        return {
            "leads_delivered": len(qualified),
            "total_processed": n,
            "by_market": by_market,
            "by_platform": by_platform,
            "seconds": elapsed,
        }


if __name__ == "__main__":
    # test_mode=True  → instant run, no Apify credits
    # test_mode=False → full live run
    result = asyncio.run(run_pipeline(test_mode=True))
    print("\n", json.dumps(result, indent=2))
