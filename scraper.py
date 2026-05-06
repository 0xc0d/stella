"""
News scraper — fetches posts from configured WordPress sites
(Ajax Load More + WP REST API) and exports to CSV for Google Sheets.

Usage:
    python3.10 1.py                        # interactive menu
    python3.10 1.py 2026-03-01             # interactive menu, custom cutoff date
    python3.10 1.py 2026-03-01 3           # site index 3 (RRN), no menu prompt
    python3.10 1.py 2026-03-01 3 --text    # include full article text in CSV
"""

import requests
import csv
import time
import sys
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Site configurations
# Add a new entry here to support a new website.
# ---------------------------------------------------------------------------

SITES = [
    {
        "name": "Ostlicher Wind (ostlicherwind.org)",
        "type": "alm",  # Ajax Load More
        "base_url": "https://ostlicherwind.org/wp-admin/admin-ajax.php",
        "homepage_url": "https://ostlicherwind.org/",
        "referer": "https://ostlicherwind.org/",
        "cookies": {
            "_ga": "GA1.1.1443190349.1775464967",
            "pvc_visits[0]": "1775551407b5212",
        },
        "params": {
            "action": "alm_get_posts",
            "query_type": "standard",
            "id": "9317641231",
            "post_id": "0",
            "slug": "home",
            "canonical_url": "https://ostlicherwind.org/",
            "posts_per_page": "50",
            "offset": "0",
            "original_offset": "0",
            "post_type": "post",
            "repeater": "default",
            "seo_start_page": "1",
            "order": "DESC",
            "orderby": "date",
            "post__not_in": "2,5228,5225,5219,5222,5215,5212",
        },
        # CSS selectors for parsing
        "item_selector": ("div", "post-item_2"),
        "title_selector": ("a", "post-item_2_title"),
        "date_selector": ("span", "post-item_2_date"),
        # date_transform: callable to clean raw date string before parsing
        "date_transform": lambda s: s.strip(),
        "date_format": "%d.%m.%Y, %H:%M",
        "article_content_selector": ("div", "singlepost"),
    },
    {
        "name": "Stolz Volk (stolzvolk.ac)",
        "type": "alm",
        "base_url": "https://stolzvolk.ac/wp-admin/admin-ajax.php",
        "homepage_url": "https://stolzvolk.ac/",  # scraped first to catch pre-loaded posts
        "referer": "https://stolzvolk.ac/",
        "cookies": {
            "_ga": "GA1.1.811254199.1775465239",
        },
        "params": {
            "action": "alm_get_posts",
            "query_type": "standard",
            "id": "9317641231",
            "post_id": "0",
            "slug": "home",
            "canonical_url": "https://stolzvolk.ac/",
            "posts_per_page": "50",
            "offset": "0",
            "original_offset": "0",
            "post_type": "post",
            "repeater": "default",
            "seo_start_page": "1",
            "order": "DESC",
            "orderby": "date",
            "post__not_in": "2,7969,7968,7967,7966,7965,7964,7951,7950,7949,7948,1955,1954,1953,1952,1951,1394,1393,1392,1391,936,7971,7952,7959,7961,7943,7941,7937,7933,7914",
        },
        "item_selector": ("div", "post-item_medium2"),
        "title_selector": ("a", "post-item_medium2_title"),
        "date_selector": ("span", "post-item_medium2_date"),
        # Strip "Stand: " prefix and " Uhr" suffix
        "date_transform": lambda s: re.sub(r"^Stand:\s*|\s*Uhr$", "", s.strip()),
        "date_format": "%d.%m.%Y %H:%M",
        "article_content_selector": ("div", "singlepost"),
    },
    {
        "name": "RRN (rrn.com.tr)",
        "type": "wp_newsflow",  # WP REST API newsflow endpoint
        "base_url": "https://rrn.com.tr/wp-json/wp/v2/newsflow",
        "referer": "https://rrn.com.tr/",
        "cookies": {},
        "api_params": {
            "page": "front",
            "lang": "en",
        },
        # CSS selectors for parsing the HTML inside the JSON response
        "item_selector": ("div", "postcard"),
        "title_selector": ("a", "postcard-title"),
        "date_selector": ("time", "timestamp"),
        "date_transform": lambda s: s.strip(),
        "date_format": "%Y-%m-%d %H:%M",  # from the datetime attribute
        # article body selector (for full-text fetch)
        "article_content_selector": ("div", "article_content"),
    },
    {
        "name": "Brennende Frage (brennendefrage.net)",
        "type": "alm",
        "base_url": "https://brennendefrage.net/wp-admin/admin-ajax.php",
        "homepage_url": "https://brennendefrage.net/",
        "referer": "https://brennendefrage.net/",
        "cookies": {
            "_ga": "GA1.1.26812040.1775841518",
        },
        "params": {
            "action": "alm_get_posts",
            "query_type": "standard",
            "id": "9317641231",
            "post_id": "0",
            "slug": "home",
            "canonical_url": "https://brennendefrage.net/",
            "posts_per_page": "50",
            "offset": "0",
            "post_type": "post",
            "repeater": "default",
            "seo_start_page": "1",
            "order": "DESC",
            "orderby": "date",
            "post__not_in": "14728,14707,14681,957,14724,14722,14720,14718,14716,14714,14665,14662,14633,7117,14629,14606",
        },
        "item_selector": ("div", "post-item_medium"),
        "title_selector": ("a", None),  # <a> has no class
        "date_selector": ("span", "date"),
        "date_transform": lambda s: s.strip(),
        "date_format": "%d.%m.%Y, %H:%M",
        "article_content_selector": ("div", "singlepost"),
    },
    {
        "name": "Wahlomacht (wahlomacht.io)",
        "type": "alm",
        "base_url": "https://wahlomacht.io/wp-admin/admin-ajax.php",
        "homepage_url": "https://wahlomacht.io/",
        "referer": "https://wahlomacht.io/",
        "cookies": {},
        "params": {
            "action": "alm_get_posts",
            "query_type": "standard",
            "id": "9317641231",
            "post_id": "0",
            "slug": "home",
            "canonical_url": "https://wahlomacht.io/",
            "posts_per_page": "50",
            "offset": "0",
            "post_type": "post",
            "repeater": "default",
            "seo_start_page": "1",
            "order": "DESC",
            "orderby": "date",
            "post__not_in": "2,4071,4079,4055,4083,4062",
        },
        "item_selector": ("div", "post-item_large"),
        "title_selector": ("a", None),  # <a> has no class
        "date_selector": ("span", None),  # <span> has no class, contains "Stand: DD/MM/YYYY HH:MM Uhr"
        "date_transform": lambda s: re.sub(r"^Stand:\s*|\s*Uhr$", "", s.strip()),
        "date_format": "%d/%m/%Y %H:%M",
        "article_content_selector": ("div", "singlepost"),
    },
    {
        "name": "DM Zeitung (dmzeitung.net)",
        "type": "alm",
        "base_url": "https://dmzeitung.net/wp-admin/admin-ajax.php",
        "homepage_url": "https://dmzeitung.net/",
        "referer": "https://dmzeitung.net/",
        "cookies": {
            "_ga": "GA1.1.471265736.1775841675",
        },
        "params": {
            "action": "alm_get_posts",
            "query_type": "standard",
            "id": "9317641231",
            "post_id": "0",
            "slug": "home",
            "canonical_url": "https://dmzeitung.net/",
            "posts_per_page": "50",
            "offset": "0",
            "original_offset": "0",
            "post_type": "post",
            "repeater": "default",
            "seo_start_page": "1",
            "order": "DESC",
            "orderby": "date",
            "post__not_in": ",4576,4573,4570,4566,4563,4560,4556,4553,4550,4547,4543,4540,4537",
            "currentPage": "1",
        },
        "item_selector": ("div", "post-item_medium"),
        "title_selector": ("a", None),  # <a> has no class
        "date_selector": ("span", "post-item_medium_date"),
        "date_transform": lambda s: s.strip(),
        "date_format": "%d.%m.%Y, %H:%M",
        "article_content_selector": ("div", "singlepost"),
    },
]

