# Read Tracking, Tagged Bookmarks & Prompted Self-Update — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-article read tracking (dim read titles), free-form tags on any article with tag-filtered browsing, a one-time "what's new" popup after upgrades, and a prompted self-update that relaunches in place and restores the user's position.

**Architecture:** One new JSON store `stella_state.json` keyed by article URL holds `{read, tags}` per article plus a reserved `"__meta__"` row for app metadata (`last_seen_version`, `resume`). Pure dict/file helpers are unit-tested with `unittest`; TUI rendering and key loops are verified manually against a documented test plan. The existing GitHub self-update machinery (background download → `*.stella_pending` → `apply_pending_update_if_any`) is reused; only the "apply" step changes from "wait for next launch" to "prompt → `os.execv` relaunch → resume".

**Tech Stack:** Python 3.10 stdlib only (`json`, `os`, `sys`, `re`, `unittest`). No third-party deps. Terminal UI in `stella.py`. No pytest available — tests use `unittest`, run with `python3 -m unittest`.

**Spec:** `docs/superpowers/specs/2026-06-09-read-tracking-and-tagged-bookmarks-design.md`

---

## File Structure

- **Modify `stella.py`** — all feature code lives here (single-file app, established pattern):
  - New "Per-article state (read + tags)" section after `count_unread` (~line 1756): the `stella_state.json` store + all pure helpers.
  - New `CHANGELOG` constant + `show_whatsnew` + `should_show_whatsnew` near the version block.
  - Edits to `print_post_line`, `show_post_detail`, `paginate_posts`, `show_bookmarks`, `site_selector`, `show_help`, `main`, `_raw_url`, `start_update_checker`.
  - New `tag_picker`, `pick_tag`, `confirm_modal`, `apply_update_and_relaunch`, `_restore_from_resume`.
- **Create `tests/test_state.py`** — unit tests for the state-store helpers.
- **Create `tests/test_update_logic.py`** — unit tests for version-compare / what's-new gating / `_raw_url` env override / resume (de)serialization.

> **Testing note:** `stella.py` imports cleanly (no side effects at import — `main()` is guarded by `if __name__ == "__main__"`), so tests can `import stella`. Tests must not touch real data files: every test passes an explicit temp `path` to `load_state`/`save_state` and operates on in-memory dicts otherwise.

---

## Task 1: State store — read + tags + meta (pure helpers, TDD)

**Files:**
- Create: `tests/test_state.py`
- Modify: `stella.py` (new section after line 1756, end of `count_unread`)

- [ ] **Step 1: Write failing tests**

Create `tests/test_state.py`:

```python
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stella


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)  # start with no file

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_missing_file_loads_empty(self):
        self.assertEqual(stella.load_state(self.path), {})

    def test_corrupt_file_loads_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(stella.load_state(self.path), {})

    def test_read_roundtrip(self):
        state = {}
        self.assertFalse(stella.is_read(state, "u1"))
        stella.set_read(state, "u1", True)
        self.assertTrue(stella.is_read(state, "u1"))
        stella.save_state(state, self.path)
        self.assertTrue(stella.is_read(stella.load_state(self.path), "u1"))

    def test_unread_with_no_tags_prunes_row(self):
        state = {}
        stella.set_read(state, "u1", True)
        stella.set_read(state, "u1", False)
        self.assertNotIn("u1", state)  # pruned when unread and untagged

    def test_tags_normalized_and_deduped(self):
        state = {}
        stella.set_tags(state, "u1", ["  Politics ", "politics", "EU", ""])
        self.assertEqual(stella.get_tags(state, "u1"), ["politics", "eu"])

    def test_empty_tags_prune_when_unread(self):
        state = {}
        stella.set_tags(state, "u1", ["x"])
        stella.set_tags(state, "u1", [])
        self.assertNotIn("u1", state)

    def test_empty_tags_kept_when_read(self):
        state = {}
        stella.set_read(state, "u1", True)
        stella.set_tags(state, "u1", [])
        self.assertIn("u1", state)
        self.assertEqual(stella.get_tags(state, "u1"), [])

    def test_all_tags_union_sorted_skips_meta(self):
        state = {"__meta__": {"last_seen_version": "1.1.0"}}
        stella.set_tags(state, "u1", ["b", "a"])
        stella.set_tags(state, "u2", ["a", "c"])
        self.assertEqual(stella.all_tags(state), ["a", "b", "c"])

    def test_meta_helpers(self):
        state = {}
        self.assertIsNone(stella.get_last_seen_version(state))
        stella.set_last_seen_version(state, "1.1.0")
        self.assertEqual(stella.get_last_seen_version(state), "1.1.0")
        self.assertNotIn("__meta__", stella.all_tags(state))

    def test_resume_roundtrip(self):
        state = {}
        self.assertIsNone(stella.get_resume(state))
        stella.set_resume(state, {"slug": "rrn_com_tr", "cursor": 4})
        self.assertEqual(stella.get_resume(state)["cursor"], 4)
        stella.clear_resume(state)
        self.assertIsNone(stella.get_resume(state))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_state -v`
