"""herdr-onair core: paths, settings, station sources, the mpv player and liked songs.

Everything here is Python's standard library; playback needs mpv. The popup UI is in app.py and
the command line in bin/onair.
"""
import json
import os
import re
import socket
import threading
import time
import unicodedata

# Network, hashing and process modules are imported inside the functions that use them: herdr runs
# the top-bar line (bin/now-playing) every second, and loading them each time cost ~30 ms of CPU.

VERSION = "0.1.0"
PLUGIN_ID = "herdr-onair"
USER_AGENT = f"herdr-onair/{VERSION}"

HOME = os.path.expanduser("~")
ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "herdr", "plugins", "config", PLUGIN_ID)
STATE = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "herdr", "plugins", PLUGIN_ID)
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), PLUGIN_ID)
SOCK = os.path.join(STATE, "mpv.sock")
if len(SOCK.encode()) > 100:  # unix socket paths max out around 104-108 bytes
    SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"herdr-onair-{os.getuid()}.sock")
STATE_FILE = os.path.join(STATE, "state.json")
APP_PID = os.path.join(STATE, "app.pid")
ROOT_FILE = os.path.join(STATE, "root")  # where the plugin lives now; read by the installed commands

DEFAULTS = {
    "accent": "cyan",                         # a terminal colour name, so the theme decides the shade
    "likes_dir": "~/Music/Radio likes",       # one sorted .txt per station + .likes.json
    "min_bitrate": "64",                      # hide Radio Browser streams below this (kbps); 0 = all
    "bar_icons": "nerd",                      # top bar marks: nerd (Nerd Font) or plain
    "volume_step": "5",
    "layout": "spotlight",                    # spotlight, classic, framed, sidebar or cards (v cycles)
    "visualizer": "auto",                     # cava bars: auto (when cava is installed) or off (c toggles)
    "popup_width": "72%",                     # size of the main popup: cells (90) or % of herdr's window
    "popup_height": "80%",
    "setup_done": "",                         # set by the setup screen; empty = show it on first open
}


# ---------------------------------------------------------------- settings / state / files

