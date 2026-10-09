"""
THE NEWSROOM - NEWS SERVICE
Version 7.0

Direct publisher RSS architecture.

Features:
    - Direct publisher RSS feeds
    - Telangana regional news fix
    - Better 3-sentence descriptions
    - Publisher article-page extraction
    - Duplicate removal
    - Recent-news filtering
    - Search support

No GDELT.
No Google News RSS.
No Ollama during feed loading.
"""

from __future__ import annotations

import asyncio
import time
import json

import html
import re

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from threading import Lock
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import List, Dict, Optional
import latest_registry
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlparse

import xml.etree.ElementTree as ET


# ============================================================
# CONFIG
# ============================================================

REQUEST_TIMEOUT = 10
ARTICLE_TIMEOUT = 8

MAX_ARTICLES = 20
MAX_FEED_ITEMS = 100
MAX_WORKERS = 8
ONJOB_FEED_URL = "https://onjob.io/feeds/jobs.xml"

# Recognize specific vacancy wording that appears in publisher headlines,
# including a post count between "applications for" and the role noun.
JOB_POSTING_PATTERN = re.compile(
    r"\b(?:invites?|invited|seeks?)\s+applications?\s+for\s+"
    r"(?:(?:\d+|several|multiple)\s+)?"
    r"(?:[a-z][a-z'/-]*\s+){0,2}(?:posts?|positions?|vacancies)\b"
    r"|\bapply\s+for\s+\d+\s+(?:research\s+)?(?:roles?|positions?)\b",
    re.IGNORECASE,
)


def is_active_job_listing(item: dict) -> bool:
    """The OnJob feed contains currently open, directly applicable roles."""
    feed_url = str(
        item.get("_feed_url") or item.get("feed_url") or ""
    ).strip().lower()
    return feed_url.rstrip("/") == ONJOB_FEED_URL.rstrip("/")

# ============================================================
# RSS CACHE
# ============================================================

RSS_CACHE_TTL = 10 * 60

RSS_CACHE = {}

RSS_CACHE_LOCK = Lock()

GOOGLE_NEWS_URL_CACHE_TTL = 24 * 60 * 60
GOOGLE_NEWS_URL_CACHE = {}
GOOGLE_NEWS_URL_CACHE_LOCK = Lock()

# Share recently served Latest and India stories across category requests.
CROSS_CATEGORY_URL_REGISTRY = {}
CROSS_CATEGORY_REGISTRY_LOCK = Lock()
CROSS_CATEGORY_REGISTRY_TTL = 10 * 60

# TV9 Telugu -> English translation cache
TV9_TRANSLATION_CACHE_TTL = 30 * 60
TV9_TRANSLATION_CACHE = {}
TV9_TRANSLATION_CACHE_LOCK = Lock()


# We want enough article paragraphs to build a useful summary.
MAX_PARAGRAPHS_TO_CHECK = 40

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 "
    "Chrome/140.0 Safari/537.36"
)


# ============================================================
# DIRECT PUBLISHER RSS FEEDS
# ============================================================


# ============================================================
# INDEPENDENT LATEST & HOT FEEDS
# ============================================================
# Kept separate from all normal category feeds.
# These feeds are used only by the LATEST & HOT section.

LATEST_HOT_FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "world"),
    ("The Guardian", "https://www.theguardian.com/world/rss", "world"),
    ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "world"),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/home.xml", "world"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml", "world"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "world"),
    ("France 24", "https://www.france24.com/en/rss", "world"),
    ("The Straits Times", "https://www.straitstimes.com/news/world/rss.xml", "world"),
    # Dedicated Indian feeds add central-government and state-level stories
    # without replacing the independent international coverage above.
    ("India Central News", "https://news.google.com/rss/search?q=India+central+government+OR+Parliament+OR+Supreme+Court+OR+Union+Cabinet+when%3A2d&hl=en-IN&gl=IN&ceid=IN:en", "national"),
    ("India National News", "https://indianexpress.com/section/india/feed/", "national"),
    ("India Regional News", "https://feeds.feedburner.com/ndtvnews-south", "regional"),
    ("India State News", "https://news.google.com/rss/search?q=Telangana+OR+Andhra+Pradesh+OR+Karnataka+OR+Tamil+Nadu+OR+Kerala+OR+Maharashtra+OR+Delhi+local+news+when%3A2d&hl=en-IN&gl=IN&ceid=IN:en", "regional"),
]

