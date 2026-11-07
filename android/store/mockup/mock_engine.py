"""A test copy of the Liner Notes engine for the Play Store screenshots: the made-up albums from albums.py,
no microphone, and no outside services (lyrics, band stories and similar bands come from albums.py).
It runs on http://127.0.0.1:8797 with its own empty settings folder.

  /shot?view=main|recognized|lyrics|info|tracklist|shelf&phone=1   the page, set up for one screenshot
  /mock/scene?name=...                                             what's playing for that screenshot
"""
import asyncio
import base64
import os
import random
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "..", "app"))
DATA = tempfile.mkdtemp(prefix="ln-mock-")
os.makedirs(os.path.join(DATA, "Liner Notes"), exist_ok=True)
os.environ["APPDATA"] = DATA
sys.path.insert(0, APP_DIR)
sys.path.insert(0, HERE)

import core  # noqa: E402
from aiohttp import web  # noqa: E402
from albums import ALBUMS, SIMILAR, by_id  # noqa: E402

PORT = 8797
COVER = "/mock/covers/{}.png"


def secs(d):
    m, s = d.split(":")
    return int(m) * 60 + int(s)


def release(a):
    """The album as Discogs would describe it, so the engine follows it like a real record."""
    tl = [{"position": f"{side}{n}" if side.isalpha() else str(n), "title": t, "duration": d, "type_": "track"}
          for side, songs in a["sides"].items() for n, (t, d) in enumerate(songs, 1)]
    fmt = {"vinyl": "Vinyl", "cd": "CD", "cassette": "Cassette"}[a["medium"]]
    return {"id": 9000000 + ALBUMS.index(a), "title": a["title"], "artists": [{"name": a["artist"]}], "year": a["year"],
            "labels": [{"name": a["label"], "catno": a["catno"]}], "genres": [a["genre"]], "styles": [a["genre"]],
            "images": [{"type": "primary", "uri": COVER.format(a["id"])}], "tracklist": tl,
            "formats": [{"name": fmt, "descriptions": ["LP", "Album"] if fmt == "Vinyl" else ["Album"]}], "country": "US",
            "extraartists": [{"name": n, "role": r} for n, r in a["credits"]],
            "companies": [{"name": a["label"], "entity_type_name": "Record Company"}]}


# ---- no outside services: answers from albums.py
async def fake_lyrics(http, artist, title, album=None, duration=None):
    for a in ALBUMS:
        if a.get("lyrics_song") == title:
            return "\n".join(w for _, w in a["lyrics"]), [[t, w] for t, w in a["lyrics"]]
    return None, None


async def fake_bio(http, artist):
    a = next((x for x in ALBUMS if x["artist"] == artist), None)
    return {"text": a["bio"], "source": "Wikipedia", "url": "https://liner-notes.app"} if a else None


async def fake_similar(http, artist, limit=16):
    links = {"spotify": "https://liner-notes.app", "bandcamp": "https://liner-notes.app"}
    return [{"name": n, "match": 1 - i / 10, "lastfm": "https://liner-notes.app", "links": links}
            for i, n in enumerate(SIMILAR[:limit])]


async def nothing(*a, **k):
    return None


async def quiet_listening(app):
    """No microphone: just look like it's listening."""
    app["mic"] = core.PhoneMic()
    app["listen_mode"] = "active"
    while True:
        await asyncio.sleep(3600)


core.fetch_lyrics = fake_lyrics
core.fetch_bio = fake_bio
core.similar_artists = fake_similar
core.shazam_page = nothing
core.enrich_track = nothing
core.original_year = nothing
core.check_for_update = nothing
core.turntable_loop = quiet_listening
core.discogs_ready = lambda: True             # the credits come from the made-up record, not Discogs
core.CFG.update(port=PORT, allow_other_devices=False, audio_device="phone", follow_album=True)
app = core.build_app(False)
app.router.add_static("/mock/covers/", os.path.join(HERE, "out", "covers"))