Expected: FAIL/ERROR — `AttributeError: module 'stella' has no attribute 'load_state'`.

- [ ] **Step 3: Implement the state store**

In `stella.py`, after `count_unread` ends (line 1756, before the `# Clipboard copy` section banner at 1759), insert:

```python
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
```

> Note: `SCRIPT_DIR` is defined later in the file (line ~2477). Python resolves module-level names at call time, not def time, so referencing `SCRIPT_DIR` in the `STATE_FILE` default is safe **only if** `STATE_FILE` is evaluated after `SCRIPT_DIR` is assigned. Because `STATE_FILE = os.path.join(SCRIPT_DIR, ...)` runs at import time top-to-bottom, and this block is at line ~1756 while `SCRIPT_DIR` is at ~2477, this would raise `NameError` at import. **Fix:** move the `SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))` assignment up to just below the imports (near line 30), and delete the duplicate at line ~2477. Do this as the first edit of Step 3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_state -v`
Expected: PASS (all 11 tests OK).

- [ ] **Step 5: Verify the app still imports and runs**

Run: `python3 -c "import stella; print('ok')"`
Expected: prints `ok` (no NameError from the SCRIPT_DIR move).

- [ ] **Step 6: Commit**

```bash
git add tests/test_state.py stella.py
git commit -m "feat: per-article read/tag state store (stella_state.json)"
```

---

## Task 2: Dim read titles in the browse list

**Files:**
- Modify: `stella.py` — `print_post_line` (lines 668-713)

- [ ] **Step 1: Add a `read` parameter and dim the title**

Change the signature (line 668-669):

```python
def print_post_line(i: int, post: dict, highlight=None,
                    bookmarked: bool = False, selected: bool = False,
                    read: bool = False):
```

In the non-selected, non-highlight branch, choose the title style by read state. Replace the `else` block at lines 684-685:

```python
    else:
        if read:
            title_str = c(title, "dim")
        else:
            title_str = c(title, "title", "bold")
```

And in the highlight branch (lines 679-685), make the non-matched parts dim when read. Replace lines 679-685 with:

```python
        title_str = ""
        base_style = ("dim",) if read else ("title", "bold")
        for j, part in enumerate(parts):
            title_str += c(part, *base_style)
            if j < len(matches):
                title_str += c(matches[j], "highlight", "bold")
```

> The `selected` (reverse-video) branch at lines 687-711 is intentionally left unchanged — a selected row stays fully visible regardless of read state.

- [ ] **Step 2: Manual verification**

This is rendering-only; verified end-to-end in Task 3 once `read=` is wired in. No standalone run here.

- [ ] **Step 3: Commit**

```bash
git add stella.py
git commit -m "feat: dim read article titles in print_post_line"
```

---

## Task 3: Wire read state into the browse list (`Enter` marks read, `r` toggles)

**Files:**
- Modify: `stella.py` — `paginate_posts` (lines 1413-1640)

- [ ] **Step 1: Load state and pass `read=` when rendering**

After line 1424 (`bm_urls = {...}`), add:

```python
    state = load_state()