CATEGORY_FEEDS = {
    "latest": [
        # A rolling Google News search supplements publisher RSS feeds that
        # sometimes stop advancing or lag behind on Render.
        "https://news.google.com/rss/search?q=India+news+OR+India+breaking+news+when%3A1d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-latest",
        "https://www.hindustantimes.com/feeds/rss/latest/rssfeed.xml",
        "https://www.thehindu.com/news/national/feeder/default.rss",
    ],

    "india": [
"https://www.thehindu.com/news/national/feeder/default.rss",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-india-news",
        "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml",
        "https://news.abplive.com/news/india/feed",
        "https://news.google.com/rss/search?q=India+national+news+government+court+parliament+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+national+policy+Supreme+Court+election+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    # Telangana:
    # Hyderabad-specific feed is already a Telangana regional feed.
    # Therefore we do NOT apply the strict keyword filter to it.
    "telangana": [
        "https://www.thehindu.com/news/national/telangana/feeder/default.rss",
        "https://telanganatoday.com/feed",
        "https://indianexpress.com/section/cities/hyderabad/feed/",
        "https://tv9telugu.com/telangana/feed",
        "https://www.thehansindia.com/rss/telangana",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Ftelangana+Telangana+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Ftelangana+Telangana+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fhyderabad+Telangana+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "andhra pradesh": [
        "https://www.thehindu.com/news/national/andhra-pradesh/feeder/default.rss",
        "https://news.google.com/rss/search?q=Andhra+Pradesh&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Andhra+Pradesh+Vijayawada+Visakhapatnam+English+news+when%3A24h&hl=en-IN&gl=IN&ceid=IN:en",
        "https://tv9telugu.com/andhra-pradesh/feed",
        "https://www.deccanchronicle.com/feeds.xml",
        "https://www.thehansindia.com/rss/andhra-pradesh",
        "https://news.google.com/rss/search?q=Andhra+Pradesh+Vijayawada+Visakhapatnam+Amaravati+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Andhra+Pradesh+district+government+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Fandhra-pradesh+Andhra+Pradesh+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Fandhra-pradesh+Andhra+Pradesh+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fvijayawada+Andhra+Pradesh+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "andhrapradesh": [
        "https://www.thehindu.com/news/national/andhra-pradesh/feeder/default.rss",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Fandhra-pradesh+Andhra+Pradesh+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Fandhra-pradesh+Andhra+Pradesh+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fvijayawada+Andhra+Pradesh+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://tv9telugu.com/andhra-pradesh/feed",
        "https://www.thehansindia.com/rss/andhra-pradesh",
        "https://www.deccanchronicle.com/feeds.xml",
        "https://indianexpress.com/section/india/feed/",
        "https://news.google.com/rss/search?q=Andhra+Pradesh+district+government+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "karnataka": [
        "https://www.thehindu.com/news/national/karnataka/feeder/default.rss",
        "https://deccanherald.com/feed",
        "https://kannada.oneindia.com/rss/feeds/kannada-news-fb.xml",
        "https://newsable.asianetnews.com/rss",
        "https://indianexpress.com/section/cities/bangalore/feed/",
        "https://news.google.com/rss/search?q=Karnataka+Bengaluru+Bangalore+Mysuru+Mangaluru+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Fkarnataka+Karnataka+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Fkarnataka+Karnataka+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fbengaluru+Karnataka+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "tamil nadu": [
        "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss",
        "https://www.thehindu.com/news/cities/chennai/feeder/default.rss",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://www.deccanchronicle.com/feeds.xml",
        "https://www.thehindubusinessline.com/news/national/?service=rss",
        "https://timesofindia.indiatimes.com/rssfeeds/2950623.cms",
        "https://indianexpress.com/section/india/feed/",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Ftamil-nadu+Tamil+Nadu+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Ftamil-nadu+Tamil+Nadu+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fchennai+Tamil+Nadu+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "tamilnadu": [
        "https://www.thehindu.com/news/national/tamil-nadu/feeder/default.rss",
        "https://www.thehindu.com/news/cities/chennai/feeder/default.rss",
        "https://www.hindustantimes.com/feeds/rss/cities/chennai-news/rssfeed.xml",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://www.deccanchronicle.com/feeds.xml",
        "https://www.thehindubusinessline.com/news/national/?service=rss",
        "https://timesofindia.indiatimes.com/rssfeeds/2950623.cms",
        "https://indianexpress.com/section/cities/chennai/feed/",
        "https://indianexpress.com/section/india/feed/",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Ftamil-nadu+Tamil+Nadu+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Ftamil-nadu+Tamil+Nadu+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fchennai+Tamil+Nadu+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adtnext.in+Tamil+Nadu+OR+Chennai+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanchronicle.com+Tamil+Nadu+OR+Chennai+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "kerala": [
        "https://www.thehindu.com/news/national/kerala/feeder/default.rss",
        "https://www.thehindu.com/news/cities/Kochi/feeder/default.rss",
        "https://www.thehindu.com/news/cities/Thiruvananthapuram/feeder/default.rss",
        "https://timesofindia.indiatimes.com/rssfeeds/878156304.cms",
        "https://www.mathrubhumi.com/rss",
        "https://malayalam.oneindia.com/rss/feeds/oneindia-malayalam-fb.xml",
        "https://www.onmanorama.com/kerala.feeds.onmrss.xml",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Fkerala+Kerala+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Fkerala+Kerala+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fkochi+Kerala+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://indianexpress.com/section/cities/kochi/feed/",
        "https://news.google.com/rss/search?q=Kerala+Kochi+Thiruvananthapuram+English+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "maharashtra": [
        "https://www.thehindu.com/news/cities/mumbai/feeder/default.rss",
        "https://indianexpress.com/section/cities/mumbai/feed/",
        "https://www.hindustantimes.com/feeds/rss/cities/pune-news/rssfeed.xml",
        "https://www.hindustantimes.com/feeds/rss/cities/mumbai-news/rssfeed.xml",
        "https://timesofindia.indiatimes.com/rssfeeds/-2128838597.cms",
        "https://timesofindia.indiatimes.com/rssfeeds/-2128821991.cms",
        "https://government.economictimes.indiatimes.com/rss/lateststories",
        "https://news.abplive.com/states/feed",
        "https://tv9marathi.com/feed",
        "https://indianexpress.com/section/cities/pune/feed/",
        "https://indianexpress.com/section/cities/nagpur/feed/",
        "https://indianexpress.com/section/cities/nashik/feed/",
        "https://news.google.com/rss/search?q=Maharashtra+Mumbai+Pune+Nagpur+Nashik+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Maharashtra+news+Mumbai+Pune+Nagpur+Nashik+when%3A1d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Maharashtra+government+district+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://indianexpress.com/section/india/feed/",
        "https://feeds.feedburner.com/ndtvnews-south",
        "https://news.google.com/rss/search?q=site%3Anewindianexpress.com%2Fstates%2Fmaharashtra+Maharashtra+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Adeccanherald.com%2Findia%2Fmaharashtra+Maharashtra+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com%2Fcity%2Fmumbai+Maharashtra+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Afreepressjournal.in+Maharashtra+OR+Mumbai+OR+Pune+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=site%3Amid-day.com+Maharashtra+OR+Mumbai+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "delhi": [
        "https://indianexpress.com/section/cities/delhi/feed/",
        "https://www.hindustantimes.com/feeds/rss/cities/delhi-news/rssfeed.xml",
        "https://timesofindia.indiatimes.com/rssfeeds/-2128839596.cms",
        "https://news.abplive.com/states/feed",
        "https://news.google.com/rss/search?q=Delhi&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Delhi+NCR+Noida+Gurugram+Ghaziabad+Faridabad+news+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Delhi+government+court+transport+pollution+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],

    "world": [
"https://www.firstpost.com/commonfeeds/v1/mfp/rss/world.xml",
        "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms",
        "https://indianexpress.com/section/world/feed/",
        "https://feeds.feedburner.com/ndtvnews-world-news",
        "https://www.hindustantimes.com/feeds/rss/world-news/rssfeed.xml",
    ],

    "ai": [
        "https://indianexpress.com/section/technology/artificial-intelligence/feed/",
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://arstechnica.com/ai/feed/",
        "https://www.wired.com/feed/tag/ai/latest/rss",
    ],

    "technology": [
        "https://timesofindia.indiatimes.com/rssfeeds/66949542.cms",
        "https://indianexpress.com/section/technology/feed/",
        "https://feeds.feedburner.com/gadgets360-latest",
        "https://www.hindustantimes.com/feeds/rss/technology/rssfeed.xml",
        "https://www.thehindu.com/sci-tech/technology/feeder/default.rss",
    ],

    "business": [
"https://www.firstpost.com/commonfeeds/v1/mfp/rss/business.xml",
        "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms",
        "https://indianexpress.com/section/business/feed/",
        "https://feeds.feedburner.com/ndtvprofit-latest",
        "https://www.hindustantimes.com/feeds/rss/business/rssfeed.xml",
    ],

    "sports": [
"https://www.firstpost.com/commonfeeds/v1/mfp/rss/sports.xml",
        "https://timesofindia.indiatimes.com/rssfeeds/4719148.cms",
        "https://indianexpress.com/section/sports/feed/",
        "https://feeds.feedburner.com/ndtvsports-latest",
        "https://www.hindustantimes.com/feeds/rss/sports/rssfeed.xml",
    ],

    "entertainment": [
        "https://timesofindia.indiatimes.com/rssfeeds/1081479906.cms",
        "https://indianexpress.com/section/entertainment/feed/",
        "https://feeds.feedburner.com/ndtvmovies-latest",
        "https://www.hindustantimes.com/feeds/rss/entertainment/rssfeed.xml",
        "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
        "https://www.theguardian.com/culture/rss",
    ],
}


# --------------------------------------------------------
# JOBS CATEGORY
# --------------------------------------------------------

CATEGORY_FEEDS.update({
    "jobs": [
        "https://www.careerindia.com/rss/feeds/careerindia-fb.xml",
        ONJOB_FEED_URL,
        "https://news.google.com/rss/search?q=government+job+recruitment+vacancy+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+government+job+recruitment+vacancy+apply+when%3A24h&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+private+company+hiring+job+openings+apply+when%3A24h&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+jobs+notification+posts+apply+official+when%3A24h&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+public+sector+recruitment+vacancy+application+when%3A24h&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+state+government+recruitment+vacancies+posts+apply+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+central+government+ministries+departments+recruitment+vacancies+apply+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+state+public+service+commission+recruitment+vacancies+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+private+sector+company+job+openings+careers+apply+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=hiring+jobs+India+companies+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=IT+technology+jobs+hiring+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=internships+freshers+jobs+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=freshers+recruitment+jobs+India+apply+online+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://indianexpress.com/section/jobs/feed/",
        "https://indianexpress.com/section/jobs/bank-jobs/feed/",
        "https://indianexpress.com/section/jobs/railway-jobs/feed/",
        "https://www.hindustantimes.com/feeds/rss/education/employment-news/rssfeed.xml",
        "https://news.abplive.com/education/feed",
        "https://news.google.com/rss/search?q=India+recruitment+notification+apply+vacancy+government+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+bank+railway+defence+teacher+recruitment+apply+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=companies+worldwide+job+openings+recruitment+careers+apply+when%3A5d&hl=en&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=global+employers+are+hiring+open+positions+apply+when%3A5d&hl=en&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=remote+jobs+worldwide+company+careers+openings+when%3A5d&hl=en&gl=US&ceid=US:en",
    ],
    "stocks": [
        "https://economictimes.indiatimes.com/markets/stocks/rss.cms",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.livemint.com/rss/markets",
        "https://indianexpress.com/section/smart-stocks/feed/",
        "https://www.business-standard.com/rss/markets-106.rss",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
        "https://news.google.com/rss/search?q=India+stock+market+Nifty+Sensex+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Indian+stocks+shares+market+today+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=NSE+BSE+stocks+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=stocks+in+news+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+listed+companies+shares+earnings+IPO+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],
})


# ============================================================
# CATEGORY KEYWORDS
# ============================================================

CATEGORY_KEYWORDS = {

    "telangana": [
        "telangana",
        "hyderabad",
        "secunderabad",
        "warangal",
        "hanamkonda",
        "kazipet",
        "nizamabad",
        "karimnagar",
        "khammam",
        "adilabad",
        "nalgonda",
        "suryapet",
        "medak",
        "siddipet",
        "sangareddy",
        "rangareddy",
        "rajendranagar",
        "shamshabad",
        "cyberabad",
        "gachibowli",
        "madhapur",
        "kukatpally",
        "uppal",
        "lb nagar",
        "charminar",
        "secunderabad",
        "telangana government",
        "telangana govt",
    ],

    "andhra pradesh": [
        "andhra",
        "andhra pradesh",
        "vijayawada",
        "visakhapatnam",
        "vizag",
        "tirupati",
        "guntur",
        "nellore",
        "kakinada",
        "amaravati",
        "rajahmundry",
        "kadapa",
        "anantapur",
        "kurnool",
        "prakasam",
        "srikakulam",
        "chittoor",
    ],

    "andhrapradesh": [
        "andhra",
        "andhra pradesh",
        "vijayawada",
        "visakhapatnam",
        "vizag",
        "tirupati",
        "guntur",
        "nellore",
        "kakinada",
        "amaravati",
        "rajahmundry",
        "kadapa",
        "anantapur",
        "kurnool",
        "prakasam",
        "srikakulam",
        "chittoor",
    ],

    "karnataka": [
        "karnataka",
        "bengaluru",
        "bangalore",
        "mysurean",
        "mysorean",
        "kannadiga",
        "mysuru",
        "mysore",
        "mangaluru",
        "mangalore",
        "hubballi",
        "dharwad",
        "belagavi",
        "shivamogga",
        "tumakuru",
    ],

    "tamil nadu": [
        "tamil nadu",
        "chennai",
        "ennore",
        "coimbatore",
        "madurai",
        "salem",
        "tiruchirappalli",
        "trichy",
        "tirunelveli",
        "vellore",
        "erode",
        "thoothukudi",
    ],

    "tamilnadu": [
        "tamil nadu",
        "chennai",
        "coimbatore",
        "madurai",
        "salem",
        "tiruchirappalli",
        "trichy",
        "tirunelveli",
        "vellore",
        "erode",
        "thoothukudi",
    ],

    "kerala": [
        "kerala",
        "kochi",
        "thiruvananthapuram",
        "trivandrum",
        "kozhikode",
        "calicut",
        "kannur",
        "kollam",
        "thrissur",
        "alappuzha",
        "palakkad",
    ],

    "maharashtra": [
        "maharashtra",
        "mumbai",
        "pune",
        "nagpur",
        "nashik",
        "thane",
        "aurangabad",
        "chhatrapati sambhajinagar",
        "kolhapur",
        "navi mumbai",
    ],

    "delhi": [
        "delhi",
        "new delhi",
        "national capital",
        "delhi government",
        "delhi govt",
        "ncr",
        "noida",
        "gurugram",
        "gurgaon",
        "ghaziabad",
        "faridabad",
    ],

    "ai": [
        "artificial intelligence",
        "artificial-intelligence",
        "generative ai",
        "generative-ai",
        "genai",
        "large language model",
        "large-language model",
        "large language models",
        "llm",
        "llms",
        "machine learning",
        "deep learning",
        "neural network",
        "neural networks",
        "ai agent",
        "ai agents",
        "ai assistant",
        "ai assistants",
        "ai model",
        "ai models",
        "ai chatbot",
        "ai chatbots",
        "ai tool",
        "ai tools",
        "ai system",
        "ai systems",
        "ai platform",
        "ai platforms",
        "ai-powered",
        "ai powered",
        "ai-driven",
        "ai driven",
        "ai startup",
        "ai startups",
        "openai",
        "chatgpt",
        "gpt-4",
        "gpt-5",
        "gemini",
        "claude",
        "copilot",
        "anthropic",
        "deepmind",
        "nvidia ai",
        "ai chip",
        "ai chips",
        "ai semiconductor",
        "ai semiconductors",
    ],

    "technology": [
        "technology",
        "tech",
        "software",
        "smartphone",
        "smartphones",
        "iphone",
        "android",
        "cybersecurity",
        "cyber security",
        "semiconductor",
        "semiconductors",
        "chip",
        "chips",
        "processor",
        "processors",
        "gadget",
        "gadgets",
        "electronics",
        "device",
        "devices",
        "internet",
        "app",
        "apps",
        "operating system",
        "windows",
        "macos",
        "linux",
        "5g",
        "6g",
    ],

    "stocks": [
        "stock market",
        "stock price",
        "share price",
        "shares",
        "sensex",
        "nifty",
        "nse",
        "bse",
        "equity market",
        "listed company",
        "ipo",
    ],
}


# ============================================================
# REGIONAL FEED BYPASS
# ============================================================

# These feeds are already specific to the requested region.
# Articles from these feeds should not be rejected just because
# the article text does not literally contain the region name.

REGIONAL_FEED_BYPASS = {
    "telangana": [
        "indianexpress.com/section/cities/hyderabad/feed/",
        "tv9telugu.com/telangana/feed",
    ],

    "karnataka": [
        "indianexpress.com/section/cities/bangalore/feed/",
        "hindustantimes.com/feeds/rss/cities/bengaluru-news/rssfeed.xml",
    ],

    "tamil nadu": [
        "indianexpress.com/section/cities/chennai/feed/",
        "newindianexpress-tamil-nadu",
    ],

    "tamilnadu": [
        "indianexpress.com/section/cities/chennai/feed/",
        "newindianexpress-tamil-nadu",
    ],

    "kerala": [
        "indianexpress.com/section/india/kerala/feed/",
        "timesofindia.indiatimes.com/rssfeeds/878156304.cms",
        "newindianexpress.com/states/kerala",
    ],

    "andhra pradesh": [
        "newindianexpress.com/states/andhra-pradesh",
    ],

    "maharashtra": [
        "newindianexpress.com/states/maharashtra",
    ],

    "maharashtra": [
        "indianexpress.com/section/cities/mumbai/feed/",
        "hindustantimes.com/feeds/rss/cities/mumbai-news/rssfeed.xml",
    ],

    "delhi": [
        "indianexpress.com/section/cities/delhi/feed/",
        "hindustantimes.com/feeds/rss/cities/delhi-news/rssfeed.xml",
    ],
}


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_text(text: str) -> str:

    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<script.*?</script>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<style.*?</style>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = text.replace("\xa0", " ")

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def normalize_text(text: str) -> str:

    text = clean_text(text).lower()

    # Keep Unicode letters and numbers so Telugu,
    # Hindi and other non-English headlines are preserved.
    text = re.sub(
        r"[^\w\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(
        r"_+",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


_NON_ENGLISH_SCRIPT_RE = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff"
    r"\u0900-\u0dff\u0e00-\u0eff\u0f00-\u0fff"
    r"\u1000-\u109f\u10a0-\u10ff\u1200-\u137f"
    r"\u1780-\u17ff\u3040-\u30ff\u3400-\u9fff"
    r"\uac00-\ud7af]"
)


def is_english_text(*values: str) -> bool:
    """Reject copy containing a meaningful amount of non-Latin script."""
    text = " ".join(str(value or "") for value in values)
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return False

    non_english_letters = sum(
        1 for character in letters
        if _NON_ENGLISH_SCRIPT_RE.search(character)
    )
    return non_english_letters <= max(2, int(len(letters) * 0.02))


# ============================================================
# SENTENCE EXTRACTION
# ============================================================

def split_sentences(text: str, latest_mode: bool = False) -> List[str]:

    text = clean_text(text)

    if not text:
        return []

    # Google and publisher RSS descriptions can include Indian initials or
    # honorifics in the middle of a sentence (for example, "V.D. Satheesan",
    # "Edappadi K. Palaniswami", or "Thol. Thirumavalavan"). Protect those
    # periods for Latest only so the summary builder does not discard a name
    # fragment as though it were a complete sentence.
    period_marker = "\uE000"
    if latest_mode:
        text = re.sub(
            r"(?<!\w)(?:[A-Z]\.){1,3}(?=\s+[A-Z])|"
            r"(?i:\b(?:Thol|Dr|Mr|Ms|Mrs|Smt|Shri)\.)(?=\s+[A-Z])",
            lambda match: match.group(0).replace(".", period_marker),
            text,
        )

    # Handle common sentence endings.
    parts = re.split(
        r"(?<=[.!?])\s+(?=[A-Z0-9\"“‘'(\[])",
        text,
    )

    results = []

    for part in parts:

        if latest_mode:
            part = part.replace(period_marker, ".")

        part = part.strip()

        if not part:
            continue

        # Avoid extremely short fragments.
        if len(part) < 35:
            continue

        # Avoid very long navigation / footer fragments.
        if len(part) > 500:
            continue

        results.append(part)

    return results


# ============================================================
# BAD CONTENT FILTER
# ============================================================

BAD_PHRASES = [
    "read more",
    "read the full story",
    "click here",
    "subscribe",
    "newsletter",
    "advertisement",
    "advertising",
    "cookie policy",
    "privacy policy",
    "terms of use",
    "follow us",
    "share this article",
    "download the app",
    "install the app",
    "sign up",
    "login",
    "log in",
    "also read",
    "related stories",
    "related articles",
    "latest news",
    "recommended",
    "trending",
    "you may also like",
    "watch now",
    "listen now",
    "follow our",
    "join our",
]


def is_bad_sentence(sentence: str) -> bool:

    import re

    raw = str(sentence or "").strip()

    if not raw:
        return True

    key = normalize_text(raw)

    if not key:
        return True

    # ---------------------------------------------------------
    # Normalize punctuation/apostrophes for robust UI filtering.
    # This catches both:
    # don't
    # don’t
    # don t
    # ---------------------------------------------------------
    filter_key = raw.lower()
    filter_key = filter_key.replace("’", "'")
    filter_key = filter_key.replace("‘", "'")
    filter_key = filter_key.replace("`", "'")
    filter_key = re.sub(r"[^a-z0-9]+", " ", filter_key)
    filter_key = re.sub(r"\s+", " ", filter_key).strip()

    # ---------------------------------------------------------
    # Very short fragments are usually UI/navigation text.
    # ---------------------------------------------------------
    if len(key) < 35:
        return True

    # ---------------------------------------------------------
    # Existing bad-source phrase rules.
    # ---------------------------------------------------------
    if any(
        phrase in key
        for phrase in BAD_PHRASES
    ):
        return True

    # ---------------------------------------------------------
    # Subscription / account / paywall boilerplate.
    # ---------------------------------------------------------
    bad_boilerplate = [
        "you dont have any active subscription",
        "you don t have any active subscription",
        "you do not have any active subscription",
        "active subscription",
        "account subscription benefits",
        "subscription benefits",
        "subscribed with another email",
        "logout and login",
        "log out and log in",
        "products youve access to",
        "additional subscription benefits",
        "account settings",
        "need help with your subscription",
        "subscribe to continue",
        "subscribe now",
        "premium stories",
        "premium story",
        "login to continue",
        "log in to continue",
        "sign in to continue",
        "create an account",
        "already have an account",
        "please login",
        "please log in",
        "please sign in",
    ]

    if any(
        phrase in filter_key
        for phrase in bad_boilerplate
    ):
        return True

    # ---------------------------------------------------------
    # Metadata / timestamps.
    # ---------------------------------------------------------
    if filter_key.startswith("updated "):
        return True

    if filter_key.startswith("published "):
        return True

    # ---------------------------------------------------------
    # Navigation / UI.
    # ---------------------------------------------------------
    ui_phrases = [
        "home ",
        "menu ",
        "search ",
        "share ",
        "follow us",
        "read more",
        "load more",
        "view all",
        "click here",
        "tap here",
        "download app",
        "get the app",
        "enable notifications",
        "allow notifications",
        "cookie policy",
        "privacy policy",
        "terms of use",
        "advertisement",
        "advertisements",
    ]

    if any(
        filter_key.startswith(phrase)
        for phrase in ui_phrases
    ):
        return True

    if any(
        phrase in filter_key
        for phrase in ui_phrases
        if len(phrase) > 10
    ):
        return True

    return False


# ============================================================
# DESCRIPTION BUILDER
# ============================================================
def build_description(
    title: str,
    candidates: List[str],
    latest_mode: bool = False,
) -> str:
    """
    Build a clear, continuous article description.

    Rules:
    - Source-derived information only.
    - No invented facts.
    - No bullet points.
    - Continuous paragraph.
    - Prefer 2-4 useful sentences.
    - Keep the description concise enough for the
      website's 2-4 line display.
    """

    title_key = normalize_text(title)
    title_words = set(title_key.split())

    sentences = []

    for candidate in candidates:

        if not candidate:
            continue

        candidate = str(candidate)

        # Remove bullet markers.
        candidate = re.sub(
            r"(?m)^\s*[\*\u2022\u25E6\u25AA\u25AB\-]+\s*",
            "",
            candidate,
        )

        # Normalize whitespace.
        candidate = re.sub(
            r"\s+",
            " ",
            candidate,
        ).strip()

        if not candidate:
            continue

        for sentence in split_sentences(candidate, latest_mode=latest_mode):

            sentence = clean_text(sentence)

            if not sentence:
                continue

            # Remove bullet markers again after splitting.
            sentence = re.sub(
                r"^\s*[\*\u2022\u25E6\u25AA\u25AB\-]+\s*",
                "",
                sentence,
            ).strip()

            if not sentence:
                continue

            key = normalize_text(sentence)

            if not key:
                continue

            # Do not repeat the headline as the description.
            if key == title_key:
                continue

            sentence_words = set(key.split())

            # Ignore sentences that are basically just the title.
            if title_words:
                overlap = (
                    len(title_words & sentence_words)
                    / max(len(title_words), 1)
                )

                if (
                    overlap >= 0.90
                    and len(sentence_words)
                    <= len(title_words) + 8
                ):
                    continue

            if is_bad_sentence(sentence):
                continue

            # Remove duplicates.
            duplicate = False

            for existing in sentences:

                existing_key = normalize_text(
                    existing
                )

                if existing_key == key:
                    duplicate = True
                    break

                existing_words = set(
                    existing_key.split()
                )

                union = existing_words | sentence_words

                if union:
                    similarity = (
                        len(
                            existing_words
                            & sentence_words
                        )
                        / len(union)
                    )

                    # Also catch cases where one sentence
                    # contains almost all of the other sentence.
                    containment = (
                        len(
                            existing_words
                            & sentence_words
                        )
                        / max(
                            min(
                                len(existing_words),
                                len(sentence_words)
                            ),
                            1,
                        )
                    )

                    # Catch sentences that repeat most of
                    # the same information with slightly
                    # different wording.
                    shorter_words = min(
                        len(existing_words),
                        len(sentence_words),
                    )

                    if shorter_words:
                        overlap_ratio = (
                            len(
                                existing_words
                                & sentence_words
                            )
                            / shorter_words
                        )
                    else:
                        overlap_ratio = 0

                    if (
                        similarity >= 0.82
                        or containment >= 0.90
                        or overlap_ratio >= 0.88
                    ):
                        duplicate = True
                        break

            if duplicate:
                continue

            # Keep useful complete sentences.
            # RSS summaries commonly use "..." for an unfinished teaser;
            # although that ends with a period, it is not a complete sentence.
            if re.search(r"(?:\.{2,}|…)+\s*[\"'’”)]*$", sentence):
                continue

            if sentence.endswith(
                (".", "!", "?", '"', "”", "’")
            ):
                # Reject near-duplicate sentences that repeat
                # the same named person/topic and core facts.
                sentence_key = normalize_text(sentence)
                sentence_words_lower = set(
                    sentence_key.split()
                )

                repeated = False

                for existing in sentences:
                    existing_key = normalize_text(existing)
                    existing_words_lower = set(
                        existing_key.split()
                    )

                    common = (
                        sentence_words_lower
                        & existing_words_lower
                    )

                    if not common:
                        continue

                    shorter = min(
                        len(sentence_words_lower),
                        len(existing_words_lower),
                    )

                    if shorter < 8:
                        continue

                    common_ratio = len(common) / shorter

                    if common_ratio >= 0.75:
                        repeated = True
                        break

                if not repeated:
                    sentences.append(sentence)

            # Stop once we have enough source material.
            if len(sentences) >= 4:
                break

        if len(sentences) >= 4:
            break

    if not sentences:
        return ""

    # Select a concise amount of genuine source text.
    # Keep complete sentences only and stop before the
    # description becomes too long for the article card.
    selected = []
    # Keep the card copy compact. CSS line clamping remains the final
    # display constraint because line count depends on viewport width.
    max_chars = 900

    for sentence in sentences:
        # Keep source sentences intact. If a sentence is too long for the
        # card, omit it instead of clipping it in the middle.
        if len(sentence) > max_chars:
            continue

        if not selected:
            selected.append(sentence)
            continue

        candidate = " ".join(selected + [sentence])

        if len(candidate) > max_chars:
            break

        selected.append(sentence)

        # Two or three complete sentences are normally enough.
        if len(selected) >= 4:
            break

    result = " ".join(selected)

    # Make it a single continuous paragraph.
    result = re.sub(
        r"\s+",
        " ",
        result,
    ).strip()

    # Remove accidental duplicated punctuation.
    result = re.sub(
        r'([.!?]["”’\'])\.',
        r'\1',
        result,
    )

    # Keep the paragraph reasonably compact.
    # Never invent text to reach a target length.
    if len(result) > 750:
        shortened = result[:750]

        # Prefer ending at a complete sentence.
        match = re.match(
            r"^(.*?[.!?])(?:\s|$)",
            shortened,
            re.DOTALL,
        )

        if match and len(match.group(1)) >= 250:
            result = match.group(1).strip()

    return result


def description_matches_title(title: str, description: str) -> bool:
    """Reject descriptions that clearly belong to a different story."""
    title_words = set(normalize_text(title).split())
    description_words = set(normalize_text(description).split())
    stop_words = {
        "about", "after", "again", "amid", "among", "before", "being",
        "from", "have", "into", "over", "says", "said", "their", "there",
        "these", "those", "today", "when", "where", "which", "while", "with",
        "will", "would", "could", "should", "news", "latest", "live", "update",
        "updates", "report", "reports", "india", "indian", "world", "state",
        "city", "cities", "people", "official", "officials", "according",
        # Common Indian surnames alone are weak evidence that two
        # otherwise different stories are about the same person.
        "kumar", "singh", "sharma", "yadav", "patel", "rao", "reddy",
    }
    signals = {
        word for word in title_words
        if (len(word) >= 4 or word.isdigit()) and word not in stop_words
    }
    if not signals:
        return True
    return bool(signals & description_words)

def parse_date(
    value: str,
) -> Optional[datetime]:

    if not value:
        return None

    try:

        result = parsedate_to_datetime(value)

        if result.tzinfo is None:

            result = result.replace(
                tzinfo=timezone.utc
            )

        return result.astimezone(
            timezone.utc
        )

    except Exception:

        # Some feeds may provide ISO timestamps.
        try:

            iso_value = value.replace(
                "Z",
                "+00:00",
            )

            result = datetime.fromisoformat(
                iso_value
            )

            if result.tzinfo is None:

                result = result.replace(
                    tzinfo=timezone.utc
                )

            return result.astimezone(
                timezone.utc
            )

        except Exception:

            return None


def format_date(
    value: str,
) -> str:

    parsed = parse_date(value)

    if parsed is None:
        return value or ""

    return parsed.isoformat()


# ============================================================
# NETWORK
# ============================================================

def fetch_url(
    url: str,
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[bytes]:

    try:

        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "application/rss+xml, "
                    "application/xml, "
                    "text/xml, "
                    "text/html"
                ),
                "Accept-Language": "en-IN,en;q=0.9",
            },
        )

        with urlopen(
            request,
            timeout=timeout,
        ) as response:

            return response.read()

    except HTTPError as error:

        print(
            f"HTTP ERROR {error.code}: {url}"
        )

    except URLError as error:

        print(
            f"URL ERROR: {error.reason}: {url}"
        )

    except Exception as error:

        print(
            f"FETCH ERROR: {error}: {url}"
        )

    return None


# ============================================================
# RSS PARSER
# ============================================================

def element_text(
    element: Optional[ET.Element],
) -> str:

    if element is None:
        return ""

    return clean_text(
        "".join(
            element.itertext()
        )
    )


def domain_name(
    url: str,
) -> str:

    try:

        host = (
            urlparse(url).hostname
            or ""
        )

        host = host.lower()

        if host.startswith("www."):
            host = host[4:]

        if "indianexpress.com" in host:
            return "The Indian Express"

        if "ndtv.com" in host:
            return "NDTV"

        if "feedburner.com" in host:
            return "NDTV"

        if "hindustantimes.com" in host:
            return "Hindustan Times"

        if "gadgets360.com" in host:
            return "Gadgets 360"

        return host

    except Exception:

        return "News Source"


# ============================================================
# FETCH RSS
# ============================================================


def fetch_new_indian_express_tamil_nadu(
    state_slug="tamil-nadu",
    state_name="Tamil Nadu",
):
    """
    Fetch current state article listings directly from The New
    Indian Express. This page is HTML rather than RSS.

    This is intentionally separate from fetch_rss() because
    the page is HTML, not RSS.
    """

    page_url = f"https://www.newindianexpress.com/states/{state_slug}"
    state_path = f"/states/{state_slug}/"

    try:
        request = Request(
            page_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            },
        )

        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:

            html = response.read().decode(
                "utf-8",
                errors="ignore",
            )

    except HTTPError as error:
        print(
            f"NIE HTTP ERROR {error.code}: {page_url}"
        )
        return []

    except URLError as error:
        print(
            f"NIE URL ERROR: {error.reason}: {page_url}"
        )
        return []

    except Exception as error:
        print(
            f"NIE FETCH ERROR: {error}: {page_url}"
        )
        return []

    class NewIndianExpressParser(HTMLParser):

        def __init__(self):
            super().__init__(
                convert_charrefs=True
            )

            self.links = []
            self.current = None

        def handle_starttag(
            self,
            tag,
            attrs,
        ):

            if tag.lower() != "a":
                return

            attrs_dict = dict(attrs)
            href = (
                attrs_dict.get("href")
                or ""
            ).strip()

            if (
                state_path
                not in href
            ):
                return

            absolute_url = urljoin(
                page_url,
                href,
            )

            self.current = {
                "url": absolute_url,
                "text": [],
            }

        def handle_data(
            self,
            data,
        ):

            if self.current is not None:

                value = data.strip()

                if value:
                    self.current[
                        "text"
                    ].append(value)

        def handle_endtag(
            self,
            tag,
        ):

            if (
                tag.lower() != "a"
                or self.current is None
            ):
                return

            title = clean_text(
                " ".join(
                    self.current["text"]
                )
            )

            url = self.current["url"]

            if (
                title
                and len(title) > 20
                and re.search(
                    r"/20\d{2}/",
                    url,
                )
            ):

                self.links.append(
                    {
                        "title": title,
                        "url": url,
                    }
                )

            self.current = None

    parser = NewIndianExpressParser()

    try:
        parser.feed(html)
    except Exception as error:
        print(
            f"NIE PARSE ERROR: {error}"
        )
        return []

    items = []
    seen_urls = set()

    for item in parser.links:

        url = item["url"]

        if url in seen_urls:
            continue

        seen_urls.add(url)

        match = re.search(
            r"/(20\d{2})/([A-Za-z]{3})/(\d{1,2})/",
            url,
        )

        if not match:
            continue

        year, month, day = match.groups()

        try:
            published_at = datetime.strptime(
                f"{year}/{month}/{day}",
                "%Y/%b/%d",
            ).replace(
                tzinfo=timezone.utc
            ).isoformat()

        except ValueError:
            continue

        items.append(
            {
                "title": item["title"],
                "url": url,
                "description": "",
                "source": "The New Indian Express",
                "published_at": published_at,
                "feed_url": page_url,
            }
        )

    print(
        f"NIE {state_name.upper()} ITEMS: {len(items)}"
    )

    return items


def fetch_rss(
    feed_url: str,
    timeout: int = REQUEST_TIMEOUT,
    force_refresh: bool = False,
) -> List[dict]:

    feed_url = str(
        feed_url or ""
    ).strip()

    if not feed_url:
        return []

    # ========================================================
    # RSS CACHE CHECK
    # ========================================================

    now = datetime.now(
        timezone.utc
    )

    with RSS_CACHE_LOCK:

        cached = RSS_CACHE.get(
            feed_url
        )

        if cached and not force_refresh:

            cached_at, cached_items = cached

            age = (
                now - cached_at
            ).total_seconds()

            if age < RSS_CACHE_TTL:

                print(
                    f"RSS CACHE HIT: {feed_url}"
                )

                return [
                    dict(item)
                    for item in cached_items
                ]

    print(
        f"RSS SOURCE: {feed_url}"
    )

    raw = fetch_url(
        feed_url,
        timeout,
    )

    # ========================================================
    # LIVE FETCH FAILED
    # ========================================================

    if not raw:

        print(
            "RSS FAILED"
        )

        # If live fetching fails, use stale cache
        # as a fallback instead of returning nothing.

        with RSS_CACHE_LOCK:

            cached = RSS_CACHE.get(
                feed_url
            )

            if cached and not force_refresh:

                print(
                    f"RSS STALE CACHE FALLBACK: {feed_url}"
                )

                return [
                    dict(item)
                    for item in cached[1]
                ]

        return []

    try:

        root = ET.fromstring(
            raw
        )

    except Exception as error:

        print(
            f"RSS XML ERROR: {error}"
        )

        # Use stale cache when RSS XML is invalid.

        with RSS_CACHE_LOCK:

            cached = RSS_CACHE.get(
                feed_url
            )

            if cached and not force_refresh:

                print(
                    f"RSS STALE CACHE FALLBACK: {feed_url}"
                )

                return [
                    dict(item)
                    for item in cached[1]
                ]

        return []

    results = []

    # OnJob supplies live vacancy records in Indeed-style XML rather than
    # RSS <item> entries. Normalize them into the existing article shape so
    # the Jobs pipeline can apply its usual freshness, deduplication,
    # description, and publisher-balance checks.
    if (
        feed_url.rstrip("/").lower() == ONJOB_FEED_URL.rstrip("/").lower()
        and root.tag.lower() == "source"
    ):
        for job in root.findall("./job")[:MAX_FEED_ITEMS]:
            title = element_text(job.find("title"))
            link = element_text(job.find("url"))
            company = element_text(job.find("company"))
            description = clean_text(element_text(job.find("description")))
            published = element_text(job.find("date"))
            location_parts = [
                element_text(job.find(field))
                for field in ("city", "state", "country")
            ]
            location_parts = [
                re.sub(r"-\d+$", "", part).strip()
                for part in location_parts
                if part
            ]
            remote = element_text(job.find("remote")).lower() in {
                "yes", "true", "1"
            }
            location = ", ".join(location_parts)
            if remote:
                location = f"Remote · {location}" if location else "Remote"
            if location:
                description = f"Location: {location}. {description}"
            if not title or not link or not company or not description:
                continue
            results.append({
                "title": title,
                "url": link,
                "description": description,
                "source": company,
                "published_at": published,
                "feed_url": feed_url,
                "location": location,
            })
    else:

      for item in root.findall(
          ".//item"
      ):

        title = element_text(
            item.find("title")
        )

        link = element_text(
            item.find("link")
        )

        # --------------------------------------------------------
        # Get the best description available from the RSS item.
        # --------------------------------------------------------

        description = element_text(
            item.find("description")
        )

        content_encoded = element_text(
            item.find(
                "{http://purl.org/rss/1.0/modules/content/}encoded"
            )
        )

        media_content = element_text(
            item.find(
                "{http://search.yahoo.com/mrss/}content"
            )
        )

        description_parts = []

        for value in (
            description,
            content_encoded,
            media_content,
        ):

            value = clean_text(
                value
            )

            if not value:
                continue

            sentences = split_sentences(
                value
            )

            for sentence in sentences:

                sentence = clean_text(
                    sentence
                )

                if not sentence:
                    continue

                lower = sentence.lower()

                if lower in {
                    "representative image",
                    "representative image.",
                    "file photo",
                    "file photo.",
                }:
                    continue

                if lower.startswith(
                    (
                        "representative image",
                        "image credit",
                        "photo credit",
                        "file photo",
                        "bihar minister ",
                    )
                ):
                    continue

                if len(sentence.split()) < 5:
                    continue

                if sentence not in description_parts:
                    description_parts.append(
                        sentence
                    )

        description = " ".join(
            description_parts
        )

        published = element_text(
            item.find("pubDate")
        )

        source_element = item.find(
            "source"
        )

        source = element_text(
            source_element
        )

        if not source:

            source = domain_name(
                link
            )

        if not title or not link:
            continue

        results.append(
            {
                "title": title,
                "url": link,
                "description": description,
                "source": source,
                "published_at": published,
                "feed_url": feed_url,
            }
        )

        if len(results) >= MAX_FEED_ITEMS:
            break

    print(
        f"RSS ITEMS: {len(results)}"
    )

    # ========================================================
    # SAVE RSS CACHE
    # ========================================================

    with RSS_CACHE_LOCK:

        RSS_CACHE[
            feed_url
        ] = (
            now,
            [
                dict(item)
                for item in results
            ],
        )

    print(
        f"RSS CACHE SAVED: {feed_url}"
    )

    return results


# ============================================================
# ARTICLE PAGE PARSER
# ============================================================

class ArticleParser(HTMLParser):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.meta = {}
        self.paragraphs = []

        self.current_paragraph = []
        self.inside_paragraph = False

        self.skip_depth = 0

        self.skip_tags = {
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
            "button",
        }

        self.article_depth = 0

        self.article_tags = {
            "article",
        }

        self.block_tags = {
            "p",
            "div",
            "section",
            "li",
        }

    def handle_starttag(
        self,
        tag,
        attrs,
    ):

        tag = tag.lower()
        attrs_dict = dict(attrs)

        if tag == "meta":

            name = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
            )

            content = attrs_dict.get("content")

            if name and content:

                self.meta[
                    name.lower()
                ] = clean_text(content)

            return

        if tag in self.skip_tags:

            self.skip_depth += 1
            return

        if tag in self.article_tags:

            self.article_depth += 1

        if (
            tag == "p"
            and self.skip_depth == 0
        ):

            self.inside_paragraph = True
            self.current_paragraph = []

    def handle_endtag(
        self,
        tag,
    ):

        tag = tag.lower()

        if tag in self.skip_tags:

            if self.skip_depth > 0:
                self.skip_depth -= 1

            return

        if tag == "article":

            if self.article_depth > 0:
                self.article_depth -= 1

            return

        if tag != "p":
            return

        if self.inside_paragraph:

            text = clean_text(
                " ".join(
                    self.current_paragraph
                )
            )

            if text:

                self.paragraphs.append(
                    text
                )

        self.inside_paragraph = False
        self.current_paragraph = []

    def handle_data(
        self,
        data,
    ):

        if (
            self.inside_paragraph
            and self.skip_depth == 0
        ):

            self.current_paragraph.append(
                data
            )


# ============================================================
# ARTICLE PAGE DESCRIPTION
# ============================================================

def get_publisher_description(
    url: str,
    title: str,
    rss_description: str = "",
    timeout: int = ARTICLE_TIMEOUT,
    latest_mode: bool = False,
) -> str:

    raw = fetch_url(url, timeout)

    if not raw:
        return build_description(title, [rss_description], latest_mode=latest_mode)

    try:
        page = raw.decode("utf-8", errors="ignore")
    except Exception:
        return build_description(title, [rss_description], latest_mode=latest_mode)

    parser = ArticleParser()

    try:
        parser.feed(page)
        parser.close()
    except Exception:
        return build_description(title, [rss_description], latest_mode=latest_mode)

    # Collect genuine article paragraphs.
    #
    # Some publisher pages begin with a short bullet-style
    # summary before the actual article body. Those bullets
    # can contain sentence fragments, so skip that block when
    # normal article paragraphs are available.
    raw_paragraphs = parser.paragraphs[:MAX_PARAGRAPHS_TO_CHECK]

    bullet_count = sum(
        1
        for paragraph in raw_paragraphs[:8]
        if str(paragraph).lstrip().startswith((
            "*",
            "•",
            "◦",
            "▪",
            "▫",
            "-"
        ))
    )

    if bullet_count >= 2:
        first_normal = None

        for index, paragraph in enumerate(raw_paragraphs):
            cleaned = clean_text(paragraph)

            if not cleaned:
                continue

            if str(paragraph).lstrip().startswith((
                "*",
                "•",
                "◦",
                "▪",
                "▫",
                "-"
            )):
                continue

            # Skip obvious section headings.
            if len(cleaned.split()) < 5:
                continue

            first_normal = index
            break

        if first_normal is not None:
            raw_paragraphs = raw_paragraphs[first_normal:]

    article_paragraphs = []

    for paragraph in raw_paragraphs:
        text = clean_text(paragraph)

        if not text:
            continue

        if is_bad_sentence(text):
            continue

        lower = text.lower()

        # Ignore author/profile information
        if (
            "principal correspondent" in lower
            or "expertise and experience" in lower
            or "specialized conflict reporting" in lower
            or "diverse investigative background" in lower
            or "frontline journalism" in lower
        ):
            continue

        article_paragraphs.append(text)

    # Prefer actual article paragraphs
    if article_paragraphs:
        description = build_description(
            title,
            article_paragraphs,
            latest_mode=latest_mode,
        )

        sentence_count = (
            len(split_sentences(description, latest_mode=latest_mode))
            if description
            else 0
        )

        # Source-specific minimum description length.
        #
        # NDTV and The Indian Express may provide only a short
        # genuine source description because their publisher pages
        # can restrict full article extraction.
        #
        # All other sources require at least 3 genuine sentences.
        source_text = f"{url} {title}".lower()

        if (
            "ndtv.com" in source_text
            or "indianexpress.com" in source_text
        ):
            minimum_sentences = 2
        else:
            minimum_sentences = 3

        if sentence_count >= minimum_sentences:
            return description

    # Fallback: RSS + article paragraphs
    candidates = []

    if rss_description:
        candidates.append(rss_description)

    candidates.extend(article_paragraphs)

    description = build_description(
        title,
        candidates,
        latest_mode=latest_mode,
    )

    sentence_count = (
        len(split_sentences(description, latest_mode=latest_mode))
        if description
        else 0
    )

    if description and sentence_count >= 3:
        return description

    # Final fallback: metadata descriptions
    meta_candidates = []

    for key in [
        "og:description",
        "twitter:description",
        "description",
        "article:description",
    ]:
        value = parser.meta.get(key, "")

        if value:
            meta_candidates.append(value)

    candidates.extend(meta_candidates)

    return build_description(
        title,
        candidates,
    )
# ============================================================
# TITLE CLEANING
# ============================================================

def clean_title(
    title: str,
    source: str,
) -> str:

    title = clean_text(
        title
    )

    source = clean_text(
        source
    )

    if source:

        suffix = (
            " - "
            + source
        )

        if title.lower().endswith(
            suffix.lower()
        ):

            title = title[
                : -len(suffix)
            ].rstrip()

    # Remove common publisher suffixes.
    for suffix in [
        " | The Indian Express",
        " | Hindustan Times",
        " | NDTV",
        " - NDTV",
    ]:

        if title.lower().endswith(
            suffix.lower()
        ):

            title = title[
                : -len(suffix)
            ].rstrip()

    return title


# ============================================================
# RECENCY
# ============================================================

def is_recent(
    value: str,
    max_age_hours: int = 48,
) -> bool:

    parsed = parse_date(
        value
    )

    # Require a valid publication date and keep each category within its
    # configured freshness window.
    if parsed is None:
        return False

    now = datetime.now(
        timezone.utc
    )

    age = (
        now - parsed
    ).total_seconds()

    # Allow a small future clock difference.
    if age < -7200:
        return False

    return age <= max_age_hours * 60 * 60


def compact_card_description(title: str, description: str, max_chars: int = 360) -> str:
    """Keep a few complete source sentences for a readable article card."""
    complete = build_description(title, [description])
    selected = []
    for sentence in split_sentences(complete):
        sentence = clean_text(sentence)
        if not sentence or not re.search(r"[.!?][\"'’”)]*$", sentence):
            continue
        candidate = " ".join(selected + [sentence])
        if len(candidate) > max_chars:
            break
        selected.append(sentence)
        if len(selected) >= 4:
            break
    compact = " ".join(selected)
    # Preserve the complete source summary if its first whole sentence alone
    # exceeds the card target; never clip a sentence to force a line count.
    return compact or complete


def resolve_google_news_urls(urls: List[str], timeout: float = 8.0) -> List[str]:
    """Resolve Google News RSS links in one bounded batch.

    Google News RSS snippets often contain only a headline and publisher.
    Resolving the redirect lets Weather cards use the publisher's actual
    article text instead of dropping every short RSS excerpt.
    """
    now = time.time()
    resolved = {}
    pending = []
    for url in urls:
        key = str(url or "").strip()
        if not key:
            resolved[key] = ""
            continue
        with GOOGLE_NEWS_URL_CACHE_LOCK:
            cached = GOOGLE_NEWS_URL_CACHE.get(key)
        if cached and now - cached[0] < GOOGLE_NEWS_URL_CACHE_TTL:
            resolved[key] = cached[1]
        else:
            pending.append(key)

    if pending:
        try:
            from googlenewsdecoder import gnews_decoder_async

            decoded = asyncio.run(
                gnews_decoder_async(
                    pending,
                    timeout=timeout,
                    concurrency=8,
                )
            )
        except Exception as error:
            print("GOOGLE NEWS URL RESOLUTION FAILED:", error)
            decoded = []

        with GOOGLE_NEWS_URL_CACHE_LOCK:
            for source_url, result in zip(pending, decoded):
                target_url = str(
                    result.get("decoded_url", "")
                    if result.get("success")
                    else ""
                ).strip()
                resolved[source_url] = target_url
                if target_url:
                    GOOGLE_NEWS_URL_CACHE[source_url] = (now, target_url)

    return [resolved.get(str(url or "").strip(), "") for url in urls]



# ============================================================
# TV9 TELUGU -> ENGLISH
# ============================================================

def translate_tv9_article(title, description=""):
    """
    Translate TV9 Telugu headline and RSS description into English
    using a single Ollama request.
    """

    from ollama_service import call_ollama

    original_title = str(title or "").strip()
    original_description = str(description or "").strip()

    if not original_title:
        return original_title, original_description

    cache_key = (
        original_title,
        original_description,
    )

    now = time.time()

    with TV9_TRANSLATION_CACHE_LOCK:
        cached = TV9_TRANSLATION_CACHE.get(cache_key)

    if cached:
        cached_at, cached_title, cached_description = cached

        if now - cached_at < TV9_TRANSLATION_CACHE_TTL:
            print("TV9 TRANSLATION CACHE HIT")
            return cached_title, cached_description

    prompt = f"""
You are a professional Telugu-to-English news translator.

Translate the following Telugu news article fields into clear,
natural, professional English.

STRICT RULES:
- Preserve the exact meaning and factual information.
- Do NOT invent, guess, replace or modify any person's name.
- Telugu person names must be transliterated faithfully into English.
- Preserve political party names, organizations, institutions and places.
- Preserve every number, date, percentage, amount and currency value.
- Do NOT change who did what to whom.
- Do NOT infer missing information.
- Do NOT add background information.
- Do NOT summarize.
- Do NOT add sensational wording.
- Translate headline wordplay naturally while preserving its intended meaning.

Return ONLY valid JSON in exactly this format:

{{
  "title": "English translated headline",
  "description": "English translated description"
}}

TELUGU TITLE:
{original_title}

TELUGU DESCRIPTION:
{original_description}
"""

    try:
        translated = call_ollama(
            prompt,
            json_mode=True,
            model="translategemma:4b-it-q4_K_M",
            num_predict=160,
        )

        import json

        if translated:
            data = json.loads(translated)

            translated_title = str(
                data.get("title", "")
            ).strip()

            translated_description = str(
                data.get("description", "")
            ).strip()

            if translated_title and translated_description:
                with TV9_TRANSLATION_CACHE_LOCK:
                    TV9_TRANSLATION_CACHE[cache_key] = (
                        time.time(),
                        translated_title,
                        translated_description,
                    )

                print("TV9 TITLE + DESCRIPTION TRANSLATED IN ONE CALL")
                print("TV9 TRANSLATION CACHE SAVED")

                return (
                    translated_title,
                    translated_description,
                )

    except Exception as error:
        print(
            "TV9 COMBINED TRANSLATION ERROR:",
            error
        )

    print("TV9 COMBINED TRANSLATION FAILED")

    return (
        original_title,
        original_description or original_title,
    )


# ============================================================
# ARTICLE BUILDING
# ============================================================

def process_article(
    item: dict,
    rss_fallback_mode: bool = False,
    fast_search_mode: bool = False,
    max_age_hours: int = 48,
    latest_mode: bool = False,
) -> Optional[dict]:

    title = clean_title(
        item.get(
            "title",
            "",
        ),
        item.get(
            "source",
            "",
        ),
    )

    url = (
        item.get(
            "url",
            "",
        )
        or ""
    ).strip()

    if not title or not url:
        return None

    # --------------------------------------------------------
    # Skip TV9 photo-gallery pages.
    # Keep normal TV9 news articles.
    # --------------------------------------------------------

    feed_url = str(
        item.get("feed_url", "")
    ).lower()

    if (
        "tv9telugu.com" in feed_url
        and "/photo-gallery/" in url.lower()
    ):
        print("SKIPPING TV9 PHOTO GALLERY:", title)
        return None

    # Apply the category's freshness limit to normal and fallback paths.
    if not is_recent(
        item.get(
            "published_at",
            "",
        ),
        max_age_hours=max_age_hours,
    ):
        return None

    # ========================================================
    # RSS-FIRST DESCRIPTION
    # ========================================================
    # RSS already provides a source-derived description. For fallback
    # candidates, use it directly to avoid slow or blocked publisher pages.
    # The same publication cutoff applies in either processing mode.
    # ========================================================

    rss_description = str(
        item.get(
            "description",
            "",
        )
        or ""
    ).strip()

    # Google News sometimes supplies its own site-wide tagline as the
    # article description. It passes length checks but is not story copy.
    # For those entries only, resolve the publisher URL and extract a real
    # source description; omit the card if the publisher copy is unavailable.
    google_news_boilerplate = (
        "comprehensive up to date news coverage aggregated from sources "
        "all over the world by google news"
    )
    if google_news_boilerplate in normalize_text(rss_description):
        if "news.google.com" in url.lower():
            resolved_urls = resolve_google_news_urls([url], timeout=4)
            url = resolved_urls[0] if resolved_urls else ""
        if not url:
            return None

        publisher_copy = get_publisher_description(
            url,
            title,
            "",
            timeout=4,
            latest_mode=latest_mode,
        )
        publisher_copy = build_description(
            title,
            [publisher_copy],
            latest_mode=latest_mode,
        )
        if (
            not publisher_copy
            or google_news_boilerplate in normalize_text(publisher_copy)
            or len(publisher_copy) < 80
            or len(publisher_copy.split()) < 8
        ):
            return None
        rss_description = publisher_copy

    # ========================================================
    # RSS-FIRST FOR PUBLISHERS THAT RESTRICT ARTICLE PAGES
    # ========================================================
    # Some publishers consistently return 403/405 responses
    # when their article pages are requested. Their RSS feeds
    # already provide source-derived descriptions, so avoid
    # unnecessary page requests for those publishers.
    # ========================================================

    article_host = url.lower()

    rss_only_hosts = (
        "gadgets360.com",
        "arstechnica.com",
        "onjob.io",

        # Regional RSS feeds already provide source-derived
        # descriptions. Avoid unnecessary publisher-page
        # requests for faster regional category loading.
        "thehindu.com",
        "telanganatoday.com",
        "indianexpress.com",
        "tv9telugu.com",
        "thehansindia.com",
    )

    # --------------------------------------------------------
    # RSS-FIRST ARTICLE PROCESSING
    # --------------------------------------------------------
    # RSS descriptions are already available from the feed and
    # are sufficient for category filtering and source balancing.
    # Avoid publisher-page requests for regional/category feeds.
    # This prevents slow publisher pages from making an entire
    # category wait several tens of seconds.
    # --------------------------------------------------------

    regional_rss_hosts = (
        "thehindu.com",
        "telanganatoday.com",
        "indianexpress.com",
        "tv9telugu.com",
        "thehansindia.com",
        "deccanherald.com",
        "sakshi.com",
        "eenadu.net",
        "greatandhra.com",
        "newindianexpress.com",
        "onmanorama.com",
        "mathrubhumi.com",
        "manoramaonline.com",
        "timesofindia.indiatimes.com",
        "hindustantimes.com",
        "firstpost.com",
        "ndtv.com",
    )

    is_rss_first = (
        rss_fallback_mode
        or any(
            host in article_host
            for host in rss_only_hosts
        )
        or any(
            host in article_host
            for host in regional_rss_hosts
        )
    )

    if is_rss_first or (
        fast_search_mode
        and len(build_description(title, [rss_description], latest_mode=latest_mode)) >= 80
        and len(build_description(title, [rss_description], latest_mode=latest_mode).split()) >= 8
    ):
        description = build_description(
            title,
            [rss_description],
            latest_mode=latest_mode,
        )
    else:
        description = get_publisher_description(
            url,
            title,
            rss_description,
            timeout=4 if fast_search_mode else ARTICLE_TIMEOUT,
            latest_mode=latest_mode,
        )
    # If the RSS feed has no usable description, keep the
         # --------------------------------------------------------
    # NDTV SHORT-DESCRIPTION FIX
    # --------------------------------------------------------
    # NDTV publisher pages may return HTTP 403.
    # Do not ask Ollama to invent missing facts.
    # Keep the genuine source description when the page
    # cannot be extracted.
    # --------------------------------------------------------

    # Keep missing source copy empty rather than substituting the
    # headline or inventing filler.
    if not description:
        description = ""

    # RSS-first feeds occasionally publish an empty or one-line
    # description. Try the article page only for those weak cases;
    # keep the summary source-derived and reject the card if the
    # publisher provides too little usable text.
    description = build_description(
        title,
        [description, rss_description],
        latest_mode=latest_mode,
    )

    # A source can return a long but unrelated blurb (for example, a
    # live-update headline paired with a different story's summary).
    # Enrich weak or mismatched RSS text from the article page, then
    # reject the item if the page still cannot provide a related summary.
    latest_initial_cutoff = latest_mode and bool(
        re.search(
            r"(?<!U\.)(?<!E\.)(?<!\w)[A-Z]\.\s+"
            r"(?:He|She|The|With|And|But|His|Her|It|They|This|That|Before|After)\b"
            r"|\bThol\.\s+(?:He|She|The|With|And|But|His|Her|It|They|This|That|Before|After)\b",
            description,
            flags=re.IGNORECASE,
        )
    )
    # Latest RSS feeds often publish a short teaser instead of an article
    # summary. Try the publisher page for those short cards so Latest can show
    # the same useful amount of source text as the regional categories.
    latest_short_teaser = latest_mode and (
        len(description) < 360
        or len(description.split()) < 45
        or bool(re.search(r"(?:\.{2,}|…)+\s*[\"'’”)]*$", description))
    )

    if (
        (not fast_search_mode or latest_initial_cutoff or latest_short_teaser)
        and (
        latest_initial_cutoff
        or latest_short_teaser
        or
        len(description) < 280
        or len(description.split()) < 20
        or len(split_sentences(description, latest_mode=latest_mode)) < 2
        or (
            not is_active_job_listing(item)
            and not description_matches_title(title, description)
        )
        )
    ):
        page_description = get_publisher_description(
            url,
            title,
            rss_description,
            timeout=2 if fast_search_mode else ARTICLE_TIMEOUT,
            latest_mode=latest_mode,
        )
        page_summary = build_description(
            title,
            [page_description],
            latest_mode=latest_mode,
        )
        if (
            page_summary
            and (
                is_active_job_listing(item)
                or description_matches_title(title, page_summary)
            )
            and len(page_summary) >= 280
            and len(page_summary.split()) >= 20
        ):
            description = page_summary

    if (
        not fast_search_mode
        and
        not is_active_job_listing(item)
        and not description_matches_title(title, description)
    ):
        return None

    min_description_chars = 80 if fast_search_mode else 280
    min_description_words = 8 if fast_search_mode else 20

    if (
        len(description) < min_description_chars
        or len(description.split()) < min_description_words
    ):
        return None

    published_at = format_date(
        item.get("published_at", "")
    )
    if parse_date(published_at) is None:
        return None

    # --------------------------------------------------------
    # TV9 Telugu articles are translated to English.
    # Other sources remain unchanged.
    # --------------------------------------------------------

    feed_url = str(
        item.get("feed_url", "")
    ).lower()

    # TV9 Telugu translation is delayed until AFTER source balancing.
    # This prevents Ollama from translating many candidate articles
    # that will later be discarded.

    source = (
        item.get("source")
        or domain_name(url)
    )

    # Normalize escaped publisher names.
    source = str(source).replace("\\", "")

    # Separate major Kerala editions for source balancing.
    source_feed = str(
        item.get("feed_url", "")
    ).lower()

    if "thehindu.com/news/national/kerala/feeder" in source_feed:
        source = "The Hindu - Kerala"
    elif "thehindu.com/news/cities/kochi/feeder" in source_feed:
        source = "The Hindu - Kochi"
    elif "thehindu.com/news/cities/thiruvananthapuram/feeder" in source_feed:
        source = "The Hindu - Thiruvananthapuram"
    elif "thehindubusinessline.com" in source_feed:
        source = "The Hindu BusinessLine"

    return {
        "title": title,
        "description": description,
        "url": url,
        "source": source,
        "published_at": published_at,
        "feed_url": item.get(
            "feed_url",
            "",
        ),
    }


# ============================================================
# REGIONAL FILTER
# ============================================================

def feed_is_region_specific(
    feed_url: str,
    category: str,
) -> bool:

    bypass_feeds = (
        REGIONAL_FEED_BYPASS.get(
            category,
            [],
        )
    )

    feed_lower = feed_url.lower()

    return any(
        bypass.lower() in feed_lower
        for bypass in bypass_feeds
    )


def cross_category_relevance(
    text: str,
    category: str,
) -> set:
    """
    Return other specific categories strongly indicated by the article text.
    Used to prevent broad categories from unnecessarily accepting
    stories that are clearly regional or topic-specific.
    """

    text = str(text or "").lower()
    category = str(category or "").strip().lower()

    detected = set()

    category_groups = {
        "telangana": [
            "telangana",
            "hyderabad",
            "secunderabad",
            "warangal",
            "hanamkonda",
            "nizamabad",
            "karimnagar",
            "khammam",
        ],
        "andhra pradesh": [
            "andhra pradesh",
            "vijayawada",
            "visakhapatnam",
            "vizag",
            "tirupati",
            "guntur",
            "nellore",
            "kakinada",
            "amaravati",
            "rajahmundry",
        ],
        "karnataka": [
            "karnataka",
            "bengaluru",
            "bangalore",
            "mysurean",
            "mysorean",
            "kannadiga",
            "mysuru",
            "mysore",
            "mangaluru",
            "mangalore",
        ],
        "tamil nadu": [
            "tamil nadu",
            "chennai",
            "ennore",
            "coimbatore",
            "madurai",
            "salem",
            "tiruchirappalli",
            "trichy",
        ],
        "kerala": [
            "kerala",
            "kochi",
            "thiruvananthapuram",
            "trivandrum",
            "kozhikode",
            "calicut",
            "kannur",
        ],
        "maharashtra": [
            "maharashtra",
            "mumbai",
            "pune",
            "nagpur",
            "nashik",
            "thane",
        ],
        "delhi": [
            "delhi",
            "new delhi",
            "national capital",
        ],
        "ai": [
            "artificial intelligence",
            "generative ai",
            "genai",
            "large language model",
            "llm",
            "chatgpt",
            "openai",
            "gemini",
            "claude",
            "copilot",
            "machine learning",
        ],
        "technology": [
            "technology",
            "tech",
            "software",
            "smartphone",
            "iphone",
            "android",
            "cybersecurity",
            "semiconductor",
            "chip",
            "processor",
            "gadget",
        ],
    }

    for group, keywords in category_groups.items():
        if group == category:
            continue

        if any(keyword in text for keyword in keywords):
            detected.add(group)

    return detected


def matches_category(
    item: dict,
    category: str,
    feed_url: str = "",
) -> bool:

    category = str(category or "").strip().lower()
    feed_url = str(feed_url or "").lower()

    # Latest/general news does not need regional filtering.
    if category == "latest":
        return True

    title = str(item.get("title", "") or "").lower()
    description = str(item.get("description", "") or "").lower()

    # ========================================================
    # AI URL SEPARATION
    # ========================================================
    # Indian Express exposes some AI articles in BOTH:
    #   /technology/artificial-intelligence/feed/
    #   /technology/feed/
    #
    # Keep articles whose canonical URL is explicitly inside
    # the dedicated AI section in the AI category only.
    article_url = str(
        item.get("url", "")
        or item.get("link", "")
        or ""
    ).lower()

    if (
        category == "technology"
        and "indianexpress.com" in article_url
        and "/technology/artificial-intelligence/" in article_url
    ):
        return False

    if (
        category == "ai"
        and "indianexpress.com" in article_url
        and "/technology/artificial-intelligence/" not in article_url
    ):
        # Do not pull ordinary Technology articles into AI merely
        # because they contain a generic AI-related word.
        pass

    # Some RSS feeds, especially Deccan Herald, place the
    # actual article text in content:encoded instead of
    # description. Include it when checking relevance.
    encoded = str(
        item.get("encoded", "")
        or item.get("content_encoded", "")
        or ""
    ).lower()

    text = f"{title} {description}"

    # ========================================================
    # STRICT KARNATAKA FILTER
    # ========================================================
    # Broad publisher feeds are used for Karnataka, so an article
    # must have a meaningful Karnataka location/state reference.
    if category == "karnataka":

        karnataka_locations = [
            "karnataka",
            "bengaluru",
            "bangalore",
            "mysuru",
            "mysore",
            "mysurean",
            "mysorean",
            "kannadiga",
            "mangaluru",
            "mangalore",
            "hubballi",
            "hubli",
            "belagavi",
            "belgaum",
            "shivamogga",
            "shimoga",
            "tumakuru",
            "tumkur",
            "ballari",
            "bellary",
            "dharwad",
            "kalaburagi",
            "gulbarga",
            "udupi",
            "dakshina kannada",
            "kodagu",
            "coorg",
            "hassan",
            "mandya",
            "vijayapura",
            "bijapur",
            "chikkamagaluru",
            "chikmagalur",
            "mysurean",
            "mysorean",
            "kannadiga",
            "chikkaballapur",
            "kolar",
            "ramanagara",
            "raichur",
            "bidar",
            "yadgir",
            "bagalkot",
            "chamarajanagar",
            "davangere",
            "davanagere",
            "chitradurga",
            "koppal",
            "haveri",
            "gadag",
            "uttara kannada",
            "karwar",
            "chikkodi",
            "vijayanagara",
            "madikeri",
        ]

        other_state_locations = [
            "kerala",
            "kollam",
            "kochi",
            "thiruvananthapuram",
            "tamil nadu",
            "chennai",
            "hyderabad",
            "telangana",
            "andhra pradesh",
            "vijayawada",
            "visakhapatnam",
            "maharashtra",
            "mumbai",
            "pune",
            "delhi",
            "new delhi",
            "uttar pradesh",
            "nagaland",
            "mokokchung",
            "tuensang",
            "assam",
            "guwahati",
            "west bengal",
            "kolkata",
            "odisha",
            "bhubaneswar",
            "rajasthan",
            "jaipur",
            "gujarat",
            "ahmedabad",
            "punjab",
            "haryana",
            "bihar",
            "jharkhand",
            "patna",
            "jammu",
            "kashmir",
            "srinagar",
        ]

        title_lower = title.lower()
        description_lower = description.lower()

        # Strongest signal: Karnataka location in the title.
        title_has_karnataka = any(
            keyword in title_lower
            for keyword in karnataka_locations
        )

        if title_has_karnataka:
            return True

        # Description must contain Karnataka relevance.
        description_has_karnataka = any(
            keyword in description_lower
            for keyword in karnataka_locations
        )

        if not description_has_karnataka:
            return False

        # Reject stories whose description clearly identifies
        # another state/location as the actual subject.
        has_other_location = any(
            keyword in description_lower
            for keyword in other_state_locations
        )

        if has_other_location:
            return False

        return True

    # Dedicated AI feeds are already category-specific.
    # Do not reject valid AI stories merely because their
    # headline uses the short "AI" term instead of one of the
    # longer AI keyword phrases.
    if category == "ai":
        ai_feed_markers = (
            "indianexpress.com/section/technology/artificial-intelligence/feed",
            "techcrunch.com/category/artificial-intelligence/feed",
            "arstechnica.com/ai/feed",
            "wired.com/feed/tag/ai/latest/rss",
        )

        if any(
            marker in feed_url
            for marker in ai_feed_markers
        ):
            return True

    # Dedicated Technology feeds are already category-specific.
    # Accept genuine Technology stories from these feeds even when
    # the headline does not contain one of the narrower technology
    # keyword signals.
    if category == "technology":
        technology_feed_markers = (
            "timesofindia.indiatimes.com/rssfeeds/66949542",
            "indianexpress.com/section/technology/feed",
            "feeds.feedburner.com/gadgets360-latest",
            "hindustantimes.com/feeds/rss/technology/rssfeed.xml",
            "thehindu.com/sci-tech/technology/feeder/default.rss",
        )

        if any(
            marker in feed_url
            for marker in technology_feed_markers
        ):
            return True

    keywords = CATEGORY_KEYWORDS.get(category, [])

    # Keep the weather-specific relevance gate active even though Weather
    # does not use the generic keyword table.
    if not keywords and category != "weather":
        return True

    # ========================================================
    # CROSS-CATEGORY SEPARATION
    # ========================================================
    # Prevent broad categories from accepting articles that are
    # clearly about a more specific category.
    #
    # This is intentionally conservative: a simple mention of
    # another category must NOT reject an otherwise relevant story.
    detected_categories = cross_category_relevance(
        text,
        category,
    )

    # India should represent broader national stories rather than
    # stories whose primary subject is clearly a specific state.
    if category == "india":
        regional_categories = {
            "telangana",
            "andhra pradesh",
            "karnataka",
            "tamil nadu",
            "kerala",
            "maharashtra",
            "delhi",
        }

        regional_hits = detected_categories & regional_categories

        if regional_hits:
            # Require a stronger India-wide signal to keep the story.
            india_strong_signals = [
                "india",
                "indian government",
                "central government",
                "union government",
                "supreme court",
                "parliament",
                "lok sabha",
                "rajya sabha",
                "prime minister",
                "president of india",
                "national",
                "nationwide",
                "across india",
            ]

            if not any(
                signal in text
                for signal in india_strong_signals
            ):
                return False

    # ========================================================
    # STRICT AI / TECHNOLOGY SEPARATION
    # ========================================================
    # AI-focused stories belong to AI.
    # General technology stories belong to Technology.
    #
    # This is intentionally title/description/content based so
    # an AI story cannot enter Technology merely because it also
    # contains generic technology words such as "software",
    # "technology", "platform", "device", or "chip".

    ai_primary_signals = [
        "artificial intelligence",
        "artificial-intelligence",
        "generative ai",
        "generative-ai",
        "genai",
        "large language model",
        "large-language model",
        "large language models",
        "llm",
        "llms",
        "machine learning",
        "deep learning",
        "neural network",
        "neural networks",
        "ai agent",
        "ai agents",
        "ai assistant",
        "ai assistants",
        "ai model",
        "ai models",
        "ai chatbot",
        "ai chatbots",
        "ai tool",
        "ai tools",
        "ai system",
        "ai systems",
        "ai platform",
        "ai platforms",
        "ai-powered",
        "ai powered",
        "ai-driven",
        "ai driven",
        "ai startup",
        "ai startups",
        "openai",
        "chatgpt",
        "gpt-4",
        "gpt-5",
        "gemini",
        "claude",
        "copilot",
        "anthropic",
        "deepmind",
        "nvidia ai",
        "ai chip",
        "ai chips",
        "ai semiconductor",
        "ai semiconductors",
    ]

    has_ai_signal = any(
        signal in text
        for signal in ai_primary_signals
    )

    if category == "technology" and has_ai_signal:
        return False

    if category == "ai":
        # AI must have an explicit AI-related signal.
        if not has_ai_signal:
            return False

    if category == "weather":
        # Weather must be the article's headline topic. A mention in an RSS
        # description cannot pull a market or general-news story into Weather.
        weather_terms = (
            "weather", "forecast", "weather forecast", "rain forecast", "storm forecast",
            "temperature forecast", "monsoon forecast", "rain", "rainfall", "monsoon",
            "cyclone", "hurricane", "typhoon", "storm", "thunderstorm",
            "flood", "flooding", "heatwave", "heat wave", "cold wave",
            "temperature", "temperatures", "meteorological", "imd",
            "precipitation", "drought", "snowfall", "snowstorm",
            "blizzard", "tornado", "wildfire", "cloudburst", "lightning",
            "fog", "dense fog", "hailstorm", "hail", "landslide",
            "waterlogging", "extreme weather", "el nino", "la nina",
            "air quality", "heat index", "cold conditions", "climate",
        )
        non_weather_terms = (
            "cricket", "football", "soccer", "tennis", "match",
            "tournament", "asian games", "sports", "movie", "film",
            "actor", "actress", "entertainment", "gaming", "video game",
            "politics", "political", "election", "minister", "parliament",
            "business", "stock market", "stocks", "shares", "finance",
            "technology", "smartphone", "iphone", "android",
            "live streaming", "scorecard", "wicket", "innings",
            "batting", "bowling", "goal", "medal",
            "sugar", "commodity", "commodities", "supply risk",
            "supply risks", "supply chain", "price rises", "prices rise",
            "price falls", "prices fall", "price surge", "trading",
            "versus", "vs", "t20i", "t20", "odi", "test match", "clash",
            "scorecard", "team news", "highlights", "repo rate",
            "gdp growth", "growth forecast", "central bank", "rbi",
            "profit", "profits", "revenue", "earnings", "consumer confidence",
            "sales", "retail", "quarterly results", "company forecast",
            "supercomputing", "supercomputer", "artificial intelligence",
            "scientific research", "hill station", "hill stations",
            "travel", "tourism", "getaway", "destinations", "places to visit",
        )
        weather_text = title
        has_weather_signal = any(
            re.search(
                rf"(?<!\w){re.escape(term)}(?!\w)",
                weather_text,
            )
            for term in weather_terms
        )
        # A weather article description can mention officials, markets, or
        # other background context. Reject unrelated items based on the
        # headline instead of any incidental description word.
        has_non_weather_signal = any(
            re.search(
                rf"(?<!\w){re.escape(term)}(?!\w)",
                title,
            )
            for term in non_weather_terms
        )
        if not has_weather_signal or has_non_weather_signal:
            return False

    if category == "weather":
        return True

    if category == "stocks":
        # A general business article may mention a company's stock or
        # shareholders without being stock-market news. Require a clear
        # market/instrument signal in the headline or summary.
        stock_market_signals = (
            "stock market", "stock-market", "stock price", "share price",
            "share prices", "shares rise", "shares fall", "shares gain",
            "shares lose", "sensex", "nifty", "nse", "bse", "equity market",
            "equity markets", "ipo", "initial public offering", "listed shares",
            "listed company", "listed companies", "market benchmark",
            "market indices", "stock index", "stock indices", "stocks surge",
            "stocks slide", "stocks gain", "stocks fall", "stocks rise",
            "stocks decline", "stock rally", "stock selloff", "share trading",
            "shareholder", "shareholders",
        )
        if not any(signal in f"{text} {encoded}" for signal in stock_market_signals):
            return False

    if category == "stocks":
        return True

    return any(
        keyword.lower() in text
        for keyword in keywords
    )


# ============================================================
# PARTIAL SOURCE-BALANCED SELECTION
# ============================================================
def select_partial_balanced(
    groups: Dict[str, List[dict]],
    limit: int = 20,
    max_per_source: int = 6,
    require_4_to_6_sources: bool = False,
) -> List[dict]:
    """Return a unique selection from 4–6 publishers, capped at six each."""
    from itertools import combinations

    ranked = sorted(
        groups.items(),
        key=lambda pair: (len(pair[1]), pair[0]),
        reverse=True,
    )
    eligible = [(publisher, articles) for publisher, articles in ranked if articles][:16]

    def select_combo(combo, require_full=True):
        selected = []
        seen_urls = set()
        seen_titles = set()
        per_source = {publisher: 0 for publisher in combo}
        positions = {publisher: 0 for publisher in combo}

        def add_next(publisher):
            articles = groups[publisher]
            while positions[publisher] < len(articles):
                article = articles[positions[publisher]]
                positions[publisher] += 1
                url = str(article.get("url", "") or "").strip().lower()
                url = url.split("#", 1)[0].rstrip("/")
                title = normalize_text(article.get("title", ""))
                if not url or not title or url in seen_urls or title in seen_titles:
                    continue
                selected.append(article)
                per_source[publisher] += 1
                seen_urls.add(url)
                seen_titles.add(title)
                return True
            return False

        # Spread stories evenly while allowing a smaller fourth/fifth/sixth
        # publisher to complete the 20 when one major publisher is sparse.
        while len(selected) < limit:
            added = False
            for publisher in combo:
                if len(selected) >= limit:
                    break
                if per_source[publisher] >= max_per_source:
                    continue
                if add_next(publisher):
                    added = True
            if not added:
                return None if require_full else selected

        return selected

    # A publisher may contribute fewer than four stories; the user rule is
    # about having 4–6 distinct publishers, not an arbitrary minimum quota.
    for source_count in (4, 5, 6):
        if len(eligible) < source_count:
            continue
        combos = sorted(
            combinations([publisher for publisher, _ in eligible], source_count),
            key=lambda combo: sum(min(max_per_source, len(groups[publisher])) for publisher in combo),
            reverse=True,
        )
        for combo in combos:
            selected = select_combo(combo)
            if selected is not None:
                return selected

    if require_4_to_6_sources:
        # If a full 20-story set is not currently available, retain the
        # largest partial set from exactly four to six publishers. Never let
        # one prolific feed or a long tail of one-off publishers dominate.
        best_partial = []
        best_source_count = 0
        for source_count in (4, 5, 6):
            if len(eligible) < source_count:
                continue
            combos = sorted(
                combinations([publisher for publisher, _ in eligible], source_count),
                key=lambda combo: sum(min(max_per_source, len(groups[publisher])) for publisher in combo),
                reverse=True,
            )
            for combo in combos:
                partial = select_combo(combo, require_full=False)
                if len(partial) > len(best_partial):
                    best_partial = partial
                    best_source_count = source_count
                if len(partial) == limit:
                    return partial
        if best_partial and 4 <= best_source_count <= 6:
            return best_partial
        return []

    # No complete 4–6-publisher allocation survived deduplication. Return
    # the largest unique selection and still never exceed six from a source.
    selected = []
    seen_urls = set()
    seen_titles = set()
    per_source = {publisher: 0 for publisher, _ in ranked}
    fallback_positions = {publisher: 0 for publisher, _ in ranked}
    while len(selected) < limit:
        added = False
        for publisher, articles in ranked:
            if len(selected) >= limit:
                break
            if per_source[publisher] >= max_per_source:
                continue
            while fallback_positions[publisher] < len(articles):
                article = articles[fallback_positions[publisher]]
                fallback_positions[publisher] += 1
                url = str(article.get("url", "") or "").strip().lower()
                url = url.split("#", 1)[0].rstrip("/")
                title = normalize_text(article.get("title", ""))
                if not url or not title or url in seen_urls or title in seen_titles:
                    continue
                selected.append(article)
                per_source[publisher] += 1
                seen_urls.add(url)
                seen_titles.add(title)
                added = True
                break
        if not added:
            break
    return selected


# ============================================================
# GET NEWS
# ============================================================


def get_news(
    category: str = "latest",
    force_refresh: bool = False,
) -> List[dict]:

    _debug_request_start = time.perf_counter()

    print(
        f"\nTIMING START: get_news({category})"
    )

    # ========================================================
    # CATEGORY ALIASES
    # ========================================================

    aliases = {
        "andhrapradesh": "andhra pradesh",
        "andhra-pradesh": "andhra pradesh",
        "tamilnadu": "tamilnadu",
        "tamil-nadu": "tamilnadu",
    }

    requested_category = str(
        category or "latest"
    ).strip().lower()

    category = aliases.get(
        requested_category,
        requested_category,
    )

    # Apply one India-local calendar window to every category: today and the
    # two preceding dates. This keeps results current without allowing a
    # category to fill its 20-card target with older stories.
    india_timezone = timezone(timedelta(hours=5, minutes=30))
    today_india = datetime.now(timezone.utc).astimezone(india_timezone).date()
    earliest_india_date = today_india - timedelta(days=2)
    freshness_hours = 96  # extraction guard; category_is_recent is definitive

    def category_is_recent(value):
        parsed = value if isinstance(value, datetime) else parse_date(value)
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        published_india_date = parsed.astimezone(india_timezone).date()
        return earliest_india_date <= published_india_date <= today_india

    # Use the compact, publisher-diverse set of Andhra feeds instead of the
    # older broad national list. Both category aliases still share filtering.
    feed_category = "andhrapradesh" if category == "andhra pradesh" else category
    feeds = CATEGORY_FEEDS.get(
        feed_category,
        CATEGORY_FEEDS.get(
            "latest",
            [],
        ),
    )

    if not feeds:
        feeds = CATEGORY_FEEDS.get(
            "latest",
            [],
        )

    if category == "weather":
        # Keep Weather refreshes fast and current. Broad Latest feeds made
        # each category change wait on many unrelated publishers and still
        # produced few valid Weather headlines after filtering.
        feeds = [
            "https://odishatv.in/weather/feed",
            "https://news.google.com/rss/search?q=India+weather+OR+rainfall+OR+monsoon+OR+cyclone+when%3A2d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=India+weather+forecast+OR+IMD+alert+OR+rain+warning+when%3A2d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=Telangana+OR+Andhra+Pradesh+OR+Karnataka+OR+Tamil+Nadu+OR+Kerala+OR+Maharashtra+OR+Delhi+local+news+when%3A2d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Ahindustantimes.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Andtv.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Athehindu.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Aindiatoday.in+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Aeconomictimes.indiatimes.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Aindianexpress.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=site%3Aapnews.com+weather+OR+flood+OR+storm+OR+heatwave+when%3A5d&hl=en&gl=US&ceid=US:en",
        ]

    # The Jobs page was waiting on more than twenty independent RSS
    # publishers before it could render. Prioritize a small, mixed set of
    # direct vacancy and government/private-sector feeds for a fast first
    # response; feed caching keeps repeat visits quick as well.
    if category == "jobs":
        feeds = feeds[:6]

    print("\n" + "=" * 70)
    print("GET NEWS:", requested_category)
    print("CANONICAL CATEGORY:", category)
    print("FEEDS:", len(feeds))
    print("=" * 70)

    # ========================================================
    # REGIONAL FEED TRUST
    # ========================================================

    trusted_markers = {

        "telangana": [
            "thehindu.com/news/national/telangana",
            "telanganatoday.com/feed",
            "indianexpress.com/section/cities/hyderabad",
            "tv9telugu.com/telangana",
            "thehansindia.com/rss/telangana",
        ],

        "andhra pradesh": [
            "thehindu.com/news/national/andhra-pradesh",
            "tv9telugu.com/andhra-pradesh",
            "thehansindia.com/rss/andhra-pradesh",
            "newindianexpress.com/states/andhra-pradesh",
        ],

        "karnataka": [
            "indianexpress.com/section/cities/bangalore",
            "thehansindia.com/rss/karnataka",
            "hindustantimes.com/feeds/rss/cities/bengaluru",
        ],

        "tamil nadu": [
            "thehindu.com/news/national/tamil-nadu",
            "thehindu.com/news/cities/chennai",
            "timesofindia.indiatimes.com/rssfeeds/2950623",
        ],

        "kerala": [
            "thehindu.com/news/national/kerala",
            "thehindu.com/news/cities/kochi",
            "thehindu.com/news/cities/thiruvananthapuram",
            "newindianexpress.com/states/kerala",
            "mathrubhumi.com/rss",
            "oneindia.com/rss",
            "onmanorama.com/kerala",
        ],

        "maharashtra": [
            "indianexpress.com/section/cities/mumbai",
            "indianexpress.com/section/cities/pune",
            "indianexpress.com/section/cities/nagpur",
            "indianexpress.com/section/cities/nashik",
            "hindustantimes.com/feeds/rss/cities/mumbai",
            "timesofindia.indiatimes.com/rssfeeds/-2128838597",
            "maharashtratimes.com/rss",
        ],

        "stocks": [
            "livemint.com/rss/markets",
            "indianexpress.com/section/smart-stocks/feed",
            "business-standard.com/rss/markets-106.rss",
            "moneycontrol.com/rss/marketreports.xml",
            "cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
        ],

        # Dedicated Weather RSS feeds are category-specific. Their summaries
        # can mention other topics without changing the article's main focus.
        "weather": [
            "indianexpress.com/section/weather/feed",
            "odishatv.in/weather/feed",
            "timesofindia.indiatimes.com/rssfeeds/2647163",
        ],
    }

    # Karnataka feeds are broad publisher feeds.
    # Require Karnataka relevance from article content/title
    # instead of trusting the entire feed.
    if category == "karnataka":

        trusted_markers[
            "karnataka"
        ] = []

    def trusted_feed(
        feed_url: str,
    ) -> bool:

        feed_url = str(
            feed_url or ""
        ).lower().replace(
            "\\",
            "",
        )

        trust_aliases = {
            "andhrapradesh": "andhra pradesh",
            "tamilnadu": "tamil nadu",
        }
        markers = trusted_markers.get(
            trust_aliases.get(category, category),
            [],
        )

        return any(
            str(marker).lower()
            in feed_url
            for marker in markers
        )

    def category_article_relevant(
        item: dict,
        feed_url: str,
    ) -> bool:
        # Dedicated Weather feeds can contain unrelated entries, so they
        # must pass the same headline check as every other feed.
        if category == "weather":
            return matches_category(item, category, feed_url)
        if trusted_feed(feed_url):
            return True
        return matches_category(item, category, feed_url)

    # ========================================================
    # SOURCE NORMALIZATION
    # ========================================================

    def source_name(
        value,
    ) -> str:

        value = str(
            value or ""
        ).replace(
            "\\",
            "",
        ).strip()

        lower = value.lower()

        # All The Hindu Kerala editions are ONE source.
        if lower.startswith(
            "the hindu -"
        ):
            return "The Hindu"

        if (
            "indianexpress.com" in lower
            and "newindianexpress.com"
            not in lower
        ):
            return "The Indian Express"

        if "newindianexpress.com" in lower:
            return "The New Indian Express"

        if "hindustantimes.com" in lower:
            return "Hindustan Times"

        if "timesofindia.indiatimes.com" in lower:
            return "The Times of India"

        if "thehindu.com" in lower:
            return "The Hindu"

        if "ndtv.com" in lower:
            return "NDTV"

        if "feedburner.com" in lower:
            if "ndtv" in lower:
                return "NDTV"

        if "firstpost.com" in lower:
            return "Firstpost"

        if "tv9telugu.com" in lower:
            return "TV9 Telugu"

        if "tv9marathi.com" in lower:
            return "TV9 Marathi"

        if "thehansindia.com" in lower:
            return "The Hans India"

        if "deccanchronicle.com" in lower:
            return "Deccan Chronicle"

        if "mathrubhumi.com" in lower:
            return "Mathrubhumi"

        if "oneindia.com" in lower:
            return "OneIndia"

        if "onmanorama.com" in lower:
            return "Onmanorama"

        if "maharashtratimes.com" in lower:
            return "Maharashtra Times"

        if "businessline" in lower:
            return "The Hindu BusinessLine"

        if "techcrunch.com" in lower:
            return "TechCrunch"

        if "arstechnica.com" in lower:
            return "Ars Technica"

        if "wired.com" in lower:
            return "WIRED"

        if "gadgets360.com" in lower:
            return "Gadgets 360"

        if "sciencedaily.com" in lower:
            return "ScienceDaily"

        if "phys.org" in lower:
            return "Phys.org"

        if "bbc" in lower:
            return "BBC"

        if value:
            return value

        return "Unknown"

    # ========================================================
    # FETCH RSS
    # ========================================================

    raw_by_feed = {}

    with ThreadPoolExecutor(
        max_workers=min(
            max(len(feeds), 1),
            12,
        )
    ) as executor:

        future_map = {}

        for index, feed in enumerate(
            feeds
        ):

            future_map[
                executor.submit(
                    fetch_rss,
                    feed,
                    force_refresh=force_refresh,
                )
            ] = (
                index,
                feed,
            )

        for future in as_completed(
            future_map
        ):

            index, feed = future_map[
                future
            ]

            try:

                items = future.result()

                raw_by_feed[
                    index
                ] = (
                    feed,
                    items,
                )

                print(
                    "RSS:",
                    len(items),
                    "->",
                    feed,
                )

            except Exception as error:

                raw_by_feed[
                    index
                ] = (
                    feed,
                    [],
                )

                print(
                    "RSS ERROR:",
                    feed,
                    error,
                )

    # Tamil Nadu direct HTML source.
    if category in {"tamil nadu", "tamilnadu"}:

        try:

            items = (
                fetch_new_indian_express_tamil_nadu()
            )

            raw_by_feed[
                len(feeds)
            ] = (
                "newindianexpress-tamil-nadu",
                items,
            )

        except Exception as error:

            print(
                "NIE ERROR:",
                error,
            )

    direct_state_slugs = {
        "andhra pradesh": "andhra-pradesh",
        "kerala": "kerala",
        "maharashtra": "maharashtra",
    }
    if category in direct_state_slugs:
        try:
            state_slug = direct_state_slugs[category]
            items = fetch_new_indian_express_tamil_nadu(
                state_slug,
                category,
            )
            raw_by_feed[len(feeds)] = (
                f"newindianexpress-{state_slug}",
                items,
            )
        except Exception as error:
            print("NIE REGIONAL ERROR:", error)

    # ========================================================
    # BUILD FEED CANDIDATES
    # ========================================================

    candidates_by_feed = {}

    global_urls = set()

    source_title_seen = set()

    # ========================================================
    # CATEGORY-LEVEL TITLE DEDUPLICATION
    # Prevent the same/near-identical headline from different
    # publishers from entering the same category.
    # ========================================================

    category_title_seen = set()

    def category_title_tokens(title):
        stop_words = {
            "a", "an", "the", "and", "or", "of", "to", "in",
            "on", "for", "with", "by", "from", "at", "is",
            "are", "was", "were", "has", "have", "new"
        }

        normalized = re.sub(
            r"[^a-z0-9\s]",
            " ",
            str(title or "").lower()
        )

        return {
            word
            for word in re.sub(r"\s+", " ", normalized).strip().split()
            if len(word) >= 3 and word not in stop_words
        }

    def category_similar_title(title_a, title_b):
        tokens_a = category_title_tokens(title_a)
        tokens_b = category_title_tokens(title_b)

        if not tokens_a or not tokens_b:
            return False

        smaller = min(len(tokens_a), len(tokens_b))

        if smaller == 0:
            return False

        return (
            len(tokens_a & tokens_b) / smaller
        ) >= 0.80

    # ========================================================

    for feed_index, (
        feed_url,
        items,
    ) in raw_by_feed.items():

        candidates_by_feed[
            feed_index
        ] = []

        for item in items:

            if not isinstance(
                item,
                dict,
            ):
                continue

            item = dict(item)

            actual_feed = str(
                item.get(
                    "feed_url",
                    "",
                )
                or feed_url
            )

            item[
                "_balance_feed"
            ] = actual_feed

            title = clean_title(
                item.get(
                    "title",
                    "",
                ),
                item.get(
                    "source",
                    "",
                ),
            )

            url = str(
                item.get(
                    "url",
                    "",
                )
                or ""
            ).strip()

            if not title or not url:
                continue

            # TV9 Telugu stories are translated after source balancing.
            # Other feeds must already have English headlines and summaries
            # before they can count toward a category result.
            language_source = str(
                item.get("source", "") or actual_feed
            ).lower()
            is_tv9_story = (
                "tv9telugu.com" in language_source
                or "tv9 telugu" in language_source
                or "tv9telugu.com" in url.lower()
            )
            if not is_tv9_story and not is_english_text(
                title,
                item.get("description", ""),
            ):
                continue

            # ------------------------------------------------
            # GLOBAL NON-NEGOTIABLE FRESHNESS FILTER
            # ------------------------------------------------

            published_value = item.get(
                "published_at",
                ""
            )

            published_date = parse_date(
                published_value
            )

            # ------------------------------------------------
            # Jobs uses the same India-local three-calendar-day window as
            # every other category.
            # ------------------------------------------------

            if category == "jobs":

                jobs_current_india_date = today_india
                jobs_freshness_start_date = earliest_india_date

                if published_date is None:
                    print(
                        "JOBS FRESHNESS REJECT: INVALID DATE:",
                        title[:80]
                    )
                    continue

                try:

                    if published_date.tzinfo is not None:
                        published_india_date = (
                            published_date
                            .astimezone(
                                timezone(
                                    timedelta(
                                        hours=5,
                                        minutes=30
                                    )
                                )
                            )
                            .date()
                        )
                    else:
                        published_india_date = (
                            published_date.date()
                        )

                except Exception:
                    print(
                        "JOBS FRESHNESS REJECT: DATE ERROR:",
                        title[:80]
                    )
                    continue

                if (
                    published_india_date
                    < jobs_freshness_start_date
                    or
                    published_india_date
                    > jobs_current_india_date
                ):
                    print(
                        "JOBS FRESHNESS REJECT:",
                        published_india_date,
                        "|",
                        title[:100]
                    )
                    continue

                # ------------------------------------------------
                # JOBS ACTIONABILITY FILTER
                #
                # Keep genuine opportunities that an Indian job
                # seeker can act on. Startup hiring is allowed
                # when the headline represents an actual hiring
                # opportunity.
                #
                # General employment/workforce/hiring analysis
                # without an actionable opportunity is excluded.
                # ------------------------------------------------

                jobs_opportunity_terms = {
                    "recruitment",
                    "recruitment notification",
                    "recruitment drive",
                    "job opening",
                    "job openings",
                    "job vacancy",
                    "job vacancies",
                    "invites applications for posts",
                    "invites applications for positions",
                    "applications invited for posts",
                    "applications invited for positions",
                    "junior research fellow",
                    "research fellow position",
                    "research fellow positions",
                    "vacancy",
                    "vacancies",
                    "apply online",
                    "apply now",
                    "applications open",
                    "application open",
                    "last date to apply",
                    "application deadline",
                    "internship",
                    "internships",
                    "apprentice",
                    "apprenticeship",
                    "walk-in interview",
                    "walk in interview",
                    "freshers job",
                    "freshers jobs",
                    "fresher job",
                    "fresher jobs",
                    "freshers recruitment",
                    "fresher recruitment",
                    "graduate recruitment",
                    "graduate hiring",
                    "graduate jobs",
                    "campus recruitment",
                    "campus hiring",
                    "government job",
                    "government jobs",
                    "govt job",
                    "govt jobs",
                    "government recruitment",
                    "govt recruitment",
                    "railway recruitment",
                    "railway jobs",
                    "bank recruitment",
                    "banking jobs",
                    "police recruitment",
                    "police jobs",
                    "defence recruitment",
                    "defense recruitment",
                    "defence jobs",
                    "defense jobs",
                    "teacher recruitment",
                    "teaching jobs",
                    "research fellowship",
                    "fellowship",
                    "research opportunity",
                    "research opportunities",
                    "project position",
                    "project associate",
                    "young professional recruitment",
                }

                jobs_analysis_terms = {
                    "hiring assessments",
                    "hiring assessment",
                    "employment rate",
                    "unemployment rate",
                    "industrial jobs cross",
                    "jobs cross",
                    "job market",
                    "job market trends",
                    "employment trends",
                    "workforce trends",
                    "workforce outlook",
                    "employment outlook",
                    "salary trends",
                    "salary trend",
                    "campus salaries",
                    "campus salary",
                    "median salaries",
                    "median salary",
                    "skills and job readiness",
                    "job readiness",
                    "future of work",
                    "future of jobs",
                    "skills gap",
                    "skills shortage",
                    "talent powering",
                    "talent powering india",
                    "cheating in hiring assessments",
                    "curious case of hiring",
                    "paychecks hit pause",
                    "campus hiring towards",
                    "campus hiring towards skills",
                    "skilled talent powering",
                }

                jobs_text = (
                    f"{title} "
                    f"{str(item.get('description', '') or '').lower()}"
                )

                has_job_opportunity = any(
                    term in jobs_text
                    for term in jobs_opportunity_terms
                ) or bool(JOB_POSTING_PATTERN.search(jobs_text)) or is_active_job_listing(item)

                has_job_analysis = any(
                    term in title.lower()
                    for term in jobs_analysis_terms
                )

                if (
                    has_job_analysis
                    and not has_job_opportunity
                ):
                    print(
                        "JOBS ANALYSIS REJECT:",
                        title[:120]
                    )
                    continue

                if not has_job_opportunity:
                    print(
                        "JOBS NON-ACTIONABLE REJECT:",
                        title[:120]
                    )
                    continue

            # ------------------------------------------------
            # Category filtering
            # ------------------------------------------------

            if category in CATEGORY_KEYWORDS or category == "weather":

                if not category_article_relevant(item, actual_feed):
                    continue

            url_key = (
                url.split(
                    "#",
                    1,
                )[0]
                .rstrip("/")
                .lower()
            )

            if url_key in global_urls:
                continue

            publisher_hint = source_name(
                item.get(
                    "source",
                    "",
                )
                or actual_feed
            )

            title_key = normalize_text(
                title
            )

            source_title_key = (
                publisher_hint,
                title_key,
            )

            if source_title_key in source_title_seen:
                continue

            # Reject exact or near-duplicate headlines inside
            # the same category, even when URLs/publishers differ.
            if title_key:
                if title_key in category_title_seen:
                    continue

                if any(
                    category_similar_title(
                        title_key,
                        existing_title,
                    )
                    for existing_title in category_title_seen
                ):
                    continue

            global_urls.add(
                url_key
            )

            source_title_seen.add(
                source_title_key
            )

            if title_key:
                category_title_seen.add(title_key)

            # ------------------------------------------------
            # JOBS PRE-FILTER
            #
            # Remove clearly non-actionable employment analysis
            # before expensive article processing.
            #
            # Actual opportunities remain eligible:
            # recruitment, vacancies, applications, internships,
            # apprenticeships, fellowships, government recruitment,
            # campus hiring and genuine company hiring.
            # ------------------------------------------------

            if category == "jobs":

                jobs_title = str(
                    item.get("title", "") or ""
                ).lower()

                jobs_description = str(
                    item.get("description", "") or ""
                ).lower()

                jobs_text = (
                    jobs_title
                    + " "
                    + jobs_description
                )

                jobs_actionable_terms = {
                    "recruitment",
                    "recruitment notification",
                    "recruitment drive",
                    "job opening",
                    "job openings",
                    "job vacancy",
                    "job vacancies",
                    "invites applications for posts",
                    "invites applications for positions",
                    "applications invited for posts",
                    "applications invited for positions",
                    "junior research fellow",
                    "research fellow position",
                    "research fellow positions",
                    "vacancy",
                    "vacancies",
                    "apply online",
                    "apply now",
                    "applications open",
                    "application open",
                    "last date to apply",
                    "application deadline",
                    "internship",
                    "internships",
                    "apprentice",
                    "apprenticeship",
                    "walk-in interview",
                    "walk in interview",
                    "freshers job",
                    "freshers jobs",
                    "fresher job",
                    "fresher jobs",
                    "freshers recruitment",
                    "fresher recruitment",
                    "graduate recruitment",
                    "graduate hiring",
                    "graduate jobs",
                    "campus recruitment",
                    "campus hiring",
                    "government job",
                    "government jobs",
                    "govt job",
                    "govt jobs",
                    "government recruitment",
                    "govt recruitment",
                    "railway recruitment",
                    "railway jobs",
                    "bank recruitment",
                    "banking jobs",
                    "police recruitment",
                    "police jobs",
                    "defence recruitment",
                    "defense recruitment",
                    "defence jobs",
                    "defense jobs",
                    "teacher recruitment",
                    "teaching jobs",
                    "research fellowship",
                    "fellowship",
                    "research opportunity",
                    "research opportunities",
                    "project position",
                    "project associate",
                    "young professional recruitment",
                }

                jobs_analysis_terms = {
                    "hiring assessments",
                    "hiring assessment",
                    "employment rate",
                    "unemployment rate",
                    "industrial jobs cross",
                    "jobs cross",
                    "job market",
                    "job market trends",
                    "employment trends",
                    "workforce trends",
                    "workforce outlook",
                    "employment outlook",
                    "salary trends",
                    "salary trend",
                    "campus salaries",
                    "campus salary",
                    "median salaries",
                    "median salary",
                    "skills and job readiness",
                    "job readiness",
                    "future of work",
                    "future of jobs",
                    "skills gap",
                    "skills shortage",
                    "talent powering",
                    "talent powering india",
                    "cheating in hiring assessments",
                    "curious case of hiring",
                    "paychecks hit pause",
                    "campus hiring towards",
                    "campus hiring towards skills",
                    "skilled talent powering",
                }

                has_actionable = any(
                    term in jobs_text
                    for term in jobs_actionable_terms
                ) or bool(JOB_POSTING_PATTERN.search(jobs_text)) or is_active_job_listing(item)

                has_analysis = any(
                    term in jobs_title
                    for term in jobs_analysis_terms
                )

                if has_analysis and not has_actionable:
                    print(
                        "JOBS PRE-FILTER REJECT:",
                        item.get("title", "")[:120]
                    )
                    continue

                if not has_actionable:
                    print(
                        "JOBS PRE-FILTER REJECT:",
                        item.get("title", "")[:120]
                    )
                    continue

            candidates_by_feed[
                feed_index
            ].append(
                item
            )

    print(
        f"TIMING AFTER CANDIDATE BUILD: "
        f"{time.perf_counter() - _debug_request_start:.2f}s"
    )

    print(
        f"TIMING RSS/CANDIDATE STAGE: "
        f"{time.perf_counter() - _debug_request_start:.2f}s"
    )

    # ========================================================
    # SORT
    # ========================================================

    def item_date(
        item,
    ):

        parsed = parse_date(
            item.get(
                "published_at",
                "",
            )
        )

        if parsed is None:

            return datetime.min.replace(
                tzinfo=timezone.utc
            )

        return parsed

    for feed_index in candidates_by_feed:

        candidates_by_feed[
            feed_index
        ].sort(
            key=item_date,
            reverse=True,
        )

    # Weather cards can be served directly from the already validated RSS
    # headlines and summaries. Crawling every publisher article page here
    # added a long wait without improving these feed-provided excerpts.
    if category == "weather":
        weather_groups = {}
        weather_urls = set()
        weather_titles = set()

        for feed_index in sorted(candidates_by_feed):
            for item in candidates_by_feed[feed_index]:
                publisher = str(item.get("source", "") or "").strip()
                feed_for_source = str(
                    item.get("feed_url", "")
                    or item.get("_balance_feed", "")
                    or ""
                )
                title = clean_title(
                    item.get("title", ""),
                    publisher or source_name(feed_for_source),
                )
                url = str(item.get("url", "") or "").strip()
                published_at = item.get("published_at", "")
                url_key = url.split("#", 1)[0].rstrip("/").lower()
                title_key = normalize_text(title)

                if (
                    not title
                    or not url_key
                    or not title_key
                    or not category_is_recent(published_at)
                    or url_key in weather_urls
                    or title_key in weather_titles
                ):
                    continue

                # Candidate building can trust a dedicated RSS URL when it
                # fetches the feed, but some publisher feeds mix weather with
                # general headlines. Apply the same headline relevance gate
                # in this fast path before it can bypass the normal pipeline.
                weather_item = dict(item)
                weather_item["title"] = title
                if not matches_category(weather_item, "weather", feed_for_source):
                    continue

                description_parts = [
                    clean_text(item.get("description", "")),
                    clean_text(
                        item.get("encoded", "")
                        or item.get("content_encoded", "")
                        or item.get("content", "")
                    ),
                ]
                summary_title = title
                summary_title_key = normalize_text(summary_title)
                cleaned_parts = []
                for part in description_parts:
                    part_key = normalize_text(part)
                    if summary_title_key and part_key.startswith(summary_title_key):
                        part = re.sub(
                            rf"^\s*{re.escape(summary_title)}[\s:;,.|–—-]*",
                            "",
                            part,
                            flags=re.IGNORECASE,
                        ).strip()
                    if len(part.split()) >= 12:
                        cleaned_parts.append(part)

                description = build_description(title, cleaned_parts)
                if len(description) < 120 or len(description.split()) < 12:
                    description = next(
                        (
                            part for part in cleaned_parts
                            if len(part) >= 120 and len(part.split()) >= 12
                        ),
                        "",
                    )
                if len(description) < 120 or len(description.split()) < 12:
                    continue
                # Keep the visible summary compact, continuous, and made of
                # complete source sentences. The card CSS wraps this into a
                # readable 2–4 line block without clipping it.
                description = compact_card_description(title, description)
                if len(description) < 120 or len(description.split()) < 12:
                    continue

                article = {
                    "title": title,
                    "description": description,
                    "url": url,
                    "source": source_name(
                        item.get("source", "")
                        or item.get("feed_url", "")
                    ),
                    "published_at": published_at,
                }
                weather_groups.setdefault(article["source"], []).append(article)
                weather_urls.add(url_key)
                weather_titles.add(title_key)

        weather_articles = select_partial_balanced(
            weather_groups,
            limit=20,
            require_4_to_6_sources=True,
        )

        print(
            "WEATHER RSS RESULTS:",
            len(weather_articles),
            "fresh relevant articles",
        )
        if len(weather_articles) == 20:
            return weather_articles

        # Google News RSS links usually carry a headline plus publisher name,
        # which is too short to display as an article description. Resolve a
        # balanced set of fresh weather stories to their publisher pages and
        # use the source text there. This keeps the Weather tab populated
        # without filling it with clipped or invented descriptions.
        weather_resolution_groups = {}
        weather_candidate_urls = set()
        weather_candidate_titles = set()
        weather_title_terms = (
            "weather", "forecast", "rain", "rainfall", "shower",
            "thunderstorm", "lightning", "cyclone", "storm", "flood",
            "heatwave", "heat wave", "cold wave", "temperature",
            "monsoon", "el nino", "la nina", "drought", "cloudburst",
            "imd", "meteorological",
        )
        weather_irrelevant_terms = (
            "malaria", "dengue", "mosquito", "chatbot", "flight",
            "plane", "aircraft", "indigo", "water conservation after",
            "food prices", "rural demand", "stock market", "share price",
            "market flooding", "subsidised cars", "credit growth",
            "rural lending", "emergency landing", "wholesale",
            "banana prices", "export disruption", "debt surge", "central aid",
            "government committed to supporting farmers",
        )
        india_weather_terms = (
            "india", "indian", "delhi", "ncr", "mumbai", "maharashtra",
            "pune", "nagpur", "goa", "odisha", "bhubaneswar", "odisha",
            "west bengal", "kolkata", "sikkim", "assam", "guwahati",
            "meghalaya", "arunachal", "manipur", "mizoram", "tripura",
            "nagaland", "uttarakhand", "himachal", "shimla", "jammu",
            "kashmir", "uttar pradesh", " uttar pradesh", "lucknow", "bihar",
            "patna", "jharkhand", "chhattisgarh", "madhya pradesh", "rajasthan",
            "gujarat", "surat", "ahmedabad", "karnataka", "bengaluru",
            "bangalore", "kerala", "tamil nadu", "chennai", "telangana",
            "hyderabad", "andhra pradesh", "vijayawada", "visakhapatnam",
            "punjab", "haryana", "chandigarh", "ladakh",
        )

        for feed_index in sorted(candidates_by_feed):
            for item in candidates_by_feed[feed_index]:
                title = clean_title(
                    item.get("title", ""),
                    item.get("source", ""),
                )
                url = str(item.get("url", "") or "").strip()
                source = source_name(
                    item.get("source", "")
                    or item.get("feed_url", "")
                )
                title_key = normalize_text(title)
                url_key = url.split("#", 1)[0].rstrip("/").lower()
                title_lower = title.lower()
                if (
                    not url
                    or "news.google.com/rss/articles/" not in url.lower()
                    or not title_key
                    or not category_is_recent(item.get("published_at", ""))
                    or not matches_category(item, "weather")
                    or not any(term in title_lower for term in weather_title_terms)
                    or any(term in title_lower for term in weather_irrelevant_terms)
                    or url_key in weather_candidate_urls
                    or title_key in weather_candidate_titles
                ):
                    continue
                item_copy = dict(item)
                item_copy["title"] = title
                item_copy["source"] = source
                weather_resolution_groups.setdefault(source, []).append(item_copy)
                weather_candidate_urls.add(url_key)
                weather_candidate_titles.add(title_key)

        resolution_candidates = select_partial_balanced(
            weather_resolution_groups,
            limit=48,
            max_per_source=8,
            require_4_to_6_sources=True,
        )
        if resolution_candidates:
            google_urls = [item.get("url", "") for item in resolution_candidates]
            publisher_urls = resolve_google_news_urls(google_urls)

            def build_weather_page_article(pair):
                item, publisher_url = pair
                if not publisher_url:
                    return None
                title = item.get("title", "")
                rss_description = clean_text(item.get("description", ""))
                page_description = get_publisher_description(
                    publisher_url,
                    title,
                    rss_description,
                    timeout=5,
                )
                description = build_description(
                    title,
                    [page_description, rss_description],
                )
                description = compact_card_description(title, description)
                if (
                    len(description) < 120
                    or len(description.split()) < 12
                    or not description_matches_title(title, description)
                    or not any(
                        term in f"{title} {description}".lower()
                        for term in india_weather_terms
                    )
                ):
                    return None
                return {
                    "title": title,
                    "description": description,
                    "url": publisher_url,
                    "source": item.get("source", ""),
                    "published_at": item.get("published_at", ""),
                }

            with ThreadPoolExecutor(max_workers=8) as executor:
                enriched_weather = list(
                    executor.map(
                        build_weather_page_article,
                        zip(resolution_candidates, publisher_urls),
                    )
                )

            enriched_groups = {}
            for article in enriched_weather:
                if not article:
                    continue
                enriched_groups.setdefault(article["source"], []).append(article)
            # Retain complete descriptions already provided by dedicated
            # publisher Weather RSS feeds. A single current local forecast
            # can complete the final 20-card set without stretching a feed's
            # Google News query for older or unrelated results.
            for publisher, articles in weather_groups.items():
                enriched_groups.setdefault(publisher, []).extend(articles)
            resolved_weather_articles = select_partial_balanced(
                enriched_groups,
                limit=20,
                max_per_source=6,
                require_4_to_6_sources=True,
            )
            print(
                "WEATHER PUBLISHER ARTICLES:",
                len(resolved_weather_articles),
                "from",
                len({article["source"] for article in resolved_weather_articles}),
                "sources",
            )
            if resolved_weather_articles:
                return resolved_weather_articles

    print(
        "\nCANDIDATES:"
    )

    for feed_index in sorted(
        candidates_by_feed
    ):

        print(
            f"  Feed {feed_index + 1}: "
            f"{len(candidates_by_feed[feed_index])}"
        )

    # ========================================================
    # ARTICLE PROCESSING
    # ========================================================

    processed = []

    processed_urls = set()

    # ========================================================
    # CROSS-CATEGORY ARTICLE REGISTRY
    # Prevent the same article URL from being served in
    # multiple different categories during the active cache
    # window. The same category may reuse its own articles.
    # ========================================================

    cross_category_now = datetime.now(timezone.utc)

    # Keep the registry short-lived so news can naturally
    # become available again after the active window.
    with CROSS_CATEGORY_REGISTRY_LOCK:
        expired_urls = [
            registry_url
            for registry_url, registry_data
            in CROSS_CATEGORY_URL_REGISTRY.items()
            if (
                cross_category_now - registry_data["time"]
            ).total_seconds() > CROSS_CATEGORY_REGISTRY_TTL
        ]

        for registry_url in expired_urls:
            CROSS_CATEGORY_URL_REGISTRY.pop(
                registry_url,
                None,
            )

    def process_batch(
        batch,
        rss_fallback_mode=False,
    ):

        if not batch:
            return

        # Process every feed's candidates in this round. The prior
        # 30-item slice was applied after feed groups were concatenated,
        # which could starve later feeds and leave categories short.
        # The executor still bounds concurrent publisher-page requests.
        processing_batch = list(batch)

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            future_map = {
                executor.submit(
                    process_article,
                    item,
                    rss_fallback_mode,
                    # Keep Jobs and Latest lightweight. Latest retries
                    # publisher extraction only when text suggests a cutoff
                    # after a name initial or honorific.
                    category in {"jobs", "latest"},
                    freshness_hours,
                    latest_mode=(category == "latest"),
                ): item
                for item in processing_batch
            }

            for future in as_completed(
                future_map
            ):

                try:

                    article = future.result()

                    if not article:
                        continue

                    # Recheck relevance after processing as a final gate.
                    # This prevents any fallback or special selector from
                    # returning an article that no longer matches its category.
                    candidate_item = future_map[future]
                    if (
                        category in CATEGORY_KEYWORDS
                        or category == "weather"
                    ) and not category_article_relevant(
                        candidate_item,
                        article.get("feed_url", ""),
                    ):
                        print(
                            "FINAL CATEGORY REJECT:",
                            category,
                            article.get("title", "")[:100],
                        )
                        continue

                    url = str(
                        article.get(
                            "url",
                            "",
                        )
                        or ""
                    ).strip().lower()

                    if url in processed_urls:
                        continue

                    # ------------------------------------------------
                    # LATEST/HOT UNIQUENESS CHECK
                    # ------------------------------------------------
                    # Do not return an article that is currently part
                    # of the Latest/Hot feed when serving another
                    # category.
                    # ------------------------------------------------

                    # ------------------------------------------------
                    # LATEST/HOT UNIQUENESS CHECK
                    # ------------------------------------------------
                    # Read the shared Latest/Hot URL set directly.
                    # The registry is maintained by main.py and shared
                    # through the latest_registry module.
                    # ------------------------------------------------

                    with latest_registry.LATEST_HOT_URL_REGISTRY_LOCK:
                        latest_urls = set(
                            latest_registry.LATEST_HOT_URL_REGISTRY
                        )
                        latest_registry_updated = (
                            latest_registry.LATEST_HOT_URL_REGISTRY_UPDATED
                        )

                    latest_registry_age = (
                        time.time() - latest_registry_updated
                    )

                    if category in (
                        "andhrapradesh",
                        "tamilnadu",
                    ):
                        print(
                            "LATEST REGISTRY DEBUG:",
                            "category=", category,
                            "size=", len(latest_urls),
                            "age=", round(latest_registry_age, 3),
                            "url_match=", url in latest_urls,
                        )

                    if (
                        latest_registry_age
                        < latest_registry.LATEST_HOT_URL_REGISTRY_TTL
                        and url in latest_urls
                    ):
                        print(
                            "LATEST-CATEGORY DUPLICATE SKIPPED:",
                            category,
                            article.get("title", "")[:100],
                        )
                        continue

                    # Latest and India intentionally draw from overlapping
                    # publisher feeds. Suppress a story already served to a
                    # different category while allowing the current category
                    # to refresh its own articles.
                    url_key = re.sub(
                        r"([?&])(utm_[^=]+|fbclid|gclid)=[^&#]*",
                        "",
                        url.split("#", 1)[0],
                        flags=re.IGNORECASE,
                    ).rstrip("?&/").lower()
                    title_key = normalize_text(
                        article.get("title", "")
                    )

                    cross_category_duplicate = False
                    with CROSS_CATEGORY_REGISTRY_LOCK:
                        existing = CROSS_CATEGORY_URL_REGISTRY.get(url_key)
                        latest_india_pair = bool(
                            existing
                            and {existing.get("category"), category}
                            == {"latest", "india"}
                        )
                        if latest_india_pair:
                            cross_category_duplicate = True
                        else:
                            for known in CROSS_CATEGORY_URL_REGISTRY.values():
                                if (
                                    {known.get("category"), category}
                                    == {"latest", "india"}
                                    and title_key
                                    and title_key == known.get("title")
                                ):
                                    cross_category_duplicate = True
                                    break

                    if cross_category_duplicate:
                        print(
                            "CROSS-CATEGORY DUPLICATE SKIPPED:",
                            category,
                            article.get("title", "")[:100],
                        )
                        continue

                    # ------------------------------------------------
                    # Duplicate suppression is scoped to this category.
                    # A story may be relevant to more than one section
                    # (for example, a Maharashtra business story), and
                    # a process-global registry must not starve whichever
                    # category is fetched second.
                    processed_urls.add(
                        url
                    )

                    article[
                        "source"
                    ] = source_name(
                        article.get(
                            "source",
                            "",
                        )
                    )

                    processed.append(
                        article
                    )

                except Exception as error:

                    print(
                        "ARTICLE ERROR:",
                        error,
                    )

    print(
        f"TIMING AFTER ARTICLE PROCESSING: "
        f"{time.perf_counter() - _debug_request_start:.2f}s"
    )

    # ========================================================
    # GROUP PROCESSED ARTICLES
    # ========================================================

    def build_groups():

        groups = {}

        for article in processed:

            publisher = source_name(
                article.get(
                    "source",
                    "",
                )
            )

            groups.setdefault(
                publisher,
                [],
            ).append(
                article
            )

        for publisher in groups:

            groups[
                publisher
            ].sort(
                key=lambda article: (
                    parse_date(
                        article.get(
                            "published_at",
                            "",
                        )
                    )
                    or datetime.min.replace(
                        tzinfo=timezone.utc
                    )
                ),
                reverse=True,
            )

        return groups

    def can_make_20():

        groups = build_groups()

        # Count globally unique headlines while measuring each publisher's
        # capacity. Raw per-source counts can claim 20 are available even
        # when the same headline appears in several feeds; the final
        # selector then removes those copies and can stop at 17 or 18.
        unique_groups = {}
        seen_urls = set()
        seen_titles = set()

        for publisher, items in groups.items():
            for article in items:
                url = str(article.get("url", "") or "").strip().lower()
                url = url.split("#", 1)[0].rstrip("/")
                title = normalize_text(article.get("title", ""))
                if not url or not title or url in seen_urls or title in seen_titles:
                    continue
                seen_urls.add(url)
                seen_titles.add(title)
                unique_groups.setdefault(publisher, []).append(article)

        eligible = [
            count
            for count in (
                len(items)
                for items in unique_groups.values()
            )
            if count >= 4
        ]

        # Five sources x four = 20.
        if len(eligible) >= 5:
            return True

        # Four sources need four additional articles
        # beyond the minimum 4 each.
        if len(eligible) == 4:

            capacity = sum(
                min(
                    6,
                    count,
                ) - 4
                for count in eligible
            )

            return capacity >= 4

        return False

    # ========================================================
    # PROCESS IN ROUNDS
    # ========================================================

    # Process only enough candidates to build the required
    # 20-article balanced result. Processing very large RSS
    # candidate batches causes unnecessary publisher-page
    # requests and can make regional categories time out.
    limits = [
        4,
        8,
        12,
        16,
        20,
    ]

    previous = 0

    for limit in limits:

        batch = []

        for feed_index in sorted(
            candidates_by_feed
        ):

            feed_items = candidates_by_feed[
                feed_index
            ]

            batch.extend(
                feed_items[
                    previous:limit
                ]
            )

        if batch:

            print(
                "\nPROCESSING:",
                previous + 1,
                "to",
                limit,
                "PER FEED",
            )

            _batch_start = time.perf_counter()

            process_batch(
                batch,
                category == "jobs",
            )

            print(
                "TIMING PROCESS BATCH:",
                previous + 1,
                "to",
                limit,
                "=",
                f"{time.perf_counter() - _batch_start:.2f}s",
            )

        if can_make_20():
            break

        previous = limit

    # ========================================================
    # FALLBACK FOR REMAINING RSS CANDIDATES
    # ========================================================

    if not can_make_20():

        print(
            "\nRECENT ARTICLES INSUFFICIENT."
        )

        print(
            "USING REMAINING RSS CANDIDATES WITH THE 48-HOUR CUTOFF."
        )

        fallback = []

        for feed_index in sorted(
            candidates_by_feed
        ):

            for item in candidates_by_feed[
                feed_index
            ]:

                url = str(
                    item.get(
                        "url",
                        "",
                    )
                    or ""
                ).strip().lower()

                if url not in processed_urls:

                    fallback.append(
                        item
                    )

        process_batch(
            fallback,
            True,
        )

    # ========================================================
    # FINAL GROUPS
    # ========================================================

    groups = build_groups()

    print(
        "\nAVAILABLE SOURCES:"
    )

    for publisher, items in sorted(
        groups.items(),
        key=lambda pair: len(pair[1]),
        reverse=True,
    ):

        print(
            f"  {publisher}: "
            f"{len(items)}"
        )

    # ========================================================
    # SOURCE SELECTION
    # ========================================================

    # --------------------------------------------------------
    # STARTUP CLEAN GROUPS
    #
    # The source-balancing algorithm must see only genuine
    # startup articles. Otherwise a source such as Indian
    # Retailer can become eligible because it has enough
    # generic retail articles.
    # --------------------------------------------------------

    if category == "startups":

        startup_bad = [
            "gold price",
            "silver price",
            "gold and silver",
            "stock market",
            "nifty",
            "sensex",
            "share price",
            "shares list",
            "shares listed",
            "copyright dispute",
            "contempt action",
            "nclt approves merger",
            "merger gets nclt approval",
            "merger approval",
            "preferential issue",
            "bulk deal",
            "real estate growth",
            "future of indian housing",
            "housing market",
            "teams across the globe",
            "team across the globe",
            "kahlil gibran",
            "quotes that",
            "chief marketing officer",
            "marketing officer",
            "marketing appointment",
            "jewellery collection",
            "jewelry collection",
            "apparel collection",
            "retail india news:",
            "retail news:",
            "open-source foundations",
            "african funding",
            "four-year bootstrapping",
            "family health insurance",
            "crude oil",
            "oil prices",
            "apple tv",
            "homepod",
            "smart home hub",
            "pilot",
            "aircraft",
            "aviation",
            "plumber",
            "traders call off",
            "no upi day",
            "biography",
        ]

        startup_strong = [
            "startup",
            "startups",
            "founder",
            "founders",
            "co-founder",
            "cofounder",
            "entrepreneur",
            "entrepreneurs",
            "funding",
            "funded",
            "fundraise",
            "fundraising",
            "seed round",
            "seed funding",
            "pre-seed",
            "series a",
            "series b",
            "series c",
            "series d",
            "venture capital",
            "vc funding",
            "investment round",
            "unicorn",
            "valuation",
            "esop",
            "esops",
            "acquisition",
            "acquires",
            "acquired",
            "startup launch",
            "startup launches",
            "startup ecosystem",
            "startup india",
            "new venture",
            "d2c",
            "deeptech",
            "fintech startup",
            "edtech startup",
            "healthtech startup",
            "agritech startup",
            "saas startup",
        ]

        startup_general_news_exclusions = [
            "business model explained",
            "how one of india",
            "shares end market debut",
            "market debut",
            "share price",
            "stock debut",
            "crude oil",
            "oil prices",
            "family health insurance",
            "procurement and legal",
            "gemini 4",
            "gemini 3",
            "best ai tools",
            "ai tools for",
            "smart home hub",
            "homepod",
            "apple tv",
            "pilot",
            "aircraft",
            "aviation",
            "plumber",
            "biography",
            "net worth",
            "cybersecurity",
            "open-source ai chip",
            "chip software",
            "e2w registrations",
            "electric two-wheeler registrations",
            "no upi day",
            "traders call off",
        ]

        startup_supporting = [
            "raises",
            "raised",
            "investor",
            "investors",
            "investment",
            "backed by",
            "cash-out",
            "cash out",
            "employee stock",
            "stock options",
            "accelerator",
            "incubator",
            "portfolio company",
            "scale",
            "scaling",
            "expansion",
            "launches",
            "launched",
        ]

        clean_groups = {}

        for publisher, items in groups.items():

            clean_items = []

            for article in items:

                title = str(
                    article.get("title", "")
                    or ""
                ).strip().lower()

                description = str(
                    article.get("description", "")
                    or ""
                ).strip().lower()

                if not title:
                    continue

                if any(
                    bad in title
                    for bad in startup_bad
                ):
                    continue

                if any(
                    bad in title
                    for bad in startup_general_news_exclusions
                ):
                    continue

                title_strong_hits = sum(
                    1
                    for signal in startup_strong
                    if signal in title
                )

                description_strong_hits = sum(
                    1
                    for signal in startup_strong
                    if signal in description
                )

                title_supporting_hits = sum(
                    1
                    for signal in startup_supporting
                    if signal in title
                )

                description_supporting_hits = sum(
                    1
                    for signal in startup_supporting
                    if signal in description
                )

                startup_score = (
                    title_strong_hits * 4
                    + description_strong_hits * 2
                    + title_supporting_hits * 2
                    + description_supporting_hits
                )

                # Strong title signal = genuine startup candidate.
                # Otherwise require multiple supporting signals.
                if title_strong_hits == 0 and startup_score < 4:
                    continue

                clean_items.append(article)

            if clean_items:
                clean_groups[publisher] = clean_items

        groups = clean_groups

        # --------------------------------------------------------
        # STARTUP PUBLISHER QUALITY FILTER
        #
        # Google News can aggregate unrelated publishers into
        # startup searches. Keep only publishers that are useful
        # for an India-startup category.
        # --------------------------------------------------------

        startup_allowed_publishers = {
            "Indian Startup Times",
            "ascendants.in",
            "BW Disrupt",
            "The Economic Times",
            "Indian Retailer",
            "YourStory",
            "Inc42",
            "Entrackr",
        }

        startup_filtered_groups = {}

        for publisher, items in groups.items():

            publisher_key = (
                str(publisher or "")
                .strip()
                .lower()
            )

            allowed = any(
                publisher_key == allowed.lower()
                or publisher_key in allowed.lower()
                or allowed.lower() in publisher_key
                for allowed in startup_allowed_publishers
            )

            if allowed:
                startup_filtered_groups[
                    publisher
                ] = items

        groups = startup_filtered_groups

        print(
            "\nSTARTUP PUBLISHER FILTERED SOURCES:"
        )

        for publisher, items in sorted(
            groups.items(),
            key=lambda pair: len(pair[1]),
            reverse=True,
        ):
            print(
                f"  {publisher}: {len(items)}"
            )

        print(
            "STARTUP SOURCES AFTER FILTER:",
            len(groups),
        )

        print(
            "\nSTARTUP CLEAN SOURCE COUNTS:"
        )

        for publisher, items in sorted(
            groups.items(),
            key=lambda pair: len(pair[1]),
            reverse=True,
        ):
            print(
                f"  {publisher}: {len(items)}"
            )

    # ========================================================
    # CATEGORY SOURCE QUALITY FILTER
    #
    # Keep category results concentrated on relevant publishers.
    # Google News can aggregate many unrelated publishers, so
    # category-specific source policies prevent low-quality
    # one-article publishers from entering the final result.
    # ========================================================

    category_source_policy = {
        "kerala": {
            "The Hindu",
            "Mathrubhumi",
            "OneIndia",
            "Onmanorama",
        "Kerala Kaumudi",
        "The New Indian Express",
        "The Indian Express",
        "The Times of India",
            "The Hindu BusinessLine",
            "Deccan Chronicle",
            "NDTV",
        },
        "delhi": {
            "The Indian Express",
            "Hindustan Times",
            "The Times of India",
        "NDTV",
        "The New Indian Express",
        "news.abplive.com",
        },
        "startups": {
            "Indian Startup Times",
            "BW Disrupt",
            "The Economic Times",
            "Indian Retailer",
            "Inc42",
            "YourStory",
        },
    }

    allowed_sources = category_source_policy.get(category)

    if allowed_sources:
        filtered_groups = {}

        for publisher, items in groups.items():
            if publisher in allowed_sources:
                filtered_groups[publisher] = items

        groups = filtered_groups

        print(
            "\nCATEGORY SOURCE POLICY:",
            category,
        )

        for publisher, items in sorted(
            groups.items(),
            key=lambda pair: len(pair[1]),
            reverse=True,
        ):
            print(
                f"  {publisher}: {len(items)}"
            )

    eligible = [
        publisher
        for publisher, items
        in groups.items()
        if len(items) >= 4
    ]

    eligible.sort(
        key=lambda publisher: (
            len(
                groups[publisher]
            ),
            publisher,
        ),
        reverse=True,
    )

    selected = None
    targets = {}

    from itertools import combinations

    # --------------------------------------------------------
    # Four-source solution
    # --------------------------------------------------------

    if len(eligible) >= 4:

        for combo in combinations(
            eligible,
            4,
        ):

            capacity = sum(
                min(
                    6,
                    len(
                        groups[publisher]
                    ),
                )
                for publisher in combo
            )

            if capacity < 20:
                continue

            selected = list(
                combo
            )

            targets = {
                publisher: 4
                for publisher in selected
            }

            remaining = 4

            order = sorted(
                selected,
                key=lambda publisher: len(
                    groups[publisher]
                ),
                reverse=True,
            )

            while remaining > 0:

                changed = False

                for publisher in order:

                    if remaining <= 0:
                        break

                    if targets[
                        publisher
                    ] >= 6:
                        continue

                    if targets[
                        publisher
                    ] >= len(
                        groups[publisher]
                    ):
                        continue

                    targets[
                        publisher
                    ] += 1

                    remaining -= 1
                    changed = True

                if not changed:
                    break

            if remaining == 0:
                break

            selected = None
            targets = {}

    # --------------------------------------------------------
    # Five-source solution
    # --------------------------------------------------------

    if selected is None and len(eligible) >= 5:

        selected = eligible[:5]

        targets = {
            publisher: 4
            for publisher in selected
        }

    # ========================================================
    # MAHARASHTRA EXTRA FALLBACK
    #
    # If Maharashtra has fewer than 20 usable articles, scan all remaining
    # regional RSS candidates while keeping the 48-hour publication cutoff.
    # ========================================================

    if selected is None and category == "maharashtra":

        print(
            "\nMAHARASHTRA EXTRA FALLBACK"
        )

        # Reprocess remaining Maharashtra candidates in fallback mode.
        extra_items = []

        for feed_index in sorted(
            candidates_by_feed
        ):

            for item in candidates_by_feed[
                feed_index
            ]:

                url = str(
                    item.get(
                        "url",
                        "",
                    )
                    or ""
                ).strip().lower()

                if url not in processed_urls:
                    extra_items.append(
                        item
                    )

        print(
            "MAHARASHTRA EXTRA CANDIDATES:",
            len(extra_items),
        )

        process_batch(
            extra_items,
            True,
        )

        # Rebuild groups.
        groups = build_groups()

        print(
            "\nMAHARASHTRA SOURCES AFTER FALLBACK:"
        )

        for publisher, items in sorted(
            groups.items(),
            key=lambda pair: len(pair[1]),
            reverse=True,
        ):

            print(
                f"  {publisher}: "
                f"{len(items)}"
            )

        eligible = [
            publisher
            for publisher, items
            in groups.items()
            if len(items) >= 4
        ]

        eligible.sort(
            key=lambda publisher: (
                len(
                    groups[publisher]
                ),
                publisher,
            ),
            reverse=True,
        )

        # Try four-source solution again.
        if len(eligible) >= 4:

            from itertools import combinations

            for combo in combinations(
                eligible,
                4,
            ):

                capacity = sum(
                    min(
                        6,
                        len(
                            groups[publisher]
                        ),
                    )
                    for publisher in combo
                )

                if capacity < 20:
                    continue

                candidate_targets = {
                    publisher: 4
                    for publisher in combo
                }

                remaining = 4

                order = sorted(
                    combo,
                    key=lambda publisher: len(
                        groups[publisher]
                    ),
                    reverse=True,
                )

                while remaining > 0:

                    changed = False

                    for publisher in order:

                        if remaining <= 0:
                            break

                        if candidate_targets[
                            publisher
                        ] >= 6:
                            continue

                        if candidate_targets[
                            publisher
                        ] >= len(
                            groups[publisher]
                        ):
                            continue

                        candidate_targets[
                            publisher
                        ] += 1

                        remaining -= 1
                        changed = True

                    if not changed:
                        break

                if remaining == 0:

                    selected = list(
                        combo
                    )

                    targets = (
                        candidate_targets
                    )

                    break

        # Try five-source solution.
        if (
            selected is None
            and len(eligible) >= 5
        ):

            selected = eligible[:5]

            targets = {
                publisher: 4
                for publisher in selected
            }

    # ========================================================
    # BUILD ARTICLES FROM SUCCESSFUL SOURCE SELECTION
    # ========================================================
    #
    # The normal 4-source / 5-source selection creates
    # `selected` and `targets`. Convert those targets into
    # the final article list before final validation.
    # ========================================================

    if selected is not None:

        final_articles = []
        final_urls = set()
        final_titles = set()

        for publisher in selected:

            target = targets.get(
                publisher,
                0,
            )

            for article in groups.get(
                publisher,
                [],
            )[:target]:

                url = str(
                    article.get(
                        "url",
                        "",
                    )
                    or ""
                ).strip().lower()

                title = normalize_text(
                    article.get(
                        "title",
                        "",
                    )
                )

                if not url:
                    continue

                if url in final_urls:
                    continue

                if title and title in final_titles:
                    continue

                final_articles.append(
                    article
                )

                final_urls.add(url)

                if title:
                    final_titles.add(
                        title
                    )

        # If deduplication reduced this allocation below 20, let the
        # partial fallback gather every remaining unique candidate.
        if len(final_articles) != 20:
            selected = None

        final_articles.sort(
            key=lambda article: (
                parse_date(
                    article.get(
                        "published_at",
                        "",
                    )
                )
                or datetime.min.replace(
                    tzinfo=timezone.utc
                )
            ),
            reverse=True,
        )

        print(
            "\nSELECTED SOURCE DISTRIBUTION:"
        )

        selected_distribution = {}

        for article in final_articles:

            publisher = source_name(
                article.get(
                    "source",
                    "",
                )
            )

            selected_distribution[publisher] = (
                selected_distribution.get(
                    publisher,
                    0,
                ) + 1
            )

        for publisher, count in sorted(
            selected_distribution.items(),
            key=lambda pair: pair[1],
            reverse=True,
        ):

            print(
                f"  {publisher}: {count}"
            )

        print(
            f"TOTAL ARTICLES: "
            f"{len(final_articles)}"
        )

        if len(final_articles) == 20 and not all(
            4 <= count <= 6
            for count in selected_distribution.values()
        ):
            # A publisher may lose candidates to title/URL
            # deduplication after target allocation. Let the strict
            # fallback rebalance from the full candidate groups rather
            # than failing the whole category request.
            print(
                "SOURCE ALLOCATION NEEDS REBALANCING:",
                selected_distribution,
            )
            selected = None
            final_articles = []

    # ========================================================
    # FINAL FAILURE
    # ========================================================

    balance_fallback = False
    partial_fallback = False

    # WEATHER ONLY:
    # Keep a balanced 20-story mix from four to six publishers. The
    # Weather fallback previously appended newest stories without a source
    # limit, which could leave the page short or dominated by one publisher.
    if category == "weather" and selected is None:

        balance_fallback = True
        weather_groups = {}
        weather_urls = set()
        weather_titles = set()

        weather_processed = sorted(
            processed,
            key=lambda article: (
                parse_date(
                    article.get(
                        "published_at",
                        "",
                    )
                )
                or datetime.min.replace(
                    tzinfo=timezone.utc
                )
            ),
            reverse=True,
        )

        for article in weather_processed:

            url = str(
                article.get("url", "") or ""
            ).strip().lower()

            title = normalize_text(
                article.get("title", "")
            )

            if not url or url in weather_urls:
                continue

            if title and title in weather_titles:
                continue

            # ------------------------------------------------
            # WEATHER FINAL VALIDATION
            # ------------------------------------------------
            # Weather results must be genuinely weather-focused.
            # Do not allow sports, entertainment, business,
            # politics, technology, gaming, etc. just because
            # they mention rain/weather in passing.
            # ------------------------------------------------

            article_title = normalize_text(
                article.get("title", "")
            ).lower()

            article_description = normalize_text(
                article.get("description", "")
            ).lower()

            # The headline determines the story's subject. The summary often
            # mentions weather as background, which was allowing finance and
            # general-news headlines into this category.
            weather_text = article_title

            weather_terms = {
                "weather",
                "forecast",
                "weather forecast",
                "rain forecast",
                "storm forecast",
                "temperature forecast",
                "monsoon forecast",
                "rain",
                "rainfall",
                "heavy rain",
                "heavy rainfall",
                "monsoon",
                "cyclone",
                "hurricane",
                "typhoon",
                "storm",
                "thunderstorm",
                "flood",
                "flooding",
                "heatwave",
                "heat wave",
                "cold wave",
                "temperature",
                "temperatures",
                "meteorological",
                "imd",
                "precipitation",
                "drought",
                "snowfall",
                "snowstorm",
                "blizzard",
                "tornado",
                "wildfire",
                "cloudburst",
                "lightning",
                "fog",
                "hailstorm",
                "hail",
                "landslide",
                "waterlogging",
                "extreme weather",
                "el nino",
                "la nina",
                "air quality",
                "heat index",
                "climate",
            }

            non_weather_topic_terms = {
                "cricket",
                "football",
                "soccer",
                "tennis",
                "match",
                "tournament",
                "asian games",
                "sports",
                "movie",
                "film",
                "actor",
                "actress",
                "entertainment",
                "gaming",
                "video game",
                "grand theft auto",
                "gta",
                "politics",
                "political",
                "election",
                "minister",
                "parliament",
                "business",
                "stock market",
                "stocks",
                "shares",
                "finance",
                "technology",
                "technology news",
                "smartphone",
                "iphone",
                "android",

                # Strong sports-event indicators. These prevent
                # match/sports stories from entering Weather just
                # because the article mentions rain or weather.
                " vs ",
                "versus",
                "final",
                "semi-final",
                "semifinal",
                "quarter-final",
                "quarterfinal",
                "playing 11",
                "playing xi",
                "pitch report",
                "team news",
                "live streaming",
                "live stream",
                "scorecard",
                "wicket",
                "wickets",
                "innings",
                "batting",
                "bowling",
                "goal",
                "goals",
                "medal",
            }

            weather_signal_count = sum(
                1
                for term in weather_terms
                if re.search(
                    rf"(?<!\w){re.escape(term)}(?!\w)",
                    weather_text,
                )
            )

            # Background descriptions can mention other subjects; use the
            # headline to decide whether the article itself is off-topic.
            non_weather_signal_count = sum(
                1
                for term in non_weather_topic_terms
                if re.search(
                    rf"(?<!\w){re.escape(term)}(?!\w)",
                    article_title,
                )
            )

            article_feed_url = str(
                article.get("feed_url", "") or ""
            ).lower()
            # Even a publisher's dedicated Weather feed can mix in general
            # headlines. Require an actual weather signal in each headline
            # so unrelated summaries cannot fill the category quota.
            if weather_signal_count == 0:
                continue

            # A clearly non-weather topic must not enter the
            # Weather category merely because weather is mentioned.
            if non_weather_signal_count > 0:
                continue

            publisher = source_name(article.get("source", ""))
            weather_groups.setdefault(publisher, []).append(article)
            weather_urls.add(url)

            if title:
                weather_titles.add(title)

        final_articles = select_partial_balanced(
            weather_groups,
            limit=20,
            max_per_source=6,
            require_4_to_6_sources=True,
        )
        print(
            f"WEATHER BALANCED FALLBACK: "
            f"{len(final_articles)} unique articles"
        )

    if selected is None and category != "weather":

        balance_fallback = False

        print(
            "\nBALANCE FALLBACK: searching for a strict "
            "4-6 source distribution."
        )

        # ========================================================
        # STRICT FALLBACK
        #
        # Requirements:
        #   - exactly 20 unique articles
        #   - exactly 4 or 5 publishers
        #   - every publisher contributes 4-6 articles
        #
        # Publishers with fewer than 4 usable articles are
        # completely excluded.
        # ========================================================

        fallback_groups = {}

        for article in processed:

            url = str(
                article.get("url", "") or ""
            ).strip().lower()

            title = normalize_text(
                article.get("title", "")
            )

            publisher = source_name(
                article.get("source", "")
            )

            if not url or not publisher:
                continue

            fallback_groups.setdefault(
                publisher,
                []
            ).append(article)

        # Remove duplicate URLs/titles per publisher.
        for publisher, items in list(
            fallback_groups.items()
        ):

            clean_items = []
            seen_urls = set()
            seen_titles = set()

            for article in items:

                url = str(
                    article.get("url", "") or ""
                ).strip().lower()

                title = normalize_text(
                    article.get("title", "")
                )

                if not url:
                    continue

                if url in seen_urls:
                    continue

                if title and title in seen_titles:
                    continue

                clean_items.append(article)
                seen_urls.add(url)

                if title:
                    seen_titles.add(title)

            if clean_items:
                fallback_groups[publisher] = clean_items
            else:
                del fallback_groups[publisher]

        print("\nDEBUG FALLBACK GROUP COUNTS:")
        for _publisher, _items in sorted(
            fallback_groups.items(),
            key=lambda pair: len(pair[1]),
            reverse=True,
        ):
            print(
                f"  {_publisher}: {len(_items)}"
            )

        # ONLY publishers with at least 4 articles are eligible.
        fallback_publishers = [
            publisher
            for publisher, items in fallback_groups.items()
            if len(items) >= 4
        ]

        fallback_publishers.sort(
            key=lambda publisher: (
                min(6, len(fallback_groups[publisher])),
                len(fallback_groups[publisher]),
                publisher,
            ),
            reverse=True,
        )

        print("\nSTRICT FALLBACK ELIGIBLE SOURCES:")

        for publisher in fallback_publishers:
            print(
                f"  {publisher}: "
                f"{len(fallback_groups[publisher])}"
            )

        selected_fallback = None
        fallback_targets = None

        from itertools import combinations

        # --------------------------------------------------------
        # FOUR-SOURCE SOLUTION
        # --------------------------------------------------------

        if len(fallback_publishers) >= 4:

            for combo in combinations(
                fallback_publishers,
                4,
            ):

                capacities = {
                    publisher: min(
                        6,
                        len(
                            fallback_groups[publisher]
                        ),
                    )
                    for publisher in combo
                }

                if sum(capacities.values()) < 20:
                    continue

                candidate_targets = {
                    publisher: 4
                    for publisher in combo
                }

                remaining = 4

                order = sorted(
                    combo,
                    key=lambda publisher: (
                        capacities[publisher],
                        len(
                            fallback_groups[publisher]
                        ),
                    ),
                    reverse=True,
                )

                while remaining > 0:

                    changed = False

                    for publisher in order:

                        if remaining <= 0:
                            break

                        if candidate_targets[publisher] >= 6:
                            continue

                        if (
                            candidate_targets[publisher]
                            >= capacities[publisher]
                        ):
                            continue

                        candidate_targets[publisher] += 1
                        remaining -= 1
                        changed = True

                    if not changed:
                        break

                if remaining == 0:

                    selected_fallback = list(combo)
                    fallback_targets = candidate_targets
                    break

        # --------------------------------------------------------
        # FIVE-SOURCE SOLUTION
        # --------------------------------------------------------

        if (
            selected_fallback is None
            and len(fallback_publishers) >= 5
        ):

            selected_fallback = fallback_publishers[:5]

            fallback_targets = {
                publisher: 4
                for publisher in selected_fallback
            }

        # --------------------------------------------------------
        # NO VALID STRICT SOLUTION
        #
        # Do not silently violate the 4-6 rule.
        # --------------------------------------------------------

        if (
            selected_fallback is None
            or fallback_targets is None
        ):
            # Do not fail an undersupplied category. Return its largest
            # unique set while retaining source caps where possible.
            partial_fallback = True
            final_articles = select_partial_balanced(
                fallback_groups,
                limit=20,
            )
            selected_fallback = []
            fallback_targets = {}

        print(
            "\nSTRICT FALLBACK SELECTED SOURCES:"
        )

        for publisher in selected_fallback:
            print(
                f"  {publisher}: "
                f"{fallback_targets[publisher]} articles"
            )

        fallback_articles = []
        fallback_urls = set()
        fallback_titles = set()

        for publisher in selected_fallback:

            target = fallback_targets[publisher]

            for article in fallback_groups[publisher][:target]:

                url = str(
                    article.get("url", "") or ""
                ).strip().lower()

                title = normalize_text(
                    article.get("title", "")
                )

                if not url or url in fallback_urls:
                    continue

                if title and title in fallback_titles:
                    continue

                fallback_articles.append(article)
                fallback_urls.add(url)

                if title:
                    fallback_titles.add(title)

        if len(fallback_articles) != 20:
            partial_fallback = True
            fallback_articles = select_partial_balanced(
                fallback_groups,
                limit=20,
            )

        final_articles = fallback_articles

        final_articles.sort(
            key=lambda article: (
                parse_date(
                    article.get(
                        "published_at",
                        "",
                    )
                )
                or datetime.min.replace(
                    tzinfo=timezone.utc
                )
            ),
            reverse=True,
        )

        distribution = {}

        for article in final_articles:

            publisher = source_name(
                article.get("source", "")
            )

            distribution[publisher] = (
                distribution.get(publisher, 0) + 1
            )

        print(
            "\nSTRICT FALLBACK SOURCE DISTRIBUTION:"
        )

        for publisher, count in sorted(
            distribution.items(),
            key=lambda pair: pair[1],
            reverse=True,
        ):
            print(
                f"  {publisher}: {count}"
            )

        print(
            f"TOTAL ARTICLES: {len(final_articles)}"
        )

        strict_distribution_ok = (
            len(final_articles) == 20
            and 4 <= len(distribution) <= 5
            and all(4 <= count <= 6 for count in distribution.values())
        )

        if not strict_distribution_ok:
            # Exact balance can become impossible after duplicate URLs,
            # duplicate headlines, and undersupplied publishers are
            # removed. Return the best unique, capped source-balanced
            # set instead of failing the entire category endpoint.
            partial_fallback = True
            final_articles = select_partial_balanced(
                fallback_groups,
                limit=20,
            )

    # ========================================================
    # FINAL VALIDATION
    # ========================================================

    distribution = {}

    for article in final_articles:

        publisher = source_name(
            article.get(
                "source",
                "",
            )
        )

        distribution[
            publisher
        ] = (
            distribution.get(
                publisher,
                0,
            )
            + 1
        )

    print(
        "\n" + "=" * 70
    )

    print(
        "FINAL SOURCE DISTRIBUTION:"
    )

    for publisher, count in sorted(
        distribution.items()
    ):

        print(
            f"  {publisher}: {count}"
        )

    print(
        "TOTAL ARTICLES:",
        len(final_articles),
    )

    print(
        "TOTAL SOURCES:",
        len(distribution),
    )

    print("DEBUG VALIDATION TYPES:")
    print("  final_articles:", type(final_articles).__name__, len(final_articles) if final_articles is not None else None)
    print("  distribution:", type(distribution).__name__, distribution)
    print("  balance_fallback:", type(balance_fallback).__name__, balance_fallback)

    valid = (
        len(final_articles) <= 20
        and (
            partial_fallback
            or (category == "weather" and balance_fallback)
            or (
                len(final_articles) == 20
                and (
                    balance_fallback
                    or (
                        4 <= len(distribution) <= 6
                        and all(
                            4 <= count <= 6
                            for count in distribution.values()
                        )
                    )
                )
            )
        )
    )

    if not valid:

        raise RuntimeError(
            "FINAL BALANCE VALIDATION FAILED: "
            f"{distribution}"
        )

    print(
        "CATEGORY BALANCE: PASS"
    )

    # --------------------------------------------------------
    # FINAL TV9 TRANSLATION
    # Translate ONLY the TV9 Telugu articles that survived
    # the final 20-article source balancing.
    # --------------------------------------------------------

    tv9_articles = []

    for article in final_articles:

        article_source = str(
            article.get("source", "")
        ).strip().lower()

        article_url = str(
            article.get("url", "")
        ).strip().lower()

        is_tv9 = (
            "tv9 telugu" in article_source
            or "tv9telugu.com" in article_url
        )

        if is_tv9:
            tv9_articles.append(article)

    def translate_tv9_worker(article):
        try:
            print(
                "FINAL TV9 TRANSLATION:",
                article.get("title", "")[:100]
            )

            translated_title, translated_description = (
                translate_tv9_article(
                    article.get("title", ""),
                    article.get("description", ""),
                )
            )

            return (
                article,
                translated_title,
                translated_description,
                article.get("description", ""),
                None,
            )

        except Exception as error:
            return (
                article,
                article.get("title", ""),
                article.get("description", ""),
                article.get("description", ""),
                error,
            )

    tv9_translation_start = time.perf_counter()
    print("TIMING TV9 TRANSLATION START")

    tv9_count = 0

    if tv9_articles:

        with ThreadPoolExecutor(
            max_workers=min(2, len(tv9_articles))
        ) as tv9_executor:

            tv9_results = list(
                tv9_executor.map(
                    translate_tv9_worker,
                    tv9_articles,
                )
            )

        for (
            article,
            translated_title,
            translated_description,
            source_description,
            error,
        ) in tv9_results:

            if error:
                print(
                    "FINAL TV9 TRANSLATION ERROR:",
                    error,
                )
                continue

            article["title"] = translated_title
            translated_copy = build_description(
                translated_title,
                [translated_description],
            )
            # Never fall back to Telugu text when translation output is
            # incomplete; the final English-only selector will replace it
            # with another validated story if one is available.
            article["description"] = translated_copy
            tv9_count += 1

    # Keep every returned description compact after any source-specific
    # translation. Preserve the already validated source copy if a
    # translation produces an unusably short description.
    for article in final_articles:
        translated_or_original = build_description(
            article.get("title", ""),
            [article.get("description", "")],
        )
        if len(translated_or_original) >= 280:
            article["description"] = translated_or_original

    print(
        f"FINAL TV9 ARTICLES TRANSLATED: {tv9_count}"
    )

    print(
        f"TIMING TV9 TRANSLATION END: "
        f"{time.perf_counter() - tv9_translation_start:.2f}s"
    )

    # Re-select from all processed, category-validated stories after
    # translations finish. This replaces failed/non-English translations
    # with English candidates while preserving de-duplication and the
    # existing 4–6-per-publisher balance whenever 20 stories are available.
    selected_english = [
        article
        for article in final_articles
        if is_english_text(
            article.get("title", ""),
            article.get("description", ""),
        )
    ]
    english_groups = {}
    for publisher, items in groups.items():
        english_items = [
            article
            for article in items
            if (
                is_english_text(
                    article.get("title", ""),
                    article.get("description", ""),
                )
                and len(
                    str(article.get("description", "")).split()
                ) >= (12 if category == "weather" else 20)
            )
        ]
        if english_items:
            english_groups[publisher] = english_items

    if len(selected_english) == 20:
        # The chosen 20 already passed the category, freshness, quality,
        # duplicate, and source-balance rules. Reusing them avoids losing a
        # category-specific publisher selection during the language pass.
        english_final = selected_english
    elif category == "weather":
        # Rebalance after the English-text pass so TV9 or non-English RSS
        # items cannot shrink the visible source mix or crowd one publisher.
        english_final = select_partial_balanced(
            english_groups,
            limit=20,
            max_per_source=6,
            require_4_to_6_sources=True,
        )
    else:
        english_final = select_partial_balanced(
            english_groups,
            limit=20,
        )

    if len(english_final) >= len(final_articles):
        final_articles = english_final
    else:
        # If translation failures leave fewer candidates, prefer the largest
        # validated English set rather than returning any non-English copy.
        final_articles = english_final

    final_articles = [
        article
        for article in final_articles
        if category_is_recent(article.get("published_at", ""))
        and is_english_text(
            article.get("title", ""),
            article.get("description", ""),
        )
    ][:20]

    # Fill any remaining Weather slots from validated RSS copy, then rebalance
    # the combined set across four to six publishers.
    if category == "weather" and len(final_articles) < 20:
        rss_weather_groups = {}
        for article in final_articles:
            publisher = source_name(article.get("source", ""))
            rss_weather_groups.setdefault(publisher, []).append(article)
        rss_urls = {
            str(article.get("url", "")).split("#", 1)[0].rstrip("/").lower()
            for article in final_articles
        }
        rss_titles = {
            normalize_text(article.get("title", ""))
            for article in final_articles
        }
        rss_items = sorted(
            (
                item
                for items in candidates_by_feed.values()
                for item in items
            ),
            key=lambda item: (
                parse_date(item.get("published_at", ""))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )

        for item in rss_items:
            item_source = str(item.get("source", "") or "").strip()
            title = clean_title(
                item.get("title", ""),
                item_source or source_name(item.get("feed_url", "")),
            )
            url = str(item.get("url", "") or "").strip()
            title_key = normalize_text(title)
            published_at = item.get("published_at", "")
            if (
                not title
                or not url
                or not category_is_recent(published_at)
                or url.lower() in rss_urls
                or title_key in rss_titles
                or not matches_category(item, "weather")
            ):
                continue

            rss_description = build_description(
                title,
                [
                    clean_text(item.get("description", "")),
                    clean_text(
                        item.get("encoded", "")
                        or item.get("content_encoded", "")
                        or item.get("content", "")
                    ),
                ],
            )
            title_key_for_summary = normalize_text(title)
            if (
                title_key_for_summary
                and normalize_text(rss_description).startswith(
                    title_key_for_summary
                )
            ):
                rss_description = re.sub(
                    rf"^\s*{re.escape(title)}[\s:;,.|–—-]*",
                    "",
                    rss_description,
                    flags=re.IGNORECASE,
                ).strip()
            if (
                len(rss_description) < 120
                or len(rss_description.split()) < 12
            ):
                continue

            rss_description = compact_card_description(title, rss_description)
            article = {
                "title": title,
                "description": rss_description,
                "url": url,
                "source": source_name(
                    item.get("source", "")
                    or item.get("feed_url", "")
                ),
                "published_at": published_at,
            }
            publisher = article["source"]
            rss_weather_groups.setdefault(publisher, []).append(article)
            rss_urls.add(url.split("#", 1)[0].rstrip("/").lower())
            rss_titles.add(title_key)

        rss_weather_articles = select_partial_balanced(
            rss_weather_groups,
            limit=20,
            max_per_source=6,
            require_4_to_6_sources=True,
        )
        if rss_weather_articles:
            print(
                "WEATHER RSS FALLBACK:",
                len(rss_weather_articles),
                "articles",
            )
            final_articles = rss_weather_articles

    if category == "weather":
        for article in final_articles:
            article["description"] = compact_card_description(
                article.get("title", ""),
                article.get("description", ""),
            )

    # Apply the Latest/India separation to the final selected feed, then
    # record only stories actually returned to the browser. This avoids
    # reserving extra candidates that did not make the visible 20 articles.
    if category in {"latest", "india"}:
        unique_for_category = []
        with CROSS_CATEGORY_REGISTRY_LOCK:
            for article in final_articles:
                raw_url = str(article.get("url", "") or "")
                url_key = re.sub(
                    r"([?&])(utm_[^=]+|fbclid|gclid)=[^&#]*",
                    "",
                    raw_url.split("#", 1)[0],
                    flags=re.IGNORECASE,
                ).rstrip("?&/").lower()
                title_key = normalize_text(article.get("title", ""))

                existing = CROSS_CATEGORY_URL_REGISTRY.get(url_key) if url_key else None
                title_duplicate = any(
                    known.get("category") in {"latest", "india"}
                    and known.get("category") != category
                    and title_key
                    and title_key == known.get("title")
                    for known in CROSS_CATEGORY_URL_REGISTRY.values()
                )
                if (
                    existing
                    and existing.get("category") in {"latest", "india"}
                    and existing.get("category") != category
                ) or title_duplicate:
                    continue

                unique_for_category.append(article)
                if url_key:
                    CROSS_CATEGORY_URL_REGISTRY[url_key] = {
                        "category": category,
                        "time": datetime.now(timezone.utc),
                        "title": title_key,
                    }

        final_articles = unique_for_category

    # Refill after translation, freshness, de-duplication, and Latest/India
    # separation. All candidates in `groups` have already passed the feed's
    # category relevance and article processing checks, so this only reuses
    # validated stories that were left out of an earlier source allocation.
    if len(final_articles) < MAX_ARTICLES:
        seen_urls = {
            str(article.get("url", "")).split("#", 1)[0].rstrip("/").lower()
            for article in final_articles
            if article.get("url")
        }
        seen_titles = {
            normalize_text(article.get("title", ""))
            for article in final_articles
            if article.get("title")
        }
        source_counts = {}
        for article in final_articles:
            publisher = source_name(article.get("source", ""))
            source_counts[publisher] = source_counts.get(publisher, 0) + 1

        for publisher, candidates in sorted(
            groups.items(), key=lambda pair: len(pair[1]), reverse=True
        ):
            if len(final_articles) >= MAX_ARTICLES:
                break
            publisher = source_name(publisher)
            if publisher not in source_counts and len(source_counts) >= 6:
                continue
            for article in candidates:
                if len(final_articles) >= MAX_ARTICLES:
                    break
                if not category_is_recent(article.get("published_at", "")):
                    continue
                title = str(article.get("title", "") or "").strip()
                description = str(article.get("description", "") or "").strip()
                url = str(article.get("url", "") or "").split("#", 1)[0].rstrip("/").lower()
                title_key = normalize_text(title)
                minimum_words = 12 if category == "weather" else 20
                minimum_chars = 120 if category == "weather" else 160
                if (
                    not url
                    or not title_key
                    or url in seen_urls
                    or title_key in seen_titles
                    or len(description) < minimum_chars
                    or len(description.split()) < minimum_words
                    or not is_english_text(title, description)
                    # The primary selector targets six per publisher. If a
                    # category is still short, allow two extra from that
                    # source before returning fewer than 20 cards.
                    or source_counts.get(publisher, 0) >= 8
                ):
                    continue

                final_articles.append(dict(article))
                seen_urls.add(url)
                seen_titles.add(title_key)
                source_counts[publisher] = source_counts.get(publisher, 0) + 1

        final_articles = final_articles[:MAX_ARTICLES]

    print(
        "FINAL ENGLISH ARTICLE COUNT:",
        len(final_articles),
    )

    print(
        "=" * 70
    )

    return final_articles


def search_news(
    query: str,
    category: str = "latest",
) -> List[dict]:
    """
    Final category/search engine.

    Category mode:
        - Returns up to 20 category articles.
        - Does NOT require the category name to appear
          in the article.
        - Balances articles across sources.
        - Removes duplicate URLs/titles.

    Search mode:
        - Uses the query to find relevant articles.
    """

    # --------------------------------------------------------
    # Normalize inputs
    # --------------------------------------------------------

    original_query = str(
        query or ""
    ).strip().lower()

    category = str(
        category or "latest"
    ).strip().lower()

    # --------------------------------------------------------
    # Category aliases
    # --------------------------------------------------------

    category_aliases = {
        "andhra-pradesh": "andhra pradesh",
        "andhrapradesh": "andhra pradesh",
        "tamil-nadu": "tamil nadu",
        "tamilnadu": "tamil nadu",
    }

    category = category_aliases.get(
        category,
        category,
    )

    # --------------------------------------------------------
    # Detect category browsing mode
    #
    # If the query is simply the category name, we should
    # NOT require that word to exist inside the article.
    # --------------------------------------------------------

    normalized_query = re.sub(
        r"[\s_-]+",
        " ",
        original_query,
    ).strip()

    category_query_names = {
        category,
        category.replace(
            " ",
            "-",
        ),
        category.replace(
            " ",
            "",
        ),
    }

    category_mode = (
        normalized_query in
        category_query_names
        or original_query in (
            "latest",
            "india",
        )
    )

    # --------------------------------------------------------
    # Get feeds
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Search feed selection
    #
    # Category browsing uses only that category's feeds.
    # Query search uses a lead feed for every category plus one
    # query-specific feed, rather than loading every source on each keystroke.
    # --------------------------------------------------------

    if category_mode:
        feeds = CATEGORY_FEEDS.get(
            category
        )
    else:
        # Use the query-specific feed for targeted searches. Waiting for
        # category lead feeds as well made a city/topic search take as long
        # as the slowest unrelated publisher feed.
        from urllib.parse import quote_plus

        query_feed_url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(original_query)}"
            "&hl=en-IN"
            "&gl=IN"
            "&ceid=IN:en"
        )
        feeds = [query_feed_url]

    if not feeds:
        print(
            f"SEARCH NO FEEDS: {category}"
        )

        return []

    print("=" * 60)
    print(
        f"SEARCH CATEGORY: {category}"
    )
    print(
        f"SEARCH MODE: "
        f"{'CATEGORY' if category_mode else 'QUERY'}"
    )
    print(
        f"SEARCH FEEDS: {len(feeds)}"
    )
    print("=" * 60)

    # --------------------------------------------------------
    # Fetch RSS feeds
    # --------------------------------------------------------

    raw_items = []

    max_workers = min(
        max(len(feeds), 1),
        12,
    )

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        future_map = {
            executor.submit(
                fetch_rss,
                feed_url,
                4 if not category_mode else REQUEST_TIMEOUT,
            ): feed_url
            for feed_url in feeds
        }

        for future in as_completed(
            future_map
        ):

            feed_url = future_map[
                future
            ]

            try:

                items = future.result()

                print(
                    f"SEARCH RSS: {feed_url}"
                )

                print(
                    f"SEARCH RSS ITEMS: "
                    f"{len(items)}"
                )

                for item in items:

                    if not isinstance(
                        item,
                        dict,
                    ):
                        continue

                    article = dict(item)

                    article[
                        "_feed_url"
                    ] = feed_url

                    raw_items.append(
                        article
                    )

            except Exception as error:

                print(
                    f"SEARCH FEED ERROR: "
                    f"{feed_url} -> {error}"
                )

    if not raw_items:

        print(
            "SEARCH NO RSS ITEMS"
        )

        return []

    print("SEARCH RAW ITEMS:", len(raw_items))

    # --------------------------------------------------------
    # Query words
    # --------------------------------------------------------

    words = [
        word
        for word in re.findall(
            r"[a-z0-9]+",
            original_query,
        )
        if len(word) >= 2
    ]

    # --------------------------------------------------------
    # Score and filter
    # --------------------------------------------------------

    scored = []

    for item in raw_items:

        title = str(
            item.get(
                "title",
                "",
            )
            or ""
        ).strip()

        description = str(
            item.get(
                "description",
                "",
            )
            or ""
        ).strip()

        source = str(
            item.get(
                "source",
                "",
            )
            or ""
        ).strip()

        feed_url = str(
            item.get(
                "_feed_url",
                "",
            )
            or ""
        ).strip()

        # ----------------------------------------------------
        # Category filtering
        #
        # IMPORTANT:
        # Region-specific feeds are trusted by the existing
        # matches_category() logic.
        # ----------------------------------------------------

        if not matches_category(
            item,
            category,
            feed_url,
        ):
            continue

        text_blob = (
            f"{title} "
            f"{description} "
            f"{source}"
        ).lower()


        # ----------------------------------------------------
        # STARTUP FINAL QUALITY GATE
        #
        # This is applied before Startup articles enter the
        # source-balancing stage.
        # ----------------------------------------------------
        if category == "startups":

            _startup_title = (item.get("title") or "").strip().lower()

            _startup_bad = [
                "gold price",
                "silver price",
                "gold and silver",
                "stock market",
                "nifty",
                "sensex",
                "share price",
                "shares list",
                "shares listed",
                "copyright dispute",
                "contempt action",
                "nclt approves merger",
                "merger gets nclt approval",
                "merger approval",
                "preferential issue",
                "bulk deal",
                "real estate growth",
                "future of indian housing",
                "housing market",
                "teams across the globe",
                "team across the globe",
                "kahlil gibran",
                "quotes that",
                "chief marketing officer",
                "marketing officer",
                "marketing appointment",
                "jewellery collection",
                "jewelry collection",
                "apparel collection",
                "retail india news",
                "retail news:",
                "d2c business",
                "open-source foundations",
                "african funding",
                "four-year bootstrapping",
                "future of indian housing",
            ]

            if any(x in _startup_title for x in _startup_bad):
                continue

            _startup_good = [
                "startup",
                "startups",
                "start-up",
                "start-ups",
                "funding",
                "fundraise",
                "fundraising",
                "raises ",
                " raised ",
                "seed round",
                "seed funding",
                "pre-seed",
                "series a",
                "series b",
                "series c",
                "venture capital",
                "venture fund",
                "vc fund",
                "angel investor",
                "angel funding",
                "unicorn",
                "founder",
                "co-founder",
                "cofounder",
                "deeptech",
                "deep-tech",
            ]

            if not any(x in _startup_title for x in _startup_good):
                continue


        # ----------------------------------------------------
        # CATEGORY MODE
        #
        # Do NOT require category words.
        # Every valid article from the category feed is useful.
        # ----------------------------------------------------

        if category_mode:

            # ------------------------------------------------
            # STARTUP QUALITY FILTER
            #
            # Run BEFORE source balancing.
            # Only accept articles whose TITLE clearly
            # describes a startup/ecosystem story.
            # ------------------------------------------------

            if category == "startups":

                # Startup candidates are already coming from
                # Startup-specific feeds/search queries.
                # Do not require literal startup keywords in
                # every headline because valid ecosystem stories
                # may mention founders, investors, AI funds,
                # accelerators, deep-tech, or startup finance
                # without using the word "startup".

                startup_title_terms = []
                startup_title_terms = [
    "startup",
    "startups",
    "start-up",
    "start-ups",
    "funding",
    "fundraise",
    "fundraising",
    "raises",
    "raised",
    "seed",
    "pre-seed",
    "series a",
    "series b",
    "series c",
    "venture capital",
    "venture fund",
    "vc fund",
    "angel investor",
    "angel funding",
    "unicorn",
    "founder",
    "co-founder",
    "cofounder",
    "deeptech",
    "deep-tech",
    "ai fund",
]
                startup_reject_terms = [
        "gadgets weekly",
                    "gold and silver",
                    "gold price",
                    "silver price",
                    "stock market",
                    "nifty",
                    "sensex",
                    "share price",
                    "shares list",
                    "shares listed",
                    "copyright dispute",
                    "contempt action",
                    "nclt approves merger",
                    "merger gets nclt approval",
                    "merger with",
                    "merger approval",
                    "preferential issue",
                    "bulk deal",
                    "real estate growth",
                    "future of indian housing",
                    "housing market",
                    "teams across the globe",
                    "team across the globe",
                    "kahlil gibran",
                    "quotes that",
                    "marketing officer",
                    "chief marketing officer",
                    "marketing appointment",
                    "jewellery collection",
                    "jewelry collection",
                    "apparel collection",
                    "retail news",
                    "retail expansion",
                    "retail business",
                    "d2c business",
                    "open-source foundations",
                ]

                if any(
                    term in title.lower()
                    for term in startup_reject_terms
                ):
                    continue

                startup_title_match = any(
                    term in title.lower()
                    for term in startup_title_terms
                )

                if not startup_title_match:
                    continue

            score = 0

            # Prefer newer-looking articles
            score += 1

            # Prefer articles with title
            if title:
                score += 5

            # Prefer articles with description
            if description:
                score += 2

            scored.append(
                (
                    score,
                    item,
                )
            )

            continue

        # ----------------------------------------------------
        # SEARCH MODE
        #
        # Concept-aware relevance ranking.
        # ----------------------------------------------------

        if not words:
            continue

        title_lower = title.lower()
        description_lower = description.lower()
        source_lower = source.lower()

        query_phrase = " ".join(words)

        def contains_term(text_value, term):
            return bool(
                re.search(
                    rf"(?<!\w){re.escape(term)}(?!\w)",
                    text_value,
                )
            )

        # ----------------------------------------------------
        # Search aliases / concepts
        # ----------------------------------------------------

        search_phrases = [query_phrase]

        # ----------------------------------------------------
        # SEARCH CONCEPT ALIASES
        # ----------------------------------------------------
        # Expand common natural-language searches so users do
        # not need the exact word used in a news headline.
        # ----------------------------------------------------

        if query_phrase in (
            "jobs",
            "job",
        ):
            search_phrases.extend([
                "jobs",
                "job",
                "hiring",
                "recruitment",
                "recruiting",
                "career",
                "careers",
                "vacancy",
                "vacancies",
                "employment",
                "job openings",
                "job opening",
            ])

        # WEATHER SEARCH RELEVANCE TERMS
        weather_relevance_terms = {
            "weather",
            "forecast",
            "weather forecast",
            "rain forecast",
            "storm forecast",
            "temperature forecast",
            "monsoon forecast",
            "weather warning",
            "weather alert",
            "rain",
            "rainfall",
            "heavy rain",
            "heavy rainfall",
            "monsoon",
            "cyclone",
            "storm",
            "flood",
            "flooding",
            "heatwave",
            "heat wave",
            "cold wave",
            "temperature",
            "temperatures",
            "thunderstorm",
            "thunderstorms",
            "imd",
            "meteorological",
            "climate",
        }

        # Strong non-weather topics to reject from Weather search.
        # This prevents sports, entertainment, politics, business,
        # technology, and similar stories from appearing merely
        # because they mention rain/weather.
        weather_search_non_topic_terms = {
            "cricket",
            "football",
            "soccer",
            "tennis",
            "match",
            "tournament",
            "asian games",
            "sports",
            " vs ",
            "versus",
            "final",
            "semi-final",
            "semifinal",
            "quarter-final",
            "quarterfinal",
            "playing 11",
            "playing xi",
            "pitch report",
            "team news",
            "live streaming",
            "live stream",
            "scorecard",
            "wicket",
            "wickets",
            "innings",
            "batting",
            "bowling",
            "medal",
            "movie",
            "film",
            "actor",
            "actress",
            "entertainment",
            "gaming",
            "video game",
            "grand theft auto",
            "gta",
            "politics",
            "political",
            "election",
            "minister",
            "parliament",
            "business",
            "stock market",
            "stocks",
            "shares",
            "finance",
            "technology",
            "smartphone",
            "iphone",
            "android",
        }

        # JOBS SEARCH RELEVANCE TERMS
        # Focus on actionable job opportunities and recruitment
        # across Government and Private sectors in India.
        jobs_relevance_terms = {
            # General job openings
            "job opening",
            "job openings",
            "job vacancy",
            "job vacancies",
            "invites applications for posts",
            "invites applications for positions",
            "applications invited for posts",
            "applications invited for positions",
            "junior research fellow",
            "research fellow position",
            "research fellow positions",
            "vacancy",
            "vacancies",
            "recruitment",
            "recruiting",
            "hiring",
            "hired",
            "career opportunity",
            "career opportunities",
            "employment opportunity",
            "employment opportunities",

            # Government recruitment
            "government job",
            "government jobs",
            "govt job",
            "govt jobs",
            "government recruitment",
            "govt recruitment",
            "central government",
            "central govt",
            "state government",
            "state govt",
            "public sector",
            "government vacancy",
            "government vacancies",
            "recruitment notification",
            "recruitment notifications",
            "recruitment drive",

            # Major government exams / recruitment bodies
            "railway recruitment",
            "railway jobs",
            "banking jobs",
            "bank jobs",
            "bank recruitment",
            "state psc",
            "psc recruitment",
            "police recruitment",
            "police jobs",
            "defence recruitment",
            "defense recruitment",
            "defence jobs",
            "defense jobs",
            "teaching jobs",
            "teacher recruitment",

            # Private-sector hiring
            "private sector jobs",
            "private jobs",
            "private company hiring",
            "company hiring",
            "corporate hiring",
            "mass hiring",
            "bulk hiring",
            "hiring drive",
            "recruitment drive",
            "campus hiring",
            "campus recruitment",
            "campus placement",
            "campus placements",

            # Freshers / graduates
            "fresher jobs",
            "fresher hiring",
            "freshers hiring",
            "freshers recruitment",
            "graduate jobs",
            "graduate hiring",
            "graduate recruitment",
            "entry level jobs",
            "entry-level jobs",

            # Internships / work opportunities
            "internship",
            "internships",
            "intern hiring",
            "internship openings",
            "work from home jobs",
            "work-from-home jobs",
            "remote jobs",
            "walk-in interview",
            "walk-in interviews",
            "walk in interview",
            "walk-in recruitment",

            # Application / recruitment status
            "apply now",
            "applications open",
            "application deadline",
            "last date to apply",
            "apply online",
            "online application",
            "admit card",
            "exam notification",
            "exam date",
            "recruitment exam",
        }

        if query_phrase in (
            "cloud computing",
            "cloud-computing",
        ):
            search_phrases.append("cloud computing")
            search_phrases.append("cloud-computing")
            search_phrases.append("cloud")
            search_phrases.append("cloud services")
            search_phrases.append("cloud service")
            search_phrases.append("cloud infrastructure")
            search_phrases.append("cloud platform")
            search_phrases.append("cloud platforms")
            search_phrases.append("cloud technology")
            search_phrases.append("cloud technologies")

        if query_phrase in (
            "artificial intelligence",
            "artificial-intelligence",
        ):
            search_phrases.append("artificial intelligence")
            search_phrases.append("artificial-intelligence")
            search_phrases.append("ai technology")
            search_phrases.append("ai model")
            search_phrases.append("ai models")
            search_phrases.append("ai system")
            search_phrases.append("ai systems")
            search_phrases.append("ai tool")
            search_phrases.append("ai tools")
            search_phrases.append("ai-powered")
            search_phrases.append("ai powered")
        # ----------------------------------------------------
        # Find strongest title concept match
        # ----------------------------------------------------

        title_phrase_match = None
        description_phrase_match = None

        for phrase in search_phrases:

            if contains_term(
                title_lower,
                phrase,
            ):
                title_phrase_match = phrase
                break

        for phrase in search_phrases:

            if contains_term(
                description_lower,
                phrase,
            ):
                description_phrase_match = phrase
                break

        # ----------------------------------------------------
        # Individual word matches
        # ----------------------------------------------------

        title_matches = [
            word
            for word in words
            if contains_term(
                title_lower,
                word,
            )
        ]

        description_matches = [
            word
            for word in words
            if contains_term(
                description_lower,
                word,
            )
        ]

        source_matches = [
            word
            for word in words
            if contains_term(
                source_lower,
                word,
            )
        ]

         # ----------------------------------------------------
         # Search relevance filter
        # ----------------------------------------------------
        # Ignore articles with no meaningful match.
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Jobs-specific relevance filter
        # ----------------------------------------------------
        # Require a genuine employment, hiring, recruitment,
        # workforce, or job-market signal for Jobs searches.
        # ----------------------------------------------------

        if query_phrase in ("jobs", "job"):
            # ------------------------------------------------
            # JOBS ACTIONABILITY SCORING
            # ------------------------------------------------
            # Prefer actual opportunities that a job seeker can
            # apply for. Government + private-sector openings
            # are both supported.
            # ------------------------------------------------

            jobs_strong_terms = {
                # Direct openings
                "job opening",
                "job openings",
                "job vacancy",
                "job vacancies",
                "invites applications for posts",
                "invites applications for positions",
                "applications invited for posts",
                "applications invited for positions",
                "junior research fellow",
                "research fellow position",
                "research fellow positions",
                "vacancy",
                "vacancies",
                "recruitment",
                "recruitment notification",
                "recruitment notifications",
                "recruitment drive",
                "hiring drive",
                "hiring",
                "apply online",
                "apply now",
                "applications open",
                "application open",
                "last date to apply",
                "application deadline",

                # Government
                "government job",
                "government jobs",
                "govt job",
                "govt jobs",
                "government recruitment",
                "govt recruitment",
                "government vacancy",
                "government vacancies",
                "central government",
                "central govt",
                "state government",
                "state govt",

                # Recruitment bodies / exams
                "railway recruitment",
                "railway jobs",
                "bank recruitment",
                "banking jobs",
                "state psc",
                "psc recruitment",
                "police recruitment",
                "police jobs",
                "defence recruitment",
                "defense recruitment",
                "defence jobs",
                "defense jobs",
                "teacher recruitment",
                "teaching jobs",

                # Private hiring
                "private jobs",
                "private sector jobs",
                "private company hiring",
                "company hiring",
                "corporate hiring",
                "mass hiring",
                "bulk hiring",
                "campus hiring",
                "campus recruitment",
                "campus placement",
                "campus placements",

                # Freshers / graduates
                "fresher jobs",
                "fresher hiring",
                "freshers hiring",
                "freshers jobs",
                "freshers recruitment",
                "graduate jobs",
                "graduate hiring",
                "graduate recruitment",
                "entry level jobs",
                "entry-level jobs",

                # Other actionable opportunities
                "internship",
                "internships",
                "internship openings",
                "apprentice",
                "apprenticeship",
                "walk-in interview",
                "walk-in interviews",
                "walk in interview",
                "walk-in recruitment",
                "work from home jobs",
                "work-from-home jobs",
                "remote jobs",
            }

            jobs_supporting_terms = {
                "posts",
                "post",
                "positions",
                "position",
                "recruit",
                "recruits",
                "recruiting",
                "selected candidates",
                "selection process",
                "eligibility",
                "eligible candidates",
                "application form",
                "admit card",
                "exam notification",
                "exam date",
                "recruitment exam",
                "joining",
                "joining date",
                "salary",
                "stipend",
                "job role",
                "job roles",
                "career opportunity",
                "career opportunities",
            }

            jobs_general_terms = {
                "employment",
                "unemployment",
                "workforce",
                "job market",
                "labour market",
                "labor market",
                "employment rate",
                "unemployment rate",
                "salary trends",
                "salary trend",
                "hiring trends",
                "employment trends",
                "workforce trends",
                "campus hiring trends",
                "skills",
                "skill development",
            }

            jobs_exclusion_terms = {
                "unemployment rate",
                "employment rate",
                "industrial jobs cross",
                "job market outlook",
                "employment outlook",
                "workforce outlook",
                "hiring outlook",
                "salary trends",
                "salary trend",
                "employment trends",
                "workforce trends",
                "job market trends",
                "skills gap",
                "skills shortage",
                "skills required",
                "future of work",
                "future of jobs",
            }

            title_strong_hits = sum(
                1
                for term in jobs_strong_terms
                if contains_term(title_lower, term)
            )

            description_strong_hits = sum(
                1
                for term in jobs_strong_terms
                if contains_term(description_lower, term)
            )

            title_supporting_hits = sum(
                1
                for term in jobs_supporting_terms
                if contains_term(title_lower, term)
            )

            description_supporting_hits = sum(
                1
                for term in jobs_supporting_terms
                if contains_term(description_lower, term)
            )

            title_general_hits = sum(
                1
                for term in jobs_general_terms
                if contains_term(title_lower, term)
            )

            jobs_score = (
                title_strong_hits * 6
                + description_strong_hits * 2
                + title_supporting_hits * 2
                + description_supporting_hits
                - title_general_hits * 3
            )

            # Explicitly reject articles whose title is clearly
            # about employment trends rather than an opportunity.
            if any(
                contains_term(title_lower, term)
                for term in jobs_exclusion_terms
            ):
                continue

            # ------------------------------------------------
            # JOBS ACTIONABILITY FILTER
            # ------------------------------------------------
            # Keep direct opportunities that an Indian job seeker
            # or student can actually act on.
            # Reject general employment, salary, hiring-trend,
            # workforce and skills-analysis articles.
            # ------------------------------------------------

            jobs_actionable_terms = {
                "recruitment",
                "recruitment notification",
                "recruitment notifications",
                "recruitment drive",
                "hiring drive",
                "job opening",
                "job openings",
                "job vacancy",
                "job vacancies",
                "invites applications for posts",
                "invites applications for positions",
                "applications invited for posts",
                "applications invited for positions",
                "junior research fellow",
                "research fellow position",
                "research fellow positions",
                "vacancy",
                "vacancies",
                "apply online",
                "apply now",
                "applications open",
                "application open",
                "last date to apply",
                "application deadline",
                "internship",
                "internships",
                "apprentice",
                "apprenticeship",
                "walk-in",
                "walk in",
                "walk-in interview",
                "walk in interview",
                "freshers job",
                "freshers jobs",
                "fresher job",
                "fresher jobs",
                "fresher hiring",
                "freshers hiring",
                "graduate recruitment",
                "graduate hiring",
                "graduate jobs",
                "campus recruitment",
                "campus hiring",
                "government job",
                "government jobs",
                "govt job",
                "govt jobs",
                "government recruitment",
                "govt recruitment",
                "state psc",
                "railway recruitment",
                "railway jobs",
                "bank recruitment",
                "banking jobs",
                "police recruitment",
                "police jobs",
                "defence recruitment",
                "defense recruitment",
                "defence jobs",
                "defense jobs",
                "teacher recruitment",
                "teaching jobs",
                "research fellowship",
                "fellowship",
                "research opportunity",
                "research opportunities",
                "dissertation opportunity",
                "project position",
                "project associate",
                "young professional recruitment",
            }

            jobs_general_news_terms = {
                "employment statistics",
                "employment rate",
                "unemployment rate",
                "industrial jobs cross",
                "jobs cross",
                "job market",
                "job market trends",
                "employment trends",
                "workforce trends",
                "workforce outlook",
                "employment outlook",
                "salary trends",
                "salary trend",
                "campus salaries",
                "campus salary",
                "median salaries",
                "median salary",
                "skills and job readiness",
                "job readiness",
                "future of work",
                "future of jobs",
                "skills gap",
                "skills shortage",
                "talent powering",
                "hiring assessments",
                "hiring assessment",
                "cheating in hiring assessments",
                "curious case of hiring",
                "paychecks hit pause",
                "campus hiring towards",
                "campus hiring towards skills",
                "skilled talent powering",
                "talent powering india",
            }

            actionable_title_hits = sum(
                1
                for term in jobs_actionable_terms
                if contains_term(title_lower, term)
            )

            actionable_description_hits = sum(
                1
                for term in jobs_actionable_terms
                if contains_term(description_lower, term)
            )

            general_title_hits = sum(
                1
                for term in jobs_general_news_terms
                if contains_term(title_lower, term)
            )

            # Hard rejection for clearly general employment analysis.
            if general_title_hits > 0 and actionable_title_hits == 0:
                continue

            # A Jobs article must have an actionable signal.
            # Prefer the title, but allow a strong actionable
            # description when the title identifies the opportunity.
            if actionable_title_hits == 0 and actionable_description_hits == 0:
                continue

        if query_phrase in ("weather", "weather forecast"):
            weather_title_match = any(
                contains_term(title_lower, term)
                for term in weather_relevance_terms
            )

            if not weather_title_match:
                continue

            # Exclude clearly non-weather topics. A story must
            # not be primarily about sports, entertainment, politics,
            # business, technology, gaming, or another unrelated topic
            # merely because weather/rain is mentioned.
            if any(
                contains_term(
                    f"{title_lower} {description_lower}",
                    term,
                )
                for term in weather_search_non_topic_terms
            ):
                continue

        if query_phrase in ("cloud computing", "cloud-computing"):
            exact_cloud_phrase_in_title = (
                contains_term(title_lower, "cloud computing")
                or contains_term(title_lower, "cloud-computing")
            )

            cloud_in_title = contains_term(title_lower, "cloud")
            computing_in_title = contains_term(title_lower, "computing")
            cloud_in_description = contains_term(description_lower, "cloud")
            computing_in_description = contains_term(description_lower, "computing")

            split_cloud_context = (
                (cloud_in_title and computing_in_description)
                or (computing_in_title and cloud_in_description)
            )

            cloud_technology_context = (
                "aws",
                "azure",
                "google cloud",
                "gcp",
                "oracle cloud",
                "ibm cloud",
                "alibaba cloud",
                "iaas",
                "paas",
                "saas",
                "cloud infrastructure",
                "cloud service",
                "cloud services",
                "cloud platform",
                "cloud platforms",
                "cloud provider",
                "cloud providers",
                "cloud storage",
                "cloud technology",
                "cloud technologies",
                "serverless",
                "cloud-native",
                "cloud native",
                "data center",
                "data centre",
            )

            cloud_context_in_text = any(
                contains_term(
                    f"{title_lower} {description_lower}",
                    term,
                )
                for term in cloud_technology_context
            )

            if not (
                exact_cloud_phrase_in_title
                or split_cloud_context
                or (
                    cloud_in_title
                    and cloud_context_in_text
                )
            ):
                continue

        if len(words) > 1:
            # Multi-word searches:
            # Require the actual query words to be represented.
            # Search aliases are used for scoring, not eligibility.

            title_word_count = len(set(title_matches))
            description_word_count = len(set(description_matches))

            all_words_found = (
                len(
                    set(title_matches) | set(description_matches)
                )
                >= len(words)
            )

            exact_query_match = (
                contains_term(title_lower, query_phrase)
                or contains_term(description_lower, query_phrase)
            )

            split_query_match = (
                title_word_count >= 1
                and description_word_count >= 1
                and all_words_found
            )

            full_title_match = (
                title_word_count >= len(words)
            )

            full_description_match = (
                description_word_count >= len(words)
            )

            if not (
                exact_query_match
                or split_query_match
                or full_title_match
                or full_description_match
            ):
                continue

        else:
            # Single-word searches may match either the title
            # or the description. Title matches remain stronger
            # through the scoring logic below.
            if not title_phrase_match and not title_matches and not description_matches:
                continue

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = 0

        # Exact/concept phrase in TITLE.
        if title_phrase_match:
            score += 500

        # Exact/concept phrase in DESCRIPTION.
        if description_phrase_match:
            score += 40

        # Individual words in TITLE.
        score += len(title_matches) * 70

        # All query words in TITLE.
        if len(title_matches) == len(words):
            score += 200

        # Individual words in DESCRIPTION.
        score += len(description_matches) * 5

        # Source match.
        score += len(source_matches) * 2

        # ----------------------------------------------------
        # Strong preference for a title-level match.
        # ----------------------------------------------------

        if title_phrase_match:
            score += 250

        elif title_matches:
            score += 50

        else:
            # Description-only matches are intentionally weak.
            score -= 50

        scored.append(
            (
                score,
                item,
            )
        )

    if not scored:

        print(
            "SEARCH MATCHED: 0 articles"
        )

        return []

    # --------------------------------------------------------
    # Date helper
    # --------------------------------------------------------

    def article_date(
        item: dict,
    ):

        return str(
            item.get(
                "published_at",
                "",
            )
            or item.get(
                "published",
                "",
            )
            or item.get(
                "pubDate",
                "",
            )
            or ""
        )

    # --------------------------------------------------------
    # Sort newest/relevant first
    # --------------------------------------------------------

    scored.sort(
        key=lambda pair: (
            pair[0],
            article_date(
                pair[1]
            ),
        ),
        reverse=True,
    )

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique_items = []

    seen_urls = set()
    seen_titles = set()

    def normalize_news_url(url):
        """
        Normalize article URLs so tracking parameters and
        harmless URL differences do not create duplicates.
        """

        url = str(url or "").strip()

        if not url:
            return ""

        try:
            from urllib.parse import (
                urlsplit,
                urlunsplit,
                parse_qsl,
                urlencode,
            )

            parts = urlsplit(url)

            tracking_params = {
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "utm_id",
                "fbclid",
                "gclid",
                "mc_cid",
                "mc_eid",
                "ref",
                "referrer",
            }

            query_items = []

            for key, value in parse_qsl(
                parts.query,
                keep_blank_values=True,
            ):
                if key.lower() in tracking_params:
                    continue

                query_items.append(
                    (key, value)
                )

            clean_query = urlencode(
                query_items,
                doseq=True,
            )

            clean_path = parts.path.rstrip("/")

            return urlunsplit(
                (
                    parts.scheme.lower(),
                    parts.netloc.lower(),
                    clean_path,
                    clean_query,
                    "",
                )
            )

        except Exception:
            return url.split("#", 1)[0].rstrip("/")


    def normalize_news_title(title):
        """
        Normalize titles for reliable duplicate detection.
        """

        title = str(title or "").lower()

        title = re.sub(
            r"<[^>]+>",
            " ",
            title,
        )

        title = re.sub(
            r"[^a-z0-9\s]",
            " ",
            title,
        )

        title = re.sub(
            r"\s+",
            " ",
            title,
        ).strip()

        return title


    def title_tokens(title):
        """
        Return meaningful words from a normalized title.
        """

        stop_words = {
            "a",
            "an",
            "the",
            "and",
            "or",
            "of",
            "to",
            "in",
            "on",
            "for",
            "with",
            "by",
            "from",
            "at",
            "is",
            "are",
            "was",
            "were",
            "has",
            "have",
            "new",
        }

        return {
            word
            for word in normalize_news_title(title).split()
            if len(word) >= 3
            and word not in stop_words
        }


    def similar_news_title(title_a, title_b):
        """
        Detect near-duplicate headlines using token overlap.

        This intentionally requires substantial overlap so that
        unrelated articles are not accidentally removed.
        """

        tokens_a = title_tokens(title_a)
        tokens_b = title_tokens(title_b)

        if not tokens_a or not tokens_b:
            return False

        intersection = tokens_a & tokens_b

        smaller_set = min(
            len(tokens_a),
            len(tokens_b),
        )

        if smaller_set == 0:
            return False

        overlap = (
            len(intersection) / smaller_set
        )

        return overlap >= 0.80


    for score, item in scored:

        url = str(
            item.get(
                "url",
                "",
            )
            or item.get(
                "link",
                "",
            )
            or ""
        ).strip()

        title = str(
            item.get(
                "title",
                "",
            )
            or ""
        ).strip()

        normalized_url = normalize_news_url(
            url
        )

        title_key = normalize_news_title(
            title
        )

        # Exact/normalized URL duplicate.
        if (
            normalized_url
            and normalized_url in seen_urls
        ):
            continue

        # Exact normalized title duplicate.
        if (
            title_key
            and title_key in seen_titles
        ):
            continue

        # Near-duplicate headline detection.
        near_duplicate = False

        if title_key:

            for existing_title in seen_titles:

                if similar_news_title(
                    title_key,
                    existing_title,
                ):
                    near_duplicate = True
                    break

        if near_duplicate:
            continue

        if normalized_url:
            seen_urls.add(
                normalized_url
            )

        if title_key:
            seen_titles.add(
                title_key
            )

        unique_items.append(
            (
                score,
                item,
            )
        )

    # --------------------------------------------------------
    # Group by publisher/source
    # --------------------------------------------------------


    publisher_groups = {}

    for score, item in unique_items:

        source = str(
            item.get(
                "source",
                "",
            )
            or ""
        ).strip()

        if not source:

            source = str(
                item.get(
                    "_feed_url",
                    "",
                )
                or ""
            ).strip()

        if not source:
            source = "Unknown Source"

        source_key = re.sub(
            r"\s+",
            " ",
            source.lower(),
        ).strip()

        if source_key not in publisher_groups:

            publisher_groups[
                source_key
            ] = {
                "name": source,
                "articles": [],
            }

        publisher_groups[
            source_key
        ]["articles"].append(
            (
                score,
                item,
            )
        )

    # --------------------------------------------------------
    # Sort every source
    # --------------------------------------------------------

    for group in publisher_groups.values():

        group["articles"].sort(
            key=lambda pair: (
                pair[0],
                article_date(
                    pair[1]
                ),
            ),
            reverse=True,
        )

    # --------------------------------------------------------
    # FINAL STARTUP QUALITY FILTER
    #
    # IMPORTANT:
    # This runs AFTER RSS fallback and BEFORE source
    # balancing, so unwanted fallback articles cannot
    # re-enter the final 20.
    # --------------------------------------------------------

    if category == "startups":

        startup_bad_final = [
            "gold price",
            "silver price",
            "gold and silver",
            "stock market",
            "nifty",
            "sensex",
            "share price",
            "shares list",
            "shares listed",
            "copyright dispute",
            "contempt action",
            "nclt approves merger",
            "merger gets nclt approval",
            "merger approval",
            "preferential issue",
            "bulk deal",
            "real estate growth",
            "future of indian housing",
            "housing market",
            "teams across the globe",
            "team across the globe",
            "kahlil gibran",
            "quotes that",
            "chief marketing officer",
            "marketing officer",
            "marketing appointment",
            "jewellery collection",
            "jewelry collection",
            "apparel collection",
            "retail india news:",
            "retail news:",
            "d2c business",
            "open-source foundations",
            "african funding",
            "four-year bootstrapping",
        ]

        startup_good_final = [
            "startup funding",
            "startup investment",
            "startup acquisition",
            "startup ecosystem",
            "startup founder",
            "startup founders",
            "startup raises",
            "startup raised",
            "startup funding round",
            "startup funding",
            "startup venture",
            "startup capital",
            "startup ipo",
            "startup launches",
            "startup launch",
            "startup expands",
            "startup expansion",
            "startup valuation",
            "startup growth",
            "startup company",
            "startup platform",
            "startup technology",
            "startup fintech",
            "startup healthtech",
            "startup edtech",
            "startup saas",
            "startup deeptech",
            "startup deep-tech",
            "startup agritech",
            "startup foodtech",
            "startup proptech",
            "startup unicorn",
            "startup accelerator",
            "startup incubator",
            "startup accelerator",
            "funding round",
            "funding led by",
            "funding to expand",
            "raises $",
            "raises ₹",
            "raises rs",
            "raises in funding",
            "raised $",
            "raised ₹",
            "raised rs",
            "raised in funding",
            "seed round",
            "seed funding",
            "pre-seed",
            "pre seed",
            "pre-series a",
            "pre-series b",
            "series a",
            "series b",
            "series c",
            "venture capital",
            "venture fund",
            "vc fund",
            "angel investor",
            "angel funding",
            "unicorn startup",
        ]

        for source_key, group in publisher_groups.items():

            clean_articles = []

            for article in group["articles"]:

                title = (
                    article.get("title") or ""
                ).strip().lower()

                if any(
                    bad in title
                    for bad in startup_bad_final
                ):
                    continue

                # ------------------------------------------------
                # Require a genuine startup/company context.
                # A generic story mentioning "founders",
                # "funding", or "deep-tech" is not enough.
                # ------------------------------------------------

                startup_context = [
                    "startup",
                    "startups",
                    "start-up",
                    "start-ups",
                    "startup company",
                    "startup funding",
                    "startup investment",
                    "startup founder",
                    "startup founders",
                    "startup raises",
                    "startup raised",
                    "startup ecosystem",
                    "startup acquisition",
                    "startup ipo",
                    "startup launch",
                    "startup launches",
                    "startup expansion",
                    "startup valuation",
                    "startup unicorn",
                    "seed round",
                    "seed funding",
                    "pre-seed",
                    "pre-series a",
                    "pre-series b",
                    "series a",
                    "series b",
                    "series c",
                    "venture capital",
                    "venture fund",
                    "vc fund",
                    "angel investor",
                    "angel funding",
                ]

                has_good_term = any(
                    good in title
                    for good in startup_good_final
                )

                has_startup_context = any(
                    context in title
                    for context in startup_context
                )

                if not has_good_term:
                    continue

                if not has_startup_context:
                    continue

                clean_articles.append(article)

            group["articles"] = clean_articles

        # Remove sources that no longer have enough
        # clean Startup articles.
        publisher_groups = {
            source_key: group
            for source_key, group in publisher_groups.items()
            if len(group["articles"]) >= 4
        }

        print(
            "STARTUP FINAL QUALITY FILTER:",
            sum(
                len(group["articles"])
                for group in publisher_groups.values()
            ),
            "clean candidates"
        )

    # --------------------------------------------------------
    # FINAL JOBS QUALITY FILTER
    #
    # Only allow genuinely actionable job/opportunity articles
    # into the final source-selection stage.
    #
    # General employment, salary, workforce, skills, and hiring
    # analysis without an actual opportunity are excluded.
    # --------------------------------------------------------

    print("DEBUG FINAL FILTER CATEGORY:", repr(category))

    if category == "jobs":

        final_jobs_actionable_terms = {
            "recruitment",
            "recruitment notification",
            "recruitment drive",
            "hiring drive",
            "job opening",
            "job openings",
            "job vacancy",
            "job vacancies",
            "invites applications for posts",
            "invites applications for positions",
            "applications invited for posts",
            "applications invited for positions",
            "junior research fellow",
            "research fellow position",
            "research fellow positions",
            "vacancy",
            "vacancies",
            "apply online",
            "apply now",
            "applications open",
            "application open",
            "last date to apply",
            "application deadline",
            "internship",
            "internships",
            "apprentice",
            "apprenticeship",
            "walk-in",
            "walk in",
            "walk-in interview",
            "walk in interview",
            "freshers job",
            "freshers jobs",
            "fresher job",
            "fresher jobs",
            "fresher hiring",
            "freshers hiring",
            "graduate recruitment",
            "graduate hiring",
            "graduate jobs",
            "campus recruitment",
            "campus hiring",
            "government job",
            "government jobs",
            "govt job",
            "govt jobs",
            "government recruitment",
            "govt recruitment",
            "railway recruitment",
            "railway jobs",
            "bank recruitment",
            "banking jobs",
            "police recruitment",
            "police jobs",
            "defence recruitment",
            "defense recruitment",
            "defence jobs",
            "defense jobs",
            "teacher recruitment",
            "teaching jobs",
            "research fellowship",
            "fellowship",
            "research opportunity",
            "research opportunities",
            "dissertation opportunity",
            "project position",
            "project associate",
            "young professional recruitment",
        }

        final_jobs_general_terms = {
            "employment statistics",
            "employment rate",
            "unemployment rate",
            "industrial jobs cross",
            "jobs cross",
            "job market",
            "job market trends",
            "employment trends",
            "workforce trends",
            "workforce outlook",
            "employment outlook",
            "salary trends",
            "salary trend",
            "campus salaries",
            "campus salary",
            "median salaries",
            "median salary",
            "skills and job readiness",
            "job readiness",
            "future of work",
            "future of jobs",
            "skills gap",
            "skills shortage",
            "talent powering",
            "hiring assessments",
            "hiring assessment",
            "cheating in hiring assessments",
            "curious case of hiring",
            "paychecks hit pause",
            "campus hiring towards",
            "campus hiring towards skills",
            "skilled talent powering",
            "talent powering india",
        }

        filtered_job_groups = {}

        for source_key, group in publisher_groups.items():

            clean_articles = []

            for score, item in group["articles"]:

                title = str(
                    item.get("title") or ""
                ).strip().lower()

                description = str(
                    item.get("description")
                    or item.get("summary")
                    or ""
                ).strip().lower()

                text = f"{title} {description}"

                actionable_title = any(
                    term in title
                    for term in final_jobs_actionable_terms
                ) or bool(JOB_POSTING_PATTERN.search(title)) or is_active_job_listing(item)

                actionable_description = any(
                    term in description
                    for term in final_jobs_actionable_terms
                ) or bool(JOB_POSTING_PATTERN.search(description)) or is_active_job_listing(item)

                general_title = any(
                    term in title
                    for term in final_jobs_general_terms
                )

                # General employment/news analysis is not a
                # Jobs article unless the headline contains a
                # concrete opportunity signal.
                #
                # Words such as "hiring", "campus hiring", or
                # "talent" can describe employment trends without
                # offering an actual position, so they are not
                # sufficient by themselves.

                strong_job_opportunity_terms = {
                    "recruitment",
                    "recruitment notification",
                    "recruitment drive",
                    "job opening",
                    "job openings",
                    "job vacancy",
                    "job vacancies",
                    "invites applications for posts",
                    "invites applications for positions",
                    "applications invited for posts",
                    "applications invited for positions",
                    "junior research fellow",
                    "research fellow position",
                    "research fellow positions",
                    "vacancy",
                    "vacancies",
                    "apply online",
                    "apply now",
                    "applications open",
                    "application open",
                    "last date to apply",
                    "application deadline",
                    "internship",
                    "internships",
                    "apprentice",
                    "apprenticeship",
                    "walk-in interview",
                    "walk in interview",
                    "freshers job",
                    "freshers jobs",
                    "fresher job",
                    "fresher jobs",
                    "freshers recruitment",
                    "fresher recruitment",
                    "graduate recruitment",
                    "graduate jobs",
                    "campus recruitment",
                    "government job",
                    "government jobs",
                    "govt job",
                    "govt jobs",
                    "government recruitment",
                    "govt recruitment",
                    "railway recruitment",
                    "railway jobs",
                    "bank recruitment",
                    "banking jobs",
                    "police recruitment",
                    "police jobs",
                    "defence recruitment",
                    "defense recruitment",
                    "defence jobs",
                    "defense jobs",
                    "teacher recruitment",
                    "teaching jobs",
                    "research fellowship",
                    "fellowship",
                    "research opportunity",
                    "research opportunities",
                    "dissertation opportunity",
                    "project position",
                    "project associate",
                    "young professional recruitment",
                }

                strong_title_hits = sum(
                    1
                    for term in strong_job_opportunity_terms
                    if contains_term(title, term)
                ) + int(bool(JOB_POSTING_PATTERN.search(title))) + int(is_active_job_listing(item))

                strong_description_hits = sum(
                    1
                    for term in strong_job_opportunity_terms
                    if contains_term(description, term)
                ) + int(bool(JOB_POSTING_PATTERN.search(description))) + int(is_active_job_listing(item))

                # General employment analysis is rejected unless
                # the headline itself clearly identifies an actual
                # opportunity.
                if general_title and strong_title_hits == 0:
                    continue

                # Require a concrete opportunity signal somewhere
                # in the article.
                if (
                    strong_title_hits == 0
                    and strong_description_hits == 0
                ):
                    continue

                clean_articles.append(
                    (
                        score,
                        item,
                    )
                )

            if clean_articles:
                clean_group = dict(group)
                clean_group["articles"] = clean_articles
                filtered_job_groups[source_key] = clean_group

        publisher_groups = filtered_job_groups

        print(
            "FINAL JOBS QUALITY FILTER:",
            sum(
                len(group["articles"])
                for group in publisher_groups.values()
            ),
            "actionable candidates"
        )

    # --------------------------------------------------------
    # Sort sources by article availability
    # --------------------------------------------------------

    source_list = list(
        publisher_groups.items()
    )

    source_list.sort(
        key=lambda pair: len(
            pair[1]["articles"]
        ),
        reverse=True,
    )

    # --------------------------------------------------------
    # Select sources
    #
    # Prefer up to 4 strong sources.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # STARTUP SOURCE SELECTION
    #
    # For startups, remove generic retail/business articles
    # before choosing the 4 balanced sources.
    # --------------------------------------------------------

    if category == "startups":

        startup_bad = [
            "gold price",
            "silver price",
            "gold and silver",
            "stock market",
            "nifty",
            "sensex",
            "share price",
            "shares list",
            "shares listed",
            "copyright dispute",
            "contempt action",
            "nclt approves merger",
            "merger gets nclt approval",
            "merger approval",
            "preferential issue",
            "bulk deal",
            "real estate growth",
            "future of indian housing",
            "housing market",
            "teams across the globe",
            "team across the globe",
            "kahlil gibran",
            "quotes that",
            "chief marketing officer",
            "marketing officer",
            "marketing appointment",
            "jewellery collection",
            "jewelry collection",
            "apparel collection",
            "retail india news:",
            "retail news:",
            "d2c business",
            "open-source foundations",
            "african funding",
            "four-year bootstrapping",
        ]

        startup_good = [
            "startup",
            "startups",
            "start-up",
            "start-ups",
            "startup funding",
            "startup investment",
            "startup acquisition",
            "startup ecosystem",
            "startup founder",
            "startup founders",
            "startup raises",
            "startup raised",
            "startup fund",
            "startup ipo",
            "funding",
            "fundraise",
            "fundraising",
            "raises ",
            " raised ",
            "seed round",
            "seed funding",
            "pre-seed",
            "series a",
            "series b",
            "series c",
            "venture capital",
            "venture fund",
            "vc fund",
            "angel investor",
            "angel funding",
            "unicorn",
            "founder",
            "co-founder",
            "cofounder",
            "deeptech",
            "deep-tech",
        ]

        startup_source_list = []

        for source_key, group in source_list:

            clean_articles = []

            for article in group["articles"]:

                title = (
                    article.get("title") or ""
                ).strip().lower()

                if any(
                    bad in title
                    for bad in startup_bad
                ):
                    continue

                if not any(
                    good in title
                    for good in startup_good
                ):
                    continue

                clean_articles.append(article)

            if len(clean_articles) >= 4:

                clean_group = dict(group)
                clean_group["articles"] = clean_articles

                startup_source_list.append(
                    (
                        source_key,
                        clean_group
                    )
                )

        startup_source_list.sort(
            key=lambda pair: len(
                pair[1]["articles"]
            ),
            reverse=True,
        )

        print(
            "STARTUP CLEAN SOURCES:"
        )

        for source_key, group in startup_source_list:
            print(
                " ",
                source_key,
                len(group["articles"])
            )

        # Prefer sources with at least 5 clean
        # articles so 4 × 5 = exactly 20.
        strong_sources = [
            pair
            for pair in startup_source_list
            if len(
                pair[1]["articles"]
            ) >= 5
        ]

        if len(strong_sources) >= 4:

            selected_sources = (
                strong_sources[:4]
            )

        else:

            selected_sources = (
                startup_source_list[:4]
            )

    elif category == "weather":

        # --------------------------------------------------------
        # WEATHER SOURCE SELECTION
        #
        # Prefer the dedicated Weather RSS feeds over publishers
        # discovered through the Google News fallback feed.
        # Google News remains available as a fallback.
        # --------------------------------------------------------

        weather_feed_priority = [
            "indianexpress.com/section/weather/feed/",
            "odishatv.in/weather/feed",
            "timesofindia.indiatimes.com/rssfeeds/2647163.cms",
        ]

        weather_source_list = []

        for source_key, group in source_list:
            feed_urls = {
                str(
                    item.get("_feed_url", "")
                    or ""
                ).lower()
                for score, item in group["articles"]
            }

            priority = 999

            for index, feed_pattern in enumerate(weather_feed_priority):
                if any(feed_pattern in feed_url for feed_url in feed_urls):
                    priority = index
                    break

            weather_source_list.append(
                (
                    priority,
                    -len(group["articles"]),
                    source_key,
                    group,
                )
            )

        weather_source_list.sort()

        selected_sources = [
            (source_key, group)
            for priority, count, source_key, group
            in weather_source_list[:6]
        ]

        print("WEATHER SOURCE PRIORITY:")

        for source_key, group in selected_sources:
            print(
                " ",
                group.get("name", source_key),
                "articles=",
                len(group["articles"])
            )

    else:

        # --------------------------------------------------------
        # JOBS SOURCE SELECTION
        #
        # Prefer sources that contain more concrete job/opportunity
        # headlines rather than selecting sources only by article
        # count. This prevents general employment-analysis sources
        # from dominating the Jobs category.
        # --------------------------------------------------------

        if category == "jobs":

            jobs_source_terms = {
                "recruitment",
                "recruitment notification",
                "recruitment drive",
                "job opening",
                "job openings",
                "job vacancy",
                "job vacancies",
                "invites applications for posts",
                "invites applications for positions",
                "applications invited for posts",
                "applications invited for positions",
                "junior research fellow",
                "research fellow position",
                "research fellow positions",
                "vacancy",
                "vacancies",
                "apply online",
                "apply now",
                "application deadline",
                "last date to apply",
                "internship",
                "internships",
                "apprentice",
                "apprenticeship",
                "walk-in interview",
                "walk in interview",
                "freshers job",
                "freshers jobs",
                "fresher job",
                "fresher jobs",
                "freshers recruitment",
                "fresher recruitment",
                "graduate recruitment",
                "graduate jobs",
                "campus recruitment",
                "government job",
                "government jobs",
                "govt job",
                "govt jobs",
                "government recruitment",
                "govt recruitment",
                "railway recruitment",
                "railway jobs",
                "bank recruitment",
                "banking jobs",
                "police recruitment",
                "police jobs",
                "defence recruitment",
                "defense recruitment",
                "defence jobs",
                "defense jobs",
                "teacher recruitment",
                "teaching jobs",
                "research fellowship",
                "research opportunity",
                "project associate",
                "young professional recruitment",
            }

            def jobs_source_score(pair):

                source_key, group = pair

                actionable_count = 0

                for score, item in group["articles"]:

                    title = str(
                        item.get("title") or ""
                    ).strip().lower()

                    if is_active_job_listing(item) or any(
                        term in title
                        for term in jobs_source_terms
                    ):
                        actionable_count += 1

                return (
                    actionable_count,
                    len(group["articles"])
                )

            source_list.sort(
                key=jobs_source_score,
                reverse=True,
            )

            print("JOBS SOURCE PRIORITY:")

            for source_key, group in source_list[:8]:

                print(
                    " ",
                    group.get("name", source_key),
                    "actionable=",
                    jobs_source_score(
                        (
                            source_key,
                            group
                        )
                    )[0],
                    "total=",
                    len(group["articles"])
                )

            strong_sources = [
                pair
                for pair in source_list
                if jobs_source_score(pair)[0] >= 4
            ]

            if len(strong_sources) >= 4:

                selected_sources = (
                    strong_sources[:4]
                )

            else:

                selected_sources = (
                    source_list[:4]
                )

        else:

            strong_sources = [
                pair
                for pair in source_list
                if len(
                    pair[1]["articles"]
                ) >= 5
            ]

            if len(strong_sources) >= 4:

                selected_sources = (
                    strong_sources[:4]
                )

            else:

                selected_sources = (
                    source_list[:4]
                )

    # --------------------------------------------------------
    # Build final list
    #
    # SEARCH MODE:
    # Preserve relevance order.
    #
    # CATEGORY MODE:
    # Keep source-balanced round-robin selection.
    # --------------------------------------------------------

    if not category_mode:

        # Search results must remain ordered by relevance.
        final_items = unique_items[:20]

        print(
            "SEARCH RELEVANCE ORDER: PRESERVED"
        )

    else:

        final_items = []

        positions = {
            source_key: 0
            for source_key, group
            in selected_sources
        }

        # First target = 4/source
        for round_number in range(4):

            for source_key, group in selected_sources:

                if len(final_items) >= 20:
                    break

                articles = group[
                    "articles"
                ]

                position = positions[
                    source_key
                ]

                if position >= len(
                    articles
                ):
                    continue

                final_items.append(
                    articles[position]
                )

                positions[
                    source_key
                ] += 1

        # Second target = 5/source
        for source_key, group in selected_sources:

            if len(final_items) >= 20:
                break

            articles = group[
                "articles"
            ]

            position = positions[
                source_key
            ]

            if position < len(
                articles
            ):

                final_items.append(
                    articles[position]
                )

                positions[
                    source_key
                ] += 1

        # Fill remaining slots if necessary.
        while len(final_items) < 20:

            added = False

            for source_key, group in selected_sources:

                if len(final_items) >= 20:
                    break

                articles = group[
                    "articles"
                ]

                position = positions[
                    source_key
                ]

                if position >= len(
                    articles
                ):
                    continue

                final_items.append(
                    articles[position]
                )

                positions[
                    source_key
                ] += 1

                added = True

            if not added:
                break

        # Use other sources only if still below 20.
        if len(final_items) < 20:

            selected_keys = {
                source_key
                for source_key, group
                in selected_sources
            }

            for source_key, group in source_list:

                if len(final_items) >= 20:
                    break

                if source_key in selected_keys:
                    continue

                for score, item in group[
                    "articles"
                ]:

                    if len(final_items) >= 20:
                        break

                    final_items.append(
                        (
                            score,
                            item,
                        )
                    )

    # --------------------------------------------------------
    # FAST ARTICLE PROCESSING
    #
    # Only process enough publisher pages to obtain the
    # required valid articles.
    #
    # Category target:
    #   20 articles
    #   4 sources
    #   5 articles per source
    #
    # Description requirement:
    #   minimum 3 genuine source-derived sentences
    #
    # NDTV:
    #   Skip publisher requests because NDTV currently returns
    #   HTTP 403 and its RSS description is too short.
    # --------------------------------------------------------

    final_articles = []

    def valid_description(article):
        """
        Normalize every article description into a concise,
        understandable continuous paragraph.

        Final website target:
        - enough real text for approximately 2 lines
        - maximum approximately 4 lines
        - source-derived information only
        - no bullets
        - no invented information
        """

        description = str(
            article.get(
                "description",
                "",
            )
            or ""
        ).strip()

        if not description:
            return False

        # Remove bullet markers.
        description = re.sub(
            r"(?m)^\s*[\*\u2022\u25E6\u25AA\u25AB\-]+\s*",
            "",
            description,
        )

        # Convert all whitespace to one continuous paragraph.
        description = re.sub(
            r"\s+",
            " ",
            description,
        ).strip()

        if not description:
            return False

        # ----------------------------------------------------
        # Remove obvious duplicated text.
        # ----------------------------------------------------

        sentence_parts = (
            split_sentences(description, latest_mode=True)
            if category == "latest"
            else re.split(r"(?<=[.!?])\s+", description)
        )
        sentences = [s.strip() for s in sentence_parts if s.strip()]

        cleaned = []

        for sentence in sentences:

            if not sentence:
                continue

            key = normalize_text(sentence)

            if not key:
                continue

            duplicate = False

            for existing in cleaned:

                existing_key = normalize_text(
                    existing
                )

                if key == existing_key:
                    duplicate = True
                    break

                a = set(existing_key.split())
                b = set(key.split())

                if a and b:
                    similarity = (
                        len(a & b)
                        / max(len(a | b), 1)
                    )

                    if similarity >= 0.88:
                        duplicate = True
                        break

            if not duplicate:
                cleaned.append(sentence)

        if cleaned:
            description = " ".join(cleaned)

        # ----------------------------------------------------
        # TARGET LENGTH
        #
        # Around 320-520 source characters fills 2-4 lines
        # across the full-width article cards on desktop.
        #
        # CSS still enforces the absolute 4-line visual limit.
        # ----------------------------------------------------

        MIN_CHARS = 120 if category == "weather" else 320
        MIN_WORDS = 12 if category == "weather" else 20
        MAX_CHARS = 900 if category == "latest" else 360

        # If source text is already within the useful range,
        # keep it exactly as source-derived text.
        if len(description) > MAX_CHARS:

            candidate = description[:MAX_CHARS]

            # Prefer a complete sentence.
            sentence_end = max(
                candidate.rfind(". "),
                candidate.rfind("! "),
                candidate.rfind("? "),
            )

            if sentence_end >= MIN_CHARS:
                description = candidate[
                    :sentence_end + 1
                ].strip()

            elif category != "latest":
                # If no sentence ends inside the target range,
                # cut at a word boundary without inventing text.
                description = (
                    candidate
                    .rsplit(" ", 1)[0]
                    .strip()
                    + "..."
                )

        # ----------------------------------------------------
        # Do NOT invent content for short descriptions.
        #
        # Short source descriptions are accepted only when
        # the actual article extractor has already supplied
        # all available source content.
        #
        # NDTV's real-page extractor above should normally
        # provide enough article text.
        # ----------------------------------------------------

        if len(description) < MIN_CHARS:

            # Try to use additional source-derived text that
            # may already be present in the article object.
            extra_candidates = []

            for key in (
                "content",
                "full_text",
                "article_text",
                "body",
                "raw_description",
            ):

                value = article.get(key)

                if value:
                    extra_candidates.append(
                        str(value)
                    )

            for extra in extra_candidates:

                extra = re.sub(
                    r"(?m)^\s*[\*\u2022\u25E6\u25AA\u25AB\-]+\s*",
                    "",
                    extra,
                )

                extra = re.sub(
                    r"\s+",
                    " ",
                    extra,
                ).strip()

                if not extra:
                    continue

                combined = (
                    description
                    + " "
                    + extra
                )

                combined = re.sub(
                    r"\s+",
                    " ",
                    combined,
                ).strip()

                if len(combined) > len(description):
                    description = combined

                if len(description) >= MIN_CHARS:
                    break

        # Rebuild the card description from complete source sentences only.
        # CSS line clamping can otherwise hide the end of a sentence.
        complete_sentences = []
        sentence_parts = (
            split_sentences(description, latest_mode=True)
            if category == "latest"
            else re.split(r"(?<=[.!?])\s+", description)
        )
        for sentence in sentence_parts:
            sentence = sentence.strip()
            if (
                not sentence
                or re.search(r"(?:\.{2,}|…)+\s*[\"'’”)]*$", sentence)
                or not re.search(r"[.!?][\"'’”)]*$", sentence)
            ):
                continue
            sentence_limit = 500 if category == "latest" else 360
            if len(sentence) > sentence_limit:
                continue
            candidate = " ".join(complete_sentences + [sentence])
            if len(candidate) > MAX_CHARS:
                break
            complete_sentences.append(sentence)

        description = " ".join(complete_sentences)

        article["description"] = description

        # A description must contain meaningful real text.
        if (
            len(description) < MIN_CHARS
            or len(description.split()) < MIN_WORDS
        ):
            return False

        return True

    def process_source_articles(
        source_key,
        group,
    ):
        source_name = str(
            group.get(
                "name",
                source_key,
            )
            or source_key
        ).strip()

        # --------------------------------------------------------
        # NDTV + OLLAMA DESCRIPTION EXPANSION
        #
        # ONLY NDTV uses Ollama for description expansion.
        # Other news sources are completely unchanged.
        #
        # Ollama may ONLY use the NDTV title and RSS description.
        # It must not invent facts.
        # --------------------------------------------------------

        if "ndtv" in source_name.lower():

            valid_articles = []

            articles = group.get(
                "articles",
                [],
            )

            def expand_ndtv_description(
                title,
                rss_description,
            ):

                title = str(title or "").strip()
                rss_description = str(
                    rss_description or ""
                ).strip()

                if not title or not rss_description:
                    return rss_description

                prompt = f"""
You are rewriting a news article description.

SOURCE:
Publisher: NDTV

ARTICLE TITLE:
{title}

NDTV SOURCE DESCRIPTION:
{rss_description}

TASK:
Rewrite the supplied NDTV information into ONE continuous,
natural paragraph of 55 to 80 words.
The output MUST contain at least 35 words.
If the supplied information is short, carefully expand the wording by restating and clarifying only the information provided, without adding any new facts.

The paragraph should be detailed enough to visually occupy
approximately 3 to 4 lines on a normal news website.

STRICT FACT RULES:

- Use ONLY information contained in the article title and
  NDTV source description above.
- Do NOT use outside knowledge.
- Do NOT search the internet.
- Do NOT invent facts.
- Do NOT invent names.
- Do NOT invent locations.
- Do NOT invent dates.
- Do NOT invent numbers.
- Do NOT invent causes.
- Do NOT invent injuries.
- Do NOT invent consequences.
- Do NOT invent police or government actions.
- Do NOT invent quotes.
- Do NOT make predictions.
- Do NOT add information simply to make the paragraph longer.
- Preserve allegations as allegations.
- Do not convert an allegation into a confirmed fact.

You may:
- rewrite the wording,
- combine the supplied information,
- clarify the meaning,
- improve readability,
- explain the same supplied information in natural language.

If the supplied information is short, expand its wording naturally
without adding any new factual information.

OUTPUT RULES:
- Return ONLY the paragraph.
- No heading.
- No bullets.
- No numbering.
- No quotation marks.
- No "Summary:" label.
- No commentary.
"""

                try:

                    payload = {
                        "model": "qwen2.5:3b",
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.1
                        }
                    }

                    request = Request(
                        "http://127.0.0.1:11434/api/generate",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json"
                        },
                        method="POST",
                    )

                    with urlopen(
                        request,
                        timeout=60,
                    ) as response:

                        result = json.loads(
                            response.read().decode(
                                "utf-8",
                                errors="ignore",
                            )
                        )

                    expanded = str(
                        result.get(
                            "response",
                            "",
                        )
                        or ""
                    ).strip()

                    expanded = re.sub(
                        r"\s+",
                        " ",
                        expanded,
                    ).strip()

                    # Remove accidental formatting.
                    expanded = re.sub(
                        r"^```(?:text|markdown)?\s*",
                        "",
                        expanded,
                        flags=re.IGNORECASE,
                    )

                    expanded = re.sub(
                        r"\s*```$",
                        "",
                        expanded,
                    ).strip()

                    # Reject obviously bad Ollama output.
                    if not expanded:
                        return rss_description

                    if len(expanded.split()) < 35:
                        return rss_description

                    if len(expanded.split()) > 95:

                        words = expanded.split()

                        expanded = " ".join(
                            words[:95]
                        )

                        # Finish at the last complete sentence
                        last_period = expanded.rfind(".")

                        if last_period >= 120:
                            expanded = expanded[
                                :last_period + 1
                            ]

                    return expanded

                except Exception as error:

                    print(
                        "NDTV OLLAMA DESCRIPTION ERROR:",
                        error,
                    )

                    return rss_description

            for score, item in articles:

                if len(valid_articles) >= 5:
                    break

                article = dict(item)

                article.pop(
                    "_feed_url",
                    None,
                )

                title = str(
                    article.get(
                        "title",
                        "",
                    )
                    or ""
                ).strip()

                rss_description = str(
                    article.get(
                        "description",
                        "",
                    )
                    or ""
                ).strip()

                if not title or not rss_description:
                    continue

                rss_description = re.sub(
                    r"\s+",
                    " ",
                    rss_description,
                ).strip()

                # Only expand short NDTV descriptions.
                # Longer NDTV descriptions are kept as supplied.
                if len(rss_description.split()) < 35:

                    print(
                        "NDTV SHORT RSS -> OLLAMA:",
                        title,
                    )

                    description = expand_ndtv_description(
                        title,
                        rss_description,
                    )

                else:

                    description = rss_description

                description = re.sub(
                    r"\s+",
                    " ",
                    description,
                ).strip()

                if not description:
                    continue

                article["description"] = description

                # ------------------------------------------------
                # NDTV SPECIAL VALIDATION
                #
                # Do NOT run the Ollama-expanded NDTV description
                # through the generic description validator.
                #
                # The generic validator is designed for publisher
                # descriptions and can reject an Ollama rewrite,
                # which previously caused the code to restore the
                # original one-line NDTV RSS description.
                # ------------------------------------------------

                word_count = len(
                    article["description"].split()
                )

                if word_count < 35:

                    print(
                        "SKIP NDTV - OLLAMA OUTPUT TOO SHORT:",
                        word_count,
                        "words",
                    )

                    continue

                # Make sure NDTV remains a single paragraph.
                article["description"] = re.sub(
                    r"\s+",
                    " ",
                    article["description"],
                ).strip()

                # Keep NDTV cards within the same complete-sentence display
                # budget as other publishers. This only formats returned
                # text; it does not change the existing Ollama prompt/rules.
                ndtv_sentences = []
                for sentence in re.split(
                    r"(?<=[.!?])\s+",
                    article["description"],
                ):
                    sentence = sentence.strip()
                    if (
                        not sentence
                        or re.search(r"(?:\.{2,}|…)+\s*[\"'’”)]*$", sentence)
                        or not re.search(r"[.!?][\"'’”)]*$", sentence)
                        or len(sentence) > 520
                    ):
                        continue
                    candidate = " ".join(ndtv_sentences + [sentence])
                    if len(candidate) > 520:
                        break
                    ndtv_sentences.append(sentence)

                article["description"] = " ".join(ndtv_sentences)
                if (
                    len(article["description"]) < 320
                    or len(article["description"].split()) < 20
                ):
                    continue

                valid_articles.append(article)


                print(
                    "VALID [NDTV + OLLAMA]",
                    len(valid_articles),
                    "/5",
                )

                print(
                    "DESCRIPTION WORDS:",
                    len(
                        article["description"].split()
                    ),
                )

            print(
                "NDTV + OLLAMA FINAL:",
                len(valid_articles),
                "/5",
            )

            return valid_articles



        valid_articles = []

        articles = group.get(
            "articles",
            [],
        )

        # IMPORTANT:
        # Stop immediately after obtaining 5 valid articles.
        for score, item in articles:

            if len(valid_articles) >= 5:
                break

            article = dict(item)

            article.pop(
                "_feed_url",
                None,
            )

            try:

                processed = process_article(
                    article,
                    rss_fallback_mode=False,
                )

                if not processed:
                    continue

                if not valid_description(
                    processed
                ):
                    continue

                valid_articles.append(
                    processed
                )

                print(
                    f"VALID [{source_name}] "
                    f"{len(valid_articles)}/5"
                )

            except Exception as error:

                print(
                    f"ARTICLE ERROR "
                    f"[{source_name}]: {error}"
                )

        return valid_articles

    # --------------------------------------------------------
    # CATEGORY MODE
    # --------------------------------------------------------

    if category_mode:

        source_results = {}

        # First try the sources already selected above.
        for source_key, group in selected_sources:

            source_results[source_key] = (
                process_source_articles(
                    source_key,
                    group,
                )
            )

        # ----------------------------------------------------
        # FINAL JOBS ARTICLE FILTER
        #
        # This is the actual category-mode path used to build
        # the final 20 articles. Remove employment/workforce/
        # hiring-analysis stories unless the headline itself
        # contains a concrete job opportunity signal.
        # ----------------------------------------------------

        if category == "jobs":

            jobs_opportunity_terms = {
                "recruitment",
                "recruitment notification",
                "recruitment drive",
                "job opening",
                "job openings",
                "job vacancy",
                "job vacancies",
                "invites applications for posts",
                "invites applications for positions",
                "applications invited for posts",
                "applications invited for positions",
                "junior research fellow",
                "research fellow position",
                "research fellow positions",
                "vacancy",
                "vacancies",
                "apply online",
                "apply now",
                "applications open",
                "application open",
                "last date to apply",
                "application deadline",
                "internship",
                "internships",
                "apprentice",
                "apprenticeship",
                "walk-in interview",
                "walk in interview",
                "freshers job",
                "freshers jobs",
                "fresher job",
                "fresher jobs",
                "freshers recruitment",
                "fresher recruitment",
                "graduate recruitment",
                "graduate hiring",
                "graduate jobs",
                "campus recruitment",
                "campus hiring",
                "government job",
                "government jobs",
                "govt job",
                "govt jobs",
                "government recruitment",
                "govt recruitment",
                "railway recruitment",
                "railway jobs",
                "bank recruitment",
                "banking jobs",
                "police recruitment",
                "police jobs",
                "defence recruitment",
                "defense recruitment",
                "defence jobs",
                "defense jobs",
                "teacher recruitment",
                "teaching jobs",
                "research fellowship",
                "research opportunity",
                "research opportunities",
                "project position",
                "project associate",
                "young professional recruitment",
            }

            jobs_analysis_terms = {
                "hiring assessments",
                "hiring assessment",
                "employment rate",
                "unemployment rate",
                "industrial jobs cross",
                "jobs cross",
                "job market",
                "job market trends",
                "employment trends",
                "workforce trends",
                "workforce outlook",
                "employment outlook",
                "salary trends",
                "salary trend",
                "campus salaries",
                "campus salary",
                "median salaries",
                "median salary",
                "skills and job readiness",
                "job readiness",
                "future of work",
                "future of jobs",
                "skills gap",
                "skills shortage",
                "talent powering",
                "talent powering india",
                "cheating in hiring assessments",
                "curious case of hiring",
                "paychecks hit pause",
                "campus hiring towards",
                "campus hiring towards skills",
                "skilled talent powering",
            }

            filtered_source_results = {}

            for source_key, result_articles in source_results.items():

                clean_results = []

                for item in result_articles:

                    title = str(
                        item.get("title") or ""
                    ).strip().lower()

                    has_opportunity = any(
                        term in title
                        for term in jobs_opportunity_terms
                    ) or bool(JOB_POSTING_PATTERN.search(title)) or is_active_job_listing(item)

                    is_analysis = any(
                        term in title
                        for term in jobs_analysis_terms
                    )

                    if is_analysis and not has_opportunity:
                        print(
                            "JOBS ANALYSIS ARTICLE REMOVED:",
                            title,
                        )
                        continue

                    if not has_opportunity:
                        print(
                            "JOBS NON-ACTIONABLE ARTICLE REMOVED:",
                            title,
                        )
                        continue

                    clean_results.append(item)

                if clean_results:
                    filtered_source_results[
                        source_key
                    ] = clean_results

            source_results = filtered_source_results

            print(
                "FINAL JOBS SOURCE RESULTS:",
                {
                    key: len(value)
                    for key, value in source_results.items()
                }
            )

        # Keep publishers that produced enough valid articles to
        # participate in the requested 4–6-per-source balance.
        usable_sources = [
            (
                source_key,
                group,
            )
            for source_key, group in selected_sources
            if len(
                source_results.get(
                    source_key,
                    [],
                )
            ) >= 4
        ]

        # ----------------------------------------------------
        # Selected publishers can still leave the category short after
        # article-page validation or cross-publisher deduplication. Keep
        # scanning configured feeds for replacement publishers until we
        # have 20 distinct valid candidates, or have exhausted the feeds.
        # ----------------------------------------------------

        def category_fill_capacity(results, preferred_sources):
            """Estimate how many articles the final 6-per-publisher fill can use."""
            seen_titles = set()
            seen_urls = set()
            publisher_counts = {}
            count = 0

            source_order = list(preferred_sources)
            source_order.extend(
                source_key
                for source_key in results
                if source_key not in source_order
            )

            for source_key in source_order:
                result_articles = results.get(source_key, [])
                for article in result_articles:
                    title_key = re.sub(
                        r"\s+", " ",
                        str(article.get("title", "") or "").strip().lower(),
                    )
                    url_key = str(article.get("url", "") or "").strip().lower()
                    publisher = re.sub(
                        r"\s+", " ",
                        str(article.get("source", "") or "Unknown Source").strip().lower(),
                    )
                    if (
                        not title_key
                        or title_key in seen_titles
                        or (url_key and url_key in seen_urls)
                        or publisher_counts.get(publisher, 0) >= 6
                    ):
                        continue
                    seen_titles.add(title_key)
                    if url_key:
                        seen_urls.add(url_key)
                    publisher_counts[publisher] = publisher_counts.get(publisher, 0) + 1
                    count += 1
                    if count >= 20:
                        return count
            return count

        processed_source_keys = set(source_results)
        for source_key, group in source_list:
            if (
                len(usable_sources) >= 4
                and category_fill_capacity(
                    source_results,
                    [key for key, _ in usable_sources],
                ) >= 20
            ):
                break
            if source_key in processed_source_keys:
                continue

            result = process_source_articles(source_key, group)
            processed_source_keys.add(source_key)

            # Apply the same Jobs opportunity gate to replacement feeds.
            if category == "jobs":
                filtered = []
                for item in result:
                    title = str(item.get("title", "") or "").strip().lower()
                    description = str(item.get("description", "") or "").strip().lower()
                    has_opportunity = any(
                        term in f"{title} {description}"
                        for term in jobs_opportunity_terms
                    ) or bool(JOB_POSTING_PATTERN.search(f"{title} {description}")) or is_active_job_listing(item)
                    if has_opportunity:
                        filtered.append(item)
                result = filtered

            source_results[source_key] = result
            if len(result) >= 4:
                usable_sources.append((source_key, group))

        # ----------------------------------------------------
        # FLEXIBLE SOURCE FILL
        #
        # Prefer 4-6 sources and balanced distribution, but do
        # not discard valid articles just because one source has
        # fewer than 5 usable articles.
        #
        # Fill the category to 20 unique articles from all valid
        # selected/replacement sources.
        # ----------------------------------------------------

        selected_fill_sources = []

        for source_key, group in usable_sources:
            if source_key not in selected_fill_sources:
                selected_fill_sources.append(source_key)

        # Only use publishers with at least four valid articles. This
        # avoids returning an imbalanced one-off source just to pad counts.

        # First pass: up to 5 articles per source.
        for source_key in selected_fill_sources:
            if len(final_articles) >= 20:
                break

            for article in source_results.get(source_key, []):
                if len(final_articles) >= 20:
                    break

                article_url = str(
                    article.get("url", "")
                    or ""
                ).strip()

                article_title = str(
                    article.get("title", "")
                    or ""
                ).strip().lower()

                duplicate = any(
                    article_url
                    and article_url == str(
                        existing.get("url", "")
                        or ""
                    ).strip()
                    or (
                        article_title
                        and article_title == str(
                            existing.get("title", "")
                            or ""
                        ).strip().lower()
                    )
                    for existing in final_articles
                )

                if duplicate:
                    continue

                source_count = sum(
                    1
                    for existing in final_articles
                    if str(
                        existing.get("source", "")
                        or ""
                    ).strip()
                    == str(
                        article.get("source", "")
                        or ""
                    ).strip()
                )

                if source_count >= 5:
                    continue

                final_articles.append(article)

        # Second pass: if fewer than 20 are available after the
        # balanced pass, use remaining valid articles from all
        # sources without exceeding 6 articles per source.
        if len(final_articles) < 20:

            for source_key, result_articles in source_results.items():

                if len(final_articles) >= 20:
                    break

                for article in result_articles:

                    if len(final_articles) >= 20:
                        break

                    article_url = str(
                        article.get("url", "")
                        or ""
                    ).strip()

                    article_title = str(
                        article.get("title", "")
                        or ""
                    ).strip().lower()

                    duplicate = any(
                        article_url
                        and article_url == str(
                            existing.get("url", "")
                            or ""
                        ).strip()
                        or (
                            article_title
                            and article_title == str(
                                existing.get("title", "")
                                or ""
                            ).strip().lower()
                        )
                        for existing in final_articles
                    )

                    if duplicate:
                        continue

                    source_count = sum(
                        1
                        for existing in final_articles
                        if str(
                            existing.get("source", "")
                            or ""
                        ).strip()
                        == str(
                            article.get("source", "")
                            or ""
                        ).strip()
                    )

                    if source_count >= 6:
                        continue

                    final_articles.append(article)

        print(
            "CATEGORY SOURCE FILL:",
            len(final_articles),
            "articles from",
            len({
                str(
                    article.get("source", "")
                    or ""
                ).strip()
                for article in final_articles
            }),
            "sources."
        )

    # --------------------------------------------------------
    # SEARCH MODE
    # --------------------------------------------------------

    else:
        search_candidates = unique_items[:36]

        def prepare_search_candidate(candidate):
            _, item = candidate
            article = dict(item)
            article.pop("_feed_url", None)

            source_name = str(article.get("source", "") or "").strip()
            if "ndtv" in source_name.lower():
                return None

            try:
                return process_article(
                    article,
                    rss_fallback_mode=False,
                    fast_search_mode=True,
                )
            except Exception as error:
                print(f"SEARCH ARTICLE ERROR: {error}")
                return None

        # Publisher summaries are needed when RSS only repeats a headline.
        # Fetch them concurrently with a short timeout, in relevance-ordered
        # batches, instead of waiting on each publisher serially.
        with ThreadPoolExecutor(max_workers=12) as search_executor:
            for batch_start in range(0, len(search_candidates), 12):
                batch = search_candidates[batch_start:batch_start + 12]
                for processed in search_executor.map(prepare_search_candidate, batch):
                    if processed:
                        final_articles.append(processed)
                        if len(final_articles) >= 20:
                            break
                if len(final_articles) >= 20:
                    break

    # Enforce the same strict 48-hour window on cached and category-mode
    # search results before returning them to the frontend.
    final_articles = [
        article
        for article in final_articles
        if is_recent(article.get("published_at", ""))
    ][:20]

    # --------------------------------------------------------
    # Source distribution
    # --------------------------------------------------------

    distribution = {}

    for article in final_articles:

        source = str(
            article.get(
                "source",
                "Unknown Source",
            )
            or "Unknown Source"
        )

        distribution[source] = (
            distribution.get(
                source,
                0,
            )
            + 1
        )

    # --------------------------------------------------------
    # Final output
    # --------------------------------------------------------

    print("=" * 60)

    print(
        f"SEARCH MATCHED: "
        f"{len(final_articles)} articles"
    )

    print(
        "SOURCE DISTRIBUTION:"
    )

    for source, count in distribution.items():

        print(
            f"  {source}: {count}"
        )

    print("=" * 60)

    return final_articles




