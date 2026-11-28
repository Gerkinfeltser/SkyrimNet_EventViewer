"""Local helper for the SkyrimNet event view: serves the page plus SkyrimNet's on-disk history.

    python serve.py [SkyrimNet folder]      then open http://localhost:8791

Without a folder it finds SkyrimNet folders itself (see find_modlists, and "skyrimnet:" in config.yaml). The page picks one by name
(ml=<name>, never a path); without ml, the one SkyrimNet wrote to last, which is the game running now.

Reads, never writes:
  data/SkyrimNet-*.db          one SQLite database per playthrough (the events table)
  logs/openrouter_input*.log   prompts sent to the model (SkyrimNet rotates these, so only the last few files exist)
  logs/openrouter_output*.log  the model's responses, joined to prompts by request id
  <MO2 instance>/profiles/*/saves/*.ess, Documents/My Games/Skyrim*/Saves/*.ess   the game's saves (their headers only)
"""
import glob
import http.server
import ipaddress
import json
import mimetypes
import os
import re
import sqlite3
import struct
import sys
import zlib
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8791
SN_API = "http://127.0.0.1:8080"  # SkyrimNet's own server, reached through /sn by pages opened from other devices
# the read-only calls the page makes there, (path, api): the feed, the request log, and OmniSight's current scene and its picture
SN_CALLS = {("/events", "list"), ("/logs", "requests"), ("/", "recent-events"), ("/omnisight", "list"), ("/omnisight", "image")}


def no_cache(path):
    """Every response but a picture makes the browser check for a newer page or config.yaml on each load."""
    return not path.startswith(("/local/shot", "/local/savepic"))  # pictures keep their short cache


def host_ok(host):
    """The Host header names this PC: localhost, or with lan on any IP address. A website can't point its own name
    at 127.0.0.1 to read this server through the browser (DNS rebinding): such requests carry its name, not an IP."""
    name = host.rsplit(":", 1)[0] if host.count(":") <= 1 else host.rsplit("]", 1)[0].lstrip("[")  # drop the port
    if name.lower() in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ipaddress.ip_address(name)
        return LAN
    except ValueError:
        return False


def config_text():
    try:
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def config_list(text, key):
    """The "- 'value'" lines under "key:" in config.yaml."""
    m = re.search(rf"^{key}:[ \t]*(?:#.*)?\n((?:[ \t]+-.*\n?|[ \t]*(?:#.*)?\n)*)", text, re.M)
    items = re.findall(r"^[ \t]+-[ \t]*(.+?)[ \t]*$", m.group(1), re.M) if m else []
    out = []
    for v in items:  # 'single' ('' is a quote), "double", or plain; a " # note" after it is a comment
        q = re.match(r"'((?:[^']|'')*)'|\"([^\"]*)\"", v)
        out.append(q.group(1).replace("''", "'") if q and q.group(1) is not None else q.group(2) if q else re.sub(r"\s+#.*$", "", v))
    return out


def mo2_overwrite(ini):
    """An MO2 instance's overwrite folder, from its ModOrganizer.ini (default: <instance>/overwrite)."""
    try:
        with open(ini, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    def get(key):
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        if not m:
            return None
        v = m.group(1).strip()
        if v.startswith("@ByteArray(") and v.endswith(")"):
            v = v[11:-1].replace("\\\\", "\\")
        return v or None
    base = get("base_directory") or os.path.dirname(ini)
    return os.path.normpath((get("overwrite_directory") or "%BASE_DIR%/overwrite").replace("%BASE_DIR%", base))


def game_folders():
    """Skyrim SE and VR install folders, for SkyrimNet written straight into Data (Vortex, manual installs)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for game in ("Skyrim Special Edition", "Skyrim VR"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Bethesda Softworks\{game}") as k:
                out.append(os.path.join(winreg.QueryValueEx(k, "installed path")[0], "Data"))
        except OSError:
            pass
    return out


def folder_name(p):
    """<instance>/overwrite/SKSE/Plugins/SkyrimNet -> instance, <game>/Data/… -> game, <mod>/SKSE/Plugins/SkyrimNet -> mod."""
    parts = os.path.normpath(p).split(os.sep)
    if len(parts) >= 5 and [x.lower() for x in parts[-3:]] == ["skse", "plugins", "skyrimnet"]:
        return parts[-5] if parts[-4].lower() in ("overwrite", "data") else parts[-4]
    return os.path.basename(os.path.dirname(p)) or p


def fixed_drives():
    """Local fixed drives: a network, removable or CD drive can stall a glob."""
    try:
        from ctypes import windll
    except ImportError:
        return []
    return [d for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if windll.kernel32.GetDriveTypeW(f"{d}:\\") == 3]  # DRIVE_FIXED


def modlist_names(paths):
    """{name: path}, names from folder_name. A name several folders share gets each one's parent folder added
    ("ADT (Modlists)"), or the whole path if that clashes too, so a name never moves to another folder as modlists come and go."""
    def parent(p, name):
        parts = p.split(os.sep)
        i = max((i for i, x in enumerate(parts) if x == name), default=0)
        return f"{name} ({parts[i - 1]})" if i > 0 else f"{name} ({p})"
    names = [folder_name(p) for p in paths]
    names = [parent(p, n) if names.count(n) > 1 else n for p, n in zip(paths, names)]
    names = [f"{folder_name(p)} ({p})" if names.count(n) > 1 else n for p, n in zip(paths, names)]
    return dict(zip(names, paths))


def find_modlists():
    """{modlist name: SkyrimNet folder}. Looked up once at start: restart to see a new modlist.
    A folder given on the command line is the only one. Otherwise, in this order: config.yaml "skyrimnet:" folders,
    MO2 instances (a ModOrganizer.ini up to two folders deep on any drive, or a global instance), the game's Data folder."""
    arg = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    if arg:
        found = [arg]
    else:
        drives = fixed_drives()
        inis = [p for d in drives
                for p in glob.glob(f"{d}:/*/ModOrganizer.ini") + glob.glob(f"{d}:/*/*/ModOrganizer.ini")]
        inis += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "ModOrganizer", "*", "ModOrganizer.ini"))
        found = config_list(config_text(), "skyrimnet")
        found += [os.path.join(o, "SKSE", "Plugins", "SkyrimNet") for o in map(mo2_overwrite, inis) if o]
        found += [os.path.join(g, "SKSE", "Plugins", "SkyrimNet") for g in game_folders()]
        found += [p for d in drives for p in glob.glob(f"{d}:/Modlists/*/overwrite/SKSE/Plugins/SkyrimNet")]  # no readable ModOrganizer.ini
    paths, seen = [], set()
    for p in found:
        p = os.path.normpath(p)
        if os.path.normcase(p) in seen or not os.path.isdir(os.path.join(p, "data")):
            continue
        seen.add(os.path.normcase(p))
        paths.append(p)
    return modlist_names(paths)


