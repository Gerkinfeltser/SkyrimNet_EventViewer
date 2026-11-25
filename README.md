# SkyrimNet event view

A cleaner page for reading what SkyrimNet's NPCs say, think and remember.

- **Left column:** the conversation, one box per NPC turn in their colour.
- **Right column:** memories, IntelEngine and PoliticalEngine events, and OmniSight screenshots, in step with the conversation.
- **History:** the live game, any older playthrough, or any day of the conversation log.
- **Prompts:** click a line to see the LLM prompt that produced it.

It only reads: nothing here writes to the game, SkyrimNet or its files.

## Running it

Double-click `sn-eventview.bat`. It starts a small helper on <http://localhost:8791/> and opens the page in your browser. Close the window to stop it.

From a terminal: `python serve.py` (Python 3, standard library only). Options:

| Command | What it does |
|---|---|
| `python serve.py <SkyrimNet folder>` | Read only that SkyrimNet folder (default: every `?:/Modlists/<name>/overwrite/SKSE/Plugins/SkyrimNet`) |
| `python serve.py --no-browser` | Don't open a browser tab |
| `python serve.py --check` | Self-test against the real folder |
| `python serve.py --types` | List every event type with counts, for `config.yaml` |

Opening `index.html` straight from disk also works for the live game, but without history, memories, older prompts or `config.yaml`. The page says so where those would be. `?host=http://…:8080` points it at another SkyrimNet server.

## The page

**Top bar:** where you are (follows your scrolling), the **History** picker, and **☰**.

- Modlist (in ☰, shown when there is more than one): every modlist with a SkyrimNet folder under `?:/Modlists/` (found when the helper starts; restart it after adding one). ● marks the one being played, or played last: the page starts there, and *Live* always reads it. Picking another modlist opens its newest playthrough. The address keeps the modlist and view (`#ml=…&view=…`), so a refresh or a bookmark opens them again.

- History: *Live (current save)*, then **playthroughs** (newest first, named after your character), then the **conversation log** by real day.
- A playthrough's events are cut back to the save point when you load an older save. The conversation log keeps every line ever said, but has no locations or game time.

**Chat column**

- Each NPC turn is one box outlined in their colour. The header shows name, game date and time, and the place within the cell. Inside the box:
  - 💭 thoughts: a paler bubble.
  - Spoken lines start with **To *listener*:**, with the listener's name in their own colour.
  - 🗣️ marks background chatter.
- Narration is in gold italics, with 📖 books and 💤 sleep.
- Actions get their own line behind their kind's icon. The 🤝 menu in the bar switches each kind (counts beside them), or all at once:
  🎬 custom actions, 🪙 trades and purchases, 💰 gold and debts, 🎁 items given and taken, 🗝️ looting, 🍖 eating and drinking, 👊 brawls and fights, 🫳 pickpocketing, 🧭 followers (joining, leaving, waiting), 🪑 furniture, and, off at first, ⚠️ failed actions (couldn't find, couldn't reach…) and 📊 relationship checks.
- 💤 marks going to sleep and waking up.
- Your own lines are blue.
- A separator marks each change of cell. It shows the full location, the game time and ⏱ the real time spent there (gaps over 20 minutes are left out).
- The chat ends with **● Now** and the game time of the latest line (live), or *End of this playthrough* / *End of this day* in History.
- 📌 Markers such as *🧠 Hadvar memory created ┄┄┄*, *📔 Camilla Valerius diary written* and *📷 Embershard Mine captured* sit after the line each card in the context column belongs to. Click one to bring its card into view.
- Bar: 💭 thoughts and 🧾 narration toggles, the 🤝 actions menu, 📌 markers, each with counts; 🎨 colour names in the text; ⤓ keep scrolling (follow new lines as they arrive).

**Context column** (right; ☰ › context column)

| Toggle | Cards |
|---|---|
| 🧠 | NPC memories: owner, emotion, importance, kind (background knowledge entries are left out) |
| IntelEngine, PoliticalEngine | events of those mods, taken out of the chat (set in `config.yaml`) |
| 📔 | NPC diary entries, rendered as markdown |
| 📷 | OmniSight screenshots with their descriptions (click the picture to see it large with its description, middle-click for a new tab) |