# ---------------------------------------------------------------------------
# Shared headers (same for all sites)
# ---------------------------------------------------------------------------

def make_headers(referer: str) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9,nl;q=0.8,fa;q=0.7,ar;q=0.6",
        "priority": "u=1, i",
        "referer": referer,
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_posts_from_html(html: str, site: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    item_tag, item_class = site["item_selector"]
    title_tag, title_class = site["title_selector"]
    date_tag, date_class = site["date_selector"]
    date_transform = site["date_transform"]
    date_format = site["date_format"]
    use_datetime_attr = site.get("type") == "wp_newsflow"

    for item in soup.find_all(item_tag, class_=item_class):
        post = {}

        if title_class:
            title_el = item.find(title_tag, class_=title_class)
        else:
            title_el = item.find(title_tag, href=True)
        if title_el:
            post["title"] = title_el.get_text(strip=True)
            post["url"] = title_el.get("href")

        if date_class:
            date_el = item.find(date_tag, class_=date_class)
        else:
            # Find a <tag> with no class attribute (skip ones with class like "category")
            date_el = item.find(date_tag, class_=lambda c: c is None or c == [])
        if date_el:
            if use_datetime_attr:
                # RRN uses <time datetime="2026-03-13 14:31">
                raw = date_el.get("datetime", "")
            else:
                raw = date_el.get_text(strip=True)
            cleaned = date_transform(raw)
            post["date_str"] = cleaned
            try:
                post["date"] = datetime.strptime(cleaned, date_format)
            except ValueError:
                post["date"] = None

        if post:
            posts.append(post)

    return posts


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

def fetch_homepage_posts(site: dict, cutoff: datetime) -> list[dict]:
    """Scrape posts pre-loaded in the main page HTML (skipped by the Ajax endpoint)."""
    homepage_url = site.get("homepage_url")
    if not homepage_url:
        return []

    print(f"  Homepage...", end=" ", flush=True)
    headers = make_headers(site["referer"])
    headers.pop("x-requested-with", None)  # normal page request, not XHR
    headers["accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    response = requests.get(homepage_url, headers=headers, cookies=site.get("cookies", {}), timeout=15)
    response.raise_for_status()

    posts = parse_posts_from_html(response.text, site)
    kept = [p for p in posts if p.get("date") is None or p["date"] >= cutoff]
    print(f"{len(posts)} posts found, kept {len(kept)} within cutoff")
    return kept


def fetch_posts_until(site: dict, cutoff: datetime, delay: float = 0.5,
                      *, start_page: int = 0, should_abort=None,
                      include_homepage: bool = True) -> list[dict]:
    """Walk pages until we cross `cutoff` (DESC date order).

    start_page: page index to begin at (skip-ahead probe sets this).
    should_abort: optional callable; checked between pages, returns True to stop.
    include_homepage: skipped when start_page > 0 (homepage posts are always newest).
    """
    if include_homepage and start_page == 0:
        try:
            all_posts = fetch_homepage_posts(site, cutoff)
        except KeyboardInterrupt:
            return []
    else:
        all_posts = []
    seen_urls = {p.get("url") for p in all_posts}

    page = start_page
    headers = make_headers(site["referer"])

    while True:
        try:
            params = {**site["params"], "page": str(page)}

            print(f"  Page {page}...", end=" ", flush=True)
            response = requests.get(
                site["base_url"],
                headers=headers,
                cookies=site["cookies"],
                params=params,
                timeout=15,
            )
            response.raise_for_status()

            data = response.json()
            html = data.get("html", "")
            total_posts = int(data.get("meta", {}).get("totalposts", 0))

            if not html or html.strip() in ("", "false"):
                print("no content.")
                break

            page_posts = parse_posts_from_html(html, site)
            hit_cutoff = False

            for post in page_posts:
                if post.get("date") is not None and post["date"] < cutoff:
                    hit_cutoff = True
                    break
                if post.get("url") not in seen_urls:
                    all_posts.append(post)
                    seen_urls.add(post.get("url"))

            print(f"{len(page_posts)} posts on page (kept {len(all_posts)} / {total_posts} total)")

            if hit_cutoff:
                print(f"  Reached cutoff {cutoff.date()}. Done.")
                break

            if total_posts and len(all_posts) >= total_posts:
                print("  All posts fetched. Done.")
                break

            page += 1
            time.sleep(delay)

            if should_abort is not None and should_abort():
                print("  Aborted by user.")
                break
        except KeyboardInterrupt:
            print("\n  Interrupted (Ctrl-C).")
            break

    return all_posts


def fetch_newsflow_posts(site: dict, cutoff: datetime, delay: float = 0.5,
                         *, start_page: int = 0, should_abort=None) -> list[dict]:
    """Fetch posts from WP REST API newsflow endpoint (rrn.com.tr style)."""
    all_posts = []
    seen_urls = set()

    headers = make_headers(site["referer"])
    headers.pop("x-requested-with", None)

    pagenum = start_page

    while True:
        try:
            params = {**site["api_params"], "pagenum": str(pagenum)}

            print(f"  Page {pagenum}...", end=" ", flush=True)
            response = requests.get(
                site["base_url"],
                headers=headers,
                cookies=site.get("cookies", {}),
                params=params,
                timeout=15,
            )
            response.raise_for_status()

            data = response.json()
            html = data.get("data", {}).get("content", "")
            can_load_more = data.get("data", {}).get("canLoadMore", False)

            if not html or not html.strip():
                print("no content.")
                break

            page_posts = parse_posts_from_html(html, site)
            hit_cutoff = False

            for post in page_posts:
                if post.get("date") is not None and post["date"] < cutoff:
                    hit_cutoff = True
                    break
                if post.get("url") not in seen_urls:
                    all_posts.append(post)
                    seen_urls.add(post.get("url"))

            print(f"{len(page_posts)} posts on page (kept {len(all_posts)} total)")

            if hit_cutoff:
                print(f"  Reached cutoff {cutoff.date()}. Done.")
                break

            if not can_load_more:
                print("  No more pages. Done.")
                break

            pagenum += 1
            time.sleep(delay)

            if should_abort is not None and should_abort():
                print("  Aborted by user.")
                break
        except KeyboardInterrupt:
            print("\n  Interrupted (Ctrl-C).")
            break

    return all_posts


# ---------------------------------------------------------------------------
# Article text fetcher
# ---------------------------------------------------------------------------

def fetch_article_text(url: str, content_selector: tuple | None = None) -> str:
    """Fetch the full article text from a post URL using the site's content selector."""
    if content_selector is None:
        content_selector = ("div", "article_content")
    tag, cls = content_selector
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    content_div = soup.find(tag, class_=cls)
    if not content_div:
        return "(article content not found)"

    paragraphs = content_div.find_all("p")
    return "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

DATE_PRESETS = [
    ("Today",           lambda: datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)),
    ("Last 7 days",     lambda: datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).__class__(
                            *(datetime.now() - __import__('datetime').timedelta(days=7)).timetuple()[:3])),
    ("This month",      lambda: datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
    ("Last month",      lambda: (datetime.now().replace(day=1) - __import__('datetime').timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
    ("Last 3 months",   lambda: (datetime.now() - __import__('datetime').timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0)),
    ("This year",       lambda: datetime.now().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)),
    ("Custom date...",  None),
]


def ask_cutoff_date() -> datetime:
    print("\n--- Select cutoff date (fetch posts FROM this date onward) ---\n")
    for i, (label, _) in enumerate(DATE_PRESETS, 1):
        print(f"  {i}. {label}")
    print()

    while True:
        try:
            choice = int(input(f"Enter number (1-{len(DATE_PRESETS)}): ").strip())
            if 1 <= choice <= len(DATE_PRESETS):
                label, fn = DATE_PRESETS[choice - 1]
                if fn is not None:
                    return fn()
                # Custom date
                while True:
                    raw = input("  Enter date (YYYY-MM-DD): ").strip()
                    try:
                        return datetime.strptime(raw, "%Y-%m-%d")
                    except ValueError:
                        print("  Invalid format. Use YYYY-MM-DD.")
        except (ValueError, KeyboardInterrupt):
            pass
        print(f"Please enter a number between 1 and {len(DATE_PRESETS)}.")


def show_menu() -> tuple[int, bool]:
    """Returns (site_choice, fetch_text). fetch_text=True means include article body."""
    print("\n=== News Scraper ===")
    print("Choose a website:\n")
    for i, site in enumerate(SITES, 1):
        print(f"  {i}. {site['name']}")
    print(f"  {len(SITES) + 1}. All sites")
    print()

    while True:
        try:
            choice = int(input(f"Enter number (1-{len(SITES) + 1}): ").strip())
            if 1 <= choice <= len(SITES) + 1:
                break
        except (ValueError, KeyboardInterrupt):
            pass
        print(f"Please enter a number between 1 and {len(SITES) + 1}.")

    # Check if the selected site(s) support article text
    if choice <= len(SITES):
        site = SITES[choice - 1]
        supports_text = site.get("article_content_selector") is not None
    else:
        supports_text = any(s.get("article_content_selector") for s in SITES)

    fetch_text = False
    if supports_text:
        print("\nInclude full article text? (slower — fetches each article page)")
        ans = input("  [y/N]: ").strip().lower()
        fetch_text = ans in ("y", "yes")

    return choice, fetch_text


def scrape_site(site: dict, cutoff: datetime, *,
                start_page: int = 0, should_abort=None) -> list[dict]:
    print(f"\nScraping: {site['name']}")
    print(f"Cutoff date: {cutoff.date()}\n")
    if site.get("type") == "wp_newsflow":
        posts = fetch_newsflow_posts(site, cutoff, start_page=start_page, should_abort=should_abort)
    else:
        posts = fetch_posts_until(site, cutoff, start_page=start_page, should_abort=should_abort,
                                  include_homepage=(start_page == 0))
    print(f"=> {len(posts)} posts collected from {site['name']}\n")
    return posts


# ---------------------------------------------------------------------------
# Skip-ahead probe — find the first page whose newest post is <= `until`.
# Used by window_scrape so we don't walk thousands of pages from the head when
# the user wants an old date range.
# ---------------------------------------------------------------------------

def _fetch_alm_page(site: dict, page: int) -> tuple[list[dict], int]:
    """Fetch a single ALM page; return (posts, total_posts). Empty posts means past end."""
    headers = make_headers(site["referer"])
    params = {**site["params"], "page": str(page)}
    response = requests.get(
        site["base_url"], headers=headers, cookies=site["cookies"],
        params=params, timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    html = data.get("html", "")
    total = int(data.get("meta", {}).get("totalposts", 0))
    if not html or html.strip() in ("", "false"):
        return [], total
    return parse_posts_from_html(html, site), total


def _fetch_newsflow_page(site: dict, page: int) -> tuple[list[dict], bool]:
    """Fetch a single newsflow page; return (posts, can_load_more)."""
    headers = make_headers(site["referer"])
    headers.pop("x-requested-with", None)
    params = {**site["api_params"], "pagenum": str(page)}
    response = requests.get(
        site["base_url"], headers=headers, cookies=site.get("cookies", {}),
        params=params, timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    html = data.get("data", {}).get("content", "")
    can_more = data.get("data", {}).get("canLoadMore", False)
    if not html or not html.strip():
        return [], can_more
    return parse_posts_from_html(html, site), can_more


def _newest_date(posts: list[dict]) -> datetime | None:
    """Date-DESC ordered: newest is first. Return None if no parseable date."""
    for p in posts:
        if p.get("date") is not None:
            return p["date"]
    return None


def find_start_page(site: dict, until: datetime, *, max_probe_pages: int = 4096,
                    should_abort=None) -> int:
    """Return the lowest page index whose first post's date is <= `until`.

    Pages are date-DESC: page 0 is newest. We binary-search (ALM, with totalposts
    metadata) or exponentially probe then binary-search (newsflow, no total) to
    skip over pages whose contents are all newer than `until`.

    Returns 0 if `until` is in the future (or every page is older than until).
    Returns the deepest probed page if the site doesn't go back to `until`.
    """
    site_type = site.get("type")
    print(f"  Probing for page near {until.date()}...", flush=True)

    def aborted() -> bool:
        return should_abort is not None and should_abort()

    if site_type == "wp_newsflow":
        # Exponential probe to find an upper bound, then bisect.
        lo, hi = 0, None
        # Confirm page 0 is newer than `until`; if not, no skip is needed.
        first_posts, _ = _fetch_newsflow_page(site, 0)
        first_date = _newest_date(first_posts)
        if first_date is None or first_date <= until:
            return 0
        time.sleep(0.3)

        probe = 1
        while probe < max_probe_pages:
            if aborted():
                return lo
            posts, can_more = _fetch_newsflow_page(site, probe)
            d = _newest_date(posts)
            if not posts or d is None:
                hi = probe  # past the end -> upper bound for bisect
                break
            if d <= until:
                hi = probe
                break
            lo = probe
            if not can_more:
                # Page exists but server says no more after this. Treat as upper bound.
                hi = probe
                break
            probe *= 2
            time.sleep(0.3)
        if hi is None:
            return lo  # exhausted probe budget; start where we got
        # Bisect [lo, hi] for the lowest page with first_date <= until OR empty.
        while lo + 1 < hi:
            if aborted():
                return lo
            mid = (lo + hi) // 2
            posts, _ = _fetch_newsflow_page(site, mid)
            d = _newest_date(posts)
            if not posts or d is None or d <= until:
                hi = mid
            else:
                lo = mid
            time.sleep(0.3)
        return hi

    # ALM: meta.totalposts gives an exact upper bound on pages.
    first_posts, total = _fetch_alm_page(site, 0)
    first_date = _newest_date(first_posts)
    if first_date is None or first_date <= until:
        return 0
    posts_per_page = max(1, int(site["params"].get("posts_per_page", "12")))
    last_page = max(0, (total - 1) // posts_per_page) if total else max_probe_pages
    last_page = min(last_page, max_probe_pages)
    lo, hi = 0, last_page
    time.sleep(0.3)

    while lo + 1 < hi:
        if aborted():
            return lo
        mid = (lo + hi) // 2
        posts, _ = _fetch_alm_page(site, mid)
        d = _newest_date(posts)
        if not posts or d is None or d <= until:
            hi = mid
        else:
            lo = mid
        time.sleep(0.3)
    return hi


def enrich_with_text(posts: list[dict], site: dict, workers: int = 32):
    """Fetch full article text for each post (in-place) using parallel workers."""
    if not site.get("article_content_selector"):
        return
    to_fetch = [(i, p) for i, p in enumerate(posts) if p.get("url")]
    print(f"Fetching article text for {len(to_fetch)} posts ({workers} workers)...")
    done = 0

    def _fetch(idx_post):
        idx, post = idx_post
        try:
            text = fetch_article_text(post["url"], site.get("article_content_selector"))
            return idx, text, None
        except Exception as e:
            return idx, None, str(e)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, text, err in pool.map(_fetch, to_fetch):
            done += 1
            title = posts[idx].get("title", "")[:60]
            if err:
                posts[idx]["text"] = f"(error: {err})"
                print(f"  [{done}/{len(to_fetch)}] {title}... FAILED: {err}")
            else:
                posts[idx]["text"] = text
                print(f"  [{done}/{len(to_fetch)}] {title}... OK")
    print()


def save_csv(posts: list[dict], filename: str, site_name: str | None = None, include_text: bool = False):
    with open(filename, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        header = ["date", "title", "url"]
        if site_name:
            header.append("site")
        if include_text:
            header.append("text")
        writer.writerow(header)
        for p in posts:
            row = [p.get("date_str", ""), p.get("title", ""), p.get("url", "")]
            if site_name:
                row.append(site_name)
            if include_text:
                row.append(p.get("text", ""))
            writer.writerow(row)
    print(f"Saved {len(posts)} posts to {filename}")
    print("Open in Google Sheets: File > Import > Upload\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Parse CLI args
    cutoff_date = None
    site_index = None  # 1-based, len(SITES)+1 = all
    fetch_text = False

    for arg in sys.argv[1:]:
        if arg == "--text":
            fetch_text = True
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            try:
                cutoff_date = datetime.strptime(arg, "%Y-%m-%d")
            except ValueError:
                print(f"Invalid date: {arg}. Use YYYY-MM-DD.")
                sys.exit(1)
        elif arg.isdigit():
            site_index = int(arg)

    if site_index is None:
        choice, menu_fetch_text = show_menu()
        fetch_text = fetch_text or menu_fetch_text
    else:
        choice = site_index

    if cutoff_date is None:
        cutoff_date = ask_cutoff_date()

    # Scrape
    if choice == len(SITES) + 1:
        # All sites — combine into one CSV with a site column
        all_combined = []
        for site in SITES:
            posts = scrape_site(site, cutoff_date)
            if fetch_text:
                enrich_with_text(posts, site)
            for p in posts:
                p["_site"] = site["name"]
            all_combined.extend(posts)

        filename = f"posts_all_{cutoff_date.strftime('%Y%m%d')}.csv"
        with open(filename, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            header = ["date", "title", "url", "site"]
            if fetch_text:
                header.append("text")
            writer.writerow(header)
            for p in all_combined:
                row = [p.get("date_str", ""), p.get("title", ""), p.get("url", ""), p.get("_site", "")]
                if fetch_text:
                    row.append(p.get("text", ""))
                writer.writerow(row)
        print(f"\nCombined: saved {len(all_combined)} posts to {filename}")

    else:
        site = SITES[choice - 1]
        posts = scrape_site(site, cutoff_date)
        if fetch_text:
            enrich_with_text(posts, site)

        slug = site["name"].split("(")[-1].rstrip(")").replace(".", "_").replace("/", "")
        filename = f"posts_{slug}_{cutoff_date.strftime('%Y%m%d')}.csv"
        save_csv(posts, filename, include_text=fetch_text)

        # Preview
        for i, post in enumerate(posts[:5], 1):
            print(f"  {i}. [{post.get('date_str', '')}] {post.get('title', '')}")
