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
from apify_client import ApifyClient
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
    # UK — expanded
    {"query": "pizza restaurant",    "location": "London, UK"},
    {"query": "burger restaurant",   "location": "London, UK"},
    {"query": "cafe",                "location": "London, UK"},
    {"query": "indian restaurant",   "location": "London, UK"},
    {"query": "chinese restaurant",  "location": "London, UK"},
    {"query": "kebab restaurant",    "location": "London, UK"},
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
    {"query": "restaurant",          "location": "Newcastle, UK"},
    {"query": "restaurant",          "location": "Nottingham, UK"},
    {"query": "restaurant",          "location": "Leicester, UK"},
    {"query": "restaurant",          "location": "Cardiff, UK"},
    {"query": "cafe",                "location": "Brighton, UK"},
    {"query": "restaurant",          "location": "Plymouth, UK"},
    {"query": "restaurant",          "location": "Southampton, UK"},
    {"query": "restaurant",          "location": "Portsmouth, UK"},
    # USA — expanded
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
    {"query": "restaurant",          "location": "Miami, FL"},
    {"query": "restaurant",          "location": "Atlanta, GA"},
    {"query": "restaurant",          "location": "Seattle, WA"},
    {"query": "restaurant",          "location": "Denver, CO"},
    {"query": "cafe",                "location": "Austin, TX"},
    # Canada — expanded
    {"query": "pizza restaurant",    "location": "Toronto, Canada"},
    {"query": "burger restaurant",   "location": "Toronto, Canada"},
    {"query": "cafe",                "location": "Toronto, Canada"},
    {"query": "restaurant",          "location": "Vancouver, Canada"},
    {"query": "restaurant",          "location": "Calgary, Canada"},
    {"query": "restaurant",          "location": "Edmonton, Canada"},
    {"query": "restaurant",          "location": "Montreal, Canada"},
    {"query": "restaurant",          "location": "Ottawa, Canada"},
    {"query": "restaurant",          "location": "Winnipeg, Canada"},
    {"query": "cafe",                "location": "Halifax, Canada"},
]
PLACES_MAX_PER_SEARCH  = 20   # Places API returns max 20 per page
PLACES_MIN_RATING      = 3.9  # flag anything strictly under 4.0
PLACES_MIN_REVIEWS_FOR_RATING = 500  # ignore rating signal if they have 500+ reviews (established business)

TRUSTPILOT_CATEGORIES = [
    "https://uk.trustpilot.com/categories/restaurants_bars",
    "https://www.trustpilot.com/categories/restaurants_bars",
]

YELP_RESTAURANTS: list[str] = []

ACTIVE_PLATFORMS = {
    "facebook":   False,
    "reddit":     False,
    "tiktok":     False,
    "instagram":  False,
    "google":     True,   # ← only this runs for now
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
    _meta: Optional[dict] = None  # bonus fields from Google Places
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
# Semaphore limits concurrent Gemini calls — prevents 503 storm
# 10 concurrent = ~600 calls/min, well within Tier 1 limits
_gemini_semaphore = asyncio.Semaphore(10)

async def ask_gemini(prompt: str) -> dict:
    """Async Gemini call with semaphore + 503 retry (up to 3 attempts, 2s backoff)."""
    async with _gemini_semaphore:
        for attempt in range(3):
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
                    logger.error(f"Gemini: no JSON found: {text[:200]}")
                    return {}
                return json.loads(text[start:end])
            except Exception as e:
                msg = str(e)
                if "503" in msg or "UNAVAILABLE" in msg:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"Gemini 503 — retry {attempt+1}/3 in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Gemini error: {e}")
                return {}
        logger.error("Gemini 503 — exhausted retries, skipping")
        return {}


