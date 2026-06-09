"""
STELLA — Unified news scraper & terminal viewer.

Usage:
    python3 inspect.py              # site selector (default)
    python3 inspect.py posts_*.csv  # open a specific CSV directly
"""

import csv
import sys
import os
import re
import json
import subprocess
import shutil
import calendar
import time
import threading
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

__version__ = "1.1.0"

GITHUB_OWNER = "0xc0d"
GITHUB_REPO = "stella"
UPDATE_BRANCH = "main"
UPDATE_FILES = ["stella.py", "scraper.py", "Stella.cmd",
                "repair_text.py", "backfill.py"]
CHANGELOG = {
    "1.1.0": [
        "Read tracking — opening an article marks it read; press r to toggle.",
        "Read articles show dimmed in the list.",
        "Tags — press g to tag any article (pick existing or type a new one).",
        "Press G to filter the list by tag; B (bookmarks) has a tag filter too.",
        "Updates now apply in place and drop you back where you were.",
    ],
}
UPDATE_CHECK_INTERVAL_SEC = 10 * 60
UPDATE_RETRY_INTERVAL_SEC = 60

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

from scraper import SITES, scrape_site, enrich_with_text, save_csv, find_start_page, fetch_article_text

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
    # Force UTF-8 on console I/O so non-ASCII titles (German/Turkish/Cyrillic) and
    # accent glyphs like ✓ ★ — don't blow up with UnicodeEncodeError on cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
else:
    import tty
    import termios

# csv default field size is 128 KB; full article bodies can exceed that and make
# DictReader raise, which previously surfaced as "0 posts" in the site list.
csv.field_size_limit(10 * 1024 * 1024)


# ---------------------------------------------------------------------------
# Theme detection
# ---------------------------------------------------------------------------

def detect_light_theme() -> bool:
    """Detect if the terminal is using a light background."""
    # 1. Check COLORFGBG (set by many terminals: "fg;bg", bg>=8 = light)
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        parts = colorfgbg.split(";")
        try:
            bg = int(parts[-1])
            if bg >= 8 or bg in (7, 15):  # light backgrounds
                return True
            return False
        except ValueError:
            pass

    # 2. macOS: check system appearance
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1,
        )
        if result.returncode != 0:  # key missing = Light mode
            return True
        if "dark" in result.stdout.lower():
            return False
        return True
    except Exception:
        pass

    # 3. Check common env hints
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if "iterm" in term_program:
        # iTerm usually sets COLORFGBG, if we got here assume dark
        return False

    return False  # default to dark


def make_theme(light: bool) -> dict:
    """Return color codes adapted to terminal background."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    if light:
        return {
            "reset": RESET, "bold": BOLD, "dim": DIM,
            "title": "\033[34m",       # blue
            "date": "\033[36m",        # cyan (dark)
            "idx": "\033[90m",         # gray
            "highlight": "\033[91m",   # bright red
            "accent": "\033[32m",      # green
            "warn": "\033[31m",        # red
            "info": "\033[35m",        # magenta
            "header_fg": "\033[97m",   # white
            "header_bg": "\033[44m",   # blue bg
            "bar": "\033[34m",         # blue
            "bookmark": "\033[33m",    # yellow/orange
            "text": "\033[30m",        # black
        }
    else:
        return {
            "reset": RESET, "bold": BOLD, "dim": DIM,
            "title": "\033[97m",       # bright white
            "date": "\033[96m",        # bright cyan
            "idx": "\033[90m",         # gray
            "highlight": "\033[91m",   # bright red
            "accent": "\033[92m",      # bright green
            "warn": "\033[91m",        # bright red
            "info": "\033[95m",        # bright magenta
            "header_fg": "\033[97m",   # white
            "header_bg": "\033[44m",   # blue bg
            "bar": "\033[94m",         # bright blue
            "bookmark": "\033[93m",    # bright yellow
            "text": "\033[37m",        # light gray
        }


THEME = make_theme(detect_light_theme())


def c(text, *keys):
    """Colorize text using theme keys."""
    codes = "".join(THEME.get(k, "") for k in keys)
    return codes + str(text) + THEME["reset"]


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def term_size():
    s = shutil.get_terminal_size((80, 24))
    return s.columns, s.lines


def clear_screen():
    if IS_WINDOWS:
        os.system("cls")
    else:
        print("\033[2J\033[H", end="", flush=True)


def _enable_win_ansi():
    """Enable ANSI escape codes on Windows 10+."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def read_key() -> str:
    """Read a single keypress, handling arrow keys and regular chars."""
    if IS_WINDOWS:
        return _read_key_windows()
    return _read_key_unix()


def _read_key_windows() -> str:
    """Windows key reader using msvcrt."""
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # special key prefix
        ch2 = msvcrt.getwch()
        if ch2 == "H":
            return "up"
        elif ch2 == "P":
            return "down"
        elif ch2 == "M":
            return "right"
        elif ch2 == "K":
            return "left"
        return "esc"
    elif ch == "\r":
        return "enter"
    elif ch == "\x08":  # Backspace
        return "backspace"
    elif ch == "\x03":  # Ctrl-C
        return "quit"
    elif ch == "\x04":  # Ctrl-D
        return "quit"
    elif ch == "\x1b":
        return "esc"
    return ch


def _read_key_unix() -> str:
    """Unix key reader using termios."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "up"
                elif ch3 == "B":
                    return "down"
                elif ch3 == "C":
                    return "right"
                elif ch3 == "D":
                    return "left"
            return "esc"
        elif ch == "\r" or ch == "\n":
            return "enter"
        elif ch == "\x7f" or ch == "\x08":
            return "backspace"
        elif ch == "\x03":
            return "quit"
        elif ch == "\x04":
            return "quit"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def input_line(prompt: str) -> str:
    """Regular line input (restores normal terminal mode)."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


class AbortPoller:
    """Non-blocking poll for Esc/q during long-running operations.

    Use as a context manager — it puts stdin in raw/non-blocking mode while
    active, drains any pending key presses, and restores the prior tty state
    on exit even if the wrapped block raises.

    Pass `poller.check` as `should_abort=` to scrape_site; it returns True
    once the user has pressed Esc or q.
    """

    def __init__(self):
        self.aborted = False
        self._fd = None
        self._old = None

    def __enter__(self):
        if not IS_WINDOWS:
            try:
                self._fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self._fd)
                # cbreak: no line buffering, but signals (Ctrl-C) still work.
                tty.setcbreak(self._fd)
            except Exception:
                self._fd = None
                self._old = None
        return self

    def __exit__(self, *exc):
        if not IS_WINDOWS and self._fd is not None and self._old is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:
                pass
        return False

    def check(self) -> bool:
        if self.aborted:
            return True
        try:
            if IS_WINDOWS:
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x1b", "q", "Q"):
                        self.aborted = True
                        return True
            else:
                if self._fd is None:
                    return False
                import select
                r, _, _ = select.select([sys.stdin], [], [], 0)
                while r:
                    ch = sys.stdin.read(1)
                    if ch in ("\x1b", "q", "Q"):
                        self.aborted = True
                        return True
                    r, _, _ = select.select([sys.stdin], [], [], 0)
        except Exception:
            return False
        return False


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

BOOKMARK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookmarks.json")