MODLISTS = find_modlists()
LAN = bool(re.search(r"^lan:\s*['\"]?(yes|true|on)\b", config_text(), re.M | re.I))  # config.yaml: also listen on the network


def newest_db_time(sn):
    return max((os.path.getmtime(p) for p in glob.glob(os.path.join(sn, "data", "SkyrimNet-*.db"))), default=0)


def live_ml():
    """The modlist SkyrimNet wrote to last: the one being played, or played most recently."""
    return max(MODLISTS, key=lambda n: newest_db_time(MODLISTS[n]), default="")


def folder(ml=""):
    ml = ml or live_ml()
    if ml not in MODLISTS:  # a name from /local/modlists, never a path
        raise ValueError("unknown modlist")
    return MODLISTS[ml]


def modlists():
    live = live_ml()
    return [{"name": n, "live": n == live, "folder": p} for n, p in sorted(MODLISTS.items(), key=lambda x: -newest_db_time(x[1]))]
DB_NAME = re.compile(r"^SkyrimNet-[\d-]+\.db$")
HEADER = re.compile(r"^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\] \[(?P<uc>[^\]]*)\] .*?(?P<dir>request|response) \[(?P<id>req_[^\]]+)\]:$")
_cache = {}  # path -> (mtime, parsed); files are re-read only when they change


def cached(path, parse):
    m = os.path.getmtime(path)
    if path not in _cache or _cache[path][0] != m:
        _cache[path] = (m, parse(path))
    return _cache[path][1]


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def summarize_db(path):
    with ro(path) as c:
        n, first, last = c.execute("select count(*), min(id), max(id) from events").fetchone()
        row = lambda i: c.execute("select location, game_time_str, local_time from events where id=?", (i,)).fetchone() or (None, None, None)
        f, l = row(first), row(last)
        # The character's name, from their latest line ("Prisoner" until they're named in Helgen).
        player = c.execute("""select coalesce(json_extract(event_data, '$.player_name'), json_extract(event_data, '$.speaker'))
                              from events where event_type like 'dialogue_player%' and json_valid(event_data)
                                and coalesce(json_extract(event_data, '$.player_name'), json_extract(event_data, '$.speaker')) is not null
                              order by id desc limit 1""").fetchone()
    return {"db": os.path.basename(path), "events": n, "player": player[0] if player else "", "first_location": f[0], "last_location": l[0],
            "first_time": f[1], "last_time": l[1], "last_local": l[2], "modified": os.path.getmtime(path)}


def playthroughs(sn):
    out = []
    for p in glob.glob(os.path.join(sn, "data", "SkyrimNet-*.db")):
        try:
            s = cached(p, summarize_db)
            if s["events"]:
                out.append(s)
        except sqlite3.Error:
            pass  # locked or not a SkyrimNet events db; skip it
    return sorted(out, key=lambda s: s["modified"], reverse=True)


