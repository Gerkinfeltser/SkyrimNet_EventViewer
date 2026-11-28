# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A read-only viewer for SkyrimNet (a Skyrim LLM-NPC mod): chat on the left, memories, diaries, OmniSight shots and other mods' events in a right-hand "context column", and the LLM prompt behind any line. README.md is the user-facing spec. It describes every toggle and behaviour, and feature commits update it alongside the code.

## Commands

- Run: `python serve.py` (Python 3, stdlib only, no install). Serves on http://localhost:8791/. `--no-browser` skips opening a tab. A positional path picks one SkyrimNet folder instead of discovering them all.
- Self-test: `python serve.py --check`. This is the only test suite. It runs asserts against the **real** SkyrimNet folder on this machine (playthrough DBs, logs, config parsing, path-traversal guards, `host_ok`), so it fails on a machine without SkyrimNet data. Add new parser and guard asserts here.
- Pre-commit hook `.githooks/pre-commit` (turn on once per clone: `git config core.hooksPath .githooks`): runs `--check` when `serve.py`/`config.yaml` change, and fails on `innerHTML`, a DB opened without `ro()`, or `index.html`/`serve.py` changed without `CHANGELOG.md` (`SNEV_NO_CHANGELOG=1` for a change nobody using the page would notice).
- `python serve.py --types` lists the event types seen across all playthrough DBs, for writing `config.yaml` `types:` patterns.
- There is no build, lint or bundler. `index.html` is edited and served as is. Reload the page after a change; restart `serve.py` after editing Python, or after changing `skyrimnet:`/`lan:` in `config.yaml`.

## Architecture

Two files do everything:

- **`serve.py`**: a `SimpleHTTPRequestHandler` that serves static files from this folder plus JSON routes under `/local/*` (`modlists`, `playthroughs`, `events`, `requests`, `conversation-days`, `conversation`, `memories`, `diary`, `omnisight`, `saves`, `shot`). With `lan: yes` it also proxies SkyrimNet's read-only API calls (`SN_CALLS`) under `/sn/*`.
  - **Modlists**: at startup `find_modlists()` collects SkyrimNet folders from the CLI arg, `config.yaml` `skyrimnet:`, MO2 `ModOrganizer.ini` files (drive scan plus `%LOCALAPPDATA%\ModOrganizer`), and the game `Data` folder. Each folder gets a display name. The page selects one by **name** (`ml=<name>`, never a path, because `folder()` rejects paths). With no `ml`, the folder whose DB was written most recently counts as live.
  - **Data sources** (inside a SkyrimNet folder): `data/SkyrimNet-*.db` (one SQLite DB per playthrough, always opened read-only through `ro()`), `logs/conversation_log*.log` (history by real day, parsed by `parse_conv_entry`), `logs/openrouter_{input,output}*.log` (prompts joined to responses by request id), `screenshots/omnisight/*.png`. Outside it, `save_dirs()` finds the game's `.ess` saves (MO2 profile `saves/` folders, Documents) and `read_ess()` reads only their headers.
  - Parsed files are memoised by mtime through `cached()`.
- **`index.html`**: markup, CSS and one inline script. The DOM is built only with `el()` and DOM nodes, never `innerHTML` (markdown rendering in `md()`/`inline()` produces nodes too). It runs in two modes:
  - Served by `serve.py` (`LOCAL` true): history, memories, diaries, OmniSight, older prompts and `config.yaml` all work.
  - Opened from disk: live-only, talking straight to SkyrimNet's API at `http://127.0.0.1:8080` (or `?host=`). Every `/local/*` fetch must fail soft in this mode.
- **Event flow**: `poll()` fetches either the live feed (`fetchAll` pages `/events?api=list`, then polls `recent-events`) or an archive (`/local/events?db=` / `/local/conversation?day=`). Then `ingest()` → `add()` → `render()`/`renderBody()` builds chat rows. `sideKind(type)` routes an event to the context column: a configured `GROUPS` entry, or `autoGroup()` for a type with no chat style. `chatType()` lists the types that stay in the chat, and `ACT_GROUPS` maps action types to their 🤝 kinds.
- **Drawn window**: `rows` holds every chat event, but only `rows[drawStart .. drawEnd)` have real nodes (the latest `WINDOW` at first). The rest are detached placeholder `div`s, so indices (`x.at`, `data-i`, the timeline) stay global. `more()` draws `CHUNK` lines above or below without touching the drawn ones; `drawWindow()` rebuilds around a timeline click or 🔝 🔚; `renderSide()` keeps only cards in the window. Code that reads layout in a loop over cards or rows must read everything first and write after: one read after a class change lays out the whole page (~40 ms at 80k nodes).
- **Context column**: `loadMemories()` fetches memories, diaries and OmniSight records. `renderSide()` builds cards, `placeMarks()` drops 📌 markers after the chat line each card belongs to, and `alignSide()`/`alignY()`/`easeAlign()` implement 🔄️ line-up scrolling (rate capped by `ALIGN_MAX_RATE`).
- **Prompt lookup**: chat lines (`openPrompt()`) and context-column cards (their 🧾, `x.popup`) open one popup, `showShot()`. Its prompt column is filled by `promptNodes()`, which searches model **responses** for the text. The responses come from `requestLog()`: SkyrimNet's request log plus `/local/requests`, tens of MB, cached per modlist. Only model-written text can be matched. Narrative Engine plot memories are templated by that mod, so `planHits()` links them to the director request instead, matched by time and names. IntelEngine events are matched the same way to their `intel_story_dm` request (`intelHits()`, by both names). `markInputs()` greys the 🧾 of cards with no match. 👁️ scene pictures: SkyrimNet keeps only the latest in memory, so `grabScene()` copies each new one into the browser's IndexedDB (`snev-scenes`) while the live view is open; `sceneList()` builds the 👁️ cards from the vision requests. `noPrompt()` gives the reason, including "written by a mod" for a memory with no chat events.
- **Config**: `config.yaml` is parsed twice, by `parseConfig()` in the page (hide/memories/diary/omnisight/column) and by `config_list()` in `serve.py` (skyrimnet/lan). Both parse a small YAML subset by hand. Keep them in step, and don't add a YAML dependency.
- **State**: toggles are `<input data-toggle=…>` wired by `wireToggle()`. Each one sets a `hide-<name>` class on `<body>` and persists to `localStorage["snev.<name>"]`. The modlist and view live in the URL hash (`saveView()`).

## Constraints

- Read-only, always: never write to the game, SkyrimNet's DBs, logs or folders.
- Security boundary in `serve.py`: binds 127.0.0.1 unless `lan: yes`; `host_ok()` rejects other Host headers (DNS rebinding); `/local/shot` only serves names matching `SHOT_NAME`; `db=`/`ml=` values are validated against known files and names. Keep these when adding routes, and add an assert to `--check`.
- No dependencies on either side: stdlib Python, plain browser JS.
- Commit subjects are short and user-facing, often led by the emoji of the feature they touch (🔄️, 📜, 🤏…). When behaviour changes, update the matching README section and add a line under today's date in `CHANGELOG.md` in the same commit (a `### <date>` under `## Unreleased`, above the newest version; a release renames `Unreleased` to its version, `## v0.1.0 alpha · <date>`). Ideas not built yet go in the README's Backlog section; remove one when it ships.