def fill_history(db_path):
    """A few months of listening to the made-up albums, for the shelf, history and stats."""
    random.seed(4)
    rows = []
    now = time.time()
    weights = {"dellarae": 9, "bluehour": 7, "prisms": 6, "mirrorball": 5, "pines": 5, "highway": 4, "satellites": 3, "concrete": 3}
    for a in ALBUMS:
        songs = [s for side in a["sides"].values() for s in side]
        for _ in range(weights[a["id"]] * 4):
            t, d = random.choice(songs)
            rows.append((now - random.uniform(3600, 80 * 86400), t, a["artist"], a["title"], a["year"], a["label"], a["genre"],
                         a["medium"], COVER.format(a["id"]), secs(d) * 1000, a["artist"], 9000000 + ALBUMS.index(a)))
    with sqlite3.connect(db_path) as db:
        db.executemany("INSERT INTO plays (ts,title,artist,album,year,label,genre,source,art,duration_ms,album_artist,release_id) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        saved = [("dellarae", "Keep the Porch Light On"), ("bluehour", "Night Ferry"), ("prisms", "Sugar Cube Sky"),
                 ("pines", "Woodsmoke"), ("mirrorball", "Neon Avenue")]
        for i, (aid, t) in enumerate(saved):
            a = by_id(aid)
            db.execute("INSERT OR REPLACE INTO saved (tid, ts, title, artist, album, year, label, genre, art, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (core.make_id(a["artist"], t), now - i * 86400 * 3, t, a["artist"], a["title"], a["year"], a["label"],
                        a["genre"], COVER.format(aid), a["medium"]))


def play_record(aid, song, pos, exact=True):
    """Follow the made-up record at this song, this many seconds in."""
    a = by_id(aid)
    rec = core.RecordSession(release(a))
    rec.auto = False                  # as if you'd picked it in the search (no "worked this out" note)
    idx = rec.find(song, a["artist"])
    core.show_record(app, rec, rec.set_index(idx))
    app["record"] = rec
    rec.track_started, rec.exact_start = time.time() - pos, exact
    core.set_progress(app, rec.track()["id"], pos, rec.tracks[idx]["duration"], "exact" if exact else "estimate")


async def scene(request):
    name = request.query.get("name", "main")
    app.pop("album_ask", None)
    if name in ("main", "lyrics", "info", "pc"):
        play_record("dellarae", "Keep the Porch Light On", 19.0 if name == "lyrics" else 64.0)
    elif name == "recognized":
        play_record("bluehour", "Lantern Street", 14.0)
    elif name == "tracklist":
        play_record("dellarae", "Hold On, Little Sister", 255.0)
    elif name == "shelf":
        play_record("pines", "Woodsmoke", 80.0)
    return web.json_response({"ok": True, "scene": name})


# The page as one screenshot needs it: the phone app (Plus unlocked), and the screen opened
STUB = """<script>
window.LinerNotesApp = new Proxy({
  pro: () => '{"enabled":true,"unlocked":true,"state":"idle","plans":[]}',
  appUpdate: () => '{"enabled":false,"available":"","state":"idle"}',
  mode: () => "solo", version: () => "1.1.0",
}, { get: (t, k) => k in t ? t[k] : () => "" });
</script>"""
TOUCH = """<style>
  #toolbar { gap: 6px !important; right: 10px !important; }
  #toolbar button, #toolbar #tb-coffee { width: 44px !important; height: 44px !important; opacity: .7 !important; }
  #toolbar svg { width: 54% !important; height: 54% !important; }
</style>"""
ACTIONS = {
    "main": "",
    "recognized": "",
    "lyrics": "settings.lyricsBeside = true; applySettings(); if (current) render(current, true);",
    "info": "openInfo();",
    "tracklist": "openRecords();",
    "shelf": "openLibrary('shelf');",
    "pc": "",
}


async def shot_page(request):
    view = request.query.get("view", "main")
    phone = request.query.get("phone") == "1"
    html = open(os.path.join(APP_DIR, "display.html"), encoding="utf-8").read()
    head = (STUB + TOUCH if phone else "") + "<script>try { localStorage.clear(); } catch (e) {}</script>"
    act = ACTIONS.get(view, "")
    tail = (f"<script>setTimeout(() => {{ try {{ {act} }} catch (e) {{ console.log(e) }} }}, 2200);</script>"
            # Edge saves the screenshot once the page has loaded: this image holds that back for a few real
            # seconds, so the lyrics, similar bands and covers have arrived by then
            '<img src="/mock/wait?ms=6500" alt="" style="position:absolute;width:1px;height:1px;opacity:0">')
    html = html.replace("<head>", "<head>" + head, 1).replace("</body>", tail + "</body>", 1)
    return web.Response(text=html, content_type="text/html")


async def wait(request):
    await asyncio.sleep(int(request.query.get("ms", "5000")) / 1000)
    return web.Response(body=base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"), content_type="image/gif")


app.router.add_get("/mock/scene", scene)
app.router.add_get("/mock/wait", wait)
app.router.add_get("/shot", shot_page)


async def main():
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    await asyncio.sleep(1)
    fill_history(os.path.join(DATA, "Liner Notes", "history.db"))
    print("mock engine ready", flush=True)
    while True:
        await asyncio.sleep(3600)

asyncio.run(main())