def events(sn, db):
    if not DB_NAME.match(db or ""):  # a bare file name from the list, never a path
        raise ValueError("bad db name")
    path = os.path.join(sn, "data", db)
    with ro(path) as c:
        rows = c.execute("select id, event_type, event_data, location, game_time, game_time_str, local_time,"
                         " originating_actor_UUID, target_actor_UUID from events order by id").fetchall()
    out = []
    for i, t, d, loc, gt, gts, lt, o, tg in rows:
        try:
            data = json.loads(d) if d else {}
        except ValueError:
            data = {"raw": d}
        out.append({"id": i, "type": t, "data": data, "location": loc, "gameTime": gt, "gameTimeStr": gts,
                    "localTime": lt, "originatingActor": o, "targetActor": tg, "formatted_text": ""})
    return out


def parse_log(path):
    """{request id: (use_case, local unix ms, body text)} for one log file."""
    entries, cur = {}, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = HEADER.match(line.rstrip("\r\n"))
            if m:
                ts = int(datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S.%f").timestamp() * 1000)
                cur = entries[m["id"]] = [m["uc"], ts, []]
            elif cur:
                cur[2].append(line)
    return {k: (uc, ts, "".join(body).rstrip("\n")) for k, (uc, ts, body) in entries.items()}


def requests(sn):
    """Prompts from the log files, shaped like SkyrimNet's /logs?api=requests entries."""
    logs = os.path.join(sn, "logs")
    outs = {}
    for p in glob.glob(os.path.join(logs, "openrouter_output*.log*")):
        outs.update(cached(p, parse_log))
    out = []
    for p in glob.glob(os.path.join(logs, "openrouter_input*.log*")):
        for rid, (uc, ts, body) in cached(p, parse_log).items():
            try:  # bodies are pretty JSON with raw newlines inside strings, which strict=False accepts
                payload = json.loads(body, strict=False)
            except ValueError:
                payload = None
            resp = outs.get(rid)
            out.append({"request_id": rid, "use_case": uc, "source": os.path.basename(p),
                        "model_name": (payload or {}).get("model", ""),
                        "request_payload": json.dumps(payload) if payload is not None else body,
                        "request_timestamp_unix": ts,
                        "response_payload": resp[2] if resp else "",
                        "response_timestamp_unix": resp[1] if resp else ts,
                        "duration_ms": resp[1] - ts if resp else ""})
    return sorted(out, key=lambda r: r["request_timestamp_unix"])


# ---- conversation_log*.log: every spoken line and thought, across sessions; loading an older save doesn't touch it ----
CONV_HEAD = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\] \[(NPC|PLAYER)\](.*)$")
SESSION_GAP = 20 * 60  # seconds of silence that start a new play session


def bracket(s, i):
    """Name inside [...] starting at s[i] == '[', allowing nested brackets: '[Sirimunda [hunter]]'. Returns (name, end)."""
    depth = 0
    for j in range(i, len(s)):
        depth += {"[": 1, "]": -1}.get(s[j], 0)
        if depth == 0:
            return s[i + 1:j], j + 1
    return None, i


def parse_conv_entry(who, rest):
    """'[Name] to [Listener]: text' / '[Name] Internal thought (unspoken): text' / ': text' / '[Name]: text'."""
    player = who == "PLAYER"
    rest = rest.lstrip(" ")
    if rest.startswith(":"):
        return {"type": "dialogue_player_text" if player else "dialogue", "data": {"speaker": "You" if player else "?", "dialogue": rest[1:].strip()}}
    name, i = bracket(rest, 0) if rest.startswith("[") else (None, 0)
    if name is None:
        return {"type": "conversation_log", "data": {"line": rest}}
    tail = rest[i:]
    if tail.startswith(" Internal thought (unspoken):"):
        return {"type": "player_thoughts" if player else "npc_thoughts",
                "data": {"npc_name": name, "thoughts": tail.split(":", 1)[1].strip()}}
    listener = None
    if tail.startswith(" to ["):
        listener, k = bracket(tail, 4)
        tail = tail[k:]
    text = tail[1:].strip() if tail.startswith(":") else tail.strip()
    return {"type": "dialogue_player_text" if player else "dialogue",
            "data": {"speaker": name, "listener": listener, "dialogue": text}}


def parse_conv(path):
    entries, cur = [], None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CONV_HEAD.match(line.rstrip("\r\n"))
            if m:
                t = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S.%f").timestamp()
                cur = {"localTime": t, **parse_conv_entry(m[2], m[3])}
                entries.append(cur)
            elif cur:  # a line can run over several lines (narration, paragraphs)
                key = next(k for k in ("dialogue", "thoughts", "line") if k in cur["data"])
                cur["data"][key] = (cur["data"][key] + "\n" + line.rstrip("\r\n")).strip()
    return entries


def conv_all(sn):
    logs = os.path.join(sn, "logs")
    files = glob.glob(os.path.join(logs, "conversation_log*.log")) + glob.glob(os.path.join(logs, "_archive", "conversation_log*.log"))
    seen, out = set(), []
    for p in files:
        for e in cached(p, parse_conv):
            key = (e["localTime"], e["type"], json.dumps(e["data"], sort_keys=True))
            if key not in seen:  # rotated copies can overlap
                seen.add(key)
                out.append(e)
    return sorted(out, key=lambda e: e["localTime"])