```

In the render loop, change line 1490-1492:

```python
            for i in range(start, end):
                post = display[i]
                is_bm = post.get("url") in bm_urls
                is_sel = (i == cursor)
                print_post_line(i + 1, post, highlight, bookmarked=is_bm,
                                selected=is_sel, read=is_read(state, post.get("url", "")))
```

- [ ] **Step 2: Mark read on open**

Replace the `enter` handler (lines 1535-1538):

```python
        elif key == "enter" and total > 0:
            post = display[cursor]
            show_post_detail(post)
            set_read(state, post.get("url", ""), True)
            save_state(state)
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
```

- [ ] **Step 3: Add `r` toggle handler**

Insert a new handler immediately after the `enter` handler from Step 2:

```python
        elif key == "r" and total > 0:
            url = display[cursor].get("url", "")
            if url:
                set_read(state, url, not is_read(state, url))
                save_state(state)
```

- [ ] **Step 4: Manual verification**

Run on a scratch copy (do NOT use live data):

```bash
mkdir -p /tmp/stella_test && cp stella.py scraper.py /tmp/stella_test/ \
  && cp posts_brennendefrage_net_2025.csv /tmp/stella_test/ \
  && cd /tmp/stella_test && python3 stella.py
```

Verify:
- Open an article with Enter, back out → its title is now dim.
- Move to a dim row, press `r` → it un-dims; press `r` again → dims.
- `cat /tmp/stella_test/stella_state.json` shows the URL with `"read": true`.

- [ ] **Step 5: Commit**

```bash
cd /Users/ali/Desktop/Private/stella
git add stella.py
git commit -m "feat: mark articles read on open and toggle with r in browse list"
```

---

## Task 4: Wire read state into the detail view (`r` toggles there too)

**Files:**
- Modify: `stella.py` — `show_post_detail` (lines 900-986)

- [ ] **Step 1: Show read status and handle `r` in detail**

The browse list already marks read on open (Task 3). Add an in-detail `r` toggle so she can flip it without leaving. In the key loop (lines 959-986), add a handler before the `backspace` case (before line 985):

```python
        elif key == "r":
            url = post.get("url", "")
            if url:
                _state = load_state()
                set_read(_state, url, not is_read(_state, url))
                save_state(_state)
                print(c("  ✓ marked " + ("read" if is_read(_state, url) else "unread"),
                        "accent"), end="\r", flush=True)
                time.sleep(0.6)
                print(" " * 40, end="\r", flush=True)
```

> The detail view loads/saves its own `state` snapshot because it has no shared reference to the caller's dict. The caller (`paginate_posts`) reloads nothing for read state, but it set read on open and persisted; a detail-view toggle persists immediately too, and the list re-reads `state`... — note: `paginate_posts` holds `state` in memory. To avoid a stale list after an in-detail toggle, reload it. See Step 2.

- [ ] **Step 2: Reload list state after returning from detail**

In `paginate_posts`, in the `enter` handler from Task 3 Step 2, add a `state` reload so an in-detail `r` toggle is reflected when the list redraws. Update that handler to:

```python
        elif key == "enter" and total > 0:
            post = display[cursor]
            show_post_detail(post)
            set_read(state, post.get("url", ""), True)
            save_state(state)
            state = load_state()  # pick up any in-detail read toggle
            bookmarks = load_bookmarks()
            bm_urls = {b.get("url") for b in bookmarks}