- **Following you:** cards after the line you're reading are dimmed (not with 📍 on, where position shows it). ⤓ keeps the current card in view.
- **📄 Full text:** whole OmniSight descriptions and diaries. Off: the first few lines (an OmniSight picture's popup still has the whole description).
- **📍 Line up:** each card sits beside the chat line it belongs to and the column scrolls with the chat (the mouse wheel over the column scrolls the chat). Where cards crowd, the column moves faster than the chat so the card for the line at the top of the chat stays beside it. Over the last screen of the chat it runs a little ahead, so the last cards come fully into view as the chat reaches its end. Off: the column scrolls on its own and ⤓ applies.
- Turning a card type off (🧠, 📔, 📷) also hides its 📌 markers in the chat.
- **Jump to a line:** clicking a card jumps the chat to where it happened.
- **Names:** names inside the cards are coloured (🎨 in the column's bar).

**Timeline** (bottom)

- One block per stay in a cell. Hover a block for its time span and real time.
- The cursor shows the date and time of what you're reading. Coloured ticks mark memories.
- Click or drag to jump.

**Prompt panel:** click any line.

- The panel shows the request that produced it, found by matching the reply text: model, tokens, time, then each message as rendered markdown.
- Consecutive messages from one role (the event history comes as many short user messages) share one label, such as *USER · 15 messages*, and one box, with a dashed line between messages.
- **source** shows the exact text.
- The dropdown under the title lists the messages and headings. It follows your scrolling; pick one to jump there.
- Prompts come from SkyrimNet's request log (since the game started) and the prompt log files, which SkyrimNet keeps for about two days.

**☰ menu:**
- modlist
- background chatter
- other events (the few types the page has no line style for)
- context column
- ids: event ids on lines, request ids on prompts
- real time: the real date and time on every chat line, marker and card, beside the game time (on at first)

Toggles are remembered by the browser.

## config.yaml

Reload the page after editing. Only read when the page comes from `serve.py`.

```yaml
lan: no                     # yes = other devices on your network or Tailscale can open http://<this PC's IP>:8791/ (restart the helper)

hide:                       # lines to leave out: regular expressions, case-insensitive
  - 'continues the conversation\.?$'

memories: yes               # no = no 🧠 cards, markers or toggle
diary: yes                  # no = no 📔 cards, markers or toggle
omnisight: yes              # no = no 📷 cards, markers or toggle

column:                     # event groups shown as cards in the right column instead of the chat
  - label: IntelEngine      # toggle text (an emoji works)
    hue: 190                # card colour, 0-360
    types: '^(intel_.*|follower_recruited)$'   # event types (see serve.py --types)
  - label: PoliticalEngine
    hue: 42
    types: '^political_'
    # shown: no            # the toggle starts off
```

Keep patterns in single quotes. The reader understands this subset of YAML: `key: value`, lists of values and lists of maps.

## Where the data comes from

All paths are inside the picked modlist's `overwrite/SKSE/Plugins/SkyrimNet`.

| Source | Used for |
|---|---|
| SkyrimNet's web API, `http://127.0.0.1:8080` | live events, the request log since launch |
| `data/SkyrimNet-*.db` (opened read-only) | playthroughs: events, memories, diary entries, names, OmniSight records |
| `logs/conversation_log*.log`, `logs/_archive/` | history by real day |
| `logs/openrouter_input*.log`, `logs/openrouter_output*.log` | prompts for older lines |
| `screenshots/omnisight/*.png` | OmniSight pictures |

Things SkyrimNet doesn't keep, so the page can't show them:

- **Spoken audio:** TTS plays from memory and is never saved.
- **Prompts older than about two days:** the prompt log files rotate.
- **Older OmniSight pictures:** OmniSight keeps one picture per subject and replaces it when it retakes it, so an older playthrough can show a newer picture.

## Files

| File | |
|---|---|
| `index.html` | the page: markup, style and script in one file, built with DOM nodes only (no `innerHTML`) |
| `serve.py` | the read-only helper: static files plus `/local/*` JSON routes, bound to 127.0.0.1 only (all addresses with `lan: yes`); answers only requests addressed to localhost, or to an IP address with `lan: yes` |
| `sn-eventview.bat` | double-click launcher |
| `config.yaml` | LAN access, hide list and right-column groups |
| `LICENSE` | MIT |

With `lan: yes`, a page opened from another device reaches SkyrimNet through the helper (`/sn/…`, only the three read-only calls the page makes), since SkyrimNet itself answers only on this PC. Anyone who can reach this PC can then read your dialogue, memories and diaries.