def conversation_days(sn):
    days = {}
    for e in conv_all(sn):
        d = datetime.fromtimestamp(e["localTime"]).strftime("%Y-%m-%d")
        days[d] = days.get(d, 0) + 1
    return [{"day": d, "lines": n} for d, n in sorted(days.items(), reverse=True)]


def conversation(sn, day):
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", day or ""):
        raise ValueError("bad day")
    out, last, session = [], None, ""
    for e in conv_all(sn):
        dt = datetime.fromtimestamp(e["localTime"])
        if dt.strftime("%Y-%m-%d") != day:
            continue
        if last is None or e["localTime"] - last > SESSION_GAP:
            session = "Session from " + dt.strftime("%I:%M %p").lstrip("0")  # becomes the divider and timeline block
        last = e["localTime"]
        out.append({**e, "id": len(out) + 1, "location": session, "formatted_text": "",
                    "gameTimeStr": dt.strftime("%I:%M:%S %p").lstrip("0") + ", " + dt.strftime("%a %d %b %Y") + " (real time)"})
    return out


# ---- NPC memories (memories table): who remembers what, formed from which events ----
# Owners missing from uuid_mappings are named from their own events (as speaker or thinker).
MEM_SQL = """select m.id, m.memory_type,
                    coalesce(u.actor_name, (select coalesce(json_extract(e.event_data, '$.speaker'), json_extract(e.event_data, '$.npc_name'))
                                            from events e where e.originating_actor_UUID = m.actor_uuid and json_valid(e.event_data)
                                              and coalesce(json_extract(e.event_data, '$.speaker'), json_extract(e.event_data, '$.npc_name')) is not null
                                            limit 1), ''),
                    m.content, m.location, m.creation_time,
                    m.related_event_ids, m.emotion, m.importance_score, m.tags
             from memories m left join uuid_mappings u on u.uuid = m.actor_uuid
             where m.memory_type != 'KNOWLEDGE' and m.creation_time between ? and ?{packs}
             order by m.creation_time"""  # KNOWLEDGE rows are world-knowledge entries, not memories
# On a save's first load SkyrimNet copies every knowledge pack (overlay/knowledge/) into its memories table, marked by
# pack_id. Most are typed KNOWLEDGE, but some (disease-awareness's "I've got the Rattles") are EXPERIENCE: skip them all.
# Older databases have no pack_id column, and asking for it would fail the whole query.
NO_PACKS = " and m.pack_id is null"


