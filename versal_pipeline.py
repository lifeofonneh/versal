"""
Versal Digital Solutions — Lean Lead Machine v7
Platforms: Reddit, Facebook Groups, TikTok, Google Reviews, Instagram
Markets:   USA · UK · Canada
Leads → Slack (copy-paste ready) + Supabase + Make.com

v7 changes vs v6:
- Hard date filter: posts older than MAX_POST_AGE_DAYS are dropped BEFORE Gemini
- Post age injected into Gemini prompt so stale posts score low automatically
- NEW lead source: Instagram hashtag scraper (low-follower restaurant accounts)
- NEW lead source: Google Maps "no website" restaurants (social link missing = opportunity)
- NEW lead source: Just Opened detector (new restaurants on Google Maps)
- Stronger Reddit targeting: owner-specific subreddits only, not general entrepreneur subs
- TikTok: also checks author follower count (under 1000 = stronger signal)
- Gemini model: gemini-2.5-flash with thinking disabled
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
    tokens = [os.getenv(f"APIFY_TOKEN_{i}") for i in range(1, 11)]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ValueError("No Apify tokens found. Set APIFY_TOKEN_1 … APIFY_TOKEN_N as env vars.")
    return tokens

APIFY_TOKENS = _load_apify_tokens()
_apify_index = 0
_exhausted_tokens: set[str] = set()

def get_apify_token() -> str:
    """Rotate to the next non-exhausted token."""
    global _apify_index
    for _ in range(len(APIFY_TOKENS)):
        tok = APIFY_TOKENS[_apify_index % len(APIFY_TOKENS)]
        _apify_index += 1
        if tok not in _exhausted_tokens:
            return tok
    raise RuntimeError("All Apify tokens exhausted for this month.")

def mark_token_exhausted(tok: str):
    _exhausted_tokens.add(tok)
    remaining = len(APIFY_TOKENS) - len(_exhausted_tokens)
    logger.warning(f"Token exhausted — {remaining}/{len(APIFY_TOKENS)} tokens remaining")

def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise ValueError(f"Missing required environment variable: {name}")
    return val

SUPABASE_URL      = _require_env("SUPABASE_URL")
SUPABASE_KEY      = _require_env("SUPABASE_KEY")
MAKE_WEBHOOK_URL  = _require_env("MAKE_WEBHOOK_URL")
SLACK_WEBHOOK_URL = _require_env("SLACK_WEBHOOK_URL")
GEMINI_API_KEY         = _require_env("GEMINI_API_KEY")
GOOGLE_PLACES_API_KEY  = _require_env("GOOGLE_PLACES_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
MAX_POST_AGE_DAYS = 14      # hard drop anything older than this before Gemini
GEMINI_MODEL      = "gemini-2.5-flash"

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

# Tight subreddit list — actual owner communities only
REDDIT_SUBREDDITS = [
    "restaurantowners",
    "smallbusiness",
    "Entrepreneur",
    "FoodService",
    "KitchenConfidential",
    "cafe",
    "barowners",
]
REDDIT_BLOCKLIST = {"CafeRacers", "caferacer", "Coffee", "ItalianFood", "chicagofood", "OttawaFood"}

TIKTOK_HASHTAGS = [
    "restaurantowner", "smallrestaurant", "restaurantlife",
    "cafeowner", "foodbusiness", "restauranttok",
    "restaurantmarketing", "newrestaurant",
]
TIKTOK_MAX_VIEWS     = 500
TIKTOK_MAX_FOLLOWERS = 1000   # author follower ceiling — solo owner signal

# Instagram hashtags — low-view content from actual owners
INSTAGRAM_HASHTAGS = [
    "restaurantowner", "cafeowner", "pizzashop", "burgerrestaurant",
    "newrestaurant", "smallrestaurant", "restaurantuk", "londonrestaurant",
    "torontorestaurant", "nycrestaurant",
]
INSTAGRAM_MAX_LIKES = 50      # very low engagement = no marketing help

# ═══════════════════════════════════════════════════════════
# GOOGLE PLACES API — direct, no Apify, $0 within free $200 credit
# Strategy: search by type+city, filter for low rating OR no website
# Each search returns up to 20 places, we do Next Page tokens for 60 per city
# ═══════════════════════════════════════════════════════════
GOOGLE_PLACES_SEARCHES = [
    # UK cities
    {"query": "pizza restaurant",    "location": "London, UK"},
    {"query": "burger restaurant",   "location": "London, UK"},
    {"query": "cafe restaurant",     "location": "London, UK"},
    {"query": "indian restaurant",   "location": "London, UK"},
    {"query": "chinese restaurant",  "location": "London, UK"},
    {"query": "pizza restaurant",    "location": "Manchester, UK"},
    {"query": "burger restaurant",   "location": "Manchester, UK"},
    {"query": "cafe",                "location": "Manchester, UK"},
    {"query": "restaurant",          "location": "Birmingham, UK"},
    {"query": "restaurant",          "location": "Leeds, UK"},
    {"query": "restaurant",          "location": "Bristol, UK"},
    {"query": "restaurant",          "location": "Edinburgh, UK"},
    {"query": "restaurant",          "location": "Glasgow, UK"},
    {"query": "restaurant",          "location": "Liverpool, UK"},
    {"query": "restaurant",          "location": "Sheffield, UK"},
    # USA cities
    {"query": "pizza restaurant",    "location": "New York, NY"},
    {"query": "burger restaurant",   "location": "New York, NY"},
    {"query": "cafe",                "location": "New York, NY"},
    {"query": "restaurant",          "location": "Los Angeles, CA"},
    {"query": "restaurant",          "location": "Chicago, IL"},
    {"query": "restaurant",          "location": "Houston, TX"},
    {"query": "restaurant",          "location": "Phoenix, AZ"},
    {"query": "restaurant",          "location": "Philadelphia, PA"},
    {"query": "restaurant",          "location": "San Antonio, TX"},
    {"query": "restaurant",          "location": "Dallas, TX"},
    # Canada cities
    {"query": "pizza restaurant",    "location": "Toronto, Canada"},
    {"query": "burger restaurant",   "location": "Toronto, Canada"},
    {"query": "cafe",                "location": "Toronto, Canada"},
    {"query": "restaurant",          "location": "Vancouver, Canada"},
    {"query": "restaurant",          "location": "Calgary, Canada"},
    {"query": "restaurant",          "location": "Edmonton, Canada"},
    {"query": "restaurant",          "location": "Montreal, Canada"},
    {"query": "restaurant",          "location": "Ottawa, Canada"},
]
PLACES_MAX_PER_SEARCH  = 20   # Places API returns max 20 per page
PLACES_MIN_RATING      = 4.2  # flag anything at or below this

TRUSTPILOT_CATEGORIES = [
    "https://uk.trustpilot.com/categories/restaurants_bars",
    "https://www.trustpilot.com/categories/restaurants_bars",
]

YELP_RESTAURANTS: list[str] = []

ACTIVE_PLATFORMS = {
    "facebook":   True,
    "reddit":     True,
    "tiktok":     True,
    "instagram":  True,
    "google":     True,
    "trustpilot": False,
    "yelp":       False,
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
    post_age_days: Optional[int] = None
    processed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    passed_threshold: bool


# ═══════════════════════════════════════════════════════════
# DATE HELPERS
# ═══════════════════════════════════════════════════════════
def parse_post_date(raw: str) -> Optional[datetime]:
    """Try to parse a post timestamp from various formats scrapers return."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:26], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None

