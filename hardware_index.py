#!/usr/bin/env python3
"""
Hardware Price Index
--------------------
Tracks live GPU and RAM prices across eBay (new + used / sold listings),
contextualised with benchmark data and efficiency metrics.

Install:
    pip install flask requests beautifulsoup4 lxml

Run:
    python hardware_index.py

Then open http://localhost:5000 in your browser.

Notes:
- Scrapes eBay active listings (lowest-price-first for new, sold/completed
  for used) to approximate current market pricing.
- Consumer-weighted catalogue: heavy on mid-range, a handful of flagships
  for reference.
- Benchmark scores are ~3DMark Time Spy Graphics (GPU). RAM uses capacity +
  effective MT/s as a proxy for performance.
- Price history is snapshotted to price_history.json so successive refreshes
  build a trend.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

REPO_ROOT = Path(__file__).resolve().parent
SNAPSHOT_FILE = str(REPO_ROOT / "price_history.json")
DOCS_DIR = REPO_ROOT / "docs"
MAX_SNAPSHOTS = 200
OWNER_LABEL = os.environ.get("HW_OWNER", "owner")

# eBay region: change to "ebay.com" if you want US listings.
# UK users should use "ebay.co.uk" to avoid geo-redirect breaking the parser.
EBAY_DOMAIN = "ebay.co.uk"
CURRENCY = "£"           # display label only
VERBOSE = True           # print per-fetch diagnostics to terminal

# ---- Rate-limit tuning ----
# eBay's bot detection triggers around 5+ requests/sec. These conservative
# defaults keep us under the radar at the cost of slower refreshes (~3-4 min
# for the full catalogue). Tighten if you're feeling lucky.
WORKERS = 2              # concurrent item fetchers (each item = 2 requests: new + used)
DELAY_MIN = 1.5          # min seconds between requests within a worker
DELAY_MAX = 3.0          # max seconds between requests within a worker
RETRY_ON_CHALLENGE = True
RETRY_WAIT = 8.0         # seconds to wait before retrying a challenged request
ABORT_AFTER_N_CHALLENGES = 6  # stop refresh if N consecutive challenges seen

# ---------------------------------------------------------------------------
# Hardware catalogue
# ---------------------------------------------------------------------------
# Consumer-weighted: ~2/3 of entries are budget/mid-range. Flagships kept
# for top-of-index reference but deliberately limited.
#
# benchmark: approximate 3DMark Time Spy Graphics score
# msrp: GBP launch RRP (used as the baseline for vs-MSRP ratios)
# exclude: keywords that disqualify a listing (mobile chips, bundles, etc.)

GPUS = [
    # ---- NVIDIA RTX 50-series (2025 gen, current) ----
    {"name": "RTX 5090",       "brand": "NVIDIA", "tier": "flagship",  "msrp": 1939, "benchmark": 31000, "vram": "32GB GDDR7", "year": 2025, "search": "RTX 5090",       "exclude": ["laptop", "mobile", "bundle", "waterblock"]},
    {"name": "RTX 5080",       "brand": "NVIDIA", "tier": "high-end",  "msrp": 979,  "benchmark": 22000, "vram": "16GB GDDR7", "year": 2025, "search": "RTX 5080",       "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 5070 Ti",    "brand": "NVIDIA", "tier": "high-end",  "msrp": 729,  "benchmark": 18500, "vram": "16GB GDDR7", "year": 2025, "search": "RTX 5070 Ti",    "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 5070",       "brand": "NVIDIA", "tier": "mid-range", "msrp": 539,  "benchmark": 14500, "vram": "12GB GDDR7", "year": 2025, "search": "RTX 5070",       "exclude": ["laptop", "mobile", "bundle", "5070 Ti", "5070Ti"]},
    {"name": "RTX 5060 Ti",    "brand": "NVIDIA", "tier": "mid-range", "msrp": 399,  "benchmark": 12000, "vram": "16GB GDDR7", "year": 2025, "search": "RTX 5060 Ti",    "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 5060",       "brand": "NVIDIA", "tier": "budget",    "msrp": 279,  "benchmark": 10000, "vram": "8GB GDDR7",  "year": 2025, "search": "RTX 5060",       "exclude": ["laptop", "mobile", "bundle", "5060 Ti", "5060Ti"]},

    # ---- NVIDIA RTX 40-series (still widely sold) ----
    {"name": "RTX 4090",       "brand": "NVIDIA", "tier": "flagship",  "msrp": 1679, "benchmark": 27000, "vram": "24GB GDDR6X","year": 2022, "search": "RTX 4090",       "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 4080 Super", "brand": "NVIDIA", "tier": "high-end",  "msrp": 959,  "benchmark": 21000, "vram": "16GB GDDR6X","year": 2024, "search": "RTX 4080 Super", "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 4070 Super", "brand": "NVIDIA", "tier": "mid-range", "msrp": 579,  "benchmark": 15500, "vram": "12GB GDDR6X","year": 2024, "search": "RTX 4070 Super", "exclude": ["laptop", "mobile", "bundle", "Ti"]},
    {"name": "RTX 4070",       "brand": "NVIDIA", "tier": "mid-range", "msrp": 499,  "benchmark": 13500, "vram": "12GB GDDR6X","year": 2023, "search": "RTX 4070",       "exclude": ["laptop", "mobile", "bundle", "Ti", "Super"]},
    {"name": "RTX 4060 Ti",    "brand": "NVIDIA", "tier": "mid-range", "msrp": 389,  "benchmark": 11500, "vram": "8GB GDDR6",  "year": 2023, "search": "RTX 4060 Ti",    "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 4060",       "brand": "NVIDIA", "tier": "budget",    "msrp": 289,  "benchmark": 10650, "vram": "8GB GDDR6",  "year": 2023, "search": "RTX 4060",       "exclude": ["laptop", "mobile", "bundle", "Ti"]},

    # ---- NVIDIA RTX 30-series (mostly used market) ----
    {"name": "RTX 3070",       "brand": "NVIDIA", "tier": "mid-range", "msrp": 469,  "benchmark": 11000, "vram": "8GB GDDR6",  "year": 2020, "search": "RTX 3070",       "exclude": ["laptop", "mobile", "bundle", "Ti"]},
    {"name": "RTX 3060 Ti",    "brand": "NVIDIA", "tier": "mid-range", "msrp": 369,  "benchmark": 9700,  "vram": "8GB GDDR6",  "year": 2020, "search": "RTX 3060 Ti",    "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RTX 3060",       "brand": "NVIDIA", "tier": "budget",    "msrp": 299,  "benchmark": 8500,  "vram": "12GB GDDR6", "year": 2021, "search": "RTX 3060 12GB",  "exclude": ["laptop", "mobile", "bundle", "Ti"]},
    {"name": "RTX 3050",       "brand": "NVIDIA", "tier": "budget",    "msrp": 239,  "benchmark": 6000,  "vram": "8GB GDDR6",  "year": 2022, "search": "RTX 3050",       "exclude": ["laptop", "mobile", "bundle"]},

    # ---- AMD RX 9000-series (2025 gen) ----
    {"name": "RX 9070 XT",     "brand": "AMD",    "tier": "high-end",  "msrp": 569,  "benchmark": 17500, "vram": "16GB GDDR6", "year": 2025, "search": "RX 9070 XT",     "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RX 9070",        "brand": "AMD",    "tier": "mid-range", "msrp": 529,  "benchmark": 15500, "vram": "16GB GDDR6", "year": 2025, "search": "RX 9070",        "exclude": ["laptop", "mobile", "bundle", "XT"]},

    # ---- AMD RX 7000-series ----
    {"name": "RX 7900 XTX",    "brand": "AMD",    "tier": "flagship",  "msrp": 1049,  "benchmark": 22500, "vram": "24GB GDDR6", "year": 2022, "search": "RX 7900 XTX",    "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RX 7800 XT",     "brand": "AMD",    "tier": "mid-range", "msrp": 479,  "benchmark": 16500, "vram": "16GB GDDR6", "year": 2023, "search": "RX 7800 XT",     "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RX 7700 XT",     "brand": "AMD",    "tier": "mid-range", "msrp": 429,  "benchmark": 13500, "vram": "12GB GDDR6", "year": 2023, "search": "RX 7700 XT",     "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RX 7600",        "brand": "AMD",    "tier": "budget",    "msrp": 239,  "benchmark": 8500,  "vram": "8GB GDDR6",  "year": 2023, "search": "RX 7600",        "exclude": ["laptop", "mobile", "bundle", "XT"]},

    # ---- AMD RX 6000-series (used market) ----
    {"name": "RX 6700 XT",     "brand": "AMD",    "tier": "mid-range", "msrp": 419,  "benchmark": 11000, "vram": "12GB GDDR6", "year": 2021, "search": "RX 6700 XT",     "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "RX 6600",        "brand": "AMD",    "tier": "budget",    "msrp": 299,  "benchmark": 7500,  "vram": "8GB GDDR6",  "year": 2021, "search": "RX 6600",        "exclude": ["laptop", "mobile", "bundle", "XT"]},

    # ---- Intel Arc (growing budget presence) ----
    {"name": "Intel Arc B580",    "brand": "Intel","tier": "budget",  "msrp": 239,  "benchmark": 10000, "vram": "12GB GDDR6", "year": 2024, "search": "Intel Arc B580",    "exclude": ["laptop", "mobile", "bundle"]},
    {"name": "Intel Arc A750",    "brand": "Intel","tier": "budget",  "msrp": 289,  "benchmark": 8500,  "vram": "8GB GDDR6",  "year": 2022, "search": "Intel Arc A750",    "exclude": ["laptop", "mobile", "bundle"]},
]

RAM_KITS = [
    # ---- DDR4 (still relevant for AM4 + older Intel) ----
    {"name": "Corsair Vengeance LPX 16GB (2x8) DDR4-3200", "brand": "Corsair",  "tier": "budget",    "capacity": 16, "speed": 3200, "gen": "DDR4", "msrp": 39,  "search": "Corsair Vengeance LPX 16GB 3200",    "exclude": ["32GB", "64GB", "ECC", "laptop", "sodimm"]},
    {"name": "Corsair Vengeance LPX 32GB (2x16) DDR4-3600","brand": "Corsair",  "tier": "mid-range", "capacity": 32, "speed": 3600, "gen": "DDR4", "msrp": 89,   "search": "Corsair Vengeance LPX 32GB 3600",    "exclude": ["16GB", "64GB", "ECC", "laptop", "sodimm"]},
    {"name": "G.Skill Ripjaws V 16GB (2x8) DDR4-3600",    "brand": "G.Skill",  "tier": "budget",    "capacity": 16, "speed": 3600, "gen": "DDR4", "msrp": 49,  "search": "G.Skill Ripjaws V 16GB 3600",        "exclude": ["32GB", "64GB", "ECC", "laptop", "sodimm"]},
    {"name": "G.Skill Trident Z RGB 32GB (2x16) DDR4-3600","brand": "G.Skill", "tier": "mid-range", "capacity": 32, "speed": 3600, "gen": "DDR4", "msrp": 99,  "search": "G.Skill Trident Z RGB 32GB 3600",    "exclude": ["16GB", "64GB", "DDR5", "laptop", "sodimm"]},

    # ---- DDR5 (current mainstream) ----
    {"name": "Kingston Fury Beast 16GB (2x8) DDR5-5200",  "brand": "Kingston", "tier": "budget",    "capacity": 16, "speed": 5200, "gen": "DDR5", "msrp": 55,  "search": "Kingston Fury Beast 16GB DDR5 5200", "exclude": ["32GB", "64GB", "DDR4", "laptop", "sodimm"]},
    {"name": "Crucial 32GB (2x16) DDR5-5600",             "brand": "Crucial",  "tier": "budget",    "capacity": 32, "speed": 5600, "gen": "DDR5", "msrp": 89,  "search": "Crucial 32GB DDR5 5600",             "exclude": ["16GB", "64GB", "DDR4", "laptop", "sodimm", "Pro"]},
    {"name": "Kingston Fury Beast 32GB (2x16) DDR5-5600", "brand": "Kingston", "tier": "budget",    "capacity": 32, "speed": 5600, "gen": "DDR5", "msrp": 99,  "search": "Kingston Fury Beast 32GB DDR5 5600", "exclude": ["16GB", "64GB", "DDR4", "laptop", "sodimm"]},
    {"name": "Corsair Vengeance 32GB (2x16) DDR5-6000",   "brand": "Corsair",  "tier": "mid-range", "capacity": 32, "speed": 6000, "gen": "DDR5", "msrp": 129, "search": "Corsair Vengeance 32GB DDR5 6000",   "exclude": ["16GB", "64GB", "DDR4", "laptop", "sodimm"]},
    {"name": "G.Skill Trident Z5 Neo 32GB (2x16) DDR5-6000 CL30","brand":"G.Skill","tier":"mid-range","capacity":32,"speed":6000,"gen":"DDR5","msrp":139,"search":"G.Skill Trident Z5 Neo 32GB 6000 CL30","exclude":["16GB","64GB","DDR4","laptop","sodimm"]},
    {"name": "G.Skill Trident Z5 32GB (2x16) DDR5-6400",  "brand": "G.Skill",  "tier": "mid-range", "capacity": 32, "speed": 6400, "gen": "DDR5", "msrp": 159, "search": "G.Skill Trident Z5 32GB DDR5 6400",  "exclude": ["16GB", "64GB", "DDR4", "laptop", "sodimm", "Neo"]},
    {"name": "Corsair Vengeance 64GB (2x32) DDR5-6000",   "brand": "Corsair",  "tier": "high-end",  "capacity": 64, "speed": 6000, "gen": "DDR5", "msrp": 249, "search": "Corsair Vengeance 64GB DDR5 6000",   "exclude": ["16GB", "32GB", "DDR4", "laptop", "sodimm"]},
    {"name": "G.Skill Trident Z5 64GB (2x32) DDR5-6400",  "brand": "G.Skill",  "tier": "high-end",  "capacity": 64, "speed": 6400, "gen": "DDR5", "msrp": 299, "search": "G.Skill Trident Z5 64GB DDR5 6400",  "exclude": ["16GB", "32GB", "DDR4", "laptop", "sodimm"]},
]

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

cache = {
    "gpu": [],
    "ram": [],
    "last_refresh": None,
    "refreshing": False,
    "progress": {"done": 0, "total": 0, "current": ""},
}
cache_lock = threading.Lock()

# Tracks consecutive challenge responses so we can abort early
challenge_counter = {"count": 0, "abort": False}
challenge_lock = threading.Lock()


def make_session() -> requests.Session:
    """Create a session warmed up with eBay homepage cookies.

    Visiting the homepage first establishes session cookies that eBay's
    bot scoring uses to grant trust. A cold scrape with no cookies looks
    much more bot-like than one with an established browse history.
    """
    sess = requests.Session()
    ua = random.choice(USER_AGENTS)
    sess.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        sess.get(f"https://www.{EBAY_DOMAIN}/", timeout=10)
        if VERBOSE:
            print(f"  [session] warmed up · cookies={len(sess.cookies)} · UA={ua[:50]}...")
    except requests.RequestException as e:
        if VERBOSE:
            print(f"  [session] warmup failed (non-fatal): {e}")
    return sess


# ---------------------------------------------------------------------------
# eBay scraping
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Lowercase + strip whitespace/hyphens for fuzzy matching ('16 gb' == '16gb')."""
    return re.sub(r"[\s\-_/]+", "", s.lower())