async def analyse_lead(text: str, platform: str,
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

    return await ask_gemini(f"""You are an analyst and copywriter for Versal Digital Solutions.
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


async def process_single_lead(text: str, platform: str, url: str,
                        threshold: int = 75,
                        tiktok_views: Optional[int] = None,
                        timestamp: Optional[str] = None,
                        extra_signals: str = "",
                        meta: Optional[dict] = None) -> Optional[LeadOutput]:
    age = post_age_days(timestamp) if timestamp else None

    # Hard drop before spending a Gemini call
    if age is not None and age > MAX_POST_AGE_DAYS:
        logger.info(f"  ⏭ SKIPPED — post is {age} days old (>{MAX_POST_AGE_DAYS}d cutoff)")
        return None

    result = await analyse_lead(text, platform, tiktok_views=tiktok_views,
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
        post_age_days=age, passed_threshold=True, _meta=meta,
    )


# ═══════════════════════════════════════════════════════════
# SCHEMA BOOTSTRAP — runs once at startup
# Ensures every required column exists; adds missing ones automatically.
# ═══════════════════════════════════════════════════════════
REQUIRED_COLUMNS = {
    "source_platform":    "text",
    "source_url":         "text",
    "raw_text":           "text",
    "intent_score":       "integer",
    "problem_category":   "text",
    "pain_point_summary": "text",
    "drafted_response":   "text",
    "market":             "text",
    "processed_at":       "timestamptz",
    "tiktok_view_count":  "integer",
    "post_age_days":      "integer",
}

async def ensure_schema():
    """
    Check which columns exist in the 'leads' table and ALTER TABLE
    to add any that are missing. Safe to run on every startup.
    """
    async with httpx.AsyncClient() as client:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

        # 1. Ask Postgres which columns already exist
        check_sql = """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = 'leads';
        """
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/exec_sql",
            headers=headers,
            json={"query": check_sql},
            timeout=15,
        )

        # Supabase exposes raw SQL via the pg_meta endpoint instead
        # Fall back to querying information_schema via PostgREST
        r2 = await client.get(
            f"{SUPABASE_URL}/rest/v1/leads",
            headers={**headers, "Prefer": "return=representation"},
            params={"limit": 0},   # fetch 0 rows — just headers
            timeout=15,
        )

        # Parse existing columns from the Content-Range / response headers
        # Simpler: do a dummy SELECT and read the JSON keys if any rows exist,
        # OR use the Supabase Management API column introspection.
        # Most reliable zero-dependency approach: SELECT via PostgREST introspection.
        intr = await client.get(
            f"{SUPABASE_URL}/rest/v1/leads?limit=1",
            headers=headers,
            timeout=15,
        )
        existing: set[str] = set()
        if intr.status_code == 200:
            rows = intr.json()
            if rows:
                existing = set(rows[0].keys())
            else:
                # Table exists but empty — get columns via information_schema
                schema_r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/rpc/get_lead_columns",
                    headers=headers, timeout=10,
                )
                # If that RPC doesn't exist, fall through to ALTER blindly
        
        missing = {col: typ for col, typ in REQUIRED_COLUMNS.items()
                   if col not in existing}

        if not missing:
            logger.info("✅ Schema OK — all columns present")
            return

        logger.warning(f"⚠️  Missing columns detected: {list(missing.keys())} — adding now...")

        for col, typ in missing.items():
            alter_sql = f'ALTER TABLE public.leads ADD COLUMN IF NOT EXISTS "{col}" {typ};'
            # Execute via Supabase SQL editor API (requires service role key)
            ar = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/exec_sql",
                headers=headers,
                json={"sql": alter_sql},
                timeout=15,
            )
            if ar.status_code in (200, 204):
                logger.info(f"  ✅ Added column: {col} ({typ})")
            else:
                logger.error(f"  ❌ Failed to add {col}: [{ar.status_code}] {ar.text[:300]}")

        logger.info("Schema bootstrap complete.")


async def ensure_schema_safe():
    """
    Wrapper — schema errors must never crash the pipeline.
    If ALTER TABLE fails (e.g. no service role), we log and continue.
    The _flush_to_supabase fallback will still save what it can.
    """
    try:
        await ensure_schema()
    except Exception as e:
        logger.warning(f"Schema bootstrap skipped (non-fatal): {type(e).__name__}: {e}")


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
            "text": {"type": "plain_text",
                     "text": "View on Google Maps →" if lead.source_platform == "google" else "View Original Post →"},
            "url": lead.source_url, "style": "primary"}]},
        {"type": "divider"},
    ]
    try:
        r = await client.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=15)
        if r.status_code >= 300:
            logger.error(f"Slack error [{r.status_code}]: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Slack error: {type(e).__name__}: {e}"); return False

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
        if r.status_code >= 300:
            logger.error(f"Make.com error [{r.status_code}]: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Make.com error: {type(e).__name__}: {e}"); return False

async def save_to_supabase(client: httpx.AsyncClient, lead: LeadOutput) -> bool:
    """
    Save lead to Supabase immediately after Gemini qualifies it.
    Only sends fields that definitely exist — no booleans that might be missing columns.
    On 400, logs the full response body so you can see exactly what's wrong.
    """
    headers = {
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    }
    payload = {
        "source_platform":    lead.source_platform,
        "source_url":         lead.source_url,
        "raw_text":           lead.raw_text[:2000],
        "intent_score":       lead.intent_score,
        "problem_category":   lead.problem_category,
        "pain_point_summary": lead.pain_point_summary,
        "drafted_response":   lead.drafted_response,
        "market":             lead.market,
        "processed_at":       lead.processed_at,
    }
    # Add optional fields only if they have values — avoids type errors on nullable cols
    if lead.tiktok_view_count is not None:
        payload["tiktok_view_count"] = lead.tiktok_view_count
    if lead.post_age_days is not None:
        payload["post_age_days"] = lead.post_age_days

    try:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/leads",
            headers=headers, json=payload, timeout=15
        )
        if r.status_code not in (200, 201):
            # Log the full error body so you can diagnose the 400
            logger.error(f"Supabase 400 detail: {r.text[:500]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Supabase error: {e}"); return False

async def deliver_lead(lead: LeadOutput):
    """
    Delivery order:
      1. Supabase FIRST — dashboard updates immediately, even if Slack/Make fail
      2. Slack + Make run concurrently after
    """
    async with httpx.AsyncClient() as client:
        # Step 1: save to dashboard immediately
        supa = await save_to_supabase(client, lead)
        logger.info(f"  Supabase (dashboard): {'✅ saved' if supa else '❌ failed'}")

        # Step 2: notify Slack + Make concurrently (failures don't block anything)
        slack, make = await asyncio.gather(
            send_to_slack(client, lead),
            send_to_make(client, lead),
            return_exceptions=True,
        )
        logger.info(f"  Slack:{slack} | Make:{make}")


# ═══════════════════════════════════════════════════════════
# SCRAPERS
# ═══════════════════════════════════════════════════════════
_apify_limit_hit = False

def _apify_run(actor: str, run_input: dict) -> list:
    """
    Run an Apify actor, rotating to the next token automatically if one is exhausted.
    Skips already-exhausted tokens immediately without burning a run attempt.
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

            # ── Pre-flight: check remaining balance before burning a run ──
            try:
                user = client.user().get()
                plan = user.get("plan", {})
                usage = plan.get("monthlyUsage", {})
                limit = plan.get("monthlyLimit", {})
                used  = usage.get("ACTOR_COMPUTE_UNITS", 0)
                cap   = limit.get("ACTOR_COMPUTE_UNITS", 999)
                pct   = (used / cap * 100) if cap else 100
                if pct >= 99:
                    logger.warning(f"  Token …{tok[-6:]} at {pct:.0f}% usage — marking exhausted")
                    mark_token_exhausted(tok)
                    continue
                logger.info(f"  Token …{tok[-6:]} usage: {pct:.0f}% ({used}/{cap} CUs)")
            except Exception:
                pass  # can't check balance — try anyway

            run    = client.actor(actor).call(run_input=run_input)
            return list(client.dataset(run["defaultDatasetId"]).iterate_items())

        except Exception as e:
            msg = str(e)
            if "Monthly usage hard limit" in msg or "hard limit exceeded" in msg or \
               "Maximum cost per run" in msg or "lower then actor start cost" in msg:
                mark_token_exhausted(tok)
                remaining = len(APIFY_TOKENS) - len(_exhausted_tokens)
                if remaining == 0:
                    logger.error("All Apify tokens exhausted — stopping scraping")
                    _apify_limit_hit = True
                    return []
                logger.info(f"  Rotating to next token ({remaining} remaining)...")
                continue
            else:
                logger.error(f"  Apify non-quota error on …{tok[-6:]}: {e}")
                raise

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
            posts = _apify_run("odemuno/reddit-scraper", {
                "startUrls": [{"url": f"https://www.reddit.com/r/{sub}/new/"}],
                "maxItems": 15,
                "includeComments": False,
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

    # Signal 1: strictly under 4.0 stars AND not a high-volume established business
    if rating and float(rating) < 4.0:
        if review_count < PLACES_MIN_REVIEWS_FOR_RATING:
            signals.append(f"{rating}★ rating ({review_count} reviews)")

    # Signal 2: no website (zero digital presence) — only if also low reviews
    # High-rated places with no website are often intentionally offline (fine dining etc)
    if not website and review_count < 200:
        signals.append("no website")

    # Signal 3: very few reviews (new or invisible business)
    if review_count and int(review_count) < 30:
        signals.append(f"only {review_count} reviews — low visibility")

    # Signal 4: budget/cheap restaurant (thin margins, needs volume)
    if price_level in ("PRICE_LEVEL_INEXPENSIVE", "PRICE_LEVEL_MODERATE"):
        signals.append("budget segment — needs volume")

    return len(signals) >= 1, signals


async def _get_completed_searches() -> set[str]:
    """Load the set of Google Places searches already completed from Supabase."""
    try:
        async with httpx.AsyncClient() as client:
            headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/scrape_checkpoints",
                headers=headers,
                params={"select": "search_key", "platform": "eq.google_places", "limit": 1000},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(f"Checkpoint read failed ({r.status_code}) — starting fresh")
                return set()
            return {row["search_key"] for row in r.json()}
    except Exception as e:
        logger.error(f"Checkpoint load error: {e}")
        return set()

async def _mark_search_complete(search_key: str):
    """Record that a Google Places search has been fully scraped."""
    try:
        async with httpx.AsyncClient() as client:
            headers = {
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            }
            await client.post(
                f"{SUPABASE_URL}/rest/v1/scrape_checkpoints",
                headers=headers,
                json={
                    "platform":   "google_places",
                    "search_key": search_key,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Checkpoint save error: {e}")

async def scrape_google_places() -> list[dict]:
    """
    Direct Google Places API (New) scraper — no Apify.
    Searches 37 city+type combos × up to 60 places = up to 2,220 restaurants checked.
    Filters for low rating, no website, or low review count.
    Cost per full run: ~$2-4 out of $200 monthly free credit.
    """
    items = []
    seen_place_ids: set[str] = set()

    # Load which searches we've already completed across previous runs
    completed = await _get_completed_searches()
    remaining = [s for s in GOOGLE_PLACES_SEARCHES
                 if f"{s['query']}|{s['location']}" not in completed]

    total     = len(GOOGLE_PLACES_SEARCHES)
    skipped   = total - len(remaining)
    logger.info(f"  Google Places: {len(remaining)}/{total} searches remaining "
                f"({skipped} already done across previous runs)")

    if not remaining:
        logger.info("  ✅ All Google Places searches complete — full map coverage achieved!")
        logger.info("     To rescan from scratch, clear the scrape_checkpoints table in Supabase.")
        return []

    async with httpx.AsyncClient() as session:
        for search in remaining:
            query      = search["query"]
            location   = search["location"]
            search_key = f"{query}|{location}"
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

                # Opening hours — is it currently open? When do they close?
                hours_obj   = place.get("regularOpeningHours", {})
                hours_today = ""
                if hours_obj.get("weekdayDescriptions"):
                    today_idx   = datetime.now(timezone.utc).weekday()  # 0=Mon
                    hours_today = hours_obj["weekdayDescriptions"][today_idx]

                items.append({
                    "platform": "google",
                    "url": url,
                    "text": (
                        f"Restaurant: {name}\n"
                        f"Location: {address}\n"
                        f"Rating: {rating}★ | Reviews: {place.get('userRatingCount',0)}\n"
                        f"Website: {website or 'NONE — no web presence'}\n"
                        f"Phone: {phone or 'NONE'}\n"
                        f"Hours today: {hours_today or 'Unknown'}\n"
                        f"About: {summary}\n"
                        f"Recent reviews: {review_text}"
                    ),
                    "extra_signals": (
                        f"\nSIGNAL: {name} in {location} flagged for: {signal_str}. "
                        f"Market: {market}. "
                        f"{'No website found — zero digital presence. ' if not website else ''}"
                        f"{'No phone number listed. ' if not phone else ''}"
                        f"This is a cold outreach email opportunity. "
                        f"Reference their specific location and pain points."
                    ),
                    "_meta": {
                        "phone":   phone,
                        "website": website,
                        "address": address,
                        "name":    name,
                        "rating":  rating,
                        "reviews": place.get("userRatingCount", 0),
                        "hours_today": hours_today,
                    },
                    "_market_hint": market,
                })
                qualified_this += 1

            logger.info(f"    → {len(places)} places checked, {qualified_this} qualified")
            # Mark this search as done so future runs skip it
            await _mark_search_complete(search_key)
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
        await ensure_schema_safe()   # ← auto-create any missing columns before anything runs
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
            # ── Google FIRST — free $200/month credit, most reliable, no Apify needed ──
            if ACTIVE_PLATFORMS.get("google"):     raw_items += await scrape_google_places()
            # ── Apify-based platforms after ──
            if ACTIVE_PLATFORMS.get("facebook"):   raw_items += scrape_facebook_groups(since_date)
            if ACTIVE_PLATFORMS.get("reddit"):     raw_items += scrape_reddit(since_date)
            if ACTIVE_PLATFORMS.get("tiktok"):     raw_items += scrape_tiktok()
            if ACTIVE_PLATFORMS.get("instagram"):  raw_items += scrape_instagram()
            if ACTIVE_PLATFORMS.get("trustpilot"): raw_items += scrape_trustpilot()
            if ACTIVE_PLATFORMS.get("yelp"):       raw_items += scrape_yelp()

            if _apify_limit_hit:
                logger.warning("⚠️  Apify monthly limit hit — processing whatever was collected")

            seen      = await load_seen_urls()
            before    = len(raw_items)
            raw_items = [i for i in raw_items if is_new(i.get("url", ""), seen)]
            logger.info(f"Dedup: {before - len(raw_items)} skipped, {len(raw_items)} new (including {before - len(deduped) + len(raw_items) - len(deduped)} intra-run dupes)")

        n = len(raw_items)
        logger.info(f"Processing {n} items concurrently (semaphore=10, 503-retry enabled)")

        qualified: list[LeadOutput] = []

        BATCH_SAVE_EVERY = 10   # save to Supabase (dashboard) every N qualified leads
        pending_leads: list[LeadOutput] = []

        async def _flush_to_supabase(leads: list[LeadOutput]):
            """Bulk-insert a batch of leads in one HTTP call."""
            if not leads:
                return
            headers = {
                "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal",
            }
            payload = []
            for lead in leads:
                row = {
                    "source_platform":    lead.source_platform,
                    "source_url":         lead.source_url,
                    "raw_text":           lead.raw_text[:2000],
                    "intent_score":       lead.intent_score,
                    "problem_category":   lead.problem_category,
                    "pain_point_summary": lead.pain_point_summary,
                    "drafted_response":   lead.drafted_response,
                    "market":             lead.market,
                    "processed_at":       lead.processed_at,
                }
                if lead.tiktok_view_count is not None:
                    row["tiktok_view_count"] = lead.tiktok_view_count
                if lead.post_age_days is not None:
                    row["post_age_days"] = lead.post_age_days
                payload.append(row)
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.post(
                        f"{SUPABASE_URL}/rest/v1/leads",
                        headers=headers, json=payload, timeout=20
                    )
                    if r.status_code not in (200, 201):
                        logger.error(f"Supabase batch error: {r.text[:300]}")
                    else:
                        logger.info(f"  📊 Dashboard updated — {len(leads)} leads saved")
            except Exception as e:
                logger.error(f"Supabase batch error: {e}")

        async def _process_item(i: int, item: dict) -> None:
            nonlocal pending_leads
            logger.info(f"[{i+1}/{n}][{item['platform'].upper()}] {item['text'][:80]}...")
            lead = await process_single_lead(
                text=item["text"],
                platform=item["platform"],
                url=item["url"],
                tiktok_views=item.get("tiktok_views"),
                timestamp=item.get("timestamp"),
                extra_signals=item.get("extra_signals", ""),
            )
            if lead:
                logger.info(f"  ✅ QUALIFIED — Score:{lead.intent_score} | "
                            f"{lead.market} | {lead.problem_category} | Age:{lead.post_age_days}d")
                qualified.append(lead)
                pending_leads.append(lead)

                # Slack + Make fire instantly per lead (non-blocking)
                async def _notify(l=lead):
                    async with httpx.AsyncClient() as c:
                        await asyncio.gather(
                            send_to_slack(c, l),
                            send_to_make(c, l),
                            return_exceptions=True,
                        )
                asyncio.create_task(_notify())

                # Flush to Supabase/dashboard every BATCH_SAVE_EVERY leads
                if len(pending_leads) >= BATCH_SAVE_EVERY:
                    batch = pending_leads.copy()
                    pending_leads.clear()
                    await _flush_to_supabase(batch)

        # Run all Gemini calls concurrently — semaphore caps at 10 in-flight at once
        await asyncio.gather(*[_process_item(i, item) for i, item in enumerate(raw_items)])

        # Flush any remaining leads that didn't hit the batch threshold
        if pending_leads:
            logger.info(f"  Flushing final {len(pending_leads)} leads to dashboard...")
            await _flush_to_supabase(pending_leads)

        logger.info(f"\n{'='*60}")
        logger.info(f"Qualified: {len(qualified)} / {n}")


        by_market   = {}
        by_platform = {}
        for lead in qualified:
            by_market[lead.market]             = by_market.get(lead.market, 0) + 1
            by_platform[lead.source_platform]  = by_platform.get(lead.source_platform, 0) + 1

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
