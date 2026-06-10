# Stella — project rules

## NEVER change the rendering. This is the hard rule.

The v1.0.2 render is sacred and proven-good on the user's Windows machine
(Windows Terminal is installed; it ghosts/flickers/stacks with ANY render
change). Every render experiment we shipped broke it and caused real pain.

**Do not touch, for any reason, without explicit user request:**
- `clear_screen`, `hard_clear`, `enable_vt`
- `print_post_line`, `show_post_detail`, `render_chart`, `render_word_cloud`,
  and any function that draws/paints the screen
- the frame paint model — it MUST stay full-clear-then-draw (`os.system("cls")`
  / `\033[2J\033[H`). Never introduce:
  - in-place repaint (`frame_begin`/`frame_end`, home-cursor-no-clear `\033[H`)
  - alternate screen buffer (`\033[?1049h`)
  - mouse reporting (`\033[?1000h`, `\033[?1006h`)
  - fullscreen / `enter_fullscreen` / `enable_mouse`

These exact changes caused: text ghosting, flicker, stacked/overlapping frames,
the list starting at item 5, and the page not following the cursor.

**When adding features:** only touch data/logic (scraping, dedup, tags,
filters, state, file paths). Feature UI may *add* drawn elements (a tag label,
a footer line), but the painting MECHANISM stays identical. After any change,
prove the render engine is untouched:

```
diff <(git show ae8bf1d:stella.py | sed -n '/def clear_screen/,/^def /p') \
     <(sed -n '/def clear_screen/,/^def /p' stella.py)   # must be empty
grep -nE '1049|\?1000|\?1006|frame_begin|frame_end|enter_fullscreen|enable_mouse' stella.py  # must be empty
```

## Other notes
- All data files anchor to `DATA_DIR` (= script folder), never CWD. Launchers
  can start the process in System32; CWD-relative paths cause "0 data".
- Auto-update bumps `__version__`; the updater applies pending files at next
  startup. On Windows it applies-and-reopens (no `os.execv`).
- Run tests: `python3 -m unittest discover -s tests -q`.
