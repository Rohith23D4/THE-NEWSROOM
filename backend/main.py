import threading
import time
import hashlib
import hmac
import os
import re
import secrets
import requests
from datetime import datetime, timedelta, timezone
import latest_registry
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from news_service import (
    get_news,
    search_news,
    CATEGORY_FEEDS,
    LATEST_HOT_FEEDS,
    get_publisher_description,
    description_matches_title,
)
from ollama_service import (
    summarize_news,
    normalize_analysis,
    analyze_jobs_career,
    explain_like_im_10,
    compare_articles,
    translate_article_text,
)

from latest_registry import (
    LATEST_HOT_URL_REGISTRY,
    LATEST_HOT_URL_REGISTRY_LOCK,
    LATEST_HOT_URL_REGISTRY_TTL,
    LATEST_HOT_URL_REGISTRY_UPDATED,
)

from database import (
    get_connection,
    initialize_database,
    get_cached_summary,
    save_summary,
    get_cached_jobs_intelligence,
    save_jobs_intelligence,
)


# ============================================================
# THE NEWSROOM API
# ============================================================

app = FastAPI(
    title="Local Newsroom",
    description="AI-Powered Local News Intelligence",
    version="4.0.0",
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# CATEGORY MAP
# ============================================================

CATEGORY_MAP = {
    "Latest": "latest",
    "India": "india",

    "Telangana": "telangana",
    "Andhra Pradesh": "andhrapradesh",
    "Karnataka": "karnataka",
    "Tamil Nadu": "tamilnadu",
    "Kerala": "kerala",
    "Maharashtra": "maharashtra",
    "Delhi": "delhi",

    "World": "world",
    "AI": "ai",
    "Technology": "technology",
    "Business": "business",
    "Stocks": "stocks",
    "Startups": "startups",
    "Weather": "weather",
    "Sports": "sports",
    "Entertainment": "entertainment",
    "Jobs": "jobs",
}

# ============================================================
# STARTUP
# ============================================================



# Dedicated feeds for the LATEST & HOT section.
# These are independent from the normal category feeds.
# ============================================================
# LATEST & HOT RSS CACHE
# ============================================================

LATEST_HOT_CACHE_TTL = 5 * 60
LATEST_HOT_MAX_ITEMS_PER_FEED = 20

LATEST_HOT_CACHE = {}

LATEST_HOT_CACHE_LOCK = threading.Lock()

# Cache complete category responses briefly so refreshes and visits from
# separate browser sessions do not repeat the article-page extraction work.
CATEGORY_RESPONSE_CACHE_TTL = 10 * 60
CATEGORY_RESPONSE_CACHE_MAX_ENTRIES = 32
CATEGORY_RESPONSE_CACHE = {}
CATEGORY_RESPONSE_CACHE_LOCK = threading.Lock()

# The ticker already caches individual feeds. Caching the assembled response
# avoids repeating parsing, de-duplication, and ranking on every page refresh.
TRENDING_RESPONSE_CACHE_TTL = 5 * 60
TRENDING_RESPONSE_CACHE = None
TRENDING_RESPONSE_CACHE_LOCK = threading.Lock()

# Translations are deterministic for the same article and language. Keep a
# small bounded cache so selecting the same language again is immediate.
TRANSLATION_CACHE_TTL = 12 * 60 * 60
TRANSLATION_CACHE_MAX_ENTRIES = 512
TRANSLATION_CACHE = {}
TRANSLATION_CACHE_LOCK = threading.Lock()

# Reuse explanations for repeat clicks on the same article.
ELI10_CACHE = {}
ELI10_CACHE_LOCK = threading.Lock()
ELI10_CACHE_MAX_ENTRIES = 512

# Cache identical news searches briefly for instant repeat lookups.
SEARCH_CACHE_TTL = 90
SEARCH_CACHE_MAX_ENTRIES = 128
SEARCH_CACHE = {}
SEARCH_CACHE_LOCK = threading.Lock()

# Jobs articles are returned independently of local AI generation. Ollama
# can take up to two minutes, so keeping it out of the news request prevents
# the entire Jobs page from appearing broken while analysis runs.
JOBS_INTELLIGENCE_LOCK = threading.Lock()
JOBS_INTELLIGENCE_TASKS = {}

AUTH_SESSION_COOKIE = "newsroom_session"
AUTH_SESSION_DAYS = 30
AUTH_PASSWORD_ITERATIONS = 310_000
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()


class AuthCredentials(BaseModel):
    email: str
    password: str


class GoogleCredential(BaseModel):
    credential: str


@app.get("/auth/google/config")
def google_auth_config():
    return {"client_id": GOOGLE_CLIENT_ID}


@app.post("/auth/google")
def google_login(payload: GoogleCredential, request: Request, response: Response):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this server yet.")

    try:
        verification = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": payload.credential},
            timeout=8,
        )
        if verification.status_code != 200:
            raise ValueError("Google did not accept this sign-in token.")
        claims = verification.json()
    except (requests.RequestException, ValueError) as error:
        raise HTTPException(status_code=401, detail="Google sign-in could not be verified. Please try again.") from error

    if (
        claims.get("aud") != GOOGLE_CLIENT_ID
        or claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
        or claims.get("email_verified") not in {True, "true"}
        or not claims.get("sub")
        or not claims.get("email")
    ):
        raise HTTPException(status_code=401, detail="Google sign-in could not be verified.")

    email = _normalize_email(claims["email"])
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=401, detail="Google did not provide a valid email address.")

    connection = get_connection()
    try:
        user = connection.execute(
            "SELECT id, email FROM user_accounts WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
        if user:
            user_id = user["id"]
            email = user["email"]
        else:
            salt = secrets.token_hex(16)
            random_password = secrets.token_urlsafe(48)
            cursor = connection.execute(
                "INSERT INTO user_accounts (email, password_salt, password_hash) VALUES (?, ?, ?)",
                (email, salt, _password_digest(random_password, salt)),
            )
            connection.commit()
            user_id = cursor.lastrowid
    except Exception as error:
        connection.rollback()
        if "UNIQUE constraint failed" in str(error):
            user = connection.execute(
                "SELECT id, email FROM user_accounts WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if not user:
                raise
            user_id, email = user["id"], user["email"]
        else:
            raise
    finally:
        connection.close()

    return _issue_session(user_id, email, request, response)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _validate_credentials(payload: AuthCredentials) -> tuple[str, str]:
    email = _normalize_email(payload.email)
    password = payload.password
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(password) < 8 or len(password) > 128:
        raise HTTPException(status_code=400, detail="Password must be 8 to 128 characters.")
    return email, password


def _jobs_intelligence_status_key(cache_key: str) -> str:
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()


def _run_jobs_intelligence(cache_key: str, status_key: str, articles: list[dict]):
    try:
        result = analyze_jobs_career(articles[:20])
        save_jobs_intelligence(cache_key, result)
        state = {"status": "complete", "result": result}
    except Exception as error:
        print("JOBS INTELLIGENCE BACKGROUND ERROR:", error)
        state = {"status": "failed", "result": None}

    with JOBS_INTELLIGENCE_LOCK:
        JOBS_INTELLIGENCE_TASKS[status_key] = state


def _schedule_jobs_intelligence(cache_key: str, articles: list[dict]) -> str:
    status_key = _jobs_intelligence_status_key(cache_key)
    with JOBS_INTELLIGENCE_LOCK:
        existing = JOBS_INTELLIGENCE_TASKS.get(status_key)
        if existing and existing.get("status") == "pending":
            return status_key
        JOBS_INTELLIGENCE_TASKS[status_key] = {"status": "pending", "result": None}

    threading.Thread(
        target=_run_jobs_intelligence,
        args=(cache_key, status_key, list(articles)),
        daemon=True,
        name="jobs-intelligence",
    ).start()
    return status_key


def _password_digest(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), AUTH_PASSWORD_ITERATIONS
    ).hex()


def _issue_session(user_id: int, email: str, request: Request, response: Response):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=AUTH_SESSION_DAYS)
    connection = get_connection()
    try:
        connection.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))
        connection.execute(
            "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
    response.set_cookie(
        AUTH_SESSION_COOKIE, token, max_age=AUTH_SESSION_DAYS * 24 * 60 * 60,
        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/",
    )
    return {"authenticated": True, "user": {"email": email}}


@app.post("/auth/register")
def register(payload: AuthCredentials, request: Request, response: Response):
    email, password = _validate_credentials(payload)
    salt = secrets.token_hex(16)
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO user_accounts (email, password_salt, password_hash) VALUES (?, ?, ?)",
            (email, salt, _password_digest(password, salt)),
        )
        connection.commit()
        user_id = cursor.lastrowid
    except Exception as error:
        connection.rollback()
        if "UNIQUE constraint failed" in str(error):
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        raise
    finally:
        connection.close()
    return _issue_session(user_id, email, request, response)


