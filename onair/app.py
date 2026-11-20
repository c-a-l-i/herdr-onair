"""herdr-onair's popup: search stations, play one, like songs, browse liked songs (curses).

Keys (shown in the footer):
  /         search Radio Browser + My stations (enter: done, esc: clear)
  enter     play the highlighted station          space  pause / resume
  f         save to / remove from My stations      a      add a stream link to My stations
  l         like the song playing now              n / b  next / previous station
  + / -     volume                                 x      stop
  tab       stations <-> liked songs               d d    remove a liked song
  [ / ]     previous / next section                v      next layout
  c         visualizer on / off (needs cava)
  q / esc   close the popup (the radio keeps playing)

Layouts (v cycles, `layout` in settings.conf picks the default):
  spotlight  the station on air large at the top, one wide list below (default)
  classic    list + details side by side
  framed     rounded panels, now playing on top with a volume bar
  sidebar    sections on the left, list, details, a player bar at the bottom
  cards      stations as tiles with what's on air
"""
import curses
import os
import re
import shutil
import signal
import subprocess
import textwrap
import threading
import time
import urllib.parse

from . import core

LAYOUTS = ["spotlight", "classic", "framed", "sidebar", "cards"]
BLOCKS = " ▁▂▃▄▅▆▇█"
ICONS = {"play": "▶", "pause": "‖", "heart": "♥", "sel": "▌", "note": "♪", "search": "⌕"}
COLORS = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}