def settings():
    vals = dict(DEFAULTS)
    try:
        with open(os.path.join(CONFIG, "settings.conf"), encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
    except OSError:
        pass
    return vals


def setting(key):
    return settings().get(key, DEFAULTS.get(key, ""))


def set_setting(key, value):
    """Write one setting into settings.conf, keeping the rest (and its comments)."""
    path = os.path.join(CONFIG, "settings.conf")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        try:
            with open(os.path.join(ROOT, "settings.conf.example"), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    pat = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.append(f"{key} = {value}")
    os.makedirs(CONFIG, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)


def int_setting(key):
    try:
        return int(setting(key))
    except ValueError:
        return int(DEFAULTS[key])


def likes_dir():
    return os.path.expanduser(setting("likes_dir") or DEFAULTS["likes_dir"])


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


_state_lock = threading.Lock()


def load_state():
    st = read_json(STATE_FILE, {})
    st.setdefault("volume", 60)
    st.setdefault("recent", [])
    return st


def update_state(**changes):
    with _state_lock:
        st = load_state()
        st.update(changes)
        write_json(STATE_FILE, st)
        return st


def remember_root():
    """Record the plugin folder when herdr runs us (it moves when the plugin is updated)."""
    if not os.environ.get("HERDR_PLUGIN_ROOT"):
        return
    try:
        with open(ROOT_FILE) as f:
            if f.read().strip() == ROOT:
                return
    except OSError:
        pass
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(ROOT_FILE, "w") as f:
            f.write(ROOT + "\n")
    except OSError:
        pass


WRAPPER = """#!/bin/sh
# {name}: herdr-onair's {what}, written by `onair install-commands`.
# Finds the plugin through the folder herdr-onair records whenever herdr runs it, so this keeps
# working after plugin updates.
root=$(cat "${{XDG_STATE_HOME:-$HOME/.local/state}}/herdr/plugins/herdr-onair/root" 2>/dev/null)
[ -f "$root/bin/{target}" ] || root='{fallback}'
exec python3 "$root/bin/{target}" "$@"
"""


def install_commands(directory):
    """Write `onair` and `onair-now-playing` into directory (e.g. ~/.local/bin). Returns their paths."""
    os.makedirs(directory, exist_ok=True)
    out = []
    for name, target, what in (("onair", "onair", "command line"), ("onair-now-playing", "now-playing", "top-bar line")):
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write(WRAPPER.format(name=name, what=what, target=target, fallback=ROOT.replace("'", "")))
        os.chmod(path, 0o755)
        out.append(path)
    return out


# ---------------------------------------------------------------- HTTP with a small cache

def fetch(url, timeout=6):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def cached_fetch(name, url, max_age, timeout=6):
    """Bytes of url, from CACHE/name if younger than max_age s; a stale copy if the fetch fails."""
    path = os.path.join(CACHE, name)
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    try:
        data = fetch(url, timeout)
        os.makedirs(CACHE, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return data
    except Exception:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None


# ---------------------------------------------------------------- stations
#
# A station is a plain dict:
#   key       "rb:<stationuuid>" (Radio Browser) or "my:<hash>" (added by hand)
#   source    "Radio Browser" or "My stations"
#   name, url (stream or .pls/.m3u playlist), bitrate (kbps, 0 = unknown), codec, country, tags,
#   description, popularity text, homepage
#
# SomaFM is left out on purpose: its terms don't allow third-party apps to use its streams, station
# list or metadata (https://somafm.com/contact/tos.html). People can still add any stream link they
# are allowed to use personally to My stations.

def genre_group(station):
    """A short genre label: the station's first tag."""
    tags = station.get("tags") or []
    return tags[0].capitalize() if tags else ""


MY_FILE = os.path.join(CONFIG, "my-stations.json")


def load_my():
    return read_json(MY_FILE, [])


def save_my(stations):
    write_json(MY_FILE, stations)


def custom_station(url, name=""):
    import hashlib
    import urllib.parse
    url = url.strip()
    host = urllib.parse.urlsplit(url).netloc or url
    key = "my:" + hashlib.sha1(url.encode()).hexdigest()[:12]
    return {"key": key, "source": "My stations", "name": (name or host).strip(), "url": url, "bitrate": 0,
            "codec": "", "country": "", "tags": [], "description": "", "popularity": "", "homepage": ""}


def is_mine(station):
    keys = {s.get("key") for s in load_my()}
    urls = {s.get("url") for s in load_my()}
    return station.get("key") in keys or station.get("url") in urls


def toggle_mine(station):
    """Add station to My stations, or remove it. Returns True when it's now saved."""
    mine = load_my()
    kept = [s for s in mine if s.get("key") != station.get("key") and s.get("url") != station.get("url")]
    if len(kept) != len(mine):
        save_my(kept)
        return False
    save_my(mine + [dict(station)])
    return True


def my_matches(query):
    words = query.casefold().split()
    return [s for s in load_my()
            if all(w in " ".join([s.get("name", ""), " ".join(s.get("tags", [])), s.get("url", "")]).casefold()
                   for w in words)]


_rb_server = None


def _rb_servers():
    """Radio Browser API hosts. The project asks clients to find them through DNS, not hard-code one."""
    import concurrent.futures
    import random
    names = []
    try:
        infos = socket.getaddrinfo("all.api.radio-browser.info", 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            for name in pool.map(lambda ip: _reverse(ip), ips):
                if name and name not in names:
                    names.append(name)
    except OSError:
        pass
    random.shuffle(names)
    for fallback in ("de1.api.radio-browser.info", "fi1.api.radio-browser.info", "nl1.api.radio-browser.info"):
        if fallback not in names:
            names.append(fallback)
    return names


def _reverse(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return None


def rb_get(path, params, timeout=6):
    """GET a Radio Browser API path on a working server (remembered for the session)."""
    global _rb_server
    import urllib.parse
    query = urllib.parse.urlencode(params)
    servers = [_rb_server] if _rb_server else []
    servers += [s for s in _rb_servers() if s != _rb_server]
    last = None
    for host in servers[:4]:
        try:
            data = fetch(f"https://{host}{path}?{query}", timeout)
            _rb_server = host
            return json.loads(data)
        except Exception as e:  # try the next server
            last = e
    raise OSError(f"Radio Browser unreachable ({last})")


def _rb_station(s):
    tags = [t.strip() for t in (s.get("tags") or "").split(",") if t.strip()]
    return {
        "key": f"rb:{s.get('stationuuid')}", "source": "Radio Browser", "uuid": s.get("stationuuid"),
        "name": (s.get("name") or "").strip() or "(unnamed)", "url": s.get("url_resolved") or s.get("url"),
        "bitrate": int(s.get("bitrate") or 0),
        "codec": "" if (s.get("codec") or "").upper() == "UNKNOWN" else (s.get("codec") or "").upper(),
        "country": (s.get("countrycode") or "").upper(), "tags": tags[:8], "description": "",
        "popularity": f"{s.get('votes', 0)} votes", "votes": int(s.get("votes") or 0),
        "homepage": s.get("homepage") or "",
    }


def radiobrowser_search(query, limit=40):
    """Stations whose name or tag matches query, most voted first, broken and low-bitrate ones left out."""
    import concurrent.futures
    base = {"hidebroken": "true", "order": "votes", "reverse": "true", "limit": str(limit)}
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(rb_get, "/json/stations/search", dict(base, name=query)),
                pool.submit(rb_get, "/json/stations/search", dict(base, tag=query))]
        rows, errors = [], []
        for j in jobs:
            try:
                rows += j.result()
            except Exception as e:
                errors.append(e)
    if errors and not rows:
        raise errors[0]
    return _rb_filter(rows)


def is_somafm(raw):
    text = " ".join(str(raw.get(k) or "") for k in ("url", "url_resolved", "homepage", "name")).casefold()
    return "somafm" in text or "soma fm" in text


def _rb_filter(rows):
    import urllib.parse
    min_kbps = int_setting("min_bitrate")
    seen, out = set(), []
    for s in sorted(rows, key=lambda s: -int(s.get("votes") or 0)):
        st = _rb_station(s)
        url = st["url"] or ""
        ident = (st["name"].casefold(), urllib.parse.urlsplit(url).netloc)
        if not url or st["key"] in seen or ident in seen or is_somafm(s):
            continue
        if min_kbps and st["bitrate"] and st["bitrate"] < min_kbps:
            continue
        seen.update((st["key"], ident))
        out.append(st)
    return out


def radiobrowser_popular(limit=40):
    """The most-voted working stations (cached for a day): the home screen before you search."""
    path = os.path.join(CACHE, "popular.json")
    try:
        if time.time() - os.path.getmtime(path) < 86400:
            return read_json(path, [])
    except OSError:
        pass
    try:
        rows = rb_get("/json/stations/search", {"hidebroken": "true", "order": "votes", "reverse": "true",
                                                 "limit": str(limit * 2)})
        out = _rb_filter(rows)[:limit]
        write_json(path, out)
        return out
    except Exception:
        return read_json(path, [])  # offline: yesterday's list beats nothing


def rb_count_click(station):
    """Tell Radio Browser a station was played (its popularity ranking counts clicks)."""
    if station.get("source") == "Radio Browser" and station.get("uuid"):
        threading.Thread(target=lambda: _quiet(rb_get, f"/json/url/{station['uuid']}", {}), daemon=True).start()


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def resolve_stream(station):
    """A direct stream URL for station. For .pls/.m3u playlist links: their first entry (kept a week)."""
    url = station.get("url") or ""
    if not re.search(r"\.(pls|m3u8?)(\?|$)", url, re.I) or url.lower().endswith(".m3u8"):
        return url
    cache = read_json(os.path.join(CACHE, "streams.json"), {})
    hit = cache.get(url)
    if hit and time.time() - hit.get("t", 0) < 7 * 86400:
        return hit["url"]
    try:
        text = fetch(url, 5).decode("utf-8", "replace")
        m = re.search(r"^File1=(\S+)", text, re.M) or re.search(r"^(https?://\S+)", text, re.M)
        if m:
            cache[url] = {"url": m.group(1), "t": time.time()}
            write_json(os.path.join(CACHE, "streams.json"), cache)
            return m.group(1)
    except Exception:
        pass
    return hit["url"] if hit else url  # mpv can open the playlist itself as a last resort


# ---------------------------------------------------------------- mpv player

class PlayerError(Exception):
    pass


class Player:
    """Talks to one mpv process (started on demand, detached) over its JSON IPC socket."""

    def __init__(self, sock=SOCK):
        self.sock = sock
        self._id = 0

    def _request(self, args, timeout=2.0):
        self._id += 1
        rid = self._id
        try:
            c = socket.socket(socket.AF_UNIX)
            c.settimeout(timeout)
            c.connect(self.sock)
        except OSError as e:
            raise PlayerError("mpv isn't running") from e
        try:
            c.sendall((json.dumps({"command": args, "request_id": rid}) + "\n").encode())
            buf = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    raise PlayerError("mpv closed the connection")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == rid:
                        return msg
        except OSError as e:
            raise PlayerError(str(e)) from e
        finally:
            c.close()

    def command(self, *args):
        msg = self._request(list(args))
        if msg.get("error") != "success":
            raise PlayerError(msg.get("error", "error"))
        return msg.get("data")

    def get(self, prop, default=None):
        try:
            return self.command("get_property", prop)
        except PlayerError:
            return default

    def running(self):
        try:
            self._request(["get_property", "pid"], timeout=0.5)
            return True
        except PlayerError:
            return False

    def start(self, volume):
        import subprocess
        if self.running():
            return
        os.makedirs(STATE, exist_ok=True)
        try:
            os.unlink(self.sock)
        except OSError:
            pass
        args = ["mpv", "--no-config", "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
                f"--input-ipc-server={self.sock}", f"--volume={volume}", "--volume-max=100",
                "--title=herdr-onair", f"--user-agent={USER_AGENT}", "--network-timeout=15",
                "--cache=yes", "--demuxer-readahead-secs=10",
                "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=30"]
        if os.environ.get("ONAIR_AO"):  # tests: ONAIR_AO=null plays silently
            args.append(f"--ao={os.environ['ONAIR_AO']}")
        try:
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except FileNotFoundError as e:
            raise PlayerError("mpv isn't installed") from e
        for _ in range(50):
            time.sleep(0.1)
            if self.running():
                return
        raise PlayerError("mpv didn't start")

    def status(self):
        """{'playing': bool, 'paused': bool, 'title': ICY song title or '', 'volume': int}; {} if mpv is off."""
        if not self.running():
            return {}
        idle = self.get("idle-active", True)
        meta = self.get("metadata") or {}
        title = ""
        if isinstance(meta, dict):
            title = meta.get("icy-title") or meta.get("title") or ""
        return {
            "playing": not idle,
            "paused": bool(self.get("pause", False)),
            "title": title.strip(),
            "volume": int(round(self.get("volume", 0) or 0)),
            "buffering": bool(self.get("paused-for-cache", False)),
        }


# ---------------------------------------------------------------- playback commands (UI + CLI)

def play(station, station_list=None):
    """Play station; station_list (the list it was picked from) is what next/previous step through."""
    st = load_state()
    p = Player()
    p.start(st.get("volume", 60))
    url = resolve_stream(station)
    p.command("loadfile", url, "replace")
    p.command("set_property", "pause", False)
    wait_for_start(p, station)
    if station.get("source") == "My stations" and not station.get("bitrate"):
        learn_quality(p, station)
    recent = [station] + [s for s in st.get("recent", []) if s.get("key") != station.get("key")]
    changes = {"station": station, "recent": recent[:12]}
    if station_list is not None:
        changes["list"] = station_list[:200]
    update_state(**changes)
    rb_count_click(station)


def wait_for_start(p, station, timeout=12.0):
    """Block until the stream plays; PlayerError if mpv gives up on it (offline station, bad link)."""
    end = time.time() + timeout
    time.sleep(0.4)
    while time.time() < end:
        if p.get("idle-active", True):
            raise PlayerError(f"{station.get('name', 'This station')} didn't start (it may be offline). Try another one.")
        if p.get("audio-bitrate") or p.get("time-pos") is not None and p.get("audio-params"):
            return
        time.sleep(0.3)
    # still connecting after the timeout: leave it to mpv, it keeps trying


def learn_quality(p, station):
    """Stations added by hand have no bitrate or format yet: note them from the stream once it plays."""
    for _ in range(10):
        kbps = int((p.get("audio-bitrate") or 0) / 1000)
        if kbps:
            break
        time.sleep(0.3)
    codec = (p.get("audio-codec-name") or "").upper().replace("MP3FLOAT", "MP3")
    if not kbps and not codec:
        return
    station.update(bitrate=kbps, codec=codec[:5])
    mine = load_my()
    for m in mine:
        if m.get("key") == station.get("key"):
            m.update(bitrate=kbps, codec=codec[:5])
    save_my(mine)


def toggle_pause():
    p = Player()
    s = p.status()
    if s.get("playing"):
        p.command("cycle", "pause")
        return
    st = load_state()  # nothing loaded (fresh start or after stop): play the last station again
    if st.get("station"):
        play(st["station"])


def stop():
    p = Player()
    if p.running():
        try:
            p.command("quit")
        except PlayerError:
            pass


def step(delta):
    """Next (+1) or previous (-1) station in the list the current one was picked from, wrapping."""
    st = load_state()
    lst = st.get("list") or st.get("recent") or []
    if not lst:
        return None
    keys = [s.get("key") for s in lst]
    cur = st.get("station", {}).get("key")
    i = keys.index(cur) if cur in keys else (-1 if delta > 0 else 0)
    nxt = lst[(i + delta) % len(lst)]
    play(nxt)
    return nxt


def change_volume(delta):
    st = load_state()
    vol = max(0, min(100, int(st.get("volume", 60)) + delta))
    update_state(volume=vol)
    p = Player()
    if p.running():
        p.command("set_property", "volume", vol)
    return vol


def now():
    """(station dict or None, mpv status dict)."""
    s = Player().status()
    return (load_state().get("station") if s.get("playing") else None), s


# ---------------------------------------------------------------- liked songs

def _likes_file():
    return os.path.join(likes_dir(), ".likes.json")


def load_likes():
    return read_json(_likes_file(), [])


def split_title(raw):
    """'Artist - Title' (the usual stream format) -> (artist, title); otherwise ('', raw)."""
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            a, t = raw.split(sep, 1)
            return a.strip(), t.strip()
    return "", raw.strip()


def song_of(station, raw):
    """The song in a stream title, or "" when the station only sends its own name or slogan."""
    raw = (raw or "").strip()
    if not raw or not station:
        return ""
    name = station.get("name", "").casefold()
    low = raw.casefold()
    if low in name or name in low:  # "Intense Radio - We love Dance" on "Intense Radio - We love Dance 256K"
        return ""
    artist, title = split_title(raw)
    if artist and artist.casefold() in name and title.casefold() in name:
        return ""
    return raw


def sort_key(like):
    return (like.get("artist") or like.get("title") or "").casefold(), (like.get("title") or "").casefold()


def safe_name(name):
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', " ", name).strip(" .") or "Station"
    return re.sub(r"\s+", " ", name)[:80]


def station_file(station_name):
    return os.path.join(likes_dir(), safe_name(station_name) + ".txt")


def write_station_list(station_name, likes=None):
    """Rewrite one station's sorted list (or remove it when it has no likes left)."""
    likes = load_likes() if likes is None else likes
    songs = sorted((l for l in likes if l.get("station") == station_name), key=sort_key)
    path = station_file(station_name)
    if not songs:
        try:
            os.unlink(path)
        except OSError:
            pass
        return path
    lines = [f"# {station_name}: {len(songs)} liked song{'s' if len(songs) != 1 else ''}, sorted by artist",
             f"# herdr-onair keeps this file up to date; liked songs live in {os.path.basename(_likes_file())}", ""]
    for l in songs:
        song = f"{l['artist']} - {l['title']}" if l.get("artist") else l.get("title", "")
        if l.get("album"):
            song += f"  [{l['album']}]"
        lines.append(song)
    os.makedirs(likes_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def like_current():
    """Like the song playing now. Returns (like dict, 'liked' | 'already' | reason it couldn't)."""
    station, s = now()
    if not station:
        return None, "nothing is playing"
    raw = song_of(station, s.get("title"))
    if not raw:
        return None, "this station isn't sending song titles right now"
    artist, title = split_title(raw)
    album = ""
    likes = load_likes()
    for l in likes:
        if l.get("station") == station["name"] and l.get("raw", "").casefold() == raw.casefold():
            return l, "already"
    like = {"artist": artist, "title": title, "album": album, "raw": raw, "station": station["name"],
            "source": station.get("source", ""), "station_key": station.get("key", ""),
            "liked": time.strftime("%Y-%m-%d %H:%M")}
    likes.append(like)
    write_json(_likes_file(), likes)
    write_station_list(station["name"], likes)
    return like, "liked"


def unlike(like):
    likes = [l for l in load_likes()
             if not (l.get("station") == like.get("station") and l.get("raw") == like.get("raw"))]
    write_json(_likes_file(), likes)
    write_station_list(like.get("station", ""), likes)


def is_liked(station_name, raw):
    raw = (raw or "").casefold()
    return any(l.get("station") == station_name and l.get("raw", "").casefold() == raw for l in load_likes())


# ---------------------------------------------------------------- text width (for CJK / emoji names)

def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(s):
    return sum(char_width(c) for c in s)


def fit(s, width, ellipsis="…"):
    """s cut to at most width cells (with an ellipsis when cut), padded with spaces to width."""
    s = "".join(ch for ch in s if ch.isprintable())
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s + " " * (width - text_width(s))
    out, w = "", 0
    for ch in s:
        cw = char_width(ch)
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + ellipsis + " " * (width - w - 1)


# ---------------------------------------------------------------- top bar

def now_line():
    """One line for herdr's top bar: "▶ Title · Artist ♥  󰐹 Station", or "" when nothing plays."""
    station, st = now()
    if not station:
        return ""
    nerd = setting("bar_icons") != "plain"
    mark = ("\U000f03e4" if nerd else "‖") if st.get("paused") else ("\U000f040a" if nerd else "▶")
    radio = "\U000f0439" if nerd else "·"
    raw = song_of(station, st.get("title"))
    artist, title = split_title(raw)
    song = f"{title} · {artist}" if artist else title
    if len(song) > 36:
        song = song[:35].rstrip(" ·") + "…"
    name = station["name"] if len(station["name"]) <= 24 else station["name"][:23] + "…"
    heart = " ♥" if raw and is_liked(station["name"], raw) else ""
    return f"{mark} {song}{heart}  {radio} {name}" if song else f"{mark} {radio} {name}"