@app.post("/auth/login")
def login(payload: AuthCredentials, request: Request, response: Response):
    email, password = _validate_credentials(payload)
    connection = get_connection()
    try:
        user = connection.execute(
            "SELECT id, email, password_salt, password_hash FROM user_accounts WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()
    finally:
        connection.close()
    if not user or not hmac.compare_digest(_password_digest(password, user["password_salt"]), user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return _issue_session(user["id"], user["email"], request, response)


@app.get("/auth/me")
def current_user(request: Request):
    token = request.cookies.get(AUTH_SESSION_COOKIE)
    if not token:
        return {"authenticated": False}
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT a.email, s.expires_at FROM user_sessions s JOIN user_accounts a ON a.id = s.user_id WHERE s.token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row:
            return {"authenticated": False}
        if row["expires_at"] <= now:
            connection.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))
            connection.commit()
            return {"authenticated": False}
        return {"authenticated": True, "user": {"email": row["email"]}}
    finally:
        connection.close()


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(AUTH_SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        connection = get_connection()
        try:
            connection.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))
            connection.commit()
        finally:
            connection.close()
    response.delete_cookie(AUTH_SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"authenticated": False}

# URLs selected by the Latest/Hot feed.
# Other category feeds can use this registry to avoid
# returning the same stories.


FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://feeds.skynews.com/feeds/rss/home.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

@app.on_event("startup")
def startup_event():
    initialize_database()
    print("DATABASE INITIALIZED")


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():
    return FileResponse("../frontend/index.html")


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    database_status = "healthy"
    ollama_status = "healthy"

    # Check SQLite
    try:
        connection = get_connection()
        connection.execute("SELECT 1")
        connection.close()
    except Exception as error:
        database_status = "unhealthy"
        print("HEALTH DATABASE ERROR:", error)

    # Check Ollama
    try:
        import requests

        ollama_health_url = "http://127.0.0.1:11434/api/tags"

        response = requests.get(
            ollama_health_url,
            timeout=1.5
        )

        if response.status_code != 200:
            ollama_status = "unhealthy"

    except Exception as error:
        ollama_status = "unhealthy"
        print("HEALTH OLLAMA ERROR:", error)

    overall_status = (
        "healthy"
        if database_status == "healthy"
        and ollama_status == "healthy"
        else "degraded"
    )

    return {
        "status": overall_status,
        "service": "Local Newsroom",
        "ai": {
            "service": "Ollama",
            "status": ollama_status,
        },
        "database": {
            "service": "SQLite",
            "status": database_status,
        },
    }


# ============================================================
# CATEGORIES
# ============================================================

@app.get("/categories")
def categories():
    return {
        "categories": list(CATEGORY_MAP.keys())
    }


# ============================================================
# NEWS BY CATEGORY
# ============================================================