def _detect_challenge(html: str) -> Optional[str]:
    """Return a string describing why a page looks like a bot-challenge, or None."""
    head = html[:8000].lower()
    if "captcha" in head or "are you a human" in head:
        return "captcha"
    if "verify yourself" in head or "unusual traffic" in head:
        return "verification"
    if "pardon our interruption" in head:
        return "pardon-interruption"
    return None


def _parse_prices(html: str, search_term: str, exclude_terms: list[str]) -> tuple[list[float], dict]:
    """Parse eBay search HTML. Returns (prices, diagnostics)."""
    diag = {"items_seen": 0, "items_matched": 0, "challenge": None, "selector_used": None}

    challenge = _detect_challenge(html)
    if challenge:
        diag["challenge"] = challenge
        return [], diag

    soup = BeautifulSoup(html, "html.parser")

    # Try selectors in order of preference; eBay has shipped several layouts.
    candidate_selectors = [
        "li.s-item",
        "div.s-item",
        "li.srp-river-results-item",
        "div.srp-results__item",
        "li[data-view*='mi:1686']",  # newer item attribute
    ]
    items = []
    for sel in candidate_selectors:
        items = soup.select(sel)
        if items:
            diag["selector_used"] = sel
            break

    if not items:
        diag["challenge"] = "no-items-found"
        return [], diag

    diag["items_seen"] = len(items)

    # Normalize search tokens: every token of length>1 must appear in the
    # normalized title (so "16gb" matches "16 GB" and "RTX 4070" matches
    # "rtx4070").
    raw_tokens = [t for t in re.findall(r"\w+", search_term) if len(t) > 1]
    norm_tokens = [_normalize(t) for t in raw_tokens]
    norm_excludes = [_normalize(e) for e in exclude_terms if len(e) > 1]

    prices: list[float] = []

    for item in items[:60]:
        # Title selectors — try several
        title_el = (
            item.select_one(".s-item__title")
            or item.select_one("[role='heading']")
            or item.select_one(".s-item__link span")
            or item.select_one("h3")
        )
        price_el = (
            item.select_one(".s-item__price")
            or item.select_one("[class*='price']")
        )
        if not title_el or not price_el:
            continue

        title = title_el.get_text(" ", strip=True)
        if not title or "shop on ebay" in title.lower() or "new listing" == title.lower():
            continue

        norm_title = _normalize(title)

        # All search tokens must be present in the normalized title
        if not all(tok in norm_title for tok in norm_tokens):
            continue

        # No exclude term in normalized title
        if any(ex in norm_title for ex in norm_excludes):
            continue

        # Price extraction — handle £, $, €, ranges ("£10 to £20" -> take low)
        price_text = price_el.get_text(" ", strip=True)
        # Strip currency symbols & thousands separators, find first numeric
        matches = re.findall(r"([\d,]+\.\d{2})", price_text)
        if not matches:
            matches = re.findall(r"([\d,]+)", price_text)
        if not matches:
            continue

        try:
            price = float(matches[0].replace(",", ""))
        except ValueError:
            continue

        # Sanity bounds (works for both USD and GBP magnitudes)
        if 15 <= price <= 10000:
            prices.append(price)
            diag["items_matched"] += 1

    return prices, diag


def fetch_ebay(search_term: str, exclude_terms: list[str], condition: str,
               session: Optional[requests.Session] = None) -> dict:
    """Fetch prices from eBay.
    condition = 'new' -> active new listings, sorted price+shipping ascending
    condition = 'used' -> sold/completed used listings (true market prices)
    """
    q = search_term.replace(" ", "+")
    base = f"https://www.{EBAY_DOMAIN}/sch/i.html"
    if condition == "new":
        url = f"{base}?_nkw={q}&LH_ItemCondition=1000&_sop=15&_ipg=60"
    else:
        url = f"{base}?_nkw={q}&LH_Sold=1&LH_Complete=1&LH_ItemCondition=3000&_ipg=60"

    headers = {"Referer": f"https://www.{EBAY_DOMAIN}/"}
    sess = session or requests

    def _do_get() -> tuple[Optional[requests.Response], Optional[str]]:
        try:
            resp = sess.get(url, headers=headers, timeout=15, allow_redirects=True)
            return resp, None
        except requests.RequestException as e:
            return None, str(e)

    resp, err = _do_get()
    if err:
        if VERBOSE:
            print(f"  [{condition:4}] {search_term}: EXCEPTION {err}")
        return {"prices": [], "error": err, "diag": None, "url": url}

    if resp.status_code != 200:
        if VERBOSE:
            print(f"  [{condition:4}] {search_term}: HTTP {resp.status_code}")
        return {"prices": [], "error": f"HTTP {resp.status_code}", "diag": None, "url": url}

    prices, diag = _parse_prices(resp.text, search_term, exclude_terms)
    diag["final_url"] = resp.url
    diag["response_size"] = len(resp.text)
    diag["status"] = resp.status_code
    diag["retried"] = False

    # Retry once if we got a challenge page
    if diag.get("challenge") and RETRY_ON_CHALLENGE:
        if VERBOSE:
            print(f"  [{condition:4}] {search_term}: challenge — retrying in {RETRY_WAIT}s...")
        time.sleep(RETRY_WAIT)
        resp2, err2 = _do_get()
        if resp2 is not None and resp2.status_code == 200:
            prices2, diag2 = _parse_prices(resp2.text, search_term, exclude_terms)
            diag2["final_url"] = resp2.url
            diag2["response_size"] = len(resp2.text)
            diag2["status"] = resp2.status_code
            diag2["retried"] = True
            if not diag2.get("challenge"):
                # retry succeeded
                prices, diag = prices2, diag2

    # Track consecutive challenges for early-abort logic
    with challenge_lock:
        if diag.get("challenge"):
            challenge_counter["count"] += 1
            if challenge_counter["count"] >= ABORT_AFTER_N_CHALLENGES:
                challenge_counter["abort"] = True
        else:
            challenge_counter["count"] = 0

    if VERBOSE:
        ch = f" CHALLENGE={diag['challenge']}" if diag.get("challenge") else ""
        rt = " (retried)" if diag.get("retried") else ""
        redirect = " (redirected)" if resp.url != url else ""
        print(
            f"  [{condition:4}] {search_term:<42} "
            f"size={len(resp.text)//1024}KB items={diag['items_seen']:>3} "
            f"matched={diag['items_matched']:>3} prices={len(prices):>2}"
            f"{ch}{rt}{redirect}"
        )

    return {"prices": prices, "error": None, "diag": diag, "url": url}


def _stats(prices: list[float]) -> dict:
    if not prices:
        return {"count": 0, "median": None, "low": None, "high": None}
    srt = sorted(prices)
    # Trim outer 10% if we have enough data to reduce outlier impact
    if len(srt) >= 8:
        k = max(1, len(srt) // 10)
        trimmed = srt[k:-k]
    else:
        trimmed = srt
    return {
        "count": len(prices),
        "median": round(median(trimmed), 2),
        "low": round(min(trimmed), 2),
        "high": round(max(trimmed), 2),
    }


def process_item(item: dict, kind: str, session: Optional[requests.Session] = None,
                 prior_data: Optional[dict] = None) -> dict:
    """Fetch new + used prices for a single hardware item.

    prior_data: previous successful result for this item, used as fallback
                if the current fetch fails (returns 0 prices). Keeps stale
                data on screen instead of wiping it to '—'.
    """
    with cache_lock:
        cache["progress"]["current"] = item["name"]

    # Early-abort check: if eBay has been blocking us, skip the rest of the run
    with challenge_lock:
        if challenge_counter["abort"]:
            with cache_lock:
                cache["progress"]["done"] += 1
            result = dict(item)
            if prior_data:
                # Carry forward old data with stale flag
                result["used"] = prior_data.get("used", {"count": 0, "median": None, "low": None, "high": None})
                result["new"] = prior_data.get("new", {"count": 0, "median": None, "low": None, "high": None})
                result["stale"] = True
            else:
                result["used"] = {"count": 0, "median": None, "low": None, "high": None}
                result["new"] = {"count": 0, "median": None, "low": None, "high": None}
                result["stale"] = False
            result["errors"] = {"new": "skipped (rate-limited)", "used": "skipped (rate-limited)"}
            result["status"] = {"new": "skipped", "used": "skipped"}
            return result

    used = fetch_ebay(item["search"], item.get("exclude", []), "used", session=session)
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    new = fetch_ebay(item["search"], item.get("exclude", []), "new", session=session)
    # Trailing delay so the next item in this worker also gets paced
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    used_stats = _stats(used["prices"])
    new_stats = _stats(new["prices"])

    def _status(fetch_result: dict) -> str:
        if fetch_result["error"]:
            return "error"
        d = fetch_result.get("diag") or {}
        if d.get("challenge"):
            return "challenge"
        if d.get("items_seen", 0) == 0:
            return "no-items"
        if d.get("items_matched", 0) == 0:
            return "no-match"
        return "ok"

    result = dict(item)
    result["used"] = used_stats
    result["new"] = new_stats
    result["errors"] = {"new": new["error"], "used": used["error"]}
    result["status"] = {"new": _status(new), "used": _status(used)}
    result["stale"] = False

    # Fallback: if a side returned no prices but we have prior data, carry forward
    if prior_data:
        if used_stats["count"] == 0 and prior_data.get("used", {}).get("count"):
            result["used"] = prior_data["used"]
            result["status"]["used"] = "stale"
            result["stale"] = True
        if new_stats["count"] == 0 and prior_data.get("new", {}).get("count"):
            result["new"] = prior_data["new"]
            result["status"]["new"] = "stale"
            result["stale"] = True

    # Efficiency metrics
    if kind == "gpu":
        if used_stats["median"]:
            result["perf_per_dollar_used"] = round(item["benchmark"] / used_stats["median"], 1)
        if new_stats["median"]:
            result["perf_per_dollar_new"] = round(item["benchmark"] / new_stats["median"], 1)
    else:  # ram
        if used_stats["median"]:
            result["cost_per_gb_used"] = round(used_stats["median"] / item["capacity"], 2)
        if new_stats["median"]:
            result["cost_per_gb_new"] = round(new_stats["median"] / item["capacity"], 2)

    with cache_lock:
        cache["progress"]["done"] += 1
    return result


# ---------------------------------------------------------------------------
# Trend calculation from snapshot history
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    if not os.path.exists(SNAPSHOT_FILE):
        return []
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_snapshot(gpu_data: list[dict], ram_data: list[dict]) -> None:
    history = load_history()
    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "gpu": [
            {"name": x["name"],
             "new": x["new"],
             "used": x["used"]}
            for x in gpu_data
        ],
        "ram": [
            {"name": x["name"],
             "new": x["new"],
             "used": x["used"]}
            for x in ram_data
        ],
    }
    history.append(snap)
    history = history[-MAX_SNAPSHOTS:]
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(history, f)
    except OSError as e:
        print(f"[warn] snapshot save failed: {e}")


def latest_data_for_kind(kind: str) -> dict[str, dict]:
    """Return {item_name: {new, used}} from most recent snapshot, for warm-loading."""
    history = load_history()
    for snap in reversed(history):
        items = snap.get(kind, [])
        # Tolerate the legacy snapshot format (median-only)
        if items and isinstance(items[0].get("new"), dict):
            return {x["name"]: {"new": x["new"], "used": x["used"]} for x in items}
    return {}


