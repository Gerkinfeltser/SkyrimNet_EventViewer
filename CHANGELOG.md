# Changelog

What changed for someone using the page, newest first. The README describes how everything works now; the git log has the detail.

## Unreleased

### 2026-11-28

- 📡 History's box is green while it shows the running game, so it's clear you're not on a history item.
- 🧾 The popup's resize corner also works where a browser has no pointer events (in game through Magelight).
- 🎮 In game with Magelight UI: `python serve.py --magelight=<mod folder>` builds a mod that opens the page on F7, with history, memories, pictures and `config.yaml` from serve.py.
- 🎮 `sn-eventview-magelight.bat`: double-click to make that mod in MO2's mods folder, no path to type.
- 🎮 The in-game hotkey is yours to pick in `magelight.ini`, which updates leave alone. ☰ shows it, and how to change it.

## v0.1.0 alpha · 2026-11-28

The first tagged version, a pre-release: alpha software, so expect rough edges.

### 2026-11-28

- 📌 Cards and markers on the same chat line (such as everything after a day's last line) go in the order they happened, not grouped by kind.
- 📡 Live with no game running shows the newest playthrough (*⚠ No game · newest: …*) and switches to the live feed by itself when the game starts.
- 💾 A save copied into several MO2 profiles shows once, not once per profile.
- 📷 A card whose picture OmniSight took again later (in any playthrough) no longer shows that later picture; it says when it was retaken. A person or an item keeps the newer picture, labelled with its date.
- 📌 Connectors to cards above their markers (at the end of a playthrough, say) no longer run on top of each other: they nest, the lanes use the whole gap up to the cards and close up when many need room, and the faint ones show over the column's edge.

### 2026-11-27

- ⏪ The undone-play fold and its marker get a grey line icon like the rest.
- 📒 This changelog, and a Backlog section in the README.
- 👁️ Tested in a live game: a new view's picture is kept while the live view is open, and still shows after a reload.
- 📌 Every card on screen gets its own connector, in a lane of its own so none cross (circuit style); the outlined card's is bright.
- 🖼️ OmniSight and 👁️ pictures are 16:9; in 🤏 compact view they sit left of the first four lines of text.
- 🧾 A popup with no prompt found stays small.
- ⏪ Cards from play that a loaded save took back fold into one card, which opens on click.
- 📡 History says "⚠ Live: no game running" when SkyrimNet isn't answering.
- 📌 Column buttons hide cards only; the 📌 menu hides markers. Clicking a hidden card's marker opens its popup.
- 💾 Save cards show the save's own screenshot. 👁️ "scenes" are now "views". 🧠 Recalled memories show as small cards.
- 📌 A connector runs from each marker to its card's title; the first and last markers are linked; the date sits above the timeline.
- 📡 The live view loads again when an event carries plain text ("Jenassa ate Ale") instead of data; a leading `[item_consumed]` tag is dropped from its text.
- 📔 Diary cards sit at their "diary written" line.
- 📍 The timeline cursor stays put when your own turn is at the top.
- ℹ️ About in ☰: version, what the page reads, links.
- 🔍 Filter both columns by name or words, 🔦 find, the column can lead the chat; long playthroughs open fast.
- 👁️ Views of what NPCs saw in the context column, 🧾 for IntelEngine events, a resizable popup, darker scrollbars.
- 💾 Saves you made, in the context column.
- 📅 Minimap hover follows the short game dates.

### 2026-11-26

- 💭 Thought bubbles a little narrower than spoken lines.
- 🧠 🏞️ Recalled memories and new scenes noted under replies; short dates.
- 🏷️ Prompt sections list, `*` marks the cached part, the popup keeps the mouse wheel.
- Line icons instead of emoji (☰ Use emoji to switch back); 🔝 🔚 📡 in the top bar.
- 📍 Location changes stand out with › chevrons in their colour, ⏫ ⏬ step between them; your turns get faint stripes; 🍖 "consumed".
- 🎮 Game dialogue marked, 🎯 the action a reply picked.

### 2026-11-25

- 🌓 Timeline: sky wave, game-time spacing, text size.
- 🧠 Knowledge packs no longer show as memories.
- README: getting it (Python, download, first run).
- First version: chat and context column, prompt popup, column steps, player turns.