def _json(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def memories_in(path, start=0, end=1e12):
    try:
        with ro(path) as c:
            packs = NO_PACKS if any(r[1] == "pack_id" for r in c.execute("pragma table_info(memories)")) else ""
            rows = c.execute(MEM_SQL.format(packs=packs), (start, end)).fetchall()
    except sqlite3.Error:
        return []  # older databases may lack the table or columns
    return [{"id": i, "kind": k, "actor": a, "content": ct, "location": loc, "time": t, "events": _json(ev, []),
             "emotion": emo, "importance": imp, "tags": _json(tags, []), "db": os.path.basename(path)}
            for i, k, a, ct, loc, t, ev, emo, imp, tags in rows]


SHOT_NAME = re.compile(r"^[\w.-]+\.(png|jpg|jpeg|bmp)$", re.I)
SHOT_SQL = """select id, subject_type, subject_name, location_name, capture_time_game, capture_timestamp_local, description, file_path
              from omnisight_screenshots where is_valid and capture_timestamp_local between ? and ? order by capture_timestamp_local"""


# ---- Diaries (diary_entries): what an NPC wrote about a stretch of events, as markdown ----
DIARY_SQL = """select d.id,
                      coalesce(u.actor_name, (select coalesce(json_extract(e.event_data, '$.speaker'), json_extract(e.event_data, '$.npc_name'))
                                              from events e where e.originating_actor_UUID = d.actor_uuid and json_valid(e.event_data)
                                                and coalesce(json_extract(e.event_data, '$.speaker'), json_extract(e.event_data, '$.npc_name')) is not null
                                              limit 1), ''),
                      d.content, d.location, d.creation_time, d.related_event_ids, d.emotion, d.importance_score
               from diary_entries d left join uuid_mappings u on u.uuid = d.actor_uuid
               where d.creation_time between ? and ? order by d.creation_time"""


def diary_in(path, start=0, end=1e12):
    try:
        with ro(path) as c:
            rows = c.execute(DIARY_SQL, (start, end)).fetchall()
    except sqlite3.Error:
        return []  # older databases have no such table
    return [{"id": i, "actor": a, "content": ct, "location": loc, "time": t, "events": _json(ev, []), "emotion": emo,
             "importance": imp, "db": os.path.basename(path)} for i, a, ct, loc, t, ev, emo, imp in rows]


def shots_dir(sn):
    return os.path.join(sn, "screenshots", "omnisight")


def omnisight_in(path, start=0, end=1e12):
    """OmniSight's screenshots: one per subject (an actor, a place…), each with an AI description. SkyrimNet replaces
    a subject's image when it recaptures it, in any playthrough ("Wilderness" is many places): a picture written over a
    minute after the record is a later one: its time is given as "retaken", and for a place it is left out (one place
    name covers many spots), while a person or an item still looks like itself, so theirs stays."""
    try:
        with ro(path) as c:
            rows = c.execute(SHOT_SQL, (start, end)).fetchall()
    except sqlite3.Error:
        return []  # older databases have no such table
    out, shots = [], shots_dir(os.path.dirname(os.path.dirname(path)))  # <SkyrimNet>/data/x.db -> <SkyrimNet>
    for i, kind, name, loc, game, t, desc, files in rows:
        image = next((os.path.basename(f.replace("\\", "/")) for f in _json(files, []) if isinstance(f, str)), "")
        taken = os.path.getmtime(os.path.join(shots, image)) if image and os.path.isfile(os.path.join(shots, image)) else None
        later = taken is not None and taken - (t or 0) > 60  # a matching picture is written within a second or two of its record
        out.append({"id": i, "kind": kind, "name": name, "location": loc, "gameTimeStr": game, "time": t, "description": desc,
                    "image": image if taken and not (later and kind == "location") else "", "retaken": taken if later else None, "db": os.path.basename(path)})
    return out


def from_dbs(sn, read, db="", day=""):
    if day:  # conversation-log view: everything from every playthrough on that real day
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
            raise ValueError("bad day")
        start = datetime.strptime(day, "%Y-%m-%d").timestamp()
        out = [m for p in glob.glob(os.path.join(sn, "data", "SkyrimNet-*.db")) for m in read(p, start, start + 86400)]
        return sorted(out, key=lambda m: m["time"])
    if db == "live":  # the database SkyrimNet is writing right now: the most recently changed one
        pts = playthroughs(sn)
        db = pts[0]["db"] if pts else ""
    if not DB_NAME.match(db or ""):
        raise ValueError("bad db name")
    return read(os.path.join(sn, "data", db))


def memories(sn, db="", day=""):
    return from_dbs(sn, memories_in, db, day)


def diary(sn, db="", day=""):
    return from_dbs(sn, diary_in, db, day)


def omnisight(sn, db="", day=""):
    return from_dbs(sn, omnisight_in, db, day)


# ---- Saves (*.ess): the ones you made. SkyrimNet logs when a save happens, not its name, so the save files are the record.
# SkyrimNet writes its playthrough id into each save's SKSE cosave (<save>.skse), which ties a save to its database exactly ----
AUTO_SAVE = re.compile(r"^(Autosave|Quicksave)\d*_", re.I)  # the game's own saves: left out
MENU_SAVE = re.compile(r"^Save(\d+)_[0-9A-F]{8}_")  # the name the game gives a save made from its menu: shown as "Save 12"


def read_ess(path):
    """A save's header (TESV_SAVEGAME: Skyrim SE, AE and VR): character, level, location, play time, and when it was saved."""
    with open(path, "rb") as f:
        b = f.read(1024)
    if b[:13] != b"TESV_SAVEGAME":
        raise ValueError("not a Skyrim save")
    o = 21  # after the magic, header size and version
    def u32():
        nonlocal o
        o += 4
        return struct.unpack_from("<I", b, o - 4)[0]
    def text():
        nonlocal o
        n = struct.unpack_from("<H", b, o)[0]
        o += 2 + n
        return b[o - n:o].decode("utf-8", "replace")
    number, player, level, location, played, race = u32(), text(), u32(), text(), text(), text()
    filetime = struct.unpack_from("<Q", b, o + 10)[0]  # after sex and two experience floats: 100 ns ticks since 1601, UTC
    name = os.path.basename(path)[:-4]
    m = MENU_SAVE.match(name)
    return {"name": f"Save {m[1]}" if m else name, "file": os.path.basename(path), "number": number, "player": player, "level": level,
            "location": location, "played": played, "race": race, "time": filetime / 1e7 - 11644473600,  # played: hours.minutes.seconds
            "db": cosave_db(path[:-4] + ".skse")}


def save_pic(path):
    """A save's screenshot as PNG bytes, or None when it is black (a save made from the console). The picture follows the
    header uncompressed: RGBA in SE and AE (version 12), RGB in LE; the compression type only covers the rest of the file."""
    with open(path, "rb") as f:
        b = f.read(1024)
        size, version = struct.unpack_from("<II", b, 13)
        w, h = struct.unpack_from("<II", b, 17 + size - (10 if version >= 12 else 8))
        px = 4 if version >= 12 else 3
        if not 0 < w <= 4096 or not 0 < h <= 4096:
            return None
        f.seek(17 + size)
        data = f.read(w * h * px)
    if len(data) < w * h * px:
        return None
    rgb = bytearray(w * h * 3)
    for c in range(3):
        rgb[c::3] = data[c::px]  # the alpha byte isn't a real alpha: dropped
    if max(rgb) < 24:
        return None
    rows = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))  # each row starts with filter 0
    chunk = lambda kind, d: struct.pack(">I", len(d)) + kind + d + struct.pack(">I", zlib.crc32(kind + d))
    return b"\x89PNG\x0d\x0a\x1a\x0a" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b"")