```

- [ ] **Step 3: Manual verification**

In the scratch copy: open an article, press `r` inside detail → see "marked unread", back out → list shows it un-dimmed. Open again, it auto-marks read on open (Enter), so it dims.

- [ ] **Step 4: Commit**

```bash
git add stella.py
git commit -m "feat: toggle read/unread from the article detail view"
```

---

## Task 5: Tag entry picker (`g` in the browse list)

**Files:**
- Modify: `stella.py` — add `tag_picker`; add `g` handler in `paginate_posts`

- [ ] **Step 1: Implement the tag picker screen**

Add this function just above `paginate_posts` (before line 1413):

```python
def tag_picker(state: dict, url: str):
    """Toggle existing tags on/off for `url` and add new ones. Mutates+saves state."""
    if not url:
        return
    while True:
        existing = all_tags(state)
        current = set(get_tags(state, url))
        clear_screen()
        print_header("Tags")
        print()
        if existing:
            print(c("  Existing tags (type a number to toggle):", "dim"))
            for idx, t in enumerate(existing, 1):
                mark = c(" ✓", "accent") if t in current else "  "
                print(f"   {mark} {c(str(idx).rjust(3), 'accent')}  {t}")
        else:
            print(c("  No tags yet.", "dim"))
        print()
        print(c("  Current: ", "dim") + (c(", ".join(sorted(current)), "accent")
                                         if current else c("(none)", "dim")))
        print()
        print(c("  [number] toggle   [n] new tag   [backspace] done", "dim"))

        key = read_key()
        if key in ("backspace", "esc", "enter", "q", "quit"):
            break
        elif key == "n":
            print()
            new = input_line(c("  New tag: ", "accent"))
            if new:
                tags = get_tags(state, url)
                tags.append(new)
                set_tags(state, url, tags)
                save_state(state)
        elif key.isdigit():
            n = int(key)
            # support multi-digit by reading more digits
            if existing:
                # single keypress only handles 1-9; for >9 tags, fall through
                if 1 <= n <= len(existing):
                    t = existing[n - 1]
                    tags = get_tags(state, url)
                    if t in tags:
                        tags = [x for x in tags if x != t]
                    else:
                        tags.append(t)
                    set_tags(state, url, tags)
                    save_state(state)
```

> Scope note (YAGNI): single-digit toggle covers up to 9 existing tags, which is plenty for this user. New tags are always addable via `n`. If the tag list ever exceeds 9, the `n` path still works; multi-digit input is deliberately out of scope.

- [ ] **Step 2: Add the `g` handler in `paginate_posts`**

After the `r` handler from Task 3 Step 3, add:

```python
        elif key == "g" and total > 0:
            tag_picker(state, display[cursor].get("url", ""))
            state = load_state()
```

- [ ] **Step 3: Manual verification**

Scratch copy: press `g` on a row → picker opens. Press `n`, type `politics`, Enter → added (shows in Current). Press `1` → toggles it off/on. Backspace → done. `cat stella_state.json` shows `"tags": ["politics"]`.

- [ ] **Step 4: Commit**

```bash
git add stella.py
git commit -m "feat: tag picker (g) — toggle existing tags and add new ones"
```

---

## Task 6: Tag filter in the browse list (`G`)

**Files:**
- Modify: `stella.py` — add `pick_tag`; extend filter logic + add `G` handler in `paginate_posts`

- [ ] **Step 1: Implement a tag chooser**

Add above `paginate_posts`:

```python
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
```

- [ ] **Step 2: Extend the filter derivation to support `filter_kind == "tag"`**

In `paginate_posts`, the filter block (lines 1433-1452) handles `None`, `"month"`, `"day"`. Add a `"tag"` branch. Replace lines 1436-1452 (the `else:` building `display`) with:

```python
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
```

> The `if filter_kind is None:` branch above it (lines 1433-1435) stays as-is. This converts the prior `else` into an `elif filter_kind == "tag"` + `else`.

- [ ] **Step 3: Add the `G` handler**

After the `g` handler from Task 5 Step 2, add:

```python
        elif key == "G":
            chosen = pick_tag(state)
            if chosen is not None:
                filter_kind = "tag"
                filter_value = chosen
                cursor = 0
```

`c` (clear filter, line 1576) already resets `filter_kind`/`filter_value` to `None`, so it clears a tag filter too — no change needed.

- [ ] **Step 4: Manual verification**

Scratch copy: tag two articles `politics`, press `G`, choose `politics` → list shows only those two; top strip reads `Filter: tag: politics`. Press `c` → filter clears.

- [ ] **Step 5: Commit**

```bash
git add stella.py
git commit -m "feat: filter browse list by tag (G)"
```

---

## Task 7: Tag filter inside the bookmarks view (`B`)

**Files:**
- Modify: `stella.py` — `show_bookmarks` (lines 1643-1704)

- [ ] **Step 1: Add an optional tag filter to the bookmarks list**

Replace the body of `show_bookmarks` (lines 1643-1704) with a version that loads state and supports a `t` key to pick a tag filter:

```python
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
            show_post_detail(full_post)
            set_read(state, bm.get("url", ""), True)
            save_state(state)
        elif key == "r" and total > 0:
            remove_bookmark(shown[cursor].get("url", ""))
            bookmarks = load_bookmarks()
            if not bookmarks:
                clear_screen()
                print(c("\n  All bookmarks removed. Press any key...", "dim"))
                read_key()
                break
            cursor = min(cursor, max(0, len(bookmarks) - 1))
