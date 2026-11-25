"""Local helper for the SkyrimNet event view: serves the page plus SkyrimNet's on-disk history.

    python serve.py [SkyrimNet folder]      then open http://localhost:8791

Without a folder it finds every modlist's ?:/Modlists/<name>/overwrite/SKSE/Plugins/SkyrimNet. The page picks one by name
(ml=<name>, never a path); without ml, the one SkyrimNet wrote to last, which is the game running now.

Reads, never writes:
  data/SkyrimNet-*.db          one SQLite database per playthrough (the events table)
  logs/openrouter_input*.log   prompts sent to the model (SkyrimNet rotates these, so only the last few files exist)
  logs/openrouter_output*.log  the model's responses, joined to prompts by request id
"""
import glob
import http.server
import ipaddress
import json
import mimetypes
import os
import re
import sqlite3
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8791
SN_API = "http://127.0.0.1:8080"  # SkyrimNet's own server, reached through /sn by pages opened from other devices


def config_lan():
    """config.yaml "lan: yes": also listen on the network (LAN, Tailscale). Off unless set."""
    try:
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            return bool(re.search(r"^lan:\s*['\"]?(yes|true|on)\b", f.read(), re.M | re.I))
    except OSError:
        return False


LAN = config_lan()


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


def find_modlists():
    """{modlist name: SkyrimNet folder}. Looked up once at start: restart to see a new modlist."""
    arg = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    found = [arg] if arg else [p for d in "CDEFGHIJ" for p in glob.glob(f"{d}:/Modlists/*/overwrite/SKSE/Plugins/SkyrimNet")]
    out = {}
    for p in found:
        p = os.path.normpath(p)
        parts = p.split(os.sep)
        name = parts[-5] if len(parts) >= 5 and parts[-4] == "overwrite" else os.path.basename(os.path.dirname(p)) or p
        if os.path.isdir(os.path.join(p, "data")):
            out.setdefault(name, p)
    return out


MODLISTS = find_modlists()


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
             where m.memory_type != 'KNOWLEDGE' and m.creation_time between ? and ?
             order by m.creation_time"""  # KNOWLEDGE rows are world-knowledge entries, not memories


def _json(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def memories_in(path, start=0, end=1e12):
    try:
        with ro(path) as c:
            rows = c.execute(MEM_SQL, (start, end)).fetchall()
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
    a subject's image when it recaptures it, so an older playthrough can show a newer picture of the same subject."""
    try:
        with ro(path) as c:
            rows = c.execute(SHOT_SQL, (start, end)).fetchall()
    except sqlite3.Error:
        return []  # older databases have no such table
    out, shots = [], shots_dir(os.path.dirname(os.path.dirname(path)))  # <SkyrimNet>/data/x.db -> <SkyrimNet>
    for i, kind, name, loc, game, t, desc, files in rows:
        image = next((os.path.basename(f.replace("\\", "/")) for f in _json(files, []) if isinstance(f, str)), "")
        out.append({"id": i, "kind": kind, "name": name, "location": loc, "gameTimeStr": game, "time": t, "description": desc,
                    "image": image if image and os.path.isfile(os.path.join(shots, image)) else "", "db": os.path.basename(path)})
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
            if not LAN or (path, api) not in (("/events", "list"), ("/logs", "requests"), ("/", "recent-events")):
                return self.send_error(404)
            try:
                with urlopen(SN_API + path + "?" + u.query, timeout=10) as r:
                    body = r.read()
            except OSError:
                return self.send_error(502, "SkyrimNet isn't answering (game closed?)")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
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
               "/local/diary": lambda: diary(sn(), arg("db"), arg("day"))}.get(u.path)
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

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    assert MODLISTS, "no SkyrimNet folder found under ?:/Modlists/*/overwrite/SKSE/Plugins/SkyrimNet; pass one as an argument"
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
        print(f"memories: {len(m)} in the newest playthrough, {len(memories(SN, day=days[0]['day']))} formed on {days[0]['day']}")
        dl = [x for q in p[:40] for x in diary(SN, q["db"])]
        assert all(x["content"] and isinstance(x["events"], list) for x in dl)
        print(f"diary: {len(dl)} entries in the 40 newest playthroughs")
        shots = [x for q in p[:40] for x in omnisight(SN, q["db"])]
        assert all(not x["image"] or SHOT_NAME.match(x["image"]) for x in shots)
        assert not SHOT_NAME.match("../x.png") and not SHOT_NAME.match("a/b.png")
        assert host_ok("localhost:8791") and host_ok("127.0.0.1:8791") and host_ok("[::1]:8791")
        assert not host_ok("evil.example:8791") and not host_ok("") and host_ok("192.168.1.5:8791") == LAN
        print(f"omnisight: {len(shots)} screenshots in the 40 newest playthroughs, {sum(bool(x['image']) for x in shots)} with an image on disk")
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
