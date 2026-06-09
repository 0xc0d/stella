# Read Tracking & Tagged Bookmarks — Design

Date: 2026-06-09
Project: Stella news reader TUI (`stella.py`)

## Goal

Two related features:

1. **Per-article read tracking** — mark individual articles read/unread; read
   articles dim in the browse list.
2. **Tagged bookmarks / tags** — any article can carry multiple tags (independent
   of bookmark state). Tags are chosen from existing tags or created on the fly.
   Tagged articles can be browsed by tag.
3. **"What's new" popup** — after an update to a newer version, show a short
   centered popup once describing the new version and how the additions work.

## Non-goals (YAGNI)

- Tag rename / delete / merge management UI.
- Tag colors.
- Read-progress percentage.
- Changing the site-level "+N new" badge (stays timestamp-based — see below).
- Cross-device sync.

## Data model

One new JSON store, keyed by article URL (the stable identifier already used by
bookmarks):

```json
{
  "https://rrn.com.tr/...": { "read": true,  "tags": ["politics", "eu"] },
  "https://ostlicherwind.org/...": { "read": false, "tags": ["important"] }
}
```

File: `stella_state.json` (sibling of `stella_seen.json` / `bookmarks.json`).

A reserved non-URL key `"__meta__"` holds app metadata, currently the
"what's new" bookkeeping:

```json
{
  "__meta__": { "last_seen_version": "1.1.0" },
  "https://rrn.com.tr/...": { "read": true, "tags": ["politics"] }
}
```

All per-article helpers (`all_tags`, any iteration over articles) **must skip
keys starting with `__`** so the meta row is never treated as an article.

Rationale:

- Read state and tags are both per-article and both keyed by URL → one store, one
  load/save pair.
- Bookmarks remain in `bookmarks.json`, unchanged. Tags are **independent** of
  bookmark state, so they do not belong in the bookmark record.
- The global tag list is the **union** of all `tags` across the store, computed on
  demand (`all_tags()`). No separate tag-registry file.
- Entries are created lazily: an article only gets a row once it is read or tagged.
  Missing URL ⇒ unread, no tags.

## Components (all in `stella.py`)

### 1. State store helpers

New section near the existing `SEEN_FILE` block:

- `load_state() -> dict` / `save_state(state: dict)` — mirror `load_seen` /
  `save_seen` (tolerant of missing/corrupt file, UTF-8).
- `is_read(state, url) -> bool`
- `set_read(state, url, value: bool)` — creates the row if needed.
- `get_tags(state, url) -> list[str]`
- `set_tags(state, url, tags: list[str])` — normalizes (strip, dedupe,
  lowercase); drops the `tags` key / row when empty and unread.
- `all_tags(state) -> list[str]` — sorted union for the picker.

The in-memory `state` dict is loaded once in `paginate_posts` and passed to
helpers/screens, then re-synced after sub-screens (same pattern as `bookmarks` /
`bm_urls`).

### 2. Read marking

- `show_post_detail` sets `read = True` on open.
- New `r` key toggles read/unread — available in **both** the browse list and the
  detail view.

### 3. Read display

`print_post_line` gains a `read: bool` parameter. When `read` and not `selected`,
the title renders dim (color key `"dim"`) instead of `("title", "bold")`. The
selected (reverse-video) row is unaffected.

### 4. Tag entry — `g`

A picker screen for the current article:

- Lists existing tags (`all_tags`) numbered; pressing a number toggles that tag on
  or off for the article.
- A text field accepts a new tag name; submitting adds it to the article (and thus
  to the global union).
- Esc / enter commits via `set_tags`.

### 5. Tag browse — two entry points

- **`G` in the browse list:** pick a tag → list filters to articles carrying that
  tag, using the same `filter_kind` / `filter_value` mechanism as month/day
  filters. `c` clears.
- **`B` bookmarks view:** gains a tag filter — pick "all" or a specific tag to
  narrow the displayed bookmarks.

## Key bindings added

| Key | Action                         | Context            |
|-----|--------------------------------|--------------------|
| `r` | toggle read / unread           | list + detail      |
| `g` | edit tags on current article   | list (+ detail)    |
| `G` | filter browse list by tag      | list               |

`t` (chart), `w` (word cloud), `b`/`B` (bookmark/bookmarks), `s`/`S` (search),
`m`/`d`/`c` (filters), `u`/`U` (update), `y` (copy) are already taken — the new
keys avoid all of them. Update the `?` help screen and the footer hint line.

### 6. "What's new" popup

- New `CHANGELOG: dict[str, list[str]]` constant in `stella.py`, keyed by version
  string → short bullet lines describing additions and how they work. Kept in sync
  with `__version__` on each release.
- New helpers `get_last_seen_version(state)` / `set_last_seen_version(state, v)`
  read/write `state["__meta__"]["last_seen_version"]`.
- On **startup** (after `load_state`, before the site selector): if
  `last_seen_version != __version__` and `__version__ in CHANGELOG`, render a
  centered popup (reuse the existing dancer-modal rendering / box-drawing code)
  titled `What's new in Stella v{__version__}` listing `CHANGELOG[__version__]`,
  with a "press any key" dismiss. Then call `set_last_seen_version(state,
  __version__)` + `save_state` so it never shows again for this version.
- **Current version only** (user choice): only `CHANGELOG[__version__]` is shown,
  not stacked notes for skipped versions.
- First-ever run (no `__meta__`): set `last_seen_version = __version__` silently
  **without** showing the popup, so new installs don't get a changelog for a
  version they never "upgraded" from.

This feature ships as version **1.1.0**; bump `__version__` accordingly and add a
`"1.1.0"` entry to `CHANGELOG` covering read tracking + tags.

## Site-level "+N new" badge

**Unchanged.** Stays timestamp-based (`count_unread` vs `stella_seen.json`). It
answers "new since last visit"; per-article read answers "have I read this." User
confirmed the count does not need to track the new read store.

## Data flow

```
paginate_posts
  load_state() once  ->  state dict
  render list: print_post_line(..., read=is_read(state, url))
  key 'enter' -> show_post_detail -> set_read(state, url, True) -> save_state
  key 'r'     -> set_read(state, url, not is_read) -> save_state
  key 'g'     -> tag_picker(state, url) -> set_tags -> save_state
  key 'G'     -> pick tag -> filter_kind='tag', filter_value=tag
  key 'B'     -> show_bookmarks(state) with tag filter
```

## Error handling

- Corrupt / missing `stella_state.json` → treated as empty (try/except like
  `load_seen`).
- Articles without a URL cannot be tracked/tagged (URL is the key) — `r`/`g` no-op
  gracefully on such rows.
- Tag normalization prevents duplicate/whitespace tags polluting the union.

## Testing

Manual TUI verification (no test harness in repo):

1. Open an article → returns to list dimmed (read).
2. `r` on a read row → un-dims; `r` again → dims.
3. `g` → add a new tag, toggle an existing one, confirm persistence in
   `stella_state.json`.
4. `G` → pick a tag → only matching articles show; `c` clears.
5. `B` → tag filter narrows bookmarks.
6. Delete `stella_state.json` → app starts clean, everything unread/untagged.
```