@app.get("/news")
def news(category: str = "Latest"):

    requested_category = category.strip()

    matched_category = None

    # Support both display names and API-friendly lowercase aliases.
    category_aliases = {
        "latest": "Latest",
        "india": "India",
        "telangana": "Telangana",
        "andhrapradesh": "Andhra Pradesh",
        "karnataka": "Karnataka",
        "tamilnadu": "Tamil Nadu",
        "tamil nadu": "Tamil Nadu",
        "kerala": "Kerala",
        "maharashtra": "Maharashtra",
        "delhi": "Delhi",
        "world": "World",
        "ai": "AI",
        "technology": "Technology",
        "business": "Business",
        "stocks": "Stocks",
        "startups": "Startups",
        "weather": "Weather",
        "sports": "Sports",
        "entertainment": "Entertainment",
        "jobs": "Jobs",
    }

    normalized_category = requested_category.lower().strip()

    if normalized_category in category_aliases:
        matched_category = category_aliases[normalized_category]
    else:
        for available_category in CATEGORY_MAP:
            if available_category.lower() == normalized_category:
                matched_category = available_category
                break

    if matched_category is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category: {requested_category}"
        )

    search_category = CATEGORY_MAP[matched_category]

    print("=" * 70)
    print("NEWS REQUEST")
    print("=" * 70)
    print("REQUESTED CATEGORY:", requested_category)
    print("SEARCH CATEGORY:", search_category)

    try:

        # The Latest category uses its own India-focused feeds. The
        # independent Latest & Hot strip uses worldwide publisher feeds.
        india_today = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        ).date().isoformat()
        cache_key = f"{search_category}:{india_today}"
        cached_response = None
        now_monotonic = time.monotonic()
        with CATEGORY_RESPONSE_CACHE_LOCK:
            cached_entry = CATEGORY_RESPONSE_CACHE.get(cache_key)
            if (
                cached_entry
                and now_monotonic - cached_entry[0]
                < cached_entry[2]
            ):
                cached_response = [
                    dict(article) for article in cached_entry[1]
                ]

        articles = (
            cached_response
            if cached_response is not None
            else get_news(search_category)
        )[:20]

        # Cache only complete, balanced responses. A partial response must
        # not become the category's short-term answer on the next visit.
        if cached_response is None and len(articles) == 20:
            source_counts = {}
            for article in articles:
                source = str(article.get("source", "Unknown")).strip() or "Unknown"
                source_counts[source] = source_counts.get(source, 0) + 1
            is_balanced_full_response = (
                len(articles) == 20
                and 4 <= len(source_counts) <= 6
            )
            if is_balanced_full_response:
                with CATEGORY_RESPONSE_CACHE_LOCK:
                    CATEGORY_RESPONSE_CACHE[cache_key] = (
                        time.monotonic(),
                        [dict(article) for article in articles],
                        CATEGORY_RESPONSE_CACHE_TTL,
                    )
                    while len(CATEGORY_RESPONSE_CACHE) > CATEGORY_RESPONSE_CACHE_MAX_ENTRIES:
                        CATEGORY_RESPONSE_CACHE.pop(next(iter(CATEGORY_RESPONSE_CACHE)))

        print(
            "CATEGORY RESPONSE CACHE:",
            "HIT" if cached_response is not None else "MISS",
            search_category,
        )

        # Keep recently served category stories out of the worldwide strip.
        now = time.time()
        with LATEST_HOT_URL_REGISTRY_LOCK:
            expired_category_urls = [
                url
                for url, entry in latest_registry.CATEGORY_ARTICLE_REGISTRY.items()
                if now - entry.get("updated_at", 0) > latest_registry.CATEGORY_ARTICLE_REGISTRY_TTL
            ]
            for url in expired_category_urls:
                latest_registry.CATEGORY_ARTICLE_REGISTRY.pop(url, None)

            for article in articles:
                article_url = str(article.get("url", "")).strip()
                article_title = str(article.get("title", "")).strip()
                if not article_url or not article_title:
                    continue
                key = article_url.split("#", 1)[0].rstrip("/").lower()
                latest_registry.CATEGORY_ARTICLE_REGISTRY[key] = {
                    "title": article_title,
                    "updated_at": now,
                }

            if len(latest_registry.CATEGORY_ARTICLE_REGISTRY) > latest_registry.CATEGORY_ARTICLE_REGISTRY_MAX:
                newest_category_articles = sorted(
                    latest_registry.CATEGORY_ARTICLE_REGISTRY.items(),
                    key=lambda item: item[1].get("updated_at", 0),
                    reverse=True,
                )[:latest_registry.CATEGORY_ARTICLE_REGISTRY_MAX]
                latest_registry.CATEGORY_ARTICLE_REGISTRY.clear()
                latest_registry.CATEGORY_ARTICLE_REGISTRY.update(newest_category_articles)

            # The Latest category is separate from the worldwide strip but
            # should still stay unique from the other category pages.
            if search_category == "latest":
                latest_registry.LATEST_HOT_URL_REGISTRY.update(
                    str(article.get("url", "")).strip().lower()
                    for article in articles
                    if article.get("url")
                )
                latest_registry.LATEST_HOT_URL_REGISTRY_UPDATED = now

        response = {
            "category": matched_category,
            "search_category": search_category,
            "articles": articles,
        }

        # ====================================================
        # JOBS & CAREER INTELLIGENCE
        # ====================================================

        if search_category == "jobs":

            print("=" * 70)
            print("JOBS INTELLIGENCE REQUEST")
            print("=" * 70)

            # Create a stable cache key from the current
            # 20 Jobs article titles.
            article_titles = [
                str(article.get("title", "")).strip()
                for article in articles[:20]
                if str(article.get("title", "")).strip()
            ]

            cache_key = "|".join(article_titles)

            cached_result = None

            if cache_key:

                cached_result = get_cached_jobs_intelligence(
                    cache_key
                )

            if cached_result is not None:

                print("JOBS INTELLIGENCE CACHE: HIT")

                response["jobs_intelligence"] = cached_result
                response["jobs_intelligence_cached"] = True
                response["jobs_intelligence_pending"] = False

            else:

                print("JOBS INTELLIGENCE CACHE: MISS")
                status_key = _schedule_jobs_intelligence(cache_key, articles)
                response["jobs_intelligence_key"] = status_key
                response["jobs_intelligence"] = None
                response["jobs_intelligence_cached"] = False
                response["jobs_intelligence_pending"] = True

        return response

    except Exception as error:

        import traceback
        print("NEWS ERROR:", error)
        traceback.print_exc()

        raise HTTPException(
            status_code=500,
            detail=f"Unable to fetch news: {str(error)}",
        )