def post_age_days(timestamp_str: str) -> Optional[int]:
    dt = parse_post_date(timestamp_str)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).days

def is_recent(timestamp_str: str, max_days: int = MAX_POST_AGE_DAYS) -> bool:
    """Return True if post is within max_days. If date unknown, allow through."""
    age = post_age_days(timestamp_str)
    if age is None:
        return True   # can't determine age → let Gemini decide
    return age <= max_days


# ═══════════════════════════════════════════════════════════
# GEMINI
# ═══════════════════════════════════════════════════════════
def ask_gemini(prompt: str) -> dict:
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=900,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = response.text.strip()
        if "```" in text:
            parts = text.split("```")
            text = parts[1].lstrip("json").strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start == -1 or end == 0:
            logger.error(f"Gemini: no JSON found in response: {text[:200]}")
            return {}
        return json.loads(text[start:end])
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return {}


def analyse_lead(text: str, platform: str,
                 tiktok_views: Optional[int] = None,
                 age_days: Optional[int] = None,
                 extra_signals: str = "") -> dict:
    market_notes = {
        "UK":     "Use British spelling. Reference UK platforms (Just Eat, Deliveroo).",
        "USA":    "Use American spelling. Reference US platforms (DoorDash, Grubhub, Uber Eats).",
        "Canada": "Use Canadian context. Reference Skip The Dishes / DoorDash Canada.",
    }
    platform_tones = {
        "reddit":    "casual Reddit reply, peer-to-peer, no formality",
        "facebook":  "friendly Facebook comment, warm and helpful",
        "tiktok":    "short friendly TikTok comment (max 2 sentences), then DM offer",
        "instagram": "short friendly Instagram comment (max 2 sentences), then DM offer",
        "yelp":      "empathetic cold outreach email to restaurant owner",
        "trustpilot":"empathetic cold outreach email to struggling restaurant owner",
        "google":    "empathetic cold outreach email referencing their Google presence",
    }
    tiktok_note = (f"\nThis TikTok video has only {tiktok_views} views — "
                   "reference this naturally." if tiktok_views else "")
    age_note = ""
    if age_days is not None:
        if age_days > MAX_POST_AGE_DAYS:
            age_note = f"\n⚠️ POST IS {age_days} DAYS OLD — this is stale. Score intent_score no higher than 20 regardless of content."
        elif age_days > 7:
            age_note = f"\nPost is {age_days} days old — slightly dated, factor this into urgency."
        else:
            age_note = f"\nPost is {age_days} days old — very recent."

    tone = platform_tones.get(platform, "friendly helpful message")
    market_block = "\n".join(f"- If market={k}: {v}" for k, v in market_notes.items())

    return ask_gemini(f"""You are an analyst and copywriter for Versal Digital Solutions.
{AGENCY_CONTEXT}

Analyse this {platform} post/text and return ONLY valid JSON with ALL fields below.
{age_note}{extra_signals}

SCORING:
- is_restaurant_owner: true only if the author is an owner/operator (not a diner or staff)
- intent_score: 0-100 urgency for social media / marketing / visibility help
  90-100 = actively asking for help OR clearly struggling with reach/visibility right now
  70-89  = owner pain around visibility/growth is obvious
  50-69  = owner venting, not actively seeking help
  0-49   = diner, staff, unrelated, or post is too old to act on
- market: "USA", "UK", "Canada", or "Unknown"
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
                        tiktok_views: Optional[int] = None,
                        timestamp: Optional[str] = None,
                        extra_signals: str = "") -> Optional[LeadOutput]:
    age = post_age_days(timestamp) if timestamp else None

    # Hard drop before spending a Gemini call
    if age is not None and age > MAX_POST_AGE_DAYS:
        logger.info(f"  ⏭ SKIPPED — post is {age} days old (>{MAX_POST_AGE_DAYS}d cutoff)")
        return None

    result = analyse_lead(text, platform, tiktok_views=tiktok_views,
                          age_days=age, extra_signals=extra_signals)
    if not result:
        return None

    score    = result.get("intent_score", 0)
    is_owner = result.get("is_restaurant_owner", False)
    market   = result.get("market", "Unknown")
    logger.info(f"  Score:{score} | Owner:{is_owner} | Market:{market} | Age:{age}d")

    if not is_owner or score < threshold or not result.get("drafted_response"):
        return None

    return LeadOutput(
        source_platform=platform, source_url=url, raw_text=text,
        intent_score=score, is_restaurant_owner=is_owner,
        problem_category=result.get("problem_category", "Other"),
        pain_point_summary=result.get("pain_point_summary", ""),
        drafted_response=result.get("drafted_response", ""),
        free_resource_offered=result.get("free_resource_offered", "Free Versal Mini-Audit"),
        market=market, tiktok_view_count=tiktok_views,
        post_age_days=age, passed_threshold=True,
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
                    f"{SUPABASE_URL}/rest/v1/leads", headers=headers,
                    params={"select": "source_url", "limit": 1000, "offset": offset},
                    timeout=15,
                )
                rows = r.json()
                if not rows: break
                seen.update(row["source_url"] for row in rows if row.get("source_url"))
                if len(rows) < 1000: break
                offset += 1000
            logger.info(f"Dedup: {len(seen)} previously seen URLs loaded")
            return seen
    except Exception as e:
        logger.error(f"Dedup load error: {e}")
        return set()

def is_new(url: str, seen: set[str]) -> bool:
    if not url or url in seen: return False
    seen.add(url)
    return True


# ═══════════════════════════════════════════════════════════
# LAST RUN DATE
# ═══════════════════════════════════════════════════════════
async def get_last_run_date() -> str:
    fallback = (datetime.now(timezone.utc) - timedelta(days=MAX_POST_AGE_DAYS)).strftime("%Y-%m-%d")
    try:
        async with httpx.AsyncClient() as client:
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/leads", headers=headers,
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
        "instagram": "📸 Instagram", "yelp": "⭐ Yelp",
        "trustpilot": "🟩 TrustPilot", "google": "📍 Google Maps",
    }.get(lead.source_platform, lead.source_platform.upper())
    flag = MARKET_FLAG.get(lead.market, "🌍")
    tiktok_text = f"\n*👁 Views:* {lead.tiktok_view_count}" if lead.tiktok_view_count is not None else ""
    age_text    = f"\n*📅 Post age:* {lead.post_age_days}d ago" if lead.post_age_days is not None else ""
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{urgency_label(lead.intent_score)} — {lead.intent_score}/100  {flag} {lead.market}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Platform:*\n{platform_emoji}"},
            {"type": "mrkdwn", "text": f"*Problem:*\n{lead.problem_category}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Pain Point:*\n{lead.pain_point_summary}{tiktok_text}{age_text}"}},
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
            "post_age_days": lead.post_age_days, "processed_at": lead.processed_at,
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
            "post_age_days": lead.post_age_days,
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
_apify_limit_hit = False

def _apify_run(actor: str, run_input: dict) -> list:
    """
    Run an Apify actor, rotating to the next token automatically if one is exhausted.
    Tries every available non-exhausted token before giving up.
    """
    global _apify_limit_hit

    available = [t for t in APIFY_TOKENS if t not in _exhausted_tokens]
    if not available:
        logger.error("All Apify tokens exhausted")
        _apify_limit_hit = True
        return []

    for tok in available:
        try:
            logger.info(f"  Apify: using token …{tok[-6:]}")
            client = ApifyClient(tok)
            run    = client.actor(actor).call(run_input=run_input)
            return list(client.dataset(run["defaultDatasetId"]).iterate_items())
        except Exception as e:
            msg = str(e)
            if "Monthly usage hard limit" in msg or "hard limit exceeded" in msg:
                mark_token_exhausted(tok)
                remaining = len(APIFY_TOKENS) - len(_exhausted_tokens)
                if remaining == 0:
                    logger.error("All Apify tokens exhausted — stopping scraping")
                    _apify_limit_hit = True
                    return []
                logger.info(f"  Rotating to next token ({remaining} remaining)...")
                continue
            else:
                raise  # non-quota error — let caller handle

    logger.error("All token rotation attempts failed")
    _apify_limit_hit = True
    return []


def scrape_facebook_groups(since_date: str) -> list[dict]:
    """
    ONE actor run for ALL groups — single container startup = fraction of the cost.
    resultsLimit applies across all groups combined.
    """
    global _apify_limit_hit
    if _apify_limit_hit: return []
    items = []
    logger.info(f"  Facebook: {len(FACEBOOK_GROUPS)} groups in one run")
    try:
        posts = _apify_run("apify/facebook-groups-scraper", {
            "startUrls": [{"url": u} for u in FACEBOOK_GROUPS],
            "resultsLimit": 50,
            "maxComments": 0,
        })
    except Exception as e:
        logger.error(f"Facebook error: {e}")
        return []
    for post in posts:
        text      = post.get("text") or post.get("message", "")
        url       = post.get("url") or post.get("postUrl", "")
        timestamp = post.get("time") or post.get("timestamp") or post.get("date", "")
        if not text or not any(kw in text.lower() for kw in KEYWORDS):
            continue
        if not is_recent(timestamp):
            logger.info(f"  ⏭ Facebook post too old: {timestamp}")
            continue
        items.append({"platform": "facebook", "url": url, "text": text,
                      "timestamp": timestamp})
    logger.info(f"Facebook: {len(items)} matching posts")
    return items

def scrape_reddit(since_date: str) -> list[dict]:
    global _apify_limit_hit
    if _apify_limit_hit: return []
    items  = []
    # Use the curated subreddit list directly — no discovery needed
    for sub in REDDIT_SUBREDDITS:
        if _apify_limit_hit: break
        if sub in REDDIT_BLOCKLIST: continue
        logger.info(f"  Reddit: r/{sub}")
        try:
            posts = _apify_run("trudax/reddit-scraper-lite", {
                "startUrls": [{"url": f"https://www.reddit.com/r/{sub}/new/"}],
                "maxPostCount": 30, "maxCommentCount": 0, "afterDate": since_date,
            })
        except Exception as e:
            logger.error(f"Reddit r/{sub} error: {e}"); time.sleep(2); continue
        for post in posts:
            combined  = f"{post.get('title','')} {post.get('body','')}".lower()
            timestamp = post.get("created_utc") or post.get("createdAt") or post.get("date", "")
            if not any(kw in combined for kw in KEYWORDS):
                continue
            if not is_recent(str(timestamp) if timestamp else ""):
                continue
            items.append({
                "platform": "reddit",
                "url":  post.get("url", f"https://reddit.com/r/{sub}"),
                "text": f"{post.get('title','')} {post.get('body','')}",
                "timestamp": str(timestamp),
            })
        time.sleep(2)
    logger.info(f"Reddit: {len(items)} matching posts")
    return items


def scrape_tiktok() -> list[dict]:
    global _apify_limit_hit
    if _apify_limit_hit: return []
    items  = []
    for hashtag in TIKTOK_HASHTAGS:
        if _apify_limit_hit: break
        logger.info(f"  TikTok: #{hashtag}")
        try:
            videos = _apify_run("clockworks/free-tiktok-scraper", {
                "hashtags": [hashtag], "resultsPerPage": 30,
                "shouldDownloadVideos": False, "shouldDownloadCovers": False,
            })
        except Exception as e:
            logger.error(f"TikTok #{hashtag} error: {e}"); continue
        for video in videos:
            views     = video.get("playCount") or video.get("stats", {}).get("playCount", 9999)
            followers = video.get("authorMeta", {}).get("fans") or \
                        video.get("authorMeta", {}).get("followers", 9999)
            caption   = video.get("text") or video.get("desc", "")
            url       = video.get("webVideoUrl") or video.get("url", "")
            author    = video.get("authorMeta", {})
            bio       = author.get("signature", "") or author.get("bio", "")
            timestamp = video.get("createTime") or video.get("createTimeISO", "")

            if views > TIKTOK_MAX_VIEWS: continue
            if followers > TIKTOK_MAX_FOLLOWERS: continue
            if not any(s in (caption + bio).lower() for s in
                       ["restaurant","café","cafe","pizza","burger","food",
                        "kitchen","chef","menu","hospitality","diner","eatery"]):
                continue
            if not is_recent(str(timestamp) if timestamp else ""):
                continue

            items.append({
                "platform": "tiktok",
                "url": url or f"https://tiktok.com/@{author.get('name','')}",
                "text": (f"TikTok caption: {caption}\nBio: {bio}\n"
                         f"Views: {views} | Followers: {followers}"),
                "tiktok_views": views,
                "timestamp": str(timestamp),
            })
        time.sleep(3)
    logger.info(f"TikTok: {len(items)} qualifying videos")
    return items


def scrape_instagram() -> list[dict]:
    """
    Scrape Instagram hashtags for low-engagement restaurant posts.
    Low likes + restaurant bio = owner posting manually with no strategy.
    """
    global _apify_limit_hit
    if _apify_limit_hit: return []
    items  = []
    for hashtag in INSTAGRAM_HASHTAGS:
        if _apify_limit_hit: break
        logger.info(f"  Instagram: #{hashtag}")
        try:
            posts = _apify_run("apify/instagram-hashtag-scraper", {
                "hashtags": [hashtag], "resultsLimit": 30,
            })
        except Exception as e:
            logger.error(f"Instagram #{hashtag} error: {e}"); continue
        for post in posts:
            likes     = post.get("likesCount", 9999)
            caption   = post.get("caption") or post.get("text", "")
            url       = post.get("url") or post.get("shortCode", "")
            if url and not url.startswith("http"):
                url = f"https://instagram.com/p/{url}"
            owner     = post.get("ownerFullName") or post.get("owner", {}).get("fullName", "")
            followers  = post.get("ownerFollowersCount") or \
                         post.get("owner", {}).get("followersCount", 9999)
            bio       = post.get("ownerBiography") or post.get("owner", {}).get("biography", "")
            timestamp = post.get("timestamp") or post.get("takenAt", "")

            if likes > INSTAGRAM_MAX_LIKES: continue
            if followers and followers > 2000: continue
            if not any(s in (caption + bio).lower() for s in
                       ["restaurant","café","cafe","pizza","burger","food",
                        "kitchen","chef","menu","hospitality","diner","eatery","owner"]):
                continue
            if not is_recent(timestamp):
                continue

            items.append({
                "platform": "instagram",
                "url": url,
                "text": (f"Instagram post: {caption[:500]}\n"
                         f"Owner: {owner} | Followers: {followers} | Likes: {likes}\n"
                         f"Bio: {bio}"),
                "timestamp": timestamp,
                "extra_signals": (
                    f"\nSIGNAL: This account has only {followers} followers "
                    f"and this post got {likes} likes — very low reach for a restaurant. "
                    "High probability they need social media help."
                ),
            })
        time.sleep(3)
    logger.info(f"Instagram: {len(items)} low-engagement posts")
    return items


async def _places_text_search(session: httpx.AsyncClient, query: str, location: str) -> list[dict]:
    """
    Google Places API (New) Text Search.
    Returns up to 20 places per call. We pull 3 pages = 60 per search term.
    Cost: ~$0.017 per call → full run of 37 searches × 3 pages = ~$1.89 total.
    """
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.rating,places.userRatingCount,"
            "places.websiteUri,places.nationalPhoneNumber,places.formattedAddress,"
            "places.googleMapsUri,places.regularOpeningHours,places.reviews,"
            "places.editorialSummary,places.priceLevel,places.businessStatus,"
            "nextPageToken"
        ),
    }
    all_places = []
    page_token = None

    for page in range(3):   # 3 pages = up to 60 places per search
        body = {
            "textQuery": f"{query} in {location}",
            "maxResultCount": 20,
            "languageCode": "en",
        }
        if page_token:
            body["pageToken"] = page_token

        try:
            r = await session.post(url, headers=headers, json=body, timeout=15)
            data = r.json()
            if r.status_code != 200:
                logger.error(f"Places API error {r.status_code}: {data.get('error',{}).get('message',data)}")
                break
            places = data.get("places", [])
            all_places.extend(places)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
            await asyncio.sleep(0.5)  # brief pause between pages
        except Exception as e:
            logger.error(f"Places API request error: {e}")
            break

    return all_places


def _score_place(place: dict) -> tuple[bool, list[str]]:
    """
    Score a place and return (qualifies, signals[]).
    A place qualifies if it hits ANY of our lead signals.
    """
    rating       = place.get("rating")
    review_count = place.get("userRatingCount", 0)
    website      = place.get("websiteUri", "")
    status       = place.get("businessStatus", "OPERATIONAL")
    price_level  = place.get("priceLevel", "")

    signals = []

    # Signal 1: low rating (struggling reputation = pain point)
    if rating and float(rating) <= PLACES_MIN_RATING:
        signals.append(f"{rating}★ rating ({review_count} reviews)")

    # Signal 2: no website (zero digital presence)
    if not website:
        signals.append("no website")

    # Signal 3: very few reviews (new or invisible business)
    if review_count and int(review_count) < 30:
        signals.append(f"only {review_count} reviews — low visibility")

    # Signal 4: budget/cheap restaurant (thin margins, needs volume)
    if price_level in ("PRICE_LEVEL_INEXPENSIVE", "PRICE_LEVEL_MODERATE"):
        signals.append("budget segment — needs volume")

    return len(signals) >= 1, signals


async def scrape_google_places() -> list[dict]:
    """
    Direct Google Places API (New) scraper — no Apify.
    Searches 37 city+type combos × up to 60 places = up to 2,220 restaurants checked.
    Filters for low rating, no website, or low review count.
    Cost per full run: ~$2-4 out of $200 monthly free credit.
    """
    items = []
    seen_place_ids: set[str] = set()

    async with httpx.AsyncClient() as session:
        for search in GOOGLE_PLACES_SEARCHES:
            query    = search["query"]
            location = search["location"]
            logger.info(f"  Google Places: {query} in {location}")

            places = await _places_text_search(session, query, location)
            qualified_this = 0

            for place in places:
                place_id = place.get("id", "")
                if place_id in seen_place_ids:
                    continue
                seen_place_ids.add(place_id)

                qualifies, signals = _score_place(place)
                if not qualifies:
                    continue

                name    = place.get("displayName", {}).get("text", "Unknown")
                rating  = place.get("rating", "N/A")
                website = place.get("websiteUri", "")
                phone   = place.get("nationalPhoneNumber", "")
                address = place.get("formattedAddress", "")
                gmaps   = place.get("googleMapsUri", "")
                summary = place.get("editorialSummary", {}).get("text", "")

                # Pull top 2 recent reviews for context
                reviews = place.get("reviews", [])
                review_snippets = []
                for r in reviews[:2]:
                    txt = r.get("text", {}).get("text", "")
                    if txt:
                        review_snippets.append(txt[:200])
                review_text = " | ".join(review_snippets)

                # Build the URL — prefer website, fall back to Google Maps link
                url = website or gmaps
                if not url:
                    continue

                signal_str = ", ".join(signals)
                market = (
                    "UK"     if any(c in location for c in ["UK", "London", "Manchester", "Birmingham",
                                                             "Leeds", "Bristol", "Edinburgh", "Glasgow",
                                                             "Liverpool", "Sheffield"]) else
                    "Canada" if any(c in location for c in ["Canada", "Toronto", "Vancouver",
                                                             "Calgary", "Edmonton", "Montreal", "Ottawa"]) else
                    "USA"
                )

                items.append({
                    "platform": "google",
                    "url": url,
                    "text": (
                        f"Restaurant: {name}\n"
                        f"Location: {address}\n"
                        f"Rating: {rating}★ | Reviews: {place.get('userRatingCount',0)}\n"
                        f"Website: {website or 'NONE — no web presence'}\n"
                        f"Phone: {phone}\n"
                        f"About: {summary}\n"
                        f"Recent reviews: {review_text}"
                    ),
                    "extra_signals": (
                        f"\nSIGNAL: {name} in {location} flagged for: {signal_str}. "
                        f"Market: {market}. "
                        f"{'No website found — zero digital presence. ' if not website else ''}"
                        f"This is a cold outreach email opportunity. "
                        f"Reference their specific location and pain points."
                    ),
                    "_market_hint": market,
                })
                qualified_this += 1

            logger.info(f"    → {len(places)} places checked, {qualified_this} qualified")
            await asyncio.sleep(0.3)  # be polite to the API

    logger.info(f"Google Places: {len(items)} target restaurants (deduped by place_id)")
    return items


def scrape_trustpilot() -> list[dict]:
    global _apify_limit_hit
    if _apify_limit_hit: return []
    items  = []
    try:
        results = _apify_run("apify/trustpilot-scraper", {
            "startUrls": [{"url": u} for u in TRUSTPILOT_CATEGORIES],
            "maxReviews": 30, "ratingFilter": [1, 2],
        })
    except Exception as e:
        logger.error(f"TrustPilot error: {e}"); return []
    for item in results:
        text = item.get("text") or item.get("reviewBody", "")
        url  = item.get("businessUrl") or item.get("url", "")
        if text:
            items.append({"platform": "trustpilot", "url": url,
                          "text": f"[TrustPilot 1-2★] {text}"})
    logger.info(f"TrustPilot: {len(items)} reviews")
    return items


def scrape_yelp() -> list[dict]:
    global _apify_limit_hit
    if _apify_limit_hit or not YELP_RESTAURANTS: return []
    items  = []
    try:
        results = _apify_run("apify/yelp-scraper", {
            "startUrls": [{"url": u} for u in YELP_RESTAURANTS], "maxReviews": 10,
        })
    except Exception as e:
        logger.error(f"Yelp error: {e}"); return []
    for item in results:
        if item.get("rating", 5) in [1, 2] and item.get("text"):
            items.append({
                "platform": "yelp", "url": item.get("businessUrl", ""),
                "text": f"[{item.get('rating')}★ Yelp] {item.get('text','')}",
            })
    return items


# ═══════════════════════════════════════════════════════════
# MASTER PIPELINE
# ═══════════════════════════════════════════════════════════
_pipeline_lock = asyncio.Lock()

async def run_pipeline(test_mode: bool = False):
    global _apify_limit_hit, _exhausted_tokens
    _apify_limit_hit = False
    _exhausted_tokens = set()  # reset per run — monthly limits reset on different dates

    if _pipeline_lock.locked():
        logger.warning("⚠️  Pipeline already running — skipping duplicate call")
        return {"skipped": True, "reason": "already running"}

    async with _pipeline_lock:
        start = time.monotonic()
        logger.info("=" * 60)
        logger.info("🚀 VERSAL DIGITAL SOLUTIONS — LEAN LEAD MACHINE v7")
        logger.info(f"   Gemini model: {GEMINI_MODEL}")
        logger.info(f"   Max post age: {MAX_POST_AGE_DAYS} days")
        logger.info("   Platforms: Facebook · Reddit · TikTok · Instagram · Google Maps")
        logger.info("   Markets:   🇺🇸 USA  🇬🇧 UK  🇨🇦 Canada")
        logger.info("=" * 60)

        if test_mode:
            logger.info("TEST MODE — no Apify credits used")
            from datetime import timedelta
            recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            old    = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
            raw_items = [
                # Should qualify — recent, owner, UK, visibility pain
                {"platform": "facebook", "url": "https://facebook.com/test1",
                 "text": "I run a burger spot in Manchester. Instagram for 2 years, still only 80-100 views per video. Revenue down 25%. Just Eat taking 35% commission.",
                 "timestamp": recent},
                # Should qualify — recent, owner, USA
                {"platform": "reddit", "url": "https://reddit.com/test2",
                 "text": "Running a burger joint in Austin TX. DoorDash killing our margins, barely any Instagram presence.",
                 "timestamp": recent},
                # Should be DROPPED — 365 days old
                {"platform": "reddit", "url": "https://reddit.com/test_old",
                 "text": "I own a pizza restaurant, struggling to get customers, no social media presence at all.",
                 "timestamp": old},
                # Should qualify — low views, low followers, recent
                {"platform": "tiktok", "url": "https://tiktok.com/@leedscafe/video/123",
                 "text": "TikTok caption: Made 50 croissants today 😭\nBio: Owner of The Corner Café, Leeds\nViews: 47 | Followers: 230",
                 "tiktok_views": 47, "timestamp": recent},
                # Should qualify — Google Maps cold outreach signal
                {"platform": "google", "url": "https://maps.google.com/?cid=test1",
                 "text": "Google Maps: Mario's Pizza NYC (2.8★). Signals: low rating, no website. Recent reviews: food is ok but never see them online.",
                 "extra_signals": "\nSIGNAL: Restaurant found via Google Maps with these flags: 2.8★ rating, no website. Cold outreach opportunity."},
                # Should qualify — Instagram low engagement
                {"platform": "instagram", "url": "https://instagram.com/p/abc123",
                 "text": "Instagram post: Check out our new menu! Come visit us 🍕\nOwner: Tony Rossi | Followers: 180 | Likes: 3\nBio: Owner of Rossi's Pizzeria, Toronto",
                 "timestamp": recent,
                 "extra_signals": "\nSIGNAL: This account has only 180 followers and this post got 3 likes — very low reach for a restaurant."},
            ]
        else:
            since_date = await get_last_run_date()
            raw_items  = []
            if ACTIVE_PLATFORMS.get("facebook"):   raw_items += scrape_facebook_groups(since_date)
            if ACTIVE_PLATFORMS.get("reddit"):     raw_items += scrape_reddit(since_date)
            if ACTIVE_PLATFORMS.get("tiktok"):     raw_items += scrape_tiktok()
            if ACTIVE_PLATFORMS.get("instagram"):  raw_items += scrape_instagram()
            if ACTIVE_PLATFORMS.get("google"):     raw_items += await scrape_google_places()
            if ACTIVE_PLATFORMS.get("trustpilot"): raw_items += scrape_trustpilot()
            if ACTIVE_PLATFORMS.get("yelp"):       raw_items += scrape_yelp()

            if _apify_limit_hit:
                logger.warning("⚠️  Apify monthly limit hit — processing whatever was collected")

            seen      = await load_seen_urls()
            before    = len(raw_items)
            raw_items = [i for i in raw_items if is_new(i.get("url", ""), seen)]
            logger.info(f"Dedup: {before - len(raw_items)} skipped, {len(raw_items)} new")

        n = len(raw_items)
        logger.info(f"Processing {n} items (hard date filter: >{MAX_POST_AGE_DAYS}d dropped before Gemini)")
        qualified: list[LeadOutput] = []

        for i, item in enumerate(raw_items):
            logger.info(f"\n[{i+1}/{n}][{item['platform'].upper()}] {item['text'][:80]}...")
            lead = process_single_lead(
                text=item["text"],
                platform=item["platform"],
                url=item["url"],
                tiktok_views=item.get("tiktok_views"),
                timestamp=item.get("timestamp"),
                extra_signals=item.get("extra_signals", ""),
            )
            if lead:
                qualified.append(lead)
                logger.info(f"  ✅ QUALIFIED — Score:{lead.intent_score} | "
                            f"{lead.market} | {lead.problem_category} | Age:{lead.post_age_days}d")

        logger.info(f"\n{'='*60}")
        logger.info(f"Qualified: {len(qualified)} / {n}")

        by_market, by_platform = {}, {}
        for lead in qualified:
            by_market[lead.market]            = by_market.get(lead.market, 0) + 1
            by_platform[lead.source_platform] = by_platform.get(lead.source_platform, 0) + 1

        for lead in qualified:
            logger.info(f"Delivering [{lead.source_platform}] score:{lead.intent_score} "
                        f"{lead.market} age:{lead.post_age_days}d...")
            await deliver_lead(lead)

        elapsed = round(time.monotonic() - start, 1)
        logger.info(f"\n✅ DONE — {len(qualified)} leads delivered in {elapsed}s")
        return {
            "leads_delivered": len(qualified),
            "total_processed": n,
            "by_market": by_market,
            "by_platform": by_platform,
            "seconds": elapsed,
            "apify_limit_hit": _apify_limit_hit,
        }


if __name__ == "__main__":
    # test_mode=True  → instant run, no Apify credits
    # test_mode=False → full live run
    result = asyncio.run(run_pipeline(test_mode=True))
    print("\n", json.dumps(result, indent=2))