class Visualizer:
    """cava in raw mode on a pipe: the newest frame of bar heights (0..RANGE), lowest notes first.

    cava listens to what the computer plays (its default input), so other sounds show up too.
    """
    BARS = 64
    RANGE = 64

    def __init__(self):
        self.frame = []
        self.proc = None

    @staticmethod
    def available():
        return shutil.which("cava") is not None

    def start(self):
        if self.proc or not self.available():
            return bool(self.proc)
        conf = os.path.join(core.STATE, "cava.conf")
        os.makedirs(core.STATE, exist_ok=True)
        with open(conf, "w") as f:
            f.write(f"""# written by herdr-onair for its visualizer
[general]
bars = {self.BARS}
framerate = 30
[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = {self.RANGE}
bar_delimiter = 59
frame_delimiter = 10
channels = mono
""")
        try:
            self.proc = subprocess.Popen(["cava", "-p", conf], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            return False
        threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
        return True

    def read(self, proc):
        for line in proc.stdout:
            try:
                self.frame = [int(v) for v in line.split(b";") if v.strip()]
            except ValueError:
                pass
        if proc is self.proc:
            self.proc = None

    def stop(self):
        proc, self.proc = self.proc, None
        self.frame = []
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


class App:
    def __init__(self, scr):
        self.scr = scr
        self.lock = threading.RLock()
        self.view = "stations"          # or "likes"
        self.mode = "normal"            # or "search"
        self.query = ""
        self.typed_at = 0.0
        self.searched = ""
        self.search_seq = 0
        self.searching = False
        self.rb_results = []
        self.rb_error = ""
        self.popular = []
        self.popular_loaded = False
        self.prompt = None              # adding a stream link: {"step": "url"|"name", "url", "text"}
        self.rows = []                  # dicts: {"header": text} or {"station": st} or {"like": l}
        self.sel = 0
        self.top = 0
        self.like_rows = []
        self.like_sel = 0
        self.like_top = 0
        self.card_top = 0
        self.grid = {}                  # cards layout: row index -> (line, col) as last drawn
        self.now_station = None
        self.now = {}
        self.liked_now = False
        self.msg = ""
        self.msg_until = 0.0
        self.confirm = None
        self.alive = True
        self.dirty = True               # something besides the bars changed: redraw everything
        self.viz_rect = None            # where the bars went in the last full draw
        state = core.load_state()
        layout = state.get("layout") or core.setting("layout")
        self.layout = layout if layout in LAYOUTS else "spotlight"
        self.vol = int(state.get("volume", 60))
        self.likes_count = len(core.load_likes())
        self.my_keys = {m.get("key") for m in core.load_my()}
        self.viz = Visualizer()
        self.viz_on = (core.setting("visualizer") != "off" and state.get("visualizer", True)
                       and Visualizer.available())

    # ------------------------------------------------------------ setup

    def colors(self):
        curses.curs_set(0)
        try:
            curses.start_color()
            curses.use_default_colors()
            name = core.setting("accent").lower()
            bright = name.startswith("bright-")
            base = COLORS.get(name.replace("bright-", ""), 6)
            if bright and curses.COLORS >= 16:
                base += 8
            curses.init_pair(1, -1 if name == "none" else base, -1)
            curses.init_pair(2, 1, -1)
        except curses.error:
            pass
        self.ACC = curses.color_pair(1)
        self.ERR = curses.color_pair(2)
        self.DIM = curses.A_DIM
        self.BOLD = curses.A_BOLD

    def start_threads(self):
        threading.Thread(target=self.load_popular, daemon=True).start()
        threading.Thread(target=self.poll_loop, daemon=True).start()

    def load_popular(self):
        stations = core.radiobrowser_popular()
        with self.lock:
            self.popular, self.popular_loaded, self.dirty = stations, True, True
            if not self.query:
                self.build_rows(keep=True)

    def poll_loop(self):
        while self.alive:
            try:
                station, st = core.now()
                song = core.song_of(station, st.get("title")) if station else ""
                liked = bool(song and core.is_liked(station["name"], song))
            except Exception:
                station, st, liked = None, {}, False
            try:
                vol, likes = int(core.load_state().get("volume", 60)), len(core.load_likes())
                mine = {m.get("key") for m in core.load_my()}
            except Exception:
                vol, likes, mine = self.vol, self.likes_count, self.my_keys
            with self.lock:
                if (station, st, liked, vol, likes, mine) != (self.now_station, self.now, self.liked_now,
                                                             self.vol, self.likes_count, self.my_keys):
                    self.dirty = True
                self.now_station, self.now, self.liked_now = station, st, liked
                self.vol, self.likes_count, self.my_keys = vol, likes, mine
            time.sleep(1.0)

    # ------------------------------------------------------------ lists

    def home_rows(self):
        rows = []
        mine = core.load_my()
        if mine:
            rows.append({"header": f"MY STATIONS · {len(mine)}", "nav": "My stations", "count": len(mine)})
            rows += [{"station": s} for s in mine]
        recent = core.load_state().get("recent", [])[:8]
        if recent:
            rows.append({"header": "RECENTLY PLAYED", "nav": "Recently played"})
            rows += [{"station": s} for s in recent]
        if self.popular:
            rows.append({"header": f"POPULAR ON RADIO BROWSER · {len(self.popular)}", "nav": "Popular",
                         "count": len(self.popular)})
            rows += [{"station": s} for s in self.popular]
        else:
            rows.append({"header": "POPULAR ON RADIO BROWSER · " + ("loading…" if not self.popular_loaded
                                                                    else "couldn't load (offline?)"),
                         "nav": "Popular"})
        return rows

    def search_rows(self):
        rows = []
        mine = core.my_matches(self.query)
        if mine:
            rows.append({"header": f"MY STATIONS · {len(mine)}", "nav": "My stations", "count": len(mine)})
            rows += [{"station": s} for s in mine]
        if self.searching:
            head = "RADIO BROWSER · searching…"
        elif self.rb_error:
            head = "RADIO BROWSER · couldn't search (offline?)"
        else:
            head = f"RADIO BROWSER · {len(self.rb_results)}"
        rows.append({"header": head, "nav": "Radio Browser", "count": len(self.rb_results)})
        rows += [{"station": s} for s in self.rb_results]
        return rows

    def build_rows(self, keep=False):
        old = self.selected_station()
        self.rows = self.search_rows() if self.query else self.home_rows()
        self.sel = 0
        if keep and old:
            for i, r in enumerate(self.rows):
                if r.get("station", {}).get("key") == old.get("key"):
                    self.sel = i
                    break
        self.fix_sel(1)

    def fix_sel(self, direction):
        rows = self.rows
        if not rows:
            self.sel = 0
            return
        self.sel = max(0, min(self.sel, len(rows) - 1))
        i = self.sel
        while 0 <= i < len(rows) and "header" in rows[i]:
            i += direction
        if not 0 <= i < len(rows):
            i = self.sel
            while 0 <= i < len(rows) and "header" in rows[i]:
                i -= direction
        self.sel = max(0, min(i, len(rows) - 1))

    def selected_station(self):
        if 0 <= self.sel < len(self.rows):
            return self.rows[self.sel].get("station")
        return None

    def visible_stations(self):
        return [r["station"] for r in self.rows if "station" in r]

    def sections(self):
        """[(header row index, row)] of the station list."""
        return [(i, r) for i, r in enumerate(self.rows) if "header" in r]

    def jump_section(self, delta):
        secs = [i for i, _ in self.sections()]
        if not secs:
            return
        cur = max([i for i in secs if i <= self.sel] or [secs[0]])
        k = secs.index(cur) + delta
        if 0 <= k < len(secs):
            self.sel = secs[k] + 1
            self.fix_sel(1)

    def build_like_rows(self):
        likes = core.load_likes()
        groups = {}
        for l in likes:
            groups.setdefault(l.get("station", "?"), []).append(l)
        rows = []
        for name in sorted(groups, key=str.casefold):
            songs = sorted(groups[name], key=core.sort_key)
            rows.append({"header": f"{name.upper()} · {len(songs)}", "nav": name, "count": len(songs)})
            rows += [{"like": l} for l in songs]
        self.like_rows = rows
        self.like_sel = max(0, min(self.like_sel, len(rows) - 1))
        self.skip_like_headers(1)

    def skip_like_headers(self, direction):
        rows = self.like_rows
        i = self.like_sel
        while 0 <= i < len(rows) and "header" in rows[i]:
            i += direction
        if not 0 <= i < len(rows):
            i = self.like_sel
            while 0 <= i < len(rows) and "header" in rows[i]:
                i -= direction
        self.like_sel = max(0, min(i, len(rows) - 1)) if rows else 0

    def selected_like(self):
        if 0 <= self.like_sel < len(self.like_rows):
            return self.like_rows[self.like_sel].get("like")
        return None

    # ------------------------------------------------------------ search

    def maybe_search(self):
        if not self.query or self.query == self.searched or time.time() - self.typed_at < 0.35:
            return
        self.searched = self.query
        self.search_seq += 1
        seq, q = self.search_seq, self.query
        self.rb_results, self.rb_error = [], ""
        self.searching = True
        self.build_rows()
        threading.Thread(target=self.rb_search, args=(seq, q), daemon=True).start()

    def rb_search(self, seq, q):
        try:
            results, err = core.radiobrowser_search(q), ""
        except Exception as e:
            results, err = [], str(e)
        with self.lock:
            if seq != self.search_seq:
                return
            self.rb_results, self.rb_error, self.searching = results, err, False
            self.build_rows(keep=True)
            self.dirty = True

    # ------------------------------------------------------------ actions

    def say(self, text, secs=3.0):
        self.msg, self.msg_until = text, time.time() + secs

    def run_action(self, fn, *args, done=None):
        def work():
            try:
                result = fn(*args)
                if done:
                    self.say(done(result) if callable(done) else done)
            except core.PlayerError as e:
                self.say(f"Player: {e}", 5)
            except Exception as e:  # network etc.
                self.say(f"Error: {e}", 5)
        threading.Thread(target=work, daemon=True).start()

    def play_selected(self):
        st = self.selected_station()
        if not st:
            return
        self.say(f"Tuning in to {st['name']}…", 4)
        self.run_action(core.play, st, self.visible_stations(), done=f"Playing {st['name']}")

    def like(self):
        def done(result):
            like, how = result
            if how == "liked":
                if self.view == "likes":
                    self.build_like_rows()
                return f"♥ Liked {like['title']}" + (f" · {like['artist']}" if like.get("artist") else "")
            if how == "already":
                return "Already liked"
            return f"Can't like: {how}"
        self.run_action(core.like_current, done=done)

    def next_layout(self):
        self.layout = LAYOUTS[(LAYOUTS.index(self.layout) + 1) % len(LAYOUTS)]
        core.update_state(layout=self.layout)
        self.build_rows(keep=True)
        self.say(f"Layout: {self.layout}  (v for the next one)")

    def toggle_viz(self):
        if not Visualizer.available():
            self.say("The visualizer needs cava (e.g. pacman -S cava, apt install cava, brew install cava)", 5)
            return
        self.viz_on = not self.viz_on
        core.update_state(visualizer=self.viz_on)
        if self.viz_on:
            self.viz.start()
        else:
            self.viz.stop()
        self.say("Visualizer on" if self.viz_on else "Visualizer off")

    # ------------------------------------------------------------ keys

    def key(self, k):
        if k == curses.KEY_RESIZE:
            return
        if self.mode == "prompt":
            return self.key_prompt(k)
        if self.mode == "search":
            return self.key_search(k)
        if k == "a":
            self.mode, self.prompt = "prompt", {"step": "url", "url": "", "text": ""}
            return
        if self.view == "likes":
            return self.key_likes(k)
        return self.key_stations(k)

    def key_common(self, k):
        if k in ("q", "\x1b"):
            self.alive = False
        elif k == "\t":
            self.view = "likes" if self.view == "stations" else "stations"
            self.confirm = None
            if self.view == "likes":
                self.build_like_rows()
        elif k == " ":
            self.run_action(core.toggle_pause)
        elif k == "l":
            self.like()
        elif k == "v":
            self.next_layout()
        elif k == "c":
            self.toggle_viz()
        elif k in ("+", "="):
            self.run_action(core.change_volume, core.int_setting("volume_step"), done=lambda v: f"Volume {v}%")
        elif k == "-":
            self.run_action(core.change_volume, -core.int_setting("volume_step"), done=lambda v: f"Volume {v}%")
        elif k in ("n", ">"):
            self.run_action(core.step, 1, done=lambda s: f"Playing {s['name']}" if s else "No stations to step through")
        elif k in ("b", "<"):
            self.run_action(core.step, -1, done=lambda s: f"Playing {s['name']}" if s else "No stations to step through")
        elif k == "x":
            self.run_action(core.stop, done="Stopped")
        else:
            return False
        return True

    def move(self, k, sel, rows, page):
        if k in (curses.KEY_DOWN, "j"):
            return sel + 1, 1
        if k in (curses.KEY_UP, "k"):
            return sel - 1, -1
        if k in (curses.KEY_NPAGE, "\x06", "\x04"):
            return sel + page, 1
        if k in (curses.KEY_PPAGE, "\x02", "\x15"):
            return sel - page, -1
        if k in (curses.KEY_HOME, "g"):
            return 0, 1
        if k in (curses.KEY_END, "G"):
            return len(rows) - 1, -1
        return None, 0

    def move_grid(self, k):
        """Arrow keys in the cards layout: up/down a row of cards, left/right one card."""
        if self.sel not in self.grid:
            return False
        line, col = self.grid[self.sel]
        if k in (curses.KEY_RIGHT, curses.KEY_LEFT):
            order = sorted(self.grid, key=lambda i: self.grid[i])
            i = order.index(self.sel) + (1 if k == curses.KEY_RIGHT else -1)
            if 0 <= i < len(order):
                self.sel = order[i]
            return True
        if k in (curses.KEY_DOWN, "j", curses.KEY_UP, "k"):
            down = k in (curses.KEY_DOWN, "j")
            lines = sorted({ln for ln, _ in self.grid.values()}, reverse=not down)
            for ln in lines:
                if (ln > line) if down else (ln < line):
                    cards = sorted((c, i) for i, (l2, c) in self.grid.items() if l2 == ln)
                    best = min(cards, key=lambda ci: abs(ci[0] - col))
                    self.sel = best[1]
                    return True
            return True
        return False

    def key_stations(self, k):
        if k in ("/", "s"):
            self.mode = "search"
            return
        if k in ("\n", "\r", curses.KEY_ENTER):
            self.play_selected()
            return
        if k == "f":
            st = self.selected_station()
            if st:
                saved = core.toggle_mine(st)
                self.my_keys = {m.get("key") for m in core.load_my()}
                self.say(f"★ Saved {st['name']} to My stations" if saved else f"Removed {st['name']} from My stations")
                if not self.query:
                    self.build_rows(keep=True)
            return
        if k == "[" or (k == curses.KEY_LEFT and self.layout == "sidebar"):
            self.jump_section(-1)
            return
        if k == "]" or (k == curses.KEY_RIGHT and self.layout == "sidebar"):
            self.jump_section(1)
            return
        if self.layout == "cards" and self.move_grid(k):
            return
        new, d = self.move(k, self.sel, self.rows, self.page())
        if new is not None:
            self.sel = new
            self.fix_sel(d)
            return
        self.key_common(k)

    def key_prompt(self, k):
        """Adding a stream link: first the link, then a name for it."""
        p = self.prompt
        if k == "\x1b":
            self.mode, self.prompt = "normal", None
            return
        if k in ("\n", "\r", curses.KEY_ENTER):
            text = p["text"].strip()
            if p["step"] == "url":
                if not re.match(r"^https?://\S+$", text):
                    self.say("Paste a stream link that starts with http:// or https://", 4)
                    return
                p.update(step="name", url=text, text="")
                return
            st = core.custom_station(p["url"], text or core.custom_station(p["url"])["name"])
            if not core.is_mine(st):
                core.toggle_mine(st)
            self.my_keys = {m.get("key") for m in core.load_my()}
            self.mode, self.prompt = "normal", None
            self.say(f"★ Saved {st['name']} to My stations · enter plays it")
            if not self.query:
                self.build_rows()
                for i, r in enumerate(self.rows):
                    if r.get("station", {}).get("key") == st["key"]:
                        self.sel = i
                        break
            return
        if k in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            p["text"] = p["text"][:-1]
        elif k == "\x15":
            p["text"] = ""
        elif isinstance(k, str) and k.isprintable():
            p["text"] += k

    def key_search(self, k):
        if k in ("\n", "\r", curses.KEY_ENTER):
            self.mode = "normal"
            self.typed_at = 0
            self.maybe_search()
        elif k == "\x1b":
            self.mode = "normal"
            if self.query:
                self.query = self.searched = ""
                self.build_rows()
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.query = self.query[:-1]
            self.typed_at = time.time()
            if not self.query:
                self.searched = ""
                self.build_rows()
        elif k == "\x15":
            self.query = self.searched = ""
            self.build_rows()
        elif k in (curses.KEY_DOWN, curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE):
            new, d = self.move(k, self.sel, self.rows, self.page())
            self.sel = new
            self.fix_sel(d)
        elif isinstance(k, str) and k.isprintable():
            self.query += k
            self.typed_at = time.time()

    def key_likes(self, k):
        if k == "d":
            like = self.selected_like()
            if like:
                if self.confirm is like:
                    core.unlike(like)
                    self.confirm = None
                    self.say(f"Removed {like['title']}")
                    self.build_like_rows()
                else:
                    self.confirm = like
                    self.say("Press d again to remove this song", 4)
            return
        self.confirm = None
        new, d = self.move(k, self.like_sel, self.like_rows, self.page())
        if new is not None:
            self.like_sel = max(0, min(new, len(self.like_rows) - 1))
            self.skip_like_headers(d)
            return
        if k in ("/", "s"):
            self.view = "stations"
            self.mode = "search"
            return
        self.key_common(k)

    # ------------------------------------------------------------ drawing primitives

    def page(self):
        h, _ = self.scr.getmaxyx()
        return max(1, h - 8)

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        width = min(width if width is not None else w - x, w - x)
        if y == h - 1:
            width = min(width, w - x - 1)  # writing the last cell scrolls curses
        if width <= 0:
            return
        try:
            self.scr.addstr(y, x, core.fit(text, width), attr)
        except curses.error:
            pass

    def text(self, y, x, text, attr=0):
        """Write text without padding; returns the x after it."""
        width = core.text_width(text)
        self.put(y, x, text, attr, width)
        return x + width

    def center(self, y, x, width, text, attr=0):
        tw = min(core.text_width(text), width)
        self.put(y, x + max(0, (width - tw) // 2), text, attr, tw)

    def box(self, y, x, hgt, wid, title="", attr=None, title_attr=None, right=None):
        """A rounded box; title (and right, a list of (text, attr)) sit in the top border."""
        attr = self.DIM if attr is None else attr
        if hgt < 2 or wid < 4:
            return
        self.put(y, x, "╭" + "─" * (wid - 2) + "╮", attr, wid)
        for yy in range(y + 1, y + hgt - 1):
            self.put(yy, x, "│", attr, 1)
            self.put(yy, x + wid - 1, "│", attr, 1)
        self.put(y + hgt - 1, x, "╰" + "─" * (wid - 2) + "╯", attr, wid)
        if title:
            t = f" {title} "
            self.put(y, x + 2, t, self.ACC | self.BOLD if title_attr is None else title_attr,
                     min(core.text_width(t), wid - 4))
        if right:
            total = sum(core.text_width(t) for t, _ in right)
            xx = x + wid - 2 - total
            for t, a in right:
                xx = self.text(y, xx, t, a)

    def hline(self, y, x, width):
        self.put(y, x, "─" * width, self.DIM, width)

    def vline(self, x, y0, y1):
        for y in range(y0, y1 + 1):
            self.put(y, x, "│", self.DIM, 1)

    def vol_bar(self, y, x, width):
        vol = int(self.now.get("volume", self.vol))
        label = f" {vol}%"
        bar_w = max(4, width - len(label) - 4)
        filled = round(bar_w * vol / 100)
        x = self.text(y, x, "vol ", self.DIM)
        x = self.text(y, x, "━" * filled, self.ACC)
        x = self.text(y, x, "─" * (bar_w - filled), self.DIM)
        self.text(y, x, label, self.DIM)

    def show_viz(self):
        return self.viz_on and self.viz.proc is not None

    def draw_viz(self, y, x, width, rows):
        """Bars from the newest cava frame in a width x rows box (bottom-aligned, one-cell gaps).

        Built row by row and written in a few runs per row (it runs ~16 times a second)."""
        if width < 4 or rows < 1:
            return
        self.viz_rect = (y, x, width, rows)
        h, w = self.scr.getmaxyx()
        width = min(width, w - x - 1)
        vals = self.viz.frame
        playing = bool(self.now_station and self.now.get("playing") and not self.now.get("paused"))
        n = max(1, (width + 1) // 2)
        total = rows * 8
        levels = []
        for i in range(n):
            v = vals[min(len(vals) - 1, i * len(vals) // n)] if vals and playing else 0
            levels.append(v * total // Visualizer.RANGE)
        for r in range(rows):
            yy = y + rows - 1 - r
            if not 0 <= yy < h:
                continue
            attr = self.ACC | (self.BOLD if r == rows - 1 else 0)
            runs, cur, text = [], None, ""
            for v in levels:
                level = max(0, min(8, v - r * 8))
                if level:
                    a, ch = attr, BLOCKS[level]
                elif r == 0:
                    a, ch = self.DIM, "▁"  # a quiet baseline under silent bars
                else:
                    a, ch = 0, " "
                if a != cur and text:
                    runs.append((text, cur))
                    text = ""
                cur = a
                text += ch + " "
            if text:
                runs.append((text, cur))
            xx = x
            for text, a in runs:
                text = text[:max(0, x + width - xx)]
                if text:
                    try:
                        self.scr.addstr(yy, xx, text, a)
                    except curses.error:
                        pass
                xx += len(text)

    def tabs(self, y, x):
        for view, label in (("stations", "STATIONS"), ("likes", "LIKED SONGS")):
            x = self.text(y, x, label, (self.BOLD | curses.A_UNDERLINE) if self.view == view else self.DIM) + 3
        return x

    def now_info(self):
        """(station or None, mark, title, artist) of what's on."""
        station, now = self.now_station, self.now
        if not (station and now.get("playing")):
            return None, "", "", ""
        mark = ICONS["pause"] if now.get("paused") else ICONS["play"]
        song = core.song_of(station, now.get("title"))
        if not song and now.get("buffering"):
            return station, mark, "buffering…", ""
        artist, title = core.split_title(song)
        return station, mark, title, artist

    def now_line(self, y, x, width):
        """▶ Station  Title · Artist ♥ (one line)."""
        station, mark, title, artist = self.now_info()
        if not station:
            self.put(y, x, "■ Nothing playing. Pick a station and press enter.", self.DIM, width)
            return
        end = x + width
        xx = self.text(y, x, mark + " ", self.ACC | self.BOLD)
        name = core.fit(station["name"], min(32, core.text_width(station["name"]))).rstrip()
        xx = self.text(y, xx, name, self.ACC | self.BOLD) + 2
        if title:
            song = f"{title} · {artist}" if artist else title
            heart = f"  {ICONS['heart']}" if self.liked_now else ""
            self.put(y, xx, song + heart, 0, max(0, end - xx))

    def search_line(self, y, x, width):
        if self.mode == "prompt":
            p = self.prompt
            if p["step"] == "url":
                label = "+ Stream link: "
            else:
                label = f"+ Name (enter keeps \"{core.custom_station(p['url'])['name']}\"): "
            self.put(y, x, label, self.ACC | self.BOLD, core.text_width(label))
            self.put(y, x + core.text_width(label), p["text"] + "▏", self.BOLD, width - core.text_width(label))
            return
        if self.view == "likes" and self.mode != "search":
            self.put(y, x, f"{ICONS['heart']} Each station's liked songs are kept sorted in "
                     f"{self.short(core.likes_dir())}/", self.DIM, width)
        elif self.mode == "search":
            self.put(y, x, ICONS["search"] + " ", self.ACC | self.BOLD, 2)
            self.put(y, x + 2, self.query + "▏", self.BOLD, width - 2)
        elif self.query:
            self.put(y, x, ICONS["search"] + " ", self.DIM, 2)
            self.put(y, x + 2, f"{self.query}   (/ edit · esc in search clears)", self.DIM, width - 2)
        else:
            self.put(y, x, f"{ICONS['search']} / to search 50,000+ stations (try lofi, house, jazz…) · "
                     "a adds a stream link", self.DIM, width)

    def short(self, path):
        return path.replace(core.HOME, "~", 1) if path.startswith(core.HOME) else path

    def wrap(self, text, width):
        return textwrap.wrap(text, max(10, width)) if text else []

    def draw_lines(self, lines, y0, y1, x, width):
        y = y0
        for text, attr in lines:
            if y > y1:
                break
            self.put(y, x, text, attr, width)
            y += 1

    # ------------------------------------------------------------ lists and details

    def draw_list(self, rows, sel, top_attr, y0, y1, x0, width, render):
        height = y1 - y0 + 1
        if height <= 0:
            return
        top = getattr(self, top_attr)
        if sel < top:
            top = sel
        if sel >= top + height:
            top = sel - height + 1
        if top > 0 and top - 1 < len(rows) and "header" in rows[top - 1] and sel - (top - 1) < height:
            top -= 1  # keep a section's heading on screen with its first row
        top = max(0, min(top, max(0, len(rows) - height)))
        setattr(self, top_attr, top)
        for i, row in enumerate(rows[top:top + height]):
            y = y0 + i
            if "header" in row:
                self.put(y, x0 + 1, row["header"], self.ACC | self.BOLD, width - 2)
            else:
                render(y, x0, row, top + i == sel, width)

    def is_playing(self, st):
        return bool(self.now_station and self.now_station.get("key") == st.get("key") and self.now.get("playing"))

    def quality(self, st):
        return " ".join(x for x in (f"{st['bitrate']}k" if st.get("bitrate") else "", st.get("codec", ""),
                                    st.get("country", "")) if x)

    def station_row(self, y, x0, row, selected, width):
        st = row["station"]
        playing = self.is_playing(st)
        meta = self.quality(st) if width >= 44 else (f"{st['bitrate']}k" if st.get("bitrate") else "")
        meta_w = min(len(meta), 14)
        name_w = width - 7 - meta_w  # one blank cell before the quality column
        if selected:
            self.put(y, x0 + 1, ICONS["sel"], self.ACC | self.BOLD, 1)
        if playing:
            self.put(y, x0 + 3, ICONS["play"], self.ACC | self.BOLD, 1)
        elif st.get("source") == "My stations" or st.get("key") in self.my_keys:
            self.put(y, x0 + 3, "★", self.DIM, 1)
        attr = (self.BOLD if selected else 0) | (self.ACC if playing else 0)
        self.put(y, x0 + 5, st["name"], attr, name_w)
        self.put(y, x0 + width - meta_w - 1, meta, self.DIM, meta_w)

    def wide_row(self, y, x0, row, selected, width):
        """spotlight: name | genre | on air now | quality."""
        st = row["station"]
        playing = self.is_playing(st)
        meta = self.quality(st)
        meta_w = 14
        name_w = min(30, max(16, width // 4))
        tag_w = 16 if width > 90 else 0
        now_w = width - 5 - name_w - tag_w - meta_w - 3
        if selected:
            self.put(y, x0 + 1, ICONS["sel"], self.ACC | self.BOLD, 1)
        if playing:
            self.put(y, x0 + 3, ICONS["play"], self.ACC | self.BOLD, 1)
        elif st.get("source") == "My stations" or st.get("key") in self.my_keys:
            self.put(y, x0 + 3, "★", self.DIM, 1)
        attr = (self.BOLD if selected else 0) | (self.ACC if playing else 0)
        x = x0 + 5
        self.put(y, x, st["name"], attr, name_w)
        x += name_w + 1
        tags = st.get("tags", [])
        if tag_w:
            tag = tags[0] if tags else ("my station" if st.get("source") == "My stations" else "")
            self.put(y, x, tag, self.DIM, tag_w)
            x += tag_w + 1
        on = core.song_of(st, self.now.get("title")) if playing else ""
        if now_w > 8:
            if on:
                self.put(y, x, f"{ICONS['note']} {on}", 0 if playing else self.DIM, now_w)
            else:  # no song while not playing: the station's tags and votes (or its link)
                extra = ", ".join(tags[1:6]) if tag_w else ", ".join(tags[:5])
                if st.get("source") == "My stations":
                    extra = urllib.parse.urlsplit(st.get("url", "")).netloc
                bits = [b for b in (extra, st.get("popularity", "")) if b]
                self.put(y, x, "  ".join(bits), self.DIM, now_w)
        self.put(y, x0 + width - meta_w - 1, meta.rjust(meta_w), self.DIM, meta_w)

    def like_row(self, y, x0, row, selected, width):
        l = row["like"]
        text = f"{l['artist']} - {l['title']}" if l.get("artist") else l.get("title", "")
        if selected:
            self.put(y, x0 + 1, ICONS["sel"], self.ACC | self.BOLD, 1)
        self.put(y, x0 + 3, text, self.BOLD if selected else 0, width - 4)

    def details_lines(self, st, width, title=True):
        if not st:
            return []
        lines = []
        if title:
            lines.append((st["name"], self.ACC | self.BOLD))
        info = [st.get("source", "")]
        if st.get("bitrate"):
            info.append(f"{st['bitrate']} kbps {st.get('codec', '')}".strip())
        if st.get("country"):
            info.append(st["country"])
        if st.get("popularity"):
            info.append(st["popularity"])
        lines.append((" · ".join(i for i in info if i), self.DIM))
        if st.get("tags"):
            lines += [(t, self.DIM) for t in self.wrap(", ".join(st["tags"]), width)]
        if st.get("description"):
            lines.append(("", 0))
            lines += [(t, 0) for t in self.wrap(st["description"], width)]
        playing = self.is_playing(st)
        song = core.song_of(st, self.now.get("title")) if playing else ""
        if song:
            lines += [("", 0), ("NOW PLAYING", self.BOLD)]
            heart = f"  {ICONS['heart']}  liked" if self.liked_now else "   (l to like)"
            lines.append((song + heart, self.ACC))
        if st.get("homepage"):
            lines += [("", 0), (st["homepage"], self.DIM)]
        if st.get("source") == "My stations":
            lines += [("", 0), ("Stream link:", self.DIM)] + [(t, self.DIM) for t in self.wrap(st.get("url", ""), width)]
        saved = st.get("source") == "My stations" or st.get("key") in self.my_keys
        lines += [("", 0), ("★ in My stations · f removes it" if saved else "f saves it to My stations", self.DIM)]
        if not playing:
            lines.append(("enter plays it", self.DIM))
        return lines

    def like_lines(self, width, title=True):
        l = self.selected_like()
        if not l:
            return []
        lines = [(l.get("title", ""), self.ACC | self.BOLD)] if title else []
        if l.get("artist"):
            lines.append((l["artist"], self.BOLD))
        if l.get("album"):
            lines.append((f"Album: {l['album']}", 0))
        lines += [("", 0), (f"Heard on {l.get('station', '')}" + (f" ({l['source']})" if l.get("source") else ""), self.DIM),
                  (f"Liked {l.get('liked', '')}", self.DIM), ("", 0), ("This station's list:", self.DIM)]
        lines += [(t, self.DIM) for t in self.wrap(self.short(core.station_file(l.get("station", ""))), width)]
        return lines

    def empty_likes(self, y, x, width):
        self.put(y, x, "No liked songs yet. While a station plays, press l to like the song.", self.DIM, width)
        self.put(y + 1, x, f"Lists are saved per station in {self.short(core.likes_dir())}/", self.DIM, width)

    def draw_body(self, y0, y1, x0, list_w, details_x=None, details_w=0, render=None, details_bottom=None):
        """The list (stations or liked songs) and, when there's room, its details."""
        dy1 = y1 if details_bottom is None else details_bottom
        if self.view == "stations":
            self.draw_list(self.rows, self.sel, "top", y0, y1, x0, list_w, render or self.station_row)
            if details_x is not None and details_w >= 20:
                self.draw_lines(self.details_lines(self.selected_station(), details_w), y0, dy1, details_x, details_w)
        else:
            if not self.like_rows:
                self.empty_likes(y0 + 1, x0 + 2, list_w - 3)
            self.draw_list(self.like_rows, self.like_sel, "like_top", y0, y1, x0, list_w, self.like_row)
            if details_x is not None and details_w >= 20 and self.like_rows:
                self.draw_lines(self.like_lines(details_w), y0, dy1, details_x, details_w)

    # ------------------------------------------------------------ layouts

    def draw_bars_only(self):
        """Between full redraws, only the bars move: clear and redraw just their box."""
        y, x, width, rows = self.viz_rect
        self.draw_viz(y, x, width, rows)  # every cell of the box is rewritten, so no clearing
        self.scr.refresh()

    def draw(self):
        scr = self.scr
        self.viz_rect = None
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 12 or w < 50:
            self.put(0, 0, "herdr-onair: make the popup bigger")
            scr.refresh()
            return
        layout = self.layout
        if (layout in ("sidebar", "framed") and w < 100) or (layout == "spotlight" and h < 18):
            layout = "classic"  # not enough room: fall back
        getattr(self, f"layout_{layout}")(h, w)
        self.footer(h, w)
        scr.refresh()

    def footer(self, h, w):
        if self.msg and time.time() < self.msg_until:
            self.put(h - 1, 1, self.msg, self.ACC | self.BOLD)
            return
        if self.mode == "prompt":
            hint = ("paste a stream link (direct, .pls or .m3u)   enter next   esc cancel" if self.prompt["step"] == "url"
                    else "type a name   enter save   esc cancel")
        elif self.mode == "search":
            hint = "type to search   ↑↓ move   enter done   esc clear"
        elif self.view == "likes":
            hint = "d d remove   l like   space pause   n/b station   +/- volume   tab stations   v layout   q close"
        else:
            hint = ("enter play   / search   f save   a add link   l like   space pause   n/b station   +/- vol   "
                    "tab liked   v layout   c bars   q close")
        self.put(h - 1, 1, hint, self.DIM)

    def header_row(self, y, w, vol=True):
        self.text(y, 1, "RADIO", self.ACC | self.BOLD)
        self.tabs(y, 9)
        if vol:
            v = f"vol {self.now.get('volume', self.vol)}%"
            self.put(y, w - len(v) - 2, v, self.DIM, len(v))

    # classic: list + details side by side ---------------------------------------
    def layout_classic(self, h, w):
        self.header_row(0, w)
        self.now_line(1, 1, w - 2)
        self.search_line(2, 1, w - 2)
        self.hline(3, 0, w)
        split = w >= 96
        list_w = int(w * 0.55) if split else w
        viz = split and self.show_viz()
        self.draw_body(4, h - 2, 0, list_w, list_w + 2 if split else None, w - list_w - 3,
                       details_bottom=h - 7 if viz else None)
        if split:
            self.vline(list_w, 4, h - 2)
        if viz:
            self.draw_viz(h - 5, list_w + 2, w - list_w - 4, 3)

    # framed: rounded panels -------------------------------------------------------
    def layout_framed(self, h, w):
        tabs = []
        for view, label in (("stations", "STATIONS"), ("likes", "LIKED SONGS")):
            tabs.append((f" {label} ", (self.BOLD | curses.A_UNDERLINE) if self.view == view else self.DIM))
        self.box(0, 0, 3, w, "RADIO", right=tabs)
        self.now_line(1, 2, w - 34)
        self.vol_bar(1, w - 30, 28)

        list_w = int(w * 0.56)
        det_w = w - list_w
        top, bottom = 3, h - 2
        title = "STATIONS" if self.view == "stations" else "LIKED SONGS"
        self.box(top, 0, bottom - top + 1, list_w, title)
        self.search_line(top + 1, 2, list_w - 4)
        self.put(top + 2, 1, "─" * (list_w - 2), self.DIM, list_w - 2)
        if self.view == "stations":
            st = self.selected_station()
            self.box(top, list_w, bottom - top + 1, det_w, (st["name"].upper() if st else "DETAILS")[:det_w - 6])
            self.draw_list(self.rows, self.sel, "top", top + 3, bottom - 1, 1, list_w - 2, self.station_row)
            viz = self.show_viz()
            self.draw_lines(self.details_lines(st, det_w - 4, title=False), top + 1, bottom - (5 if viz else 1),
                            list_w + 2, det_w - 4)
            if viz:
                self.draw_viz(bottom - 3, list_w + 2, det_w - 4, 3)
        else:
            l = self.selected_like()
            self.box(top, list_w, bottom - top + 1, det_w, (l.get("title", "") if l else "SONG")[:det_w - 6])
            if not self.like_rows:
                self.empty_likes(top + 4, 3, list_w - 6)
            self.draw_list(self.like_rows, self.like_sel, "like_top", top + 3, bottom - 1, 1, list_w - 2, self.like_row)
            self.draw_lines(self.like_lines(det_w - 4, title=False), top + 1, bottom - 1, list_w + 2, det_w - 4)

    # spotlight: what's on air, large, on top ---------------------------------------
    def layout_spotlight(self, h, w):
        self.header_row(0, w, vol=False)
        station, mark, title, artist = self.now_info()
        roomy = h >= 36  # smaller popups get a tighter top: no blank lines, shorter bars
        viz = self.show_viz() and h >= 22
        if station:
            paused = self.now.get("paused")
            state = "‖ PAUSED" if paused else "● ON AIR"
            name = station["name"].upper()
            spaced = " ".join(name)
            big = spaced if core.text_width(spaced) <= w - 8 else name
            heart = f"   {ICONS['heart']} liked" if self.liked_now else ("   l to like" if title else "")
            meta = " · ".join(x for x in (station.get("source", ""), self.quality(station),
                                          station.get("popularity", "")) if x)
            if roomy:
                self.center(2, 0, w, state, self.ACC if not paused else self.DIM)
                self.center(3, 0, w, big, self.ACC | self.BOLD)
                self.center(5, 0, w, title or "—", self.BOLD)
                self.center(6, 0, w, (artist or "") + heart, self.DIM if not self.liked_now else self.ACC)
                self.center(7, 0, w, meta, self.DIM)
            else:
                self.center(1, 0, w, f"{state}   {big}" if core.text_width(f"{state}   {big}") <= w - 4 else big,
                            self.ACC | self.BOLD)
                song = f"{title} · {artist}" if artist else (title or "—")
                self.center(2, 0, w, song, self.BOLD)
                self.center(3, 0, w, (heart.strip() + "   " if heart else "") + meta,
                            self.ACC if self.liked_now else self.DIM)
        else:
            self.center(3 if roomy else 2, 0, w, "O F F   A I R", self.DIM | self.BOLD)
            self.center(5 if roomy else 3, 0, w, "Pick a station below and press enter", self.DIM)
        if roomy:
            y = 8
            if viz:
                vw = min(w - 16, 84)
                self.draw_viz(9, (w - vw) // 2, vw, 4)
                y = 14
        else:
            y = 4
            if viz:
                vw = min(w - 12, 72)
                self.draw_viz(4, (w - vw) // 2, vw, 2)
                y = 6
        bar_w = 36
        self.vol_bar(y, (w - bar_w) // 2, bar_w)
        self.hline(y + 1, 0, w)
        self.search_line(y + 2, 1, w - 2)
        self.hline(y + 3, 0, w)
        list_top = y + 4
        body_bottom = h - 3
        if self.view == "stations":
            self.draw_list(self.rows, self.sel, "top", list_top, body_bottom, 0, w, self.wide_row)
            st = self.selected_station()
            if st:
                desc = (st.get("description") or ", ".join(st.get("tags", [])) or st.get("homepage", "")
                        or (st.get("url", "") if st.get("source") == "My stations" else ""))
                self.put(h - 2, 1, f"› {st['name']}: {desc}" if desc else f"› {st['name']}", self.DIM, w - 2)
        else:
            if not self.like_rows:
                self.empty_likes(list_top + 1, 3, w - 6)
            self.draw_list(self.like_rows, self.like_sel, "like_top", list_top, body_bottom, 0, w, self.like_row)
            l = self.selected_like()
            if l:
                bits = [f"Album: {l['album']}" if l.get("album") else "", f"heard on {l.get('station', '')}",
                        f"liked {l.get('liked', '')}"]
                self.put(h - 2, 1, "› " + " · ".join(b for b in bits if b), self.DIM, w - 2)

    # sidebar: sections | list | details, player bar at the bottom ---------------------
    def layout_sidebar(self, h, w):
        self.text(0, 1, "RADIO", self.ACC | self.BOLD)
        self.search_line(0, 9, w - 10)
        self.hline(1, 0, w)
        nav_w = 24
        body_top, body_bottom = 2, h - 5
        # sections
        y = body_top
        cur = None
        if self.view == "stations":
            secs = self.sections()
            cur = max([i for i, _ in secs if i <= self.sel] or [None], key=lambda i: -1 if i is None else i)
            for i, r in secs:
                if y > body_bottom - 2:
                    break
                label = r.get("nav") or r["header"].split(" · ")[0].title()
                count = str(r.get("count", "")) if r.get("count") is not None else ""
                active = i == cur
                if active:
                    self.put(y, 1, ICONS["sel"], self.ACC | self.BOLD, 1)
                self.put(y, 3, label, (self.ACC | self.BOLD) if active else 0, nav_w - 8)
                self.put(y, nav_w - 1 - len(count), count, self.DIM, len(count))
                y += 1
        likes_n = self.likes_count
        yl = body_bottom
        if self.view == "likes":
            self.put(yl, 1, ICONS["sel"], self.ACC | self.BOLD, 1)
        self.put(yl, 3, f"{ICONS['heart']} Liked songs", (self.ACC | self.BOLD) if self.view == "likes" else 0, nav_w - 8)
        self.put(yl, nav_w - 1 - len(str(likes_n)), str(likes_n), self.DIM, len(str(likes_n)))
        self.put(yl - 1, 1, "─" * (nav_w - 2), self.DIM, nav_w - 2)
        self.vline(nav_w, body_top, body_bottom)
        # list + details
        rest = w - nav_w - 1
        list_w = int(rest * 0.5)
        det_x, det_w = nav_w + list_w + 3, w - nav_w - list_w - 4
        viz = self.show_viz()
        self.draw_body(body_top, body_bottom, nav_w + 1, list_w, det_x, det_w,
                       details_bottom=body_bottom - 4 if viz else None)
        self.vline(nav_w + 1 + list_w, body_top, body_bottom)
        if viz:
            self.draw_viz(body_bottom - 2, det_x, det_w - 1, 3)
        # player bar
        self.hline(h - 4, 0, w)
        station, mark, title, artist = self.now_info()
        if station:
            x = self.text(h - 3, 2, mark + " ", self.ACC | self.BOLD)
            self.put(h - 3, x, title or station["name"], self.BOLD, w // 2 - x)
            sub = " · ".join(b for b in (artist, station["name"]) if b) if title else station.get("source", "")
            heart = f"  {ICONS['heart']}" if self.liked_now else ""
            self.put(h - 2, 4, sub + heart, self.DIM, w // 2 - 4)
        else:
            self.put(h - 3, 2, "■ Nothing playing", self.DIM, w // 2)
        cx = w // 2 - 6
        for key, label in (("b", "prev"), ("space", "pause" if self.now.get("playing") and not self.now.get("paused") else "play"),
                           ("n", "next"), ("l", "like")):
            cx = self.text(h - 3, cx, key + " ", self.ACC | self.BOLD)
            cx = self.text(h - 3, cx, label, 0) + 3
        self.vol_bar(h - 3, w - 32, 30)
        self.put(h - 2, w // 2 - 6, "← → or [ ] jump between sections", self.DIM, 34)

    # cards: stations as tiles ---------------------------------------------------------
    def layout_cards(self, h, w):
        self.header_row(0, w)
        viz = self.show_viz() and w >= 110
        side = 44 if viz else 0
        self.now_line(1, 1, w - 2 - side)
        self.search_line(2, 1, w - 2 - side)
        if viz:
            self.draw_viz(1, w - side, side - 2, 2)
        self.hline(3, 0, w)
        y0, y1 = 4, h - 2
        if self.view == "likes":
            split = w >= 96
            list_w = int(w * 0.55) if split else w
            self.draw_body(y0, y1, 0, list_w, list_w + 2 if split else None, w - list_w - 3)
            if split:
                self.vline(list_w, y0, y1)
            return
        cols = max(1, (w - 1) // 32)
        card_w = (w - 1) // cols
        card_h = 5
        # lay everything out on a virtual canvas: (y, kind, payload)
        items, y, line = [], 0, 0
        grid = {}
        row_cards = []

        def flush():
            nonlocal y, line, row_cards
            if row_cards:
                for c, idx in enumerate(row_cards):
                    items.append((y, "card", (idx, c)))
                    grid[idx] = (line, c)
                y += card_h
                line += 1
                row_cards = []

        for i, r in enumerate(self.rows):
            if "header" in r:
                flush()
                items.append((y, "header", r["header"]))
                y += 1
                line += 1
            else:
                row_cards.append(i)
                if len(row_cards) == cols:
                    flush()
        flush()
        self.grid = grid
        height = y1 - y0 + 1
        if self.sel in grid:
            sel_y = next(iy for iy, kind, p in items if kind == "card" and p[0] == self.sel)
            if sel_y < self.card_top:
                self.card_top = sel_y
            if sel_y + card_h > self.card_top + height:
                self.card_top = sel_y + card_h - height
            hy = [iy for iy, kind, _ in items if kind == "header" and iy == sel_y - 1]
            if hy and hy[0] < self.card_top:
                self.card_top = hy[0]
        self.card_top = max(0, self.card_top)
        for iy, kind, payload in items:
            sy = y0 + iy - self.card_top
            if kind == "header":
                if y0 <= sy <= y1:
                    self.put(sy, 1, payload, self.ACC | self.BOLD, w - 2)
                continue
            if sy + card_h - 1 < y0 or sy > y1:
                continue
            idx, c = payload
            self.card(sy, 1 + c * card_w, card_w - 1, self.rows[idx]["station"], idx == self.sel, y0, y1)

    def card(self, y, x, width, st, selected, y0, y1):
        playing = self.is_playing(st)
        border = (self.ACC | self.BOLD) if selected else (self.ACC if playing else self.DIM)
        lines = []
        name = (ICONS["play"] + " " if playing else "") + st["name"]
        lines.append((name, (self.BOLD if selected else 0) | (self.ACC if playing else 0)))
        tag = ", ".join(st.get("tags", [])[:2]) or ("my station" if st.get("source") == "My stations" else "")
        lines.append((" · ".join(b for b in (tag, self.quality(st)) if b), self.DIM))
        on = core.song_of(st, self.now.get("title")) if playing else ""
        rest = st.get("popularity") or urllib.parse.urlsplit(st.get("url", "")).netloc
        lines.append((f"{ICONS['note']} {on}" if on else rest, 0 if playing else self.DIM))
        rows = [("╭" + "─" * (width - 2) + "╮", border)]
        rows += [None] * len(lines)
        rows.append(("╰" + "─" * (width - 2) + "╯", border))
        for k, row in enumerate(rows):
            yy = y + k
            if not y0 <= yy <= y1:
                continue
            if row:
                self.put(yy, x, row[0], row[1], width)
            else:
                text, attr = lines[k - 1]
                self.put(yy, x, "│", border, 1)
                self.put(yy, x + 2, text, attr, width - 4)
                self.put(yy, x + width - 1, "│", border, 1)

    # ------------------------------------------------------------ loop

    def run(self):
        self.colors()
        self.scr.timeout(150)
        self.scr.keypad(True)
        with self.lock:
            self.build_rows()
        self.start_threads()
        if self.viz_on:
            self.viz.start()
        last_full = 0.0
        try:
            while self.alive:
                bars = self.show_viz()
                self.scr.timeout(60 if bars else 150)
                with self.lock:
                    if self.mode == "search":
                        self.maybe_search()
                    now = time.time()
                    # Full redraws when something changed (and at least twice a second, for messages
                    # and clocks); the frames in between only move the visualizer's bars.
                    if self.dirty or not bars or self.viz_rect is None or now - last_full >= 0.5:
                        self.draw()
                        self.dirty, last_full = False, now
                    else:
                        self.draw_bars_only()
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                except KeyboardInterrupt:
                    break
                with self.lock:
                    self.dirty = True
                    self.key(k)
        finally:  # also when the toggle closes the popup (SIGTERM -> SystemExit)
            self.alive = False
            self.viz.stop()


def main():
    os.environ.setdefault("ESCDELAY", "25")
    os.makedirs(core.STATE, exist_ok=True)
    with open(core.APP_PID, "w") as f:
        f.write(str(os.getpid()))

    def on_term(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)
    try:
        curses.wrapper(lambda scr: App(scr).run())
    finally:
        try:
            with open(core.APP_PID) as f:
                if f.read().strip() == str(os.getpid()):
                    os.unlink(core.APP_PID)
        except OSError:
            pass