```

> `r` still means "remove bookmark" inside this screen (its existing meaning), not "toggle read" — bookmarks view keeps its own key semantics. Read state is shown (dim) and set-on-open, consistent with the main list.

- [ ] **Step 2: Manual verification**

Scratch copy: bookmark two articles, tag one `politics`. Press `B` → both shown. Press `t`, choose `politics` → only the tagged one. Press `c` → both again. Open one → it dims (read).

- [ ] **Step 3: Commit**

```bash
git add stella.py
git commit -m "feat: tag filter inside the bookmarks view"
```

---

## Task 8: What's-new popup + version bump (TDD for gating)

**Files:**
- Create: `tests/test_update_logic.py`
- Modify: `stella.py` — `__version__`, add `CHANGELOG`, `should_show_whatsnew`, `show_whatsnew`

- [ ] **Step 1: Write failing tests**

Create `tests/test_update_logic.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stella


class WhatsNewGateTest(unittest.TestCase):
    CHANGELOG = {"1.1.0": ["line a", "line b"]}

    def test_first_run_does_not_show(self):
        # last_seen None => fresh install, no popup
        self.assertFalse(stella.should_show_whatsnew(None, "1.1.0", self.CHANGELOG))

    def test_upgrade_shows(self):
        self.assertTrue(stella.should_show_whatsnew("1.0.2", "1.1.0", self.CHANGELOG))

    def test_same_version_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew("1.1.0", "1.1.0", self.CHANGELOG))

    def test_no_changelog_entry_does_not_show(self):
        self.assertFalse(stella.should_show_whatsnew("1.0.2", "1.2.0", self.CHANGELOG))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_update_logic -v`
Expected: ERROR — `module 'stella' has no attribute 'should_show_whatsnew'`.

- [ ] **Step 3: Bump version, add CHANGELOG + gate + popup**

Change line 23:

```python
__version__ = "1.1.0"
```

Add near the version block (after line 28, the `UPDATE_FILES` list), the changelog:

```python
CHANGELOG = {
    "1.1.0": [
        "Read tracking — opening an article marks it read; press r to toggle.",
        "Read articles show dimmed in the list.",
        "Tags — press g to tag any article (pick existing or type a new one).",
        "Press G to filter the list by tag; B (bookmarks) has a tag filter too.",
        "Updates now apply in place and drop you back where you were.",
    ],
}
```

Add the gate function near the state helpers (e.g. just after `clear_resume` in Task 1's block):

```python
def should_show_whatsnew(last_seen, current: str, changelog: dict) -> bool:
    """Show the popup only on a real upgrade to a version we have notes for."""
    if not last_seen:           # first-ever run: set baseline silently
        return False
    if last_seen == current:
        return False
    return current in changelog
```

Add the popup renderer near `dance_for_stella` (after line 2457). Reuse the same centered-box drawing:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_update_logic -v`
Expected: PASS (4 tests OK).

- [ ] **Step 5: Commit**

```bash
git add tests/test_update_logic.py stella.py
git commit -m "feat: what's-new popup + CHANGELOG, bump to v1.1.0"
```

---

## Task 9: Startup wiring — what's-new + resume restore (TDD for resume serialization)

**Files:**
- Modify: `stella.py` — `main` (lines 2666-2693); `paginate_posts` (add `resume` param); add `_restore_from_resume`, `_serialize_filter`, `_deserialize_filter`
- Modify: `tests/test_update_logic.py` (add resume serialization tests)

- [ ] **Step 1: Write failing tests for filter (de)serialization**

Append to `tests/test_update_logic.py`:

```python
from datetime import datetime


class FilterSerializeTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(stella._serialize_filter(None, None), None)
        self.assertEqual(stella._deserialize_filter(None, None), (None, None))

    def test_month(self):
        v = stella._serialize_filter("month", (2026, 5))
        self.assertEqual(v, [2026, 5])
        self.assertEqual(stella._deserialize_filter("month", v), ("month", (2026, 5)))

    def test_day(self):
        d = datetime(2026, 5, 6)
        v = stella._serialize_filter("day", d)
        self.assertEqual(stella._deserialize_filter("day", v)[1].date(), d.date())

    def test_tag(self):
        v = stella._serialize_filter("tag", "politics")
        self.assertEqual(v, "politics")
        self.assertEqual(stella._deserialize_filter("tag", v), ("tag", "politics"))
```

Run: `python3 -m unittest tests.test_update_logic -v` → ERROR (`_serialize_filter` missing).

- [ ] **Step 2: Implement filter (de)serialization helpers**

Add near the state helpers:

```python
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
```

Run: `python3 -m unittest tests.test_update_logic -v` → PASS.

- [ ] **Step 3: Add `resume` param to `paginate_posts`**

Change the signature (lines 1413-1415):

```python
def paginate_posts(posts: list[dict], highlight: str | None = None, all_posts: list[dict] | None = None,
                   db_total: int | None = None, site: dict | None = None,
                   slug: str | None = None, all_loaded: bool = True,
                   resume: dict | None = None):
```

After the filter-state init (after line 1429, `filter_value = None`), seed from resume:

```python
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
```

> `state` is defined at the top of `paginate_posts` (Task 3 Step 1) before this block runs, so `set_read(state, ...)` is valid. `cursor` was set to 0 at line 1422; this overwrites it.

- [ ] **Step 4: Implement `_restore_from_resume`**

Add just above `main` (before line 2666):

```python
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
```

- [ ] **Step 5: Wire what's-new + resume into `main`**

Replace the top of `main` (lines 2666-2669) with:

```python
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
```

> The existing `if len(sys.argv) > 1:` direct-CSV branch and the `while True: site_selector()` loop stay unchanged below this. After a resume returns (user backs out of the restored list), control falls into the normal selector loop.

- [ ] **Step 6: Manual verification (resume + popup, no real update)**

Scratch copy. Resume:
```bash
cd /tmp/stella_test
python3 - <<'PY'
import json
json.dump({"__meta__": {"resume": {"slug": "brennendefrage_net", "cursor": 5,
          "filter_kind": None, "filter_value": None, "open_url": None}}},
          open("stella_state.json", "w"))
PY
python3 stella.py   # should jump straight into brennendefrage list at row 6
```
What's-new:
```bash
python3 - <<'PY'
import json
json.dump({"__meta__": {"last_seen_version": "1.0.2"}}, open("stella_state.json","w"))
PY
python3 stella.py   # popup appears once; relaunch → no popup
```

- [ ] **Step 7: Commit**

```bash
cd /Users/ali/Desktop/Private/stella
git add stella.py tests/test_update_logic.py
git commit -m "feat: startup what's-new popup + resume restore"
```

---

## Task 10: Prompted self-update with auto-relaunch (TDD for `_raw_url` env)

**Files:**
- Modify: `stella.py` — `_raw_url` (lines 2494-2496), `start_update_checker` (debug hook), add `confirm_modal` + `apply_update_and_relaunch`, banner+`A` in `paginate_posts` and `site_selector`
- Modify: `tests/test_update_logic.py` (add `_raw_url` env tests)

- [ ] **Step 1: Write failing test for `_raw_url` override**

Append to `tests/test_update_logic.py`:

```python
class RawUrlTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("STELLA_UPDATE_BASE", None)

    def test_default_is_github(self):
        os.environ.pop("STELLA_UPDATE_BASE", None)
        self.assertIn("raw.githubusercontent.com", stella._raw_url("stella.py"))

    def test_env_override(self):
        os.environ["STELLA_UPDATE_BASE"] = "http://localhost:8000/x/"
        self.assertEqual(stella._raw_url("stella.py"),
                         "http://localhost:8000/x/stella.py")
```

