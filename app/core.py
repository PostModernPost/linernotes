"""
Liner Notes: shows the album art and story of whatever is playing.

Shows the album art of whatever is playing:
  1. Anything playing on this PC (Spotify app, Spotify web, etc.) via Windows media info.
  2. Otherwise, whatever the microphone hears (your turntable), identified with Shazam.

Run:   python now_playing.py
Then open http://localhost:8765 on the small screen (start.bat does this for you).

Other options:
  python now_playing.py --list-devices   show microphones / inputs and their numbers
  python now_playing.py --demo           cycle fake tracks so you can see the screen
"""

import argparse
import asyncio
import collections
import itertools
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import wave

from aiohttp import web, ClientSession, ClientTimeout

import recap

APP_NAME = "Liner Notes"
# Bundled files (display.html) live next to this file, or in PyInstaller's unpack folder when installed.
HERE = os.environ.get("LINERNOTES_HERE") or getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
# Running inside the Android app (the phone works on its own, without a PC): no Windows media, no tray,
# no installer updates; the app records with the phone's mic and uploads to /api/audio.
ANDROID = os.environ.get("LINERNOTES_ENGINE") == "android"
# Settings and the log live in the user's AppData, since Program Files is read-only.
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)

def _move_old_data(new_dir):
    """Cover Art was renamed Liner Notes: carry its history and settings over, once."""
    old_dir = os.path.join(os.path.dirname(new_dir), "Cover Art")
    if os.path.isdir(old_dir) and not os.path.exists(new_dir):
        try:
            import shutil
            shutil.move(old_dir, new_dir)
        except Exception:
            pass


_move_old_data(DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------- settings
# Override any of these in config.json in DATA_DIR (same keys). The tray menu edits it for you.
CFG = {
    "port": 8765,
    "allow_other_devices": False,  # True = phones/tablets on your Wi-Fi can open the display
    "audio_device": None,          # None = Windows default mic, "phone" = the phone's mic, or part of a mic's name
    "silence_threshold": 0.003,    # below this loudness counts as silence / no music
    "gap_ratio": 0.3,              # this much quieter than the surrounding music counts as a gap between songs
    "gap_seconds": 1.0,            # a gap this long triggers a fresh identify
    "probe_every_seconds": 20,     # standby: how often to take a short sample
    "probe_seconds": 2,            # standby: length of that sample
    "clip_seconds": 12,            # length of audio sent to Shazam (12 s is the most it takes; 10 s missed many songs)
    "retry_seconds": 15,           # when nothing is identified yet, try this often
    "recheck_seconds": 15,         # a song is named but where in it isn't known yet: check again this soon
    "idle_after_seconds": 20,      # silence this long = turntable stopped
    "poll_seconds": 1.0,           # how often to check Spotify / PC media
    "pc_media": "music",           # "music": only music apps on this PC; "all": anything Windows says is playing
    "ignore_apps": [],             # e.g. ["msedge", "chrome"] to ignore browser videos
    "phone_follows_pc": True,      # phone screen turns off when this PC sleeps, back on when it wakes
    "room_medium": None,
    "mic_sensitivity": "normal",
    "hold_seconds": 60,
    "phone_off_minutes": 0,        # USB phone screen turns off after this long with nothing playing (0 = never)
    "listening_paused": False,     # the listener turned listening off (main screen or Settings) until turned back on
    "follow_album": True,          # after a song is recognized on physical media, follow its album's tracklist
    "stylus_since": None,          # when the current stylus went on (unix time); None = since the first record in Liner Notes
    "stylus_start_hours": 0,       # hours the stylus already had when tracking started
    "stylus_life_hours": 1000,     # replace it around here (most diamond styli: 800 to 1,000 hours)
    "stylus_snooze_hours": 0,      # "remind me later": don't nag again until this many hours
    "discogs_token": "",           # optional personal Discogs token (the app has its own key when built)
    "donate_url": "https://buymeacoffee.com/goodisanoun",  # shown in the info panel ("" hides it)
}
cfg_path = os.path.join(DATA_DIR, "config.json")
if os.path.exists(cfg_path):
    with open(cfg_path, encoding="utf-8") as f:
        CFG.update(json.load(f))

# Windows media info (Spotify etc.). Optional so the file still runs elsewhere.
try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winrt.windows.storage.streams import Buffer, InputStreamOptions, DataReader
    HAVE_WINRT = True
except Exception:
    HAVE_WINRT = False


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def make_id(artist, title):
    return hashlib.sha1(f"{norm(artist)}|{norm(title)}".encode()).hexdigest()[:12]


def search_links(artist, title, album=None, shazam_url=None):
    q = urllib.parse.quote(f"{artist} {title}")
    qa = urllib.parse.quote(artist or "")
    links = [
        {"name": "Spotify", "url": f"https://open.spotify.com/search/{q}"},
        {"name": "YouTube", "url": f"https://www.youtube.com/results?search_query={q}"},
        {"name": "Bandcamp", "url": f"https://bandcamp.com/search?q={qa}"},
        {"name": "Discogs", "url": f"https://www.discogs.com/search/?q={urllib.parse.quote(f'{artist} {album or title}')}&type=all"},
        {"name": "Genius", "url": f"https://genius.com/search?q={q}"},
        {"name": "Last.fm", "url": f"https://www.last.fm/music/{urllib.parse.quote((artist or '').replace(' ', '+'))}"},
    ]
    if shazam_url:
        links.insert(0, {"name": "Shazam", "url": shazam_url})
    return links


# ---------------------------------------------------------------- shared state
# ---------------------------------------------------------------- what's playing the music in the room
# Songs heard through the mic could be on vinyl, CD, cassette, the radio... Discogs says so in record
# mode; otherwise the listener's default (Settings) is used, or they're asked once per listening session.
MEDIA = {"vinyl": "Vinyl", "cd": "CD", "cassette": "Cassette", "radio": "Radio", "other": "Other"}
ROOM_SOURCES = ("turntable", "record", "manual")
ROOM = {"session": None, "last": 0.0}
SESSION_GAP = 30 * 60          # a pick lasts until the room has been quiet this long


def discogs_medium(rel):
    names = " ".join((f.get("name") or "") for f in rel.get("formats") or []).lower()
    if "vinyl" in names or "lathe" in names or "acetate" in names:
        return "vinyl"
    if "cd" in names or "sacd" in names:
        return "cd"
    if "cassette" in names:
        return "cassette"
    return None


def room_medium(app):
    rec = app.get("record") if app is not None else None
    if rec is not None and getattr(rec, "medium", None):
        return rec.medium
    if ROOM["session"] and time.time() - ROOM["last"] < SESSION_GAP:
        return ROOM["session"]          # picked for this listening session: beats the usual one
    if CFG.get("room_medium") in MEDIA:
        return CFG["room_medium"]
    return None


class State:
    def __init__(self):
        self.app = None            # set by build_app
        self.idle_at = 0.0
        self.track = None          # what the screen shows
        self.version = 0
        self.db = None             # History, set by build_app
        self.last_logged = None
        self.art = {}              # id -> (bytes, mime) for thumbnails from Windows
        self.history = {}          # id -> track, so info lookups work after a change
        self.info_cache = {}

    def _bump(self, track):
        if track and track.get("label"):
            track = {**track, "label": tidy_label(track["label"])}
        if track and track.get("source") in ROOM_SOURCES:
            if not track.get("medium"):
                track = {**track, "medium": room_medium(self.app)}
            if track.get("status") == "playing":
                ROOM["last"] = time.time()
        self.track = track
        self.version += 1
        if track and track.get("id"):
            self.history[track["id"]] = track
        if track and track.get("status") == "playing" and self.app is not None:
            try:
                wake_screens(self.app)
            except Exception as e:
                log("waking screens failed:", e)
        if track and track.get("status") == "playing" and track.get("id") != self.last_logged and self.db:
            self.last_logged = track["id"]
            try:
                self.db.add(track)
            except Exception as e:
                log("history write failed:", e)
        if track:
            log(f"[{track['source']}] {track.get('status')}: {track.get('artist')} - {track.get('title')}")
        else:
            log("idle")

    def show(self, track):
        t = self.track
        if t and t.get("id") == track.get("id") and t.get("status") == track.get("status") and t.get("source") == track.get("source"):
            return
        self._bump(track)

    def idle(self):
        if self.track is not None:
            self.idle_at = time.time()          # when the last song ended (or the room went quiet)
            self._bump(None)

    def enrich(self, tid, fields):
        """Fill in details looked up after the song went on screen (year, label, length, BPM...)."""
        if not fields:
            return
        if fields.get("label"):
            fields = {**fields, "label": tidy_label(fields["label"])}
        if tid in self.history:
            self.history[tid].update(fields)
        if self.track and self.track.get("id") == tid:
            self.track = {**self.track, **fields}
            self.version += 1


# ---------------------------------------------------------------- lookups
ITUNES = "https://itunes.apple.com"
_itunes_cache = {}


LABEL_CUTS = (r",?\s+a division of\b", r",?\s+a (?:[\w&.' -]+ )?company\b", r",?\s+an? [\w&.' -]+ (?:label|imprint)\b",
              r"\s+under (?:exclusive )?licen[cs]e\b",
              r"\.?,?\s+(?:distributed|marketed|manufactured)(?:,? (?:and |& )?(?:distributed|marketed|manufactured))* (?:by|for)\b",
              r"\s+for the (?:world|united states|us|uk)\b", r"\s+in association with\b", r"\s+/\s+", r";\s")
LABEL_SUFFIX = r"(?:,?\s+(?:LLC|L\.L\.C\.|Ltd\.?|Limited|Inc\.?|Incorporated|Corp\.?|GmbH|S\.A\.|B\.V\.|Pty\.? Ltd\.?|plc))+\.?$"
LABEL_NAMES = {"atlantic recording corporation": "Atlantic Records", "umg recordings": "UMG Recordings",
               "warner records": "Warner Records", "sony music entertainment": "Sony Music"}


def tidy_label(label):
    """'Island Records, a division of Universal Music Operations Limited' -> 'Island Records'"""
    if not label:
        return label
    s = str(label).strip()
    s = re.sub(r"^.*?[℗©]\s*(?:\d{4}\s*)?", "", s) or s          # leftover copyright marks and year
    s = re.sub(r"^\(?[PC]\)\s*(?:\d{4}\s*)?", "", s)
    s = re.sub(r"^(?:\d{4}(?:\s*[-–,/&]\s*|\s+|$))+", "", s).strip() or s   # "1974, 1976, 1988 Sony" -> "Sony"
    for cut in LABEL_CUTS:
        s = re.split(cut, s, maxsplit=1, flags=re.I)[0]
    s = re.sub(r"\b(Inc|Ltd|LLC|Corp|Limited)\.\s+\S.*$", r"\1", s)     # "Warner Records Inc. Anything else" -> "Warner Records Inc"
    s = re.sub(r"\s*\((?:[^)]*(?:music|records|recordings|entertainment|group|ltd|inc|llc)[^)]*)\)$", "", s, flags=re.I)
    s = re.sub(LABEL_SUFFIX, "", s, flags=re.I).strip(" ,.-")
    if s.isupper() and len(s) > 4:                                          # "SONY BMG MUSIC" -> "Sony BMG Music"
        small = {"of", "and", "the", "a", "an", "for", "de", "la"}
        words = s.split()
        s = " ".join(w if len(w) <= 3 and w.isalpha() and w.lower() not in small else
                     (w.lower() if i and w.lower() in small else w.capitalize()) for i, w in enumerate(words))
    return LABEL_NAMES.get(s.lower(), s) or str(label).strip()


def clean_label(copyright_text):
    """'℗ 2019 Dead Oceans' -> 'Dead Oceans'"""
    if not copyright_text:
        return None
    c = re.sub(r"^.*?[℗©]\s*(\d{4})?\s*", "", copyright_text).strip()
    return tidy_label(c) or None


def itunes_fields(res):
    """Turn an iTunes song result into our track fields."""
    art = res.get("artworkUrl100")
    return {
        "title": res.get("trackName"), "artist": res.get("artistName"), "album": res.get("collectionName"),
        "year": (res.get("releaseDate") or "")[:4] or None, "genre": res.get("primaryGenreName"),
        "art": art.replace("100x100bb", "1000x1000bb") if art else None,
        "thumb": art.replace("100x100bb", "300x300bb") if art else None,
        "duration_ms": res.get("trackTimeMillis"), "collection_id": res.get("collectionId"),
        "track_id": res.get("trackId"),
    }


async def itunes_label(http, collection_id):
    if not collection_id:
        return None
    key = ("label", collection_id)
    if key not in _itunes_cache:
        try:
            async with http.get(f"{ITUNES}/lookup?id={collection_id}") as r:
                data = await r.json(content_type=None)
            res = (data.get("results") or [{}])[0]
            _itunes_cache[key] = clean_label(res.get("copyright"))
        except Exception as e:
            log("label lookup failed:", e)
            return None
    return _itunes_cache[key]


async def itunes_lookup(http, artist, title):
    """Best iTunes catalog match for this artist + title (artist must match): art, album, year, genre, label."""
    key = (norm(artist), norm(title))
    if key in _itunes_cache:
        return _itunes_cache[key]
    info = None
    try:
        q = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 10})
        async with http.get(f"{ITUNES}/search?" + q) as r:
            data = await r.json(content_type=None)
        best = None
        for res in data.get("results", []):
            a, b = norm(artist), norm(res.get("artistName"))
            if not (a and b and (a in b or b in a)):
                continue
            if norm(res.get("trackName")) == norm(title):
                best = res
                break
            best = best or res
        if best:
            info = itunes_fields(best)
            info["label"] = await itunes_label(http, info["collection_id"])
    except Exception as e:
        log("itunes lookup failed:", e)
        return None
    _itunes_cache[key] = info
    return info


async def itunes_art(http, artist, title):
    info = await itunes_lookup(http, artist, title)
    return (info["art"], info["album"]) if info else (None, None)


HITS_WORDS = re.compile(r"\b(greatest hits|best of|the best|hits|essential|essentials|anthology|collection|gold|definitive|"
                        r"retrospective|very best|platinum|ultimate|number ones|#1'?s|singles|20th century masters|"
                        r"the masters|icon|playlist:|millennium collection|chronicles|in concert)\b", re.I)


def album_kind(name, track_count=None, album_artist=None):
    """'single', 'ep', 'compilation' (various artists), 'hits' (one artist's best-of) or 'album'."""
    n = (name or "").strip()
    if re.search(r"(\s-\s|\()single\)?$", n, re.I) or (track_count is not None and 0 < track_count <= 3):
        return "single"
    if re.search(r"(\s-\s|\()ep\)?$", n, re.I):
        return "ep"
    if (album_artist or "").strip().lower() in ("various", "various artists"):
        return "compilation"
    if HITS_WORDS.search(n):
        return "hits"
    return "album"


def bare_album(name):
    """'Rumours (Super Deluxe)' / 'Dreams - Single' -> the name to search for."""
    n = re.sub(r"\s+-\s+(single|ep)$", "", (name or "").strip(), flags=re.I)
    return clean_title(n) or n


async def itunes_song_albums(http, artist, title, limit=50):
    """Every release Apple Music has this song on: the studio album, best-ofs, compilations, the single."""
    q = urllib.parse.urlencode({"term": f"{main_artist(artist)} {clean_title(title)}", "entity": "song", "limit": limit})
    async with http.get(f"{ITUNES}/search?" + q) as r:
        data = await r.json(content_type=None)
    out, seen = [], set()
    want_t, want_a = norm(clean_title(title)), norm(main_artist(artist))
    for x in data.get("results", []):
        if norm(clean_title(x.get("trackName"))) != want_t:
            continue
        a = norm(x.get("artistName"))
        if not (want_a and a and (want_a in a or a in want_a)):
            continue
        cid = x.get("collectionId")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"collection_id": cid, "album": x.get("collectionName"), "year": (x.get("releaseDate") or "")[:4],
                    "tracks": x.get("trackCount"), "album_artist": x.get("collectionArtistName") or x.get("artistName"),
                    "art": (x.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb") or None,
                    "kind": album_kind(x.get("collectionName"), x.get("trackCount"), x.get("collectionArtistName"))})
    return out


def studio_album(cands):
    """The original album among a song's releases: a regular album, earliest first."""
    albums = [c for c in cands if c["kind"] == "album" and not re.search(r"\blive\b", c["album"] or "", re.I)]
    return min(albums, key=lambda c: c["year"] or "9999") if albums else None


async def resolve_pair(http, first, second, medium=None):
    """Two songs heard back to back: which release has both, in that order? Apple Music first (each
    song's releases, the ones they share), then Discogs. Picks the release where the second song comes
    right after the first, so a studio album and a best-of with both songs are told apart."""
    try:
        a = await itunes_song_albums(http, first["artist"], first["title"])
        b = await itunes_song_albums(http, second["artist"], second["title"])
        shared = [c for c in a if c["collection_id"] in {x["collection_id"] for x in b}]
        best = None
        for c in shared[:6]:
            rec = await itunes_album_record(http, c["collection_id"], medium)
            if not rec:
                continue
            ia, ib = rec.find(first["title"], first["artist"]), rec.find(second["title"], second["artist"])
            if ia is None or ib is None or ib <= ia:
                continue
            score = (ib - ia, 0 if c["kind"] == "album" else 1)     # right after it wins; then studio over best-of
            if best is None or score < best[0]:
                best = (score, rec, ib)
        if best:
            return await discogs_version(http, best[1], second["title"], second["artist"], medium) or (best[1], best[2])
    except Exception as e:
        log("record lookup (Apple Music) failed:", repr(e))
    if discogs_ready():
        try:
            return await discogs_find_record(http, first, second)
        except Exception as e:
            log("record lookup (Discogs) failed:", repr(e))
    return None, None


async def itunes_search(http, query, limit=15):
    q = urllib.parse.urlencode({"term": query, "entity": "song", "limit": limit})
    async with http.get(f"{ITUNES}/search?" + q) as r:
        data = await r.json(content_type=None)
    return [itunes_fields(res) for res in data.get("results", []) if res.get("trackId")]


async def itunes_track(http, track_id):
    async with http.get(f"{ITUNES}/lookup?id={int(track_id)}") as r:
        data = await r.json(content_type=None)
    res = (data.get("results") or [None])[0]
    if not res:
        return None
    info = itunes_fields(res)
    info["label"] = await itunes_label(http, info["collection_id"])
    return info


def parse_lrc(text):
    """'[01:23.45] words' lines -> [[83.45, 'words'], ...] (timed lyrics from LRCLIB)."""
    out = []
    for line in (text or "").splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        words = re.sub(r"\[[^\]]*\]", "", line).strip()
        for m, sec in stamps:
            out.append([int(m) * 60 + float(sec), words])
    out.sort(key=lambda x: x[0])
    return out


# Words streaming services tack onto titles that lyric sites don't use
TITLE_EXTRAS = re.compile(
    r"\s*(?:[-–]\s*(?:\d{4}\s+)?(?:re-?master(?:ed)?|remix|mix|live|mono|stereo|single|radio|edit|version|acoustic|demo|bonus|deluxe|from|feat|with)\b.*$"
    r"|[\(\[](?:[^\)\]]*\b(?:re-?master(?:ed)?|feat\.?|ft\.|with|live|mono|stereo|version|edit|explicit|clean|bonus|deluxe|anniversary)\b[^\)\]]*)[\)\]])",
    re.I)


def clean_title(title):
    """'Lido Shuffle - 2007 Remaster' -> 'Lido Shuffle'; 'Song (feat. X) [Live]' -> 'Song'"""
    t = title or ""
    for _ in range(3):
        t2 = TITLE_EXTRAS.sub("", t).strip()
        if t2 == t or not t2:
            break
        t = t2
    return t


def main_artist(artist):
    """'Khruangbin & Leon Bridges' -> 'Khruangbin'. Only used as a last try, since bands like Simon & Garfunkel exist."""
    return re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.|\bx\b|\band\b|\bwith\b)\s*", artist or "", maxsplit=1, flags=re.I)[0].strip()


async def _lrclib(http, params):
    async with http.get("https://lrclib.net/api/search?" + urllib.parse.urlencode(params),
                        headers={"User-Agent": "LinerNotes/1.0 +https://github.com/PostModernPost/linernotes"}) as r:
        return (await r.json(content_type=None)) or []


def _pick_lrclib(results, duration_s):
    if duration_s:      # versions of the same song (live, edits) differ in length; take the closest
        results = sorted(results, key=lambda x: abs((x.get("duration") or 0) - duration_s))
    for res in results:
        if res.get("syncedLyrics"):
            return res.get("plainLyrics") or "", parse_lrc(res["syncedLyrics"])
    for res in results:
        if res.get("plainLyrics"):
            return res["plainLyrics"], None
        if res.get("instrumental"):
            return "(Instrumental)", None
    return None


async def fetch_lyrics(http, artist, title, album, duration_s=None):
    """(plain text, timed lines or None). LRCLIB first (it has timed lyrics), trying the exact title, then
    the title without "Remaster"/"Live"/"feat." extras, then a looser search; lyrics.ovh as a last resort."""
    title, artist = (title or "").strip(), (artist or "").strip()
    if not title or not artist:
        return None, None
    short, lead = clean_title(title), main_artist(artist)
    tries = [{"artist_name": artist, "track_name": title}]
    if short.lower() != title.lower():
        tries.append({"artist_name": artist, "track_name": short})
    if lead.lower() != artist.lower():
        tries.append({"artist_name": lead, "track_name": short})
    tries.append({"q": f"{lead} {short}"})
    seen = set()
    for params in tries:
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        try:
            results = await _lrclib(http, params)
        except Exception as e:
            log("lyrics lookup failed:", e)
            break
        if "q" in params:       # a loose search can return other songs; keep the ones that match
            results = [x for x in results if norm(lead) in norm(x.get("artistName")) and
                       norm(clean_title(x.get("trackName"))) == norm(short)]
        got = _pick_lrclib(results, duration_s)
        if got:
            return got
    # lyrics.ovh: plain lyrics only, from other lyric sites
    for a in dict.fromkeys((artist, lead)):
        try:
            url = "https://api.lyrics.ovh/v1/" + urllib.parse.quote(a, safe="") + "/" + urllib.parse.quote(short, safe="")
            async with http.get(url, timeout=ClientTimeout(total=6)) as r:
                if r.status != 200:
                    continue
                d = await r.json(content_type=None)
            text = (d or {}).get("lyrics") or ""
            text = re.sub(r"^Paroles de la chanson .*? par .*?\r?\n", "", text).replace("\r\n", "\n").strip()
            text = re.sub(r"\n{3,}", "\n\n", text)
            if len(text) > 20:
                return text, None
        except Exception as e:
            log("lyrics.ovh lookup failed:", e)
    return None, None


MUSIC_WORDS = ("band", "musician", "singer", "rapper", "group", "duo", "songwriter", "music",
               "dj", "producer", "composer", "guitarist", "drummer", "pianist", "artist", "project")


WIKI_HEADERS = {"User-Agent": "LinerNotes/0.8 (https://github.com/PostModernPost/linernotes; desktop app)"}


async def _wiki_summary(http, title):
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"), safe="")
    async with http.get(url, headers=WIKI_HEADERS) as r:
        if r.status != 200:
            return None
        d = await r.json(content_type=None)
    if d.get("type") != "standard":
        return None
    blurb = (d.get("description", "") + " " + d.get("extract", "")[:300]).lower()
    if not any(w in blurb for w in MUSIC_WORDS):
        return None
    return {"text": d.get("extract"), "url": d.get("content_urls", {}).get("desktop", {}).get("page"),
            "image": (d.get("thumbnail") or {}).get("source"), "source": "Wikipedia"}