def save_file(sn, name):
    """The path of a save by its bare file name, from this folder's save folders only; None for anything else."""
    if os.path.basename(name) != name or not name.lower().endswith(".ess"):
        return None
    return next((p for p in (os.path.join(d, name) for d in save_dirs(sn)) if os.path.isfile(p)), None)


SN_COSAVE = (0x53504E53, 0x44494E53)  # SkyrimNet's plugin id and its "unique save id" record in an SKSE cosave


def cosave_db(path):
    """The playthrough database a save belongs to, from SkyrimNet's record in its SKSE cosave; "" without one.
    Other mods' sections are skipped, not read: a cosave can run to megabytes."""
    try:
        with open(path, "rb") as f:
            read = lambda n: struct.unpack("<%dI" % n, f.read(4 * n))
            for _ in range(read(5)[4]):  # magic and versions, then the plugin count
                uid, _records, size = read(3)
                if uid != SN_COSAVE[0]:
                    f.seek(size, 1)
                    continue
                data = f.read(size)
                o = 0
                while o + 12 <= len(data):
                    kind, _ver, n = struct.unpack_from("<3I", data, o)
                    m = re.search(rb"\d+-\d+", data[o + 12:o + 12 + n]) if kind == SN_COSAVE[1] else None
                    if m:
                        return f"SkyrimNet-{m[0].decode()}.db"
                    o += 12 + n
                return ""
    except (OSError, struct.error):
        pass
    return ""


def save_dirs(sn):
    """Where the game writes saves: each MO2 profile's own folder (<instance>/overwrite/SKSE/Plugins/SkyrimNet ->
    <instance>/profiles/*/saves), and Documents/My Games/Skyrim…/Saves for profiles without local saves and other installs."""
    parts = os.path.normpath(sn).split(os.sep)
    mo2 = len(parts) > 4 and parts[-4].lower() == "overwrite"
    # ponytail: misses a custom profiles_directory and a Documents folder moved by OneDrive; read ModOrganizer.ini / the shell folder if that bites
    return ((glob.glob(os.path.join(os.sep.join(parts[:-4]), "profiles", "*", "saves")) if mo2 else []) +
            glob.glob(os.path.join(os.path.expanduser("~"), "Documents", "My Games", "Skyrim*", "Saves")))


def all_saves(sn):
    out = {}
    for d in save_dirs(sn):
        for p in glob.glob(os.path.join(d, "*.ess")):
            if AUTO_SAVE.match(os.path.basename(p)):
                continue
            try:
                s = cached(p, read_ess)
            except (OSError, ValueError, struct.error):
                continue  # unreadable or cut short
            out.setdefault((s["file"], s["time"]), s)  # one save copied into several MO2 profiles (a copied profile keeps its saves): once
    return sorted(out.values(), key=lambda s: s["time"])