@app.get("/jobs-intelligence")
def jobs_intelligence_status(key: str):
    with JOBS_INTELLIGENCE_LOCK:
        state = JOBS_INTELLIGENCE_TASKS.get(key)
        return dict(state) if state else {"status": "pending", "result": None}



# ============================================================
# TRENDING NEWS
# ============================================================









@app.get("/trending")
def get_trending():
    """
    LATEST & HOT

    Fetch fresh RSS stories, use publication time,
    sort newest first, remove duplicate events,
    and return the newest 20 unique stories.
    """

    import re
    import html
    import urllib.request
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    from difflib import SequenceMatcher
    from concurrent.futures import ThreadPoolExecutor, as_completed

    global TRENDING_RESPONSE_CACHE

    now_monotonic = time.monotonic()
    with TRENDING_RESPONSE_CACHE_LOCK:
        cached_entry = TRENDING_RESPONSE_CACHE
        if (
            cached_entry
            and now_monotonic - cached_entry[0] < TRENDING_RESPONSE_CACHE_TTL
        ):
            cached_articles = [dict(article) for article in cached_entry[1]["articles"]]
            with LATEST_HOT_URL_REGISTRY_LOCK:
                LATEST_HOT_URL_REGISTRY.clear()
                LATEST_HOT_URL_REGISTRY.update(
                    str(article.get("url", "")).strip().lower()
                    for article in cached_articles
                    if article.get("url")
                )
                latest_registry.LATEST_HOT_URL_REGISTRY_UPDATED = time.time()
            return {
                **cached_entry[1],
                "articles": cached_articles,
                "cached": True,
            }

    FEEDS = LATEST_HOT_FEEDS

    # Warm expired feeds concurrently so one slow publisher cannot make the
    # complete ticker wait behind every other source. Fresh cache entries are
    # left untouched and will be read by the normal feed loop below.
    prefetched_feed_data = {}

    def prefetch_feed(feed_url):
        now = datetime.now(timezone.utc)
        with LATEST_HOT_CACHE_LOCK:
            cached = LATEST_HOT_CACHE.get(feed_url)
            if cached and (now - cached[0]).total_seconds() < LATEST_HOT_CACHE_TTL:
                return feed_url, None, False

        try:
            request = urllib.request.Request(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return feed_url, response.read(), True
        except Exception as error:
            print("LATEST & HOT PREFETCH ERROR:", feed_url, error)
            return feed_url, None, True

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(FEEDS)))) as executor:
        futures = [executor.submit(prefetch_feed, feed_url) for _, feed_url, _ in FEEDS]
        for future in as_completed(futures):
            feed_url, xml_data, attempted = future.result()
            if attempted:
                prefetched_feed_data[feed_url] = xml_data

    def clean(value):
        value = value or ""
        value = re.sub(r"<[^>]+>", " ", value)
        value = html.unescape(value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def normalize(value):
        value = clean(value).lower()
        value = re.sub(r"[^a-z0-9\s]", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def parse_date(value):
        value = clean(value)

        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)

        try:
            dt = parsedate_to_datetime(value)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(timezone.utc)

        except Exception:
            pass

        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(value, fmt)

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

                return dt.astimezone(timezone.utc)

            except Exception:
                continue

        return datetime.min.replace(tzinfo=timezone.utc)

    STOP = {
        "the","a","an","and","or","of","to","in","on","for",
        "with","from","after","before","as","at","by","is",
        "are","was","were","has","have","had","this","that",
        "these","those","new","news","says","said","report",
        "reports","latest"
    }

    def important_words(text):
        return {
            w for w in normalize(text).split()
            if len(w) >= 3 and w not in STOP
        }

    def compact_description(title, value, max_chars=900):
        """Clean RSS copy without adding facts or navigation filler."""
        text = clean(value)
        if not text:
            return ""

        boilerplate = re.compile(
            r"(?:\s*(?:continue reading|read more|click here|read the full story)"
            r"\s*(?:\.{2,}|…)?\s*)+$",
            re.IGNORECASE,
        )
        text = clean(boilerplate.sub("", text))
        if not text:
            return ""

        title_words = important_words(title)
        accepted = []
        seen = set()

        # RSS feeds often append an ellipsis when they truncate a teaser.
        # Drop that unfinished tail; never show an artificial sentence end.
        text = re.sub(r"(?:\.{3}|…)+\s*$", "", text).strip()

        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = clean(sentence)
            key = normalize(sentence)
            words = important_words(sentence)
            if not key or not words or key in seen:
                continue

            # A sentence without terminal punctuation is likely a truncated
            # RSS teaser. Keep only complete source sentences.
            if not re.search(r"[.!?][\"'’”)]*$", sentence):
                continue

            if key == normalize(title):
                continue

            # Skip a sentence that mostly restates the headline.
            if len(title_words) >= 5:
                title_overlap = len(words & title_words) / len(title_words)
                if title_overlap >= 0.48 and len(words) <= len(title_words) + 8:
                    continue

            duplicate = False
            for existing in accepted:
                existing_words = important_words(existing)
                shared = len(words & existing_words)
                smaller = min(len(words), len(existing_words))
                union = len(words | existing_words)
                if smaller >= 6 and (
                    shared / smaller >= 0.82
                    or (union and shared / union >= 0.68)
                ):
                    duplicate = True
                    break
            if duplicate:
                continue

            if len(sentence) > max_chars:
                continue

            candidate = " ".join(accepted + [sentence])
            if accepted and len(candidate) > max_chars:
                break

            accepted.append(sentence)
            seen.add(key)
            if len(accepted) >= 4:
                break

        if not accepted:
            # Do not return a clipped sentence or invent a replacement.
            return ""

        return " ".join(accepted)

    def same_event(a, b):

        title_a = normalize(a["title"])
        title_b = normalize(b["title"])

        if title_a == title_b:
            return True

        ratio = SequenceMatcher(
            None,
            title_a,
            title_b
        ).ratio()

        if ratio >= 0.82:
            return True

        words_a = important_words(title_a)
        words_b = important_words(title_b)

        common = words_a & words_b

        if len(common) >= 4:

            overlap = len(common) / max(
                1,
                min(len(words_a), len(words_b))
            )

            if overlap >= 0.65:
                return True

        desc_a = important_words(a.get("description", ""))
        desc_b = important_words(b.get("description", ""))

        common_desc = desc_a & desc_b

        if len(common_desc) >= 8:

            overlap = len(common_desc) / max(
                1,
                min(len(desc_a), len(desc_b))
            )

            if overlap >= 0.30:
                return True

        return False

    candidates = []

    # --------------------------------------------------
    # DIRECT RSS READING WITH 5-MINUTE CACHE
    # --------------------------------------------------

    for source, feed_url, coverage in FEEDS:

        now = datetime.now(timezone.utc)

        # --------------------------------------------------
        # CHECK CACHE
        # --------------------------------------------------

        with LATEST_HOT_CACHE_LOCK:

            cached = LATEST_HOT_CACHE.get(
                feed_url
            )

            if cached:

                cached_at, cached_items = cached

                age = (
                    now - cached_at
                ).total_seconds()

                if age < LATEST_HOT_CACHE_TTL:

                    print(
                        "LATEST & HOT CACHE HIT:",
                        source
                    )

                    candidates.extend(
                        [
                            dict(item)
                            for item in cached_items
                        ]
                    )

                    continue

        # --------------------------------------------------
        # LIVE RSS FETCH
        # --------------------------------------------------

        try:

            if feed_url in prefetched_feed_data:
                xml_data = prefetched_feed_data[feed_url]
                if xml_data is None:
                    raise RuntimeError("Feed prefetch failed")
            else:
                request = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )

                with urllib.request.urlopen(
                    request,
                    timeout=10,
                ) as response:
                    xml_data = response.read()

            root = ET.fromstring(
                xml_data
            )

            items = root.findall(
                ".//{*}item"
            )

            print(
                "LATEST & HOT:",
                source,
                "RSS ITEMS =",
                len(items)
            )

            feed_candidates = []

            for item in items:

                title = clean(
                    item.findtext("title")
                )

                link = clean(
                    item.findtext("link")
                )

                description = compact_description(
                    title,
                    item.findtext("description"),
                )

                pub_date = clean(
                    item.findtext("pubDate")
                    or item.findtext("published")
                    or item.findtext("updated")
                )

                if not title or not link:
                    continue

                published_dt = parse_date(
                    pub_date
                )

                # Latest cards require a real publication timestamp.
                # Do not display undated RSS entries with a made-up
                # current time.
                if published_dt == datetime.min.replace(tzinfo=timezone.utc):
                    continue

                feed_candidates.append(
                    {
                        "title": title,
                        "description": description,
                        "url": link,
                        "source": source,
                        "published_at": (
                            published_dt.isoformat()
                            if published_dt != datetime.min.replace(tzinfo=timezone.utc)
                            else ""
                        ),
                        "_published": published_dt,
                        "_coverage": coverage,
                    }
                )

            # The ticker only displays 20 headlines. Keep the newest 40
            # per feed for ranking and cross-source event matching; parsing
            # hundreds of older entries adds work without changing the list.
            feed_candidates.sort(
                key=lambda article: article["_published"],
                reverse=True,
            )
            feed_candidates = feed_candidates[:LATEST_HOT_MAX_ITEMS_PER_FEED]

            # --------------------------------------------------
            # SAVE CACHE
            # --------------------------------------------------

            with LATEST_HOT_CACHE_LOCK:

                LATEST_HOT_CACHE[
                    feed_url
                ] = (
                    now,
                    [
                        dict(item)
                        for item in feed_candidates
                    ]
                )

            print(
                "LATEST & HOT CACHE SAVED:",
                source
            )

            candidates.extend(
                [
                    dict(item)
                    for item in feed_candidates
                ]
            )

        except Exception as error:

            print(
                "LATEST & HOT FEED ERROR:",
                source,
                error
            )

            # --------------------------------------------------
            # STALE CACHE FALLBACK
            # --------------------------------------------------

            with LATEST_HOT_CACHE_LOCK:

                cached = LATEST_HOT_CACHE.get(
                    feed_url
                )

                if cached:

                    print(
                        "LATEST & HOT STALE CACHE FALLBACK:",
                        source
                    )

                    candidates.extend(
                        [
                            dict(item)
                            for item in cached[1]
                        ]
                    )

    print(
        "LATEST & HOT TOTAL RAW:",
        len(candidates)
    )

    # Cached RSS entries must still pass the age check at response time.
    now_utc = datetime.now(timezone.utc)
    before_freshness_filter = len(candidates)
    candidates = [
        article
        for article in candidates
        if isinstance(article.get("_published"), datetime)
        and -7200
        <= (now_utc - article["_published"]).total_seconds()
        <= 48 * 60 * 60
    ]
    print(
        "LATEST & HOT OUTSIDE 48-HOUR WINDOW REMOVED:",
        before_freshness_filter - len(candidates),
    )

    # Keep recent category stories out of this independent worldwide feed.
    # The category route records titles and URLs, allowing this list to
    # remove both exact URL matches and syndicated headline copies.
    now_ts = time.time()
    with latest_registry.LATEST_HOT_URL_REGISTRY_LOCK:
        expired_category_urls = [
            url
            for url, entry in latest_registry.CATEGORY_ARTICLE_REGISTRY.items()
            if now_ts - entry.get("updated_at", 0) > latest_registry.CATEGORY_ARTICLE_REGISTRY_TTL
        ]
        for url in expired_category_urls:
            latest_registry.CATEGORY_ARTICLE_REGISTRY.pop(url, None)
        category_articles = [
            dict(entry)
            for entry in latest_registry.CATEGORY_ARTICLE_REGISTRY.values()
            if entry.get("title")
        ]
        category_urls = set(latest_registry.CATEGORY_ARTICLE_REGISTRY)

    category_title_words = [
        (normalize(article.get("title", "")), important_words(article.get("title", "")))
        for article in category_articles
    ]

    def overlaps_category(article):
        article_url = str(article.get("url", "")).strip().lower()
        article_url = article_url.split("#", 1)[0].rstrip("/")
        if article_url in category_urls:
            return True

        title_key = normalize(article.get("title", ""))
        words = important_words(article.get("title", ""))
        for category_title, category_words in category_title_words:
            if title_key == category_title:
                return True
            shared = words & category_words
            smaller = min(len(words), len(category_words))
            if len(shared) >= 4 and smaller and len(shared) / smaller >= 0.65:
                return True
            if (
                len(words) >= 5
                and len(category_words) >= 5
                and abs(len(words) - len(category_words)) <= 3
                and SequenceMatcher(None, title_key, category_title).ratio() >= 0.84
            ):
                return True
        return False

    before_category_dedupe = len(candidates)
    candidates = [
        article for article in candidates
        if not overlaps_category(article)
    ]
    print(
        "LATEST & HOT CATEGORY DUPLICATES REMOVED:",
        before_category_dedupe - len(candidates),
    )

    # --------------------------------------------------
    # TRENDING SCORE
    # --------------------------------------------------

    # Recent stories receive a higher score.
    # Stories from different sources are also boosted.
    # This keeps the feed fresh while giving more weight
    # to stories receiving broader coverage.

    from collections import defaultdict
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    def recency_score(published):
        if published == datetime.min.replace(tzinfo=timezone.utc):
            return 0

        age_hours = max(
            0,
            (now - published).total_seconds() / 3600
        )

        if age_hours <= 1:
            return 100
        if age_hours <= 3:
            return 90
        if age_hours <= 6:
            return 80
        if age_hours <= 12:
            return 65
        if age_hours <= 24:
            return 50
        if age_hours <= 48:
            return 30

        return 10

    # First group similar stories so we can measure
    # how many independent sources are covering them.
    event_groups = []

    for article in candidates:
        matched_group = None

        for group in event_groups:
            if same_event(article, group[0]):
                matched_group = group
                break

        if matched_group is None:
            event_groups.append([article])
        else:
            matched_group.append(article)

    # Calculate a trend score for every article.
    for group in event_groups:

        source_count = len({
            article["source"]
            for article in group
        })

        source_bonus = min(
            40,
            max(0, source_count - 1) * 15
        )

        for article in group:
            article["_trend_score"] = (
                recency_score(article["_published"])
                + source_bonus
            )

    candidates.sort(
        key=lambda article: (
            article["_trend_score"],
            article["_published"]
        ),
        reverse=True
    )

    print("LATEST & HOT SORTED BY TRENDING SCORE")

    # --------------------------------------------------
    # REMOVE EXACT URL DUPLICATES
    # --------------------------------------------------

    unique = []

    seen_urls = set()

    for article in candidates:

        key = article["url"].lower().strip()

        if key in seen_urls:
            continue

        seen_urls.add(key)
        unique.append(article)

    # --------------------------------------------------
    # REMOVE SAME-EVENT STORIES
    # --------------------------------------------------

    selected = []

    for article in unique:

        duplicate = False

        for existing in selected:

            if same_event(article, existing):

                print(
                    "SAME EVENT REMOVED:",
                    article["source"],
                    "->",
                    article["title"]
                )

                duplicate = True
                break

        if duplicate:
            continue

        selected.append(article)

    # --------------------------------------------------
    # FINAL UNIQUE CHECK
    # --------------------------------------------------

    # Reserve space for each coverage area so India regional and central
    # stories remain visible alongside major world headlines.
    final = []
    seen_titles = set()
    seen_urls = set()
    source_counts = {}
    coverage_targets = {
        "regional": 7,
        "national": 7,
        "world": 6,
    }

    def add_from_coverage(coverage, target):
        added = 0
        for article in selected:
            if article.get("_coverage") != coverage:
                continue
            source = article.get("source", "")
            title_key = normalize(article.get("title", ""))
            url_key = str(article.get("url", "")).lower().strip()
            if (
                not title_key
                or not url_key
                or title_key in seen_titles
                or url_key in seen_urls
                or source_counts.get(source, 0) >= 5
            ):
                continue
            seen_titles.add(title_key)
            seen_urls.add(url_key)
            source_counts[source] = source_counts.get(source, 0) + 1
            final.append(article)
            added += 1
            if added >= target or len(final) >= 20:
                break
        return added

    for coverage, target in coverage_targets.items():
        add_from_coverage(coverage, target)

    # Fill any remaining slots in trend order, while keeping a source cap.
    for article in selected:
        if len(final) >= 20:
            break
        source = article.get("source", "")
        title_key = normalize(article.get("title", ""))
        url_key = str(article.get("url", "")).lower().strip()
        if (
            not title_key
            or not url_key
            or title_key in seen_titles
            or url_key in seen_urls
            or source_counts.get(source, 0) >= 6
        ):
            continue
        seen_titles.add(title_key)
        seen_urls.add(url_key)
        source_counts[source] = source_counts.get(source, 0) + 1
        final.append(article)

    # Keep the page's existing newest/trending order after source balancing.
    final.sort(
        key=lambda article: (
            article.get("_trend_score", 0),
            article.get("_published", datetime.min.replace(tzinfo=timezone.utc)),
        ),
        reverse=True,
    )
    for article in final:
        article.pop("_published", None)
        article.pop("_trend_score", None)
        article.pop("_coverage", None)

    print(
        "LATEST & HOT FINAL:",
        len(final)
    )

    # Register the final Latest/Hot URLs so category feeds
    # can avoid returning the same stories.
    with latest_registry.LATEST_HOT_URL_REGISTRY_LOCK:
        latest_registry.LATEST_HOT_URL_REGISTRY.clear()
        latest_registry.LATEST_HOT_URL_REGISTRY.update(
            {
                str(article.get("url", "")).strip().lower()
                for article in final
                if str(article.get("url", "")).strip()
            }
        )
        latest_registry.LATEST_HOT_URL_REGISTRY_UPDATED = time.time()

    print(
        "LATEST URL REGISTRY UPDATED:",
        len(latest_registry.LATEST_HOT_URL_REGISTRY)
    )

    for index, article in enumerate(final, 1):
        print(
            f"LATEST & HOT {index}:",
            article["source"],
            "->",
            article["title"]
        )

    response = {
        "articles": final,
        "count": len(final),
        "rule": "fresh regional, national and world stories ranked by recency and cross-source coverage"
    }

    with TRENDING_RESPONSE_CACHE_LOCK:
        TRENDING_RESPONSE_CACHE = (
            time.monotonic(),
            {
                **response,
                "articles": [dict(article) for article in final],
            },
        )

    return response