def load_bookmarks() -> list[dict]:
    if os.path.exists(BOOKMARK_FILE):
        with open(BOOKMARK_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_bookmarks(bookmarks: list[dict]):
    with open(BOOKMARK_FILE, "w", encoding="utf-8") as f:
        json.dump(bookmarks, f, ensure_ascii=False, indent=2)


def add_bookmark(post: dict):
    bookmarks = load_bookmarks()
    # Avoid duplicates by URL
    urls = {b.get("url") for b in bookmarks}
    if post.get("url") in urls:
        return False  # already bookmarked
    bookmarks.append({
        "date": post.get("date", ""),
        "title": post.get("title", ""),
        "url": post.get("url", ""),
    })
    save_bookmarks(bookmarks)
    return True


def remove_bookmark(url: str):
    bookmarks = load_bookmarks()
    bookmarks = [b for b in bookmarks if b.get("url") != url]
    save_bookmarks(bookmarks)


# ---------------------------------------------------------------------------
# Load CSV
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def find_latest_csv() -> str | None:
    csvs = [f for f in os.listdir(".") if f.startswith("posts_") and f.endswith(".csv")]
    if not csvs:
        return None
    return max(csvs, key=lambda f: os.path.getmtime(f))


def site_slug(site: dict) -> str:
    """Derive CSV filename slug from site config, e.g. 'rrn_com_tr'."""
    return site["name"].split("(")[-1].rstrip(")").replace(".", "_").replace("/", "")


def site_for_url(url: str) -> dict | None:
    """Match a post URL back to its site config by domain."""
    if not url:
        return None
    for s in SITES:
        domain = site_slug(s).replace("_", ".")
        if domain in url:
            return s
    return None


def _text_is_missing(text: str) -> bool:
    if not text:
        return True
    return text.startswith("(error") or text.startswith("(article content not found")


def csv_path_for_site(slug: str) -> str:
    """Canonical CSV path for a site: posts_{slug}.csv"""
    return f"posts_{slug}.csv"


def find_csv_for_site(slug: str) -> str | None:
    """Find CSV for a site. Prefers canonical name, falls back to dated files."""
    canonical = csv_path_for_site(slug)
    if os.path.exists(canonical):
        return canonical
    # Fallback: find latest dated file and rename it to canonical
    prefix = f"posts_{slug}_"
    matches = [f for f in os.listdir(".") if f.startswith(prefix) and f.endswith(".csv")]
    if not matches:
        return None
    def extract_date(fname):
        m = re.search(r'_(\d{8})\.csv$', fname)
        return m.group(1) if m else "00000000"
    latest = max(matches, key=extract_date)
    os.rename(latest, canonical)
    return canonical


def date_coverage(posts: list[dict]) -> str:
    """Return 'earliest — latest' date string from posts."""
    if not posts:
        return "no data"
    dates = [p.get("date", "") for p in posts if p.get("date")]
    if not dates:
        return "no dates"
    return f"{min(dates)}  —  {max(dates)}"


def scraped_to_csv_row(post: dict) -> dict:
    """Convert scraper output dict to CSV-compatible dict."""
    return {
        "date": post.get("date_str", ""),
        "title": post.get("title", ""),
        "url": post.get("url", ""),
        "text": post.get("text", ""),
    }


def merge_new_posts(existing: list[dict], new_posts: list[dict]) -> list[dict]:
    """Return only genuinely new posts (not already in existing), deduplicated."""
    has_url = any(p.get("url") for p in existing)
    if has_url:
        seen = {p.get("url") for p in existing if p.get("url")}
        return [p for p in new_posts if p.get("url") and p["url"] not in seen]
    else:
        seen = {(p.get("date", ""), p.get("title", "")) for p in existing}
        return [p for p in new_posts if (p.get("date", ""), p.get("title", "")) not in seen]


def save_merged_csv(posts: list[dict], csv_path: str):
    """Write post list back to CSV, preserving original column structure."""
    if not posts:
        return
    has_text = any(p.get("text") for p in posts)
    has_url = any(p.get("url") for p in posts)
    has_site = any(p.get("site") for p in posts)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        header = ["date", "title"]
        if has_url:
            header.append("url")
        if has_site:
            header.append("site")
        if has_text:
            header.append("text")
        writer.writerow(header)
        for p in posts:
            row = [p.get("date", ""), p.get("title", "")]
            if has_url:
                row.append(p.get("url", ""))
            if has_site:
                row.append(p.get("site", ""))
            if has_text:
                row.append(p.get("text", ""))
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_header(title: str):
    width = min(term_size()[0], 120)
    line = f" {title} ".center(width, "═")
    print(c(line, "header_bg", "header_fg", "bold"))


def build_monthly_counts(posts: list[dict], all_posts: list[dict]) -> tuple[list[str], list[int], list[int]]:
    """Count posts per month, filling ALL months in the range (even zeros)."""

    def parse_month(date_str: str) -> tuple[int, int] | None:
        """Extract (year, month) from various date formats."""
        m = re.match(r"(\d{4})-(\d{2})", date_str)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.match(r"\d{2}\.(\d{2})\.(\d{4})", date_str)
        if m:
            return (int(m.group(2)), int(m.group(1)))
        return None

    # Find min/max month from all_posts
    months_seen = set()
    for p in all_posts:
        mo = parse_month(p.get("date", ""))
        if mo:
            months_seen.add(mo)

    if not months_seen:
        return [], [], []

    min_ym = min(months_seen)
    max_ym = max(months_seen)

    # Generate every month from min to max
    all_months = []
    y, m = min_ym
    while (y, m) <= max_ym:
        all_months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    labels = [f"{y:04d}-{m:02d}" for y, m in all_months]

    # Count matches
    match_map: dict[tuple[int, int], int] = {}
    for p in posts:
        mo = parse_month(p.get("date", ""))
        if mo:
            match_map[mo] = match_map.get(mo, 0) + 1

    total_map: dict[tuple[int, int], int] = {}
    for p in all_posts:
        mo = parse_month(p.get("date", ""))
        if mo:
            total_map[mo] = total_map.get(mo, 0) + 1

    match_counts = [match_map.get(ym, 0) for ym in all_months]
    total_counts = [total_map.get(ym, 0) for ym in all_months]

    return labels, match_counts, total_counts


def render_chart(posts: list[dict], all_posts: list[dict], chart_height: int, chart_width: int) -> list[str]:
    """Render a vertical bar chart of monthly post counts. Returns lines to print."""
    labels, match_counts, total_counts = build_monthly_counts(posts, all_posts)
    if not labels:
        return [c("  No date data available for chart.", "dim")]

    MONTH_ABBR = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
    n = len(labels)

    # Header line
    lines = []
    lines.append(c(f"  Monthly distribution ({len(posts)} matching posts)", "bar", "bold"))

    # Layout: left_margin │ bars area
    left_margin = 7  # "  999 "
    avail_w = chart_width - left_margin - 2

    # Calculate per-bar width (min 1 char bar + 1 gap)
    col_w = max(2, avail_w // n)  # each column = bar + gap
    if col_w > 4:
        col_w = 4
    bar_w = max(1, col_w - 1)
    gap = col_w - bar_w

    # If columns overflow, shrink
    while n * col_w > avail_w and bar_w > 1:
        bar_w -= 1
        col_w = bar_w + gap
    if n * col_w > avail_w:
        gap = 0
        col_w = 1
        bar_w = 1

    # Chart body height (minus: header, axis line, month labels, year labels)
    body_h = max(3, chart_height - 4)

    max_count = max(match_counts) if match_counts else 1
    if max_count == 0:
        max_count = max(total_counts) if total_counts else 1
    if max_count == 0:
        max_count = 1

    # Render rows top-to-bottom
    for row in range(body_h, 0, -1):
        # Y-axis label
        if row == body_h:
            y_label = f"{max_count:>5} "
        elif row == (body_h + 1) // 2:
            y_label = f"{max_count // 2:>5} "
        elif row == 1:
            y_label = f"{'0':>5} "
        else:
            y_label = "      "

        bar_str = ""
        for i in range(n):
            cnt = match_counts[i]
            filled = (cnt / max_count) * body_h if max_count else 0
            if filled >= row:
                bar_str += c("█" * bar_w, "accent")
            elif filled >= row - 1 and filled > 0 and row == 1:
                # At least show a low block for small nonzero values
                bar_str += c("▁" * bar_w, "accent")
            elif filled >= row - 0.5 and filled > 0:
                bar_str += c("▄" * bar_w, "accent")
            else:
                bar_str += " " * bar_w
            bar_str += " " * gap

        lines.append(f"  {c(y_label, 'dim')}{c('│', 'bar')}{bar_str}")

    # X-axis line
    axis_w = n * col_w + 1
    lines.append(f"  {'':>5} {c('└' + '─' * axis_w, 'bar')}")

    # Month abbreviation labels
    month_line = "  " + " " * 6 + " "
    for i, lbl in enumerate(labels):
        m = int(lbl[5:7])
        ch = MONTH_ABBR[m - 1]
        month_line += c(ch, "date") + " " * (col_w - 1)
    lines.append(month_line)

    # Year labels — show year at Jan or at the first month
    year_line = "  " + " " * 6 + " "
    prev_year = ""
    for i, lbl in enumerate(labels):
        y_str = lbl[:4]
        m = int(lbl[5:7])
        if y_str != prev_year:
            yr_short = "'" + y_str[2:]
            year_line += c(yr_short, "dim")
            # pad remaining col width
            year_line += " " * max(0, col_w - len(yr_short))
            prev_year = y_str
        else:
            year_line += " " * col_w
    lines.append(year_line)

    return lines


def _highlight_terms(highlight) -> list[str]:
    """Normalize highlight argument (str | list[str] | None) into a clean list."""
    if not highlight:
        return []
    if isinstance(highlight, str):
        return [highlight]
    # de-dup and longest-first so longer phrases match before sub-words
    seen = set()
    terms = []
    for t in highlight:
        if t and t not in seen:
            seen.add(t)
            terms.append(t)
    terms.sort(key=len, reverse=True)
    return terms


def _highlight_pattern(highlight):
    terms = _highlight_terms(highlight)
    if not terms:
        return None
    return re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)


def print_post_line(i: int, post: dict, highlight=None,
                    bookmarked: bool = False, selected: bool = False,
                    read: bool = False):
    bm = c(" ★", "bookmark") if bookmarked else "  "
    idx_str = c(f"{i:>4}.", "idx")
    date_str = c(f"[{post.get('date', '')}]", "date")

    title = post.get("title", "")
    pattern = _highlight_pattern(highlight)
    if pattern and pattern.search(title):
        parts = pattern.split(title)
        matches = pattern.findall(title)
        title_str = ""
        base_style = ("dim",) if read else ("title", "bold")
        for j, part in enumerate(parts):
            title_str += c(part, *base_style)
            if j < len(matches):
                title_str += c(matches[j], "highlight", "bold")
    else:
        base_style = ("dim",) if read else ("title", "bold")
        title_str = c(title, *base_style)

    if selected:
        # Full-line reverse video with search term highlight
        RV = "\033[7m"  # reverse video
        RS = "\033[0m"  # reset
        HI = "\033[7m\033[91m\033[1m"  # reverse + bright red + bold for search match
        width = term_size()[0]
        bm_plain = " ★" if bookmarked else "  "
        date_val = post.get("date", "")
        prefix = f" ▸ {bm_plain} {i:>4}. [{date_val}] "

        sel_pattern = _highlight_pattern(highlight)
        if sel_pattern and sel_pattern.search(title):
            parts = sel_pattern.split(title)
            matches = sel_pattern.findall(title)
            title_out = ""
            for j, part in enumerate(parts):
                title_out += part
                if j < len(matches):
                    title_out += f"{RS}{HI}{matches[j]}{RS}{RV}"
        else:
            title_out = title

        plain_len = len(prefix) + len(title)
        padding = max(0, width - plain_len)
        print(f"{RV}{prefix}{title_out}{' ' * padding}{RS}")
    else:
        print(f"   {bm} {idx_str} {date_str} {title_str}")


STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "it", "its",
    "he", "she", "they", "we", "you", "i", "me", "him", "her", "us",
    "them", "my", "your", "his", "our", "their", "this", "that", "these",
    "those", "not", "no", "nor", "so", "if", "then", "than", "too",
    "very", "just", "about", "also", "more", "most", "some", "any",
    "all", "each", "every", "both", "few", "many", "much", "own",
    "such", "what", "which", "who", "whom", "how", "when", "where",
    "why", "up", "out", "into", "over", "after", "before", "between",
    "under", "again", "there", "here", "once", "during", "while",
    "as", "until", "because", "through", "above", "below", "said",
    "says", "according", "one", "two", "new", "first", "also", "like",
    "even", "back", "still", "well", "way", "get", "got", "make",
    "made", "going", "go", "been", "come", "came", "take", "took",
    "know", "see", "think", "tell", "told", "say", "let", "keep",
    "give", "gave", "find", "found", "want", "need", "use", "used",
    "work", "call", "called", "try", "ask", "put", "set", "run",
    "part", "long", "great", "little", "man", "old", "right", "big",
    "high", "small", "large", "next", "early", "young", "last",
    "good", "same", "able", "around", "another", "since", "against",
    "only", "other", "however", "among", "per", "within", "without",
    "de", "ve", "bir", "bu", "da", "den", "ile", "icin", "olan",
}