Run: `python3 -m unittest tests.test_update_logic -v` → the override test FAILS.

- [ ] **Step 2: Add env override to `_raw_url`**

Replace `_raw_url` (lines 2494-2496):

```python
def _raw_url(fname: str) -> str:
    base = os.environ.get("STELLA_UPDATE_BASE")
    if base:
        return base.rstrip("/") + "/" + fname
    return (f"https://raw.githubusercontent.com/{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/{UPDATE_BRANCH}/{fname}")
```

Run: `python3 -m unittest tests.test_update_logic -v` → PASS.

- [ ] **Step 3: Add the `STELLA_FAKE_DOWNLOADED` debug hook**

In `start_update_checker` (lines 2643-2649), at the top of the function add:

```python
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
```

- [ ] **Step 4: Add `confirm_modal` and `apply_update_and_relaunch`**

Add near `show_whatsnew`:

```python
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
```

- [ ] **Step 5: Add the `A` apply key + banner hint to `paginate_posts`**

In `paginate_posts`, surface the pending-update banner in the footer. After line 1508 (the footer `print(...)`), add:

```python
        with _update_lock:
            _pending_v = _update_status["remote_version"] if _update_status["downloaded"] else None
        if _pending_v:
            print(c(f"  ★ Stella v{_pending_v} ready — press A to update now", "accent", "bold"))
```

Add the `A` handler (after the `G` handler from Task 6 Step 3):

```python
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
```

> `A` only acts when an update is actually pending; otherwise it's a no-op. If `slug` is `None` (e.g. a cross-site search result list), resume still re-execs and simply lands at the selector after restart because `_restore_from_resume` can't match a `None` slug — acceptable.

- [ ] **Step 6: Add the `A` apply key to `site_selector`**

`site_selector` already prints `update_banner_line()` (lines 1921-1923). Add an `A` handler in its key loop (after the `!` handler at line 1962):

```python
        elif key == "A":
            with _update_lock:
                pend = _update_status["downloaded"]
                pend_v = _update_status["remote_version"]
            if pend and confirm_modal(f"Update to Stella v{pend_v} now?"):
                state = load_state()
                apply_update_and_relaunch(state, {})  # no list position to resume
```

- [ ] **Step 7: Manual verification — fake-downloaded path (no network)**

Scratch copy, stage a bumped pending file and trigger the prompt:

```bash
cd /tmp/stella_test
# make a pending stella.py that reports v1.1.1
sed 's/__version__ = "1.1.0"/__version__ = "1.1.1"/' stella.py > stella.py.stella_pending
# add a changelog entry for 1.1.1 in the pending file (manual edit or sed)
STELLA_FAKE_DOWNLOADED=1.1.1 python3 stella.py
```
Verify: banner shows `★ Stella v1.1.1 ready — press A`. Navigate into a site, press `A` → confirm → screen says "Updating… restarting", app relaunches, shows the v1.1.1 what's-new popup, and `grep __version__ stella.py` in the scratch dir now reads `1.1.1`. Resume lands you back in the same site list.

- [ ] **Step 8: Manual verification — local fake remote (real download path)**

```bash
cd /tmp && mkdir -p fake_remote && cd fake_remote
sed 's/__version__ = "1.1.0"/__version__ = "1.1.2"/' /tmp/stella_test/stella.py > stella.py
cp /tmp/stella_test/scraper.py . ; cp /tmp/stella_test/Stella.cmd . 2>/dev/null || true
python3 -m http.server 8000 &
cd /tmp/stella_test
STELLA_UPDATE_BASE=http://localhost:8000/ python3 stella.py
# in the selector press g (Get update) → wait → banner shows v1.1.2 ready → A → confirm
kill %1   # stop http.server when done
```
Verify the same relaunch + popup + resume, sourced from localhost.

- [ ] **Step 9: Commit**

```bash
cd /Users/ali/Desktop/Private/stella
git add stella.py tests/test_update_logic.py
git commit -m "feat: prompted self-update with in-place relaunch and resume"
```

---

## Task 11: Update help screen + footer hints

