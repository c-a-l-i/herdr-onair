"""herdr-onair setup: what a new install needs, in four steps (curses).

  Check    python, mpv (needed), cava (optional, for the bars), Radio Browser reachable
  Key      a herdr key for the radio popup, written to herdr's config.toml
  Top bar  the now-playing line in herdr's top bar (and the `onair` command in ~/.local/bin)
  Done     open the radio

Opens by itself the first time the radio is opened (until settings.conf has setup_done), and any
time from the "setup" action. Every config change is backed up first and checked with herdr's own
config reload; if herdr reports a problem, the backup goes back.
"""
import curses
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time

from . import core

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
HERDR_CONF = os.environ.get("HERDR_CONFIG_PATH") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(core.HOME, ".config"), "herdr", "config.toml")
STEPS = ["Check", "Key", "Top bar", "Done"]
# herdr's own default prefix keys (0.9); anything already in config.toml is taken too.
HERDR_DEFAULT_KEYS = set("bcdeghjklnopqrsvwxz[;?") | {"tab", "minus"} | set("0123456789")
KEY_IDEAS = ["y", "u", "i", "a", "m", "t", "f"]
TOGGLE = "herdr-onair.toggle"


def install_cmd(pkg):
    if platform.system() == "Darwin":
        return f"brew install {pkg}"
    for tool, cmd in (("pacman", "sudo pacman -S"), ("apt", "sudo apt install"), ("dnf", "sudo dnf install"),
                      ("zypper", "sudo zypper install")):
        if shutil.which(tool):
            return f"{cmd} {pkg}"
    return f"install {pkg} with your package manager"