def saves(sn, db="", day=""):
    """The saves you made in a playthrough, or on a real day in any of this folder's playthroughs. A save SkyrimNet
    didn't tag (made without it, or before it was installed) belongs to none, so it never shows."""
    if day:
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
            raise ValueError("bad day")
        start = datetime.strptime(day, "%Y-%m-%d").timestamp()
        return [s for s in all_saves(sn) if start <= s["time"] < start + 86400 and s["db"] and os.path.isfile(os.path.join(sn, "data", s["db"]))]
    db = os.path.basename(from_dbs(sn, lambda p: p, db))  # from_dbs checks the name and resolves "live"
    return [s for s in all_saves(sn) if s["db"] == db]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)  # static files only from this folder

    def do_GET(self):
        if not host_ok(self.headers.get("Host", "")):
            return self.send_error(403, "Open this page as http://localhost:8791/ (or by IP address with lan: yes in config.yaml)")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/sn/"):  # SkyrimNet's read-only calls the page makes, for a page on another device
            path, api = u.path[3:], q.get("api", [""])[0]
            if not LAN or (path, api) not in SN_CALLS:
                return self.send_error(404)
            try:
                with urlopen(SN_API + path + "?" + u.query, timeout=10) as r:
                    body, kind = r.read(), r.headers.get("Content-Type", "")
            except OSError:
                return self.send_error(502, "SkyrimNet isn't answering (game closed?)")
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if kind.startswith("image/png") else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if u.path == "/local/shot":  # an OmniSight image, by file name only
            name = q.get("name", [""])[0]
            try:
                path = os.path.join(shots_dir(folder(q.get("ml", [""])[0])), name)
            except ValueError:
                return self.send_error(404)
            if not SHOT_NAME.match(name) or not os.path.isfile(path):
                return self.send_error(404)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=60")
            self.end_headers()
            return self.wfile.write(data)
        if u.path == "/local/savepic":  # a save's screenshot, by file name only
            try:
                path = save_file(folder(q.get("ml", [""])[0]), q.get("file", [""])[0])
                data = path and save_pic(path)
            except (ValueError, OSError, struct.error):
                data = None
            if not data:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=60")
            self.end_headers()
            return self.wfile.write(data)
        arg = lambda k: q.get(k, [""])[0]
        sn = lambda: folder(arg("ml"))  # ml=<modlist name>; none: the one being played
        api = {"/local/modlists": lambda: modlists(),
               "/local/playthroughs": lambda: playthroughs(sn()),
               "/local/events": lambda: events(sn(), arg("db")),
               "/local/requests": lambda: requests(sn()),
               "/local/conversation-days": lambda: conversation_days(sn()),
               "/local/conversation": lambda: conversation(sn(), arg("day")),
               "/local/memories": lambda: memories(sn(), arg("db"), arg("day")),
               "/local/omnisight": lambda: omnisight(sn(), arg("db"), arg("day")),
               "/local/diary": lambda: diary(sn(), arg("db"), arg("day")),
               "/local/saves": lambda: saves(sn(), arg("db"), arg("day"))}.get(u.path)
        if not api:
            return super().do_GET()
        try:
            body, code = json.dumps(api()).encode(), 200
        except (ValueError, OSError, sqlite3.Error) as e:
            body, code = json.dumps({"error": str(e)}).encode(), 400
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # path is unset for a malformed request line (send_error)
        if no_cache(getattr(self, "path", "")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    assert MODLISTS, "no SkyrimNet folder found: list yours under skyrimnet: in config.yaml, or pass one as an argument"
    SN = folder()
    if "--types" in sys.argv:  # every event type in every playthrough, for config.yaml's "types:" patterns
        counts = {}
        for p in glob.glob(os.path.join(SN, "data", "SkyrimNet-*.db")):
            try:
                with ro(p) as c:
                    for t, n in c.execute("select event_type, count(*) from events group by 1"):
                        counts[t] = counts.get(t, 0) + n
            except sqlite3.Error:
                pass
        for t, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"{n:7}  {t}")
        sys.exit(0)
    if "--check" in sys.argv:  # quick self-test against the real folder
        print("modlists:", ", ".join(f"{m['name']}{' (live)' if m['live'] else ''}" for m in modlists()))
        try:
            folder("../ADT")
            raise AssertionError("modlist path accepted")
        except ValueError:
            pass
        p = playthroughs(SN)
        assert p, "no playthrough databases found in " + SN
        assert events(SN, p[0]["db"]), "newest playthrough has no events"
        try:
            events(SN, "../x.db")
            raise AssertionError("path accepted")
        except ValueError:
            pass
        r = requests(SN)
        assert all(isinstance(x["request_payload"], str) for x in r)
        e = parse_conv_entry("NPC", " [Sirimunda [hunter]] to [Orgnar]: Bring out what you have.")
        assert e["data"] == {"speaker": "Sirimunda [hunter]", "listener": "Orgnar", "dialogue": "Bring out what you have."}, e
        assert parse_conv_entry("PLAYER", ": At dawn?")["type"] == "dialogue_player_text"
        assert parse_conv_entry("NPC", " [Delphine] Internal thought (unspoken): Hm.")["data"] == {"npc_name": "Delphine", "thoughts": "Hm."}
        days = conversation_days(SN)
        assert days and conversation(SN, days[0]["day"]), "no conversation log lines"
        print(f"conversation log: {sum(d['lines'] for d in days)} lines over {len(days)} days, newest {days[0]}")
        m = memories(SN, "live")
        assert all(x["kind"] != "KNOWLEDGE" for x in m)
        packed = old = 0  # knowledge-pack rows never show as memories; databases from before pack_id still show theirs
        for q in p:
            path = os.path.join(SN, "data", q["db"])
            with ro(path) as c:
                cols = [r[1] for r in c.execute("pragma table_info(memories)")]
                pack_ids = {r[0] for r in c.execute("select id from memories where pack_id is not null")} if "pack_id" in cols else set()
            shown = memories_in(path)
            assert not pack_ids & {x["id"] for x in shown}, q["db"]
            packed += bool(pack_ids)
            old += "pack_id" not in cols and bool(cols) and bool(shown)
        assert old, "no database from before pack_id kept its memories"
        print(f"knowledge packs: hidden in {packed} playthroughs; {old} older ones without pack_id still show memories")
        print(f"memories: {len(m)} in the newest playthrough, {len(memories(SN, day=days[0]['day']))} formed on {days[0]['day']}")
        dl = [x for q in p[:40] for x in diary(SN, q["db"])]
        assert all(x["content"] and isinstance(x["events"], list) for x in dl)
        print(f"diary: {len(dl)} entries in the 40 newest playthroughs")
        shots = [x for q in p[:40] for x in omnisight(SN, q["db"])]
        assert all(not x["image"] or SHOT_NAME.match(x["image"]) for x in shots)
        assert all(not (x["image"] and x["retaken"] and x["kind"] == "location") for x in shots), "a retaken place picture shown as the record's own"
        assert not SHOT_NAME.match("../x.png") and not SHOT_NAME.match("a/b.png")
        assert {a for _, a in SN_CALLS} <= {"list", "requests", "recent-events", "image"}  # /sn passes on read-only calls only
        assert config_list("a: 1\nskyrimnet:\n  # x\n  - 'D:\\MO2\\it''s'\n  - E:/x\nb: 2\n", "skyrimnet") == ["D:\\MO2\\it's", "E:/x"]
        assert config_list("skyrimnet: []\n", "skyrimnet") == []
        assert config_list("skyrimnet:\n  - 'E:\\x'  # main\n  - \"F:/y\" # b\n  - G:/z  # c\n", "skyrimnet") == ["E:\\x", "F:/y", "G:/z"]
        a, b, c = (os.path.normpath(x) for x in (r"D:\Modlists\ADT\overwrite\SKSE\Plugins\SkyrimNet",
                                                  r"E:\Games\ADT\overwrite\SKSE\Plugins\SkyrimNet", r"F:\Other\SKSE\Plugins\SkyrimNet"))
        assert modlist_names([a, b, c]) == {"ADT (Modlists)": a, "ADT (Games)": b, "Other": c}
        assert modlist_names([b, c, a]) == modlist_names([a, b, c])  # names don't depend on discovery order
        assert folder_name(r"D:\Modlists\ADT\overwrite\SKSE\Plugins\SkyrimNet") == "ADT"
        assert folder_name(r"E:\MO2\mods\SN data\SKSE\Plugins\SkyrimNet") == "SN data"
        assert folder_name(r"D:\Steam\Skyrim Special Edition\Data\SKSE\Plugins\SkyrimNet") == "Skyrim Special Edition"
        assert folder_name(r"E:\Saves\MyGame\SkyrimNet") == "MyGame"
        assert host_ok("localhost:8791") and host_ok("127.0.0.1:8791") and host_ok("[::1]:8791")
        assert no_cache("/") and no_cache("/config.yaml") and no_cache("/local/events?db=x") and not no_cache("/local/shot?name=a.png") and not no_cache("/local/savepic?file=a.ess")
        assert not host_ok("evil.example:8791") and not host_ok("") and host_ok("192.168.1.5:8791") == LAN
        assert AUTO_SAVE.match("Autosave2_A5DA4D24_0_44_Solitude_000007_20261127015759_1_1.ess") and AUTO_SAVE.match("Quicksave0_x.ess")
        assert not AUTO_SAVE.match("omega-c.ess") and not AUTO_SAVE.match("Save1_DB837310_0_42_Whiterun_000001_20261125231926_1_1.ess")
        assert MENU_SAVE.match("Save12_DB837310_0_42_Whiterun") and not MENU_SAVE.match("Saves-before-boss")
        try:
            read_ess(os.path.abspath(__file__))
            raise AssertionError("a non-save read as a save")
        except ValueError:
            pass
        for bad in ({"db": "../x.db"}, {"day": "../x"}):
            try:
                saves(SN, **bad)
                raise AssertionError(f"saves accepted {bad}")
            except ValueError:
                pass
        sv = all_saves(SN)
        assert all(x["player"] and 1 <= x["level"] < 1000 and 946684800 < x["time"] < 4102444800 for x in sv), "a save header read wrong"
        assert all(not AUTO_SAVE.match(x["file"]) for x in sv)
        assert len({(x["file"], x["time"]) for x in sv}) == len(sv), "a save listed twice (copied profiles)"
        tagged = [x for x in sv if x["db"]]
        assert all(DB_NAME.match(x["db"]) for x in tagged), "a cosave id read wrong"
        assert sum(os.path.isfile(os.path.join(SN, "data", x["db"])) for x in tagged) > len(tagged) // 2, "most tagged saves match no playthrough"
        live = saves(SN, "live")
        assert all(x["db"] == p[0]["db"] for x in live)
        assert cosave_db(os.path.abspath(__file__)) == ""
        assert all(save_file(SN, n) is None for n in ("../x.ess", r"..\x.ess", "C:x.ess", "a/b.ess", "x.txt", ""))
        pics = [save_pic(save_file(SN, x["file"])) for x in sv[-10:]]
        assert all(x is None or x[1:4] == b"PNG" for x in pics) and (not sv or any(pics)), "save pictures read wrong"
        print(f"saves: {len(sv)} you made in {len(save_dirs(SN))} folders, {len(tagged)} tagged by SkyrimNet, {len(live)} in the newest playthrough")
        print(f"omnisight: {len(shots)} screenshots in the 40 newest playthroughs, {sum(bool(x['image']) for x in shots)} with their own image on disk, {sum(bool(x['retaken']) for x in shots)} retaken later")
        print(f"ok: {len(p)} playthroughs, {len(r)} logged prompts, {sum(1 for x in r if x['response_payload'])} with responses")
        sys.exit(0)
    server = http.server.ThreadingHTTPServer(("0.0.0.0" if LAN else "127.0.0.1", PORT), Handler)  # bind first, so the page is up when the browser asks
    url = f"http://localhost:{PORT}/"
    print(f"SkyrimNet event view: {url}  (modlists: {', '.join(MODLISTS)}; playing: {live_ml()})\nClose this window to stop it.")
    if LAN:
        print(f"lan: yes in config.yaml: other devices can open http://<this PC's IP>:{PORT}/ (LAN or Tailscale address)")
    if "--no-browser" not in sys.argv:
        import webbrowser
        webbrowser.open(url)
    server.serve_forever()