**Files:**
- Modify: `stella.py` — `show_help` (lines 1787-1832), `paginate_posts` footer (line 1507)

- [ ] **Step 1: Add new keys to the help screen**

In `show_help`, extend the `"Actions"` section (lines 1807-1813) to include read/tags:

```python
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
```

In the `"Updates"` section (lines 1814-1817), add the apply key:

```python
        ("Updates", [
            ("u", "Quick update (fetch latest)"),
            ("U", "Scrape custom date range"),
            ("A", "Apply a downloaded update now (when available)"),
        ]),
```

- [ ] **Step 2: Update the browse-list footer hint**

Replace the footer hint string (line 1507) to mention the new keys without overcrowding:

```python
            c(f" ↑↓ move  ←→ page  [s] title  [r] read  [g] tag  [G] tag filter  [b] bookmark  [B] bookmarks{filter_hint}{update_hint}  [?] help", "dim")
```

- [ ] **Step 3: Manual verification**

Scratch copy: press `?` → help lists `r`, `g`, `G`, `A`. Footer shows the new keys.

- [ ] **Step 4: Commit**

```bash
git add stella.py
git commit -m "docs: surface read/tag/update keys in help and footer"
```

---

## Task 12: Full regression test pass + run the unit suite

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests PASS (state + update logic).

- [ ] **Step 2: Walk the spec's manual test plan**

On the scratch copy, execute every numbered check in the spec's "Testing" section:
read dim/toggle (1-2), tag add/toggle/persist (3), `G` filter + clear (4), bookmarks tag filter (5), clean start after deleting `stella_state.json` (6), what's-new once + first-run silent (7-8), resume restore + missing-slug skip (9-10), fake-downloaded self-update (a), local fake-remote self-update (b).

- [ ] **Step 3: Confirm no live data was touched**

Run: `cd /Users/ali/Desktop/Private/stella && git status`
Expected: only `stella.py`, `tests/`, and `docs/` changes — no modified CSVs, no `stella_state.json` committed (add it to `.gitignore` if not already ignored — see Step 4).

- [ ] **Step 4: Ignore the new state file**

Check `.gitignore` includes the runtime state file; if not:

```bash
grep -q stella_state.json .gitignore || echo "stella_state.json" >> .gitignore
git add .gitignore
git commit -m "chore: ignore stella_state.json runtime store"
```

- [ ] **Step 5: STOP — report to user, do NOT push**

Present unit results + manual-test results. Await explicit approval before any branch/PR/push.

---

## Self-Review

**Spec coverage:**
- Read tracking (open + `r`, dim) → Tasks 2, 3, 4. ✓
- `stella_state.json` keyed by URL, `__meta__` reserved, helpers skip `__` keys → Task 1. ✓
- Tags independent of bookmarks, picker + free text → Task 5. ✓
- Browse tags both entry points (`G` list + `B` bookmarks) → Tasks 6, 7. ✓
- Site badge unchanged → untouched (no task modifies `count_unread`). ✓
- What's-new popup, current version only, first-run silent → Tasks 8, 9. ✓
- Prompted self-update, `A`, confirm, apply, `os.execv`, resume → Tasks 9, 10. ✓
- Debug hooks `STELLA_UPDATE_BASE` / `STELLA_FAKE_DOWNLOADED` → Task 10. ✓
- Local test plan (no GitHub, no push) → Tasks 10, 12. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type/name consistency:** `load_state/save_state(path=...)`, `is_read/set_read(state,url[,v])`, `get_tags/set_tags`, `all_tags`, `get/set_last_seen_version`, `get/set/clear_resume`, `_serialize_filter/_deserialize_filter`, `should_show_whatsnew`, `show_whatsnew`, `tag_picker`, `pick_tag`, `confirm_modal`, `apply_update_and_relaunch`, `_restore_from_resume` — names used identically across tasks. `resume` dict shape `{slug, cursor, filter_kind, filter_value, open_url}` consistent between capture (Task 10 Step 5) and restore (Task 9 Steps 3-4). ✓

**Known scope notes (intentional):** single-digit tag toggle (≤9 tags); `A` from a `None`-slug list resumes to selector; in-detail apply not supported (apply from list/selector only).