@app.get("/search")
def search(q: str = ""):

    query = q.strip()

    print("=" * 70)
    print("NEWS SEARCH")
    print("=" * 70)
    print("SEARCH QUERY:", query)

    if not query:

        return {
            "query": "",
            "articles": [],
        }

    cache_key = " ".join(query.lower().split())
    now = time.monotonic()
    with SEARCH_CACHE_LOCK:
        cached = SEARCH_CACHE.get(cache_key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL:
            print("SEARCH CACHE HIT:", cache_key)
            return {
                "query": query,
                "articles": [dict(article) for article in cached[1]],
                "cached": True,
            }

    try:

        articles = search_news(query)

        # Cache successful searches for fast repeats, but don't cache an
        # empty response: upstream RSS feeds can fail transiently, and a
        # temporary miss should not hide fresh search results for 90 seconds.
        if articles:
            with SEARCH_CACHE_LOCK:
                SEARCH_CACHE[cache_key] = (time.monotonic(), [dict(article) for article in articles])
                while len(SEARCH_CACHE) > SEARCH_CACHE_MAX_ENTRIES:
                    SEARCH_CACHE.pop(next(iter(SEARCH_CACHE)))

        print(
            f"SEARCH RESULTS: {len(articles)} articles"
        )

        return {
            "query": query,
            "articles": articles,
        }

    except Exception as error:

        print("SEARCH ERROR:", error)

        raise HTTPException(
            status_code=500,
            detail=f"Unable to search news: {str(error)}",
        )


# ============================================================
# AI SUMMARY
# ============================================================

# ============================================================
# EXPLAIN LIKE I'M 10
# ============================================================

@app.post("/translate")
def translate_article(payload: dict):

    title = str(payload.get("title", "")).strip()
    description = str(payload.get("description", "")).strip()
    language_code = str(payload.get("language", "")).strip().lower()
    languages = {
        "en": "English",
        "hi": "Hindi",
        "te": "Telugu",
    }

    if not title and not description:
        raise HTTPException(
            status_code=400,
            detail="Article title or summary is required",
        )

    target_language = languages.get(language_code)
    if not target_language:
        raise HTTPException(
            status_code=400,
            detail="Choose English, Hindi, or Telugu",
        )

    cache_key = hashlib.sha256(
        f"{language_code}\0{title}\0{description}".encode("utf-8")
    ).hexdigest()
    now_monotonic = time.monotonic()
    with TRANSLATION_CACHE_LOCK:
        cached_translation = TRANSLATION_CACHE.get(cache_key)
        if (
            cached_translation
            and now_monotonic - cached_translation[0] < TRANSLATION_CACHE_TTL
        ):
            return {
                "success": True,
                "language": language_code,
                **cached_translation[1],
                "cached": True,
            }

    translated = translate_article_text(
        title,
        description,
        target_language,
    )

    if not translated:
        raise HTTPException(
            status_code=503,
            detail="Translation is unavailable. Check that the local Ollama service is running, then try again.",
        )

    with TRANSLATION_CACHE_LOCK:
        TRANSLATION_CACHE[cache_key] = (
            time.monotonic(),
            dict(translated),
        )
        while len(TRANSLATION_CACHE) > TRANSLATION_CACHE_MAX_ENTRIES:
            TRANSLATION_CACHE.pop(next(iter(TRANSLATION_CACHE)))

    return {
        "success": True,
        "language": language_code,
        **translated,
        "cached": False,
    }


# ============================================================
# EXPLAIN LIKE I'M 10
# ============================================================

@app.post("/explain-eli10")
def explain_eli10(payload: dict):

    title = str(
        payload.get("title", "")
    ).strip()

    description = str(
        payload.get("description", "")
    ).strip()

    if not title and not description:
        raise HTTPException(
            status_code=400,
            detail="Title or description is required"
        )

    cache_key = hashlib.sha256(
        f"{title}\0{description}".encode("utf-8")
    ).hexdigest()
    with ELI10_CACHE_LOCK:
        cached_explanation = ELI10_CACHE.get(cache_key)
    if cached_explanation:
        return {
            "success": True,
            "explanation": cached_explanation,
            "cached": True,
        }

    explanation = explain_like_im_10(
        title,
        description
    )

    if not explanation:
        raise HTTPException(
            status_code=500,
            detail="Could not generate explanation"
        )

    # Put the required two complete sentences on separate display lines;
    # older entries in the in-memory cache may contain a single paragraph.
    explanation = re.sub(r"\s+", " ", explanation).strip()
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", explanation)
        if part.strip()
    ]
    if len(sentences) >= 2:
        explanation = "\n".join(sentences[:2])

    with ELI10_CACHE_LOCK:
        ELI10_CACHE[cache_key] = explanation
        while len(ELI10_CACHE) > ELI10_CACHE_MAX_ENTRIES:
            ELI10_CACHE.pop(next(iter(ELI10_CACHE)))

    return {
        "success": True,
        "explanation": explanation
    }