def _pct_change(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    if current is None or prior is None or prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


def attach_trends(items: list[dict], kind: str) -> None:
    """Compare current medians to previous snapshot and attach % change."""
    history = load_history()
    if len(history) < 2:
        for it in items:
            it["trend_new"] = None
            it["trend_used"] = None
        return

    prev = history[-2]  # -1 is the snapshot we just saved
    prev_map = {x["name"]: x for x in prev.get(kind, [])}
    for it in items:
        p = prev_map.get(it["name"])
        if not p:
            it["trend_new"] = None
            it["trend_used"] = None
            continue
        # Support both new format (full dict) and legacy format (median fields)
        prev_new_med = p["new"]["median"] if isinstance(p.get("new"), dict) else p.get("new_median")
        prev_used_med = p["used"]["median"] if isinstance(p.get("used"), dict) else p.get("used_median")
        it["trend_new"] = _pct_change(it["new"]["median"], prev_new_med)
        it["trend_used"] = _pct_change(it["used"]["median"], prev_used_med)


# ---------------------------------------------------------------------------
# Refresh orchestration
# ---------------------------------------------------------------------------

def refresh_all() -> None:
    with cache_lock:
        if cache["refreshing"]:
            return
        cache["refreshing"] = True
        cache["progress"] = {"done": 0, "total": len(GPUS) + len(RAM_KITS), "current": "starting..."}

    try:
        # Reset the early-abort tracker for this run
        with challenge_lock:
            challenge_counter["count"] = 0
            challenge_counter["abort"] = False

        # Build name -> prior result map so we can carry forward stale data
        with cache_lock:
            prior_gpu = {x["name"]: x for x in cache["gpu"]}
            prior_ram = {x["name"]: x for x in cache["ram"]}

        # Each worker gets its own session so cookies stay isolated per UA
        sessions = [make_session() for _ in range(WORKERS)]

        gpu_results: list[dict] = []
        ram_results: list[dict] = []

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {
                ex.submit(process_item, g, "gpu", sessions[i % WORKERS], prior_gpu.get(g["name"])): g
                for i, g in enumerate(GPUS)
            }
            for fut in as_completed(futures):
                gpu_results.append(fut.result())

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {
                ex.submit(process_item, r, "ram", sessions[i % WORKERS], prior_ram.get(r["name"])): r
                for i, r in enumerate(RAM_KITS)
            }
            for fut in as_completed(futures):
                ram_results.append(fut.result())

        gpu_results.sort(key=lambda x: x["benchmark"], reverse=True)
        ram_results.sort(key=lambda x: (x["gen"], x["capacity"], x["speed"]), reverse=True)

        save_snapshot(gpu_results, ram_results)
        attach_trends(gpu_results, "gpu")
        attach_trends(ram_results, "ram")

        with cache_lock:
            cache["gpu"] = gpu_results
            cache["ram"] = ram_results
            cache["last_refresh"] = datetime.now(timezone.utc).isoformat()

        # Final summary in terminal
        with challenge_lock:
            aborted = challenge_counter["abort"]
        ok_count = sum(
            1 for x in gpu_results + ram_results
            if x.get("new", {}).get("count", 0) > 0 or x.get("used", {}).get("count", 0) > 0
        )
        stale_count = sum(1 for x in gpu_results + ram_results if x.get("stale"))
        total = len(gpu_results) + len(ram_results)
        print(f"\n  [refresh complete] {ok_count}/{total} live · {stale_count} stale-fallback · aborted={aborted}\n")

        # Mirror to the static export so docs/ is always one Refresh ahead of the
        # next Publish. Publish itself stays a manual, owner-driven action.
        try:
            export_static_site(DOCS_DIR)
            print(f"  [export] static site written to {DOCS_DIR}\n")
        except Exception as e:
            print(f"[warn] static export failed: {e}")
    except Exception as e:
        print(f"[error] refresh failed: {e}")
    finally:
        with cache_lock:
            cache["refreshing"] = False
            cache["progress"]["current"] = ""


def warm_load_from_history() -> None:
    """On startup, repopulate the cache from the most recent snapshot so the
    UI shows the last known prices immediately instead of an empty table."""
    gpu_prior = latest_data_for_kind("gpu")
    ram_prior = latest_data_for_kind("ram")
    if not gpu_prior and not ram_prior:
        return

    history = load_history()
    last_ts = history[-1].get("ts") if history else None

    def _attach(items, kind, prior):
        out = []
        for item in items:
            r = dict(item)
            p = prior.get(item["name"], {})
            r["new"] = p.get("new", {"count": 0, "median": None, "low": None, "high": None})
            r["used"] = p.get("used", {"count": 0, "median": None, "low": None, "high": None})
            r["errors"] = {"new": None, "used": None}
            r["status"] = {"new": "stale", "used": "stale"}
            r["stale"] = True
            r["trend_new"] = None
            r["trend_used"] = None
            if kind == "gpu":
                if r["used"].get("median"):
                    r["perf_per_dollar_used"] = round(item["benchmark"] / r["used"]["median"], 1)
                if r["new"].get("median"):
                    r["perf_per_dollar_new"] = round(item["benchmark"] / r["new"]["median"], 1)
            else:
                if r["used"].get("median"):
                    r["cost_per_gb_used"] = round(r["used"]["median"] / item["capacity"], 2)
                if r["new"].get("median"):
                    r["cost_per_gb_new"] = round(r["new"]["median"] / item["capacity"], 2)
            out.append(r)
        return out

    gpu_warm = _attach(GPUS, "gpu", gpu_prior)
    ram_warm = _attach(RAM_KITS, "ram", ram_prior)
    gpu_warm.sort(key=lambda x: x["benchmark"], reverse=True)
    ram_warm.sort(key=lambda x: (x["gen"], x["capacity"], x["speed"]), reverse=True)

    with cache_lock:
        cache["gpu"] = gpu_warm
        cache["ram"] = ram_warm
        cache["last_refresh"] = last_ts

    print(f"  [startup] warm-loaded {len(gpu_warm)} GPUs + {len(ram_warm)} RAM kits from {last_ts}")


# ---------------------------------------------------------------------------
# Index / aggregate computation
# ---------------------------------------------------------------------------
# These power the INDEX tab in the UI. The "index" is the average ratio of
# market price to MSRP across a category. A value of 1.07 means the market
# is trading 7% over MSRP on average; 0.62 means used is at 62% of MSRP.

def _ratio_index(items: list[dict], meta: dict, side: str) -> tuple[Optional[float], Optional[float], int]:
    """Return (avg_ratio, avg_price, n) for items with valid medians and MSRPs."""
    ratios = []
    prices = []
    for x in items:
        m = meta.get(x["name"])
        if not m:
            continue
        side_data = x.get(side) if isinstance(x.get(side), dict) else {}
        med = side_data.get("median")
        if med and m.get("msrp"):
            ratios.append(med / m["msrp"])
            prices.append(med)
    if not ratios:
        return None, None, 0
    return (sum(ratios) / len(ratios)), (sum(prices) / len(prices)), len(ratios)


def compute_time_series() -> list[dict]:
    """Walk snapshot history; for each snapshot, compute the 4 indices.

    Skips legacy snapshots (older format that only stored medians without
    full stats) since they don't carry enough data — though for this purpose
    medians alone would suffice. We accept either format here.
    """
    history = load_history()
    gpu_meta = {g["name"]: g for g in GPUS}
    ram_meta = {r["name"]: r for r in RAM_KITS}

    series: list[dict] = []
    for snap in history:
        ts = snap.get("ts")
        gpu_items = snap.get("gpu", [])
        ram_items = snap.get("ram", [])

        # Normalize legacy format (median-only) to look like the full format
        def _norm(items):
            out = []
            for x in items:
                if isinstance(x.get("new"), dict):
                    out.append(x)
                else:
                    out.append({
                        "name": x.get("name"),
                        "new": {"median": x.get("new_median")},
                        "used": {"median": x.get("used_median")},
                    })
            return out

        gpu_items = _norm(gpu_items)
        ram_items = _norm(ram_items)

        gpu_new_idx, gpu_new_avg, gpu_new_n = _ratio_index(gpu_items, gpu_meta, "new")
        gpu_used_idx, gpu_used_avg, gpu_used_n = _ratio_index(gpu_items, gpu_meta, "used")
        ram_new_idx, ram_new_avg, ram_new_n = _ratio_index(ram_items, ram_meta, "new")
        ram_used_idx, ram_used_avg, ram_used_n = _ratio_index(ram_items, ram_meta, "used")

        series.append({
            "ts": ts,
            "gpu_new_idx": gpu_new_idx,
            "gpu_used_idx": gpu_used_idx,
            "ram_new_idx": ram_new_idx,
            "ram_used_idx": ram_used_idx,
            "gpu_new_avg": gpu_new_avg,
            "gpu_used_avg": gpu_used_avg,
            "ram_new_avg": ram_new_avg,
            "ram_used_avg": ram_used_avg,
            "n": {
                "gpu_new": gpu_new_n, "gpu_used": gpu_used_n,
                "ram_new": ram_new_n, "ram_used": ram_used_n,
            },
        })
    return series


def compute_current_stats() -> dict:
    """Tier-level breakdowns + best-deal leaderboards from the live cache."""
    with cache_lock:
        gpu_data = list(cache["gpu"])
        ram_data = list(cache["ram"])

    def tier_stats(items: list[dict], side: str) -> dict:
        by_tier: dict[str, list[dict]] = {}
        for x in items:
            tier = x.get("tier")
            side_data = x.get(side) if isinstance(x.get(side), dict) else {}
            med = side_data.get("median")
            msrp = x.get("msrp")
            if not tier or not med or not msrp:
                continue
            by_tier.setdefault(tier, []).append({"median": med, "msrp": msrp, "ratio": med / msrp})
        out = {}
        for tier, vals in by_tier.items():
            ratios = [v["ratio"] for v in vals]
            out[tier] = {
                "count": len(vals),
                "avg_ratio": round(sum(ratios) / len(ratios), 3),
                "avg_price": round(sum(v["median"] for v in vals) / len(vals), 2),
                "avg_msrp": round(sum(v["msrp"] for v in vals) / len(vals), 2),
            }
        return out

    def used_vs_new_discount(items: list[dict]) -> Optional[float]:
        deltas = []
        for x in items:
            n = x.get("new", {}).get("median") if isinstance(x.get("new"), dict) else None
            u = x.get("used", {}).get("median") if isinstance(x.get("used"), dict) else None
            if n and u and n > 0:
                deltas.append((n - u) / n)
        return round(sum(deltas) / len(deltas), 3) if deltas else None

    # GPU best perf/£ — higher is better
    gpu_best_new = sorted(
        [x for x in gpu_data if x.get("perf_per_dollar_new")],
        key=lambda x: x["perf_per_dollar_new"], reverse=True
    )[:5]
    gpu_best_used = sorted(
        [x for x in gpu_data if x.get("perf_per_dollar_used")],
        key=lambda x: x["perf_per_dollar_used"], reverse=True
    )[:5]
    # RAM best £/GB — lower is better
    ram_best_new = sorted(
        [x for x in ram_data if x.get("cost_per_gb_new")],
        key=lambda x: x["cost_per_gb_new"]
    )[:5]
    ram_best_used = sorted(
        [x for x in ram_data if x.get("cost_per_gb_used")],
        key=lambda x: x["cost_per_gb_used"]
    )[:5]

    return {
        "tiers": {
            "gpu_new": tier_stats(gpu_data, "new"),
            "gpu_used": tier_stats(gpu_data, "used"),
            "ram_new": tier_stats(ram_data, "new"),
            "ram_used": tier_stats(ram_data, "used"),
        },
        "discounts": {
            "gpu_used_vs_new": used_vs_new_discount(gpu_data),
            "ram_used_vs_new": used_vs_new_discount(ram_data),
        },
        "leaders": {
            "gpu_new": [
                {"name": x["name"], "tier": x["tier"], "value": x["perf_per_dollar_new"], "price": x["new"]["median"]}
                for x in gpu_best_new
            ],
            "gpu_used": [
                {"name": x["name"], "tier": x["tier"], "value": x["perf_per_dollar_used"], "price": x["used"]["median"]}
                for x in gpu_best_used
            ],
            "ram_new": [
                {"name": x["name"], "tier": x["tier"], "value": x["cost_per_gb_new"], "price": x["new"]["median"], "capacity": x["capacity"]}
                for x in ram_best_new
            ],
            "ram_used": [
                {"name": x["name"], "tier": x["tier"], "value": x["cost_per_gb_used"], "price": x["used"]["median"], "capacity": x["capacity"]}
                for x in ram_best_used
            ],
        },
    }


# ---------------------------------------------------------------------------
# Static export
# ---------------------------------------------------------------------------
# The Flask app is the local control panel. For public viewing we mirror the
# dashboard to a static directory (default `docs/`) that GitHub Pages serves.
# The HTML template is the single source of truth — at export time we inject
# `window.HW_STATIC = true` plus the JSON payloads the JS would otherwise have
# fetched from /api/data and /api/aggregates, and the JS branches on that flag
# to switch URLs and hide owner-only buttons.

STATIC_INJECT_MARKER = "<!-- STATIC-INJECT -->"


def _build_static_html() -> str:
    """Render the same template, but with a STATIC marker for the JS to flip on."""
    flag = (
        f'<script>window.HW_STATIC = true; '
        f'window.HW_STATIC_OWNER = {json.dumps(OWNER_LABEL)};</script>'
    )
    if STATIC_INJECT_MARKER in HTML:
        return HTML.replace(STATIC_INJECT_MARKER, flag)
    # Fallback: stick it right after <head>
    return HTML.replace("<head>", "<head>\n" + flag, 1)


def export_static_site(output_dir) -> Path:
    """Write a self-contained mirror of the dashboard to `output_dir`.

    Writes to a sibling temp directory and atomically swaps it in, so a
    half-written export can never get served by GitHub Pages.
    """
    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    with cache_lock:
        gpu_data = list(cache["gpu"])
        ram_data = list(cache["ram"])
        last_refresh = cache["last_refresh"]

    data_payload = {
        "gpu": gpu_data,
        "ram": ram_data,
        "last_refresh": last_refresh,
        "refreshing": False,
        "progress": {"done": 0, "total": 0, "current": ""},
    }
    aggregates_payload = {
        "series": compute_time_series(),
        "current": compute_current_stats(),
    }
    meta_payload = {
        "owner": OWNER_LABEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_refresh": last_refresh,
        "gpu_count": len(gpu_data),
        "ram_count": len(ram_data),
    }

    tmp = Path(tempfile.mkdtemp(prefix=".docs-tmp-", dir=str(output_dir.parent)))
    try:
        (tmp / "index.html").write_text(_build_static_html(), encoding="utf-8")
        (tmp / "data.json").write_text(json.dumps(data_payload), encoding="utf-8")
        (tmp / "aggregates.json").write_text(json.dumps(aggregates_payload), encoding="utf-8")
        (tmp / "meta.json").write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")
        # Disable Jekyll on Pages so files starting with _ aren't filtered out
        # and the build is instant. Harmless if already disabled.
        (tmp / ".nojekyll").write_text("", encoding="utf-8")

        # Atomic swap: replace the existing docs/ with the freshly built tree.
        if output_dir.exists():
            backup = output_dir.with_name(output_dir.name + ".old")
            if backup.exists():
                shutil.rmtree(backup)
            output_dir.rename(backup)
            try:
                tmp.rename(output_dir)
            except Exception:
                backup.rename(output_dir)  # roll back
                raise
            shutil.rmtree(backup, ignore_errors=True)
        else:
            tmp.rename(output_dir)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    return output_dir


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/data")
def api_data():
    with cache_lock:
        return jsonify({
            "gpu": cache["gpu"],
            "ram": cache["ram"],
            "last_refresh": cache["last_refresh"],
            "refreshing": cache["refreshing"],
            "progress": cache["progress"],
        })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    with cache_lock:
        if cache["refreshing"]:
            return jsonify({"status": "already_refreshing"})
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/history")
def api_history():
    return jsonify(load_history())


@app.route("/api/aggregates")
def api_aggregates():
    """Returns time series of indices + current tier/leader stats for the INDEX tab."""
    return jsonify({
        "series": compute_time_series(),
        "current": compute_current_stats(),
    })


@app.route("/api/publish", methods=["POST"])
def api_publish():
    """Stage docs/ + price_history.json, commit, push.

    The owner inspects data after Refresh, then triggers this when satisfied.
    Failures (no remote, auth, conflicts) bubble back to the UI verbatim from
    git stderr so they can be acted on without leaving the browser.
    """
    git = shutil.which("git")
    if not git:
        return jsonify({
            "ok": False,
            "stage": "preflight",
            "error": "git is not on PATH — install Git for Windows or add it to PATH and retry.",
        }), 500

    if not DOCS_DIR.exists():
        return jsonify({
            "ok": False,
            "stage": "preflight",
            "error": "docs/ does not exist yet — click Refresh Prices once before publishing.",
        }), 400

    if not (REPO_ROOT / ".git").exists():
        return jsonify({
            "ok": False,
            "stage": "preflight",
            "error": "This directory is not a git repository. Run `git init` and configure a remote first.",
        }), 400

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"publish: {ts}"

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [git, *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    # Stage both the static export and the snapshot history.
    snap_arg = "price_history.json" if Path(SNAPSHOT_FILE).exists() else None
    add_args = ["add", "docs/"] + ([snap_arg] if snap_arg else [])
    add = run(*add_args)
    if add.returncode != 0:
        return jsonify({"ok": False, "stage": "git add", "error": (add.stderr or add.stdout).strip()}), 500

    commit = run("commit", "-m", msg)
    nothing_to_commit = (
        commit.returncode != 0
        and "nothing to commit" in ((commit.stdout or "") + (commit.stderr or "")).lower()
    )
    if commit.returncode != 0 and not nothing_to_commit:
        return jsonify({"ok": False, "stage": "git commit", "error": (commit.stderr or commit.stdout).strip()}), 500

    push = run("push")
    if push.returncode != 0:
        return jsonify({
            "ok": False,
            "stage": "git push",
            "error": (push.stderr or push.stdout).strip(),
            "commit_made": not nothing_to_commit,
        }), 500

    return jsonify({
        "ok": True,
        "message": msg,
        "nothing_to_commit": nothing_to_commit,
        "push_output": (push.stdout + push.stderr).strip(),
    })


@app.route("/api/debug")
def api_debug():
    """Diagnose a single eBay fetch — useful when prices come back empty.

    Usage: /api/debug?q=RTX+4070&condition=used
           /api/debug?q=Corsair+Vengeance+32GB+DDR5+6000&condition=new
    """
    from flask import request as flask_req
    q = flask_req.args.get("q", "RTX 4070")
    condition = flask_req.args.get("condition", "used")
    excludes = flask_req.args.get("exclude", "laptop,mobile,bundle").split(",")

    result = fetch_ebay(q, excludes, condition)
    # Don't dump the full HTML, just the diagnostic
    return jsonify({
        "query": q,
        "condition": condition,
        "domain": EBAY_DOMAIN,
        "url": result.get("url"),
        "error": result.get("error"),
        "diagnostic": result.get("diag"),
        "prices_extracted": result["prices"],
        "median": round(median(result["prices"]), 2) if result["prices"] else None,
    })


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- STATIC-INJECT -->
<title>HW//INDEX · live hardware pricing</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0d10;
    --bg-1: #0f1317;
    --bg-2: #151a20;
    --line: #1e252d;
    --line-2: #2a333c;
    --fg: #d8dde3;
    --fg-dim: #7a8590;
    --fg-faint: #4a525c;
    --amber: #e8b04b;
    --amber-dim: #a67a1f;
    --green: #7cc67c;
    --red: #d96666;
    --blue: #6fa8d6;
    --mono: 'JetBrains Mono', ui-monospace, monospace;
    --display: 'Space Grotesk', system-ui, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: var(--bg); color: var(--fg); font-family: var(--mono); font-size: 13px; line-height: 1.5; }
  body {
    min-height: 100vh;
    background-image:
      linear-gradient(rgba(232, 176, 75, 0.015) 1px, transparent 1px),
      linear-gradient(90deg, rgba(232, 176, 75, 0.015) 1px, transparent 1px);
    background-size: 32px 32px;
  }
  .container { max-width: 1480px; margin: 0 auto; padding: 24px; }

  /* ---- Header ---- */
  header {
    display: flex; justify-content: space-between; align-items: flex-end;
    padding-bottom: 20px; margin-bottom: 24px;
    border-bottom: 1px solid var(--line);
  }
  .brand { display: flex; align-items: baseline; gap: 14px; }
  .brand h1 {
    font-family: var(--display); font-weight: 700; font-size: 28px;
    letter-spacing: -0.02em; color: var(--fg);
  }
  .brand h1 .slash { color: var(--amber); }
  .brand .subtitle {
    color: var(--fg-dim); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.15em;
  }
  .header-right { display: flex; align-items: center; gap: 18px; }
  .status-block {
    text-align: right; font-size: 11px; color: var(--fg-dim);
    text-transform: uppercase; letter-spacing: 0.1em;
  }
  .status-block .ts { color: var(--fg); font-weight: 500; margin-top: 2px; }
  .status-block .live-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--fg-faint); margin-right: 6px; vertical-align: middle;
  }
  .status-block.fresh .live-dot { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-block.refreshing .live-dot { background: var(--amber); animation: pulse 1s infinite; }
  @keyframes pulse { 50% { opacity: 0.3; } }

  button.refresh-btn, button.publish-btn {
    background: var(--amber); color: var(--bg); border: 0;
    padding: 10px 20px; font-family: var(--mono); font-size: 12px;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.12em;
    cursor: pointer; transition: all 0.15s;
  }
  button.refresh-btn:hover:not(:disabled),
  button.publish-btn:hover:not(:disabled) { background: #f5c566; transform: translateY(-1px); }
  button.refresh-btn:disabled, button.publish-btn:disabled {
    background: var(--line-2); color: var(--fg-dim); cursor: not-allowed;
  }
  button.refresh-btn .arrow { display: inline-block; margin-right: 6px; }
  button.refresh-btn.spinning .arrow { animation: spin 1.2s linear infinite; display: inline-block; }
  button.publish-btn {
    background: transparent; color: var(--amber);
    border: 1px solid var(--amber-dim);
  }
  button.publish-btn:hover:not(:disabled) {
    background: var(--amber); color: var(--bg);
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .readonly-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 10px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.15em; color: var(--fg-dim);
    border: 1px solid var(--line-2); background: var(--bg-1);
  }
  .readonly-pill .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--green); box-shadow: 0 0 4px var(--green);
  }
  .readonly-pill b { color: var(--fg); font-weight: 500; }

  .publish-result {
    margin-bottom: 16px; padding: 12px 16px; font-size: 11px;
    border: 1px solid var(--line-2); background: var(--bg-1);
    white-space: pre-wrap; word-break: break-word;
    font-family: var(--mono);
  }
  .publish-result.ok { border-color: #4a7a4a; color: var(--green); }
  .publish-result.fail { border-color: #844; color: var(--red); }
  .publish-result .stage {
    color: var(--fg); text-transform: uppercase;
    letter-spacing: 0.12em; font-size: 10px; margin-bottom: 6px;
  }
  .publish-result .close {
    float: right; cursor: pointer; color: var(--fg-faint);
  }

  /* ---- Tabs ---- */
  .tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 1px solid var(--line); }
  .tab {
    background: none; border: 0; color: var(--fg-dim);
    padding: 12px 24px; font-family: var(--mono); font-size: 12px;
    font-weight: 500; text-transform: uppercase; letter-spacing: 0.12em;
    cursor: pointer; border-bottom: 2px solid transparent;
    margin-bottom: -1px;
  }
  .tab:hover { color: var(--fg); }
  .tab.active { color: var(--amber); border-bottom-color: var(--amber); }
  .tab .count { color: var(--fg-faint); font-size: 10px; margin-left: 6px; }

  /* ---- Filters ---- */
  .filters {
    display: flex; gap: 14px; flex-wrap: wrap; align-items: center;
    padding: 14px 16px; background: var(--bg-1); border: 1px solid var(--line);
    margin-bottom: 16px;
  }
  .filter-group { display: flex; align-items: center; gap: 8px; }
  .filter-label {
    font-size: 10px; color: var(--fg-dim); text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  .filter-select, .filter-input {
    background: var(--bg); color: var(--fg); border: 1px solid var(--line-2);
    padding: 6px 10px; font-family: var(--mono); font-size: 12px;
    min-width: 120px;
  }
  .filter-input { min-width: 180px; }
  .filter-input::placeholder { color: var(--fg-faint); }

  /* ---- Progress ---- */
  .progress {
    display: none; margin-bottom: 16px; padding: 12px 16px;
    background: var(--bg-1); border: 1px solid var(--amber-dim);
    font-size: 11px; color: var(--fg-dim); text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .progress.active { display: block; }
  .progress .bar-wrap { height: 2px; background: var(--line); margin-top: 8px; }
  .progress .bar { height: 100%; background: var(--amber); transition: width 0.3s; width: 0; }
  .progress .label { color: var(--amber); }

  /* ---- Table ---- */
  .table-wrap { overflow-x: auto; border: 1px solid var(--line); background: var(--bg-1); }
  table { width: 100%; border-collapse: collapse; min-width: 1200px; }
  thead th {
    background: var(--bg-2); color: var(--fg-dim);
    font-size: 10px; font-weight: 500; text-transform: uppercase;
    letter-spacing: 0.12em; text-align: left;
    padding: 12px 10px; border-bottom: 1px solid var(--line);
    white-space: nowrap; cursor: pointer; user-select: none;
  }
  thead th:hover { color: var(--fg); }
  thead th.sorted { color: var(--amber); }
  thead th .sort-ind { color: var(--amber); margin-left: 4px; font-size: 9px; }
  th.num, td.num { text-align: right; }
  tbody tr { border-bottom: 1px solid var(--line); }
  tbody tr:hover { background: var(--bg-2); }
  tbody td {
    padding: 11px 10px; font-size: 12px; white-space: nowrap;
    vertical-align: middle;
  }
  td.name { font-weight: 500; color: var(--fg); }
  td.brand { color: var(--fg-dim); font-size: 11px; }

  .tier-badge {
    display: inline-block; padding: 2px 8px; font-size: 9px;
    text-transform: uppercase; letter-spacing: 0.1em; font-weight: 500;
    border: 1px solid; border-radius: 2px;
  }
  .tier-flagship  { color: #e8a5c6; border-color: #c46a94; }
  .tier-high-end  { color: var(--amber); border-color: var(--amber-dim); }
  .tier-mid-range { color: var(--blue); border-color: #3d6680; }
  .tier-budget    { color: var(--green); border-color: #4a7a4a; }

  .price-main { color: var(--fg); font-weight: 500; }
  .price-range { color: var(--fg-faint); font-size: 10px; margin-top: 2px; }
  .price-empty { color: var(--fg-faint); }

  .trend { display: inline-block; font-size: 10px; margin-left: 6px; font-weight: 500; }
  .trend.up { color: var(--red); }
  .trend.down { color: var(--green); }

  .bench-bar {
    display: inline-block; height: 4px; background: var(--line-2);
    min-width: 60px; margin-left: 8px; vertical-align: middle; position: relative;
  }
  .bench-bar .fill { position: absolute; inset: 0 auto 0 0; background: var(--amber); }

  .value-highlight { color: var(--green); font-weight: 500; }
  .value-poor { color: var(--red); }

  .error-cell { color: var(--red); font-size: 10px; }
  .stale-cell { color: var(--fg-faint); font-size: 10px; font-style: italic; }
  .price-main.stale { color: var(--fg-dim); }
  .stale-tag {
    display: inline-block; font-size: 9px; color: var(--fg-faint);
    text-transform: uppercase; letter-spacing: 0.1em; margin-left: 4px;
    padding: 1px 5px; border: 1px solid var(--line-2); border-radius: 2px;
  }

  footer {
    margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--line);
    color: var(--fg-faint); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.1em; text-align: center;
  }
  .empty-state {
    padding: 80px 20px; text-align: center; color: var(--fg-dim);
  }
  .empty-state .big { font-size: 14px; color: var(--amber); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.15em; }

  /* ---- Index tab ---- */
  .hero-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
    margin-bottom: 16px;
  }
  .hero-card {
    background: var(--bg-1); border: 1px solid var(--line);
    padding: 18px 18px 14px;
    position: relative; overflow: hidden;
  }
  .hero-card::before {
    content: ''; position: absolute; top: 0; left: 0; height: 2px; width: 100%;
    background: var(--amber);
  }
  .hero-card.green::before { background: var(--green); }
  .hero-card.red::before { background: var(--red); }
  .hero-card .label {
    color: var(--fg-dim); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.15em; margin-bottom: 8px;
  }
  .hero-card .value {
    font-family: var(--display); font-size: 36px; font-weight: 700;
    letter-spacing: -0.02em; color: var(--fg); line-height: 1;
  }
  .hero-card .value.green { color: var(--green); }
  .hero-card .value.red { color: var(--red); }
  .hero-card .delta {
    color: var(--fg-dim); font-size: 11px; margin-top: 4px; margin-bottom: 10px;
  }
  .hero-card .delta .up { color: var(--red); }
  .hero-card .delta .down { color: var(--green); }
  .hero-card .meta {
    color: var(--fg-faint); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.1em; margin-top: 8px;
  }
  .hero-card .sparkline {
    margin-top: 10px; height: 28px; width: 100%; display: block;
  }
  .hero-card.empty .value { color: var(--fg-faint); font-size: 18px; }

  /* ---- Chart ---- */
  .chart-card {
    background: var(--bg-1); border: 1px solid var(--line);
    padding: 18px 20px; margin-bottom: 16px;
  }
  .chart-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px;
  }
  .chart-title {
    font-size: 11px; color: var(--fg-dim); text-transform: uppercase;
    letter-spacing: 0.15em;
  }
  .chart-legend {
    display: flex; gap: 16px; font-size: 10px; color: var(--fg-dim);
    text-transform: uppercase; letter-spacing: 0.1em;
  }
  .chart-legend .dot {
    display: inline-block; width: 8px; height: 8px; margin-right: 6px;
    vertical-align: middle;
  }
  #index-chart { width: 100%; height: 340px; display: block; }
  #index-chart .grid line { stroke: var(--line); stroke-width: 1; }
  #index-chart .axis text { fill: var(--fg-faint); font-size: 10px; font-family: var(--mono); }
  #index-chart .ref-line { stroke: var(--amber-dim); stroke-dasharray: 3,3; stroke-width: 1; opacity: 0.5; }
  #index-chart .ref-label { fill: var(--amber-dim); font-size: 9px; font-family: var(--mono); }
  #index-chart .series { fill: none; stroke-width: 1.5; }
  #index-chart .series-dot { stroke: var(--bg-1); stroke-width: 1.5; }
  #index-chart .crosshair { stroke: var(--fg-dim); stroke-dasharray: 2,3; stroke-width: 1; opacity: 0; }
  #index-chart.hovering .crosshair { opacity: 0.6; }
  .chart-tooltip {
    position: absolute; background: var(--bg-2); border: 1px solid var(--line-2);
    padding: 8px 10px; font-size: 10px; color: var(--fg);
    pointer-events: none; opacity: 0; transition: opacity 0.1s;
    min-width: 160px; z-index: 5;
  }
  .chart-tooltip.visible { opacity: 1; }
  .chart-tooltip .ts { color: var(--fg-dim); margin-bottom: 4px; font-size: 9px; text-transform: uppercase; letter-spacing: 0.1em; }
  .chart-tooltip .row { display: flex; justify-content: space-between; gap: 12px; padding: 1px 0; }
  .chart-tooltip .row .name { color: var(--fg-dim); }
  .chart-tooltip .row .val { color: var(--fg); font-weight: 500; }

  /* ---- Lower grid: tier breakdown + leaderboards ---- */
  .lower-grid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px;
    margin-bottom: 12px;
  }
  @media (max-width: 980px) {
    .lower-grid { grid-template-columns: 1fr; }
    .hero-grid { grid-template-columns: repeat(2, 1fr) !important; }
  }
  .chart-card.full { grid-column: 1 / -1; }
  .chart-card .chart-sub {
    font-size: 10px; color: var(--fg-faint); text-transform: uppercase;
    letter-spacing: 0.1em; margin-top: 8px;
  }

  /* ---- Horizontal bar list (used by spread, confidence, generation, momentum) ---- */
  .hbar-list { display: flex; flex-direction: column; gap: 6px; }
  .hbar-row {
    display: grid; grid-template-columns: 150px 1fr 90px;
    gap: 10px; align-items: center; font-size: 11px;
  }
  .hbar-row .lbl { color: var(--fg); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hbar-row .val { color: var(--fg-dim); text-align: right; font-variant-numeric: tabular-nums; }
  .hbar-track {
    position: relative; height: 14px; background: var(--bg-2);
    border: 1px solid var(--line);
  }
  .hbar-track .seg {
    position: absolute; top: 0; bottom: 0;
  }
  .hbar-track .seg.new  { background: rgba(232, 176, 75, 0.55); }
  .hbar-track .seg.used { background: rgba(124, 198, 124, 0.55); }
  .hbar-track .seg.solid-amber { background: var(--amber); }
  .hbar-track .seg.solid-green { background: var(--green); }
  .hbar-track .seg.solid-blue  { background: var(--blue); }
  .hbar-track .midmark {
    position: absolute; top: -2px; bottom: -2px; width: 1px;
    background: var(--fg-faint);
  }

  /* ---- Heatmap-style table for premium grid ---- */
  .heat-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .heat-table th {
    background: transparent; color: var(--fg-faint); font-size: 10px;
    text-align: left; padding: 8px 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.1em;
    border-bottom: 1px solid var(--line);
  }
  .heat-table th.num { text-align: right; }
  .heat-table td {
    padding: 6px 10px; border-bottom: 1px solid var(--line);
    font-variant-numeric: tabular-nums;
  }
  .heat-table td.heat { text-align: right; font-weight: 500; }
  .heat-table tr:last-child td { border-bottom: 0; }

  /* ---- Tier-budget cards (best value per band) ---- */
  .budget-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px;
  }
  .budget-card {
    background: var(--bg-2); border: 1px solid var(--line);
    padding: 12px 14px;
  }
  .budget-card .band {
    color: var(--fg-dim); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.12em; margin-bottom: 6px;
  }
  .budget-card .pick {
    font-family: var(--display); font-size: 16px; font-weight: 700;
    color: var(--fg); margin-bottom: 4px;
  }
  .budget-card .stats { font-size: 10px; color: var(--fg-dim); }
  .budget-card .stats b { color: var(--amber); font-weight: 500; }
  .budget-card.empty .pick { color: var(--fg-faint); font-size: 12px; font-weight: 400; }

  /* ---- Headline number tiles (DDR4 vs DDR5 etc) ---- */
  .duo-tiles {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px;
  }
  .duo-tile {
    background: var(--bg-2); border: 1px solid var(--line);
    padding: 18px 18px;
  }
  .duo-tile .lbl {
    font-size: 10px; color: var(--fg-dim); text-transform: uppercase;
    letter-spacing: 0.15em; margin-bottom: 8px;
  }
  .duo-tile .big {
    font-family: var(--display); font-size: 30px; font-weight: 700;
    color: var(--fg); line-height: 1;
  }
  .duo-tile .sub { font-size: 10px; color: var(--fg-faint); margin-top: 6px; }
  .duo-tile.ddr5 .big { color: var(--blue); }
  .duo-tile.ddr4 .big { color: var(--amber); }
  .panel {
    background: var(--bg-1); border: 1px solid var(--line);
    min-width: 0; overflow: hidden;
  }
  .panel-header {
    padding: 12px 16px; border-bottom: 1px solid var(--line);
    font-size: 10px; color: var(--fg-dim); text-transform: uppercase;
    letter-spacing: 0.15em;
  }
  .panel-header .accent { color: var(--amber); }
  .panel-body { padding: 4px 0; }
  .panel-body table {
    width: 100%; min-width: 0; max-width: 100%;
    table-layout: fixed; border-collapse: collapse;
  }
  .panel-body th {
    background: transparent; padding: 8px 12px; font-size: 9px;
    color: var(--fg-faint); border-bottom: 1px solid var(--line);
    white-space: normal;
  }
  /* Override the global tbody td { white-space: nowrap } that the
     main GPU/RAM tables need but causes the index-tab panels to
     overflow horizontally on narrow viewports / Pages. */
  .panel-body td {
    padding: 8px 12px; font-size: 11px; border-bottom: 1px solid var(--line);
    white-space: normal; word-break: break-word; overflow-wrap: anywhere;
    vertical-align: top;
  }
  .panel-body tr:last-child td { border-bottom: 0; }
  /* Tier panel: 3 columns, give each ~33% so the ratio bar + label can wrap inside */
  .panel-body table.tier-table col.col-tier { width: 28%; }
  .panel-body table.tier-table col.col-side { width: 36%; }
  /* Leader panel: name | metric | price */
  .panel-body table.leader-table col.col-name  { width: 56%; }
  .panel-body table.leader-table col.col-val   { width: 26%; }
  .panel-body table.leader-table col.col-price { width: 18%; }
  .panel-body .price-range {
    display: block; white-space: normal; word-break: break-word;
  }
  .panel-body .rank {
    display: inline-block; width: 18px; color: var(--fg-faint); text-align: right;
    margin-right: 8px;
  }
  .ratio-bar {
    display: block; width: 100%; max-width: 80px; height: 4px;
    background: var(--line-2); margin: 4px 0; vertical-align: middle;
    position: relative;
  }
  .ratio-bar .fill {
    position: absolute; top: 0; bottom: 0; background: var(--amber);
  }
  .ratio-bar .fill.over  { background: var(--red); left: 50%; }
  .ratio-bar .fill.under { background: var(--green); right: 50%; }
  .ratio-bar .midline {
    position: absolute; top: -2px; bottom: -2px; left: 50%; width: 1px;
    background: var(--fg-faint);
  }
  .stat-pill {
    display: inline-block; padding: 2px 8px; font-size: 10px;
    background: var(--bg-2); border: 1px solid var(--line-2);
    color: var(--fg-dim); border-radius: 2px; margin-right: 4px;
  }
  .stat-pill.green { color: var(--green); border-color: #4a7a4a; }
  .stat-pill.red { color: var(--red); border-color: #844; }
  .panel.full { grid-column: 1 / -1; }
  .discount-banner {
    background: var(--bg-2); border: 1px solid var(--line);
    padding: 14px 18px; margin-bottom: 16px;
    display: flex; gap: 32px; align-items: center; flex-wrap: wrap;
  }
  .discount-banner .item { display: flex; flex-direction: column; gap: 4px; }
  .discount-banner .item .lbl {
    font-size: 10px; color: var(--fg-dim); text-transform: uppercase;
    letter-spacing: 0.12em;
  }
  .discount-banner .item .val {
    font-family: var(--display); font-size: 22px; font-weight: 700; color: var(--green);
  }
  .index-empty {
    padding: 100px 20px; text-align: center; color: var(--fg-dim);
    background: var(--bg-1); border: 1px solid var(--line);
  }
  .index-empty .big { font-size: 14px; color: var(--amber); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.15em; }
  .snapshot-hint {
    font-size: 10px; color: var(--fg-faint); text-align: right; margin-top: 6px;
    text-transform: uppercase; letter-spacing: 0.1em;
  }

  /* ---- Benchmark legend ---- */
  .legend {
    display: flex; gap: 24px; flex-wrap: wrap;
    padding: 14px 16px; background: var(--bg-1); border: 1px solid var(--line);
    border-top: 0; margin-bottom: 16px; font-size: 10px; color: var(--fg-dim);
    text-transform: uppercase; letter-spacing: 0.1em;
  }
  .legend b { color: var(--fg); font-weight: 500; }
</style>
</head>
<body>
<div class="container">

  <header>
    <div class="brand">
      <h1>HW<span class="slash">//</span>INDEX</h1>
      <span class="subtitle">live hardware pricing · ebay · benchmark-weighted</span>
    </div>
    <div class="header-right">
      <div class="readonly-pill" id="readonly-pill" style="display:none">
        <span class="dot"></span><span>READ-ONLY · UPDATED <b id="readonly-age">—</b></span>
      </div>
      <div class="status-block" id="status">
        <div><span class="live-dot"></span>Never refreshed</div>
        <div class="ts" id="ts">—</div>
      </div>
      <button class="refresh-btn" id="refresh-btn">
        <span class="arrow">↻</span>Refresh Prices
      </button>
      <button class="publish-btn" id="publish-btn" title="Commit docs/ + price_history.json and push to GitHub">
        Publish
      </button>
    </div>
  </header>

  <div id="publish-result-mount"></div>

  <div class="tabs">
    <button class="tab active" data-tab="gpu">GPU<span class="count" id="gpu-count"></span></button>
    <button class="tab" data-tab="ram">RAM<span class="count" id="ram-count"></span></button>
    <button class="tab" data-tab="index">Index<span class="count" id="index-count"></span></button>
  </div>

  <div class="progress" id="progress">
    <span class="label" id="progress-label">Fetching...</span>
    <span id="progress-current" style="margin-left: 12px; color: var(--fg);"></span>
    <span style="float: right;" id="progress-count"></span>
    <div class="bar-wrap"><div class="bar" id="progress-bar"></div></div>
  </div>

  <div class="filters" id="filters-gpu">
    <div class="filter-group">
      <span class="filter-label">Search</span>
      <input type="text" class="filter-input" id="gpu-search" placeholder="e.g. 4070, AMD, RX...">
    </div>
    <div class="filter-group">
      <span class="filter-label">Brand</span>
      <select class="filter-select" id="gpu-brand">
        <option value="">All</option><option>NVIDIA</option><option>AMD</option><option>Intel</option>
      </select>
    </div>
    <div class="filter-group">
      <span class="filter-label">Tier</span>
      <select class="filter-select" id="gpu-tier">
        <option value="">All</option><option>flagship</option><option>high-end</option><option>mid-range</option><option>budget</option>
      </select>
    </div>
  </div>

  <div class="filters" id="filters-ram" style="display:none">
    <div class="filter-group">
      <span class="filter-label">Search</span>
      <input type="text" class="filter-input" id="ram-search" placeholder="e.g. DDR5, 32GB, Corsair...">
    </div>
    <div class="filter-group">
      <span class="filter-label">Gen</span>
      <select class="filter-select" id="ram-gen">
        <option value="">All</option><option>DDR5</option><option>DDR4</option>
      </select>
    </div>
    <div class="filter-group">
      <span class="filter-label">Capacity</span>
      <select class="filter-select" id="ram-capacity">
        <option value="">All</option><option value="16">16 GB</option><option value="32">32 GB</option><option value="64">64 GB</option>
      </select>
    </div>
  </div>

  <div class="legend" id="legend-gpu">
    <span><b>BENCHMARK:</b> 3DMark Time Spy Graphics (approx)</span>
    <span><b>PERF/$:</b> benchmark score per USD · higher = better value</span>
    <span><b>PRICE:</b> median of matched eBay listings (active new · sold used)</span>
    <span><b>TREND:</b> % change vs previous refresh</span>
  </div>
  <div class="legend" id="legend-ram" style="display:none">
    <span><b>SPEED:</b> MT/s effective data rate</span>
    <span><b>$/GB:</b> cost per gigabyte · lower = better value</span>
    <span><b>PRICE:</b> median of matched eBay listings (active new · sold used)</span>
    <span><b>TREND:</b> % change vs previous refresh</span>
  </div>

  <div id="tab-gpu" class="tab-content">
    <div class="table-wrap">
      <table id="gpu-table">
        <thead><tr>
          <th data-sort="benchmark" class="sorted">Model <span class="sort-ind">▼</span></th>
          <th>Tier</th>
          <th class="num" data-sort="benchmark">Benchmark</th>
          <th class="num" data-sort="msrp">MSRP</th>
          <th class="num" data-sort="new.median">New (median)</th>
          <th class="num" data-sort="used.median">Used (median)</th>
          <th class="num" data-sort="perf_per_dollar_new">Perf/$ (new)</th>
          <th class="num" data-sort="perf_per_dollar_used">Perf/$ (used)</th>
          <th>VRAM</th>
          <th class="num" data-sort="year">Year</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div id="tab-ram" class="tab-content" style="display:none">
    <div class="table-wrap">
      <table id="ram-table">
        <thead><tr>
          <th data-sort="speed" class="sorted">Kit <span class="sort-ind">▼</span></th>
          <th>Tier</th>
          <th class="num" data-sort="capacity">Capacity</th>
          <th class="num" data-sort="speed">Speed</th>
          <th>Gen</th>
          <th class="num" data-sort="msrp">MSRP</th>
          <th class="num" data-sort="new.median">New (median)</th>
          <th class="num" data-sort="used.median">Used (median)</th>
          <th class="num" data-sort="cost_per_gb_new">$/GB (new)</th>
          <th class="num" data-sort="cost_per_gb_used">$/GB (used)</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div id="tab-index" class="tab-content" style="display:none">
    <div id="index-content">
      <div class="index-empty">
        <div class="big">Loading index data...</div>
      </div>
    </div>
  </div>

  <footer>
    HW//INDEX · data scraped from eBay on refresh · benchmarks are approximate · not financial advice
  </footer>
</div>

<script>
// ---- Static / live mode ----
// In static mode (GitHub Pages export) we read JSON files committed alongside
// index.html and hide owner-only controls. window.HW_STATIC is injected by
// export_static_site() before <head> closes.
const STATIC = !!window.HW_STATIC;
const URL_DATA       = STATIC ? './data.json'        : '/api/data';
const URL_AGGREGATES = STATIC ? './aggregates.json'  : '/api/aggregates';
const URL_REFRESH    = '/api/refresh';
const URL_PUBLISH    = '/api/publish';

// ---- State ----
let state = {
  activeTab: 'gpu',
  data: { gpu: [], ram: [] },
  aggregates: null,
  sort: { gpu: { key: 'benchmark', dir: -1 }, ram: { key: 'speed', dir: -1 } },
  maxBench: 1,
  bestPerfNew: 0,
  bestPerfUsed: 0,
  bestCostPerGbNew: Infinity,
  bestCostPerGbUsed: Infinity,
};

// ---- Helpers ----
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

function fmtPrice(p) {
  return p == null ? '<span class="price-empty">—</span>' : '£' + p.toFixed(2);
}
function fmtStatus(status, count) {
  if (count > 0) return '';
  if (status === 'error')      return '<div class="error-cell">network err</div>';
  if (status === 'challenge')  return '<div class="error-cell">blocked / captcha</div>';
  if (status === 'no-items')   return '<div class="error-cell">no listings on page</div>';
  if (status === 'no-match')   return '<div class="error-cell">no matches in listings</div>';
  if (status === 'skipped')    return '<div class="error-cell">skipped (rate-limited)</div>';
  if (status === 'stale')      return '<div class="stale-cell">stale</div>';
  return '';
}
// For rendering a price cell when we have count > 0 but the data is stale
function fmtPriceCell(side, trend, statusVal) {
  const isStale = statusVal === 'stale';
  const cls = isStale ? 'price-main stale' : 'price-main';
  return `<div class="${cls}">${fmtPrice(side.median)}${isStale ? ' <span class="stale-tag">stale</span>' : fmtTrend(trend)}</div>`;
}
function fmtTrend(t) {
  if (t == null) return '';
  if (Math.abs(t) < 0.5) return ' <span class="trend" style="color:var(--fg-faint)">◆</span>';
  const cls = t > 0 ? 'up' : 'down';
  const arr = t > 0 ? '▲' : '▼';
  return ` <span class="trend ${cls}">${arr}${Math.abs(t).toFixed(1)}%</span>`;
}
function getNested(obj, path) {
  return path.split('.').reduce((a, k) => a == null ? null : a[k], obj);
}
function relTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

// ---- Render ----
function renderGPU(items) {
  state.maxBench = Math.max(...items.map(i => i.benchmark || 0), 1);
  state.bestPerfNew = Math.max(...items.map(i => i.perf_per_dollar_new || 0), 0);
  state.bestPerfUsed = Math.max(...items.map(i => i.perf_per_dollar_used || 0), 0);

  const tbody = $('#gpu-table tbody');
  tbody.innerHTML = items.map(g => {
    const benchPct = Math.min(100, (g.benchmark / state.maxBench) * 100);
    const ppn = g.perf_per_dollar_new;
    const ppu = g.perf_per_dollar_used;
    const ppnCls = ppn && ppn >= state.bestPerfNew * 0.85 ? 'value-highlight' : '';
    const ppuCls = ppu && ppu >= state.bestPerfUsed * 0.85 ? 'value-highlight' : '';
    return `
      <tr>
        <td class="name">${g.name}<br><span class="brand">${g.brand}</span></td>
        <td><span class="tier-badge tier-${g.tier}">${g.tier}</span></td>
        <td class="num">
          ${g.benchmark.toLocaleString()}
          <span class="bench-bar"><span class="fill" style="width:${benchPct}%"></span></span>
        </td>
        <td class="num">£${g.msrp}</td>
        <td class="num">
          ${fmtPriceCell(g.new, g.trend_new, g.status?.new)}
          ${g.new.count ? `<div class="price-range">n=${g.new.count} · £${g.new.low}–£${g.new.high}</div>` : fmtStatus(g.status?.new, 0)}
        </td>
        <td class="num">
          ${fmtPriceCell(g.used, g.trend_used, g.status?.used)}
          ${g.used.count ? `<div class="price-range">n=${g.used.count} · £${g.used.low}–£${g.used.high}</div>` : fmtStatus(g.status?.used, 0)}
        </td>
        <td class="num ${ppnCls}">${ppn ? ppn.toFixed(1) : '—'}</td>
        <td class="num ${ppuCls}">${ppu ? ppu.toFixed(1) : '—'}</td>
        <td>${g.vram}</td>
        <td class="num">${g.year}</td>
      </tr>
    `;
  }).join('');
}

function renderRAM(items) {
  state.bestCostPerGbNew = Math.min(...items.map(i => i.cost_per_gb_new || Infinity), Infinity);
  state.bestCostPerGbUsed = Math.min(...items.map(i => i.cost_per_gb_used || Infinity), Infinity);

  const tbody = $('#ram-table tbody');
  tbody.innerHTML = items.map(r => {
    const cpn = r.cost_per_gb_new;
    const cpu = r.cost_per_gb_used;
    const cpnCls = cpn && cpn <= state.bestCostPerGbNew * 1.15 ? 'value-highlight' : '';
    const cpuCls = cpu && cpu <= state.bestCostPerGbUsed * 1.15 ? 'value-highlight' : '';
    return `
      <tr>
        <td class="name">${r.name}<br><span class="brand">${r.brand}</span></td>
        <td><span class="tier-badge tier-${r.tier}">${r.tier}</span></td>
        <td class="num">${r.capacity} GB</td>
        <td class="num">${r.speed.toLocaleString()} MT/s</td>
        <td>${r.gen}</td>
        <td class="num">£${r.msrp}</td>
        <td class="num">
          ${fmtPriceCell(r.new, r.trend_new, r.status?.new)}
          ${r.new.count ? `<div class="price-range">n=${r.new.count} · £${r.new.low}–£${r.new.high}</div>` : fmtStatus(r.status?.new, 0)}
        </td>
        <td class="num">
          ${fmtPriceCell(r.used, r.trend_used, r.status?.used)}
          ${r.used.count ? `<div class="price-range">n=${r.used.count} · £${r.used.low}–£${r.used.high}</div>` : fmtStatus(r.status?.used, 0)}
        </td>
        <td class="num ${cpnCls}">${cpn ? '£' + cpn.toFixed(2) : '—'}</td>
        <td class="num ${cpuCls}">${cpu ? '£' + cpu.toFixed(2) : '—'}</td>
      </tr>
    `;
  }).join('');
}

// ---- Filter + sort ----
function getFilteredGPU() {
  const q = $('#gpu-search').value.toLowerCase().trim();
  const brand = $('#gpu-brand').value;
  const tier = $('#gpu-tier').value;
  let list = state.data.gpu.filter(g => {
    if (q && !(g.name.toLowerCase().includes(q) || g.brand.toLowerCase().includes(q))) return false;
    if (brand && g.brand !== brand) return false;
    if (tier && g.tier !== tier) return false;
    return true;
  });
  return sortItems(list, 'gpu');
}

function getFilteredRAM() {
  const q = $('#ram-search').value.toLowerCase().trim();
  const gen = $('#ram-gen').value;
  const cap = $('#ram-capacity').value;
  let list = state.data.ram.filter(r => {
    if (q && !(r.name.toLowerCase().includes(q) || r.brand.toLowerCase().includes(q))) return false;
    if (gen && r.gen !== gen) return false;
    if (cap && String(r.capacity) !== cap) return false;
    return true;
  });
  return sortItems(list, 'ram');
}

function sortItems(items, kind) {
  const { key, dir } = state.sort[kind];
  return [...items].sort((a, b) => {
    const av = getNested(a, key);
    const bv = getNested(b, key);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    return (av - bv) * dir;
  });
}

function refreshTable() {
  if (state.activeTab === 'gpu') {
    const items = getFilteredGPU();
    renderGPU(items);
    $('#gpu-count').textContent = `[${items.length}/${state.data.gpu.length}]`;
  } else if (state.activeTab === 'ram') {
    const items = getFilteredRAM();
    renderRAM(items);
    $('#ram-count').textContent = `[${items.length}/${state.data.ram.length}]`;
  }
  // index tab handled separately via loadAggregates
}

// ---- Data fetch ----
async function loadData() {
  try {
    const resp = await fetch(URL_DATA, { cache: STATIC ? 'no-store' : 'default' });
    const d = await resp.json();
    state.data.gpu = d.gpu || [];
    state.data.ram = d.ram || [];

    const s = $('#status');
    if (d.refreshing) {
      s.className = 'status-block refreshing';
      s.firstElementChild.innerHTML = '<span class="live-dot"></span>Refreshing...';
      $('#ts').textContent = d.progress.current || '...';
      $('#progress').classList.add('active');
      const pct = d.progress.total ? (d.progress.done / d.progress.total * 100) : 0;
      $('#progress-bar').style.width = pct + '%';
      $('#progress-count').textContent = `${d.progress.done}/${d.progress.total}`;
      $('#progress-current').textContent = d.progress.current ? '→ ' + d.progress.current : '';
      $('#refresh-btn').classList.add('spinning');
      $('#refresh-btn').disabled = true;
    } else {
      $('#progress').classList.remove('active');
      $('#refresh-btn').classList.remove('spinning');
      $('#refresh-btn').disabled = false;
      if (d.last_refresh) {
        s.className = 'status-block fresh';
        s.firstElementChild.innerHTML = '<span class="live-dot"></span>' + (STATIC ? 'Snapshot' : 'Live');
        $('#ts').textContent = 'Updated ' + relTime(d.last_refresh);
        if (STATIC) {
          const pill = $('#readonly-pill');
          if (pill) {
            pill.style.display = '';
            $('#readonly-age').textContent = relTime(d.last_refresh).toUpperCase();
          }
        }
      }
    }
    $('#gpu-count').textContent = `[${state.data.gpu.length}]`;
    $('#ram-count').textContent = `[${state.data.ram.length}]`;
    refreshTable();
  } catch (e) {
    console.error('loadData failed', e);
  }
}

async function triggerRefresh() {
  if (STATIC) return;
  $('#refresh-btn').disabled = true;
  $('#refresh-btn').classList.add('spinning');
  try {
    await fetch(URL_REFRESH, { method: 'POST' });
  } catch (e) {
    console.error(e);
  }
  // Poll every 1.5s while refreshing
  const poll = setInterval(async () => {
    await loadData();
    const resp = await fetch(URL_DATA);
    const d = await resp.json();
    if (!d.refreshing) {
      clearInterval(poll);
      // Refresh just finished — reload aggregates so the index tab reflects the new snapshot
      if (state.activeTab === 'index') loadAggregates();
      else state.aggregates = null;  // invalidate so next switch refetches
    }
  }, 1500);
}

function showPublishResult(html, ok) {
  const mount = $('#publish-result-mount');
  mount.innerHTML = `<div class="publish-result ${ok ? 'ok' : 'fail'}">
    <span class="close" onclick="this.parentElement.remove()">✕</span>
    ${html}
  </div>`;
}

async function triggerPublish() {
  if (STATIC) return;
  const btn = $('#publish-btn');
  if (!confirm('Publish current data to GitHub Pages?\n\nThis will git add docs/ + price_history.json, commit, and push.')) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Publishing…';
  try {
    const resp = await fetch(URL_PUBLISH, { method: 'POST' });
    const r = await resp.json();
    if (r.ok) {
      const note = r.nothing_to_commit
        ? 'Nothing new to commit — push skipped or fast-forward only.'
        : 'Committed: ' + (r.message || '(no message)');
      showPublishResult(
        `<div class="stage">Published</div>${note}\n${r.push_output || ''}`.trim(),
        true,
      );
    } else {
      showPublishResult(
        `<div class="stage">Failed at ${r.stage || 'unknown'}</div>${(r.error || 'unknown error')}`,
        false,
      );
    }
  } catch (e) {
    showPublishResult(`<div class="stage">Network error</div>${e.message || e}`, false);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ---- Wire up ----
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  state.activeTab = t.dataset.tab;
  $('#tab-gpu').style.display = state.activeTab === 'gpu' ? '' : 'none';
  $('#tab-ram').style.display = state.activeTab === 'ram' ? '' : 'none';
  $('#tab-index').style.display = state.activeTab === 'index' ? '' : 'none';
  $('#filters-gpu').style.display = state.activeTab === 'gpu' ? '' : 'none';
  $('#filters-ram').style.display = state.activeTab === 'ram' ? '' : 'none';
  $('#legend-gpu').style.display = state.activeTab === 'gpu' ? '' : 'none';
  $('#legend-ram').style.display = state.activeTab === 'ram' ? '' : 'none';
  if (state.activeTab === 'index') {
    loadAggregates();
  } else {
    refreshTable();
  }
}));

['gpu-search','gpu-brand','gpu-tier','ram-search','ram-gen','ram-capacity'].forEach(id => {
  $('#' + id).addEventListener('input', refreshTable);
  $('#' + id).addEventListener('change', refreshTable);
});

document.addEventListener('click', (e) => {
  const th = e.target.closest('th[data-sort]');
  if (!th) return;
  const table = th.closest('table');
  const kind = table.id === 'gpu-table' ? 'gpu' : 'ram';
  const key = th.dataset.sort;
  if (state.sort[kind].key === key) {
    state.sort[kind].dir *= -1;
  } else {
    state.sort[kind] = { key, dir: -1 };
  }
  table.querySelectorAll('th').forEach(x => {
    x.classList.remove('sorted');
    const ind = x.querySelector('.sort-ind');
    if (ind) ind.remove();
  });
  th.classList.add('sorted');
  const ind = document.createElement('span');
  ind.className = 'sort-ind';
  ind.textContent = state.sort[kind].dir === -1 ? '▼' : '▲';
  th.appendChild(ind);
  refreshTable();
});

$('#refresh-btn').addEventListener('click', triggerRefresh);
$('#publish-btn').addEventListener('click', triggerPublish);

if (STATIC) {
  // Hide owner controls — the public site is read-only.
  ['#refresh-btn', '#publish-btn'].forEach(sel => {
    const el = $(sel);
    if (el) el.style.display = 'none';
  });
  const pill = $('#readonly-pill');
  if (pill) pill.style.display = '';
}

// ============================================================
// INDEX TAB: aggregates, hero cards, chart, leaderboards
// ============================================================

const INDEX_COLORS = {
  gpu_new:  '#e8b04b', // amber
  gpu_used: '#7cc67c', // green
  ram_new:  '#6fa8d6', // blue
  ram_used: '#c46a94', // pink
};
const INDEX_LABELS = {
  gpu_new:  'GPU NEW',
  gpu_used: 'GPU USED',
  ram_new:  'RAM NEW',
  ram_used: 'RAM USED',
};

async function loadAggregates() {
  try {
    const resp = await fetch(URL_AGGREGATES, { cache: STATIC ? 'no-store' : 'default' });
    state.aggregates = await resp.json();
    renderIndex(state.aggregates);
  } catch (e) {
    console.error('aggregates fetch failed', e);
    $('#index-content').innerHTML = '<div class="index-empty"><div class="big">Failed to load aggregates</div><div>' + (e.message || e) + '</div></div>';
  }
}

function renderIndex(data) {
  const series = data.series || [];
  const current = data.current || {};
  const validSeries = series.filter(s =>
    s.gpu_new_idx != null || s.gpu_used_idx != null ||
    s.ram_new_idx != null || s.ram_used_idx != null
  );

  if (!validSeries.length) {
    $('#index-content').innerHTML = `
      <div class="index-empty">
        <div class="big">No data yet</div>
        <div>Click <strong>Refresh Prices</strong> at least once to populate the index.</div>
      </div>`;
    $('#index-count').textContent = '';
    return;
  }

  $('#index-count').textContent = `[${validSeries.length} snapshot${validSeries.length === 1 ? '' : 's'}]`;

  const latest = validSeries[validSeries.length - 1];
  const prev = validSeries.length > 1 ? validSeries[validSeries.length - 2] : null;

  $('#index-content').innerHTML = `
    <div class="hero-grid">
      ${renderHeroCard('gpu_new',  latest, prev, validSeries)}
      ${renderHeroCard('gpu_used', latest, prev, validSeries)}
      ${renderHeroCard('ram_new',  latest, prev, validSeries)}
      ${renderHeroCard('ram_used', latest, prev, validSeries)}
    </div>

    ${renderDiscountBanner(current.discounts)}

    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">Price Index vs MSRP — over time</div>
        <div class="chart-legend">
          <span><span class="dot" style="background:${INDEX_COLORS.gpu_new}"></span>GPU New</span>
          <span><span class="dot" style="background:${INDEX_COLORS.gpu_used}"></span>GPU Used</span>
          <span><span class="dot" style="background:${INDEX_COLORS.ram_new}"></span>RAM New</span>
          <span><span class="dot" style="background:${INDEX_COLORS.ram_used}"></span>RAM Used</span>
        </div>
      </div>
      <div style="position:relative">
        <svg id="index-chart" viewBox="0 0 1400 340" preserveAspectRatio="none"></svg>
        <div class="chart-tooltip" id="chart-tooltip"></div>
      </div>
      <div class="snapshot-hint">Each point = one refresh · 1.0 line = MSRP parity · &lt; 1.0 = market below MSRP</div>
    </div>

    <div class="lower-grid">
      ${renderTierPanel('GPU', current.tiers?.gpu_new, current.tiers?.gpu_used)}
      ${renderTierPanel('RAM', current.tiers?.ram_new, current.tiers?.ram_used)}
      ${renderLeaderPanel('GPU · TOP PERF/£', current.leaders?.gpu_used, current.leaders?.gpu_new, 'gpu')}
      ${renderLeaderPanel('RAM · LOWEST £/GB', current.leaders?.ram_used, current.leaders?.ram_new, 'ram')}
    </div>

    ${renderGenerationChart(state.data.gpu)}
    ${renderSpreadChart(state.data.gpu)}
    ${renderBudgetTierChart(state.data.gpu)}
    ${renderPremiumHeatmap(state.data.gpu)}

    <div class="lower-grid">
      ${renderRamGenTiles(state.data.ram)}
      ${renderConfidenceChart(state.data.gpu)}
    </div>
  `;

  drawChart(validSeries);
}

// ---- Task 4: appended visualisations ----

function _ratio(it, side) {
  const m = it?.[side]?.median;
  if (!m || !it.msrp) return null;
  return m / it.msrp;
}

function _genOf(gpu) {
  const n = gpu.name || '';
  if (n.startsWith('RTX 50')) return 'RTX 50-series';
  if (n.startsWith('RTX 40')) return 'RTX 40-series';
  if (n.startsWith('RTX 30')) return 'RTX 30-series';
  if (n.startsWith('RX 9'))   return 'RX 9000 (RDNA4)';
  if (n.startsWith('RX 7'))   return 'RX 7000 (RDNA3)';
  if (n.startsWith('RX 6'))   return 'RX 6000 (RDNA2)';
  if (n.startsWith('Intel'))  return 'Intel Arc';
  return 'Other';
}

function ratioPctSpan(r) {
  const p = (r - 1) * 100;
  const cls = p > 2 ? 'value-poor' : (p < -2 ? 'value-highlight' : '');
  const sign = p >= 0 ? '+' : '';
  return `<span class="${cls}">${sign}${p.toFixed(1)}%</span>`;
}

function renderGenerationChart(gpus) {
  const buckets = {};
  (gpus || []).forEach(g => {
    const gen = _genOf(g);
    const rN = _ratio(g, 'new');
    const rU = _ratio(g, 'used');
    if (rN == null && rU == null) return;
    if (!buckets[gen]) buckets[gen] = { n: [], u: [] };
    if (rN != null) buckets[gen].n.push(rN);
    if (rU != null) buckets[gen].u.push(rU);
  });
  const order = ['RTX 50-series', 'RTX 40-series', 'RTX 30-series',
                 'RX 9000 (RDNA4)', 'RX 7000 (RDNA3)', 'RX 6000 (RDNA2)', 'Intel Arc'];
  const rows = order.filter(k => buckets[k]).map(k => {
    const b = buckets[k];
    const avgN = b.n.length ? b.n.reduce((a,c)=>a+c,0)/b.n.length : null;
    const avgU = b.u.length ? b.u.reduce((a,c)=>a+c,0)/b.u.length : null;
    // Two-row visual: new bar above, used bar below — both centered on 1.0 line
    const renderBar = (r, cls) => {
      if (r == null) return '<div class="hbar-track"></div>';
      const dev = Math.max(-0.5, Math.min(0.5, r - 1));
      const w = (Math.abs(dev) / 0.5) * 50;
      const left = dev >= 0 ? 50 : (50 - w);
      return `<div class="hbar-track">
        <div class="midmark" style="left:50%"></div>
        <div class="seg ${cls}" style="left:${left}%; width:${w}%"></div>
      </div>`;
    };
    const valStr = (avgN != null ? `N ${ratioPctSpan(avgN)}` : 'N —') +
                   ' · ' +
                   (avgU != null ? `U ${ratioPctSpan(avgU)}` : 'U —');
    return `
      <div class="hbar-row">
        <div class="lbl">${k}</div>
        <div style="display:flex; flex-direction:column; gap:3px">
          ${renderBar(avgN, 'solid-amber')}
          ${renderBar(avgU, 'solid-green')}
        </div>
        <div class="val">${valStr}</div>
      </div>`;
  }).join('');

  if (!rows) return '';

  return `
    <div class="chart-card full">
      <div class="chart-header">
        <div class="chart-title">GPU Generation · avg price-to-MSRP ratio</div>
        <div class="chart-legend">
          <span><span class="dot" style="background:var(--amber)"></span>New</span>
          <span><span class="dot" style="background:var(--green)"></span>Used</span>
        </div>
      </div>
      <div class="hbar-list">${rows}</div>
      <div class="chart-sub">Bars centered on MSRP parity · right = above MSRP · left = below</div>
    </div>`;
}

function renderSpreadChart(gpus) {
  const items = (gpus || [])
    .map(g => {
      const n = g?.new?.median, u = g?.used?.median;
      if (!n || !u) return null;
      return { name: g.name, tier: g.tier, n, u, spread: n - u, pct: (n - u) / n };
    })
    .filter(Boolean)
    .sort((a, b) => b.pct - a.pct);

  if (!items.length) return '';

  const maxN = Math.max(...items.map(x => x.n));

  const rows = items.map(it => {
    const newW = (it.n / maxN) * 100;
    const usedW = (it.u / maxN) * 100;
    return `
      <div class="hbar-row">
        <div class="lbl">${it.name}</div>
        <div class="hbar-track" style="height:18px">
          <div class="seg new"  style="left:0; width:${newW}%"></div>
          <div class="seg used" style="left:0; width:${usedW}%"></div>
        </div>
        <div class="val">£${Math.round(it.u)}–£${Math.round(it.n)} · ${(it.pct*100).toFixed(0)}%</div>
      </div>`;
  }).join('');

  return `
    <div class="chart-card full">
      <div class="chart-header">
        <div class="chart-title">New vs Used Price Spread per GPU</div>
        <div class="chart-legend">
          <span><span class="dot" style="background:rgba(232,176,75,0.55)"></span>New median</span>
          <span><span class="dot" style="background:rgba(124,198,124,0.55)"></span>Used median</span>
        </div>
      </div>
      <div class="hbar-list">${rows}</div>
      <div class="chart-sub">Sorted by used-vs-new discount · wider gap = sharper depreciation</div>
    </div>`;
}

function renderBudgetTierChart(gpus) {
  const bands = [
    { label: 'Sub-£200',   min: 0,    max: 200 },
    { label: '£200–£350',  min: 200,  max: 350 },
    { label: '£350–£500',  min: 350,  max: 500 },
    { label: '£500–£800',  min: 500,  max: 800 },
    { label: '£800+',      min: 800,  max: Infinity },
  ];
  const cards = bands.map(band => {
    let bestNew = null, bestUsed = null;
    (gpus || []).forEach(g => {
      const n = g?.new?.median, u = g?.used?.median, b = g.benchmark;
      if (!b) return;
      if (n != null && n >= band.min && n < band.max) {
        const v = b / n;
        if (!bestNew || v > bestNew.v) bestNew = { name: g.name, price: n, v, side: 'new' };
      }
      if (u != null && u >= band.min && u < band.max) {
        const v = b / u;
        if (!bestUsed || v > bestUsed.v) bestUsed = { name: g.name, price: u, v, side: 'used' };
      }
    });
    const pick = (bestNew && bestUsed)
      ? (bestNew.v >= bestUsed.v ? bestNew : bestUsed)
      : (bestNew || bestUsed);
    if (!pick) {
      return `
        <div class="budget-card empty">
          <div class="band">${band.label}</div>
          <div class="pick">no listings in band</div>
        </div>`;
    }
    return `
      <div class="budget-card">
        <div class="band">${band.label}</div>
        <div class="pick">${pick.name}</div>
        <div class="stats"><b>${pick.v.toFixed(1)}</b> pts/£ · £${Math.round(pick.price)} (${pick.side})</div>
      </div>`;
  }).join('');

  return `
    <div class="chart-card full">
      <div class="chart-header">
        <div class="chart-title">Best Value GPU by Budget Tier</div>
      </div>
      <div class="budget-grid">${cards}</div>
      <div class="chart-sub">Top 3DMark Time Spy points per pound in each price band · best of new or used</div>
    </div>`;
}

function renderPremiumHeatmap(gpus) {
  const rows = (gpus || [])
    .map(g => {
      const rN = _ratio(g, 'new');
      const rU = _ratio(g, 'used');
      if (rN == null && rU == null) return null;
      return { name: g.name, tier: g.tier, msrp: g.msrp, rN, rU };
    })
    .filter(Boolean)
    .sort((a, b) => (b.rN ?? b.rU ?? 0) - (a.rN ?? a.rU ?? 0));

  if (!rows.length) return '';

  const heatColor = (r) => {
    if (r == null) return 'var(--bg-2)';
    const dev = Math.max(-0.5, Math.min(0.5, r - 1));
    if (dev > 0) {
      const a = Math.min(0.85, dev * 1.6 + 0.15);
      return `rgba(217, 102, 102, ${a.toFixed(2)})`;
    }
    const a = Math.min(0.85, -dev * 1.6 + 0.15);
    return `rgba(124, 198, 124, ${a.toFixed(2)})`;
  };

  const body = rows.map(r => `
    <tr>
      <td>${r.name}</td>
      <td><span class="tier-badge tier-${r.tier}">${r.tier}</span></td>
      <td class="num">£${r.msrp}</td>
      <td class="heat" style="background:${heatColor(r.rN)}">${r.rN != null ? ratioPctSpan(r.rN) : '<span class="price-empty">—</span>'}</td>
      <td class="heat" style="background:${heatColor(r.rU)}">${r.rU != null ? ratioPctSpan(r.rU) : '<span class="price-empty">—</span>'}</td>
    </tr>`).join('');

  return `
    <div class="chart-card full">
      <div class="chart-header">
        <div class="chart-title">Market Premium / Discount Heatmap</div>
        <div class="chart-legend">
          <span style="color:var(--red)">red = above MSRP</span>
          <span style="color:var(--green)">green = below MSRP</span>
        </div>
      </div>
      <table class="heat-table">
        <thead><tr>
          <th>GPU</th><th>Tier</th><th class="num">MSRP</th>
          <th class="num">New vs MSRP</th><th class="num">Used vs MSRP</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function renderRamGenTiles(rams) {
  const calc = (filter) => {
    const items = (rams || []).filter(filter);
    const newVals = items.map(r => r?.cost_per_gb_new).filter(v => v != null);
    const usedVals = items.map(r => r?.cost_per_gb_used).filter(v => v != null);
    const avg = arr => arr.length ? arr.reduce((a,c)=>a+c,0)/arr.length : null;
    return {
      n: items.length,
      newAvg: avg(newVals),
      usedAvg: avg(usedVals),
      newCount: newVals.length,
      usedCount: usedVals.length,
    };
  };
  const ddr4 = calc(r => r.gen === 'DDR4');
  const ddr5 = calc(r => r.gen === 'DDR5');

  const tile = (cls, label, d) => {
    if (!d.n) return `<div class="duo-tile ${cls}"><div class="lbl">${label}</div><div class="big" style="color:var(--fg-faint); font-size:14px">no kits</div></div>`;
    const fmt = v => v == null ? '—' : '£' + v.toFixed(2);
    return `
      <div class="duo-tile ${cls}">
        <div class="lbl">${label} · avg cost / GB</div>
        <div class="big">${fmt(d.newAvg)}</div>
        <div class="sub">New (n=${d.newCount}) · Used ${fmt(d.usedAvg)} (n=${d.usedCount})</div>
      </div>`;
  };

  let premium = '';
  if (ddr4.newAvg && ddr5.newAvg) {
    const pct = ((ddr5.newAvg - ddr4.newAvg) / ddr4.newAvg) * 100;
    premium = `<div class="chart-sub">DDR5 currently <b style="color:var(--fg)">${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%</b> vs DDR4 on cost per gigabyte (new market)</div>`;
  }

  return `
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">RAM · DDR4 vs DDR5 cost per GB</div>
      </div>
      <div class="duo-tiles">
        ${tile('ddr4', 'DDR4', ddr4)}
        ${tile('ddr5', 'DDR5', ddr5)}
      </div>
      ${premium}
    </div>`;
}

function renderConfidenceChart(gpus) {
  const items = (gpus || [])
    .map(g => ({
      name: g.name,
      newN: g?.new?.count ?? 0,
      usedN: g?.used?.count ?? 0,
      total: (g?.new?.count ?? 0) + (g?.used?.count ?? 0),
    }))
    .filter(x => x.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 12);

  if (!items.length) {
    return `
      <div class="chart-card">
        <div class="chart-header">
          <div class="chart-title">Listing Volume Confidence</div>
        </div>
        <div style="padding:24px;text-align:center;color:var(--fg-faint);font-size:11px">No listing counts in current snapshot</div>
      </div>`;
  }

  const maxTotal = Math.max(...items.map(x => x.total));
  const rows = items.map(it => {
    const lowConf = it.total < 5;
    const w = (it.total / maxTotal) * 100;
    return `
      <div class="hbar-row">
        <div class="lbl" style="font-size:11px">${it.name}</div>
        <div class="hbar-track">
          <div class="seg ${lowConf ? 'solid-blue' : 'solid-amber'}" style="left:0; width:${w}%; opacity:${lowConf ? 0.5 : 1}"></div>
        </div>
        <div class="val">${it.total}${lowConf ? ' ⚠' : ''}</div>
      </div>`;
  }).join('');

  return `
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">Listing Volume · Confidence (top 12)</div>
        <div class="chart-legend">
          <span style="color:var(--fg-faint)">⚠ &lt; 5 listings</span>
        </div>
      </div>
      <div class="hbar-list">${rows}</div>
    </div>`;
}

function renderHeroCard(key, latest, prev, series) {
  const val = latest[key + '_idx'];
  const avgKey = key + '_avg';
  const avgPrice = latest[avgKey];

  if (val == null) {
    return `
      <div class="hero-card empty">
        <div class="label">${INDEX_LABELS[key]} vs MSRP</div>
        <div class="value">—</div>
        <div class="delta">no data this snapshot</div>
      </div>`;
  }

  // Display as +X% / -X% from MSRP
  const pct = (val - 1) * 100;
  const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
  const valColor = pct > 5 ? 'red' : (pct < -5 ? 'green' : '');
  const cardColor = pct > 5 ? 'red' : (pct < -5 ? 'green' : '');

  // Delta vs previous
  let deltaHtml = '';
  if (prev && prev[key + '_idx'] != null) {
    const change = (val - prev[key + '_idx']) * 100;
    const arrow = change > 0.05 ? '▲' : (change < -0.05 ? '▼' : '◆');
    const cls = change > 0.05 ? 'up' : (change < -0.05 ? 'down' : '');
    deltaHtml = `<div class="delta">${avgPrice ? '£' + avgPrice.toFixed(0) + ' avg · ' : ''}<span class="${cls}">${arrow} ${Math.abs(change).toFixed(2)}pp</span> vs prior</div>`;
  } else {
    deltaHtml = `<div class="delta">${avgPrice ? '£' + avgPrice.toFixed(0) + ' avg' : ''}</div>`;
  }

  // Sparkline
  const points = series.map(s => s[key + '_idx']).filter(v => v != null);
  const sparkSvg = sparkline(points, INDEX_COLORS[key]);

  const n = latest.n?.[key] ?? 0;

  return `
    <div class="hero-card ${cardColor}">
      <div class="label">${INDEX_LABELS[key]} vs MSRP</div>
      <div class="value ${valColor}">${pctStr}</div>
      ${deltaHtml}
      ${sparkSvg}
      <div class="meta">${n} item${n === 1 ? '' : 's'} priced · index ${val.toFixed(3)}x</div>
    </div>`;
}

function sparkline(points, color) {
  if (points.length < 2) {
    return `<svg class="sparkline" viewBox="0 0 100 30" preserveAspectRatio="none">
      <circle cx="50" cy="15" r="2" fill="${color}"/>
    </svg>`;
  }
  const minV = Math.min(...points);
  const maxV = Math.max(...points);
  const range = maxV - minV || 1;
  const w = 100, h = 30, pad = 2;
  const xs = points.map((_, i) => pad + (i / (points.length - 1)) * (w - 2 * pad));
  const ys = points.map(v => pad + (1 - (v - minV) / range) * (h - 2 * pad));
  const d = xs.map((x, i) => (i === 0 ? 'M' : 'L') + x.toFixed(2) + ' ' + ys[i].toFixed(2)).join(' ');
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5"/>
    <circle cx="${xs[xs.length - 1].toFixed(2)}" cy="${ys[ys.length - 1].toFixed(2)}" r="2" fill="${color}"/>
  </svg>`;
}

function renderDiscountBanner(disc) {
  if (!disc) return '';
  const items = [];
  if (disc.gpu_used_vs_new != null) {
    items.push(`<div class="item"><span class="lbl">GPU · Used vs New discount</span><span class="val">${(disc.gpu_used_vs_new * 100).toFixed(1)}%</span></div>`);
  }
  if (disc.ram_used_vs_new != null) {
    items.push(`<div class="item"><span class="lbl">RAM · Used vs New discount</span><span class="val">${(disc.ram_used_vs_new * 100).toFixed(1)}%</span></div>`);
  }
  if (!items.length) return '';
  return `<div class="discount-banner">
    <div class="item"><span class="lbl">Avg savings buying secondhand</span></div>
    ${items.join('')}
  </div>`;
}

function renderTierPanel(label, newTiers, usedTiers) {
  const tiers = ['flagship', 'high-end', 'mid-range', 'budget'];
  const rows = tiers.map(t => {
    const n = newTiers?.[t];
    const u = usedTiers?.[t];
    if (!n && !u) return '';
    const newCell = n
      ? `<td class="num">${ratioToPct(n.avg_ratio)}${ratioBar(n.avg_ratio)}<span class="price-range">£${Math.round(n.avg_price)} · n=${n.count}</span></td>`
      : '<td class="num"><span class="price-empty">—</span></td>';
    const usedCell = u
      ? `<td class="num">${ratioToPct(u.avg_ratio)}${ratioBar(u.avg_ratio)}<span class="price-range">£${Math.round(u.avg_price)} · n=${u.count}</span></td>`
      : '<td class="num"><span class="price-empty">—</span></td>';
    return `<tr>
      <td><span class="tier-badge tier-${t}">${t}</span></td>
      ${newCell}
      ${usedCell}
    </tr>`;
  }).join('');

  return `
    <div class="panel">
      <div class="panel-header"><span class="accent">${label}</span> · Tier index vs MSRP</div>
      <div class="panel-body">
        <table class="tier-table">
          <colgroup>
            <col class="col-tier"><col class="col-side"><col class="col-side">
          </colgroup>
          <thead><tr>
            <th style="text-align:left">Tier</th>
            <th class="num">New</th>
            <th class="num">Used</th>
          </tr></thead>
          <tbody>${rows || '<tr><td colspan=3 style="padding:24px;text-align:center;color:var(--fg-faint)">No tier data</td></tr>'}</tbody>
        </table>
      </div>
    </div>`;
}

function ratioBar(ratio) {
  // 0.5 to 1.5 maps to a centered bar where 1.0 is the midline
  const pct = Math.max(0.5, Math.min(1.5, ratio));
  const dev = (pct - 1.0); // -0.5 to +0.5
  const width = Math.abs(dev) / 0.5 * 30; // up to 30px each side
  const fillCls = dev > 0 ? 'over' : 'under';
  const style = dev > 0 ? `width:${width}px` : `width:${width}px`;
  return `<span class="ratio-bar"><span class="midline"></span><span class="fill ${fillCls}" style="${style}"></span></span>`;
}

function ratioToPct(r) {
  const p = (r - 1) * 100;
  const cls = p > 2 ? 'red' : (p < -2 ? 'green' : '');
  const sign = p >= 0 ? '+' : '';
  return `<span class="${cls === 'red' ? 'value-poor' : cls === 'green' ? 'value-highlight' : ''}">${sign}${p.toFixed(1)}%</span>`;
}

function renderLeaderPanel(title, usedList, newList, kind) {
  const fmt = (item, rank) => {
    if (!item) return '';
    const valueStr = kind === 'gpu'
      ? `${item.value.toFixed(1)} pts/£`
      : `£${item.value.toFixed(2)}/GB`;
    return `<tr>
      <td><span class="rank">${rank}</span>${item.name}<br><span class="brand">${item.tier}</span></td>
      <td class="num">${valueStr}</td>
      <td class="num">£${item.price?.toFixed(0) ?? '—'}</td>
    </tr>`;
  };

  const usedRows = (usedList || []).slice(0, 5).map((x, i) => fmt(x, i + 1)).join('');
  const newRows  = (newList  || []).slice(0, 5).map((x, i) => fmt(x, i + 1)).join('');

  return `
    <div class="panel">
      <div class="panel-header"><span class="accent">${title}</span></div>
      <div class="panel-body">
        <table class="leader-table">
          <colgroup>
            <col class="col-name"><col class="col-val"><col class="col-price">
          </colgroup>
          <thead><tr>
            <th colspan="3" style="text-align:left;color:var(--green)">Used market</th>
          </tr></thead>
          <tbody>${usedRows || '<tr><td colspan=3 style="padding:16px;text-align:center;color:var(--fg-faint)">No data</td></tr>'}</tbody>
        </table>
        <table class="leader-table">
          <colgroup>
            <col class="col-name"><col class="col-val"><col class="col-price">
          </colgroup>
          <thead><tr>
            <th colspan="3" style="text-align:left;color:var(--amber);border-top:1px solid var(--line)">New market</th>
          </tr></thead>
          <tbody>${newRows || '<tr><td colspan=3 style="padding:16px;text-align:center;color:var(--fg-faint)">No data</td></tr>'}</tbody>
        </table>
      </div>
    </div>`;
}

// ---- Main chart ----
function drawChart(series) {
  const svg = $('#index-chart');
  if (!svg) return;
  svg.innerHTML = '';

  const W = 1400, H = 340;
  const M = { top: 20, right: 20, bottom: 40, left: 50 };
  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom;

  // Collect all values for y-domain
  const allVals = [];
  series.forEach(s => {
    ['gpu_new_idx', 'gpu_used_idx', 'ram_new_idx', 'ram_used_idx'].forEach(k => {
      if (s[k] != null) allVals.push(s[k]);
    });
  });
  if (!allVals.length) return;

  let yMin = Math.min(...allVals, 0.9);
  let yMax = Math.max(...allVals, 1.1);
  const pad = (yMax - yMin) * 0.1;
  yMin = Math.max(0, yMin - pad);
  yMax = yMax + pad;

  const xScale = i => M.left + (series.length === 1 ? innerW / 2 : (i / (series.length - 1)) * innerW);
  const yScale = v => M.top + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  // Grid + y-axis labels
  const yTicks = 5;
  const gridG = svgEl('g', { class: 'grid' });
  const axisG = svgEl('g', { class: 'axis' });
  for (let i = 0; i <= yTicks; i++) {
    const v = yMin + (i / yTicks) * (yMax - yMin);
    const y = yScale(v);
    gridG.appendChild(svgEl('line', { x1: M.left, y1: y, x2: W - M.right, y2: y }));
    const lbl = svgEl('text', { x: M.left - 8, y: y + 3, 'text-anchor': 'end' });
    lbl.textContent = v.toFixed(2) + 'x';
    axisG.appendChild(lbl);
  }
  svg.appendChild(gridG);

  // Reference line at 1.0 (MSRP parity)
  if (yMin <= 1.0 && yMax >= 1.0) {
    const y1 = yScale(1.0);
    const refLine = svgEl('line', { class: 'ref-line', x1: M.left, y1: y1, x2: W - M.right, y2: y1 });
    svg.appendChild(refLine);
    const refLabel = svgEl('text', { class: 'ref-label', x: W - M.right - 4, y: y1 - 4, 'text-anchor': 'end' });
    refLabel.textContent = 'MSRP';
    svg.appendChild(refLabel);
  }

  // X-axis labels (showing dates / relative)
  const tickEvery = Math.max(1, Math.floor(series.length / 8));
  series.forEach((s, i) => {
    if (i % tickEvery !== 0 && i !== series.length - 1) return;
    const x = xScale(i);
    const lbl = svgEl('text', { x: x, y: H - M.bottom + 16, 'text-anchor': 'middle' });
    lbl.textContent = formatTickDate(s.ts);
    axisG.appendChild(lbl);
  });
  svg.appendChild(axisG);

  // Plot each series
  ['gpu_new_idx', 'gpu_used_idx', 'ram_new_idx', 'ram_used_idx'].forEach(key => {
    const baseKey = key.replace('_idx', '');
    const color = INDEX_COLORS[baseKey];
    const pts = series.map((s, i) => ({ i, v: s[key] })).filter(p => p.v != null);
    if (!pts.length) return;
    const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + xScale(p.i).toFixed(2) + ' ' + yScale(p.v).toFixed(2)).join(' ');
    svg.appendChild(svgEl('path', { class: 'series', d: d, stroke: color }));
    pts.forEach(p => {
      svg.appendChild(svgEl('circle', {
        class: 'series-dot', cx: xScale(p.i), cy: yScale(p.v), r: 3, fill: color,
        'data-key': key, 'data-i': p.i,
      }));
    });
  });

  // Hover behaviour
  const tooltip = $('#chart-tooltip');
  const crosshair = svgEl('line', { class: 'crosshair', x1: 0, y1: M.top, x2: 0, y2: H - M.bottom });
  svg.appendChild(crosshair);

  svg.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const xPx = (e.clientX - rect.left) * (W / rect.width);
    if (xPx < M.left || xPx > W - M.right) {
      svg.classList.remove('hovering');
      tooltip.classList.remove('visible');
      return;
    }
    // Find closest snapshot index
    let bestI = 0, bestDist = Infinity;
    series.forEach((_, i) => {
      const d = Math.abs(xScale(i) - xPx);
      if (d < bestDist) { bestDist = d; bestI = i; }
    });
    const s = series[bestI];
    crosshair.setAttribute('x1', xScale(bestI));
    crosshair.setAttribute('x2', xScale(bestI));
    svg.classList.add('hovering');

    // Build tooltip
    const rows = ['gpu_new_idx', 'gpu_used_idx', 'ram_new_idx', 'ram_used_idx'].map(k => {
      const baseKey = k.replace('_idx', '');
      const v = s[k];
      if (v == null) return '';
      const pct = (v - 1) * 100;
      const sign = pct >= 0 ? '+' : '';
      return `<div class="row"><span class="name"><span style="color:${INDEX_COLORS[baseKey]}">●</span> ${INDEX_LABELS[baseKey]}</span><span class="val">${v.toFixed(3)}x (${sign}${pct.toFixed(1)}%)</span></div>`;
    }).join('');
    tooltip.innerHTML = `<div class="ts">${formatFullDate(s.ts)}</div>${rows}`;
    tooltip.classList.add('visible');

    // Position tooltip
    const tooltipRect = tooltip.getBoundingClientRect();
    const containerRect = svg.parentElement.getBoundingClientRect();
    const xPxScreen = (xScale(bestI) / W) * rect.width;
    let tx = xPxScreen + 12;
    if (tx + tooltipRect.width > containerRect.width) tx = xPxScreen - tooltipRect.width - 12;
    tooltip.style.left = tx + 'px';
    tooltip.style.top = '20px';
  });
  svg.addEventListener('mouseleave', () => {
    svg.classList.remove('hovering');
    tooltip.classList.remove('visible');
  });
}

function svgEl(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

function formatTickDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
    ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
function formatFullDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// Initial load
loadData();
// In live mode, poll periodically so the "X ago" label and refresh progress
// stay current. In static mode the page is a frozen snapshot — no polling.
if (!STATIC) {
  setInterval(() => {
    if (state.data.gpu.length || state.data.ram.length) loadData();
  }, 10000);
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print(f"  HW//INDEX starting  (region: {EBAY_DOMAIN}, currency: {CURRENCY})")
    print(f"  pacing: {WORKERS} workers · {DELAY_MIN}-{DELAY_MAX}s delays · retry-on-challenge={RETRY_ON_CHALLENGE}")
    print("  → http://localhost:5000")
    print("  → debug:  http://localhost:5000/api/debug?q=RTX+4070&condition=used")
    print("  Press Ctrl+C to stop. Click 'Refresh Prices' in the UI.")
    print("=" * 70)
    warm_load_from_history()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