# ============================================================
# STARTUPS / STOCKS / WEATHER
# ============================================================

CATEGORY_FEEDS.update({

    "startups": [
    "https://inc42.com/feed/",
    "https://yourstory.com/feed",
    "https://startuptalky.com/feed/",
    "https://businessoutreach.in/feed/",
    "https://officechai.com/feed/",
],

    "stocks": [
        "https://economictimes.indiatimes.com/markets/stocks/rss.cms",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.livemint.com/rss/markets",
        "https://indianexpress.com/section/smart-stocks/feed/",
        "https://www.business-standard.com/rss/markets-106.rss",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
        "https://news.google.com/rss/search?q=India+stock+market+Nifty+Sensex+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=Indian+stocks+shares+market+today+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=NSE+BSE+stocks+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=stocks+in+news+India+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=India+listed+companies+shares+earnings+IPO+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    ],
"weather": [
    "https://indianexpress.com/section/weather/feed/",
    "https://odishatv.in/weather/feed",
    "https://timesofindia.indiatimes.com/rssfeeds/2647163.cms",
    "https://news.google.com/rss/search?q=weather+India+OR+rainfall+India+OR+monsoon+India&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=India+weather+forecast+rainfall+cyclone+flood+heatwave+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=India+IMD+weather+alert+districts+rain+temperature+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=world+weather+forecast+rainfall+storm+flood+cyclone+when%3A5d&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site%3Ahindustantimes.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Andtv.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Athehindu.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Aindiatoday.in+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Aindianexpress.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Atimesofindia.indiatimes.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Aeconomictimes.indiatimes.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Aenglish.dainikjagranmpcg.com+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Addindia.co.in+India+weather+OR+rainfall+OR+cyclone+OR+monsoon+when%3A5d&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=site%3Abbc.com%2Fnews+weather+OR+flood+OR+storm+OR+heatwave+when%3A5d&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site%3Areuters.com+weather+OR+flood+OR+storm+OR+heatwave+when%3A5d&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site%3Aapnews.com+weather+OR+flood+OR+storm+OR+heatwave+when%3A5d&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site%3Aaljazeera.com+weather+OR+flood+OR+storm+OR+heatwave+when%3A5d&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site%3Adw.com+weather+OR+flood+OR+storm+OR+heatwave+when%3A5d&hl=en&gl=US&ceid=US:en",
    "https://feeds.feedburner.com/ndtvnews-india-news",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://www.theguardian.com/environment/climate-crisis/rss",
],

})

# Search supplements extend the publisher feeds above while the regular
# category relevance gate still decides whether each individual story fits.
CATEGORY_FEEDS["latest"].extend([
    "https://feeds.npr.org/1001/rss.xml",
    "https://rss.dw.com/rdf/rss-en-all",
])
