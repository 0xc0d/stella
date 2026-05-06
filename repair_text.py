"""Backfill missing article text across all CSV shards.

Walks every posts_{slug}_{year}.csv, finds rows with empty or "(error..." text,
re-fetches via fetch_article_text in parallel, writes back changed shards.

Usage:
    python repair_text.py              # all sites, empty rows only
    python repair_text.py --errors     # also retry "(error..." rows
    python repair_text.py rrn          # only sites whose name contains "rrn"
"""

import os
import sys
import glob
from concurrent.futures import ThreadPoolExecutor

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from scraper import SITES, fetch_article_text
from stella import site_slug, load_csv, save_merged_csv


def repair_site(site: dict, retry_errors: bool, workers: int = 16):
    selector = site.get("article_content_selector")
    if not selector:
        return 0, 0
    slug = site_slug(site)
    fixed = failed = 0

    for path in sorted(glob.glob(f"posts_{slug}_*.csv")):
        rows = load_csv(path)
        targets = []
        for r in rows:
            url = r.get("url") or ""
            t = r.get("text") or ""
            if not url:
                continue
            if not t:
                targets.append(r)
            elif retry_errors and t.startswith("(error"):
                targets.append(r)
        if not targets:
            continue
        print(f"  {path}: {len(targets)} to retry")

        def _fetch(row):
            try:
                t = fetch_article_text(row["url"], selector)
                if t and not t.startswith("(article content not found"):
                    return row, t, None
                return row, None, "no content"
            except Exception as e:
                return row, None, str(e)[:80]

        path_fixed = path_failed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for row, text, err in pool.map(_fetch, targets):
                if text:
                    row["text"] = text
                    path_fixed += 1
                else:
                    path_failed += 1

        if path_fixed:
            save_merged_csv(rows, path)
        fixed += path_fixed
        failed += path_failed
        print(f"    fixed={path_fixed} failed={path_failed}")
    return fixed, failed


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    retry_errors = "--errors" in args
    args = [a for a in args if not a.startswith("--")]
    only = args[0].lower() if args else None

    summary = {}
    for site in SITES:
        if only and only not in site["name"].lower():
            continue
        print(f"\n=== {site['name']} ===")
        try:
            fixed, failed = repair_site(site, retry_errors=retry_errors)
            summary[site["name"]] = (fixed, failed)
        except Exception as e:
            print(f"  !! crashed: {e}")
            summary[site["name"]] = (-1, -1)

    print("\n=== SUMMARY ===")
    for name, (f, fl) in summary.items():
        marker = "ERR" if f < 0 else f"+{f}/-{fl}"
        print(f"  {marker:>10}  {name}")