# ============================================================
# COMPARE ARTICLES
# ============================================================

@app.post("/compare-articles")
def compare_news_articles(payload: dict):

    article1 = payload.get("article1", {})
    article2 = payload.get("article2", {})

    if not isinstance(article1, dict):
        article1 = {}

    if not isinstance(article2, dict):
        article2 = {}

    if not article1.get("title") and not article1.get("description"):
        raise HTTPException(
            status_code=400,
            detail="Article 1 is required"
        )

    if not article2.get("title") and not article2.get("description"):
        raise HTTPException(
            status_code=400,
            detail="Article 2 is required"
        )

    comparison = compare_articles(
        article1,
        article2
    )

    if not comparison:
        raise HTTPException(
            status_code=500,
            detail="Could not compare articles"
        )

    return {
        "success": True,
        "comparison": comparison
    }

@app.post("/summarize")
def summarize(title: str, description: str):

    title = title.strip()
    description = description.strip()

    if not title:

        raise HTTPException(
            status_code=400,
            detail="Article title is required",
        )

    print("=" * 70)
    print("AI SUMMARY")
    print("=" * 70)
    print("TITLE:", title)

    try:

        # ----------------------------------------------------
        # CHECK CACHE
        # ----------------------------------------------------

        cached_result = get_cached_summary(title)

        if cached_result is not None:

            print("USING CACHED SUMMARY")

            cached_result = normalize_analysis(
                cached_result,
                title,
                description,
            )

            try:
                save_summary(title, cached_result)
            except Exception as cache_error:
                print("CACHE NORMALIZATION WARNING:", cache_error)

            return {
                "summary": cached_result,
                "cached": True,
            }

        # ----------------------------------------------------
        # CALL OLLAMA
        # ----------------------------------------------------

        result = summarize_news(
            title,
            description
        )
        result = normalize_analysis(result, title, description)

        # ----------------------------------------------------
        # SAVE SUMMARY TO CACHE
        # ----------------------------------------------------

        try:

            save_summary(
                title,
                result
            )

        except Exception as cache_error:

            print(
                "CACHE SAVE WARNING:",
                cache_error
            )

        print("AI SUMMARY GENERATED")

        return {
            "summary": result,
            "cached": False,
        }

    except Exception as error:

        print("AI SUMMARY ERROR:", error)

        raise HTTPException(
            status_code=500,
            detail=f"Unable to generate AI summary: {str(error)}",
        )


