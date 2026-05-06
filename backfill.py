"""Backfill all sites from 2022-01-01 and merge into year-sharded CSVs.

Skips full article text for backfilled posts (would take hours).
Existing posts retain their text column.
"""

import os
import sys
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from scraper import SITES, scrape_site
from stella import (
    site_slug,
    migrate_to_shards,
    merge_into_shards,
    count_shards,
    scraped_to_csv_row,
)

CUTOFF = datetime(2022, 1, 1)


def run_one(site):
    name = site["name"]
    slug = site_slug(site)
    migrate_to_shards(slug)
    existing_count = count_shards(slug)

    print(f"\n{'=' * 70}")
    print(f"SITE: {name}")
    print(f"  Existing posts: {existing_count}")
    print(f"  Cutoff: {CUTOFF.date()}")
    print(f"{'=' * 70}", flush=True)

    try:
        scraped = scrape_site(site, CUTOFF)
    except Exception as e:
        print(f"  ! scrape failed: {e}", flush=True)
        return 0

    new_rows = [scraped_to_csv_row(p) for p in scraped]
    added = merge_into_shards(new_rows, slug)

    if added:
        print(f"  + Added {added} new posts -> total {count_shards(slug)}", flush=True)
    else:
        print(f"  = No new posts (all {len(scraped)} scraped already in shards)", flush=True)
    return added


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    totals = {}
    for site in SITES:
        if only and only.lower() not in site["name"].lower():
            continue
        try:
            totals[site["name"]] = run_one(site)
        except Exception as e:
            print(f"  !! {site['name']} crashed: {e}", flush=True)
            totals[site["name"]] = -1

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, n in totals.items():
        marker = "ERR" if n < 0 else f"+{n}"
        print(f"  {marker:>6}  {name}")