async def _lastfm_bio(http, artist):
    """Last.fm has bios for lots of small bands Wikipedia doesn't cover."""
    if not LASTFM_KEY:
        return None
    async with http.get(LASTFM_API, params={"method": "artist.getInfo", "artist": artist, "api_key": LASTFM_KEY,
                                            "autocorrect": "1", "format": "json"}) as r:
        d = await r.json(content_type=None)
    a = (d or {}).get("artist") or {}
    raw = ((a.get("bio") or {}).get("content") or (a.get("bio") or {}).get("summary") or "")
    text = re.sub(r"<a [^>]*>Read more on Last\.fm</a>\.?", "", raw)
    text = re.sub(r"User-contributed text is available under the Creative Commons.*$", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 60:
        return None
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    short = ""
    for p in paras:                       # first couple of paragraphs, about Wikipedia-summary length
        if len(short) > 500:
            break
        short = (short + "\n\n" + p).strip()
    return {"text": short, "url": a.get("url"), "image": None, "source": "Last.fm"}


async def fetch_bio(http, artist):
    """Wikipedia first, then Last.fm, then a Wikipedia search for pages titled differently."""
    if not artist:
        return None
    names = list(dict.fromkeys([artist, main_artist(artist)]))
    try:
        for name in names:
            for title in (f"{name} (band)", f"{name} (musician)", name, f"{name} (singer)", f"{name} (rapper)"):
                got = await _wiki_summary(http, title)
                if got:
                    return got
    except Exception as e:
        log("bio lookup (Wikipedia) failed:", e)
    for name in names:
        try:
            got = await _lastfm_bio(http, name)
            if got:
                return got
        except Exception as e:
            log("bio lookup (Last.fm) failed:", e)
    try:
        lead = main_artist(artist)
        async with http.get("https://en.wikipedia.org/w/api.php", headers=WIKI_HEADERS, params={
                "action": "query", "list": "search", "format": "json", "srlimit": "5",
                "srsearch": f'"{lead}" band OR musician OR singer OR rapper OR duo'}) as r:
            d = await r.json(content_type=None)
        for hit in ((d or {}).get("query") or {}).get("search") or []:
            if norm(lead) in norm(hit.get("title")):
                got = await _wiki_summary(http, hit["title"])
                if got:
                    return got
    except Exception as e:
        log("bio lookup (Wikipedia search) failed:", e)
    return None


# ---------------------------------------------------------------- PC media (Spotify etc.)
async def read_thumbnail(props):
    ref = props.thumbnail
    if ref is None:
        return None
    stream = await ref.open_read_async()
    size = int(stream.size)
    if size <= 0:
        return None
    buf = Buffer(size)
    buf = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    try:
        data = bytes(memoryview(buf))
    except Exception:
        reader = DataReader.from_buffer(buf)
        arr = bytearray(buf.length)
        reader.read_bytes(arr)
        data = bytes(arr)
    return data, (stream.content_type or "image/png")


# Apps that play music. Anything else (YouTube, Reddit or Netflix in a browser, video players) only counts
# when Settings > Apps on this PC is set to Everything, or when it clearly reports an album (Spotify's web
# player, YouTube Music and Bandcamp in a browser do; videos don't).
MUSIC_APPS = ("spotify", "applemusic", "itunes", "zunemusic", "tidal", "amazonmusic", "amazon music", "deezer",
              "qobuz", "foobar2000", "musicbee", "aimp", "winamp", "plexamp", "youtube-music", "youtubemusic",
              "cider", "soundcloud", "pandora", "audacious", "clementine", "strawberry", "mediamonkey", "dopamine",
              "tauon", "roon", "sonos", "nugs", "bandcamp")
VIDEO_APPS = ("vlc", "mpc-hc", "mpc-be", "potplayer", "netflix", "zunevideo", "disney", "primevideo", "hulu", "plex.")
BROWSERS = ("chrome", "msedge", "firefox", "308046b0af4a39cb", "opera", "brave", "vivaldi", "arc", "zen")


def is_music_session(app_id, props):
    a = (app_id or "").lower()
    if any(k in a for k in MUSIC_APPS):
        return True
    if any(k in a for k in VIDEO_APPS):
        return False
    # a browser or an app we don't know: only when it reports a real track with an album
    return bool(props and props.title and (props.artist or props.album_artist) and props.album_title)


async def current_pc_media(mgr):
    best = None
    music_only = CFG.get("pc_media", "music") != "all"
    for s in mgr.get_sessions():
        try:
            app_id = s.source_app_user_model_id or ""
            if any(x.lower() in app_id.lower() for x in CFG["ignore_apps"]):
                continue
            pb = s.get_playback_info()
            if pb is None or pb.playback_status != PlaybackStatus.PLAYING:
                continue
            props = await s.try_get_media_properties_async()
            if not props or not props.title:
                continue
            if music_only and not is_music_session(app_id, props):
                continue
            score = 2 if "spotify" in app_id.lower() else 1
            if best is None or score > best[0]:
                best = (score, s, app_id, props)
        except Exception:
            continue
    if not best:
        return None
    _, s, app_id, props = best
    return props, app_id, s


def timeline_position(session):
    """Spotify & co. tell Windows exactly where in the song they are."""
    tl = session.get_timeline_properties()

    def secs(v):
        if v is None:
            return None
        if hasattr(v, "total_seconds"):
            return v.total_seconds()
        try:
            return int(v) / 1e7            # 100-nanosecond ticks
        except Exception:
            return None
    pos, end = secs(tl.position), secs(tl.end_time)
    upd = tl.last_updated_time
    if pos is not None and upd is not None and hasattr(upd, "timestamp"):
        try:
            pos += max(0.0, time.time() - upd.timestamp())   # reported as of its last update
        except Exception:
            pass
    if end is not None and end <= 0:
        end = None
    return pos, end


def app_label(app_id):
    a = app_id.lower()
    for key, name in (("spotify", "Spotify"), ("msedge", "Edge"), ("chrome", "Chrome"), ("firefox", "Firefox"),
                      ("308046b0af4a39cb", "Firefox"), ("tidal", "Tidal"), ("amazonmusic", "Amazon Music"),
                      ("deezer", "Deezer"), ("itunes", "iTunes"), ("foobar2000", "foobar2000"), ("musicbee", "MusicBee"),
                      ("youtube-music", "YouTube Music"), ("opera", "Opera"), ("brave", "Brave"),
                      ("applemusic", "Apple Music"), ("zune", "Media Player"), ("vlc", "VLC")):
        if key in a:
            return name
    return app_id.split("!")[-1].split("\\")[-1].replace(".exe", "") or "PC"


async def pc_media_loop(app):
    st: State = app["state"]
    http = app["http"]
    app["pc_playing"] = False
    if not HAVE_WINRT:
        log("Windows media info not available (winrt packages missing) - Spotify detection off")
        return
    mgr = await MediaManager.request_async()
    stopped_since = None
    while True:
        try:
            res = await current_pc_media(mgr)
        except Exception as e:
            log("media read error:", e)
            res = None
        if res:
            stopped_since = None
            app["pc_playing"] = True
            props, app_id, session = res
            artist = props.artist or props.album_artist or ""
            title = props.title
            tid = make_id(artist, title)
            cur = st.track
            if not (cur and cur.get("id") == tid and cur.get("source") == "pc"):
                album = props.album_title or None
                art = None
                try:
                    thumb = await read_thumbnail(props)
                    if thumb:
                        st.art[tid] = thumb
                        art = f"/api/art/{tid}"
                except Exception as e:
                    log("thumbnail read failed:", e)
                info = await itunes_lookup(http, artist, title) or {}
                album = album or info.get("album")
                show_and_enrich(app, {
                    "id": tid, "title": title, "artist": artist, "album": album,
                    "art": info.get("art") or art, "source": "pc", "app": app_label(app_id), "status": "playing",
                    "year": info.get("year"), "label": info.get("label"), "genre": info.get("genre"),
                    "duration_ms": info.get("duration_ms"),
                    "links": search_links(artist, title, album),
                })
            try:
                pos, end = timeline_position(session)
                if pos is not None:
                    set_progress(app, tid, pos, end, "exact")
            except Exception:
                pass
        else:
            app["pc_playing"] = False
            if st.track and st.track.get("source") == "pc":
                stopped_since = stopped_since or time.time()
                if time.time() - stopped_since > 4:   # brief gaps happen when skipping tracks
                    st.idle()
        await asyncio.sleep(CFG["poll_seconds"])


# ---------------------------------------------------------------- song position
def set_progress(app, tid, pos_s, dur_s=None, kind="exact", at=None):
    """Where in the song we are: pos_s seconds at time `at`. kind: exact (Spotify, Shazam) or estimate."""
    app["progress"] = {"id": tid, "pos_s": pos_s, "at": at or time.time(), "dur_s": dur_s, "kind": kind}


def progress_now(app):
    t = app["state"].track
    if not t or t.get("status") != "playing":
        return None
    dur = (t.get("duration_ms") or 0) / 1000 or None
    p = app.get("progress")
    if p and p["id"] == t["id"]:
        dur = p.get("dur_s") or dur
        pos = None if p["pos_s"] is None else p["pos_s"] + (time.time() - p["at"]) * (1 if p.get("playing", True) else 0)
        if pos is not None and dur:
            pos = max(0, min(pos, dur))
        return {"pos": pos, "dur": dur, "kind": p["kind"]}
    return {"pos": None, "dur": dur, "kind": None} if dur else None


def record_progress(app, rec):
    """Record mode without a Shazam match: count from when the song started."""
    if rec and rec.idx is not None:
        t = rec.tracks[rec.idx]
        set_progress(app, make_id(rec.track()["artist"], t["title"]), 0, t["duration"], "estimate", at=rec.track_started)


# ---------------------------------------------------------------- more song details (Deezer) and tempo
DEEZER = "https://api.deezer.com"


async def deezer_lookup(http, artist, title):
    """Deezer's free catalog: BPM, length, release year, label, genre."""
    key = ("dz", norm(artist), norm(title))
    if key in _itunes_cache:
        return _itunes_cache[key]
    info = None
    try:
        q = f'artist:"{artist}" track:"{title}"'
        async with http.get(f"{DEEZER}/search", params={"q": q, "limit": 8}) as r:
            data = await r.json(content_type=None)
        best = None
        for d in data.get("data") or []:
            a, b = norm(artist), norm((d.get("artist") or {}).get("name"))
            if a and b and (a in b or b in a):
                if norm(d.get("title")) == norm(title) or norm(d.get("title_short")) == norm(title):
                    best = d
                    break
                best = best or d
        if best:
            async with http.get(f"{DEEZER}/track/{best['id']}") as r:
                tr = await r.json(content_type=None)
            info = {"bpm": round(tr["bpm"]) if tr.get("bpm") else None,
                    "duration_ms": (tr.get("duration") or 0) * 1000 or None,
                    "year": (tr.get("release_date") or "")[:4] or None,
                    "album": (tr.get("album") or {}).get("title"), "label": None, "genre": None}
            album_id = (tr.get("album") or {}).get("id")
            if album_id:
                async with http.get(f"{DEEZER}/album/{album_id}") as r:
                    al = await r.json(content_type=None)
                info["label"] = al.get("label")
                info["genre"] = ((al.get("genres") or {}).get("data") or [{}])[0].get("name")
                info["year"] = info["year"] if info["year"] and info["year"] != "0000" else (al.get("release_date") or "")[:4] or None
    except Exception as e:
        log("deezer lookup failed:", e)
        return None
    _itunes_cache[key] = info
    return info


def estimate_bpm(samples, sr):
    """Tempo from the sound itself: how often loudness jumps repeat (60 to 180 BPM)."""
    np = __import__("numpy")
    hop = max(1, int(sr / 100))                          # 100 loudness readings per second
    frames = len(samples) // hop
    if frames < 600:                                     # need at least 6 seconds
        return None
    x = samples[:frames * hop].reshape(frames, hop)
    env = np.log1p(np.sqrt((x * x).mean(axis=1)) * 1000)
    onset = np.maximum(0, np.diff(env))
    onset = onset - onset.mean()
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    lo, hi = int(6000 / 180), int(6000 / 60)
    if hi >= len(ac) or ac[0] <= 0:
        return None
    lags = np.arange(lo, hi + 1)
    seg = ac[lo:hi + 1] / ac[0]
    weight = np.exp(-0.5 * (np.log2((6000 / lags) / 120) / 0.7) ** 2)    # prefer ~120 to avoid half/double
    i = int(np.argmax(seg * weight))
    if seg[i] < 0.1:                                     # no steady beat
        return None
    # refine with the neighbours for a non-integer lag
    if 0 < i < len(seg) - 1:
        a, b, c = seg[i - 1], seg[i], seg[i + 1]
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0
    else:
        d = 0
    return round(6000 / (lags[i] + d))


# MusicBrainz knows the label each release came out on, as a plain name ("Warner Bros. Records"),
# so it's asked first. Streaming copyright lines describe today's reissue ("Warner Records Inc.
# Manufactured and Marketed by Rhino...").
MB = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "LinerNotes/0.8 ( https://github.com/PostModernPost/linernotes )", "Accept": "application/json"}
_mb_lock = None
_mb_last = 0.0


async def _mb_get(http, path, params):
    global _mb_lock, _mb_last
    if _mb_lock is None:
        _mb_lock = asyncio.Lock()
    async with _mb_lock:                       # MusicBrainz allows about one request a second
        wait = _mb_last + 1.1 - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _mb_last = time.time()
        async with http.get(MB + path, params={**params, "fmt": "json"}, headers=MB_HEADERS) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)


async def musicbrainz_label(http, artist, title, album=None):
    """The label of the song's original release (the album it's on when we know it)."""
    key = ("mb-label", norm(artist), norm(title), norm(album))
    if key in _itunes_cache:
        return _itunes_cache[key]
    label = None
    try:
        q = lambda x: (x or "").replace("\\", " ").replace('"', " ")
        d = await _mb_get(http, "/recording", {"query": f'recording:"{q(clean_title(title))}" AND artist:"{q(main_artist(artist))}"',
                                                "limit": "25"})
        releases = []
        for rec in (d or {}).get("recordings") or []:
            credit = " ".join(a.get("name", "") for a in rec.get("artist-credit") or [] if isinstance(a, dict))
            if norm(main_artist(artist)) not in norm(credit):
                continue
            for rel in rec.get("releases") or []:
                if rel.get("status", "Official") != "Official":
                    continue
                rg = rel.get("release-group") or {}
                releases.append((rel, rg))
        if releases:
            same_album = [x for x in releases if album and norm(clean_title(x[0].get("title"))) == norm(clean_title(album))]
            pool = same_album or [x for x in releases if (x[1].get("primary-type") in ("Album", "Single", "EP"))
                                  and "Compilation" not in (x[1].get("secondary-types") or [])] or releases
            pool.sort(key=lambda x: x[0].get("date") or "9999")
            for rel, _ in pool[:3]:              # the earliest releases; skip any without a label
                full = await _mb_get(http, f"/release/{rel['id']}", {"inc": "labels"})
                names = [((li or {}).get("label") or {}).get("name") for li in (full or {}).get("label-info") or []]
                names = [n for n in names if n and n.lower() not in ("[no label]", "not on label")]
                if names:
                    label = names[0]
                    break
    except Exception as e:
        log("musicbrainz lookup failed:", e)
        return None                              # don't cache: try again next time
    _itunes_cache[key] = label
    return label


async def enrich_track(app, t):
    """Look up whatever the source didn't give us, then update the screen."""
    try:
        if not t or t.get("status") != "playing" or not t.get("artist"):
            return
        http = app["http"]
        fields = {}
        want = ("year", "genre", "duration_ms", "album")
        it = await itunes_lookup(http, t["artist"], t["title"]) or {}      # cached, usually already looked up
        fields.update({k: it.get(k) for k in want if not t.get(k) and it.get(k)})
        # The label: keep one that came with the song (Shazam, Discogs). One that only came from the
        # iTunes copyright line gets replaced by MusicBrainz's, or Deezer's, when they have one.
        itunes_lab = tidy_label(it.get("label")) if it.get("label") else None
        need_label = not t.get("label") or tidy_label(t["label"]) == itunes_lab
        merged = {**t, **fields}
        dz = {}
        if not merged.get("bpm") or need_label or any(not merged.get(k) for k in ("year", "genre", "duration_ms")):
            dz = await deezer_lookup(http, t["artist"], t["title"]) or {}
            fields.update({k: dz.get(k) for k in ("bpm", "year", "genre", "duration_ms", "album")
                           if not merged.get(k) and dz.get(k)})
        if need_label:
            better = await musicbrainz_label(http, t["artist"], t["title"], merged.get("album")) or dz.get("label")
            if better or not t.get("label"):
                fields["label"] = better or itunes_lab
        if not t.get("bpm") and not fields.get("bpm") and t.get("source") in ("turntable", "record"):
            mic = app.get("mic")
            if mic is not None:
                samples = mic.buf.last_samples(10)
                if samples is not None:
                    bpm = await asyncio.get_running_loop().run_in_executor(None, estimate_bpm, samples, mic.sr)
                    if bpm:
                        fields["bpm"], fields["bpm_estimated"] = bpm, True
        app["state"].enrich(t["id"], {k: v for k, v in fields.items() if v})
    except Exception as e:
        log("details lookup failed:", repr(e))


def show_and_enrich(app, track):
    st = app["state"]
    same = st.track and st.track.get("id") == track.get("id") and st.track.get("source") == track.get("source")
    st.show(track)
    if not same:
        asyncio.get_running_loop().create_task(enrich_track(app, track))


def fill_artists(app, rec):
    """A compilation whose tracklist doesn't say who a song is by: use who Shazam said it was by, when it
    named that song before (often just before the record was picked). True if any were filled in."""
    todo = [t for t in rec.tracks if not t["artist"] or (rec.various and "original" not in t)]
    if not todo:
        return False
    try:
        known = app["state"].db.song_origins([t["title"] for t in todo])
    except Exception as e:
        log("song artists lookup failed:", repr(e))
        return False
    changed = False
    for t in todo:
        k = known.get(norm(clean_title(t["title"])))
        if not k:
            continue
        if not t["artist"]:
            t["artist"], changed = k["artist"], True
        if rec.various and k.get("album") and "original" not in t and norm(main_artist(k["artist"])) == norm(main_artist(t["artist"])):
            t["original"], changed = {"album": k["album"], "year": k["year"], "art": k.get("art")}, True
    return changed


def show_record(app, rec, track):
    """A song from the record's tracklist: show it, and count its position from when it started."""
    if fill_artists(app, rec) and rec.idx is not None and track.get("title") == rec.tracks[rec.idx]["title"]:
        track = rec.track()
    show_and_enrich(app, track)
    if rec.various and rec.idx is not None and "original" not in rec.tracks[rec.idx] and rec.tracks[rec.idx]["artist"]:
        asyncio.create_task(find_original(app, rec, rec.idx))
    record_progress(app, rec)
    save_record(rec)
    if not getattr(rec, "orig_checked", False):
        rec.orig_checked = True
        asyncio.create_task(original_year(app, rec))


async def find_original(app, rec, i):
    """A song on a compilation: the album it first came out on (Apple Music), for "Originally on" on the song page."""
    t = rec.tracks[i]
    t["original"] = None                 # looked up once
    try:
        st = studio_album(await itunes_song_albums(app["http"], t["artist"], t["title"]))
    except Exception as e:
        log("original album lookup failed:", repr(e))
        return
    if not st or norm(st["album"]) == norm(rec.title):
        return
    t["original"] = {"album": st["album"], "year": st["year"] or None, "art": st.get("art")}
    if rec.idx == i:
        cur = rec.track()
        app["state"].enrich(cur["id"], {"original": t["original"]})


async def original_year(app, rec):
    """A reissue's release year is when it was pressed (a 50th anniversary edition says 2026).
    Show the year the album first came out, from Discogs' master release."""
    mid = (rec.rel or {}).get("master_id")
    if not mid or not discogs_ready():
        return
    try:
        m = await discogs_get(app["http"], f"/masters/{int(mid)}")
        y = str(m.get("year") or "")
        if y.isdigit() and int(y) > 1800 and y != str(rec.year or ""):
            rec.orig_year = y
            log(f"{rec.title}: first released {y} (this pressing {rec.year})")
            st = app["state"]
            if st.track and st.track.get("source") == "record" and st.track.get("album") == rec.title:
                st.track["year"] = y
                st.version += 1
    except Exception as e:
        log("original year lookup failed:", repr(e))


RECORD_FILE = os.path.join(DATA_DIR, "record.json")


def save_record(rec):
    """Remember the record on the turntable and where it is, so restarting Liner Notes mid-side keeps it."""
    try:
        if rec is None:
            if os.path.exists(RECORD_FILE):
                os.remove(RECORD_FILE)
            return
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump({"rel": rec.rel, "idx": rec.idx, "track_started": rec.track_started, "auto": rec.auto,
                       "side_done": rec.side_done, "saved": time.time()}, f)
    except Exception as e:
        log("couldn't save the record:", repr(e))