# ============================================================
# SAVED ARTICLES
# ============================================================

@app.get("/saved")
def get_saved_articles():

    print("=" * 70)
    print("GET SAVED ARTICLES")
    print("=" * 70)

    connection = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT
                id,
                title,
                article_title,
                description,
                url,
                source,
                published_at,
                saved_at
            FROM saved_articles
            ORDER BY id DESC
            """
        )

        rows = cursor.fetchall()

        articles = []

        for row in rows:

            articles.append(
                {
                    "id": row[0],
                    "title": row[1],
                    "article_title": row[2],
                    "description": row[3],
                    "url": row[4],
                    "source": row[5],
                    "published_at": row[6],
                    "saved_at": row[7],
                }
            )

        print(
            f"SAVED ARTICLES: {len(articles)}"
        )

        return {
            "count": len(articles),
            "articles": articles,
        }

    except Exception as error:

        print(
            "SAVED ARTICLES ERROR:",
            error
        )

        raise HTTPException(
            status_code=500,
            detail=f"Unable to load saved articles: {str(error)}",
        )

    finally:

        if connection:

            connection.close()


# ============================================================
# SAVE ARTICLE
# ============================================================

@app.post("/saved")
def save_article(
    title: str,
    description: str = "",
    url: str = "",
    source: str = "",
    published_at: str = "",
):

    title = title.strip()
    description = description.strip()
    url = url.strip()
    source = source.strip()
    published_at = published_at.strip()

    if not title:

        raise HTTPException(
            status_code=400,
            detail="Article title is required",
        )

    connection = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        # ----------------------------------------------------
        # CHECK DUPLICATE
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT id
            FROM saved_articles
            WHERE title = ?
            LIMIT 1
            """,
            (title,),
        )

        existing = cursor.fetchone()

        if existing:

            return {
                "status": "success",
                "message": "Article already saved",
                "id": existing[0],
            }

        # ----------------------------------------------------
        # INSERT ARTICLE
        # ----------------------------------------------------

        cursor.execute(
            """
            INSERT INTO saved_articles
            (
                title,
                article_title,
                description,
                url,
                source,
                published_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                title,
                description,
                url,
                source,
                published_at,
            ),
        )

        article_id = cursor.lastrowid

        connection.commit()

        print(
            "ARTICLE SAVED:",
            article_id
        )

        return {
            "status": "success",
            "message": "Article saved successfully",
            "id": article_id,
        }

    except Exception as error:

        if connection:

            connection.rollback()

        print(
            "SAVE ARTICLE ERROR:",
            error
        )

        raise HTTPException(
            status_code=500,
            detail=f"Unable to save article: {str(error)}",
        )

    finally:

        if connection:

            connection.close()


# ============================================================
# DELETE SAVED ARTICLE
# ============================================================

@app.delete("/saved/{article_id}")
def delete_saved_article(article_id: int):

    print("=" * 70)
    print("DELETE SAVED ARTICLE")
    print("=" * 70)
    print("ARTICLE ID:", article_id)

    connection = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        cursor.execute(
            """
            DELETE FROM saved_articles
            WHERE id = ?
            """,
            (article_id,),
        )

        deleted_count = cursor.rowcount

        connection.commit()

        if deleted_count == 0:

            raise HTTPException(
                status_code=404,
                detail="Saved article not found",
            )

        print(
            "ARTICLE DELETED:",
            article_id
        )

        return {
            "status": "success",
            "message": "Saved article deleted successfully",
            "id": article_id,
        }

    except HTTPException:

        raise

    except Exception as error:

        if connection:

            connection.rollback()

        print(
            "DELETE ARTICLE ERROR:",
            error
        )

        raise HTTPException(
            status_code=500,
            detail=f"Unable to delete saved article: {str(error)}",
        )

    finally:

        if connection:

            connection.close()


# ============================================================
# API INFORMATION
# ============================================================

@app.get("/api-info")
def api_info():

    return {
        "name": "The Newsroom API",
        "version": "4.0.0",
        "features": [
            "News Categories",
            "Custom News Search",
            "Ollama AI Analysis",
            "SQLite Database",
            "AI Summary Cache",
            "Saved Articles",
        ],
        "endpoints": {
            "home": "/",
            "health": "/health",
            "categories": "/categories",
            "news": "/news",
            "search": "/search",
            "summarize": "/summarize",
            "saved_articles": "/saved",
            "api_info": "/api-info",
        },
    }


# ============================================================
# STARTUP MESSAGE
# ============================================================

print("=" * 70)
print("THE NEWSROOM API")
print("=" * 70)
print("FastAPI: RUNNING")
print("Ollama: ENABLED")
print("SQLite: ENABLED")
print("Saved Articles API: ENABLED")
print("Custom Search API: ENABLED")
print("=" * 70)

# Serve existing frontend assets (CSS, JS, HTML)
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