def read_conf():
    try:
        with open(HERDR_CONF, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def herdr_prefix(conf):
    m = re.search(r'^\s*prefix\s*=\s*"([^"]+)"', conf, re.M)
    return m.group(1) if m else "ctrl+b"


def current_key(conf):
    """The key already bound to the radio, or ""."""
    for block in re.split(r"^\s*\[\[keys\.command\]\]", conf, flags=re.M)[1:]:
        block = re.split(r"^\s*\[", block, maxsplit=1, flags=re.M)[0]
        if TOGGLE in block:
            m = re.search(r'^\s*key\s*=\s*"([^"]+)"', block, re.M)
            if m:
                return m.group(1)
    return ""


def key_taken(key, conf):
    k = key[len("prefix+"):] if key.startswith("prefix+") else None
    if k is not None and k in HERDR_DEFAULT_KEYS:
        return True
    return f'"{key}"' in conf


def has_top_bar(conf):
    return "onair-now-playing" in conf or "herdr-onair/bin/now-playing" in conf


def reload_herdr():
    """(ok, message) from herdr's own config check."""
    try:
        out = subprocess.run([HERDR, "server", "reload-config"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"herdr didn't answer ({e}); the change applies the next time herdr starts"
    text = out.stdout + out.stderr
    if '"status":"applied"' in text.replace(" ", "") and '"diagnostics":[]' in text.replace(" ", ""):
        return True, "herdr reloaded its config"
    if out.returncode != 0 and "diagnostics" not in text:
        return None, "herdr isn't running here; the change applies the next time it starts"
    return False, text.strip()[:300]


def edit_conf(change):
    """Back up config.toml, apply change(text) -> text, reload; put the backup back if herdr objects."""
    old = read_conf()
    backup = f"{HERDR_CONF}.bak-onair-{time.strftime('%Y%m%d-%H%M%S')}"
    os.makedirs(os.path.dirname(HERDR_CONF), exist_ok=True)
    if old:
        with open(backup, "w", encoding="utf-8") as f:
            f.write(old)
    with open(HERDR_CONF, "w", encoding="utf-8") as f:
        f.write(change(old))
    ok, msg = reload_herdr()
    if ok is False:
        with open(HERDR_CONF, "w", encoding="utf-8") as f:
            f.write(old)
        reload_herdr()
        return False, f"herdr didn't accept the change, so your config is back as it was: {msg}"
    note = f" (backup: {backup.replace(core.HOME, '~', 1)})" if old else ""
    return True, msg + note


def add_key(key):
    block = (f'\n[[keys.command]]              # herdr-onair: open/close the radio (added by herdr-onair setup)\n'
             f'key = "{key}"\ntype = "plugin_action"\ncommand = "{TOGGLE}"\n')

    def change(text):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if TOGGLE in line and re.match(r"^\s*command\s*=", line):
                for j in range(i - 1, -1, -1):  # this block's key line, above the command
                    if lines[j].lstrip().startswith("[["):
                        break
                    if re.match(r"^\s*key\s*=", lines[j]):
                        lines[j] = f'key = "{key}"'
                        return "\n".join(lines) + "\n"
        return text.rstrip("\n") + "\n" + block
    return edit_conf(change)


def add_top_bar(path):
    entry = f'{{ type = "command", command = "{path}", interval_seconds = 1, timeout_seconds = 1 }}'

    def change(text):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*tab_bar_right\s*=\s*)\[(.*)$", line)
            if not m:
                continue
            rest = m.group(2)
            if rest.strip().startswith("]"):  # tab_bar_right = []
                lines[i] = f"{m.group(1)}[\n  {entry},  # herdr-onair\n]"
            elif "]" in rest and rest.count("[") < rest.count("]"):  # one line with entries
                head, _, tail = line.rpartition("]")
                lines[i] = f"{head.rstrip().rstrip(',')}, {entry} ]{tail}"
            else:  # spread over several lines: put ours first
                lines.insert(i + 1, f"  {entry},  # herdr-onair: what the radio plays")
            return "\n".join(lines) + "\n"
        block = f"tab_bar_right = [\n  {entry},  # herdr-onair: what the radio plays\n]"
        for i, line in enumerate(lines):
            if re.match(r"^\s*\[ui\]\s*(#.*)?$", line):
                lines.insert(i + 1, block)
                return "\n".join(lines) + "\n"
        return text.rstrip("\n") + f"\n\n[ui]\n{block}\n"
    return edit_conf(change)


class Setup:
    def __init__(self, scr):
        self.scr = scr
        self.step = 0
        self.sel = 0
        self.msg = ""
        self.msg_ok = True
        self.net = None  # None = checking, True / False
        self.key_done = ""
        self.bar_done = False
        self.open_radio = False

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        width = min(width if width is not None else w - x, w - x - (1 if y == h - 1 else 0))
        if width > 0:
            try:
                self.scr.addstr(y, x, core.fit(text, width), attr)
            except curses.error:
                pass

    def check_net(self):
        try:
            core.rb_get("/json/stats", {}, timeout=5)
            self.net = True
        except Exception:
            self.net = False

    # ---------------------------------------------------------------- steps

    def options(self):
        conf = read_conf()
        if self.step == 1:
            cur = current_key(conf)
            opts = []
            if cur:
                opts.append(("keep", f"Keep {cur}", "already set"))
            for k in KEY_IDEAS:
                key = f"prefix+{k}"
                if key != cur and not key_taken(key, conf):
                    opts.append((key, key, "free"))
                if len(opts) >= 5:
                    break
            opts.append(("skip", "Skip", "I'll add a key myself (see README)"))
            return opts
        if self.step == 2:
            if has_top_bar(conf):
                return [("keep", "Keep it", "already in your top bar")]
            return [("add", "Add it", "▶ Song · Artist  Station, on the right of herdr's top bar"),
                    ("skip", "Skip", "")]
        return []

    def draw(self):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        acc, dim, bold = self.ACC, curses.A_DIM, curses.A_BOLD
        self.put(0, 1, "RADIO", acc | bold, 5)
        self.put(0, 7, "· setup", dim, 8)
        x = 17
        for i, name in enumerate(STEPS):
            attr = (bold | curses.A_UNDERLINE) if i == self.step else (acc if i < self.step else dim)
            self.put(0, x, name, attr, len(name))
            x += len(name) + (3 if i < len(STEPS) - 1 else 0)
            if i < len(STEPS) - 1:
                self.put(0, x - 2, "›", dim, 1)
        self.put(1, 0, "─" * w, dim)
        y = 3
        conf = read_conf()
        if self.step == 0:
            self.put(y, 2, "What the radio needs", bold)
            y += 2
            rows = [
                (True, "python3", f"{platform.python_version()}", ""),
                (bool(shutil.which("mpv")), "mpv", "plays the radio" if shutil.which("mpv") else "needed to play",
                 "" if shutil.which("mpv") else install_cmd("mpv")),
                (bool(shutil.which("cava")) or None, "cava", "visualizer bars" if shutil.which("cava") else
                 "optional, for the visualizer bars", "" if shutil.which("cava") else install_cmd("cava")),
                (self.net, "Radio Browser", {None: "checking…", True: "reachable", False: "can't reach it (offline?)"}[self.net], ""),
            ]
            for ok, name, note, cmd in rows:
                mark, attr = {True: ("✓", acc), False: ("✗", self.ERR), None: ("·", dim)}[ok]
                self.put(y, 4, mark, attr | bold, 1)
                self.put(y, 6, name, bold, 16)
                self.put(y, 22, note, dim, w - 24)
                y += 1
                if cmd:
                    self.put(y, 22, "→ " + cmd, acc, w - 24)
                    y += 1
            y += 1
            if not shutil.which("mpv"):
                self.put(y, 2, "Install mpv, then come back (setup checks again each time you open it).", dim)
                y += 1
            self.put(y, 2, "Stations come from Radio Browser, a free community directory of 50,000+ stations.", dim)
        elif self.step in (1, 2):
            if self.step == 1:
                prefix = herdr_prefix(conf)
                self.put(y, 2, "A key to open and close the radio", bold)
                self.put(y + 1, 2, f"Your herdr prefix is {prefix}, so prefix+y means {prefix}, then y.", dim)
            else:
                self.put(y, 2, "What's playing, in herdr's top bar", bold)
                self.put(y + 1, 2, "Also puts the `onair` command in ~/.local/bin, for desktop hotkeys.", dim)
            y += 3
            for i, (_, label, note) in enumerate(self.options()):
                if i == self.sel:
                    self.put(y, 2, "▌", acc | bold, 1)
                self.put(y, 4, label, (bold | acc) if i == self.sel else 0, 22)
                self.put(y, 27, note, dim, w - 29)
                y += 1
        else:
            key = current_key(conf)
            self.put(y, 2, "All set", acc | bold)
            y += 2
            lines = [f"Open the radio with {key} (the same key closes it)." if key else
                     "Open the radio with the herdr-onair.toggle action (add a key: see README).",
                     "Search with /, play with enter, like a song with l, switch layouts with v.",
                     f"Liked songs are saved per station in {core.likes_dir().replace(core.HOME, '~', 1)}/.",
                     "Run setup again any time from the herdr-onair \"setup\" action."]
            for line in lines:
                self.put(y, 4, line)
                y += 1
        if self.msg:
            self.put(h - 3, 2, self.msg, (acc if self.msg_ok else self.ERR) | bold, w - 4)
        hint = {0: "enter next   q close", 1: "↑↓ choose   enter next   b back   q close",
                2: "↑↓ choose   enter next   b back   q close", 3: "enter open the radio   b back   q close"}[self.step]
        self.put(h - 1, 2, hint, dim)
        s.refresh()

    def choose(self):
        opts = self.options()
        choice = opts[self.sel][0] if opts else None
        self.msg = ""
        if self.step == 1 and choice not in (None, "keep", "skip"):
            ok, msg = add_key(choice)
            self.msg, self.msg_ok = (f"Added {choice}: {msg}" if ok is not False else msg), ok is not False
            if ok is False:
                return
        if self.step == 2 and choice == "add":
            paths = core.install_commands(os.path.join(core.HOME, ".local", "bin"))
            ok, msg = add_top_bar(paths[1])
            self.msg, self.msg_ok = (f"Added the top-bar line: {msg}" if ok is not False else msg), ok is not False
            if ok is False:
                return
        if self.step == 3:
            self.open_radio = True
            return "quit"
        self.step += 1
        self.sel = 0

    def run(self):
        curses.curs_set(0)
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, 6, -1)
            curses.init_pair(2, 1, -1)
        except curses.error:
            pass
        self.ACC, self.ERR = curses.color_pair(1), curses.color_pair(2)
        self.scr.timeout(200)
        self.scr.keypad(True)
        threading.Thread(target=self.check_net, daemon=True).start()
        while True:
            self.draw()
            try:
                k = self.scr.get_wch()
            except curses.error:
                continue
            if k in ("q", "\x1b"):
                return
            if k in ("\n", "\r", curses.KEY_ENTER):
                if self.choose() == "quit":
                    return
            elif k in ("b", curses.KEY_LEFT) and self.step > 0:
                self.step -= 1
                self.sel = 0
                self.msg = ""
            elif k in (curses.KEY_DOWN, "j"):
                self.sel = min(self.sel + 1, max(0, len(self.options()) - 1))
            elif k in (curses.KEY_UP, "k"):
                self.sel = max(0, self.sel - 1)


def main():
    os.environ.setdefault("ESCDELAY", "25")
    holder = {}

    def run(scr):
        holder["s"] = Setup(scr)
        holder["s"].run()
    try:
        curses.wrapper(run)
    finally:
        core.set_setting("setup_done", "1")  # don't open by itself again; the action still works
    if holder.get("s") and holder["s"].open_radio:
        exe = os.path.join(core.ROOT, "bin", "onair")
        subprocess.Popen(["sh", "-c", f'sleep 0.4; exec "{sys.executable}" "{exe}" toggle'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