def restore_record():
    """The record from before a restart, if it was playing in the last 20 minutes."""
    try:
        with open(RECORD_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - d.get("saved", 0) > 20 * 60:
            return None
        rec = RecordSession(d["rel"])
        if not rec.tracks:
            return None
        rec.auto = bool(d.get("auto"))
        if d.get("idx") is not None:
            rec.set_index(d["idx"])
            rec.track_started = d.get("track_started") or rec.track_started
            rec.side_done = bool(d.get("side_done"))    # an ended side: the next music start asks Shazam to confirm
        log(f"still on the record from before: {rec.artist} - {rec.title}")
        return rec
    except FileNotFoundError:
        return None
    except Exception as e:
        log("couldn't restore the record:", repr(e))
        return None


# ---------------------------------------------------------------- record mode (Discogs)
#
# You tell Liner Notes which record is on the turntable. It loads the tracklist from Discogs, then
# follows along: a gap between songs moves to the next track, Shazam matches correct the position,
# and songs Shazam doesn't know still get their real title and artist from the tracklist.

DISCOGS = "https://api.discogs.com"
try:
    from _secrets import DISCOGS_KEY, DISCOGS_SECRET          # written by the build from GitHub secrets
    SECRETS_ERROR = None
except Exception as _e:
    DISCOGS_KEY = DISCOGS_SECRET = ""
    SECRETS_ERROR = repr(_e)


def discogs_headers():
    h = {"User-Agent": "LinerNotes/1.0 +https://github.com/PostModernPost/linernotes"}
    token = (CFG.get("discogs_token") or "").strip()
    if token:
        h["Authorization"] = f"Discogs token={token}"
    elif DISCOGS_KEY and DISCOGS_SECRET:
        h["Authorization"] = f"Discogs key={DISCOGS_KEY}, secret={DISCOGS_SECRET}"
    return h


_owned = {"at": 0.0, "ids": set()}


def owned_release_ids():
    """Discogs releases in your collection (synced), so the copy you own wins over other pressings."""
    if time.time() - _owned["at"] > 60:
        _owned["at"] = time.time()
        try:
            con = sqlite3.connect(os.path.join(DATA_DIR, "history.db"), timeout=2)
            try:
                _owned["ids"] = {r[0] for r in con.execute("SELECT release_id FROM collection") if r[0]}
            finally:
                con.close()
        except Exception:
            pass
    return _owned["ids"]


def order_releases(results):
    """Search results in the order to try them: your own copy first, then the original pressing before
    reissues (Discogs' own order often puts this year's reissue first)."""
    owned = owned_release_ids()
    def key(r):
        y = str(r.get("year") or "")
        return (0 if r.get("id") in owned else 1, int(y) if y.isdigit() else 9999)
    return sorted(results, key=key)


def discogs_ready():
    return "Authorization" in discogs_headers()


async def discogs_get(http, path, params=None):
    async with http.get(DISCOGS + path, params=params or {}, headers=discogs_headers()) as r:
        if r.status == 401:
            raise PermissionError("Discogs didn't accept the key")
        if r.status == 429:
            raise RuntimeError("Discogs is busy, try again in a minute")
        r.raise_for_status()
        return await r.json(content_type=None)


async def discogs_search(http, q="", barcode=""):
    params = {"type": "release", "per_page": 20}
    if barcode:
        params["barcode"] = barcode
    else:
        params["q"] = q
    data = await discogs_get(http, "/database/search", params)
    return [search_row(r) for r in data.get("results", [])]


def search_row(r):
    """A Discogs search result, as the search screen lists it."""
    return {"id": r.get("id"), "title": r.get("title"), "year": r.get("year"),
            "format": ", ".join(r.get("format") or []), "label": ", ".join((r.get("label") or [])[:2]),
            "catno": r.get("catno"), "thumb": r.get("thumb") or r.get("cover_image"), "country": r.get("country")}


def lead_artist(artist):
    """'Earth, Wind & Fire feat. The Emotions' -> 'Earth, Wind & Fire' (main_artist would cut it at the comma)."""
    return re.split(r"\s+(?:feat\.?|ft\.|featuring)\s+", artist or "", maxsplit=1, flags=re.I)[0].strip()


def medium_format(medium):
    return {"vinyl": "Vinyl", "cd": "CD", "cassette": "Cassette"}.get(medium or "vinyl", "Vinyl")


async def compilation_guesses(http, songs, medium=None):
    """Compilations on Discogs with the songs just heard: the ones with the most of them first (checked
    against each tracklist, since Discogs' search also matches common words), then on this medium
    (vinyl, CD, tape), your own copy, the original pressing."""
    found, hits, per_song = {}, collections.Counter(), []
    for x in songs[:4]:
        res = await discogs_get(http, "/database/search", {"type": "release", "format": "Compilation", "per_page": 50,
                                                           "q": f"{lead_artist(x['artist'])} {clean_title(x['title'])}"})
        per_song.append(res.get("results", []))
        for r in per_song[-1]:
            found.setdefault(r["id"], r)
            hits[r["id"]] += 1
    fmt, owned = medium_format(medium), owned_release_ids()
    def key(r):
        y = str(r.get("year") or "")
        various = (r.get("title") or "").lower().startswith("various")
        return (-hits[r["id"]], 0 if various else 1, 0 if fmt in (r.get("format") or []) else 1,
                0 if r["id"] in owned else 1, int(y) if y.isdigit() else 9999)
    # take turns between the songs' searches, so each song's compilations get looked at
    lists = [sorted((found[r["id"]] for r in rs), key=key) for rs in per_song]
    order = [r for tier in itertools.zip_longest(*lists) for r in tier if r]
    order.sort(key=lambda r: -hits[r["id"]])      # stable: the ones both searches found go first
    checked, seen = [], set()
    for r in order:
        if norm(r.get("title")) in seen:          # one pressing of each
            continue
        seen.add(norm(r.get("title")))
        try:
            rec = RecordSession(await discogs_get(http, f"/releases/{r['id']}"))
        except Exception:
            continue
        on = sum(rec.find(x["title"], x["artist"]) is not None for x in songs[:4])
        if on:
            checked.append((on, r))
        if len(seen) >= 10:
            break
    checked.sort(key=lambda c: -c[0])             # stable: keeps the order above within the same count
    return [search_row(r) for _, r in checked]


async def song_album_guesses(http, artist, title, medium=None):
    """The artist's albums on Discogs with this song on them, oldest first, one pressing each: the album
    it's from, then best-ofs and anything else it's on. Singles, 12-inch mixes and videos are left out."""
    fmt, owned, want = medium_format(medium), owned_release_ids(), norm(lead_artist(artist))
    results = []
    for t in dict.fromkeys([clean_title(title), re.sub(r"[^\w\s]", "", clean_title(title))]):   # Discogs is picky about "Can't"
        for page in (1, 2):               # a hit single is on hundreds of releases: read a second page
            res = await discogs_get(http, "/database/search", {"type": "release", "artist": lead_artist(artist),
                                                               "track": t, "per_page": 100, "page": page})
            results += res.get("results", [])
            if (res.get("pagination") or {}).get("pages", 1) <= page:
                break
        if results:
            break
    best = {}
    for r in results:
        who, _, name = (r.get("title") or "").partition(" - ")
        f = r.get("format") or []
        if not name or (want and norm(clean_artist(who).rstrip("*")) != want):
            continue
        if not ({"Album", "LP", "Compilation"} & set(f)) or {"DVD", "VHS", "Unofficial Release", "Single"} & set(f):
            continue                          # the albums it's on, not the 45 or the 12-inch mix
        y = str(r.get("year") or "")
        rank = (0 if r["id"] in owned else 1, 0 if fmt in f else 1, int(y) if y.isdigit() else 9999)
        k = norm(name.split(" = ")[0])        # "Open Our Eyes = Abre Nuestros Ojos" is the same album
        if k not in best or rank < best[k][0]:
            best[k] = (rank, r)
    def year(r):
        y = str(r.get("year") or "")
        return int(y) if y.isdigit() else 9999
    return [search_row(r) for _, r in sorted(best.values(), key=lambda x: year(x[1]))[:10]]


async def studio_album_guesses(http, artist, medium=None):
    """The artist's studio albums on Discogs, oldest first: one pressing each (your own copy, or the earliest
    on this medium), leaving out best-ofs, live albums and compilations."""
    results = []
    for page in (1, 2):                   # bands with many pressings fill the first page with a few albums
        res = await discogs_get(http, "/database/search", {"type": "release", "artist": lead_artist(artist),
                                                           "format": "Album", "per_page": 100, "page": page})
        results += res.get("results", [])
        if (res.get("pagination") or {}).get("pages", 1) <= page:
            break
    fmt, owned, want = medium_format(medium), owned_release_ids(), norm(lead_artist(artist))
    best = {}
    for r in results:
        who, _, name = (r.get("title") or "").partition(" - ")
        if not name or "Compilation" in (r.get("format") or []) or album_kind(name) != "album":
            continue
        if want and norm(clean_artist(who).rstrip("*")) != want:      # Discogs marks name variations with *
            continue
        y = str(r.get("year") or "")
        rank = (0 if r["id"] in owned else 1, 0 if fmt in (r.get("format") or []) else 1, int(y) if y.isdigit() else 9999)
        k = norm(name)
        if k not in best or rank < best[k][0]:
            best[k] = (rank, r)
    def year(r):
        y = str(r.get("year") or "")
        return int(y) if y.isdigit() else 9999
    return [search_row(r) for _, r in sorted(best.values(), key=lambda x: year(x[1]))[:15]]


def clean_artist(name):
    return re.sub(r"\s*\(\d+\)$", "", name or "").strip()      # Discogs adds "(2)" to tell artists apart


def join_artists(artists):
    out = ""
    for a in artists or []:
        out += clean_artist(a.get("anv") or a.get("name"))
        j = (a.get("join") or "").strip()
        if j == ",":
            out += ", "
        elif j:
            out += f" {j} "
    return out.strip(" ,")


def parse_duration(d):
    try:
        parts = [int(x) for x in str(d).split(":")]
        return parts[0] * 60 + parts[1] if len(parts) == 2 else (parts[0] * 3600 + parts[1] * 60 + parts[2])
    except Exception:
        return None


async def discogs_find_record(http, first, second):
    """Two songs heard back to back: find the record that has both, in that order.
    Same artist -> probably their album. Different artists -> a compilation."""
    same_artist = norm(first["artist"]) == norm(second["artist"])
    base = {"type": "release", "per_page": 100}
    if same_artist:
        base["artist"] = first["artist"]
    else:
        base["format"] = "Compilation"
    ra = await discogs_get(http, "/database/search", {**base, "track": first["title"]})
    rb = await discogs_get(http, "/database/search", {**base, "track": second["title"]})
    in_b = {r.get("id") for r in rb.get("results", [])}
    both = [r for r in ra.get("results", []) if r.get("id") in in_b]
    both = order_releases(both)
    both.sort(key=lambda r: 0 if "Vinyl" in (r.get("format") or []) else 1)   # it's a turntable (stable: keeps the order above)
    for r in both[:4]:
        rec = RecordSession(await discogs_get(http, f"/releases/{r['id']}"))
        ia, ib = rec.find(first["title"], first["artist"]), rec.find(second["title"], second["artist"])
        if ia is not None and ib is not None and ib > ia:
            return rec, ib
    return None, None


async def discogs_find_album_start(http, title, artist, album):
    """The first song after the needle went down opens a side of its own album (A1, B1...):
    you're probably playing that album. Returns (record, position) or (None, None)."""
    if not album:
        return None, None
    base = {"type": "release", "artist": artist, "release_title": album, "per_page": 10}
    results = (await discogs_get(http, "/database/search", {**base, "format": "Vinyl"})).get("results", [])
    if not results:
        results = (await discogs_get(http, "/database/search", base)).get("results", [])
    for r in order_releases(results)[:3]:
        rec = RecordSession(await discogs_get(http, f"/releases/{r['id']}"))
        i = rec.find(title, artist)
        if i is not None and (i == 0 or rec.tracks[i - 1]["side"] != rec.tracks[i]["side"]):
            return rec, i
    return None, None


async def itunes_album_record(http, collection_id, medium=None):
    """The album's tracklist from Apple Music, as a record to follow (no sides: tracks in order)."""
    if not collection_id:
        return None
    async with http.get(f"{ITUNES}/lookup", params={"id": str(collection_id), "entity": "song", "limit": "200"}) as r:
        data = await r.json(content_type=None)
    items = data.get("results") or []
    album = next((x for x in items if x.get("wrapperType") == "collection"), {})
    songs = sorted((x for x in items if x.get("wrapperType") == "track"),
                   key=lambda x: (x.get("discNumber") or 1, x.get("trackNumber") or 0))
    if len(songs) < 1:
        return None
    multi = len({x.get("discNumber") or 1 for x in songs}) > 1
    seven = medium == "vinyl" and len(songs) <= 3          # a single on vinyl: a 45 with an A side and a B side
    tl = []
    for i, x in enumerate(songs):
        secs = round((x.get("trackTimeMillis") or 0) / 1000)
        tl.append({"position": ("A" if i == 0 else "B" + (str(i) if len(songs) > 2 else "")) if seven else
                   f"{x.get('discNumber') or 1}-{x.get('trackNumber')}" if multi else str(x.get("trackNumber")),
                   "title": x.get("trackName"), "duration": f"{secs // 60}:{secs % 60:02d}" if secs else "",
                   "artists": [{"name": x.get("artistName")}] if x.get("artistName") != album.get("artistName") else []})
    art = (album.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb") or None
    rel = {"id": None, "title": album.get("collectionName"), "artists": [{"name": album.get("artistName") or ""}],
           "year": (album.get("releaseDate") or "")[:4], "labels": [{"name": clean_label(album.get("copyright"))}],
           "genres": [album.get("primaryGenreName")], "images": [{"type": "primary", "uri": art}] if art else [],
           "tracklist": tl}
    rec = RecordSession(rel)
    rec.from_itunes = True
    return rec


async def fill_duration(http, rec, i):
    """The tracklist didn't say how long this song is: ask Apple Music, so its end can be judged."""
    try:
        t = rec.tracks[i]
        info = await itunes_lookup(http, t["artist"] or rec.artist, t["title"]) or {}
        secs = round((info.get("duration_ms") or 0) / 1000)
        if secs and not rec.tracks[i]["duration"]:
            rec.tracks[i]["duration"] = secs
            log(f"{t['pos']} is {secs // 60}:{secs % 60:02d} long (Apple Music)")
    except Exception as e:
        log("song length lookup failed:", repr(e))


async def discogs_version(http, rec, title, artist, medium):
    """An Apple Music tracklist has no sides. On a record or a tape, find the same album on Discogs, which
    has them (Side A, Side B). Apple and Discogs often spell the album differently ("The" or not), so look
    for it by name, then by the song. Returns (record, index of this song) or None."""
    if not getattr(rec, "from_itunes", False) or medium not in ("vinyl", "cassette") or not discogs_ready():
        return None
    fmt = {"vinyl": "Vinyl", "cassette": "Cassette"}[medium]
    bare = lambda n: norm(re.sub(r"^the\s+", "", bare_album(n or ""), flags=re.I))
    want = bare(rec.title)
    if not want:
        return None
    for how in ({"release_title": bare_album(rec.title)}, {"track": clean_title(title)}):
        try:
            results = (await discogs_get(http, "/database/search", {"type": "release", "artist": main_artist(artist),
                                                                     "format": fmt, "per_page": 15, **how})).get("results", [])
            same = [r for r in results if bare((r.get("title") or "").split(" - ", 1)[-1]) == want]   # "Artist - Album"
            for r in order_releases(same)[:3]:
                d = RecordSession(await discogs_get(http, f"/releases/{r['id']}"))
                i = d.find(title, artist)
                if i is not None:
                    return d, i
        except Exception as e:
            log("album sides lookup (Discogs) failed:", repr(e))
            return None
    return None


async def add_sides(app):
    """The record being followed came from Apple Music's tracklist (no sides): swap in the Discogs one
    with sides, at the same song, keeping where in the song it is."""
    rec = app.get("record")
    if not rec or rec.id or rec.idx is None:
        return
    rec.from_itunes = True
    t = rec.tracks[rec.idx]
    found = await discogs_version(app["http"], rec, t["title"], t["artist"] or rec.artist, room_medium(app))
    if not found or app.get("record") is not rec:
        return
    d, i = found
    d.auto, d.medium, d.tentative = rec.auto, d.medium or rec.medium, getattr(rec, "tentative", False)
    d.set_index(i)
    d.track_started, d.exact_start, d.side_done = rec.track_started, getattr(rec, "exact_start", False), rec.side_done
    app["record"] = d
    save_record(d)
    log(f"found the sides of {d.title} on Discogs: now {d.tracks[i]['pos']}")
    cur = app["state"].track
    if cur and cur.get("source") == "record":
        show_record(app, d, d.track())


async def find_album_record(http, title, artist, album, medium=None, collection_id=None):
    """A song was recognized on a record, CD or tape: find its album's tracklist (Discogs, then Apple Music)
    so the next songs can follow in order. Returns (record, index of this song) or (None, None)."""
    kind = album_kind(album)
    if discogs_ready() and album:
        fmt = {"vinyl": "Vinyl", "cd": "CD", "cassette": "Cassette"}.get(medium or "vinyl")
        base = {"type": "release", "artist": main_artist(artist), "release_title": bare_album(album), "per_page": 10}
        if kind in ("single", "ep"):
            base["format"] = "Single" if kind == "single" else "EP"      # a 45 or a 12" single, not the LP
        try:
            results = (await discogs_get(http, "/database/search", {**base, "format": fmt} if "format" not in base
                                         else base)).get("results", [])
            if not results:
                results = (await discogs_get(http, "/database/search", base)).get("results", [])
            for r in order_releases(results)[:3]:
                rec = RecordSession(await discogs_get(http, f"/releases/{r['id']}"))
                i = rec.find(title, artist)
                if i is not None:
                    return rec, i
            log(f"album lookup: Discogs has no {album} with '{title}' on it")
        except Exception as e:
            log("album lookup (Discogs) failed:", repr(e))
    try:
        cid = collection_id or (await itunes_lookup(http, artist, title) or {}).get("collection_id")
        rec = await itunes_album_record(http, cid, medium)
        if rec:
            i = rec.find(title, artist)
            if i is not None:
                return await discogs_version(http, rec, title, artist, medium) or (rec, i)
    except Exception as e:
        log("album lookup (Apple Music) failed:", repr(e))
    return None, None


PERFORMER_ROLES = re.compile(r"^(musician|performer|artist|band|vocals|lead vocals|featuring|dj)$", re.I)


def track_performer(t):
    """Some compilations on Discogs name a song's artist only in its credits, as "Musician" or "Performer"."""
    names = [{"name": c.get("anv") or c.get("name")} for c in t.get("extraartists") or []
             if PERFORMER_ROLES.match((c.get("role") or "").strip()) and (c.get("anv") or c.get("name"))]
    return join_artists([{**n, "join": "&"} for n in names[:-1]] + names[-1:]) if names else ""


class RecordSession:
    def __init__(self, rel):
        self.started = time.time()
        self.quiet_tracks = set()       # songs whose quiet passages were mistaken for the end before
        self.pending_gap = 0.0          # a gap that might be the next song, waiting for Shazam to say
        self.last_jump = None           # (when, from which track) the last time a gap moved us along
        self.id = rel.get("id")
        self.title = rel.get("title")
        self.artist = join_artists(rel.get("artists"))
        self.various = self.artist.lower() in ("various", "various artists", "")
        self.year = str(rel.get("year") or "") or None
        labels = rel.get("labels") or [{}]
        self.label = clean_artist(labels[0].get("name")) or None
        self.catno = labels[0].get("catno")
        self.genre = (rel.get("styles") or rel.get("genres") or [None])[0]
        imgs = rel.get("images") or []
        prim = next((i for i in imgs if i.get("type") == "primary"), imgs[0] if imgs else {})
        self.cover = prim.get("uri") or prim.get("uri150")
        self.tracks = []
        for t in rel.get("tracklist") or []:
            if t.get("type_", "track") != "track":
                continue                                   # side headings, index tracks
            pos = (t.get("position") or "").strip()
            m = re.match(r"^([A-Za-z]+)", pos)
            side = m.group(1).upper() if m else (pos.split("-")[0] if "-" in pos else "1")
            self.tracks.append({"pos": pos, "side": side, "title": t.get("title"),
                                "artist": join_artists(t.get("artists")) or track_performer(t)
                                          or ("" if self.various else self.artist),
                                "duration": parse_duration(t.get("duration"))})
        self.idx = None
        self.side_done = False
        self.track_started = 0.0
        self.last_active = time.time()
        self.auto = False          # True when Liner Notes worked out the record itself
        self.unconfirmed = False   # music started again after the side ended: a flip, or a new record?
        self.medium = discogs_medium(rel)
        self.rel = rel             # the full Discogs release, for the credits on the info page

    # ---- positions
    def last_on_side(self, i):
        return i >= len(self.tracks) - 1 or self.tracks[i + 1]["side"] != self.tracks[i]["side"]

    def next_side_start(self, i):
        side = self.tracks[i]["side"]
        for j in range(i + 1, len(self.tracks)):
            if self.tracks[j]["side"] != side:
                return j
        return None

    def side_info(self, i):
        side = self.tracks[i]["side"]
        on_side = [k for k, t in enumerate(self.tracks) if t["side"] == side]
        return side, on_side.index(i) + 1, len(on_side)

    def set_index(self, i):
        self.idx = max(0, min(int(i), len(self.tracks) - 1))
        self.side_done = False
        self.track_started = time.time()
        self.exact_start = False       # True once Shazam has said exactly where in the song we are
        self.last_active = time.time()
        return self.track()

    # ---- events from the listening loop
    def on_music_start(self):
        """Music started after a silence (needle down, or a flipped record)."""
        if self.idx is None:
            return self.set_index(0)
        if self.side_done:
            j = self.next_side_start(self.idx)
            self.unconfirmed = True          # the other side, or a different record: Shazam decides
            return self.set_index(j if j is not None else 0)
        # The needle went back down mid-side: where it landed isn't known, so count this song from now
        # (Shazam fixes the place), instead of a gap seconds later counting as the song's end.
        self.track_started, self.exact_start = time.time(), False
        self.last_active = time.time()
        return self.track()

    def on_stop(self):
        """The room went quiet for a while."""
        if self.idx is not None and self.last_on_side(self.idx):
            self.side_done = True

    def gap_means_next(self, now):
        """Is this gap a real song change? Quiet passages inside a song look like gaps too.
        "yes": near the end of a song whose length is known, so switch now.
        "maybe": could be either (length unknown, or a song that fooled us before): ask Shazam first,
        and switch only if it doesn't say the same song is still playing. None: too early, ignore it."""
        if self.idx is None or self.last_on_side(self.idx):
            return None
        dur = self.tracks[self.idx]["duration"] or 0
        played = now - self.track_started
        if dur:
            if played < max(30, dur - 40):
                return None
            return "maybe" if self.idx in self.quiet_tracks else "yes"
        return "maybe" if played >= 60 else None

    def overdue(self, now):
        """Songs that run straight into each other have no gap: use the listed length instead."""
        if self.idx is None or self.last_on_side(self.idx):
            return False
        dur = self.tracks[self.idx]["duration"]
        return bool(dur) and now - self.track_started > dur + (3 if getattr(self, "exact_start", False) else 10)

    def advance(self):
        return self.set_index(self.idx + 1)

    def find(self, title, artist):
        nt, na = norm(clean_title(title)), norm(main_artist(artist))
        best = None
        for i, t in enumerate(self.tracks):
            tt, ta = norm(clean_title(t["title"])), norm(t["artist"])
            if not tt:
                continue
            if tt == nt or (len(nt) > 3 and (nt in tt or tt in nt)):
                if not na or not ta or na in ta or ta in na:
                    return i
                best = best if best is not None else i
        return best

    # ---- what goes on screen
    def track(self):
        t = self.tracks[self.idx]
        side, n, count = self.side_info(self.idx)
        artist = t["artist"] or ("Various Artists" if self.various else self.artist)
        return {"id": make_id(artist, t["title"]), "title": t["title"], "artist": artist, "album": self.title,
                "album_artist": "Various Artists" if self.various else self.artist, "release_id": self.id,
                "art": self.cover, "source": "record", "medium": self.medium,
                "app": (f"Track {n} of {count}" if self.medium == "cd" or not side.isalpha() else f"Side {side} · {n} of {count}"),
                "status": "playing",
                "year": getattr(self, "orig_year", None) or self.year, "label": self.label, "genre": self.genre,
                "duration_ms": (t["duration"] or 0) * 1000 or None, "record_pos": t["pos"],
                "original": t.get("original") if self.various else None,     # a compilation's song: its own album
                "links": search_links(artist, t["title"], self.title)}

    def summary(self):
        nxt = None
        if self.side_done and self.idx is not None:
            j = self.next_side_start(self.idx)
            nxt = self.tracks[j]["side"] if j is not None else None
        return {"id": self.id, "title": self.title, "artist": self.artist, "year": self.year, "label": self.label,
                "catno": self.catno, "cover": self.cover, "tracks": self.tracks, "idx": self.idx,
                "side_done": self.side_done, "done_side": self.tracks[self.idx]["side"] if self.side_done else None,
                "next_side": nxt, "auto": self.auto, "medium": self.medium}


# ---------------------------------------------------------------- turntable (mic + Shazam)
#
# The mic is NOT on all the time. In standby it takes a short sample every
# `probe_every_seconds`. Only when that sample sounds like music does it listen
# continuously (and identify songs), until the room has been quiet for
# `idle_after_seconds`, then it goes back to standby.

NOISE = {"floor": 0.0}      # how loud the room is with no music, learned from standby samples


SENSITIVITY = {           # (minimum level, x room noise) to START listening, and x room noise to KEEP going
    "low":    (2.0, 3.0, 1.8),
    "normal": (1.0, 2.0, 1.35),
    "high":   (0.5, 1.5, 1.2),
}


def _sens():
    return SENSITIVITY.get(CFG.get("mic_sensitivity") or "normal", SENSITIVITY["normal"])


def music_cutoff():
    """Loudness that counts as music starting: the configured minimum, or well above this room's usual noise."""
    m_min, m_floor, _ = _sens()
    return max(CFG["silence_threshold"] * m_min, NOISE["floor"] * m_floor)


def keep_cutoff():
    """A lower line for music that's already playing, so quiet stretches (a soft breakdown, a fade)
    don't count as the record stopping. Still clearly above the room's own background noise."""
    m_min, _, m_keep = _sens()
    return max(CFG["silence_threshold"] * m_min * 0.5, NOISE["floor"] * m_keep)


def learn_room_noise(level):
    NOISE["floor"] = level if NOISE["floor"] == 0 else NOISE["floor"] * 0.8 + level * 0.2


class AudioBuffer:
    """The last ~14 seconds of mic audio, plus loudness bookkeeping."""

    BLOCK = 0.02          # seconds per loudness measurement (short, so gaps between key clicks show)

    def __init__(self, sr):
        import numpy as np
        self.np = np
        self.sr = int(sr)
        self.keep = int(14 * self.sr)
        self.chunks = collections.deque()
        self.samples = 0
        self.lock = threading.Lock()
        self.recent = collections.deque(maxlen=int(8 / self.BLOCK))   # loudness over the last 8 s
        self.short = collections.deque(maxlen=int(2 / self.BLOCK))    # loudness over the last 2 s
        self.reset()

    def reset(self):
        with self.lock:
            self.chunks.clear()
            self.samples = 0
            self.fed = 0          # every sample since the reset, for "record this many fresh seconds"
        self.recent.clear()
        self.short.clear()
        self.last_music = 0.0     # last moment the sound was sustained like music (typing isn't)
        self.quiet_for = 0.0
        self.sound_for = 0.0
        self.gap_seen = False
        self.in_gap = False
        self.gap_count = 0        # every gap between songs, for record mode
        self.last_sound = 0.0
        self.level = 0.0

    def feed(self, data):
        """data: float32 mono samples in -1..1"""
        np = self.np
        if data is None or len(data) == 0:
            return
        with self.lock:
            self.chunks.append(data)
            self.samples += len(data)
            self.fed += len(data)
            while self.chunks and self.samples - len(self.chunks[0]) >= self.keep:
                self.samples -= len(self.chunks.popleft())
        step = max(1, int(self.sr * self.BLOCK))
        for i in range(0, len(data), step):
            b = data[i:i + step]
            self._block(float(np.sqrt(np.mean(b * b))), len(b) / self.sr)

    def _block(self, rms, dur):
        self.level = rms
        typical = float(self.np.median(self.recent)) if len(self.recent) >= 100 else None
        self.recent.append(rms)
        self.short.append(rms)
        thr = keep_cutoff()           # music is already playing here: the lower "keep going" line
        # Music keeps the room loud nearly all the time; typing, knocks and voices come in bursts.
        if len(self.short) >= 25 and sorted(self.short)[len(self.short) // 5] >= thr:
            self.last_music = time.time()
        # A gap between songs = clearly quieter than the music around it (room noise never reads zero)
        quiet = rms < thr or (typical is not None and rms < typical * CFG["gap_ratio"])
        if quiet:
            self.quiet_for += dur
            if self.quiet_for >= CFG["gap_seconds"]:
                self.gap_seen = True
                self.sound_for = 0.0
                if not self.in_gap:
                    self.in_gap = True
                    self.gap_count += 1
        else:
            self.quiet_for = 0.0
            self.in_gap = False
            self.sound_for += dur
        if rms >= thr:
            self.last_sound = time.time()

    def last_samples(self, seconds):
        np = self.np
        with self.lock:
            if not self.chunks:
                return None
            return np.concatenate(list(self.chunks))[-int(seconds * self.sr):]

    def clip_wav(self, seconds):
        np = self.np
        with self.lock:
            if not self.chunks:
                return None
            data = np.concatenate(list(self.chunks))[-int(seconds * self.sr):]
        return to_wav(data, self.sr)

    def stats(self):
        r = sorted(self.recent)
        if not r:
            return None
        return r[len(r) // 10], r[len(r) // 2], r[-1]


# ---------------------------------------------------------------- record skips
# A skipping record loops one turn of the groove: the same sound over and over, exactly one rotation
# long (1.8 s at 33 1/3, 1.33 s at 45). Music that repeats a bar is rarely sample-for-sample identical
# in a room, and it also repeats at half a bar; a skip doesn't.
SKIP_RATE = 8000
ROTATIONS = ((33, 60 / (100 / 3)), (45, 60 / 45))


def detect_skip(data, sr):
    """data: the last ~6 s of mono audio. Returns (rpm, loop seconds, similarity) or None."""
    np = __import__("numpy")
    step = max(1, int(round(sr / SKIP_RATE)))
    n = len(data) // step * step
    rate = sr / step
    x = data[-n:].astype(np.float64).reshape(-1, step).mean(axis=1)       # ~8 kHz
    N = len(x)
    if N < rate * 4:
        return None
    X = np.fft.rfft(x - x.mean())
    fq = np.fft.rfftfreq(N, 1 / rate)
    X[(fq < 150) | (fq > 3000)] = 0                         # where the music is; room hum and hiss count less
    x = np.fft.irfft(X, N)
    if float(np.sqrt(np.mean(x * x))) < 1e-5:
        return None
    size = 1 << (2 * N - 1).bit_length()
    f = np.fft.rfft(x, size)
    ac = np.fft.irfft(f * np.conj(f), size)[:N]            # sum x[t] x[t+L]
    c2 = np.concatenate(([0.0], np.cumsum(x * x)))

    def corr(lag):
        lag = int(lag)
        if lag <= 0 or lag > N - rate * 0.5:
            return 0.0
        e1, e2 = c2[N - lag], c2[N] - c2[lag]               # energy of x[:N-lag] and of x[lag:]
        return float(ac[lag] / (np.sqrt(e1 * e2) + 1e-12))

    best = None
    for rpm, period in ROTATIONS:
        lags = np.arange(int(period * 0.97 * rate), int(period * 1.03 * rate))
        if 2 * lags[-1] > N - rate * 0.5:
            continue
        r = np.array([corr(L) for L in lags])
        i = int(np.argmax(r))
        L, sim = int(lags[i]), float(r[i])
        if sim < 0.6:
            continue
        # turn after turn the same, so twice the loop matches too (allowing for a fraction of a sample)
        if max(corr(2 * L + d) for d in (-1, 0, 1)) < 0.45:
            continue
        if max(corr(L // 2), corr(L // 3), corr(L // 4)) > 0.35:
            continue                                         # it also repeats within the turn: that's the music
        if not best or sim > best[2]:
            best = (rpm, L / rate, sim)
    return best


def to_wav(data, sr):
    np = __import__("numpy")
    # Just bring a quiet recording up to full level. (0.7.28 to 0.7.39 also filtered and compressed the
    # clip; Shazam matched nothing in that time, so the sound goes as recorded.)
    data = data - float(np.mean(data)) if len(data) else data
    peak = float(np.max(np.abs(data))) if len(data) else 0.0
    data = data * min(0.9 / (peak or 1.0), 20.0)
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return bio.getvalue()


def music_level(data, sr):
    """How loud a sample stays: the level 80% of it is above. Music stays loud; clicks, taps and
    typing are short spikes with quiet between them, so they score low."""
    np = __import__("numpy")
    step = max(1, int(sr * 0.02))
    blocks = [data[i:i + step] for i in range(0, len(data) - step + 1, step)]
    if not blocks:
        return 0.0
    return float(np.percentile([np.sqrt(np.mean(b * b)) for b in blocks], 20))


def live_feed(mic, data):
    """The mic meter: loudness of every 20 ms as it arrives, for a bar that moves like Windows' mic test."""
    np = __import__("numpy")
    step = max(1, int(mic.sr * 0.02))
    n = len(data) // step
    if not n:
        return
    rms = np.sqrt(np.mean(data[: n * step].reshape(n, step) ** 2, axis=1))
    now = time.time()
    for i, v in enumerate(rms):
        mic.live.append((now - (n - 1 - i) * 0.02, float(v)))


def live_level(mic):
    """(right now, sustained over the last second) from the meter feed, or (None, None) if it's quiet."""
    live = getattr(mic, "live", None)
    if not live:
        return None, None
    now = time.time()
    recent = [v for t, v in list(live) if now - t <= 1.0]
    if not recent or now - live[-1][0] > 0.6:
        return None, None
    instant = max(v for t, v in list(live)[-6:])           # the loudest of the last ~120 ms, like a VU peak
    sustained = sorted(recent)[len(recent) // 5]          # the same 80%-of-the-time measure the listening uses
    return instant, sustained


class PcMic:
    kind = "pc"

    def __init__(self, device):
        import sounddevice as sd
        self.sd = sd
        self.device = device
        info = sd.query_devices(device, "input")
        self.sr = int(info["default_samplerate"])
        self.name = info["name"]
        self.buf = AudioBuffer(self.sr)
        self.stream = None
        self.live = collections.deque(maxlen=200)        # 20 ms loudness blocks for the meter
        self.meter_stream = None                          # mic open just for the meter (in standby)
        self.meter_raw = collections.deque(maxlen=120)   # its last ~2.5 s of audio, so standby can still notice music

    def _meter_cb(self, indata, frames, t, status):
        x = indata[:, 0].copy()
        self.meter_raw.append(x)
        live_feed(self, x)

    def meter_start(self):
        if self.stream is None and self.meter_stream is None:
            self.meter_stream = self.sd.InputStream(device=self.device, channels=1, samplerate=self.sr, blocksize=1024,
                                                    dtype="float32", callback=self._meter_cb)
            self.meter_stream.start()

    def meter_stop(self):
        if self.meter_stream is not None:
            try:
                self.meter_stream.stop()
                self.meter_stream.close()
            finally:
                self.meter_stream = None
                self.meter_raw.clear()

    def meter_take(self, seconds):
        """The last few seconds the meter stream heard (instead of opening the mic again for a sample)."""
        np = __import__("numpy")
        if not self.meter_raw:
            return None
        return np.concatenate(list(self.meter_raw))[-int(seconds * self.sr):]

    def probe(self, seconds):
        """Blocking: record a short sample with the mic opened just for that."""
        data = self.sd.rec(int(seconds * self.sr), samplerate=self.sr, channels=1, dtype="float32",
                           device=self.device, blocking=True)
        return data[:, 0].copy()

    def _main_cb(self, indata, frames, t, status):
        x = indata[:, 0].copy()
        self.buf.feed(x)
        live_feed(self, x)

    def start(self):
        if self.stream is None:
            self.meter_stop()                             # one stream at a time
            self.stream = self.sd.InputStream(
                device=self.device, channels=1, samplerate=self.sr, blocksize=1024, dtype="float32",
                callback=self._main_cb)
            self.stream.start()

    def stop(self):
        self.meter_stop()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None


class PhoneMic:
    """The phone's display page records and uploads audio; it follows the mode the server publishes."""
    kind = "phone"
    sr = 16000
    name = "phone microphone (via the display page)"

    def __init__(self):
        self.buf = AudioBuffer(self.sr)
        self.probes = collections.deque(maxlen=4)
        self.last_upload = 0.0
        self.live = collections.deque(maxlen=200)
        self.meter_raw = collections.deque(maxlen=15)    # ~3 s of the meter's 200 ms pieces

    def start(self):
        pass      # the phone starts streaming when it sees mode "active"

    def stop(self):
        pass


def pick_device(dev):
    if dev is None or isinstance(dev, int):
        return dev
    if isinstance(dev, str) and dev.isdigit():
        return int(dev)
    import sounddevice as sd
    devs = list(enumerate(sd.query_devices()))
    for i, d in devs:                      # exact name first (what the tray menu saves)
        if d["max_input_channels"] > 0 and d["name"].lower() == dev.lower():
            return i
    for i, d in devs:
        if d["max_input_channels"] > 0 and dev.lower() in d["name"].lower():
            return i
    log(f"No input device matching '{dev}', using default")
    return None


def mic_choices():
    """Microphones and inputs on this PC: full-length names (WASAPI lists them untruncated), deduplicated."""
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        apis = sd.query_hostapis()
        wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
        names = []
        for d in devs:
            if d["max_input_channels"] > 0 and (wasapi is None or d["hostapi"] == wasapi):
                n = d["name"]
                if n not in names and "Stereo Mix" not in n and "Hands-Free" not in n:
                    names.append(n)
        return names
    except Exception as e:
        log("device list failed:", e)
        return []


SHAZAM_REPLY = {}


def shazam_client():
    """shazamio where it installs (Windows); otherwise the plain-Python copy of its recognizer (Android)."""
    try:
        if ANDROID:
            raise ImportError("no compiled shazamio core on Android")
        from shazamio import Shazam
    except ImportError:
        from shazam_lite import Shazam
    try:
        return Shazam(segment_duration_seconds=12)    # shazamio's default 10 s window missed songs that 12 s matches
    except TypeError:
        return Shazam()


async def shazam_identify(shz, wav_bytes):
    path = os.path.join(tempfile.gettempdir(), "linernotes_clip.wav")
    with open(path, "wb") as f:
        f.write(wav_bytes)
    fn = getattr(shz, "recognize", None) or shz.recognize_song
    out = await fn(path)
    out = out if isinstance(out, dict) else {}
    tr = out.get("track")
    # Remember what Shazam actually said, so "no match" can be told apart from "slow down"
    SHAZAM_REPLY["keys"] = sorted(out.keys())
    SHAZAM_REPLY["matches"] = len(out.get("matches") or [])
    SHAZAM_REPLY["retryms"] = out.get("retryms")
    if not tr:
        return None
    meta = {}
    for sec in tr.get("sections", []) or []:
        for m in sec.get("metadata", []) or []:
            if m.get("title") and m.get("text"):
                meta[m["title"]] = m["text"]
    imgs = tr.get("images") or {}
    return {
        "title": tr.get("title"), "artist": tr.get("subtitle"), "album": meta.get("Album"),
        "label": meta.get("Label"), "year": (meta.get("Released") or "")[:4] or None,
        "genre": (tr.get("genres") or {}).get("primary"),
        "art": imgs.get("coverarthq") or imgs.get("coverart"), "shazam_url": tr.get("url"),
        "offset": ((out.get("matches") or [{}])[0] or {}).get("offset"),   # where in the song the clip began
    }


UNKNOWN = {"id": "unknown", "title": "Couldn't identify this one", "artist": "", "album": None, "art": None,
           "source": "turntable", "app": "Turntable", "status": "unknown", "links": []}


async def turntable_loop(app):
    app["listen_mode"] = "off"
    try:
        if ANDROID or str(CFG["audio_device"]).lower() == "phone":
            mic = PhoneMic()
        else:
            mic = PcMic(pick_device(CFG["audio_device"]))
    except Exception as e:
        log("Microphone not available - turntable detection off:", e)
        return
    app["mic"] = mic
    log(f"Microphone: {mic.name}. Standby: {CFG['probe_seconds']}s sample every {CFG['probe_every_seconds']}s.")
    try:
        shz = shazam_client()
    except Exception as e:
        log("Song recognition unavailable (shazamio failed to load):", repr(e))
        return
    if not app.get("record"):
        app["record"] = restore_record()       # Liner Notes restarted mid-side
    if app.get("record") and not app["record"].id:
        asyncio.create_task(add_sides(app))   # followed from Apple Music's tracklist before sides were looked up
    try:
        await _turntable_inner(app, mic, shz)
    except Exception:
        import traceback
        log("turntable loop crashed:\n" + traceback.format_exc())
    finally:
        mic.stop()


def set_mode(app, mic, mode):
    if app.get("listen_mode") == mode:
        return
    app["listen_mode"] = mode
    if mode == "active":
        mic.start()
    else:
        mic.stop()
    log({"active": "music detected, listening", "standby": "mic on standby",
         "off": "PC is playing, mic off"}[mode])


async def _turntable_inner(app, mic, shz):
    st: State = app["state"]
    loop = asyncio.get_running_loop()
    L = mic.buf
    last_try = 0.0
    last_probe = 0.0
    last_level_log = 0.0
    skip = {"at": 0.0, "hits": 0, "busy": False, "logged": {}}   # record-skip watch (vinyl only)

    async def check_skip():
        """Every 1.5 s on vinyl: is the record stuck looping one turn of the groove?"""
        skip["busy"] = True
        try:
            data = L.last_samples(6)
            hit = await loop.run_in_executor(None, detect_skip, data, mic.sr) if data is not None else None
            cur = st.track
            if not hit or not cur or cur.get("status") != "playing":
                skip["hits"] = 0
                return
            skip["hits"] += 1
            key = norm(cur.get("title")) + "|" + norm(cur.get("artist"))
            if skip["hits"] >= 2 and time.time() - skip["logged"].get(key, 0) > 180:   # ~7 s of looping
                skip["logged"][key] = time.time()
                rpm, secs, sim = hit
                log(f"record skip: looping every {secs:.2f} s ({rpm} rpm) in {cur.get('artist')} - {cur.get('title')} "
                    f"(match {sim:.2f})")
                pos = progress_now(app)
                await asyncio.to_thread(st.db.add_skip, cur, (pos or {}).get("pos"))
        except Exception as e:
            log("skip check failed:", repr(e))
        finally:
            skip["busy"] = False
    misses = 0
    not_before = 0.0          # Shazam asked us to wait, or we're backing off after misses
    rec_gaps = 0              # gaps already used to move along the tracklist
    active_since = 0.0
    heard = []                # recent songs Shazam recognized on the turntable (no record loaded)
    needle_drop = False       # music just started after a silence: the next song recognized opens the side

    tried_albums = set()

    async def check_album_start(song):
        """A song was recognized on a record, CD or tape: assume the album is playing and follow its
        tracklist, so the next songs show up even if Shazam can't match them."""
        medium = room_medium(app)
        if medium == "radio" or not CFG.get("follow_album", True) or app.get("album_name"):
            return
        try:
            album = song.get("album") or ((await itunes_lookup(app["http"], song["artist"], song["title"])) or {}).get("album")
            key = (norm(song["artist"]), norm(album))
            if not album or key in tried_albums:
                return
            tried_albums.add(key)
            kind = album_kind(album)
            found, idx, tentative = None, None, False
            if kind in ("hits", "compilation"):
                # Shazam often names a best-of or a compilation even when the original album is playing.
                # Follow the original album for now; the next song shows which one it really is.
                studio = studio_album(await itunes_song_albums(app["http"], song["artist"], song["title"]))
                if studio:
                    found, idx = await find_album_record(app["http"], song["title"], song["artist"], studio["album"],
                                                         medium, studio["collection_id"])
                    tentative = bool(found)
                    if found:
                        log(f"Shazam says '{album}' ({kind}); trying the original album '{studio['album']}' first")
            if not found:
                found, idx = await find_album_record(app["http"], song["title"], song["artist"], album, medium)
                tentative = kind == "hits"
            if not found and kind not in ("hits", "compilation"):
                # Shazam's album isn't a release Discogs or Apple Music can follow (often a single named on
                # its own): follow the song's studio album instead, until the next song confirms it
                others = [c for c in await itunes_song_albums(app["http"], song["artist"], song["title"])
                          if norm(c["album"]) != norm(album)]
                studio = studio_album(others)
                if studio:
                    found, idx = await find_album_record(app["http"], song["title"], song["artist"], studio["album"],
                                                         medium, studio["collection_id"])
                    tentative = bool(found)
                    if found:
                        log(f"Shazam says '{album}', which has no tracklist to follow; trying the album '{studio['album']}'")
        except Exception as e:
            log("album lookup failed:", repr(e))
            return
        if not found or app.get("record") or app.get("album_name"):
            return
        found.auto = True
        found.tentative = tentative          # not sure yet: the next song confirms it or picks another release
        if not found.medium:
            found.medium = medium if medium in ("vinyl", "cd", "cassette") else None
        app["record"] = found
        log(f"following the album: {found.artist} - {found.title}, now {found.tracks[idx]['pos']} "
            f"({'Apple Music' if getattr(found, 'from_itunes', False) else 'Discogs'} tracklist"
            f"{', a single' if album_kind(found.title, len(found.tracks)) == 'single' else ''}{', to be confirmed' if tentative else ''})")
        cur = st.track
        p = progress_now(app)
        pos = p.get("pos") if p and p.get("kind") == "exact" else None
        if cur and cur.get("source") == "turntable" and norm(clean_title(cur.get("title"))) == norm(clean_title(song["title"])):
            show_record(app, found, found.set_index(idx))
        else:
            found.set_index(idx)
        if pos is not None:            # keep Shazam's exact place in the song, so the switch to the next one lands on time
            found.track_started, found.exact_start = time.time() - pos, True
            set_progress(app, found.track()["id"], pos, found.tracks[idx]["duration"], "exact")

    async def work_out_record(first, second):
        """Two songs in a row that aren't on the record Liner Notes was following (or with none yet):
        find the release that has both. Different artists and nothing found: probably a compilation
        Liner Notes can't find, so ask which one. Runs in the background so listening isn't held up."""
        medium = room_medium(app)
        found, idx = await resolve_pair(app["http"], first, second, medium)
        if app.get("album_name") or app.get("record"):
            return
        if found:
            found.auto, found.tentative = True, False
            if not found.medium:
                found.medium = medium if medium in ("vinyl", "cd", "cassette") else None
            app["record"] = found
            app.pop("album_ask", None)
            log(f"worked out the record from two songs: {found.artist} - {found.title} (now {found.tracks[idx]['pos']})")
            cur = st.track
            if cur and cur.get("source") == "turntable" and norm(clean_title(cur.get("title"))) == norm(clean_title(second["title"])):
                show_record(app, found, found.set_index(idx))
            else:
                found.set_index(idx)
            return
        log(f"no release has both '{first['title']}' and '{second['title']}'")
        if norm(main_artist(first["artist"])) != norm(main_artist(second["artist"])):
            if time.time() > app.get("album_ask_off_until", 0) and medium != "radio":
                ask = app.get("album_ask") or {"songs": [], "since": first.get("at") or time.time() - 600}
                for x in (first, second):
                    if not any(norm(y["title"]) == norm(x["title"]) for y in ask["songs"]):
                        ask["songs"].append({"title": x["title"], "artist": x["artist"]})
                ask["songs"] = ask["songs"][-4:]
                ask["discogs"] = discogs_ready()
                app["album_ask"] = ask
                log("songs by different artists and no release has both: asking which compilation this is")
        else:
            await check_album_start(second)     # same band, but not in order on any one release: follow this song's album
    set_mode(app, mic, "standby")

    while True:
        await asyncio.sleep(0.5)
        now = time.time()
        t = st.track
        rec = app.get("record")            # a record you told Liner Notes about (record mode), or None
        # "mine" = what the mic side put on screen: an identified song, a picked one, or one from the tracklist
        on_tt = bool(t and t.get("source") in ("turntable", "manual", "record"))
        picked = bool(t and t.get("source") == "manual")

        # Listening turned off by the listener: the mic stays closed until it's turned back on
        if CFG.get("listening_paused"):
            if app["listen_mode"] != "off":
                set_mode(app, mic, "off")
                if on_tt:
                    st.idle()
                log("listening turned off")
            for k in ("listen_now_at", "listen_now_started"):
                app.pop(k, None)
            continue

        # Spotify etc. on the PC wins; no need for the mic at all
        if app.get("pc_playing"):
            set_mode(app, mic, "off")
            for k in ("listen_now_at", "listen_now_started"):
                app.pop(k, None)
            continue

        if rec and app["listen_mode"] != "active" and now - rec.last_active > 3 * 3600:
            app["record"] = rec = None       # forget a record that hasn't played in hours
            save_record(None)
        if (app.get("album_name") or app.get("album_ask")) and now - max(ROOM["last"], (app.get("album_name") or {}).get("since", 0)) > 20 * 60:
            app.pop("album_name", None)      # a compilation you named lasts while it plays, not into the next record
            app.pop("album_ask", None)

        mode = app["listen_mode"]
        if mode == "off":
            set_mode(app, mic, "standby")
            last_probe = 0.0          # check right away once the PC stops
            mode = "standby"

        # ---------------- standby: short sample every N seconds
        if mode == "standby":
            if on_tt:
                st.idle()
            samples = []
            if mic.kind == "pc":
                if now - last_probe >= CFG["probe_every_seconds"] and mic.meter_stream is None:
                    last_probe = now
                    try:
                        samples.append(await loop.run_in_executor(None, mic.probe, CFG["probe_seconds"]))
                    except Exception as e:
                        log("mic sample failed:", e)
            else:
                while mic.probes:
                    samples.append(mic.probes.popleft())
                if mic.meter_raw and now - last_probe >= 1.0:     # the meter is streaming: check its last second or so
                    last_probe = now
                    import numpy as np
                    x = np.concatenate(list(mic.meter_raw))[-int(1.5 * mic.sr):]
                    if len(x) >= mic.sr:
                        samples.append(x)
                    if app.get("meter_until", 0) <= now:
                        mic.meter_raw.clear()
            if app.get("listen_now_at") and now - app["listen_now_at"] < 60 and not app.get("listen_now_started"):
                app["listen_now_started"] = now          # Listen now: record right away, whatever the level
                L.reset()
                app["listen_now_mark"] = 0
                last_try = 0.0
                misses = 0
                set_mode(app, mic, "active")
                active_since = now
                continue
            if mic.kind == "pc":
                if app.get("meter_until", 0) > now:
                    # The mic meter is open: keep the mic running so the bar moves instantly, and check
                    # what it hears for music every second instead of taking separate samples
                    try:
                        mic.meter_start()
                    except Exception as e:
                        log("mic meter failed:", e)
                    if now - last_probe >= 1.0 and mic.meter_stream is not None:
                        last_probe = now - CFG["probe_every_seconds"] + 1.0
                        x = mic.meter_take(1.5)
                        if x is not None and len(x) >= mic.sr:
                            samples.append(x)
                elif mic.meter_stream is not None:
                    mic.meter_stop()
            for data in samples:
                level = music_level(data, mic.sr)
                app["meter"] = {"level": level, "at": time.time()}
                if level < music_cutoff():
                    learn_room_noise(level)
                    if now - last_level_log >= 60:      # so a mic that's too quiet shows in the log
                        last_level_log = now
                        log(f"standby: sample level {level:.4f}, below the music cutoff {music_cutoff():.4f} "
                            f"(room {NOISE['floor']:.4f})")
                    continue
                if level >= music_cutoff():
                    L.reset()
                    L.feed(data)
                    last_try = 0.0
                    misses = 0
                    last_level_log = now
                    log(f"sample level {level:.4f} (music cutoff {music_cutoff():.4f})")
                    set_mode(app, mic, "active")
                    active_since = now
                    needle_drop = True
                    heard = []                # a new record (or side): the songs before it don't pair with this one
                    app.pop("album_ask_off_until", None)    # and "don't ask" was about the last record, not this one
                    if rec and rec.tracks:
                        rec_gaps = L.gap_count
                        show_record(app, rec, rec.on_music_start())
                    break
            continue

        # ---------------- active: continuous listening while there's music
        if mic.kind == "phone":
            while mic.probes:                 # a sample that crossed with the mode change
                L.feed(mic.probes.popleft())

        if (now - skip["at"] >= 1.5 and not skip["busy"] and on_tt
                and ((rec.medium if rec else None) or (t or {}).get("medium") or room_medium(app)) == "vinyl"):
            skip["at"] = now
            asyncio.create_task(check_skip())

        if now - last_level_log >= 15:
            last_level_log = now
            s = L.stats()
            if s:
                log(f"mic level: quiet {s[0]:.4f}, typical {s[1]:.4f}, loud {s[2]:.4f} "
                    f"(music cutoff {music_cutoff():.4f}, room {NOISE['floor']:.4f})")

        if len(L.short) >= 25:
            app["meter"] = {"level": sorted(L.short)[len(L.short) // 5], "at": now}
        if app.get("listen_now_at") and not app.get("listen_now_started"):
            app["listen_now_started"] = now           # already listening: record a fresh clip, then check
            app["listen_now_mark"] = L.fed
        listen_now = bool(app.get("listen_now_started"))
        # A song Shazam found is still within its length: wait much longer before deciding the record stopped
        quiet_limit = CFG["hold_seconds"] if song_still_playing(app, t) else CFG["idle_after_seconds"]
        if listen_now:
            quiet_limit = max(quiet_limit, 30)
        if now - max(L.last_music, active_since) > quiet_limit:
            if rec:
                rec.on_stop()
                if rec.side_done:
                    log(f"side {rec.tracks[rec.idx]['side']} finished")
                    save_record(rec)         # so a restart (an update) still knows the side ended
            if on_tt:
                st.idle()
            set_mode(app, mic, "standby")
            last_probe = now
            continue

        # Record mode: follow the tracklist between Shazam checks
        if rec and rec.tracks and rec.idx is not None:
            if rec.pending_gap and now - rec.pending_gap > 45:
                rec.pending_gap = 0.0            # Shazam never got to answer: drop the question
            if L.gap_count != rec_gaps and L.sound_for >= 1.0:
                rec_gaps = L.gap_count
                verdict = rec.gap_means_next(now)
                if verdict == "yes":
                    rec.last_jump = (now, rec.idx)
                    show_record(app, rec, rec.advance())
                    log(f"gap: next track {rec.tracks[rec.idx]['pos']}")
                elif verdict == "maybe" and not rec.pending_gap:
                    rec.pending_gap = now            # ask Shazam before switching (handled after the next match)
                    log(f"gap in {rec.tracks[rec.idx]['pos']}: checking with Shazam before moving on")
            elif rec.overdue(now):
                rec.pending_gap = 0.0
                rec.last_jump = (now, rec.idx)
                show_record(app, rec, rec.advance())
                log(f"track length passed: next track {rec.tracks[rec.idx]['pos']}")

        if listen_now:
            # Listen now: send a full clip recorded after the click (loud enough or not), and not
            # before Shazam's own "try again in 12 s" is up, or it answers no match without listening
            if L.fed - app.get("listen_now_mark", 0) < CFG["clip_seconds"] * mic.sr or now < not_before:
                continue
            app["listen_now_state"] = "asking"
        elif L.sound_for < CFG["clip_seconds"]:
            continue          # wait for a full clip of music since the start or the last gap

        identified = on_tt and t.get("status") == "playing"
        # Settled: Shazam said where in a song of known length the needle is (and the record being followed is
        # confirmed). Then the song lengths and the tracklist follow along, and Shazam isn't asked again until
        # the song should be over (or there's a gap, or the music stops for a while).
        if rec and rec.idx is not None:
            settled = (getattr(rec, "exact_start", False) and bool(rec.tracks[rec.idx]["duration"])
                       and not rec.unconfirmed and not getattr(rec, "tentative", False) and not rec.pending_gap)
        else:
            pr = progress_now(app) if identified and t.get("source") == "turntable" else None
            settled = bool(pr and pr.get("kind") == "exact" and pr.get("dur") and pr.get("pos") is not None
                           and pr["pos"] < pr["dur"] - 2)
        # Nothing matched: try every 15 s for the first minute of a song, then wait longer each time
        # (30 s, 60 s, then every 90 s) so a song Shazam doesn't know isn't asked about all side long.
        # A gap (new song) starts this over.
        retry_wait = CFG["retry_seconds"] if misses <= 4 else min(CFG["retry_seconds"] * (2 ** (misses - 4)), 90)
        due = (L.gap_seen or app.pop("force_check", False) or listen_now
               or (not identified and now - last_try >= retry_wait)
               or (misses > 0 and now - last_try >= retry_wait)
               or (not settled and now - last_try >= CFG["recheck_seconds"]
                   # moved on to the next song by its length (no gap): wait for a clip that's all the new song
                   and not (rec and rec.idx is not None and now - rec.track_started < CFG["clip_seconds"] + 2)))
        app["next_try"] = (None if settled and not misses else
                           max(last_try + (CFG["recheck_seconds"] if identified and not misses else retry_wait), not_before))
        if not due or now < not_before:
            continue
        if listen_now:
            app.pop("listen_now_started", None)
            app.pop("listen_now_at", None)
        gap = L.gap_seen
        L.gap_seen = False
        if gap:
            misses = 0
        last_try = now
        wav = L.clip_wav(CFG["clip_seconds"])
        if not wav:
            continue
        app["last_clip"] = (wav, time.time())          # Settings can play back what Shazam heard
        log(f"identifying ({'new song' if gap else 'check'}, level {L.level:.4f})")
        try:
            res = await shazam_identify(shz, wav)
        except Exception as e:
            log("shazam error:", repr(e))
            res = None
        if listen_now:
            app["listen_now_state"] = "found" if res and res.get("title") else "no_match"
            app["listen_now_done"] = time.time()

        on_record = rec.find(res["title"], res["artist"]) if (rec and res and res.get("title")) else None
        if rec and not rec.auto and res and res.get("title") and on_record is None and rec.idx is not None:
            # A record you loaded yourself (a live album, a compilation): Shazam often names the studio
            # version of a song on it, or a song that just sounds alike. Stay on the record unless it
            # names two different songs that aren't on it in a row (then a different record is playing).
            off = getattr(rec, "off_record", None)
            if not off or norm(off) == norm(res["title"]):
                rec.off_record = res["title"]
                log(f"Shazam says '{res['title']}', which isn't on {rec.title}: staying on the record")
                continue
            log(f"Shazam named two songs that aren't on {rec.title}: a different record is playing")
            app["record"] = rec = None
            save_record(None)
        elif rec and on_record is not None:
            rec.off_record = None
        prev_song = None            # what was playing before, when a guessed record turns out wrong
        if rec and rec.auto and res and res.get("title") and rec.idx is not None:
            nxt = rec.next_side_start(rec.idx)
            # Where the next song should be: this one, the next (or the one after, if a change was missed),
            # or the start of the next side. Anywhere else on a guessed release means a different release
            # with the same songs, like a best-of instead of the album.
            ok = {rec.idx, rec.idx + 1, rec.idx + 2, nxt} if not getattr(rec, "tentative", False) else {rec.idx, rec.idx + 1, nxt}
            if rec.last_jump and now - rec.last_jump[0] < 180:
                ok.add(rec.last_jump[1])       # we moved on too early and the last song is still playing
            jumped = on_record is not None and on_record not in ok
            if on_record is None or jumped:
                # Not on the guessed release, or not where it should be (a best-of, not the album): guess again
                prev_song = {"title": rec.tracks[rec.idx]["title"], "artist": rec.track()["artist"]}
                log(f"'{res['title']}' {'is not next' if on_record is not None else 'is not'} on {rec.title}: dropping that guess")
                app["record"] = rec = None
                save_record(None)
                on_record = None
            elif getattr(rec, "tentative", False) and on_record != rec.idx:
                rec.tentative = False
                log(f"confirmed: it's {rec.title}")
        if rec and rec.pending_gap and (on_record is not None or not (res and res.get("title"))):
            if on_record is None and rec.idx is not None and not rec.last_on_side(rec.idx):
                log("no match after the gap: next track")      # a new song Shazam doesn't know
                rec.last_jump = (now, rec.idx)
                show_record(app, rec, rec.advance())
            elif on_record == rec.idx:
                log("same song after the gap: staying on it")
            rec.pending_gap = 0.0
        if on_record is not None:
            misses = 0
            rec.unconfirmed, rec.unconfirmed_misses = False, 0
            if not rec.tracks[on_record]["artist"] and res.get("artist"):
                rec.tracks[on_record]["artist"] = res["artist"]          # the tracklist didn't say who it's by
                if rec.idx == on_record:
                    show_record(app, rec, rec.track())
            if rec.idx != on_record:
                if rec.last_jump and rec.last_jump[1] == on_record and now - rec.last_jump[0] < 180:
                    rec.quiet_tracks.add(on_record)      # its quiet bits fooled us: ask Shazam before leaving it again
                    log(f"moved on from {rec.tracks[on_record]['pos']} too early: its quiet passages won't count as the end again")
                log(f"Shazam says track {rec.tracks[on_record]['pos']}: moving there")
                show_record(app, rec, rec.set_index(on_record))
            if not rec.tracks[on_record]["duration"]:
                asyncio.create_task(fill_duration(app["http"], rec, on_record))
            if res.get("offset") is not None:
                pos = res["offset"] + CFG["clip_seconds"]
                set_progress(app, rec.track()["id"], pos, rec.tracks[rec.idx]["duration"], "exact", at=last_try)
                rec.track_started, rec.exact_start = last_try - pos, True
            continue
        if res and res.get("title"):
            misses = 0
            tid = make_id(res["artist"], res["title"])
            song = {"title": res["title"], "artist": res["artist"], "at": now}
            named = app.get("album_name")
            if not rec and not named and room_medium(app) != "radio":
                prev = prev_song or (heard[-1] if heard and now - heard[-1]["at"] < 20 * 60 else None)
                if prev and norm(prev["title"]) != norm(res["title"]):
                    # Two different songs in a row: find the release with both (an album, a best-of, a compilation)
                    asyncio.create_task(work_out_record(prev, song))
                elif not prev:
                    # The first song: assume its album is playing and follow the tracklist
                    asyncio.create_task(check_album_start(res))
            if not heard or norm(heard[-1]["title"]) != norm(res["title"]):
                heard = (heard + [song])[-6:]
            needle_drop = False
            if not (t and t.get("id") == tid):
                info = await itunes_lookup(app["http"], res["artist"], res["title"]) or {}
                album = res["album"] or info.get("album")
                named = app.get("album_name")          # a compilation you named: its songs go under that name
                show_and_enrich(app, {
                    "id": tid, "title": res["title"], "artist": res["artist"],
                    "album": named["title"] if named else album, "album_artist": named["artist"] if named else None,
                    "art": res["art"] or info.get("art"), "source": "turntable", "app": "Turntable",
                    "status": "playing", "year": res["year"] or info.get("year"),
                    "label": res["label"] or info.get("label"), "genre": res["genre"] or info.get("genre"),
                    "duration_ms": info.get("duration_ms"),
                    "links": search_links(res["artist"], res["title"], album, res["shazam_url"]),
                })
            if res.get("offset") is not None:
                cur = st.track or {}
                set_progress(app, tid, res["offset"] + CFG["clip_seconds"],
                             (cur.get("duration_ms") or 0) / 1000 or None, "exact", at=last_try)
        else:
            misses += 1
            log(f"no match (Shazam reply: {SHAZAM_REPLY})")
            if SHAZAM_REPLY.get("retryms"):
                not_before = now + min(float(SHAZAM_REPLY["retryms"]) / 1000.0, 600)
                log(f"Shazam asked to wait {SHAZAM_REPLY['retryms'] / 1000:.0f} s")
            if rec and rec.unconfirmed:
                rec.unconfirmed_misses = getattr(rec, "unconfirmed_misses", 0) + 1     # gaps don't reset this one
            if rec and rec.auto and rec.unconfirmed and rec.unconfirmed_misses >= 3:
                # A record Liner Notes guessed, and the music that started after the side ended isn't on it
                # as far as Shazam can tell: probably a new record. Don't keep showing the old one's songs.
                log(f"Shazam can't place this on {rec.title}: probably a different record, dropping it")
                app["record"] = rec = None
                save_record(None)
                st.show(UNKNOWN)
                continue
            # The song changed to one Shazam doesn't know (common on Vinyl Moon). Don't keep showing
            # the previous song: right after a gap, one miss is enough; otherwise two in a row.
            # A song the user picked by search stays until the next gap.
            # In record mode the tracklist already says what's playing, so keep showing that.
            # A song Shazam found earlier, on a record, CD or tape, plays to its end: long beat-only
            # stretches and breakdowns often don't match, and that doesn't mean the song changed.
            if (gap or listen_now or (misses >= 2 and not picked)) and not (rec and rec.idx is not None):
                if song_still_playing(app, t):
                    log("no match, but the song Shazam found is still within its length: keeping it")
                else:
                    st.show(UNKNOWN)


def song_still_playing(app, t):
    """True while a song Shazam identified from the mic should still be playing, going by its length.
    Only for physical media: on the radio the next song can start any time."""
    if not t or t.get("source") != "turntable" or t.get("status") != "playing":
        return False
    if (t.get("medium") or room_medium(app)) == "radio":
        return False
    p = progress_now(app)
    if not p or p.get("pos") is None:
        return False
    dur = p.get("dur") or 420              # length unknown: assume up to 7 minutes
    return p["pos"] < dur - 5


# ---------------------------------------------------------------- listening history
class History:
    """Every song shown, stored on this PC (DATA_DIR/history.db)."""

    def __init__(self, path):
        self.path = path
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS plays (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, title TEXT, artist TEXT, album TEXT,
                year TEXT, label TEXT, genre TEXT, source TEXT, art TEXT, duration_ms INTEGER)""")
            db.execute("CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts)")
            db.execute("""CREATE TABLE IF NOT EXISTS saved (
                tid TEXT PRIMARY KEY, ts REAL NOT NULL, title TEXT, artist TEXT, album TEXT, year TEXT,
                label TEXT, genre TEXT, art TEXT, source TEXT, spotify_uri TEXT)""")
            db.execute("CREATE TABLE IF NOT EXISTS song_cache (key TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS shelf_hidden (key TEXT PRIMARY KEY, ts REAL NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS collection (release_id INTEGER, instance_id INTEGER PRIMARY KEY,
                title TEXT, artist TEXT, year TEXT, label TEXT, catno TEXT, formats TEXT, medium TEXT, thumb TEXT,
                cover TEXT, added TEXT, rating INTEGER)""")
            db.execute("CREATE TABLE IF NOT EXISTS shelf_links (release_id INTEGER PRIMARY KEY, key TEXT)")  # your copy -> the album you played
            db.execute("""CREATE TABLE IF NOT EXISTS skips (ts REAL NOT NULL, title TEXT, artist TEXT, album TEXT,
                album_artist TEXT, medium TEXT, pos REAL)""")
            cols = {r[1] for r in db.execute("PRAGMA table_info(plays)")}
            if "album_artist" not in cols:
                db.execute("ALTER TABLE plays ADD COLUMN album_artist TEXT")   # the record's artist (Various Artists on a compilation)
            if "release_id" not in cols:
                db.execute("ALTER TABLE plays ADD COLUMN release_id INTEGER")  # the Discogs pressing, when its tracklist was followed
        self.saved_ids = self._saved_ids()

    def _saved_ids(self):
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT tid FROM saved")}

    def save(self, tid, t):
        art = t.get("art")
        if art and art.startswith("/api/art/"):
            art = None
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO saved (tid,ts,title,artist,album,year,label,genre,art,source) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (tid, time.time(), t.get("title"), t.get("artist"), t.get("album"), t.get("year"),
                        t.get("label"), t.get("genre"), art,
                        (t.get("medium") or "room") if t.get("source") in ROOM_SOURCES else (t.get("app") or "PC")))
        self.saved_ids.add(tid)

    def set_spotify_uri(self, tid, uri):
        with self._db() as db:
            db.execute("UPDATE saved SET spotify_uri=? WHERE tid=?", (uri, tid))

    def unsave(self, tid):
        with self._db() as db:
            row = db.execute("SELECT spotify_uri FROM saved WHERE tid=?", (tid,)).fetchone()
            db.execute("DELETE FROM saved WHERE tid=?", (tid,))
        self.saved_ids.discard(tid)
        return row[0] if row else None

    def idle_data(self):
        """What the screen shows between records: the last album, recent ones, saved songs, covers for a slideshow."""
        with self._db() as db:
            db.row_factory = sqlite3.Row
            rows = lambda sql: [dict(r) for r in db.execute(sql)]
            last = rows("SELECT ts, title, artist, album, year, label, source, art, duration_ms FROM plays ORDER BY ts DESC LIMIT 1")
            recent = rows("SELECT album, artist, title, MAX(art) art, MAX(ts) ts, source FROM plays WHERE album IS NOT NULL AND album!='' "
                          "AND art IS NOT NULL GROUP BY lower(album), lower(artist) ORDER BY MAX(ts) DESC LIMIT 9")
            saved = rows("SELECT title, artist, album, art, ts FROM saved ORDER BY ts DESC LIMIT 4")
            covers = rows("SELECT album, artist, title, MAX(art) art, MAX(year) year, COUNT(*) n, MAX(ts) ts FROM plays WHERE art IS NOT NULL "
                          "AND album IS NOT NULL AND album!='' GROUP BY lower(album), lower(artist) ORDER BY MAX(ts) DESC LIMIT 120")
        last = last[0] if last else None
        if last:
            recent = [r for r in recent if not (r["album"] or "").lower() == (last.get("album") or "").lower()][:8]
        return {"last": last, "recent": recent, "saved": saved, "covers": covers, "now": time.time()}

    def name_album_since(self, since, title, artist):
        """You named the compilation that's playing: the songs already heard from it go under that name."""
        with self._db() as db:
            db.execute("UPDATE plays SET album=?, album_artist=? WHERE ts>=? AND source IN ('vinyl','cd','cassette','other','room')",
                       (title, artist, since))

    def add_skip(self, t, pos=None):
        with self._db() as db:
            db.execute("INSERT INTO skips (ts,title,artist,album,album_artist,medium,pos) VALUES (?,?,?,?,?,?,?)",
                       (time.time(), t.get("title"), t.get("artist"), t.get("album"), t.get("album_artist"),
                        t.get("medium") or "vinyl", pos))

    def _skips_by_album(self):
        """Skips heard on each shelf album: how many, which songs, and on how many different days."""
        with self._db() as db:
            rows = db.execute("SELECT ts, title, artist, album, album_artist, medium FROM skips WHERE album IS NOT NULL").fetchall()
        out = {}
        for ts, title, artist, album, aa, medium in rows:
            k = self.shelf_key(medium, album, aa or artist)
            d = out.setdefault(k, {"n": 0, "songs": {}, "days": set()})
            d["n"] += 1
            d["days"].add(time.strftime("%Y-%m-%d", time.localtime(ts)))
            s = d["songs"].setdefault(norm(title), {"title": title, "n": 0, "days": set()})
            s["n"] += 1
            s["days"].add(time.strftime("%Y-%m-%d", time.localtime(ts)))
        return out

    def change_medium(self, old, new, since):
        with self._db() as db:
            db.execute("UPDATE plays SET source=? WHERE source=? AND ts>=?", (new, old, since))
            db.execute("UPDATE saved SET source=? WHERE source=? AND ts>=?", (new, old, since))

    def set_room_medium(self, medium, since):
        """Plays heard before anyone said what they were on get filled in."""
        with self._db() as db:
            db.execute("UPDATE plays SET source=? WHERE source='room' AND ts>=?", (medium, since))
            db.execute("UPDATE saved SET source=? WHERE source='room' AND ts>=?", (medium, since))

    def vinyl_seconds(self, since):
        """Time spent playing vinyl since `since`. Each play counts for its song length, but never past
        the start of the next play, so a song cut short (or a side flipped early) isn't over-counted."""
        with self._db() as db:
            rows = db.execute("SELECT ts, source, duration_ms FROM plays WHERE ts>=? ORDER BY ts", (since or 0,)).fetchall()
        total = 0.0
        for i, (ts, source, dur) in enumerate(rows):
            if source != "vinyl":
                continue
            secs = (dur or 210000) / 1000
            end = rows[i + 1][0] if i + 1 < len(rows) else time.time()    # a song still playing counts so far
            secs = min(secs, max(0.0, end - ts))
            total += min(secs, 1800)                # nothing on a record runs longer than a side
        return total

    # ---- your shelf: every album heard on a record, CD or tape
    SHELF_MEDIA = ("vinyl", "cd", "cassette", "other")

    @staticmethod
    def shelf_key(medium, album, artist):
        """Same record, however each source spells it: 'Rumours (2004 Remaster)' and 'Rumours' match."""
        a = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", album or "")
        a = re.sub(r"\s+-\s+(deluxe|expanded|remaster(ed)?|anniversary|special).*$", "", a, flags=re.I)
        art = re.sub(r"^the\s+", "", (artist or "").strip(), flags=re.I)
        return f"{(medium or '').lower()}|{norm(a) or norm(album)}|{norm(art)}"

    def _shelf_rows(self):
        with self._db() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                f"SELECT ts, title, artist, album, album_artist, source, art, year, label, release_id FROM plays "
                f"WHERE source IN ({','.join('?' * len(self.SHELF_MEDIA))}) AND album IS NOT NULL AND trim(album)!='' "
                f"ORDER BY ts", self.SHELF_MEDIA)]
            hidden = {r[0] for r in db.execute("SELECT key FROM shelf_hidden")}
        for r in rows:
            r["key"] = self.shelf_key(r["source"], r["album"], r["album_artist"] or r["artist"])
        return rows, hidden

    # ---- your Discogs collection
    def collection_replace(self, items):
        with self._db() as db:
            db.execute("DELETE FROM collection")
            db.executemany("INSERT OR REPLACE INTO collection (release_id,instance_id,title,artist,year,label,catno,formats,"
                           "medium,thumb,cover,added,rating) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [(i["release_id"], i["instance_id"], i["title"], i["artist"], i["year"], i["label"], i["catno"],
                             i["formats"], i["medium"], i["thumb"], i["cover"], i["added"], i["rating"]) for i in items])

    def collection_add(self, i, shelf_key=None):
        with self._db() as db:
            if shelf_key:
                db.execute("INSERT OR REPLACE INTO shelf_links (release_id, key) VALUES (?, ?)", (i["release_id"], shelf_key))
            db.execute("INSERT OR REPLACE INTO collection (release_id,instance_id,title,artist,year,label,catno,formats,"
                       "medium,thumb,cover,added,rating) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (i["release_id"], i["instance_id"], i["title"], i["artist"], i["year"], i["label"], i["catno"],
                        i["formats"], i["medium"], i["thumb"], i["cover"], i["added"], i["rating"]))

    def collection_rows(self):
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM collection")]

    def shelf(self):
        rows, hidden = self._shelf_rows()
        out = {}
        for r in rows:
            if r["key"] in hidden:
                continue
            a = out.get(r["key"])
            if not a:
                a = out[r["key"]] = {"key": r["key"], "medium": r["source"], "album": r["album"],
                                     "artist": r["album_artist"] or r["artist"], "plays": 0, "first": r["ts"],
                                     "art": None, "year": None, "label": None, "_songs": set()}
            a["plays"] += 1
            a["last"] = r["ts"]
            a["_songs"].add(norm(r["title"]))
            if r["album_artist"]:                         # a record followed from its tracklist: its own names win
                a["artist"], a["album"], a["_named"] = r["album_artist"], r["album"], True
            elif not a.get("_named") and len(r["album"]) < len(a["album"]):
                a["album"] = r["album"]                   # 'Rumours' over 'Rumours (2004 Remaster)'
            for k in ("art", "year", "label"):
                a[k] = r[k] or a[k]                       # the most recent known value
            a["release_id"] = r["release_id"] or a.get("release_id")
        # Records in your Discogs collection: matched to what you've played, or on the shelf unplayed
        with self._db() as db:
            links = dict(db.execute("SELECT release_id, key FROM shelf_links").fetchall())
        for c in self.collection_rows():
            medium = c["medium"] or "other"
            if medium not in self.SHELF_MEDIA:
                continue
            k = links.get(c["release_id"]) or self.shelf_key(medium, c["title"], c["artist"])
            if k in hidden:
                continue
            a = out.get(k)
            if not a:
                a = out[k] = {"key": k, "medium": medium, "album": c["title"], "artist": c["artist"], "plays": 0,
                              "first": None, "last": 0, "art": None, "year": None, "label": None, "_songs": set()}
            a["owned"] = True
            a["release_id"] = c["release_id"]             # the copy you own
            a["art"] = a["art"] or c["cover"] or c["thumb"]
            a["year"] = a["year"] or c["year"]
            a["label"] = a["label"] or c["label"]
            a["catno"], a["formats"], a["added"] = c["catno"], c["formats"], c["added"]
        albums = sorted(out.values(), key=lambda a: -a["last"])
        skips = self._skips_by_album()
        for a in albums:
            sk = skips.get(a["key"])
            if sk:
                a["skips"] = sk["n"]
                a["skip_songs"] = [x["title"] for x in sk["songs"].values()]
                # one skip is dust or a bump; the same spot again on another day, or lots of them, is wear
                a["worn"] = sk["n"] >= 4 or len(sk["days"]) >= 3 or any(len(x["days"]) >= 2 for x in sk["songs"].values())
        for a in albums:
            a["songs"] = len(a.pop("_songs"))
            a.pop("_named", None)
            a["kind"] = album_kind(a["album"], None, a["artist"])
        return albums

    def shelf_songs(self, key):
        rows, _ = self._shelf_rows()
        songs = {}
        for r in rows:
            if r["key"] != key:
                continue
            k = norm(r["title"]) + "|" + norm(r["artist"])
            s = songs.setdefault(k, {"title": r["title"], "artist": r["artist"], "album": r["album"], "art": r["art"], "plays": 0})
            s["plays"] += 1
            s["last"] = r["ts"]
            s["art"] = r["art"] or s["art"]
        sk = self._skips_by_album().get(key)
        if sk:
            for s in songs.values():
                x = sk["songs"].get(norm(s["title"]))
                if x:
                    s["skips"] = x["n"]
        return list(songs.values())

    def shelf_hide(self, key, hide=True):
        with self._db() as db:
            if hide:
                db.execute("INSERT OR REPLACE INTO shelf_hidden (key, ts) VALUES (?, ?)", (key, time.time()))
            else:
                db.execute("DELETE FROM shelf_hidden WHERE key=?", (key,))

    # ---- song pages saved on this PC, so they open instantly (and offline) the next time
    def song_origins(self, titles, days=365):
        """Who these songs are by and the album they first came out on, from the songs Shazam named in the
        last year. Songs followed from a record's tracklist are left out: on a compilation those name the
        compilation, not the original album."""
        want = {norm(clean_title(t)) for t in titles if t}
        if not want:
            return {}
        with self._db() as db:
            rows = db.execute("SELECT title, artist, album, year, art FROM plays WHERE ts>? AND artist!='' "
                              "AND COALESCE(album_artist,'')='' ORDER BY ts DESC LIMIT 20000",
                              (time.time() - days * 86400,)).fetchall()
        out = {}
        for title, artist, album, year, art in rows:
            k = norm(clean_title(title))
            if k in want and k not in out and len(k) >= 5 and norm(artist) not in ("various", "variousartists"):   # not "Intro"
                out[k] = {"artist": artist, "album": album, "year": (re.findall(r"\d{4}", str(year or "")) or [None])[0], "art": art}
        return out

    def cache_get(self, key, max_age_days=30):
        with self._db() as db:
            row = db.execute("SELECT ts, data FROM song_cache WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[0] > max_age_days * 86400:
            return None
        try:
            return json.loads(row[1])
        except Exception:
            return None

    def cache_put(self, key, data):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO song_cache (key, ts, data) VALUES (?,?,?)", (key, time.time(), json.dumps(data)))

    def last_play_of(self, artist, title):
        with self._db() as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM plays WHERE lower(artist)=lower(?) AND lower(title)=lower(?) ORDER BY ts DESC LIMIT 1",
                           (artist, title)).fetchone()
        return dict(r) if r else None

    def artist_plays(self, names):
        """How many times each of these artists has been played here, keyed by norm(name)."""
        with self._db() as db:
            rows = db.execute("SELECT artist, COUNT(*) FROM plays GROUP BY lower(artist)").fetchall()
        counts = {}
        for a, n in rows:
            counts[norm(a)] = counts.get(norm(a), 0) + n
        return {norm(x): counts.get(norm(x), 0) for x in names}

    def saved_list(self):
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM saved ORDER BY ts DESC")]

    def _db(self):
        return sqlite3.connect(self.path)

    def add(self, t):
        art = t.get("art")
        if art and art.startswith("/api/art/"):
            art = None                  # Windows thumbnails aren't kept, so don't store a dead link
        with self._db() as db:
            db.execute("INSERT INTO plays (ts,title,artist,album,year,label,genre,source,art,duration_ms,album_artist,release_id) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), t.get("title"), t.get("artist"), t.get("album"), t.get("year"),
                        t.get("label"), t.get("genre"), (t.get("medium") or "room") if t.get("source") in ROOM_SOURCES
                        else (t.get("app") or "PC"), art, t.get("duration_ms"), t.get("album_artist"), t.get("release_id")))

    def stats(self, days):
        since = time.time() - days * 86400 if days else 0
        with self._db() as db:
            db.row_factory = sqlite3.Row
            q = lambda sql, *a: [dict(r) for r in db.execute(sql, (since, *a))]
            total = q("SELECT COUNT(*) plays, COUNT(DISTINCT lower(artist)) artists, "
                      "COUNT(DISTINCT lower(artist)||'|'||lower(title)) songs, "
                      "COALESCE(SUM(COALESCE(duration_ms, 210000)),0)/60000 minutes FROM plays WHERE ts>=?")[0]
            by_source = q("SELECT source, COUNT(*) n FROM plays WHERE ts>=? GROUP BY source ORDER BY n DESC")
            artists = q("SELECT artist, COUNT(*) n, MAX(art) art FROM plays WHERE ts>=? AND artist!='' "
                        "GROUP BY lower(artist) ORDER BY n DESC LIMIT 10")
            albums = q("SELECT album, artist, COUNT(*) n, MAX(art) art, MAX(year) year FROM plays "
                       "WHERE ts>=? AND album IS NOT NULL AND album!='' "
                       "GROUP BY lower(album), lower(artist) ORDER BY n DESC LIMIT 12")
            songs = q("SELECT title, artist, COUNT(*) n, MAX(art) art FROM plays WHERE ts>=? "
                      "GROUP BY lower(title), lower(artist) ORDER BY n DESC LIMIT 10")
            genres = q("SELECT genre, COUNT(*) n FROM plays WHERE ts>=? AND genre IS NOT NULL "
                       "GROUP BY genre ORDER BY n DESC LIMIT 6")
            span = days or 30
            per_day = q("SELECT date(ts,'unixepoch','localtime') day, COUNT(*) n FROM plays "
                        "WHERE ts>=? GROUP BY day ORDER BY day")
            recent = q("SELECT ts, title, artist, album, year, source, art FROM plays WHERE ts>=? "
                       "ORDER BY ts DESC LIMIT 40")
        return {"days": days, "total": total, "by_source": by_source, "artists": artists, "albums": albums,
                "songs": songs, "genres": genres, "per_day": per_day[-span:], "recent": recent}


# ---------------------------------------------------------------- demo mode
DEMO = [
    ("Khruangbin", "Maria También", "Con Todo El Mundo", "pc", "Spotify"),
    ("Big Thief", "Not", "Two Hands", "turntable", "Turntable"),
    ("Alvvays", "Archie, Marry Me", "Alvvays", "pc", "Spotify"),
]


async def demo_loop(app):
    st: State = app["state"]
    i = 0
    while True:
        artist, title, album, source, label = DEMO[i % len(DEMO)]
        art, _ = await itunes_art(app["http"], artist, title)
        st.show({"id": make_id(artist, title), "title": title, "artist": artist, "album": album,
                 "art": art, "source": source, "app": label, "status": "playing",
                 "links": search_links(artist, title, album)})
        i += 1
        await asyncio.sleep(12)


# ---------------------------------------------------------------- web server
async def index(request):
    # Never let a phone or browser keep an old copy of the page after an update
    return web.FileResponse(os.path.join(HERE, "display.html"),
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


MANIFEST = {"id": "/", "scope": "/", "name": APP_NAME, "short_name": APP_NAME, "start_url": "/", "display": "fullscreen",
            "orientation": "any", "background_color": "#0b0b0c", "theme_color": "#0b0b0c",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
import logo
ICON = logo.LOGO_TILE_SVG


async def manifest(request):
    return web.json_response(MANIFEST, content_type="application/manifest+json")


async def logo_svg(request):
    return web.Response(text=logo.LOGO_SVG, content_type="image/svg+xml")


async def icon(request):
    return web.Response(text=ICON, content_type="image/svg+xml")


async def api_search(request):
    q = (request.query.get("q") or "").strip()
    if not q:
        return web.json_response({"results": []})
    try:
        results = await itunes_search(request.app["http"], q)
    except Exception as e:
        log("search failed:", e)
        return web.json_response({"results": [], "error": "Search isn't reachable right now."})
    return web.json_response({"results": results})


async def api_pick(request):
    """The user picked a song by search: show it until the next song starts."""
    body = await request.json()
    info = await itunes_track(request.app["http"], body.get("track_id"))
    if not info:
        return web.json_response({"ok": False}, status=404)
    show_and_enrich(request.app, {
        "id": make_id(info["artist"], info["title"]), "title": info["title"], "artist": info["artist"],
        "album": info["album"], "art": info["art"], "source": "manual", "app": "Picked by you",
        "status": "playing", "year": info["year"], "label": info["label"], "genre": info["genre"],
        "duration_ms": info["duration_ms"], "links": search_links(info["artist"], info["title"], info["album"]),
    })
    return web.json_response({"ok": True})


async def api_stats(request):
    try:
        days = int(request.query.get("days", "30"))
    except ValueError:
        days = 30
    return web.json_response(request.app["state"].db.stats(days))


def icon_png(size):
    """Home-screen icon for the phone (the logo on a dark tile)."""
    img = logo.draw_mark(size, tile=True)
    bio = io.BytesIO()
    img.save(bio, "PNG")
    return bio.getvalue()


_icons = {}


async def icon_file(request):
    size = 512 if "512" in request.path else 192
    if size not in _icons:
        _icons[size] = icon_png(size)
    return web.Response(body=_icons[size], content_type="image/png")


SW = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
"""


async def service_worker(request):
    return web.Response(text=SW, content_type="application/javascript")


# ---------------------------------------------------------------- updates (installed app only)
try:
    from _version import VERSION
except Exception:
    VERSION = "dev"
# The repo may be named either way (it's being renamed from coverart); GitHub redirects the old name.
RELEASES_APIS = ("https://api.github.com/repos/PostModernPost/linernotes/releases/latest",
                 "https://api.github.com/repos/PostModernPost/coverart/releases/latest")


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("-")[0].split("."))
    except ValueError:
        return (0,)


async def check_for_update(app):
    """Ask GitHub for the newest release. Sets app['update'] when it's newer than this copy."""
    if VERSION == "dev":
        return None
    rel = None
    for url in RELEASES_APIS:
        try:
            async with app["http"].get(url, headers={"Accept": "application/vnd.github+json",
                                                     "User-Agent": "LinerNotes"}) as r:
                if r.status == 200:
                    rel = await r.json(content_type=None)
                    break
                log(f"update check: GitHub said {r.status} for {url.split('/repos/')[1]}")
        except Exception as e:
            log("update check failed:", e)
    if not rel:
        return None
    latest = rel.get("tag_name", "")
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
    app["update_checked"] = time.time()
    if asset and version_tuple(latest) > version_tuple(VERSION):
        app["update"] = {"version": latest.lstrip("v"), "url": asset["browser_download_url"], "api_url": asset.get("url"),
                         "notes": (rel.get("body") or "").strip()[:2000], "page": rel.get("html_url")}
        log(f"update available: {latest}")
    else:
        app["update"] = None
    return app["update"]


async def update_loop(app):
    await asyncio.sleep(10)
    while True:
        await check_for_update(app)
        await asyncio.sleep(15 * 60)       # every 15 minutes, so a new version is offered soon after it's out


async def install_update(app):
    """Download the new installer and run it quietly; it closes this copy and reopens the new one."""
    upd = app.get("update")
    if not upd:
        return False
    app["updating"] = True
    path = os.path.join(tempfile.gettempdir(), f"LinerNotes-Setup-{upd['version']}.exe")
    log(f"downloading update {upd['version']}")
    # GitHub's download page now answers with headers longer than aiohttp's 8 KB default, so allow more,
    # and ask the API for the file (it redirects straight to the download host)
    async with ClientSession(timeout=ClientTimeout(total=600), max_line_size=65536, max_field_size=65536) as http:
        last = None
        for url, headers in ((upd.get("api_url"), {"Accept": "application/octet-stream"}), (upd["url"], {})):
            if not url:
                continue
            try:
                async with http.get(url, headers=headers) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
                last = None
                break
            except Exception as e:
                last = e
                log(f"update download from {url.split('/')[2]} failed: {e!r}"[:300])
        if last:
            raise last
    log("starting installer")
    import subprocess
    subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/updated=1"],
                     creationflags=0x00000008 | 0x00000200)   # detached, own process group
    asyncio.get_running_loop().call_later(1.0, lambda: os._exit(0))
    return True


def update_info(app):
    if ANDROID:            # the app updates itself (or Play does)
        return {"current": VERSION, "available": None, "updating": False}
    return {"current": VERSION, "available": app.get("update"), "updating": bool(app.get("updating"))}


async def api_update(request):
    app = request.app
    if request.method == "POST":
        action = (await request.json()).get("action")
        if action == "check":
            await check_for_update(app)
        elif action == "install":
            try:
                ok = await install_update(app)
            except Exception as e:
                app["updating"] = False
                log("update failed:", repr(e))
                return web.json_response({**update_info(app), "error": "The download didn't work. Try again later."})
            return web.json_response({**update_info(app), "ok": ok})
    return web.json_response(update_info(app))


async def api_window_fullscreen(request):
    fn = request.app.get("on_toggle_fullscreen")
    full = await asyncio.get_running_loop().run_in_executor(None, fn) if fn else None
    return web.json_response({"ok": fn is not None, "full": full})


async def api_screen_idle(request):
    """A screen with nothing to show for a while turns off: the PC window hides to the tray, a USB phone's
    screen goes off. Both come back when the next song starts (State.wake_screens)."""
    app = request.app
    body = await request.json()
    if body.get("target") == "window":
        fn = app.get("on_hide_window")
        if fn:
            fn()
            app["window_idle_hidden"] = True
    elif "Android" in request.headers.get("User-Agent", "") and adb_path():
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, adb_phone_screens, False)
        app["phone_idle_off"] = True
    return web.json_response({"ok": True})


def wake_screens(app):
    """The next song started: bring back screens that turned off because nothing was playing."""
    if app.get("window_idle_hidden"):
        app["window_idle_hidden"] = False
        fn = app.get("on_show_window_quiet") or app.get("on_show_window")
        if fn:
            threading.Thread(target=fn, daemon=True).start()
    if app.get("phone_idle_off"):
        app["phone_idle_off"] = False
        threading.Thread(target=adb_phone_screens, args=(True,), daemon=True).start()


async def api_window(request):
    """A second launch of Liner Notes asks the running one to show its window."""
    fn = request.app.get("on_show_window")
    if fn:
        fn()
    return web.json_response({"ok": bool(fn)})


# ---------------------------------------------------------------- record mode endpoints
async def api_records_search(request):
    if not discogs_ready():
        return web.json_response({"results": [], "need_key": True})
    q = (request.query.get("q") or "").strip()
    barcode = re.sub(r"\D", "", request.query.get("barcode") or "")
    if not q and not barcode:
        return web.json_response({"results": []})
    try:
        return web.json_response({"results": await discogs_search(request.app["http"], q, barcode)})
    except PermissionError as e:
        return web.json_response({"results": [], "need_key": True, "error": str(e)})
    except Exception as e:
        log("discogs search failed:", repr(e))
        return web.json_response({"results": [], "error": "Discogs isn't reachable right now."})


async def api_records_guess(request):
    """The search screen's first suggestions, from the compilation question: compilations with the songs
    just heard, or the artist's studio albums."""
    app = request.app
    if not discogs_ready():
        return web.json_response({"results": [], "need_key": True})
    medium = room_medium(app)
    try:
        if request.query.get("kind") == "compilation":
            songs = (app.get("album_ask") or {}).get("songs") or []
            return web.json_response({"results": await compilation_guesses(app["http"], songs, medium) if songs else []})
        artist, title = (request.query.get("artist") or "").strip(), (request.query.get("title") or "").strip()
        if not artist:
            return web.json_response({"results": []})
        with_song = await song_album_guesses(app["http"], artist, title, medium) if title else []
        have = {norm(r["title"]) for r in with_song}
        others = [r for r in await studio_album_guesses(app["http"], artist, medium) if norm(r["title"]) not in have]
        return web.json_response({"results": with_song + others, "with_song": len(with_song)})
    except Exception as e:
        log("record suggestions failed:", repr(e))
        return web.json_response({"results": [], "error": "Discogs isn't reachable right now."})


async def api_records_load(request):
    app = request.app
    body = await request.json()
    try:
        rel = await discogs_get(app["http"], f"/releases/{int(body.get('id'))}")
    except Exception as e:
        log("discogs release failed:", repr(e))
        return web.json_response({"ok": False, "error": "Couldn't load that record from Discogs."})
    rec = RecordSession(rel)
    if not rec.tracks:
        return web.json_response({"ok": False, "error": "Discogs doesn't list the songs on this one."})
    app["record"] = rec
    app.pop("album_ask", None)
    app.pop("album_name", None)
    save_record(rec)
    log(f"record loaded: {rec.artist} - {rec.title} ({len(rec.tracks)} tracks)")
    if app.get("listen_mode") == "active":          # already playing: start at track 1, Shazam will correct it
        show_record(app, rec, rec.set_index(0))
        app["force_check"] = True
    return web.json_response({"ok": True, "record": rec.summary()})


async def api_records_position(request):
    """You tapped a song in the tracklist: that's what's playing now."""
    app = request.app
    rec = app.get("record")
    if not rec:
        return web.json_response({"ok": False})
    body = await request.json()
    show_record(app, rec, rec.set_index(body.get("index", 0)))
    return web.json_response({"ok": True, "record": rec.summary()})


async def api_album_name(request):
    """Name the compilation that's playing (when neither Apple Music nor Discogs could find it), or stop using the name."""
    app, st = request.app, request.app["state"]
    body = await request.json()
    if body.get("clear"):
        app.pop("album_name", None)
        return web.json_response({"ok": True})
    title = re.sub(r"\s+", " ", body.get("title") or "").strip()[:200]
    if not title:
        return web.json_response({"ok": False, "error": "Type the album's name."}, status=400)
    artist = re.sub(r"\s+", " ", body.get("artist") or "").strip()[:200] or "Various Artists"
    ask = app.pop("album_ask", None) or {}
    since = ask.get("since") or time.time() - 600
    rec = app.get("record")
    if rec and rec.auto:
        app.pop("record", None)
    app["album_name"] = {"title": title, "artist": artist, "since": time.time()}
    await asyncio.to_thread(st.db.name_album_since, since, title, artist)
    if st.track and st.track.get("source") in ROOM_SOURCES:
        st.enrich(st.track["id"], {"album": title, "album_artist": artist})
    log(f"compilation named: {title} ({artist})")
    return web.json_response({"ok": True})


async def api_album_ask(request):
    """'Not a compilation': stop asking for a few hours."""
    app = request.app
    app.pop("album_ask", None)
    app["album_ask_off_until"] = time.time() + 3 * 3600
    return web.json_response({"ok": True})


async def api_records_clear(request):
    app = request.app
    rec = app.pop("record", None)
    save_record(None)
    st = app["state"]
    if rec and st.track and st.track.get("source") == "record":
        st.idle()
    return web.json_response({"ok": True})


async def api_config(request):
    """Settings changed from the app (currently: a personal Discogs token)."""
    body = await request.json()
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if "discogs_token" in body:
        cfg["discogs_token"] = CFG["discogs_token"] = (body.get("discogs_token") or "").strip()
    if "phone_follows_pc" in body:
        cfg["phone_follows_pc"] = CFG["phone_follows_pc"] = bool(body.get("phone_follows_pc"))
    if body.get("pc_media") in ("music", "all"):
        cfg["pc_media"] = CFG["pc_media"] = body["pc_media"]
    if "listening_paused" in body:
        cfg["listening_paused"] = CFG["listening_paused"] = bool(body.get("listening_paused"))
        if not CFG["listening_paused"]:
            wake_screens(request.app)          # listening back on: screens that went dark come back
    if "phone_off_minutes" in body:
        try:
            cfg["phone_off_minutes"] = CFG["phone_off_minutes"] = max(0, min(240, int(body.get("phone_off_minutes") or 0)))
        except Exception:
            pass
    if "follow_album" in body:
        cfg["follow_album"] = CFG["follow_album"] = bool(body.get("follow_album"))
    if body.get("mic_sensitivity") in SENSITIVITY:
        cfg["mic_sensitivity"] = CFG["mic_sensitivity"] = body["mic_sensitivity"]
    if "room_medium" in body:
        cfg["room_medium"] = CFG["room_medium"] = body["room_medium"] if body["room_medium"] in MEDIA else None
    if "lastfm_include_pc" in body:
        cfg["lastfm_include_pc"] = CFG["lastfm_include_pc"] = bool(body.get("lastfm_include_pc"))
    if body.get("spotify_save_to") in ("playlist", "liked"):
        cfg["spotify_save_to"] = CFG["spotify_save_to"] = body["spotify_save_to"]
    if "spotify_client_id" in body:
        new = re.sub(r"[^0-9a-fA-F]", "", body.get("spotify_client_id") or "")
        if new != (CFG.get("spotify_client_id") or ""):
            spotify_store({})          # a different Spotify app needs a fresh login
        cfg["spotify_client_id"] = CFG["spotify_client_id"] = new
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return web.json_response({"ok": True, "discogs_ready": discogs_ready(), "spotify": spotify_status(),
                              "lastfm": lastfm_status()})


# ---------------------------------------------------------------- save the band (Spotify)
# The heart on the main screen keeps a song in Liner Notes' Saved list. With Spotify connected it
# also goes into a "Liner Notes" playlist (or Liked Songs) on the listener's account.
# Login uses Spotify's PKCE flow, so no secret ships with the app: just a client ID.
try:
    from _secrets import SPOTIFY_CLIENT_ID as _BUILTIN_SPOTIFY_ID
except Exception:
    _BUILTIN_SPOTIFY_ID = ""
SPOTIFY_FILE = os.path.join(DATA_DIR, "spotify.json")
SPOTIFY_SCOPES = "user-library-modify user-library-read playlist-modify-private playlist-modify-public playlist-read-private"
PLAYLIST_NAME = "Liner Notes"
_spotify_login = {}           # state -> code verifier, for logins in progress


def spotify_client_id():
    return (CFG.get("spotify_client_id") or "").strip() or _BUILTIN_SPOTIFY_ID


def spotify_redirect():
    return f"http://127.0.0.1:{CFG['port']}/api/spotify/callback"


def spotify_load():
    try:
        with open(SPOTIFY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def spotify_store(d):
    with open(SPOTIFY_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


async def spotify_token(http):
    """A working access token, refreshed when needed. None when Spotify isn't connected."""
    d = spotify_load()
    if not d.get("refresh_token"):
        return None
    if d.get("access_token") and d.get("expires", 0) > time.time() + 60:
        return d["access_token"]
    async with http.post("https://accounts.spotify.com/api/token", data={
            "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
            "client_id": d.get("client_id") or spotify_client_id()}) as r:
        j = await r.json(content_type=None)
    if "access_token" not in j:
        log("spotify: refresh failed:", j.get("error_description") or j.get("error") or r.status)
        if j.get("error") == "invalid_grant":
            spotify_store({})          # the listener removed access in Spotify; start over
        return None
    d.update(access_token=j["access_token"], expires=time.time() + int(j.get("expires_in", 3600)))
    if j.get("refresh_token"):
        d["refresh_token"] = j["refresh_token"]
    spotify_store(d)
    return d["access_token"]


async def spotify_call(http, method, path, **kw):
    token = await spotify_token(http)
    if not token:
        return None, None
    async with http.request(method, "https://api.spotify.com/v1" + path,
                            headers={"Authorization": f"Bearer {token}"}, **kw) as r:
        try:
            j = await r.json(content_type=None)
        except Exception:
            j = None
        if r.status >= 400:
            log("spotify:", method, path, r.status, (j or {}).get("error"))
        return r.status, j


async def spotify_find(http, t):
    """The Spotify track URI for a song, found by searching title and artist."""
    title, artist = t.get("title") or "", t.get("artist") or ""
    base = lambda x: norm(re.sub(r"\s*[\(\[].*?[\)\]]|\s+-\s+.*$", "", x))
    for q in (f'track:"{title}" artist:"{artist}"', f"{title} {artist}"):
        st, j = await spotify_call(http, "GET", "/search", params={"q": q, "type": "track", "limit": "10"})
        items = ((j or {}).get("tracks") or {}).get("items") or []
        for it in items:
            artists = [norm(a.get("name")) for a in it.get("artists") or []]
            if base(it.get("name") or "") == base(title) and (norm(artist) in artists or
                    any(a and (a in norm(artist) or norm(artist) in a) for a in artists)):
                return it.get("uri")
    return None


async def spotify_playlist(http):
    d = spotify_load()
    if d.get("playlist_id"):
        return d["playlist_id"]
    # reuse a "Liner Notes" playlist if one is already there (after reconnecting)
    st, j = await spotify_call(http, "GET", "/me/playlists", params={"limit": "50"})
    for p in (j or {}).get("items") or []:
        if p and p.get("name") == PLAYLIST_NAME:
            d["playlist_id"] = p["id"]
            break
    else:
        st, j = await spotify_call(http, "POST", "/me/playlists", json={
            "name": PLAYLIST_NAME, "public": False,
            "description": "Songs saved from Liner Notes, including the ones heard on records."})
        if not j or not j.get("id"):
            return None
        d["playlist_id"] = j["id"]
    spotify_store(d)
    return d["playlist_id"]


async def spotify_add(http, uri):
    if CFG.get("spotify_save_to", "playlist") == "liked":
        st, _ = await spotify_call(http, "PUT", "/me/library", params={"uris": uri})
        return st is not None and st < 300
    pid = await spotify_playlist(http)
    if not pid:
        return False
    st, _ = await spotify_call(http, "POST", f"/playlists/{pid}/items", json={"uris": [uri]})
    return st is not None and st < 300


async def spotify_remove(http, uri):
    if CFG.get("spotify_save_to", "playlist") == "liked":
        await spotify_call(http, "DELETE", "/me/library", params={"uris": uri})
        return
    pid = spotify_load().get("playlist_id")
    if pid:
        await spotify_call(http, "DELETE", f"/playlists/{pid}/items", json={"items": [{"uri": uri}]})


def spotify_status():
    d = spotify_load()
    return {"configured": bool(spotify_client_id()), "builtin": bool(_BUILTIN_SPOTIFY_ID),
            "connected": bool(d.get("refresh_token")), "user": d.get("user"),
            "save_to": CFG.get("spotify_save_to", "playlist"), "redirect": spotify_redirect(),
            "own_id": bool((CFG.get("spotify_client_id") or "").strip())}


async def api_save(request):
    """Heart or un-heart a song. Saved songs also go to Spotify when it's connected."""
    st: State = request.app["state"]
    body = await request.json()
    tid = body.get("id") or ""
    t = st.history.get(tid) or (st.track if st.track and st.track.get("id") == tid else None)
    db = st.db
    http = request.app["http"]
    if body.get("saved") is False or (body.get("saved") is None and tid in db.saved_ids):
        uri = db.unsave(tid)
        if uri:
            try:
                await spotify_remove(http, uri)
            except Exception as e:
                log("spotify: remove failed:", e)
        return web.json_response({"saved": False})
    if not t or not t.get("artist"):
        return web.json_response({"error": "That song isn't on screen any more."}, status=404)
    db.save(tid, t)
    result = "not_connected"
    if spotify_load().get("refresh_token"):
        try:
            uri = await spotify_find(http, t)
            if not uri:
                result = "not_found"
            elif await spotify_add(http, uri):
                db.set_spotify_uri(tid, uri)
                result = "saved"
            else:
                result = "error"
        except Exception as e:
            log("spotify: save failed:", e)
            result = "error"
    return web.json_response({"saved": True, "spotify": result,
                              "where": "Liked Songs" if CFG.get("spotify_save_to") == "liked" else PLAYLIST_NAME})


async def api_medium(request):
    """The listener says what's playing the music in the room (and maybe makes it the default)."""
    body = await request.json()
    m = body.get("medium")
    if m not in MEDIA:
        return web.json_response({"error": "unknown medium"}, status=400)
    ROOM["session"], ROOM["last"] = m, time.time()
    if body.get("default"):
        save_config_value("room_medium", m)
    st: State = request.app["state"]
    st.db.set_room_medium(m, time.time() - 6 * 3600)
    rec = request.app.get("record")
    if body.get("change"):
        # Switching what this is playing on (the medium button): this record and the songs played from it
        old = (st.track or {}).get("medium") or (rec.medium if rec else None)
        if rec is not None:
            rec.medium = m
        since = rec.started if rec is not None else time.time() - 30 * 60
        if old and old != m:
            st.db.change_medium(old, m, since)
        if st.track and st.track.get("source") in ROOM_SOURCES:
            st.track = {**st.track, "medium": m}
            st.history[st.track["id"]] = st.track
            st.version += 1
        log(f"medium changed to {m}")
    elif st.track and st.track.get("source") in ROOM_SOURCES and not st.track.get("medium"):
        st.track = {**st.track, "medium": m}
        st.history[st.track["id"]] = st.track
        st.version += 1
    return web.json_response({"ok": True, "medium": m, "default": CFG.get("room_medium")})


async def api_listen_now(request):
    """The Listen now button: record a clip and ask Shazam right away."""
    app = request.app
    if app.get("mic") is None:
        return web.json_response({"ok": False, "error": "No microphone to listen with."})
    if CFG.get("listening_paused"):
        return web.json_response({"ok": False, "error": "Listening is off. Turn it on with the microphone button first."})
    if app.get("pc_playing"):
        return web.json_response({"ok": False, "error": "Something is playing on this PC, so that's what's shown."})
    app["listen_now_at"] = time.time()
    app.pop("listen_now_started", None)
    app["listen_now_state"] = "recording"
    app["listen_now_done"] = 0
    return web.json_response({"ok": True, "seconds": CFG["clip_seconds"]})


async def api_last_clip(request):
    clip = request.app.get("last_clip")
    if not clip:
        return web.Response(status=404, text="No clip yet")
    return web.Response(body=clip[0], content_type="audio/wav", headers={"Cache-Control": "no-store"})


async def api_mic_level(request):
    """For the mic meter in Settings: what the mic hears now, the room's background, and the two lines."""
    app = request.app
    app["meter_until"] = time.time() + 3          # keep the mic running while the meter is open
    instant, sustained = live_level(app.get("mic")) if app.get("mic") is not None else (None, None)
    if sustained is None:                          # no live feed (yet): the last sample's level
        m = app.get("meter") or {}
        if time.time() - (m.get("at") or 0) < 4:
            instant = sustained = m.get("level")
    return web.json_response({"level": instant, "sustained": sustained, "floor": NOISE["floor"],
                              "clip_seconds": CFG["clip_seconds"],
                              "start": music_cutoff(), "keep": keep_cutoff(),
                              "mode": app.get("listen_mode", "off"), "has_mic": app.get("mic") is not None,
                              "sensitivity": CFG.get("mic_sensitivity") or "normal",
                              "clip_at": (app.get("last_clip") or (None, None))[1],
                              "mic_name": getattr(app.get("mic"), "name", None)})


async def api_idle(request):
    st: State = request.app["state"]
    d = st.db.idle_data()
    last = d.get("last")
    if last:
        # "Finished 2 min ago" counts from when it stopped playing, not when it started
        if st.track is None and st.idle_at > last["ts"]:
            last["ended"] = st.idle_at
        else:
            last["ended"] = min(time.time(), last["ts"] + (last.get("duration_ms") or 210000) / 1000)
    try:
        d["stylus"] = await asyncio.to_thread(stylus_status, st.db)
    except Exception:
        pass
    return web.json_response(d)


# ---------------------------------------------------------------- stylus hours
STYLUS_WARN = 0.9          # nudge at 90% of the stylus's life


def stylus_status(db):
    life = float(CFG.get("stylus_life_hours") or 1000)
    since = CFG.get("stylus_since")
    hours = float(CFG.get("stylus_start_hours") or 0) + db.vinyl_seconds(since) / 3600
    snooze = float(CFG.get("stylus_snooze_hours") or 0)
    return {"hours": round(hours, 1), "life": life, "since": since, "start_hours": float(CFG.get("stylus_start_hours") or 0),
            "pct": min(1.0, hours / life) if life else 0, "due": hours >= life,
            "warn": hours >= life * STYLUS_WARN and hours >= snooze}


def save_cfg(**changes):
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg.update(changes)
    CFG.update(changes)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


async def api_stylus(request):
    db = request.app["state"].db
    if request.method == "POST":
        body = await request.json()
        action = body.get("action")
        if action == "replaced":            # a new stylus went on today
            save_cfg(stylus_since=time.time(), stylus_start_hours=0, stylus_snooze_hours=0)
            log("stylus: replaced, counting from zero")
        elif action == "snooze":            # remind me again after another 25 hours of records
            save_cfg(stylus_snooze_hours=stylus_status(db)["hours"] + 25)
        else:
            ch = {}
            if "since" in body:             # a date (YYYY-MM-DD) the current stylus went on, or null = all history
                try:
                    ch["stylus_since"] = time.mktime(time.strptime(body["since"], "%Y-%m-%d")) if body["since"] else None
                except Exception:
                    pass
            if "start_hours" in body:
                try:
                    ch["stylus_start_hours"] = max(0.0, min(10000.0, float(body["start_hours"] or 0)))
                except Exception:
                    pass
            if "life_hours" in body:
                try:
                    ch["stylus_life_hours"] = max(50.0, min(5000.0, float(body["life_hours"])))
                except Exception:
                    pass
            if ch:
                ch["stylus_snooze_hours"] = 0
                save_cfg(**ch)
    return web.json_response(await asyncio.to_thread(stylus_status, db))


# ---------------------------------------------------------------- Discogs collection
# Your records on Discogs join the shelf, played or not. A public collection only needs your Discogs
# username; a private one needs your personal Discogs token (Settings > Accounts > Discogs).
COLLECTION = {"busy": False}


def discogs_user():
    return (CFG.get("discogs_user") or "").strip()


async def discogs_identity(http):
    """With a personal token: whose it is."""
    return (await discogs_get(http, "/oauth/identity")).get("username")


async def sync_collection(app):
    """Read every release in the collection (100 a page, gently, inside Discogs' rate limit)."""
    if COLLECTION["busy"]:
        return {"ok": False, "error": "Already syncing."}
    user = discogs_user()
    if not user:
        return {"ok": False, "error": "Add your Discogs username first."}
    COLLECTION["busy"] = True
    http = app["http"]
    items, page, pages = [], 1, 1
    try:
        while page <= pages and page <= 200:
            data = await discogs_get(http, f"/users/{urllib.parse.quote(user)}/collection/folders/0/releases",
                                     {"per_page": 100, "page": page, "sort": "added", "sort_order": "desc"})
            pages = int((data.get("pagination") or {}).get("pages") or 1)
            for r in data.get("releases") or []:
                b = r.get("basic_information") or {}
                fm = b.get("formats") or []
                names = " ".join((f.get("name") or "") for f in fm).lower()
                if not discogs_medium({"formats": fm}) and re.search(r"vhs|dvd|blu-ray|laserdisc|file|betamax|umd", names):
                    continue                      # films and downloads aren't for the turntable shelf
                labels = b.get("labels") or [{}]
                items.append({"release_id": b.get("id") or r.get("id"), "instance_id": r.get("instance_id"),
                              "title": b.get("title") or "", "artist": join_artists(b.get("artists")) or "",
                              "year": str(b.get("year") or "") or None, "label": clean_artist(labels[0].get("name")) or None,
                              "catno": labels[0].get("catno"),
                              "formats": ", ".join(" ".join([f.get("name") or ""] + (f.get("descriptions") or [])) for f in fm),
                              "medium": discogs_medium({"formats": fm}) or "other",
                              "thumb": b.get("thumb") or None, "cover": b.get("cover_image") or None,
                              "added": (r.get("date_added") or "")[:10], "rating": r.get("rating")})
            page += 1
            if page <= pages:
                await asyncio.sleep(1.1)
        await asyncio.to_thread(app["state"].db.collection_replace, items)
        save_cfg(discogs_synced=time.time(), discogs_count=len(items))
        log(f"Discogs collection: {len(items)} releases for {user}")
        return {"ok": True, "count": len(items)}
    except PermissionError:
        return {"ok": False, "error": "Discogs didn't accept the token. Paste it again in Settings."}
    except Exception as e:
        log("Discogs collection sync failed:", repr(e))
        msg = str(e)
        if "403" in msg or "404" in msg:
            return {"ok": False, "error": "Discogs won't share that collection. Check the username, or add your token if the collection is private."}
        return {"ok": False, "error": "Couldn't reach Discogs. Try again in a minute."}
    finally:
        COLLECTION["busy"] = False


async def collection_loop(app):
    """Keep the collection fresh: once a day while Liner Notes runs."""
    await asyncio.sleep(60)
    while True:
        if discogs_user() and time.time() - float(CFG.get("discogs_synced") or 0) > 24 * 3600:
            await sync_collection(app)
        await asyncio.sleep(3600)


def collection_status():
    return {"user": discogs_user(), "token": bool((CFG.get("discogs_token") or "").strip()),
            "count": CFG.get("discogs_count"), "synced": CFG.get("discogs_synced"), "busy": COLLECTION["busy"]}


async def api_collection(request):
    app = request.app
    if request.method == "GET":
        return web.json_response(collection_status())
    body = await request.json()
    action = body.get("action")
    if action == "disconnect":
        save_cfg(discogs_user="", discogs_synced=0, discogs_count=None)
        await asyncio.to_thread(app["state"].db.collection_replace, [])
        return web.json_response({"ok": True, **collection_status()})
    if "token" in body:                           # optional: a personal token, for a private collection
        save_cfg(discogs_token=(body.get("token") or "").strip())
    user = (body.get("user") or "").strip().lstrip("@")
    m = re.search(r"discogs\.com/(?:user/)?([^/?#]+)", user)
    if m:
        user = m.group(1)                         # a profile link pasted instead of the name
    if not user and (CFG.get("discogs_token") or "").strip():
        try:
            user = await discogs_identity(app["http"])
        except Exception:
            return web.json_response({"ok": False, "error": "Discogs didn't accept that token.", **collection_status()})
    if user:
        save_cfg(discogs_user=user)
    res = await sync_collection(app)
    return web.json_response({**res, **collection_status()})


async def discogs_post(http, path):
    async with http.post(DISCOGS + path, headers=discogs_headers()) as r:
        if r.status in (401, 403):
            raise PermissionError("Discogs didn't accept the token")
        if r.status == 429:
            raise RuntimeError("Discogs is busy, try again in a minute")
        r.raise_for_status()
        return await r.json(content_type=None)


def release_row(b, instance_id=None, added=None):
    """A collection row from a Discogs release (full release or a collection item's basic_information)."""
    fm = b.get("formats") or []
    labels = b.get("labels") or [{}]
    return {"release_id": b.get("id"), "instance_id": instance_id, "title": b.get("title") or "",
            "artist": join_artists(b.get("artists")) or "", "year": str(b.get("year") or "") or None,
            "label": clean_artist(labels[0].get("name")) or None, "catno": labels[0].get("catno"),
            "formats": ", ".join(" ".join([f.get("name") or ""] + (f.get("descriptions") or [])) for f in fm),
            "medium": discogs_medium({"formats": fm}) or "other",
            "thumb": b.get("thumb") or None,
            "cover": b.get("cover_image") or next((i.get("uri") for i in b.get("images") or [] if i.get("uri")), None),
            "added": added or time.strftime("%Y-%m-%d"), "rating": 0}


async def api_discogs_pressings(request):
    """Which pressing to add to your collection: the one Liner Notes followed first, then Discogs' search."""
    http = request.app["http"]
    key = request.query.get("key") or ""
    a = next((x for x in await asyncio.to_thread(request.app["state"].db.shelf) if x["key"] == key), None)
    if not a:
        return web.json_response({"pressings": []})
    out, seen = [], set()
    try:
        if a.get("release_id"):
            r = await discogs_get(http, f"/releases/{int(a['release_id'])}")
            out.append({"id": r.get("id"), "title": f"{join_artists(r.get('artists'))} - {r.get('title')}",
                        "year": r.get("year"), "format": release_row(r)["formats"], "label": release_row(r)["label"],
                        "catno": release_row(r)["catno"], "country": r.get("country"),
                        "thumb": release_row(r)["cover"], "followed": True})
            seen.add(r.get("id"))
        fmt = {"vinyl": "Vinyl", "cd": "CD", "cassette": "Cassette"}.get(a["medium"])
        params = {"type": "release", "artist": main_artist(a["artist"]) if a["artist"].lower() not in ("various", "various artists") else "",
                  "release_title": bare_album(a["album"]), "per_page": 12}
        res = (await discogs_get(http, "/database/search", {**params, **({"format": fmt} if fmt else {})})).get("results", [])
        if not res and fmt:
            res = (await discogs_get(http, "/database/search", params)).get("results", [])
        for r in res:
            if r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            out.append({"id": r.get("id"), "title": r.get("title"), "year": r.get("year"),
                        "format": ", ".join(r.get("format") or []), "label": ", ".join((r.get("label") or [])[:2]),
                        "catno": r.get("catno"), "country": r.get("country"), "thumb": r.get("thumb") or r.get("cover_image")})
    except Exception as e:
        log("Discogs pressings lookup failed:", repr(e))
        return web.json_response({"pressings": out, "error": "Couldn't reach Discogs."})
    return web.json_response({"pressings": out[:8]})


async def api_discogs_add(request):
    """Add a pressing to your Discogs collection (the Uncategorized folder), then to the shelf here."""
    app = request.app
    if not (CFG.get("discogs_token") or "").strip() or not discogs_user():
        return web.json_response({"ok": False, "need_token": True,
                                  "error": "Adding to Discogs needs your personal Discogs token: Settings > Accounts > Discogs."})
    body = await request.json()
    try:
        rid = int(body.get("release_id"))
        res = await discogs_post(app["http"], f"/users/{urllib.parse.quote(discogs_user())}/collection/folders/1/releases/{rid}")
        rel = await discogs_get(app["http"], f"/releases/{rid}")
    except PermissionError:
        return web.json_response({"ok": False, "need_token": True, "error": "Discogs didn't accept the token. Paste it again in Settings."})
    except Exception as e:
        log("adding to the Discogs collection failed:", repr(e))
        return web.json_response({"ok": False, "error": "Couldn't add it. Try again in a minute."})
    row = release_row(rel, res.get("instance_id"))
    await asyncio.to_thread(app["state"].db.collection_add, row, body.get("key"))
    save_cfg(discogs_count=(CFG.get("discogs_count") or 0) + 1)
    log(f"added to the Discogs collection: {row['artist']} - {row['title']} ({rid})")
    return web.json_response({"ok": True, "title": row["title"]})


async def api_share(request):
    """What to send when you share a song: one line and a link anyone can open in their music app
    (song.link, from the song's Apple Music entry), falling back to Spotify or Shazam."""
    st: State = request.app["state"]
    t = st.history.get(request.query.get("id", "")) or (st.track if st.track and st.track.get("id") == request.query.get("id") else None)
    if not t or not t.get("title"):
        return web.json_response({"error": "nothing to share"}, status=404)
    url = None
    try:
        info = await itunes_lookup(request.app["http"], t["artist"], t["title"]) or {}
        if info.get("track_id") and norm(clean_title(info.get("title"))) == norm(clean_title(t["title"])):
            url = f"https://song.link/i/{info['track_id']}"
    except Exception:
        pass
    if not url:
        links = {l.get("name"): l.get("url") for l in t.get("links") or []}
        url = links.get("Spotify") or links.get("Shazam") or next(iter(links.values()), None) or \
            "https://open.spotify.com/search/" + urllib.parse.quote(f"{t['artist']} {t['title']}")
    on = {"vinyl": " on vinyl", "cassette": " on tape", "cd": " on CD"}.get(t.get("medium") or "", "")
    text = f"Listening to \u201c{t['title']}\u201d by {t['artist']}{on}"
    return web.json_response({"title": f"{t['title']} \u00b7 {t['artist']}", "text": text, "url": url,
                              "via": "https://liner-notes.app"})


async def api_market(request):
    """What a release goes for on the Discogs marketplace (lowest price listed, how many for sale)."""
    try:
        rid = int(request.query.get("id"))
        d = await discogs_get(request.app["http"], f"/marketplace/stats/{rid}", {"curr_abbr": "USD"})
    except Exception:
        return web.json_response({})
    low = d.get("lowest_price") or {}
    return web.json_response({"lowest": low.get("value"), "currency": low.get("currency"),
                              "for_sale": d.get("num_for_sale"), "url": f"https://www.discogs.com/sell/release/{rid}"})


async def api_shelf(request):
    db = request.app["state"].db
    if request.query.get("key"):
        return web.json_response({"songs": await asyncio.to_thread(db.shelf_songs, request.query["key"])})
    return web.json_response({"albums": await asyncio.to_thread(db.shelf), "discogs": collection_status()})


async def api_shelf_hide(request):
    body = await request.json()
    if not body.get("key"):
        return web.json_response({"ok": False}, status=400)
    await asyncio.to_thread(request.app["state"].db.shelf_hide, body["key"], body.get("hide", True))
    return web.json_response({"ok": True})


async def api_saved(request):
    rows = request.app["state"].db.saved_list()
    for r in rows:
        r["links"] = {l["name"].lower(): l["url"] for l in search_links(r["artist"], r["title"], r.get("album"))}
        if r.get("spotify_uri"):
            r["links"]["spotify"] = "https://open.spotify.com/track/" + r["spotify_uri"].split(":")[-1]
    return web.json_response({"saved": rows, "spotify": spotify_status()})


async def api_spotify_status(request):
    return web.json_response(spotify_status())


async def api_spotify_connect(request):
    """Open Spotify's login in the PC's browser. It comes back to /api/spotify/callback."""
    import base64, secrets, webbrowser
    cid = spotify_client_id()
    if not cid:
        return web.json_response({"error": "No Spotify app set up."}, status=400)
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    _spotify_login.clear()
    _spotify_login[state] = (verifier, cid)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": spotify_redirect(), "state": state,
        "scope": SPOTIFY_SCOPES, "code_challenge_method": "S256", "code_challenge": challenge})
    if not ANDROID:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    return web.json_response({"ok": True, "url": url})


def _done_page(msg):
    return web.Response(content_type="text/html", text=f"""<!doctype html><meta charset="utf-8">
<title>Liner Notes</title><body style="background:#111;color:#eee;font:18px system-ui;display:grid;place-items:center;height:90vh;text-align:center">
<div><h2 style="font-weight:600">{msg}</h2><p style="color:#999">You can close this tab and go back to Liner Notes.</p></div>""")


async def api_spotify_callback(request):
    q = request.query
    if q.get("error"):
        return _done_page("Spotify wasn't connected" + (" (you said no)." if q["error"] == "access_denied" else f": {q['error']}."))
    pending = _spotify_login.pop(q.get("state", ""), None)
    if not pending or not q.get("code"):
        return _done_page("That login link expired. Try Connect Spotify again.")
    verifier, cid = pending
    http = request.app["http"]
    async with http.post("https://accounts.spotify.com/api/token", data={
            "grant_type": "authorization_code", "code": q["code"], "redirect_uri": spotify_redirect(),
            "client_id": cid, "code_verifier": verifier}) as r:
        j = await r.json(content_type=None)
    if "refresh_token" not in j:
        log("spotify: login failed:", j)
        return _done_page("Spotify wasn't connected: " + (j.get("error_description") or j.get("error") or "unknown error"))
    spotify_store({"client_id": cid, "refresh_token": j["refresh_token"], "access_token": j["access_token"],
                   "expires": time.time() + int(j.get("expires_in", 3600))})
    st, me = await spotify_call(http, "GET", "/me")
    if st == 403:
        spotify_store({})
        return _done_page("Spotify says this account isn't allowed to use Liner Notes yet. "
                          "Ask the app's owner to add your Spotify email, or use your own Spotify app (Settings).")
    d = spotify_load()
    d["user"] = (me or {}).get("display_name") or (me or {}).get("id")
    spotify_store(d)
    # Anything hearted before connecting goes to Spotify now
    asyncio.create_task(spotify_backfill(request.app))
    return _done_page("Spotify connected" + (f" as {d['user']}" if d.get("user") else "") + ".")


async def spotify_backfill(app):
    db, http = app["state"].db, app["http"]
    for r in reversed(db.saved_list()):
        if r.get("spotify_uri"):
            continue
        try:
            uri = await spotify_find(http, r)
            if uri and await spotify_add(http, uri):
                db.set_spotify_uri(r["tid"], uri)
        except Exception as e:
            log("spotify: backfill:", e)
        await asyncio.sleep(0.3)


async def api_spotify_disconnect(request):
    spotify_store({})
    return web.json_response(spotify_status())


# ---------------------------------------------------------------- Last.fm scrobbling
# Records you play get logged ("scrobbled") to your Last.fm profile, next to what Spotify already
# sends there. Last.fm's rule: a song counts once it has played for half its length or 4 minutes.
try:
    from _secrets import LASTFM_KEY, LASTFM_SECRET
except Exception:
    LASTFM_KEY = LASTFM_SECRET = ""
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
LASTFM_FILE = os.path.join(DATA_DIR, "lastfm.json")
VINYL_SOURCES = ("turntable", "record", "manual")


def lastfm_load():
    try:
        with open(LASTFM_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def lastfm_store(d):
    with open(LASTFM_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def lastfm_sign(params):
    raw = "".join(k + str(params[k]) for k in sorted(params) if k not in ("format", "callback")) + LASTFM_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


async def lastfm_call(http, method, params, post=True):
    p = {"method": method, "api_key": LASTFM_KEY, **{k: v for k, v in params.items() if v not in (None, "")}}
    p["api_sig"] = lastfm_sign(p)
    p["format"] = "json"
    req = http.post(LASTFM_API, data=p) if post else http.get(LASTFM_API, params=p)
    async with req as r:
        j = await r.json(content_type=None)
    if isinstance(j, dict) and j.get("error"):
        log("last.fm:", method, j.get("error"), j.get("message"))
    return j or {}


def lastfm_status():
    d = lastfm_load()
    return {"available": bool(LASTFM_KEY and LASTFM_SECRET), "connected": bool(d.get("sk")), "user": d.get("user"),
            "count": d.get("count", 0), "pending": len(d.get("pending") or []),
            "include_pc": bool(CFG.get("lastfm_include_pc", False))}


async def api_lastfm_status(request):
    return web.json_response(lastfm_status())


async def api_lastfm_connect(request):
    import webbrowser
    if not (LASTFM_KEY and LASTFM_SECRET):
        return web.json_response({"error": "This build doesn't include a Last.fm key."}, status=400)
    cb = f"http://127.0.0.1:{CFG['port']}/api/lastfm/callback"
    url = "https://www.last.fm/api/auth/?" + urllib.parse.urlencode({"api_key": LASTFM_KEY, "cb": cb})
    if not ANDROID:            # on the phone, the page opens it in the phone's browser
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    return web.json_response({"ok": True, "url": url})


async def api_lastfm_callback(request):
    token = request.query.get("token")
    if not token:
        return _done_page("Last.fm wasn't connected.")
    j = await lastfm_call(request.app["http"], "auth.getSession", {"token": token}, post=False)
    sess = j.get("session") or {}
    if not sess.get("key"):
        return _done_page("Last.fm wasn't connected: " + (j.get("message") or "unknown error"))
    d = lastfm_load()
    d.update(sk=sess["key"], user=sess.get("name"))
    lastfm_store(d)
    return _done_page("Last.fm connected" + (f" as {sess.get('name')}" if sess.get("name") else "") + ".")


async def api_lastfm_disconnect(request):
    d = lastfm_load()
    lastfm_store({"count": d.get("count", 0)})
    return web.json_response(lastfm_status())


async def lastfm_send(http, items):
    """Scrobble up to 50 plays. Returns True when Last.fm took them (or rejected them for good)."""
    d = lastfm_load()
    if not d.get("sk"):
        return False
    params = {"sk": d["sk"]}
    for i, it in enumerate(items[:50]):
        for k in ("artist", "track", "album", "timestamp", "duration"):
            if it.get(k):
                params[f"{k}[{i}]"] = it[k]
    try:
        j = await lastfm_call(http, "track.scrobble", params)
    except Exception as e:
        log("last.fm: scrobble failed (will retry):", e)
        return False
    if j.get("error") in (9, 4, 14):         # session no longer valid: needs connecting again
        d.pop("sk", None)
        lastfm_store(d)
        return False
    if j.get("error") in (11, 16, 29):       # Last.fm busy or rate limited: try later
        return False
    acc = int(((j.get("scrobbles") or {}).get("@attr") or {}).get("accepted", 0) or 0)
    d = lastfm_load()
    d["count"] = d.get("count", 0) + acc
    lastfm_store(d)
    log(f"last.fm: scrobbled {acc} of {len(items[:50])}")
    return True


async def lastfm_now_playing(http, t):
    d = lastfm_load()
    if d.get("sk"):
        try:
            await lastfm_call(http, "track.updateNowPlaying", {
                "sk": d["sk"], "artist": t.get("artist"), "track": t.get("title"), "album": t.get("album"),
                "duration": int((t.get("duration_ms") or 0) / 1000) or None})
        except Exception:
            pass


async def lastfm_loop(app):
    """Watch what's on screen. Once a song has played long enough, scrobble it (once)."""
    st: State = app["state"]
    http = app["http"]
    cur = None               # {"id", "start", "done"}
    last_retry = 0
    while True:
        await asyncio.sleep(5)
        try:
            if not (LASTFM_KEY and LASTFM_SECRET):
                continue
            d = lastfm_load()
            if not d.get("sk"):
                cur = None
                continue
            t = st.track
            ok_source = t and ((t.get("source") in VINYL_SOURCES and t.get("medium") != "radio") or
                               (t.get("source") not in VINYL_SOURCES and CFG.get("lastfm_include_pc")))
            if not t or t.get("status") != "playing" or not ok_source or not t.get("artist"):
                cur = None if not t or t.get("status") != "playing" else cur
                continue
            now = time.time()
            if not cur or cur["id"] != t["id"]:
                pr = progress_now(app)
                pos = pr.get("pos") if pr else None
                cur = {"id": t["id"], "start": now - (pos or 0), "done": False}
                asyncio.create_task(lastfm_now_playing(http, t))
            if cur["done"]:
                continue
            dur = (t.get("duration_ms") or 0) / 1000 or None
            if dur is not None and dur < 30:
                cur["done"] = True
                continue
            pr = progress_now(app)
            if pr and pr.get("pos") is not None and pr.get("kind") == "exact":
                cur["start"] = now - pr["pos"]          # Shazam or Spotify knows exactly where in the song we are
            played = now - cur["start"]
            if played >= min((dur or 180) / 2, 240):
                cur["done"] = True
                item = {"artist": t["artist"], "track": t["title"], "album": t.get("album"),
                        "timestamp": int(cur["start"]), "duration": int(dur) if dur else None}
                d = lastfm_load()
                d.setdefault("pending", []).append(item)
                lastfm_store(d)
                last_retry = 0
            # send anything waiting (new plays, or ones that failed while offline)
            pending = lastfm_load().get("pending") or []
            pending = [p for p in pending if p["timestamp"] > now - 13 * 86400]     # Last.fm takes up to 14 days back
            if pending and now - last_retry > 60:
                last_retry = now
                if await lastfm_send(http, pending):
                    d = lastfm_load()
                    d["pending"] = [p for p in (d.get("pending") or []) if p not in pending[:50]]
                    lastfm_store(d)
        except Exception as e:
            log("last.fm loop:", repr(e))


# ---------------------------------------------------------------- credits (Discogs), for the info page
# Who played what, who produced and engineered it, where it was recorded, and the release notes.
# Records loaded in record mode use that exact release; other songs use the best Discogs match for the album.
_credits_cache = {}


def _discogs_markup(text):
    """Discogs notes use [a=Name], [l=Label], [url=...]text[/url], [b]bold[/b]..."""
    t = text or ""
    t = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", t, flags=re.S)
    t = re.sub(r"\[(?:a|l|m|r)=([^\]]+)\]", r"\1", t)
    t = re.sub(r"\[(?:a|l|m|r)\d+\]", "", t)
    t = re.sub(r"\[/?(?:b|i|u)\]", "", t)
    t = re.sub(r"\r\n?", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _role(r):
    return re.sub(r"\s*\[([^\]]+)\]", r" (\1)", (r or "").strip()).strip(" ,")


def _track_positions(tracks_field, order):
    """'A1 to A3, B2' -> {'A1','A2','A3','B2'} using the tracklist's order for ranges."""
    out = set()
    for part in re.split(r"\s*,\s*", tracks_field or ""):
        if not part:
            continue
        m = re.match(r"(.+?)\s+to\s+(.+)$", part, flags=re.I)
        if m and m.group(1) in order and m.group(2) in order:
            a, b = order.index(m.group(1)), order.index(m.group(2))
            out.update(order[min(a, b): max(a, b) + 1])
        else:
            out.add(part.strip())
    return out


def _group(credits):
    """One line per person, with everything they did: "Laura Lee: Bass, Vocals"."""
    people = collections.OrderedDict()
    for c in credits:
        name = clean_artist(c.get("anv") or c.get("name"))
        if not name:
            continue
        for role in re.split(r",\s*(?![^\[]*\])", c.get("role") or ""):
            role = _role(role)
            if role and role not in people.setdefault(name, []):
                people[name].append(role)
    return [{"name": n, "roles": r} for n, r in people.items()]


def release_credits(rel, title=None, position=None):
    tracklist = [t for t in rel.get("tracklist") or [] if t.get("type_", "track") == "track"]
    order = [(t.get("position") or "").strip() for t in tracklist]
    track = None
    if position:
        track = next((t for t in tracklist if (t.get("position") or "").strip() == position), None)
    if track is None and title:
        want = norm(clean_title(title))
        track = next((t for t in tracklist if norm(clean_title(t.get("title"))) == want), None)
    pos = (track.get("position") or "").strip() if track else None
    song, album = list((track or {}).get("extraartists") or []), []
    for c in rel.get("extraartists") or []:
        applies = (c.get("tracks") or "").strip()
        if not applies:
            album.append(c)
        elif pos and pos in _track_positions(applies, order):
            song.append(c)
    companies = collections.OrderedDict()
    for c in rel.get("companies") or []:
        role = c.get("entity_type_name") or ""
        if role and c.get("name"):
            names = companies.setdefault(role, [])
            if clean_artist(c["name"]) not in names:
                names.append(clean_artist(c["name"]))
    fmt = rel.get("formats") or [{}]
    desc = ", ".join([fmt[0].get("name") or ""] + (fmt[0].get("descriptions") or []))
    pressing = " · ".join(x for x in (desc.strip(", "), rel.get("country"), str(rel.get("year") or "") or None) if x)
    return {"song": _group(song), "album": _group(album),
            "companies": [{"role": r, "names": n} for r, n in companies.items()],
            "notes": _discogs_markup(rel.get("notes"))[:2500] or None,
            "pressing": pressing, "url": rel.get("uri"), "position": pos,
            "release": f"{join_artists(rel.get('artists'))} - {rel.get('title')}"}


async def find_release(http, artist, album, medium=None):
    """The Discogs release that best matches an album, preferring the format being played."""
    key = (norm(artist), norm(album), medium)
    if key in _credits_cache:
        return _credits_cache[key]
    fmt = {"vinyl": "Vinyl", "cd": "CD", "cassette": "Cassette"}.get(medium or "vinyl")
    rel = None
    base = {"type": "release", "artist": main_artist(artist), "release_title": clean_title(album), "per_page": 10}
    for params in ({**base, "format": fmt}, base):
        res = (await discogs_get(http, "/database/search", params)).get("results", [])
        res = [r for r in res if norm(clean_title(album)) in norm(r.get("title"))]
        if res:
            # a release with credits is more useful than a bare listing; Discogs puts the most-owned first
            rel = await discogs_get(http, f"/releases/{res[0]['id']}")
            break
    _credits_cache[key] = rel
    return rel


async def api_credits(request):
    app = request.app
    st: State = app["state"]
    t = st.history.get(request.query.get("id", ""))
    if not t or not t.get("artist"):
        return web.json_response({"credits": None})
    if not discogs_ready():
        return web.json_response({"credits": None, "reason": "no_discogs"})
    cache_key = ("credits", t["id"])
    if cache_key in _credits_cache:
        return web.json_response({"credits": _credits_cache[cache_key]})
    saved = st.db.cache_get("credits:" + t["id"], 60)
    if saved:
        _credits_cache[cache_key] = saved
        return web.json_response({"credits": saved})
    try:
        rec = app.get("record")
        if t.get("source") == "record" and rec is not None and getattr(rec, "rel", None):
            cr = release_credits(rec.rel, t.get("title"), t.get("record_pos"))
            cr["exact"] = True
        elif t.get("album"):
            rel = await find_release(app["http"], t["artist"], t["album"], t.get("medium"))
            cr = release_credits(rel, t.get("title")) if rel else None
            if cr:
                cr["exact"] = False
        else:
            cr = None
    except Exception as e:
        log("credits lookup failed:", repr(e))
        return web.json_response({"credits": None, "reason": "error"})
    if cr and not (cr["song"] or cr["album"] or cr["companies"] or cr["notes"]):
        cr = {**cr, "empty": True}
    _credits_cache[cache_key] = cr
    if cr:
        st.db.cache_put("credits:" + t["id"], cr)
    return web.json_response({"credits": cr})


# ---------------------------------------------------------------- more like this (similar artists)
# Last.fm's similar artists (good for small and indie bands), with Deezer's related artists when
# Last.fm has nothing. Pictures come from Deezer, since Last.fm no longer serves artist images.
_similar_cache = {}


async def _deezer_artist(http, name):
    try:
        async with http.get(f"{DEEZER}/search/artist", params={"q": name, "limit": "3"}) as r:
            d = await r.json(content_type=None)
        for a in (d or {}).get("data") or []:
            if norm(a.get("name")) == norm(name):
                return a
        return ((d or {}).get("data") or [None])[0]
    except Exception:
        return None


async def similar_artists(http, artist, limit=8):
    lead = main_artist(artist)
    key = norm(lead)
    if key in _similar_cache:
        return _similar_cache[key]
    db = getattr(similar_artists, "db", None)
    if db is not None:
        saved = db.cache_get("similar:" + key, 30)
        if saved:
            _similar_cache[key] = saved
            return saved
    names = []
    if LASTFM_KEY:
        try:
            async with http.get(LASTFM_API, params={"method": "artist.getSimilar", "artist": lead, "api_key": LASTFM_KEY,
                                                    "autocorrect": "1", "limit": str(limit + 4), "format": "json"}) as r:
                d = await r.json(content_type=None)
            for a in ((d or {}).get("similarartists") or {}).get("artist") or []:
                if a.get("name") and norm(a["name"]) != key:
                    names.append({"name": a["name"], "match": float(a.get("match") or 0), "lastfm": a.get("url")})
        except Exception as e:
            log("similar artists (Last.fm) failed:", repr(e))
    if not names:
        me = await _deezer_artist(http, lead)
        if me and me.get("id"):
            try:
                async with http.get(f"{DEEZER}/artist/{me['id']}/related", params={"limit": str(limit)}) as r:
                    d = await r.json(content_type=None)
                for a in (d or {}).get("data") or []:
                    names.append({"name": a.get("name"), "image": a.get("picture_medium"), "match": None})
            except Exception as e:
                log("similar artists (Deezer) failed:", repr(e))
    names = names[:limit]

    async def picture(x):
        if not x.get("image"):
            a = await _deezer_artist(http, x["name"])
            if a and norm(a.get("name")) == norm(x["name"]):
                x["image"] = a.get("picture_medium")
    await asyncio.gather(*(picture(x) for x in names))
    for x in names:
        q = urllib.parse.quote(x["name"])
        x["links"] = {"spotify": f"https://open.spotify.com/search/{q}", "bandcamp": f"https://bandcamp.com/search?q={q}&item_type=b",
                      "lastfm": x.get("lastfm") or f"https://www.last.fm/music/{urllib.parse.quote(x['name'].replace(' ', '+'))}"}
    _similar_cache[key] = names
    if names and db is not None:
        db.cache_put("similar:" + key, names)
    return names


async def api_similar(request):
    st: State = request.app["state"]
    t = st.history.get(request.query.get("id", ""))
    if not t or not t.get("artist"):
        return web.json_response({"artists": []})
    try:
        similar_artists.db = st.db
        names = await similar_artists(request.app["http"], t["artist"], limit=16)
    except Exception as e:
        log("similar artists failed:", repr(e))
        names = []
    plays = st.db.artist_plays([x["name"] for x in names]) if names else {}
    out = [{**x, "plays": plays.get(norm(x["name"]), 0)} for x in names]
    return web.json_response({"artists": out, "for": main_artist(t["artist"])})


# ---------------------------------------------------------------- shareable recap cards
_recap_art = collections.OrderedDict()      # art url -> PIL image, the last few dozen


async def _recap_images(http, urls):
    from PIL import Image

    async def one(u):
        if u in _recap_art:
            return
        try:
            async with http.get(u, timeout=ClientTimeout(total=8)) as r:
                if r.status == 200:
                    img = Image.open(io.BytesIO(await r.read()))
                    img.load()
                    _recap_art[u] = img
                    while len(_recap_art) > 60:
                        _recap_art.popitem(last=False)
        except Exception as e:
            log("recap: cover download failed:", e)
    await asyncio.gather(*(one(u) for u in dict.fromkeys(u for u in urls if u and u.startswith("http"))))
    return {u: _recap_art[u] for u in urls if u in _recap_art}


def _recap_args(request):
    period = request.query.get("period", "month")
    if period not in recap.PERIODS:
        period = "month"
    try:
        offset = max(0, min(50, int(request.query.get("offset", "0"))))
    except ValueError:
        offset = 0
    return period, offset


async def _recap_png(app, period, offset):
    start, end, title, sub = recap.recap_period(period, offset)
    stats = recap.recap_stats(app["state"].db.path, start, end)
    images = await _recap_images(app["http"], [a.get("art") for a in stats["albums"]])
    png = await asyncio.get_running_loop().run_in_executor(
        None, recap.render_card, stats, title, sub, images, os.path.join(HERE, "fonts"))
    return png, title, sub, stats


async def api_recap(request):
    period, offset = _recap_args(request)
    start, end, title, sub = recap.recap_period(period, offset)
    stats = recap.recap_stats(request.app["state"].db.path, start, end)
    with sqlite3.connect(request.app["state"].db.path) as db:
        first = db.execute("SELECT MIN(ts) FROM plays").fetchone()[0]
    return web.json_response({"title": title, "sub": sub, "plays": stats["total"]["plays"],
                              "has_older": bool(first and first < start)})


async def api_recap_png(request):
    period, offset = _recap_args(request)
    png, *_ = await _recap_png(request.app, period, offset)
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})


async def api_recap_save(request):
    """Save the card to Pictures\\Liner Notes on this PC and show it in Explorer."""
    body = await request.json()
    period = body.get("period") if body.get("period") in recap.PERIODS else "month"
    offset = max(0, min(50, int(body.get("offset") or 0)))
    png, title, sub, _ = await _recap_png(request.app, period, offset)
    folder = os.path.join(os.path.expanduser("~"), "Pictures", "Liner Notes")
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    name = re.sub(r'[\\/:*?"<>|]', "", f"Liner Notes recap - {title} ({period}, {stamp}).png")
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(png)
    if sys.platform == "win32":
        import subprocess
        subprocess.Popen(["explorer", "/select,", path])
    return web.json_response({"ok": True, "path": path})


# ---------------------------------------------------------------- phone as a second screen (USB)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
TOOLS_DIR = os.path.join(DATA_DIR, "platform-tools")


def adb_path():
    """Android's USB connector (adb): ours, or one already on this PC."""
    import shutil
    ours = os.path.join(TOOLS_DIR, "adb.exe" if sys.platform == "win32" else "adb")
    if os.path.exists(ours):
        return ours
    p = shutil.which("adb")
    if p:
        return p
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet")
    for root in (os.path.join(base, "Links"), os.path.join(base, "Packages")):
        for dirpath, _, files in os.walk(root):
            if "adb.exe" in files:
                return os.path.join(dirpath, "adb.exe")
    return None


def adb_run(*args, timeout=10):
    import subprocess
    adb = adb_path()
    if not adb:
        return None
    try:
        return subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except Exception as e:
        log("adb failed:", e)
        return None


def adb_devices():
    """Phones plugged in: [{serial, state ('device' = ready, 'unauthorized' = tap Allow), model}]"""
    r = adb_run("devices", "-l")
    if not r:
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            model = next((p.split(":", 1)[1].replace("_", " ") for p in parts[2:] if p.startswith("model:")), None)
            out.append({"serial": parts[0], "state": parts[1], "model": model})
    return out


def adb_phone_screens(on):
    """Turn the screens of plugged-in phones on or off (like pressing their power button)."""
    for d in adb_devices():
        if d["state"] == "device":
            adb_run("-s", d["serial"], "shell", "input", "keyevent", "KEYCODE_WAKEUP" if on else "KEYCODE_SLEEP", timeout=5)


async def phone_off_loop(app):
    """Settings > Phone screen: turn a USB phone's screen off after a while with nothing playing and
    nothing heard (or with listening turned off). It comes back on with the next song."""
    st: State = app["state"]
    last_busy = time.time()
    while True:
        await asyncio.sleep(15)
        try:
            now = time.time()
            mins = float(CFG.get("phone_off_minutes") or 0)
            playing = bool(st.track and st.track.get("status") == "playing")
            hearing = app.get("listen_mode") == "active"
            if playing or hearing or not mins:
                last_busy = now
                continue
            # The phone's own mic stops when its screen is off, so it couldn't hear the next record:
            # with the phone listening, its screen only turns off while listening is turned off.
            phone_listens = (str(CFG.get("audio_device")).lower() == "phone" and not CFG.get("listening_paused")
                             and now - app.get("last_app_poll", 0) > 60)     # the app keeps listening with its screen off
            if (phone_listens or app.get("phone_idle_off") or now - last_busy < mins * 60
                    or now - app.get("last_phone_poll", 0) > 60):
                continue
            if adb_path():                 # a phone over USB: press its power button
                await asyncio.get_running_loop().run_in_executor(None, adb_phone_screens, False)
            app["phone_idle_off"] = True   # the phone app turns its own screen off when it sees this
            log(f"phone screen off: nothing playing for {mins:g} min")
        except Exception as e:
            log("phone screen timer:", repr(e))


def adb_connect_phones():
    """Point each ready phone's localhost at this PC, so the phone can open the screen."""
    for d in adb_devices():
        if d["state"] == "device":
            adb_run("-s", d["serial"], "reverse", f"tcp:{CFG['port']}", f"tcp:{CFG['port']}")


async def usb_phone_loop(app):
    """A phone plugged in over USB (with USB debugging on): point its localhost at this PC right away,
    so the Liner Notes app on it switches to the PC by itself. Only when the Android tools are installed."""
    done = set()
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(5)
        try:
            if not adb_path():
                continue
            ready = {d["serial"] for d in await loop.run_in_executor(None, adb_devices) if d["state"] == "device"}
            for serial in ready - done:
                await loop.run_in_executor(None, lambda s=serial: adb_run("-s", s, "reverse", f"tcp:{CFG['port']}", f"tcp:{CFG['port']}"))
                log("phone plugged in over USB: connected it")
            done = ready
        except Exception as e:
            log("usb phone check failed:", repr(e))
            await asyncio.sleep(60)


async def install_platform_tools(http):
    """Download Google's Android platform tools (about 7 MB) into Liner Notes' folder."""
    import zipfile
    path = os.path.join(tempfile.gettempdir(), "platform-tools.zip")
    async with http.get(PLATFORM_TOOLS_URL, timeout=ClientTimeout(total=300)) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 16):
                f.write(chunk)
    with zipfile.ZipFile(path) as z:
        z.extractall(DATA_DIR)            # the zip holds a platform-tools folder
    os.remove(path)
    log("Android connector installed")


async def api_phone_status(request):
    app = request.app
    loop = asyncio.get_running_loop()
    devices = await loop.run_in_executor(None, adb_devices) if adb_path() else []
    if any(d["state"] == "device" for d in devices):
        await loop.run_in_executor(None, adb_connect_phones)
    return web.json_response({
        "tools": bool(adb_path()), "installing": bool(app.get("tools_installing")),
        "install_error": app.get("tools_error"), "devices": devices,
        "page_open": time.time() - app.get("last_phone_poll", 0) < 6,
        "mic_is_phone": str(CFG.get("audio_device")).lower() == "phone",
        "port": CFG["port"],
        "wifi": bool(CFG.get("allow_other_devices")), "wifi_address": lan_address(),
        "wifi_open": time.time() - app.get("last_wifi_poll", 0) < 6,
        "wifi_iphone": "iPhone" in app.get("last_wifi_agent", "") or "iPad" in app.get("last_wifi_agent", "")})


async def api_phone_install(request):
    app = request.app
    if app.get("tools_installing"):
        return web.json_response({"ok": True})
    app["tools_installing"], app["tools_error"] = True, None

    async def go():
        try:
            await install_platform_tools(app["http"])
        except Exception as e:
            log("connector install failed:", repr(e))
            app["tools_error"] = "The download didn't work. Check the internet connection and try again."
        finally:
            app["tools_installing"] = False
    asyncio.create_task(go())
    return web.json_response({"ok": True})


async def api_mic_list(request):
    """The microphones Settings offers, and which one is listening now."""
    if ANDROID:            # the phone app always listens with the phone's mic: Settings hides the choice
        return web.json_response({"error": "phone"}, status=404)
    cur = CFG.get("audio_device")
    names = await asyncio.to_thread(mic_choices)
    mic = request.app.get("mic")
    if str(cur).lower() == "phone":
        sel = "phone"
    elif not cur:
        sel = ""
    else:
        sel = next((n for n in names if n.lower() == str(cur).lower()), None) or \
              next((n for n in names if str(cur).lower() in n.lower()), str(cur))
    return web.json_response({"choices": names, "selected": sel, "listening_with": mic.name if mic else None})


async def api_mic_select(request):
    """Listen with a different microphone: "" = Windows default, "phone", or a name from the list.
    The listening part opens the mic once at start, so Liner Notes restarts."""
    body = await request.json()
    dev = body.get("device") or None
    if dev and dev != "phone" and dev not in await asyncio.to_thread(mic_choices):
        return web.json_response({"ok": False, "error": "That microphone isn't connected."}, status=400)
    save_cfg(audio_device=dev)
    log("microphone set to", dev or "Windows default")
    fn = request.app.get("on_restart")
    if fn:
        asyncio.get_running_loop().call_later(0.5, fn)
    return web.json_response({"ok": True, "restarting": bool(fn)})


async def api_phone_use_mic(request):
    """Switch listening to the phone's microphone (restarts the listening part)."""
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg["audio_device"] = CFG["audio_device"] = "phone"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    fn = request.app.get("on_restart")
    if fn:
        asyncio.get_running_loop().call_later(0.5, fn)
    return web.json_response({"ok": True, "restarting": bool(fn)})


# ---------------------------------------------------------------- sharing on Wi-Fi (iPhones, other devices)
# Requests from other devices may show the screen and use search; anything that changes this PC
# (updates, settings, phone tools, the window) only answers requests from this PC itself.
LOCAL_ONLY = ("/api/pair/answer", "/api/devices", "/api/update", "/api/config", "/api/phone/", "/api/window", "/api/share-wifi", "/api/spotify/", "/api/lastfm/", "/api/recap/save", "/api/screen/", "/api/mic/", "/api/stylus", "/api/shelf/hide", "/api/collection", "/api/discogs/")


# ---------------------------------------------------------------- the phone app: pairing over Wi-Fi
# The Liner Notes phone app on the same Wi-Fi asks to pair; you allow it on the PC; it gets a token
# (kept as a cookie in the app) that lets it do what Liner Notes on this PC can do: change settings,
# turn listening on and off, and so on. Over USB the app reaches the PC as localhost and needs no token.
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
PAIRING = {}               # request id -> {"code", "name", "at", "state", "token"}


def _devices():
    try:
        with open(DEVICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_devices(devs):
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(devs, f, indent=2)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def paired_device(request):
    """The paired phone making this request, or None."""
    token = request.cookies.get("ln_device") or request.headers.get("X-LinerNotes-Device") or ""
    if not token:
        return None
    h = _token_hash(token)
    return next((d for d in _devices() if d.get("hash") == h), None)


def is_local(request):
    """Liner Notes on this PC itself, a phone over USB (it arrives as localhost), or a paired phone."""
    return request.remote in ("127.0.0.1", "::1") or paired_device(request) is not None


async def api_pair_start(request):
    """The phone app asks to pair. Returns a request id (secret) and a short code shown on both screens."""
    import secrets as _s
    body = await request.json() if request.can_read_body else {}
    now = time.time()
    for k in [k for k, v in PAIRING.items() if now - v["at"] > 300]:
        PAIRING.pop(k, None)               # requests expire after 5 minutes
    if sum(1 for v in PAIRING.values() if v["state"] == "pending") >= 3:
        return web.json_response({"error": "Too many pairing requests. Try again in a few minutes."}, status=429)
    rid = _s.token_urlsafe(18)
    name = re.sub(r"[^\w .\-]", "", str(body.get("name") or "A phone"))[:40] or "A phone"
    PAIRING[rid] = {"code": f"{_s.randbelow(10000):04d}", "name": name, "at": now, "state": "pending", "token": None,
                    "from": request.remote}
    log(f"pairing request from {name} ({request.remote})")
    return web.json_response({"id": rid, "code": PAIRING[rid]["code"]})


async def api_pair_status(request):
    """The phone app checks whether the PC allowed it. The token is handed over once."""
    p = PAIRING.get(request.query.get("id", ""))
    if not p:
        return web.json_response({"state": "expired"})
    if p["state"] == "approved" and p["token"]:
        token, p["token"] = p["token"], None
        PAIRING.pop(request.query["id"], None)
        resp = web.json_response({"state": "approved", "token": token})
        resp.set_cookie("ln_device", token, max_age=10 * 365 * 86400, httponly=True, samesite="Strict")
        return resp
    return web.json_response({"state": p["state"]})


def pair_requests():
    return [{"id": k, "code": v["code"], "name": v["name"]} for k, v in PAIRING.items()
            if v["state"] == "pending" and time.time() - v["at"] < 300]


async def api_pair_answer(request):
    """On the PC: allow or refuse a phone."""
    import secrets as _s
    body = await request.json()
    p = PAIRING.get(body.get("id", ""))
    if not p or p["state"] != "pending":
        return web.json_response({"ok": False, "error": "That request has expired."})
    if body.get("allow"):
        token = _s.token_urlsafe(32)
        devs = _devices()
        devs.append({"hash": _token_hash(token), "name": p["name"], "added": time.time(), "id": _s.token_hex(6)})
        _save_devices(devs)
        p["state"], p["token"] = "approved", token
        log(f"paired: {p['name']}")
    else:
        p["state"] = "denied"
    return web.json_response({"ok": True})


async def api_devices(request):
    """Settings: the phones paired with this PC, and removing one."""
    if request.method == "POST":
        body = await request.json()
        _save_devices([d for d in _devices() if d.get("id") != body.get("remove")])
    return web.json_response({"devices": [{"id": d.get("id"), "name": d.get("name"), "added": d.get("added")}
                                          for d in _devices()]})


# Bonjour / mDNS: with Wi-Fi sharing on, the phone app finds this PC by itself
_ZC = {}


def advertise_start():
    if not CFG.get("allow_other_devices") or _ZC.get("zc"):
        return
    try:
        import socket
        from zeroconf import ServiceInfo, Zeroconf
        ip = lan_address()
        if not ip:
            return
        host = socket.gethostname()
        info = ServiceInfo("_linernotes._tcp.local.", f"Liner Notes on {host[:40]}._linernotes._tcp.local.",
                           addresses=[socket.inet_aton(x) for x in (home_addresses() or [ip])], port=CFG["port"],
                           properties={"name": host, "v": "1"},
                           # its own name: Windows answers for "<pc>.local" itself, with every address it has (VPN included)
                           server=f"linernotes-{re.sub(r'[^A-Za-z0-9-]', '', host) or 'pc'}.local.")
        try:
            zc = Zeroconf(interfaces=home_addresses() or [ip])    # only on the home network, not out through a VPN
        except Exception:
            zc = Zeroconf()
        zc.register_service(info)
        _ZC.update(zc=zc, info=info)
        log(f"advertising on the Wi-Fi as {host} ({ip})")
    except Exception as e:
        log("couldn't advertise on the Wi-Fi:", repr(e))


def advertise_stop():
    zc = _ZC.pop("zc", None)
    if zc:
        try:
            zc.unregister_service(_ZC.pop("info"))
            zc.close()
        except Exception:
            pass


@web.middleware
async def local_only_guard(request, handler):
    if request.path.startswith(LOCAL_ONLY) and not is_local(request):
        return web.json_response({"error": "Only Liner Notes on this PC can do that."}, status=403)
    if request.remote not in ("127.0.0.1", "::1") and request.path == "/api/now":
        request.app["last_wifi_poll"] = time.time()
        request.app["last_wifi_agent"] = request.headers.get("User-Agent", "")
    return await handler(request)


def save_config_value(key, value):
    save_cfg(**{key: value})


async def api_share_wifi(request):
    """Turn Wi-Fi sharing on or off. The server has to restart to start or stop listening on the network."""
    body = await request.json()
    save_config_value("allow_other_devices", bool(body.get("on")))
    fn = request.app.get("on_restart")
    if fn:
        asyncio.get_running_loop().call_later(0.5, fn)
    return web.json_response({"ok": True, "restarting": bool(fn)})


async def api_qr(request):
    """QR code (SVG) for the Wi-Fi address, so a phone camera can open it."""
    import qrcode
    import qrcode.image.svg
    ip = lan_address()
    if not ip:
        raise web.HTTPNotFound()
    img = qrcode.make(f"http://{ip}:{CFG['port']}", image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return web.Response(text=img.to_string(encoding="unicode"), content_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})


def mic_live(app):
    """Is the microphone actually delivering sound? (The phone's only does while its page is open.)"""
    mic = app.get("mic")
    if mic is None:
        return False
    if mic.kind == "phone":
        return time.time() - (getattr(mic, "last_upload", 0) or 0) < 90
    return True


def mic_info(app):
    mic = app.get("mic")
    if not mic or mic.kind != "phone":
        return {"mode": "off"}
    if app.get("meter_until", 0) > time.time() and app.get("listen_mode") == "standby":
        return {"mode": "meter", "every": 1, "seconds": 1}            # the mic meter is open: stream in small pieces
    return {"mode": app.get("listen_mode", "off"), "every": CFG["probe_every_seconds"],
            "seconds": CFG["probe_seconds"]}


async def api_now(request):
    st: State = request.app["state"]
    if "Android" not in request.headers.get("User-Agent", ""):
        request.app["last_window_poll"] = time.time()      # a Liner Notes window on the PC is open
    else:
        request.app["last_phone_poll"] = time.time()       # the phone screen is open
        if "LinerNotesApp" in request.headers.get("User-Agent", ""):
            request.app["last_app_poll"] = time.time()     # ...in the Liner Notes app (it keeps listening with the screen off)
    return web.json_response({"version": st.version, "track": st.track, "mic": mic_info(request.app),
                              "donate": CFG.get("donate_url") or "", "update": update_info(request.app),
                              "record": request.app["record"].summary() if request.app.get("record") else None,
                              "progress": progress_now(request.app),
                              "phone_follows_pc": CFG.get("phone_follows_pc", True),
                              "phone_off_minutes": CFG.get("phone_off_minutes") or 0,
                              "phone_screen": "off" if request.app.get("phone_idle_off") else "on",
                              "pc_name": __import__("socket").gethostname()[:40],
                              "engine": "android" if ANDROID else "pc",
                              # a phone asking to pair: only Liner Notes on the PC itself is asked
                              "pair_requests": pair_requests() if request.remote in ("127.0.0.1", "::1")
                              and "Android" not in request.headers.get("User-Agent", "") else [],
                              "phone_listens": str(CFG.get("audio_device")).lower() == "phone",
                              "saved": bool(st.track and st.track.get("id") in st.db.saved_ids),
                              "media_default": CFG.get("room_medium"),
                              "follow_album": CFG.get("follow_album", True),
                              "pc_media": CFG.get("pc_media", "music"),
                              "ask_medium": bool(st.track and st.track.get("source") in ROOM_SOURCES and st.track.get("status") == "playing"
                                                 and not st.track.get("medium") and not room_medium(request.app)),
                              "discogs_ready": discogs_ready(),
                              "album_ask": request.app.get("album_ask"),
                              "album_name": request.app.get("album_name"),
                              "listen": {"mode": request.app.get("listen_mode", "off"),
                                         "paused": bool(CFG.get("listening_paused")),
                                         "has_mic": request.app.get("mic") is not None,
                                         "mic_live": mic_live(request.app),
                                         "now_state": request.app.get("listen_now_state") if (
                                             request.app.get("listen_now_at") or time.time() - (request.app.get("listen_now_done") or 0) < 6) else None,
                                         "next_try": request.app.get("next_try"), "now": time.time()}})


async def api_audio(request):
    """The phone page uploads 16 kHz mono 16-bit audio here."""
    import numpy as np
    app = request.app
    mic = app.get("mic")
    body = await request.read()
    if mic is None or mic.kind != "phone":
        return web.json_response({"mic": mic_info(app)})
    if len(body) >= 2:
        data = np.frombuffer(body[: len(body) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        mic.last_upload = time.time()
        live_feed(mic, data)
        if request.query.get("meter") == "1" and app.get("listen_mode") != "active":
            mic.meter_raw.append(data)           # the meter's live pieces: standby checks them for music each second
        elif request.query.get("probe") == "1" or app.get("listen_mode") != "active":
            mic.probes.append(data)
        else:
            mic.buf.feed(data)
    return web.json_response({"mic": mic_info(app)})


async def api_art(request):
    st: State = request.app["state"]
    item = st.art.get(request.match_info["tid"])
    if not item:
        raise web.HTTPNotFound()
    data, mime = item
    return web.Response(body=data, content_type=mime.split(";")[0])


_shazam_pages = {}


async def shazam_page(artist, title):
    """The song's page on Shazam, for songs that came from Spotify or a tracklist rather than Shazam itself."""
    key = (norm(artist), norm(title))
    if key in _shazam_pages:
        return _shazam_pages[key]
    url = None
    try:
        res = await asyncio.wait_for(shazam_client().search_track(f"{main_artist(artist)} {clean_title(title)}", limit=5), 8)
        hits = ((res or {}).get("tracks") or {}).get("hits") or (res or {}).get("hits") or []
        for h in hits:
            tr = h.get("track") or h
            if norm(clean_title(tr.get("title"))) == norm(clean_title(title)) and \
                    norm(main_artist(artist)) in norm(tr.get("subtitle") or tr.get("artist") or ""):
                url = tr.get("url") or (tr.get("share") or {}).get("href")
                break
    except Exception as e:
        log("shazam page lookup failed:", repr(e))
        return None                     # not cached: try again next time
    _shazam_pages[key] = url
    return url


async def api_info(request):
    st: State = request.app["state"]
    tid = request.query.get("id", "")
    if tid in st.info_cache:
        return web.json_response(st.info_cache[tid])
    t = st.history.get(tid)
    if not t or not t.get("artist"):
        return web.json_response({"lyrics": None, "bio": None})
    saved = st.db.cache_get("info:" + tid, 30)
    if saved is not None:
        st.info_cache[tid] = saved
        return web.json_response(saved)
    old = st.db.cache_get("info:" + tid, 100000)       # an older saved copy, kept forever as a fallback
    http = request.app["http"]
    (lyrics, synced), bio = await asyncio.gather(
        fetch_lyrics(http, t["artist"], t["title"], t.get("album"), (t.get("duration_ms") or 0) / 1000 or None),
        fetch_bio(http, t["artist"]),
    )
    shazam = None
    if not any(l.get("name") == "Shazam" for l in t.get("links") or []):
        shazam = await shazam_page(t["artist"], t["title"])
    info = {"lyrics": lyrics, "synced": synced, "bio": bio, "shazam": shazam,
            "genius": "https://genius.com/search?q=" + urllib.parse.quote(f"{main_artist(t['artist'])} {clean_title(t['title'])}")}
    st.info_cache[tid] = info
    if lyrics or bio:                  # don't keep a page that came back empty (a lookup may have failed)
        st.db.cache_put("info:" + tid, info)
    elif old:                          # the refresh found nothing (offline?): keep showing the saved page
        info = old
    return web.json_response(info)


async def api_song(request):
    """Open the song page for something played earlier (between records, the slideshow, saved songs)."""
    st: State = request.app["state"]
    body = await request.json()
    artist, title = (body.get("artist") or "").strip(), (body.get("title") or "").strip()
    if not artist or not title:
        return web.json_response({"error": "missing song"}, status=400)
    tid = make_id(artist, title)
    t = st.history.get(tid)
    if not t:
        row = st.db.last_play_of(artist, title) or {}
        album = body.get("album") or row.get("album")
        t = {"id": tid, "title": title, "artist": artist, "album": album, "art": body.get("art") or row.get("art"),
             "year": row.get("year"), "label": row.get("label"), "genre": row.get("genre"),
             "duration_ms": row.get("duration_ms"), "source": "history", "status": "playing",
             "links": search_links(artist, title, album)}
        st.history[tid] = t
    return web.json_response(t)


def log_keys():
    """Which built-in keys this copy has (never the keys themselves)."""
    have = lambda *v: "yes" if all(v) else "no"
    log(f"built-in keys: Discogs {have(DISCOGS_KEY, DISCOGS_SECRET)}, Spotify {have(_BUILTIN_SPOTIFY_ID)}, "
        f"Last.fm {have(LASTFM_KEY, LASTFM_SECRET)}" + (f" (couldn't read them: {SECRETS_ERROR})" if SECRETS_ERROR else ""))


async def on_startup(app):
    log_keys()
    app["http"] = ClientSession(timeout=ClientTimeout(total=10))
    if app["demo"]:
        app["tasks"] = [asyncio.create_task(demo_loop(app))]
    else:
        if ANDROID:
            app["tasks"] = [asyncio.create_task(turntable_loop(app)),
                            asyncio.create_task(lastfm_loop(app)),
                            asyncio.create_task(collection_loop(app))]
            return
        app["tasks"] = [asyncio.create_task(pc_media_loop(app)),
                        asyncio.create_task(turntable_loop(app)),
                        asyncio.create_task(update_loop(app)),
                        asyncio.create_task(lastfm_loop(app)),
                        asyncio.create_task(phone_off_loop(app)),
                        asyncio.create_task(usb_phone_loop(app)),
                        asyncio.create_task(collection_loop(app))]
        await asyncio.to_thread(advertise_start)      # so the phone app finds this PC on the Wi-Fi


async def on_cleanup(app):
    advertise_stop()
    for t in app["tasks"]:
        t.cancel()
    await app["http"].close()


def build_app(demo=False):
    app = web.Application(middlewares=[local_only_guard])
    app["state"] = State()
    app["state"].app = app
    app["state"].db = History(os.path.join(DATA_DIR, "history.db"))
    app["demo"] = demo
    app.router.add_get("/", index)
    app.router.add_get("/api/now", api_now)
    app.router.add_get("/api/info", api_info)
    app.router.add_get("/api/art/{tid}", api_art)
    app.router.add_post("/api/audio", api_audio)
    app.router.add_get("/manifest.json", manifest)
    app.router.add_get("/api/search", api_search)
    app.router.add_post("/api/pick", api_pick)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/icon-192.png", icon_file)
    app.router.add_get("/icon-512.png", icon_file)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/api/update", api_update)
    app.router.add_post("/api/window", api_window)
    app.router.add_post("/api/window/fullscreen", api_window_fullscreen)
    app.router.add_post("/api/screen/idle", api_screen_idle)
    app.router.add_get("/api/records/search", api_records_search)
    app.router.add_get("/api/records/guess", api_records_guess)
    app.router.add_post("/api/records/load", api_records_load)
    app.router.add_post("/api/records/position", api_records_position)
    app.router.add_post("/api/records/clear", api_records_clear)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/save", api_save)
    app.router.add_get("/api/saved", api_saved)
    app.router.add_get("/api/idle", api_idle)
    app.router.add_post("/api/listen-now", api_listen_now)
    app.router.add_get("/api/mic/level", api_mic_level)
    app.router.add_get("/api/mic/list", api_mic_list)
    app.router.add_post("/api/mic/select", api_mic_select)
    app.router.add_get("/api/mic/last-clip.wav", api_last_clip)
    app.router.add_get("/api/credits", api_credits)
    app.router.add_get("/api/similar", api_similar)
    app.router.add_post("/api/song", api_song)
    app.router.add_post("/api/medium", api_medium)
    app.router.add_get("/api/stylus", api_stylus)
    app.router.add_get("/api/shelf", api_shelf)
    app.router.add_get("/api/collection", api_collection)
    app.router.add_post("/api/collection", api_collection)
    app.router.add_get("/api/market", api_market)
    app.router.add_get("/api/share", api_share)
    app.router.add_post("/api/pair/start", api_pair_start)
    app.router.add_get("/api/pair/status", api_pair_status)
    app.router.add_post("/api/pair/answer", api_pair_answer)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/devices", api_devices)
    app.router.add_get("/api/discogs/pressings", api_discogs_pressings)
    app.router.add_post("/api/discogs/add", api_discogs_add)
    app.router.add_post("/api/album/name", api_album_name)
    app.router.add_post("/api/album/ask", api_album_ask)
    app.router.add_post("/api/shelf/hide", api_shelf_hide)
    app.router.add_post("/api/stylus", api_stylus)
    app.router.add_get("/api/recap", api_recap)
    app.router.add_get("/api/recap.png", api_recap_png)
    app.router.add_post("/api/recap/save", api_recap_save)
    app.router.add_get("/api/spotify/status", api_spotify_status)
    app.router.add_get("/api/lastfm/status", api_lastfm_status)
    app.router.add_post("/api/lastfm/connect", api_lastfm_connect)
    app.router.add_get("/api/lastfm/callback", api_lastfm_callback)
    app.router.add_post("/api/lastfm/disconnect", api_lastfm_disconnect)
    app.router.add_post("/api/spotify/connect", api_spotify_connect)
    app.router.add_get("/api/spotify/callback", api_spotify_callback)
    app.router.add_post("/api/spotify/disconnect", api_spotify_disconnect)
    app.router.add_get("/api/phone/status", api_phone_status)
    app.router.add_post("/api/phone/install", api_phone_install)
    app.router.add_post("/api/phone/use-mic", api_phone_use_mic)
    app.router.add_post("/api/share-wifi", api_share_wifi)
    app.router.add_get("/api/qr.svg", api_qr)
    app.router.add_post("/api/update", api_update)
    app.router.add_get("/icon.svg", icon)
    app.router.add_get("/logo.svg", logo_svg)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def bind_host():
    return "0.0.0.0" if CFG["allow_other_devices"] else "127.0.0.1"


# Adapters that aren't the home network: VPNs (their address is what "the way to the internet" finds while
# one is on), virtual machines, Bluetooth. A phone on the Wi-Fi can't reach this PC at those addresses.
NOT_LAN = re.compile(r"vpn|wireguard|surfshark|nord|express|proton|mullvad|private internet|pia|cyberghost|"
                     r"windscribe|tailscale|zerotier|hamachi|openvpn|tap|tap-|tun|wintun|fortinet|cisco|"
                     r"virtual|vethernet|hyper-v|vmware|virtualbox|wsl|docker|loopback|bluetooth", re.I)


def home_addresses():
    """This PC's addresses on the home network (Wi-Fi or ethernet), most likely first."""
    try:
        import ifaddr          # comes with zeroconf
    except ImportError:
        return []
    found = []
    for a in ifaddr.get_adapters():
        if NOT_LAN.search(a.nice_name or "") or NOT_LAN.search(a.name or ""):
            continue
        for i in a.ips:
            ip = i.ip if isinstance(i.ip, str) else None
            if not ip or ip.startswith(("127.", "169.254.")):
                continue
            o = [int(x) for x in ip.split(".")]
            rank = 0 if o[0] == 192 and o[1] == 168 else 1 if o[0] == 172 and 16 <= o[1] <= 31 else 2 if o[0] == 10 else None
            if rank is not None:              # home networks use private addresses
                found.append((rank, ip))
    found = [ip for _, ip in sorted(found)]
    # A disconnected adapter can keep its old address: keep the ones Windows actually uses for their network
    import socket
    live = []
    for ip in found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((".".join(ip.split(".")[:3] + ["1"]), 9))        # nothing is sent; this only asks for the route
            if s.getsockname()[0] == ip:
                live.append(ip)
            s.close()
        except Exception:
            pass
    return live or found


def lan_address():
    if not CFG["allow_other_devices"]:
        return None
    home = home_addresses()
    if home:
        return home[0]
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


async def serve(app):
    """Start the server inside an already-running event loop (used by the tray app)."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, bind_host(), CFG["port"]).start()
    return runner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"{i:3d}  {d['name']}")
        return

    app = build_app(args.demo)
    log(f"{APP_NAME} running at http://localhost:{CFG['port']}")
    lan = lan_address()
    if lan:
        log(f"On your phone, open http://{lan}:{CFG['port']}")
    web.run_app(app, host=bind_host(), port=CFG["port"], print=None)

if __name__ == "__main__":
    main()