def render_word_cloud(text: str) -> list[str]:
    """Build an elliptical, centered terminal word cloud. Returns lines."""
    import math
    import random

    # Tokenize and count
    words = re.findall(r"[a-zA-ZÀ-ÿ\u0400-\u04FF]{3,}", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in STOP_WORDS and len(w) >= 3:
            freq[w] = freq.get(w, 0) + 1

    if not freq:
        return [c("  (not enough text for word cloud)", "dim")]

    sorted_words = sorted(freq.items(), key=lambda x: -x[1])[:50]
    max_freq = sorted_words[0][1] if sorted_words else 1

    RS = "\033[0m"

    # 7 color tiers — vivid to dim
    COLORS = [
        "\033[97m\033[41m\033[1m",  # white on red bg, bold  (tier 0 — top)
        "\033[91m\033[1m",          # bright red bold
        "\033[93m\033[1m",          # bright yellow bold
        "\033[92m\033[1m",          # bright green bold
        "\033[96m",                 # bright cyan
        "\033[94m",                 # bright blue
        "\033[90m",                 # dark gray              (tier 6 — lowest)
    ]

    # Assign display text, visual width, and color per word
    items = []  # (display_str, plain_width, tier)
    for word, cnt in sorted_words:
        ratio = cnt / max_freq
        if ratio > 0.85:
            tier = 0
            disp = " " + " ".join(word.upper()) + " "  # L E T T E R  S P A C E D
        elif ratio > 0.65:
            tier = 1
            disp = word.upper()
        elif ratio > 0.45:
            tier = 2
            disp = word.upper()
        elif ratio > 0.30:
            tier = 3
            disp = word.capitalize()
        elif ratio > 0.18:
            tier = 4
            disp = word.capitalize()
        elif ratio > 0.08:
            tier = 5
            disp = word
        else:
            tier = 6
            disp = word
        items.append((disp, len(disp), tier))

    tw, _ = term_size()
    cloud_w = min(tw - 2, 110)
    n_lines = max(8, min(20, len(items) // 2))

    # Separate top-tier items to place in the widest rows
    top_items = [it for it in items if it[2] <= 1]
    mid_items = [it for it in items if 2 <= it[2] <= 3]
    low_items = [it for it in items if it[2] >= 4]
    random.shuffle(mid_items)
    random.shuffle(low_items)

    # Build pool ordered: low, mid, top, mid, low — so top ends up in middle rows
    pool_order = low_items[:len(low_items)//2] + mid_items[:len(mid_items)//2] + \
                 top_items + mid_items[len(mid_items)//2:] + low_items[len(low_items)//2:]

    # Compute max width per row using ellipse shape: width = cloud_w * sin(π * row/n_lines)
    row_widths = []
    for row in range(n_lines):
        t = (row + 0.5) / n_lines  # 0..1
        w = int(cloud_w * math.sin(math.pi * t))
        w = max(12, w)
        row_widths.append(w)

    # Fill rows greedily from pool
    rows: list[list[tuple[str, int, int]]] = [[] for _ in range(n_lines)]
    row_used = [0] * n_lines

    for item in pool_order:
        disp, width, tier = item
        best_row = -1
        best_score = float('inf')
        for r in range(n_lines):
            space_left = row_widths[r] - row_used[r]
            if width + 2 <= space_left or row_used[r] == 0:
                score = abs(space_left - width)
                if tier <= 1:
                    middle_dist = abs(r - n_lines // 2)
                    score += middle_dist * 3
                if score < best_score:
                    best_score = score
                    best_row = r
        if best_row >= 0:
            rows[best_row].append(item)
            row_used[best_row] += width + 3

    # Render
    lines = []
    lines.append("")
    header = "☁  W O R D   C L O U D  ☁"
    lines.append(COLORS[1] + header.center(cloud_w) + RS)
    lines.append("")

    for r in range(n_lines):
        if not rows[r]:
            continue
        parts = []
        plain_len = 0
        for disp, width, tier in rows[r]:
            color = COLORS[min(tier, len(COLORS) - 1)]
            sep = "   " if parts else ""
            parts.append(f"{sep}{color}{disp}{RS}")
            plain_len += len(sep) + width

        line_content = "".join(parts)
        pad_left = max(0, (tw - plain_len) // 2)
        lines.append(" " * pad_left + line_content)

    lines.append("")
    return lines


def show_word_cloud(posts_or_post, title: str = ""):
    """Full-screen word cloud for a single article or a list of posts."""
    if isinstance(posts_or_post, dict):
        text = posts_or_post.get("text", "")
        title = title or posts_or_post.get("title", "")
    else:
        # List of posts — combine all texts
        text = "\n\n".join(p.get("text", "") for p in posts_or_post if p.get("text"))
        title = title or f"Word cloud from {len(posts_or_post)} articles"
    if not text:
        return
    clear_screen()
    width = min(term_size()[0], 120)
    print(c("─" * width, "bar"))
    print(c(title, "title", "bold"))
    print(c("─" * width, "bar"))
    cloud_lines = render_word_cloud(text)
    for line in cloud_lines:
        print(line)
    print()
    print(c("  [backspace] back", "dim"))
    while True:
        key = read_key()
        if key in ("backspace", "q", "esc", "quit"):
            break


def show_post_detail(post: dict, highlight=None):
    width = min(term_size()[0], 120)
    clear_screen()
    print(c("─" * width, "bar"))

    title = post.get("title", "")
    title_pattern = _highlight_pattern(highlight)
    if title_pattern and title_pattern.search(title):
        parts = title_pattern.split(title)
        matches = title_pattern.findall(title)
        for j, part in enumerate(parts):
            print(c(part, "title", "bold"), end="")
            if j < len(matches):
                print(c(matches[j], "highlight", "bold"), end="")
        print()
    else:
        print(c(title, "title", "bold"))

    print(c(post.get("date", ""), "date") + "  " + c(post.get("url", ""), "dim"))
    if post.get("site"):
        print(c(f"Site: {post['site']}", "info"))
    print(c("─" * width, "bar"))

    if _text_is_missing(post.get("text", "")):
        print(c("  Fetching article text...", "dim"), end="\r", flush=True)
        ensure_article_text(post)
        print(" " * 40, end="\r", flush=True)

    text = post.get("text", "")
    if text and not _text_is_missing(text):
        body_pattern = _highlight_pattern(highlight)
        if body_pattern and body_pattern.search(text):
            parts = body_pattern.split(text)
            matches = body_pattern.findall(text)
            for j, part in enumerate(parts):
                print(c(part, "text"), end="")
                if j < len(matches):
                    print(c(matches[j], "highlight", "bold"), end="")
            print()
        else:
            print(c(text, "text"))
    else:
        print(c("  (no article text available)", "dim"))
    print(c("─" * width, "bar"))

    # Tags on this article
    _tags = get_tags(load_state(), post.get("url", ""))
    if _tags:
        print(c("  Tags   ", "dim") + c(" · ".join(_tags), "accent", "bold"))
        print(c("─" * width, "bar"))

    # Bookmark status and controls
    url = post.get("url", "")
    bookmarks = load_bookmarks()
    is_bm = url in {b.get("url") for b in bookmarks}
    has_text = not _text_is_missing(post.get("text", ""))
    r_hint = "[r] mark unread" if is_read(load_state(), url) else "[r] mark read"
    wc_hint = "  [w] word cloud" if has_text else ""
    search_hint = "  [/] search" if has_text else ""
    if is_bm:
        print(c("  ★ Bookmarked", "bookmark"), end="")
        print(c(f"  |  [b] remove bookmark  {r_hint}  [g] tag{wc_hint}{search_hint}  [backspace] back", "dim"))
    else:
        print(c(f"  [b] bookmark  {r_hint}  [g] tag{wc_hint}{search_hint}  [backspace] back", "dim"))

    while True:
        key = read_key()
        if key == "/" and has_text:
            print()
            term = input_line(c("  Search in article: ", "accent"))
            if term:
                return show_post_detail(post, highlight=term)
            return show_post_detail(post, highlight)
        elif key == "w" and has_text:
            show_word_cloud(post)
            # Re-render detail after returning from cloud
            return show_post_detail(post, highlight)
        elif key == "b":
            if is_bm:
                remove_bookmark(url)
            else:
                add_bookmark(post)
            return show_post_detail(post, highlight)  # re-render with updated bookmark
        elif key == "r":
            if url:
                _state = load_state()
                new_value = not is_read(_state, url)
                set_read(_state, url, new_value)
                save_state(_state)
                print(c("  ✓ marked " + ("read" if new_value else "unread"),
                        "accent", "bold"), end="\r", flush=True)
                time.sleep(0.6)
            return show_post_detail(post, highlight)  # re-render with updated read state
        elif key == "g":
            _state = load_state()
            tag_picker(_state, post.get("url", ""))
            return show_post_detail(post, highlight)  # re-render with updated tags
        elif key in ("backspace", "q", "esc", "quit"):
            break


def parse_post_datetime(s: str) -> datetime | None:
    """Parse any of the date formats used across SITES into a datetime."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y, %H:%M", "%d.%m.%Y %H:%M",
                "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Year-sharded CSV management
# ---------------------------------------------------------------------------

def find_shards(slug: str) -> list[tuple[int, str]]:
    """Return (year, path) tuples for all year shards of a site, sorted oldest→newest."""
    pattern = re.compile(rf"^posts_{re.escape(slug)}_(\d{{4}})\.csv$")
    results = []
    for f in os.listdir("."):
        m = pattern.match(f)
        if m:
            results.append((int(m.group(1)), f))
    return sorted(results)


def shard_path(slug: str, year: int) -> str:
    return f"posts_{slug}_{year}.csv"


def load_shards(slug: str, years: list[int] | None = None) -> list[dict]:
    """Load year shards for slug (newest-first). years=None loads all shards."""
    all_shards = find_shards(slug)
    if years is not None:
        shards = [(y, p) for y, p in all_shards if y in years]
    else:
        shards = all_shards
    posts = []
    for _, path in sorted(shards, reverse=True):
        if os.path.exists(path):
            try:
                posts.extend(load_csv(path))
            except Exception:
                pass
    return posts


def count_shards(slug: str) -> int:
    """Fast post count across all year shards without loading full dicts."""
    total = 0
    for _, path in find_shards(slug):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)
                next(reader, None)  # skip header
                total += sum(1 for _ in reader)
        except Exception:
            pass
    return total


def save_sharded(posts: list[dict], slug: str):
    """Split posts by year and write each shard; does not touch unrepresented years."""
    by_year: dict[int, list[dict]] = {}
    for p in posts:
        dt = parse_post_datetime(p.get("date", ""))
        year = dt.year if dt else datetime.now().year
        by_year.setdefault(year, []).append(p)
    for year, yr_posts in by_year.items():
        save_merged_csv(yr_posts, shard_path(slug, year))


def merge_into_shards(new_posts: list[dict], slug: str) -> int:
    """Dedup new_posts against all shards and prepend additions to their year shards."""
    all_existing = load_shards(slug)
    additions = merge_new_posts(all_existing, new_posts)
    if not additions:
        return 0
    by_year: dict[int, list[dict]] = {}
    for p in additions:
        dt = parse_post_datetime(p.get("date", ""))
        year = dt.year if dt else datetime.now().year
        by_year.setdefault(year, []).append(p)
    for year, yr_adds in by_year.items():
        path = shard_path(slug, year)
        existing_yr = load_csv(path) if os.path.exists(path) else []
        save_merged_csv(yr_adds + existing_yr, path)
    return len(additions)


def ensure_article_text(post: dict) -> bool:
    """If post text is missing/errored, fetch it and persist to its year shard."""
    if not _text_is_missing(post.get("text", "")):
        return False
    url = post.get("url", "")
    site = site_for_url(url)
    if not site or not site.get("article_content_selector"):
        return False
    try:
        new_text = fetch_article_text(url, site["article_content_selector"])
    except Exception:
        return False
    if not new_text or new_text.startswith("(article content not found"):
        return False
    post["text"] = new_text
    slug = site_slug(site)
    dt = parse_post_datetime(post.get("date", ""))
    year = dt.year if dt else datetime.now().year
    path = shard_path(slug, year)
    if os.path.exists(path):
        rows = load_csv(path)
        for r in rows:
            if r.get("url") == url:
                r["text"] = new_text
                break
        save_merged_csv(rows, path)
    return True


def migrate_to_shards(slug: str):
    """One-time: split legacy posts_{slug}.csv into per-year shards."""
    legacy = f"posts_{slug}.csv"
    if not os.path.exists(legacy) or find_shards(slug):
        return
    try:
        posts = load_csv(legacy)
        if posts:
            save_sharded(posts, slug)
        os.rename(legacy, f"posts_{slug}.csv.bak")
    except Exception as e:
        print(c(f"  Warning: shard migration failed for {slug}: {e}", "warn"))


def recent_years() -> list[int]:
    now = datetime.now()
    return [now.year - 1, now.year]


# ---------------------------------------------------------------------------
# Compound filter (`f` key) — title/text words with ANY/ALL, date range,
# multi-site selection. Produced by filter_form(), applied by apply_filter().
# ---------------------------------------------------------------------------

@dataclass
class FilterSpec:
    title_words: list[str] = field(default_factory=list)
    title_mode: str = "any"   # "any" | "all"
    text_words: list[str] = field(default_factory=list)
    text_mode: str = "any"
    date_from: datetime | None = None
    date_to: datetime | None = None
    site_slugs: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.title_words or self.text_words
                    or self.date_from or self.date_to)

    def summary(self) -> str:
        parts = []
        if self.title_words:
            parts.append(f"title {self.title_mode.upper()}=[{', '.join(self.title_words)}]")
        if self.text_words:
            parts.append(f"text {self.text_mode.upper()}=[{', '.join(self.text_words)}]")
        if self.date_from or self.date_to:
            a = self.date_from.strftime("%Y-%m-%d") if self.date_from else "any"
            b = self.date_to.strftime("%Y-%m-%d") if self.date_to else "any"
            parts.append(f"{a}..{b}")
        if len(self.site_slugs) == 1:
            parts.append(self.site_slugs[0])
        elif len(self.site_slugs) > 1:
            parts.append(f"{len(self.site_slugs)} sites")
        return " · ".join(parts) if parts else "(no constraints)"


def _match_words(haystack: str, words: list[str], mode: str) -> bool:
    if not words:
        return True
    h = haystack.lower()
    def hit(w: str) -> bool:
        return re.search(r"\b" + re.escape(w) + r"\b", h) is not None
    if mode == "all":
        return all(hit(w) for w in words)
    return any(hit(w) for w in words)


def apply_filter(post: dict, spec: FilterSpec) -> bool:
    if not _match_words(post.get("title") or "", spec.title_words, spec.title_mode):
        return False
    if not _match_words(post.get("text") or "", spec.text_words, spec.text_mode):
        return False
    if spec.date_from is not None or spec.date_to is not None:
        d = parse_post_datetime(post.get("date", ""))
        if d is None:
            return False
        if spec.date_from is not None and d < spec.date_from:
            return False
        if spec.date_to is not None and d > spec.date_to:
            return False
    return True


def filter_form(default_site_slug: str | None) -> FilterSpec | None:
    """Modal form for compound filtering. Returns FilterSpec or None on cancel."""
    spec = FilterSpec()
    if default_site_slug is not None:
        spec.site_slugs = [default_site_slug]
    else:
        spec.site_slugs = [site_slug(s) for s in SITES]

    site_options = [(site_slug(s), s["name"]) for s in SITES]

    # Build the row layout. Each row is (kind, key, label).
    # kinds: title_words, title_mode, text_words, text_mode,
    #        date_from, date_to, site (with index in payload), action.
    def build_rows():
        rows = [
            ("title_words", None, "Title contains"),
            ("title_mode",  None, "Title match"),
            ("text_words",  None, "Text contains"),
            ("text_mode",   None, "Text match"),
            ("date_from",   None, "Date from"),
            ("date_to",     None, "Date to"),
            ("header",      None, "Sites"),
        ]
        for i, (slug, name) in enumerate(site_options):
            rows.append(("site", i, name))
        rows.extend([
            ("action", "apply",  "Apply"),
            ("action", "reset",  "Reset"),
            ("action", "cancel", "Cancel"),
        ])
        return rows

    rows = build_rows()
    # Cursor must land on a focusable row; "header" is not focusable.
    def focusable(idx: int) -> bool:
        return rows[idx][0] != "header"

    cursor = 0
    while not focusable(cursor):
        cursor += 1

    warning = ""

    while True:
        clear_screen()
        print()
        print(c("  Filter posts", "title", "bold"))
        if not spec.is_empty() or len(spec.site_slugs) != len(SITES):
            print(c(f"    {spec.summary()}", "dim"))
        print()

        for idx, (kind, payload, label) in enumerate(rows):
            sel = (idx == cursor)
            arrow = c("  ▸ ", "accent") if sel else "    "

            if kind == "title_words":
                val = ", ".join(spec.title_words) if spec.title_words else "(empty)"
                print(arrow + c(f"{label}: ", "title", "bold" if sel else "dim") +
                      c(val, "accent" if sel else "info"))
            elif kind == "title_mode":
                print(arrow + c(f"{label}: ", "title", "bold" if sel else "dim") +
                      c(spec.title_mode.upper(), "accent" if sel else "info"))
            elif kind == "text_words":
                val = ", ".join(spec.text_words) if spec.text_words else "(empty)"
                print(arrow + c(f"{label}: ", "title", "bold" if sel else "dim") +
                      c(val, "accent" if sel else "info"))
            elif kind == "text_mode":
                print(arrow + c(f"{label}: ", "title", "bold" if sel else "dim") +
                      c(spec.text_mode.upper(), "accent" if sel else "info"))
            elif kind == "date_from":
                val = spec.date_from.strftime("%Y-%m-%d") if spec.date_from else "any"
                print(arrow + c(f"{label}: ", "title", "bold" if sel else "dim") +
                      c(val, "accent" if sel else "info"))
            elif kind == "date_to":
                val = spec.date_to.strftime("%Y-%m-%d") if spec.date_to else "any"
                print(arrow + c(f"{label}: ", "title", "bold" if sel else "dim") +
                      c(val, "accent" if sel else "info"))
            elif kind == "header":
                print()
                print("    " + c(label, "title", "bold") +
                      c("   (Space toggle, A=all, N=none)", "dim"))
            elif kind == "site":
                slug, name = site_options[payload]
                checked = slug in spec.site_slugs
                box = c("[×]", "accent") if checked else c("[ ]", "dim")
                style = "title" if sel else ("info" if checked else "dim")
                print(arrow + box + " " + c(name, style, "bold" if sel else style))
            elif kind == "action":
                if payload == "apply":
                    print()
                    print(arrow + c(f"[ {label} ]", "accent", "bold" if sel else "accent"))
                elif payload == "reset":
                    print(arrow + c(f"[ {label} ]", "info", "bold" if sel else "info"))
                else:
                    print(arrow + c(f"[ {label} ]", "warn", "bold" if sel else "warn"))

        print()
        if warning:
            print(c(f"  {warning}", "warn"))
            warning = ""
        print(c("  ↑↓ navigate · Enter activate · Space toggle · g apply · Esc cancel", "dim"))

        key = read_key()

        if key in ("esc", "quit"):
            return None
        if key == "up":
            i = cursor - 1
            while i >= 0 and not focusable(i):
                i -= 1
            if i >= 0:
                cursor = i
            continue
        if key == "down":
            i = cursor + 1
            while i < len(rows) and not focusable(i):
                i += 1
            if i < len(rows):
                cursor = i
            continue

        kind, payload, label = rows[cursor]

        # Global apply hotkey
        if key == "g":
            key = "enter"
            kind, payload = "action", "apply"

        if kind == "title_words":
            if key == "enter":
                term = input_line(c("  Title — comma-separate phrases (e.g. merkel, tax reform): ", "accent"))
                spec.title_words = [w.strip().lower() for w in term.split(",") if w.strip()] if term else []
            elif key == "backspace":
                spec.title_words = []
        elif kind == "title_mode":
            if key in ("enter", " ", "space"):
                spec.title_mode = "all" if spec.title_mode == "any" else "any"
        elif kind == "text_words":
            if key == "enter":
                term = input_line(c("  Text — comma-separate phrases (e.g. wirtschaft, steuer reform): ", "accent"))
                spec.text_words = [w.strip().lower() for w in term.split(",") if w.strip()] if term else []
            elif key == "backspace":
                spec.text_words = []
        elif kind == "text_mode":
            if key in ("enter", " ", "space"):
                spec.text_mode = "all" if spec.text_mode == "any" else "any"
        elif kind == "date_from":
            if key == "enter":
                picked = date_picker("Filter — date from:", spec.date_from or datetime.now())
                if picked is not None:
                    spec.date_from = picked
            elif key == "backspace":
                spec.date_from = None
        elif kind == "date_to":
            if key == "enter":
                default = spec.date_to or datetime.now()
                picked = date_picker("Filter — date to:", default)
                if picked is not None:
                    spec.date_to = picked.replace(hour=23, minute=59, second=59)
            elif key == "backspace":
                spec.date_to = None
        elif kind == "site":
            slug = site_options[payload][0]
            if key in ("enter", " ", "space"):
                if slug in spec.site_slugs:
                    spec.site_slugs = [s for s in spec.site_slugs if s != slug]
                else:
                    spec.site_slugs = spec.site_slugs + [slug]
            elif key in ("a", "A"):
                spec.site_slugs = [s for s, _ in site_options]
            elif key in ("n", "N"):
                spec.site_slugs = []
        elif kind == "action":
            if key == "enter":
                if payload == "apply":
                    if not spec.site_slugs:
                        warning = "Pick at least one site."
                        continue
                    return spec
                if payload == "reset":
                    spec = FilterSpec()
                    if default_site_slug is not None:
                        spec.site_slugs = [default_site_slug]
                    else:
                        spec.site_slugs = [site_slug(s) for s in SITES]
                if payload == "cancel":
                    return None


def run_filter(spec: FilterSpec, current_slug: str | None,
               current_posts: list[dict] | None) -> list[dict]:
    """Load posts from selected sites, apply spec, return result list.

    If exactly one site is selected and that's the current view, reuse the
    in-memory `current_posts` to avoid a redundant disk read. Otherwise load
    each selected site's shards and annotate posts with `_site` + a prefixed
    title (the same convention global_search uses for cross-site display).
    """
    selected = set(spec.site_slugs)
    can_reuse = (current_slug is not None and current_posts is not None
                 and selected == {current_slug})
    if can_reuse:
        return [p for p in current_posts if apply_filter(p, spec)]

    results = []
    for s in SITES:
        slug = site_slug(s)
        if slug not in selected:
            continue
        for p in load_shards(slug):
            if not apply_filter(p, spec):
                continue
            p_copy = dict(p)
            if len(selected) > 1:
                p_copy["_site"] = s["name"]
                p_copy["title"] = f"[{s['name']}]  {p_copy.get('title', '')}"
            results.append(p_copy)
    return results


def tag_picker(state: dict, url: str):
    """Cursor-driven multi-select of tags for `url`, plus add-new. Mutates+saves state."""
    if not url:
        return
    cursor = 0
    while True:
        existing = all_tags(state)
        current = set(get_tags(state, url))
        if cursor >= len(existing):
            cursor = max(0, len(existing) - 1)
        clear_screen()
        print_header("Tags")
        print()
        if existing:
            for idx, t in enumerate(existing):
                checked = t in current
                box = "[x]" if checked else "[ ]"
                if idx == cursor:
                    print(f"  {c('▸', 'accent')} {c(box, 'accent', 'bold')} {c(t, 'title', 'bold')}")
                else:
                    style = ("accent",) if checked else ("dim",)
                    print(f"    {c(box, *style)} {c(t, *style)}")
        else:
            print(c("  No tags yet — press [n] to add one.", "dim"))
        print()
        print(c("  Current: ", "dim") + (c(" · ".join(sorted(current)), "accent")
                                         if current else c("(none)", "dim")))
        print()
        print(c("  ↑↓ move   Space toggle   [n] new tag   [backspace] done", "dim"))

        key = read_key()
        if key in ("backspace", "esc", "enter", "q", "quit"):
            break
        elif key == "up":
            if existing:
                cursor = (cursor - 1) % len(existing)
        elif key == "down":
            if existing:
                cursor = (cursor + 1) % len(existing)
        elif key == "n":
            print()
            new = input_line(c("  New tag: ", "accent"))
            if new:
                tags = get_tags(state, url)
                tags.append(new)
                set_tags(state, url, tags)
                save_state(state)
        elif key == " ":
            if existing and 0 <= cursor < len(existing):
                t = existing[cursor]
                tags = get_tags(state, url)
                if t in tags:
                    tags = [x for x in tags if x != t]
                else:
                    tags.append(t)
                set_tags(state, url, tags)
                save_state(state)


def pick_tag(state: dict):
    """Let the user choose one tag to filter by. Returns the tag string or None."""
    tags = all_tags(state)
    if not tags:
        clear_screen()
        print(c("\n  No tags yet. Press any key...", "dim"))
        read_key()
        return None
    cursor = 0
    while True:
        clear_screen()
        print_header("Filter by tag")
        print()
        for i, t in enumerate(tags):
            if i == cursor:
                print(f"  {c('▸', 'accent')} {c(t, 'title', 'bold')}")
            else:
                print(f"    {c(t, 'dim')}")
        print()
        print(c("  ↑↓ choose   Enter select   [backspace] cancel", "dim"))
        key = read_key()
        if key in ("backspace", "esc", "q", "quit"):
            return None
        elif key == "up":
            cursor = (cursor - 1) % len(tags)
        elif key == "down":
            cursor = (cursor + 1) % len(tags)
        elif key == "enter":
            return tags[cursor]


def paginate_posts(posts: list[dict], highlight: str | None = None, all_posts: list[dict] | None = None,
                   db_total: int | None = None, site: dict | None = None,
                   slug: str | None = None, all_loaded: bool = True,
                   resume: dict | None = None):
    """Display posts with cursor navigation. ↑↓ moves selection, Enter opens article."""
    if all_posts is None:
        all_posts = posts
    can_scrape = site is not None

    show_chart = False
    cursor = 0
    bookmarks = load_bookmarks()
    bm_urls = {b.get("url") for b in bookmarks}
    state = load_state()
    has_text = any(p.get("text") for p in posts) if posts else False

    # Date filter state
    filter_kind = None     # None | "month" | "day" | "tag"
    filter_value = None    # None | (year, month) | datetime

    if resume:
        fk = resume.get("filter_kind")
        filter_kind, filter_value = _deserialize_filter(fk, resume.get("filter_value"))
        cursor = resume.get("cursor", 0)
        open_url = resume.get("open_url")
        if open_url:
            match = next((p for p in posts if p.get("url") == open_url), None)
            if match is not None:
                show_post_detail(match)
                set_read(state, open_url, True)
                save_state(state)

    while True:
        # Derive displayed list from filter
        if filter_kind is None:
            display = posts
            filter_label = None
        elif filter_kind == "tag":
            display = [p for p in posts
                       if filter_value in get_tags(state, p.get("url", ""))]
            filter_label = f"tag: {filter_value}"
        else:
            display = []
            for p in posts:
                d = parse_post_datetime(p.get("date", ""))
                if d is None:
                    continue
                if filter_kind == "month":
                    fy, fm = filter_value
                    if d.year == fy and d.month == fm:
                        display.append(p)
                else:  # "day"
                    if (d.year, d.month, d.day) == (filter_value.year, filter_value.month, filter_value.day):
                        display.append(p)
            if filter_kind == "month":
                filter_label = f"{MONTH_NAMES[filter_value[1] - 1]} {filter_value[0]}"
            else:
                filter_label = filter_value.strftime("%Y-%m-%d")

        total = len(display)
        if cursor >= total:
            cursor = max(0, total - 1)

        clear_screen()
        tw, term_h = term_size()

        # Filter strip (top)
        if filter_label:
            left = c("  Filter: ", "dim") + c(filter_label, "accent", "bold") + c(f"   {total}/{len(posts)}", "dim")
            right = c("[d] day  [m] month  [c] clear", "dim")
        else:
            left = c(f"  {len(posts)} posts", "dim")
            right = c("[d] go to date  [m] jump to month", "dim")
        print(left + "    " + right)

        chart_lines = []
        if show_chart:
            chart_height = max(5, (term_h * 2) // 5)
            chart_lines = render_chart(display, all_posts, chart_height, min(tw, 120))
            page_size = max(3, term_h - len(chart_lines) - 4)
        else:
            page_size = max(3, term_h - 4)

        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        page = cursor // page_size if total else 0
        start = page * page_size
        end = min(start + page_size, total)

        if total == 0:
            print()
            print(c("  No posts match this filter.", "warn"))
            print(c("  [c] clear filter   [m] pick another month   [backspace] back", "dim"))
        else:
            for i in range(start, end):
                post = display[i]
                is_bm = post.get("url") in bm_urls
                is_sel = (i == cursor)
                print_post_line(i + 1, post, highlight, bookmarked=is_bm,
                                selected=is_sel, read=is_read(state, post.get("url", "")))

        if show_chart and chart_lines:
            print()
            for line in chart_lines:
                print(line)

        print()
        has_any_text = any(p.get("text") for p in display) if display else False
        cur_disp = (cursor + 1) if total else 0
        filter_hint = f"  filter: {filter_kind}" if filter_kind else ""
        update_hint = "  [u] update" if can_scrape else ""
        r_hint = "[r] mark unread" if (total > 0 and is_read(state, display[cursor].get("url", ""))) else "[r] mark read"
        print(
            c(f" {cur_disp}/{total} ", "accent") +
            c(f" p.{page + 1}/{total_pages} ", "dim") +
            c(f" ↑↓ move  ←→ page  [s] title  {r_hint}  [g] tag  [G] tag filter  [b] bookmark  [B] bookmarks{filter_hint}{update_hint}  [?] help", "dim")
        )
        with _update_lock:
            _pending_v = _update_status["remote_version"] if _update_status["downloaded"] else None
        if _pending_v:
            print(c(f"  ★ Stella v{_pending_v} ready — press A to update now", "accent", "bold"))

        key = read_key()

        if key in ("backspace", "esc", "quit"):
            break
        elif key == "q":
            if can_scrape:
                sys.exit(0)
            else:
                break
        elif key == "down":
            if cursor < total - 1:
                cursor += 1
        elif key == "up":
            if cursor > 0:
                cursor -= 1
        elif key in ("right", "n"):
            next_start = (page + 1) * page_size
            cursor = min(next_start, max(total - 1, 0))
        elif key in ("left", "p"):
            prev_start = (page - 1) * page_size
            cursor = max(prev_start, 0)
        elif key == "w" and has_any_text:
            hl_label = ", ".join(_highlight_terms(highlight))
            show_word_cloud(display, title=f"Word cloud — {len(display)} articles" +
                            (f" matching '{hl_label}'" if hl_label else ""))
        elif key == "enter" and total > 0:
            post = display[cursor]
            set_read(state, post.get("url", ""), True)  # opening marks read
            save_state(state)
            show_post_detail(post, highlight)
            state = load_state()  # pick up any in-detail read toggle
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
        elif key == "r" and total > 0:
            url = display[cursor].get("url", "")
            if url:
                set_read(state, url, not is_read(state, url))
                save_state(state)
        elif key == "g" and total > 0:
            tag_picker(state, display[cursor].get("url", ""))
            state = load_state()
        elif key == "G":
            chosen = pick_tag(state)
            if chosen is not None:
                filter_kind = "tag"
                filter_value = chosen
                cursor = 0
        elif key == "A":
            with _update_lock:
                pend = _update_status["downloaded"]
                pend_v = _update_status["remote_version"]
            if pend and confirm_modal(f"Update to Stella v{pend_v} now?"):
                resume = {
                    "slug": slug,
                    "cursor": cursor,
                    "filter_kind": filter_kind,
                    "filter_value": _serialize_filter(filter_kind, filter_value),
                    "open_url": None,
                }
                apply_update_and_relaunch(state, resume)
        elif key == "b" and total > 0:
            post = display[cursor]
            if post.get("url") in bm_urls:
                remove_bookmark(post.get("url", ""))
                bm_urls.discard(post.get("url"))
            else:
                add_bookmark(post)
                bm_urls.add(post.get("url"))
            bookmarks = load_bookmarks()
        elif key == "B":
            show_bookmarks(posts)
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
        elif key == "t":
            show_chart = not show_chart
        elif key == "d":
            default = datetime.now()
            if filter_kind == "day" and filter_value:
                default = filter_value
            elif filter_kind == "month" and filter_value:
                default = datetime(filter_value[0], filter_value[1], 1)
            picked = date_picker("Filter — pick a day:", default)
            if picked is not None:
                filter_kind = "day"
                filter_value = picked
                cursor = 0
        elif key == "m":
            default = datetime.now()
            if filter_kind == "day" and filter_value:
                default = filter_value
            elif filter_kind == "month" and filter_value:
                default = datetime(filter_value[0], filter_value[1], 1)
            picked = month_picker("Filter — pick year & month:", default)
            if picked is not None:
                filter_kind = "month"
                filter_value = picked
                cursor = 0
        elif key == "c":
            filter_kind = None
            filter_value = None
            cursor = 0
        elif key == "y" and total > 0:
            url = display[cursor].get("url", "")
            if url:
                ok = copy_to_clipboard(url)
                msg = "  ✓ URL copied" if ok else "  (no clipboard tool found)"
                print(c(msg, "accent" if ok else "warn"), end="\r", flush=True)
                time.sleep(0.8)
        elif key == "?":
            show_help()
        elif key == "s":
            print()
            term = input_line(c("  Search titles: ", "accent"))
            if term:
                results = [p for p in posts if term.lower() in p.get("title", "").lower()]
                if results:
                    paginate_posts(results, highlight=term, all_posts=all_posts)
                else:
                    clear_screen()
                    print(c(f"\n  No results for '{term}'", "warn"))
                    read_key()
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
        elif key == "S" and has_text:
            print()
            term = input_line(c("  Search article text: ", "accent"))
            if term:
                results = [p for p in posts if term.lower() in p.get("text", "").lower()]
                if results:
                    paginate_posts(results, highlight=term, all_posts=all_posts)
                else:
                    clear_screen()
                    print(c(f"\n  No results for '{term}'", "warn"))
                    read_key()
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
        elif key == "f":
            spec = filter_form(default_site_slug=slug)
            if spec is not None:
                results = run_filter(spec, current_slug=slug, current_posts=posts)
                if results:
                    hl = spec.title_words + spec.text_words
                    paginate_posts(results, highlight=hl or None, all_posts=results)
                else:
                    clear_screen()
                    print(c(f"\n  No results for filter: {spec.summary()}", "warn"))
                    print(c("  Press any key...", "dim"))
                    read_key()
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
        elif key == "u" and can_scrape:
            posts = quick_update(site, posts, slug)
            has_text = any(p.get("text") for p in posts) if posts else False
            all_posts = posts
            cursor = 0
        elif key == "U" and can_scrape:
            result = window_scrape(site, posts, slug)
            if result is not None:
                posts = result
                has_text = any(p.get("text") for p in posts) if posts else False
                all_posts = posts
                cursor = 0


def show_bookmarks(posts: list[dict] | None = None):
    # Build URL->post lookup so bookmarks can show full text
    post_by_url = {}
    if posts:
        for p in posts:
            url = p.get("url")
            if url:
                post_by_url[url] = p

    bookmarks = load_bookmarks()
    if not bookmarks:
        clear_screen()
        print(c("\n  No bookmarks yet. Press any key...", "dim"))
        read_key()
        return

    state = load_state()
    tag_filter = None  # None | tag string
    _, term_h = term_size()
    page_size = max(5, term_h - 4)
    cursor = 0

    while True:
        if tag_filter is None:
            shown = bookmarks
        else:
            shown = [b for b in bookmarks
                     if tag_filter in get_tags(state, b.get("url", ""))]
        total = len(shown)
        if cursor >= total:
            cursor = max(0, total - 1)

        clear_screen()
        title = "★ Bookmarks"
        if tag_filter is not None:
            title += f" — tag: {tag_filter}  ({total})"
        else:
            title += f" — {len(bookmarks)} saved"
        print_header(title)

        page = cursor // page_size if total else 0
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = page * page_size
        end = min(start + page_size, total)

        for i in range(start, end):
            print_post_line(i + 1, shown[i], bookmarked=True, selected=(i == cursor),
                            read=is_read(state, shown[i].get("url", "")))

        print()
        print(
            c(f" {cursor + 1 if total else 0}/{total} ", "accent") +
            c(" ↑↓ navigate  Enter view  [t] tag filter  [c] clear  [r] remove  [backspace] back", "dim")
        )

        key = read_key()
        if key in ("backspace", "q", "esc", "quit"):
            break
        elif key == "down":
            if cursor < total - 1:
                cursor += 1
        elif key == "up":
            if cursor > 0:
                cursor -= 1
        elif key == "t":
            chosen = pick_tag(state)
            if chosen is not None:
                tag_filter = chosen
                cursor = 0
        elif key == "c":
            tag_filter = None
            cursor = 0
        elif key == "enter" and total > 0:
            bm = shown[cursor]
            full_post = post_by_url.get(bm.get("url"), bm)
            set_read(state, bm.get("url", ""), True)  # opening marks read
            save_state(state)
            show_post_detail(full_post)
            state = load_state()  # pick up any in-detail read toggle
        elif key == "r" and total > 0:
            remove_bookmark(shown[cursor].get("url", ""))
            bookmarks = load_bookmarks()
            if not bookmarks:
                clear_screen()
                print(c("\n  All bookmarks removed. Press any key...", "dim"))
                read_key()
                break
            cursor = min(cursor, max(0, len(bookmarks) - 1))


# ---------------------------------------------------------------------------
# Read / unread tracking
# ---------------------------------------------------------------------------

SEEN_FILE = "stella_seen.json"


def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


def mark_site_visited(slug: str):
    seen = load_seen()
    seen[slug] = datetime.now().isoformat()
    save_seen(seen)


def count_unread(slug: str) -> int:
    """Count posts newer than last visit to this site."""
    seen = load_seen()
    last_open_str = seen.get(slug)
    if not last_open_str:
        return 0
    try:
        last_open = datetime.fromisoformat(last_open_str)
    except ValueError:
        return 0
    total = 0
    for _, path in find_shards(slug):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    dt = parse_post_datetime(row.get("date", ""))
                    if dt and dt > last_open:
                        total += 1
        except Exception:
            pass
    return total


# ---------------------------------------------------------------------------
# Per-article state (read + tags) and app metadata
#
# stella_state.json keyed by article URL:
#   { "__meta__": {"last_seen_version": "1.1.0", "resume": {...}},
#     "https://...": {"read": true, "tags": ["politics"]} }
# Keys starting with "__" are metadata, never treated as articles.
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(SCRIPT_DIR, "stella_state.json")
_META_KEY = "__meta__"


def load_state(path: str = STATE_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_state(state: dict, path: str = STATE_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _row(state: dict, url: str) -> dict:
    """Get or create the per-article row for url."""
    row = state.get(url)
    if not isinstance(row, dict):
        row = {}
        state[url] = row
    return row


def _prune(state: dict, url: str):
    """Drop a row that carries no read flag and no tags, to keep the file small."""
    row = state.get(url)
    if isinstance(row, dict) and not row.get("read") and not row.get("tags"):
        state.pop(url, None)


def is_read(state: dict, url: str) -> bool:
    row = state.get(url)
    return bool(isinstance(row, dict) and row.get("read"))


def set_read(state: dict, url: str, value: bool):
    if not url:
        return
    _row(state, url)["read"] = bool(value)
    _prune(state, url)


def normalize_tag(tag: str) -> str:
    return tag.strip().lower()


def get_tags(state: dict, url: str) -> list:
    row = state.get(url)
    if isinstance(row, dict) and isinstance(row.get("tags"), list):
        return list(row["tags"])
    return []


def set_tags(state: dict, url: str, tags: list):
    if not url:
        return
    seen = []
    for t in tags:
        if not isinstance(t, str):
            continue
        n = normalize_tag(t)
        if n and n not in seen:
            seen.append(n)
    row = _row(state, url)
    if seen:
        row["tags"] = seen
    else:
        row.pop("tags", None)
    _prune(state, url)


def all_tags(state: dict) -> list:
    tags = set()
    for url, row in state.items():
        if url.startswith("__") or not isinstance(row, dict):
            continue
        for t in row.get("tags", []) or []:
            tags.add(t)
    return sorted(tags)


def _meta(state: dict) -> dict:
    m = state.get(_META_KEY)
    if not isinstance(m, dict):
        m = {}
        state[_META_KEY] = m
    return m


def get_last_seen_version(state: dict):
    return _meta(state).get("last_seen_version")


def set_last_seen_version(state: dict, version: str):
    _meta(state)["last_seen_version"] = version


def get_resume(state: dict):
    return _meta(state).get("resume")


def set_resume(state: dict, resume: dict):
    _meta(state)["resume"] = resume


def clear_resume(state: dict):
    _meta(state).pop("resume", None)


def should_show_whatsnew(last_seen, current: str, changelog: dict) -> bool:
    """Show the popup only on a real upgrade to a version we have notes for."""
    if not last_seen:           # first-ever run: set baseline silently
        return False
    if last_seen == current:
        return False
    return current in changelog


def _serialize_filter(filter_kind, filter_value):
    if filter_kind == "month":
        return [filter_value[0], filter_value[1]]
    if filter_kind == "day":
        return filter_value.isoformat()
    if filter_kind == "tag":
        return filter_value
    return None


def _deserialize_filter(filter_kind, value):
    if filter_kind == "month" and value:
        return "month", (value[0], value[1])
    if filter_kind == "day" and value:
        return "day", datetime.fromisoformat(value)
    if filter_kind == "tag" and value:
        return "tag", value
    return None, None


# ---------------------------------------------------------------------------
# Clipboard copy
# ---------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    try:
        if IS_WINDOWS:
            subprocess.run(["clip"], input=text.encode("utf-8"), check=True)
        elif shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        elif shutil.which("xclip"):
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), check=True)
        elif shutil.which("xsel"):
            subprocess.run(["xsel", "--clipboard", "--input"],
                           input=text.encode("utf-8"), check=True)
        else:
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Help screen
# ---------------------------------------------------------------------------

def show_help():
    clear_screen()
    print_header("Keyboard shortcuts")
    print()
    sections = [
        ("Navigation", [
            ("↑ / ↓", "Move cursor one article"),
            ("→ / ←  or  n / p", "Next page / previous page"),
            ("Enter", "Open article"),
            ("Esc / backspace", "Go back to site selector"),
            ("q", "Quit"),
        ]),
        ("Search & filter", [
            ("s", "Search by title"),
            ("S", "Search in article text"),
            ("f", "Compound filter (title + text + dates + sites)"),
            ("m", "Filter by month"),
            ("d", "Filter by day"),
            ("c", "Clear filter"),
        ]),
        ("Actions", [
            ("y", "Copy URL to clipboard"),
            ("r", "Mark article read / unread"),
            ("g", "Edit tags on current article"),
            ("G", "Filter list by tag"),
            ("b", "Bookmark / unbookmark current article"),
            ("B", "View all bookmarks (with tag filter)"),
            ("t", "Toggle timeline chart"),
            ("w", "Word cloud"),
        ]),
        ("Updates", [
            ("u", "Quick update (fetch latest)"),
            ("U", "Scrape custom date range"),
            ("A", "Apply a downloaded update now (when available)"),
        ]),
        ("Site selector", [
            ("s", "Search across all sites"),
            ("f", "Compound filter across selected sites"),
            ("g", "Get update from GitHub"),
            ("!", "STELLAAAAAA!"),
            ("Enter", "Open site"),
        ]),
    ]
    for section, keys in sections:
        print(c(f"  {section}", "title", "bold"))
        for key, desc in keys:
            print(f"    {c(key.ljust(12), 'accent')}  {desc}")
        print()
    print(c("  Press any key to close...", "dim"))
    read_key()


# ---------------------------------------------------------------------------
# Cross-site search
# ---------------------------------------------------------------------------

def global_search():
    """Search titles across all site shards."""
    print()
    term = input_line(c("  Search all sites: ", "accent"))
    if not term:
        return

    clear_screen()
    print_header(f"Global search — '{term}'")
    print()

    results = []
    for site in SITES:
        slug = site_slug(site)
        posts = load_shards(slug)
        for p in posts:
            if term.lower() in p.get("title", "").lower():
                p_copy = dict(p)
                p_copy["_site"] = site["name"]
                results.append(p_copy)

    if not results:
        print(c(f"  No matches for '{term}' across any site.", "warn"))
        print()
        print(c("  Press any key...", "dim"))
        read_key()
        return

    # Annotate titles with site name for display
    for p in results:
        p["title"] = f"[{p['_site']}]  {p['title']}"

    paginate_posts(results, highlight=term, all_posts=results)


# ---------------------------------------------------------------------------
# Site selector
# ---------------------------------------------------------------------------

def site_selector() -> int | None:
    """Show list of sites. Returns SITES index or None to quit."""
    dance_for_stella()
    # Pre-scan CSV info for each site
    site_info = []
    for i, site in enumerate(SITES):
        slug = site_slug(site)
        migrate_to_shards(slug)
        count = count_shards(slug)
        unread = count_unread(slug)
        site_info.append((i, site, slug, count, unread))

    # Sort by post count descending
    site_info.sort(key=lambda x: x[3], reverse=True)

    cursor = 0
    n = len(site_info)

    while True:
        clear_screen()
        print_header("S T E L L A  —  News Reader")
        print()

        for idx, (orig_i, site, slug, count, unread) in enumerate(site_info):
            name = site["name"]
            if count == 0:
                count_str = c("no data", "dim")
            elif unread > 0:
                count_str = f"{count} posts  {c(f'+{unread} new', 'accent')}"
            else:
                count_str = f"{count} posts"
            sel = idx == cursor
            if sel:
                print(f"  {c('▸', 'accent')} {c(f'{idx+1}', 'accent')}  {c(name, 'title', 'bold')}  {c(count_str, 'date')}")
            else:
                print(f"    {c(f'{idx+1}', 'dim')}  {name}  {c(count_str, 'dim')}")

        print()
        print(f"  {c('s', 'dim')}  Search all sites   {c('f', 'dim')}  Filter   {c('g', 'dim')}  Get update   {c('?', 'dim')}  Help   {c('q', 'dim')}  Quit")
        print()
        light = detect_light_theme()
        theme_name = "light" if light else "dark"
        print(c(f"  v{__version__}  |  Theme: {theme_name}  |  Terminal: {term_size()[0]}x{term_size()[1]}", "dim"))
        banner = update_banner_line()
        if banner:
            print(c(banner, "accent", "bold"))
        print()

        key = read_key()
        if key in ("q", "quit"):
            return None
        elif key == "up":
            cursor = (cursor - 1) % n
        elif key == "down":
            cursor = (cursor + 1) % n
        elif key == "enter":
            return site_info[cursor][0]
        elif key == "s":
            global_search()
        elif key == "f":
            spec = filter_form(default_site_slug=None)
            if spec is not None:
                results = run_filter(spec, current_slug=None, current_posts=None)
                if results:
                    hl = spec.title_words + spec.text_words
                    paginate_posts(results, highlight=hl or None, all_posts=results)
                else:
                    clear_screen()
                    print(c(f"\n  No results for filter: {spec.summary()}", "warn"))
                    print(c("  Press any key...", "dim"))
                    read_key()
        elif key == "?":
            show_help()
        elif key == "g":
            trigger_manual_check_in_background()
            for _ in range(20):  # ~4s — usually enough for the check to finish
                time.sleep(0.2)
                with _update_lock:
                    if not _update_status["checking"]:
                        break
        elif key == "!":
            dance_for_stella()
        elif key == "A":
            with _update_lock:
                pend = _update_status["downloaded"]
                pend_v = _update_status["remote_version"]
            if pend and confirm_modal(f"Update to Stella v{pend_v} now?"):
                state = load_state()
                apply_update_and_relaunch(state, {})  # no list position to resume


# ---------------------------------------------------------------------------
# Date picker
# ---------------------------------------------------------------------------

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def date_picker(label: str, default: datetime) -> datetime | None:
    """Arrow-key date picker with 3 scrollable columns (year/month/day).
    Returns datetime or None if cancelled."""
    col = 0  # 0=year, 1=month, 2=day
    year = default.year
    month = default.month
    day = default.day
    year_min, year_max = 2020, datetime.now().year + 1

    def clamp_day():
        nonlocal day
        max_d = calendar.monthrange(year, month)[1]
        if day > max_d:
            day = max_d

    while True:
        clamp_day()
        clear_screen()
        width = min(term_size()[0], 80)
        print()
        print(c(f"  {label}", "title", "bold"))
        print()

        # Build 3 visible rows per column (prev, current, next)
        col_data = []

        # Year column
        years = [year - 1, year, year + 1]
        yr_strs = []
        for i, y in enumerate(years):
            if y < year_min or y > year_max:
                yr_strs.append("    ")
            elif i == 1:
                yr_strs.append(f"{y}")
            else:
                yr_strs.append(f"{y}")
        col_data.append(("YEAR", yr_strs, 6))

        # Month column
        months = [month - 1 if month > 1 else 12,
                  month,
                  month + 1 if month < 12 else 1]
        mon_strs = [MONTH_NAMES[m - 1] for m in months]
        col_data.append(("MONTH", mon_strs, 7))

        # Day column
        max_d = calendar.monthrange(year, month)[1]
        days = [day - 1 if day > 1 else max_d,
                day,
                day + 1 if day < max_d else 1]
        day_strs = [f"{d:2d}" for d in days]
        col_data.append(("DAY", day_strs, 5))

        # Render the picker
        pad = "       "

        # Top border
        top_line = pad
        for ci, (_, _, w) in enumerate(col_data):
            top_line += "  ┌" + "─" * w + "┐"
        print(c(top_line, "bar"))

        # 3 value rows
        for row in range(3):
            line = pad
            for ci, (_, vals, w) in enumerate(col_data):
                txt = vals[row].center(w)
                if row == 1:  # selected row
                    if ci == col:
                        cell = c(f"  │{txt}│", "accent", "bold")
                    else:
                        cell = c(f"  │", "bar") + c(txt, "title", "bold") + c("│", "bar")
                else:
                    cell = c(f"  │", "bar") + c(txt, "dim") + c("│", "bar")
                line += cell
            print(line)

        # Bottom border
        bot_line = pad
        for ci, (_, _, w) in enumerate(col_data):
            bot_line += "  └" + "─" * w + "┘"
        print(c(bot_line, "bar"))

        # Labels
        lbl_line = pad
        for _, (name, _, w) in enumerate(col_data):
            lbl_line += "  " + c(name.center(w + 1), "dim")
        print(lbl_line)

        print()
        print(c("  ←→ column   ↑↓ scroll   Enter confirm   Esc cancel", "dim"))

        key = read_key()
        if key in ("esc", "backspace", "quit"):
            return None
        elif key == "enter":
            return datetime(year, month, day)
        elif key == "left":
            col = (col - 1) % 3
        elif key == "right":
            col = (col + 1) % 3
        elif key == "up":
            if col == 0:
                year = max(year_min, year - 1)
            elif col == 1:
                month = month - 1 if month > 1 else 12
            else:
                day = day - 1 if day > 1 else calendar.monthrange(year, month)[1]
        elif key == "down":
            if col == 0:
                year = min(year_max, year + 1)
            elif col == 1:
                month = month + 1 if month < 12 else 1
            else:
                max_d = calendar.monthrange(year, month)[1]
                day = day + 1 if day <= max_d - 1 else 1


def month_picker(label: str, default: datetime) -> tuple[int, int] | None:
    """Year/Month wheel. Returns (year, month) or None if cancelled."""
    col = 0  # 0=year, 1=month
    year = default.year
    month = default.month
    year_min, year_max = 2020, datetime.now().year + 1

    while True:
        clear_screen()
        print()
        print(c(f"  {label}", "title", "bold"))
        print()

        years = [year - 1, year, year + 1]
        yr_strs = [f"{y}" if year_min <= y <= year_max else "    " for y in years]

        months = [month - 1 if month > 1 else 12,
                  month,
                  month + 1 if month < 12 else 1]
        mon_strs = [MONTH_NAMES[m - 1] for m in months]

        col_data = [("YEAR", yr_strs, 6), ("MONTH", mon_strs, 7)]
        pad = "       "

        top_line = pad
        for _, _, w in col_data:
            top_line += "  ┌" + "─" * w + "┐"
        print(c(top_line, "bar"))

        for row in range(3):
            line = pad
            for ci, (_, vals, w) in enumerate(col_data):
                txt = vals[row].center(w)
                if row == 1:
                    if ci == col:
                        cell = c(f"  │{txt}│", "accent", "bold")
                    else:
                        cell = c(f"  │", "bar") + c(txt, "title", "bold") + c("│", "bar")
                else:
                    cell = c(f"  │", "bar") + c(txt, "dim") + c("│", "bar")
                line += cell
            print(line)

        bot_line = pad
        for _, _, w in col_data:
            bot_line += "  └" + "─" * w + "┘"
        print(c(bot_line, "bar"))

        lbl_line = pad
        for name, _, w in col_data:
            lbl_line += "  " + c(name.center(w + 1), "dim")
        print(lbl_line)

        print()
        print(c("  ←→ column   ↑↓ scroll   Enter confirm   Esc cancel", "dim"))

        key = read_key()
        if key in ("esc", "backspace", "quit"):
            return None
        elif key == "enter":
            return (year, month)
        elif key == "left":
            col = (col - 1) % 2
        elif key == "right":
            col = (col + 1) % 2
        elif key == "up":
            if col == 0:
                year = max(year_min, year - 1)
            else:
                month = month - 1 if month > 1 else 12
        elif key == "down":
            if col == 0:
                year = min(year_max, year + 1)
            else:
                month = month + 1 if month < 12 else 1


# ---------------------------------------------------------------------------
# Scrape actions (u / U)
# ---------------------------------------------------------------------------

def quick_update(site: dict, posts: list[dict], slug: str):
    """Scrape posts newer than latest in shards, save to shards. Returns updated posts."""
    clear_screen()
    print_header(f"Quick update — {site['name']}")
    print()

    date_fmt = site["date_format"]
    latest = None
    for p in posts:
        try:
            d = datetime.strptime(p.get("date", ""), date_fmt)
            if latest is None or d > latest:
                latest = d
        except ValueError:
            continue

    if latest is None:
        latest = datetime.now() - timedelta(days=7)
        print(c(f"  No dates found. Fetching last 7 days.", "warn"))
    else:
        print(c(f"  Latest post: {latest.strftime('%Y-%m-%d %H:%M')}", "info"))

    print(c("  [Esc] or Ctrl-C to abort\n", "dim"))

    new_posts: list[dict] = []
    aborted = False
    try:
        with AbortPoller() as poller:
            new_posts = scrape_site(site, latest, should_abort=poller.check)
            aborted = poller.aborted
    except KeyboardInterrupt:
        aborted = True
    except Exception as e:
        print(c(f"\n  Error: {e}", "warn"))
        print(c("\n  Press any key to continue...", "dim"))
        read_key()
        return posts

    try:
        if new_posts and site.get("article_content_selector") and any(p.get("text") for p in posts):
            enrich_with_text(new_posts, site)

        new_rows = [scraped_to_csv_row(p) for p in new_posts]
        added = merge_into_shards(new_rows, slug)

        if added:
            posts = load_shards(slug, years=recent_years())
            tag = " (partial — aborted)" if aborted else ""
            print(c(f"\n  ✓ Added {added} new posts{tag}. Loaded: {len(posts)}", "accent"))
        elif aborted:
            print(c("\n  Aborted — no new posts saved.", "warn"))
        else:
            print(c("\n  Already up to date — no new posts.", "dim"))
    except Exception as e:
        print(c(f"\n  Error during merge: {e}", "warn"))

    print(c("\n  Press any key to continue...", "dim"))
    read_key()
    return posts


def window_scrape(site: dict, existing_posts: list[dict], slug: str):
    """Scrape a user-specified date window, merge into shards. Returns updated posts or None."""
    now = datetime.now()
    since = date_picker(f"Scrape {site['name']} — select START date:", now.replace(day=1))
    if since is None:
        return None

    until = date_picker(f"Scrape {site['name']} — select END date:", now)
    if until is None:
        return None

    until = until.replace(hour=23, minute=59, second=59)

    clear_screen()
    print_header(f"Scraping — {site['name']}")
    print(c(f"\n  From: {since.strftime('%Y-%m-%d')}  To: {until.strftime('%Y-%m-%d')}", "info"))
    print(c("  [Esc] or Ctrl-C to abort\n", "dim"))

    all_scraped: list[dict] = []
    aborted = False
    try:
        with AbortPoller() as poller:
            # Skip-ahead probe so we don't walk thousands of pages from the head.
            start_page = 0
            if until < datetime.now() - timedelta(days=30):
                try:
                    start_page = find_start_page(site, until, should_abort=poller.check)
                    if start_page > 0:
                        print(c(f"  Skip-ahead: starting at page {start_page}", "info"))
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(c(f"  (skip-ahead probe failed: {e}; falling back to page 0)", "dim"))
                    start_page = 0
            if not poller.aborted:
                all_scraped = scrape_site(site, since, start_page=start_page,
                                          should_abort=poller.check)
            aborted = poller.aborted
    except KeyboardInterrupt:
        aborted = True
    except Exception as e:
        print(c(f"\n  Error: {e}", "warn"))
        print(c("\n  Press any key to continue...", "dim"))
        read_key()
        return None

    try:
        date_fmt = site["date_format"]
        filtered = []
        for p in all_scraped:
            try:
                d = p.get("date") if isinstance(p.get("date"), datetime) else datetime.strptime(p.get("date_str", ""), date_fmt)
                if d <= until:
                    filtered.append(p)
            except (ValueError, TypeError):
                filtered.append(p)

        if filtered and site.get("article_content_selector"):
            enrich_with_text(filtered, site)

        new_rows = [scraped_to_csv_row(p) for p in filtered]
        added = merge_into_shards(new_rows, slug)

        if added:
            posts = load_shards(slug, years=recent_years())
            tag = " (partial — aborted)" if aborted else ""
            print(c(f"\n  ✓ Added {added} new posts{tag}. Loaded: {len(posts)}", "accent"))
        else:
            posts = existing_posts
            if aborted:
                print(c("\n  Aborted — no new posts saved.", "warn"))
            else:
                print(c("\n  No new posts found in that range.", "dim"))
    except Exception as e:
        print(c(f"\n  Error during merge: {e}", "warn"))
        posts = existing_posts

    print(c("\n  Press any key to continue...", "dim"))
    read_key()
    return posts


# ---------------------------------------------------------------------------
# Site menu (per-site view) — thin wrapper: goes straight to browse list
# ---------------------------------------------------------------------------

def site_menu(site: dict | None, posts: list[dict], slug: str | None, all_loaded: bool = True):
    if slug and site:
        mark_site_visited(slug)
    db_total_val = count_shards(slug) if (slug and not all_loaded) else None
    paginate_posts(posts, all_posts=posts, db_total=db_total_val,
                   site=site, slug=slug, all_loaded=all_loaded)


# ---------------------------------------------------------------------------
# Easter egg: A Streetcar Named Desire-style dancer who yells for Stella.
# Triggered by `!` in the site selector, plus a small random chance when
# the selector is entered. Pure decoration — never blocks anything.
# ---------------------------------------------------------------------------

STELLA_QUOTES = [
    "STELLAAAAAAA!",
    "STELL-AAAH! Anything new from RRN today?",
    "Hey Stella, throw me down those headlines!",
    "Stella, my dove — what's brewing in Brennende Frage?",
    "Without you, Stella, I'm just a man in a terminal.",
    "I yelled your name from the gutter, Stella!",
    "Stella darling, did the Russians do something today?",
    "Press 'b' to bookmark me too, Stella!",
    "Stella, you keep me sane in this loud world.",
    "Stella baby, give me five fresh stories and I'll buy you flowers.",
    "Stella! Stop hiding behind that filter — show me ALL the posts!",
    "STELLAAAA! …Stella? Anyone?",
    "Stella, I bookmarked my heart for you.",
    "Hey kid, you ever see a man dance for a news reader before?",
    "Stella, you're the only TUI I'll ever love.",
    "Östlicher Wind blew my hat off — get me the article, Stella!",
    "Stell-aaa! Did Wahlomacht wake up yet?",
    "STELLA, I'M STANDING IN THE RAIN FOR YOU!",
    "Stella, scrape me the moon and stars!",
    "I refresh my heart at intervals of 10 minutes, Stella.",
]

DANCE_FRAMES = [
    [r"     \o/  ",
     r"      |   ",
     r"     / \  "],
    [r"     _o_  ",
     r"      |   ",
     r"     / \  "],
    [r"      o/  ",
     r"     /|   ",
     r"     / \  "],
    [r"     \o   ",
     r"      |\  ",
     r"     / \  "],
]


def _truncate_to_width(s: str, width: int) -> str:
    return s if len(s) <= width else s[: max(0, width - 1)] + "…"


def dance_for_stella():
    """Centered modal popup with an animated ASCII dancer + speech bubble."""
    cols, rows = term_size()
    quote = random.choice(STELLA_QUOTES)

    inner_w = max(40, min(72, len(quote) + 8))
    inner_h = 9
    box_w = inner_w + 2
    box_h = inner_h + 2

    if cols < box_w + 2 or rows < box_h + 2:
        # Terminal too small for a centered modal — fall back to top-anchored
        clear_screen()
        print()
        print(c(quote, "title", "bold"))
        print()
        for line in DANCE_FRAMES[0]:
            print(c(line, "accent", "bold"))
        print()
        print(c("(press any key)", "dim"))
        read_key()
        return

    quote = _truncate_to_width(quote, inner_w - 4)

    top = max(1, (rows - box_h) // 2)
    left = max(1, (cols - box_w) // 2)

    def at(r: int, col: int):
        sys.stdout.write(f"\033[{r};{col}H")

    def write(s: str):
        sys.stdout.write(s)

    write("\033[?25l")  # hide cursor
    clear_screen()

    # Border (rounded)
    border_top = "╭" + "─" * inner_w + "╮"
    border_bot = "╰" + "─" * inner_w + "╯"
    blank_row = "│" + " " * inner_w + "│"
    at(top, left);          write(c(border_top, "accent"))
    for i in range(1, box_h - 1):
        at(top + i, left);  write(c(blank_row, "accent"))
    at(top + box_h - 1, left); write(c(border_bot, "accent"))

    # Speech line (row 2 inside the box)
    quote_left = left + 1 + (inner_w - len(quote)) // 2
    at(top + 2, quote_left)
    write(c(quote, "title", "bold"))

    # Dancer area: 3 lines, centered, starts at inner row 4
    fig_w = max(len(line.rstrip()) for frame in DANCE_FRAMES for line in frame)
    fig_top = top + 4
    fig_left = left + 1 + (inner_w - fig_w) // 2

    notes_seq = ["~♪~", " ♫ ", "~♬ ", " ♩ "]
    for i in range(12):
        frame = DANCE_FRAMES[i % len(DANCE_FRAMES)]
        for ln_idx, line in enumerate(frame):
            at(fig_top + ln_idx, fig_left)
            write(c(line.ljust(fig_w), "accent", "bold"))
        # Notes row, animated
        notes = notes_seq[i % len(notes_seq)]
        at(fig_top + 3, fig_left + 1)
        write(c(notes, "title", "bold"))
        sys.stdout.flush()
        time.sleep(0.16)

    # Hint
    hint = "(press any key)"
    at(top + box_h - 2, left + 1 + (inner_w - len(hint)) // 2)
    write(c(hint, "dim"))

    # Park cursor below the box and flush
    at(min(rows, top + box_h + 1), 1)
    sys.stdout.flush()

    read_key()
    write("\033[?25h")  # show cursor
    sys.stdout.flush()


def show_whatsnew(version: str):
    lines = CHANGELOG.get(version, [])
    cols, rows = term_size()
    title = f"What's new in Stella v{version}"
    body = [title, ""] + [f"• {ln}" for ln in lines] + ["", "(press any key)"]
    inner_w = max(40, min(76, max(len(s) for s in body) + 4))
    box_h = len(body) + 2
    if cols < inner_w + 4 or rows < box_h + 2:
        clear_screen()
        print(c(title, "title", "bold"))
        print()
        for ln in lines:
            print(c(f"  • {ln}", "text"))
        print()
        print(c("  (press any key)", "dim"))
        read_key()
        return
    top = max(1, (rows - box_h) // 2)
    left = max(1, (cols - (inner_w + 2)) // 2)

    def at(r, col):
        sys.stdout.write(f"\033[{r};{col}H")

    sys.stdout.write("\033[?25l")
    sys.stdout.write(c("\033[%d;%dH╭%s╮" % (top, left, "─" * inner_w), "accent"))
    for i in range(1, box_h - 1):
        at(top + i, left)
        sys.stdout.write(c("│" + " " * inner_w + "│", "accent"))
    at(top + box_h - 1, left)
    sys.stdout.write(c("╰" + "─" * inner_w + "╯", "accent"))
    for i, s in enumerate(body):
        at(top + 1 + i, left + 2)
        if i == 0:
            sys.stdout.write(c(s, "title", "bold"))
        elif s == "(press any key)":
            sys.stdout.write(c(s, "dim"))
        else:
            sys.stdout.write(c(s, "text"))
    at(min(rows, top + box_h + 1), 1)
    sys.stdout.flush()
    read_key()
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def confirm_modal(message: str) -> bool:
    """Centered yes/no. Enter = yes, Esc/backspace = no."""
    clear_screen()
    cols, rows = term_size()
    lines = [message, "", "[Enter] yes    [Esc] later"]
    inner_w = max(30, min(70, max(len(s) for s in lines) + 4))
    top = max(1, rows // 2 - 2)
    left = max(1, (cols - (inner_w + 2)) // 2)
    print(f"\033[{top};{left}H" + c("╭" + "─" * inner_w + "╮", "accent"))
    for i, s in enumerate(lines):
        print(f"\033[{top + 1 + i};{left}H" +
              c("│", "accent") + s.center(inner_w) + c("│", "accent"))
    print(f"\033[{top + 1 + len(lines)};{left}H" + c("╰" + "─" * inner_w + "╯", "accent"))
    sys.stdout.flush()
    while True:
        k = read_key()
        if k == "enter":
            return True
        if k in ("esc", "backspace", "q", "quit"):
            return False


def apply_update_and_relaunch(state: dict, resume: dict):
    """Persist resume, swap in the pending update, and re-exec in place."""
    set_resume(state, resume)
    save_state(state)
    apply_pending_update_if_any()
    clear_screen()
    print(c("\n  Updating Stella… restarting.\n", "accent", "bold"))
    sys.stdout.flush()
    python = sys.executable
    os.execv(python, [python, os.path.abspath(__file__)] + sys.argv[1:])


# ---------------------------------------------------------------------------
# Self-update from GitHub
#
# Flow:
#   1. apply_pending_update_if_any() runs first thing in main(); replaces any
#      *.stella_pending leftover from a prior session that couldn't swap.
#   2. start_update_checker() spawns a daemon thread that polls GitHub raw
#      files every UPDATE_CHECK_INTERVAL_SEC. On a newer __version__, it
#      downloads each file in UPDATE_FILES to *.stella_pending and tries an
#      atomic os.replace() into place.
#   3. update_banner_line() returns a notification line shown in site_selector
#      whenever an update was downloaded this session.
# Replacing stella.py while running is safe — Python reads the source at
# import time and closes the handle, so on-disk swaps don't disturb the
# running process. The new code only loads on the next launch.
# ---------------------------------------------------------------------------

_update_status = {
    "downloaded": False,
    "remote_version": None,
    "last_error": None,
    "manual_msg": None,   # transient message from a manual check
    "checking": False,    # guards re-entry of manual check
}
_update_lock = threading.Lock()


def _version_tuple(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def _raw_url(fname: str) -> str:
    base = os.environ.get("STELLA_UPDATE_BASE")
    if base:
        return base.rstrip("/") + "/" + fname
    return (f"https://raw.githubusercontent.com/{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/{UPDATE_BRANCH}/{fname}")


def _fetch_url(url: str, timeout: int = 15) -> bytes:
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": f"stella/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def apply_pending_update_if_any():
    """Move *.stella_pending → real path. Best-effort, silent on failure."""
    for fname in UPDATE_FILES:
        pending = os.path.join(SCRIPT_DIR, fname + ".stella_pending")
        if os.path.exists(pending):
            try:
                os.replace(pending, os.path.join(SCRIPT_DIR, fname))
            except OSError:
                pass


def _download_pending(remote_version: str) -> bool:
    """Download all UPDATE_FILES to *.stella_pending. Validates the staged
    stella.py parses to remote_version before declaring success — so a
    captive portal or partial response can't corrupt the live file."""
    import urllib.error
    written = []
    try:
        for fname in UPDATE_FILES:
            try:
                data = _fetch_url(_raw_url(fname))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
            if not data or len(data) < 32:
                raise ValueError(f"empty/tiny payload for {fname}")
            tmp = os.path.join(SCRIPT_DIR, fname + ".stella_pending")
            with open(tmp, "wb") as f:
                f.write(data)
            written.append(tmp)

        head_path = os.path.join(SCRIPT_DIR, "stella.py.stella_pending")
        if not os.path.exists(head_path):
            raise ValueError("stella.py missing from download set")
        with open(head_path, "rb") as f:
            head = f.read().decode("utf-8", errors="replace")
        m = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']', head, re.M)
        if not m or m.group(1) != remote_version:
            raise ValueError("downloaded stella.py version mismatch")

        return True
    except Exception:
        for p in written:
            try:
                os.remove(p)
            except OSError:
                pass
        return False


def _try_apply_pending() -> int:
    """Try os.replace each pending file. Returns count successfully swapped."""
    applied = 0
    for fname in UPDATE_FILES:
        pending = os.path.join(SCRIPT_DIR, fname + ".stella_pending")
        if not os.path.exists(pending):
            continue
        try:
            os.replace(pending, os.path.join(SCRIPT_DIR, fname))
            applied += 1
        except OSError:
            pass
    return applied


def check_for_update_once() -> bool:
    """One pass. Returns True if the check completed (regardless of whether
    an update was found) and False on network/parse failure — so callers can
    decide between normal interval and shorter retry."""
    try:
        head = _fetch_url(_raw_url("stella.py")).decode(
            "utf-8", errors="replace")
        m = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']', head, re.M)
        if not m:
            with _update_lock:
                _update_status["last_error"] = "no __version__ in remote stella.py"
            return False
        remote_v = m.group(1)
        if _version_tuple(remote_v) <= _version_tuple(__version__):
            with _update_lock:
                _update_status["last_error"] = None
            return True
        if not _download_pending(remote_v):
            with _update_lock:
                _update_status["last_error"] = "download failed validation"
            return False
        _try_apply_pending()
        with _update_lock:
            _update_status["downloaded"] = True
            _update_status["remote_version"] = remote_v
            _update_status["last_error"] = None
        return True
    except Exception as e:
        with _update_lock:
            _update_status["last_error"] = str(e)
        return False


def _update_loop():
    while True:
        ok = check_for_update_once()
        time.sleep(UPDATE_CHECK_INTERVAL_SEC if ok else UPDATE_RETRY_INTERVAL_SEC)


def trigger_manual_check_in_background():
    """Fire a one-off check from a background thread. Used by the `U`
    keybinding in the site selector. Safe to call repeatedly — re-entry
    is guarded by the `checking` flag."""
    with _update_lock:
        if _update_status["checking"]:
            return
        _update_status["checking"] = True
        _update_status["manual_msg"] = "  ⟳ Checking GitHub for updates…"

    def _run():
        try:
            ok = check_for_update_once()
            with _update_lock:
                if _update_status["downloaded"]:
                    _update_status["manual_msg"] = None  # banner takes over
                elif ok:
                    _update_status["manual_msg"] = (
                        f"  ✓ You're on the latest version (v{__version__}).")
                else:
                    _update_status["manual_msg"] = (
                        "  ✗ Couldn't reach GitHub — check your connection.")
        finally:
            with _update_lock:
                _update_status["checking"] = False

    threading.Thread(target=_run, daemon=True).start()


def start_update_checker():
    """Skip silently if repo placeholder isn't configured."""
    if os.environ.get("STELLA_FAKE_DOWNLOADED"):
        with _update_lock:
            _update_status["downloaded"] = True
            _update_status["remote_version"] = os.environ.get(
                "STELLA_FAKE_DOWNLOADED")  # e.g. "1.1.1"
        return
    if not GITHUB_OWNER or not GITHUB_REPO:
        return
    t = threading.Thread(
        target=_update_loop, daemon=True, name="stella-updater")
    t.start()


def update_banner_line() -> str | None:
    with _update_lock:
        if _update_status["downloaded"]:
            v = _update_status["remote_version"]
            return f"  ★ Stella v{v} downloaded — restart to use the new version"
        if _update_status["manual_msg"]:
            return _update_status["manual_msg"]
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _restore_from_resume(resume: dict):
    """Best-effort: jump back into the site/list/article the user left."""
    slug = resume.get("slug")
    if not slug:
        return
    site = next((s for s in SITES if site_slug(s) == slug), None)
    if site is None:
        return  # site no longer exists; start at selector
    posts = load_shards(slug)
    paginate_posts(posts, all_posts=posts, site=site, slug=slug, resume=resume)


def main():
    _enable_win_ansi()
    apply_pending_update_if_any()
    start_update_checker()

    state = load_state()
    current = __version__
    if should_show_whatsnew(get_last_seen_version(state), current, CHANGELOG):
        show_whatsnew(current)
    set_last_seen_version(state, current)
    save_state(state)

    resume = get_resume(state)
    if resume:
        clear_resume(state)
        save_state(state)
        _restore_from_resume(resume)

    # Backward compat: direct CSV path as argument
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
        posts = load_csv(csv_path)
        if not posts:
            print(c(f"No posts found in {csv_path}", "warn"))
            sys.exit(1)
        site_menu(None, posts, slug=csv_path, all_loaded=True)
        return

    # Normal mode: site selector
    while True:
        choice = site_selector()
        if choice is None:
            break

        site = SITES[choice]
        slug = site_slug(site)
        posts = load_shards(slug)
        site_menu(site, posts, slug=slug, all_loaded=True)

    clear_screen()
    print(c("\nBye!\n", "dim"))


if __name__ == "__main__":
    main()
