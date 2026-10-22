"""
Tibo says:  -  a tiny desktop watcher for @thsottiaux's posts.

Built for one job: spotting when Tibo hints at (or announces) new Codex
usage-limit resets. Pure Python standard library, no installs.

Run:  double-click tibo_says.pyw   (needs Python 3.9+ from python.org)
Exe:  run build.bat
Settings live in %APPDATA%\\TiboSays\\config.json (Settings button opens it).
"""

import ctypes
import html
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    import tkinter as tk
    from tkinter import font as tkfont
except Exception:  # lets the parsing code be imported/tested without Tk
    tk = None
    tkfont = None

APP_NAME = "Tibo says:"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SYND_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{user}"

DEFAULT_CONFIG = {
    "username": "thsottiaux",
    "poll_minutes": 5,
    # Optional but the most reliable source: an X API bearer token with read access.
    # (You can also set the X_BEARER_TOKEN environment variable instead.)
    "x_bearer_token": "",
    # RSS feeds tried in order until one works. {user} is replaced by the username.
    # Public Nitter instances come and go - swap in any RSS/RSS-Bridge/RSSHub URL.
    "rss_urls": [
        "https://nitter.jaydenha.uk/{user}/rss",
        "https://nitter.meowing.monster/{user}/rss",
        "https://nitter.netbub.com/{user}/rss",
    ],
    "use_x_page_source": True,
    "include_replies": True,
    # Anything matching these (substring, case-insensitive) is highlighted.
    "keywords": ["reset", "rate limit", "usage limit", "limit", "bank", "tonight",
                 "24 hours", "compensat", "capacity", "credits", "codex"],
    # Matching one of these gets the loud alert and a "RESET?" badge.
    "hot_keywords": ["reset"],
    "sound": True,
    "always_on_top": False,
    "max_tweets": 40,
}

# --------------------------------------------------------------------------- paths / config / state

def data_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "TiboSays")
    os.makedirs(path, exist_ok=True)
    return path

CONFIG_PATH = lambda: os.path.join(data_dir(), "config.json")
STATE_PATH = lambda: os.path.join(data_dir(), "state.json")
LOG_PATH = lambda: os.path.join(data_dir(), "error.log")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    path = CONFIG_PATH()
    raw = load_json(path, None)
    if raw is None:
        save_json(path, DEFAULT_CONFIG)
        raw = {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["poll_minutes"] = max(1, float(cfg.get("poll_minutes") or 5))
    cfg["username"] = str(cfg["username"]).lstrip("@")
    return cfg


def update_config_key(key, value):
    path = CONFIG_PATH()
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        raw = dict(DEFAULT_CONFIG)
    raw[key] = value
    save_json(path, raw)

# --------------------------------------------------------------------------- tweets: parsing

def snowflake_ts(tweet_id):
    try:
        return ((int(tweet_id) >> 22) + 1288834974657) / 1000.0
    except Exception:
        return time.time()


def clean_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+\n", "\n", s).strip()


def make_tweet(tid, text, ts, user, reply_to=None):
    return {
        "id": str(tid),
        "text": text,
        "ts": ts,
        "url": f"https://x.com/{user}/status/{tid}",
        "reply_to": reply_to,
    }


def parse_rss(data, user):
    root = ET.fromstring(data)
    out = []
    for item in root.iter("item"):
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or ""
        m = re.search(r"/status/(\d+)", link) or re.search(r"/status/(\d+)", guid)
        if not m:
            continue
        owner = re.search(r"//[^/]+/([^/]+)/status/", link)
        if owner and owner.group(1).lower() != user.lower():
            continue  # retweet of someone else
        title = clean_text(item.findtext("title") or "")
        if title.startswith("RT by ") or title.startswith("RT @"):
            continue
        reply_to = None
        rm = re.match(r"R to @(\w+):\s*", title)
        if rm:
            reply_to, title = rm.group(1), title[rm.end():]
        if not title:
            title = clean_text(item.findtext("description") or "")
        pub = item.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(pub).timestamp() if pub else snowflake_ts(m.group(1))
        except Exception:
            ts = snowflake_ts(m.group(1))
        out.append(make_tweet(m.group(1), title, ts, user, reply_to))
    return out


def parse_syndication(page, user):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    entries = (data.get("props", {}).get("pageProps", {})
               .get("timeline", {}).get("entries", []))
    out = []
    for e in entries:
        if e.get("type") != "tweet":
            continue
        t = (e.get("content") or {}).get("tweet") or {}
        tid = t.get("id_str")
        if not tid or t.get("retweeted_status"):
            continue
        author = ((t.get("user") or {}).get("screen_name") or "").lower()
        if author and author != user.lower():
            continue
        text = t.get("full_text") or t.get("text") or ""
        for u in (t.get("entities") or {}).get("urls", []) or []:
            if u.get("url"):
                text = text.replace(u["url"], u.get("expanded_url") or u.get("display_url") or u["url"])
        text = re.sub(r"https://t\.co/\w+", "", text)  # leftover media links
        text = html.unescape(text).strip()
        try:
            ts = datetime.strptime(t["created_at"], "%a %b %d %H:%M:%S %z %Y").timestamp()
        except Exception:
            ts = snowflake_ts(tid)
        out.append(make_tweet(tid, text, ts, user, t.get("in_reply_to_screen_name")))
    return out


def parse_api(obj, user):
    out = []
    for t in obj.get("data", []) or []:
        tid = t.get("id")
        if not tid:
            continue
        refs = t.get("referenced_tweets") or []
        if any(r.get("type") == "retweeted" for r in refs):
            continue
        reply = any(r.get("type") == "replied_to" for r in refs)
        try:
            ts = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = snowflake_ts(tid)
        out.append(make_tweet(tid, html.unescape(t.get("text", "")), ts, user,
                              "someone" if reply else None))
    return out

# --------------------------------------------------------------------------- tweets: fetching

def http_get(url, headers=None, timeout=15):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def short_err(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return "unreachable"
    if isinstance(e, TimeoutError):
        return "timed out"
    return type(e).__name__


def host_of(url):
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def fetch_api(user, token, user_id):
    hdr = {"Authorization": f"Bearer {token}"}
    if not user_id:
        j = json.loads(http_get(f"https://api.x.com/2/users/by/username/{user}", hdr))
        user_id = j["data"]["id"]
    url = (f"https://api.x.com/2/users/{user_id}/tweets?max_results=20&exclude=retweets"
           "&tweet.fields=created_at,referenced_tweets")
    return parse_api(json.loads(http_get(url, hdr)), user), user_id


def fetch_all(cfg, user_id=None):
    """Ask every configured source and merge. Returns {tweets, status, user_id}."""
    user = cfg["username"]
    tweets, status = [], []

    token = cfg.get("x_bearer_token") or os.environ.get("X_BEARER_TOKEN", "")
    if token:
        try:
            got, user_id = fetch_api(user, token, user_id)
            tweets += got
            status.append(("X API", True, f"{len(got)} posts"))
        except Exception as e:
            status.append(("X API", False, short_err(e)))

    if cfg.get("rss_urls"):
        errs, ok = [], False
        for raw in cfg["rss_urls"]:
            url = raw.replace("{user}", user)
            try:
                got = parse_rss(http_get(url), user)
                if got:
                    tweets += got
                    status.append(("Feed", True, host_of(url)))
                    ok = True
                    break
                errs.append(f"{host_of(url)}: empty")
            except Exception as e:
                errs.append(f"{host_of(url)}: {short_err(e)}")
        if not ok:
            status.append(("Feed", False, "; ".join(errs)))

    if cfg.get("use_x_page_source", True):
        try:
            got = parse_syndication(http_get(SYND_URL.format(user=user)).decode("utf-8", "replace"), user)
            if got:
                tweets += got
                status.append(("X page", True, f"{len(got)} posts"))
            else:
                status.append(("X page", False, "no posts returned"))
        except Exception as e:
            status.append(("X page", False, short_err(e)))

    if not cfg.get("include_replies", True):
        tweets = [t for t in tweets if not t.get("reply_to")]
    return {"tweets": tweets, "status": status, "user_id": user_id}


def merge_tweets(old, new, limit):
    by_id = {t["id"]: t for t in old}
    for t in new:
        by_id[t["id"]] = t  # fresher copy wins
    merged = sorted(by_id.values(), key=lambda t: (t["ts"], int(t["id"])), reverse=True)
    return merged[:limit]

# --------------------------------------------------------------------------- helpers for display

def build_regex(words):
    words = [w for w in words if w]
    if not words:
        return None
    return re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)), re.I)


def rel_time(ts, now=None):
    d = (now or time.time()) - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]

# --------------------------------------------------------------------------- the window

BG, CARD, FG, MUTED = "#14141b", "#1d1d28", "#ececf3", "#8b8b9c"
ACCENT, HOT, NEWC = "#ffb454", "#ff5c7a", "#5ee0a0"


class TiboSays:
    def __init__(self):
        self.cfg = load_config()
        self.state = load_json(STATE_PATH(), {})
        self.first_run = not self.state.get("seen")
        self.seen = set(self.state.get("seen", []))
        self.unread = set(self.state.get("unread", []))
        self.tweets = self.state.get("tweets", [])
        self.user_id = self.state.get("user_id")
        self.status = []
        self.notes = ""
        self.q = queue.Queue()
        self.busy = False
        self.next_poll = 0.0
        self.last_checked = None
        self.last_render = 0.0
        self.compile_words()

        root = self.root = tk.Tk()
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.geometry(self.state.get("geometry") or "400x580")
        root.minsize(320, 300)
        root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.f_body = tkfont.Font(family="Segoe UI", size=10)
        self.f_small = tkfont.Font(family="Segoe UI", size=8)
        self.f_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        f_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(top, text=APP_NAME, font=f_title, fg=ACCENT, bg=BG).pack(side="left")
        self.btn_settings = self.small_button(top, "Settings", self.open_settings)
        self.btn_sound = self.small_button(top, "", self.toggle_sound)
        self.btn_pin = self.small_button(top, "", self.toggle_pin)
        self.btn_refresh = self.small_button(top, "Refresh", self.refresh_now)
        self.update_toggle_labels()

        self.status_lbl = tk.Label(root, text="", font=self.f_small, fg=MUTED, bg=BG,
                                   anchor="w", justify="left", wraplength=370)
        self.status_lbl.pack(fill="x", padx=12, pady=(2, 6))

        mid = tk.Frame(root, bg=BG)
        mid.pack(fill="both", expand=True, padx=(8, 0))
        self.text = tk.Text(mid, bg=BG, fg=FG, bd=0, highlightthickness=0, wrap="word",
                            font=self.f_body, cursor="arrow", state="disabled",
                            padx=4, pady=2, insertwidth=0)
        sb = tk.Scrollbar(mid, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.setup_tags()

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=12, pady=6)
        link = tk.Label(foot, text=f"Open @{self.cfg['username']} on X", font=self.f_small,
                        fg=ACCENT, bg=BG, cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e: webbrowser.open(f"https://x.com/{self.cfg['username']}"))
        self.small_button(foot, "Mark all read", self.mark_all_read)

        self.render()
        self.tick()

    # ---- small UI bits
    def small_button(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, font=self.f_small, fg=FG, bg=CARD,
                      activebackground="#2a2a3a", activeforeground=FG, bd=0, padx=7, pady=2,
                      cursor="hand2", relief="flat")
        b.pack(side="right", padx=(4, 0))
        return b

    def update_toggle_labels(self):
        self.btn_sound.config(text="Sound on" if self.cfg["sound"] else "Sound off")
        self.btn_pin.config(text="Pinned" if self.cfg["always_on_top"] else "Pin")

    def setup_tags(self):
        t = self.text
        t.tag_config("card", background=CARD, lmargin1=10, lmargin2=10, rmargin=10)
        t.tag_config("head", foreground=MUTED, font=self.f_small, spacing1=8)
        t.tag_config("new", foreground=NEWC, font=self.f_bold)
        t.tag_config("badge", foreground=BG, background=HOT, font=self.f_bold)
        t.tag_config("body", spacing1=2, spacing3=8)
        t.tag_config("kw", foreground=ACCENT, font=self.f_bold)
        t.tag_config("hot", foreground=HOT, font=self.f_bold)
        t.tag_config("gap", font=tkfont.Font(family="Segoe UI", size=4))
        t.tag_config("msg", foreground=MUTED, spacing1=6, spacing3=6, lmargin1=6, lmargin2=6)
        t.tag_config("err", foreground=HOT, lmargin1=6, lmargin2=6)
        t.tag_raise("sel")

    def compile_words(self):
        hot = list(self.cfg.get("hot_keywords", []))
        self.hot_re = build_regex(hot)
        self.kw_re = build_regex(list(self.cfg.get("keywords", [])) + hot)

    # ---- rendering
    def render(self):
        t = self.text
        top = t.yview()[0]
        t.config(state="normal")
        t.delete("1.0", "end")
        now = time.time()

        if not self.tweets:
            t.insert("end", "Nothing to show yet.\n", "msg")
            if self.notes:
                t.insert("end", self.notes + "\n", "err")

        for i, tw in enumerate(self.tweets):
            tag = f"tw{i}"
            unread = tw["id"] in self.unread
            hot = bool(self.hot_re and self.hot_re.search(tw["text"]))
            tags_h, tags_b = ("card", "head", tag), ("card", "body", tag)
            if unread:
                t.insert("end", "NEW  ", ("card", "new", tag))
            when = datetime.fromtimestamp(tw["ts"]).strftime("%b %d, %H:%M")
            line = f"{rel_time(tw['ts'], now)}  ·  {when}"
            if tw.get("reply_to"):
                who = tw["reply_to"]
                line += f"  ·  reply to @{who}" if who != "someone" else "  ·  reply"
            t.insert("end", line + "  ", tags_h)
            if hot:
                t.insert("end", " RESET? ", ("card", "badge", tag))
            t.insert("end", "\n", tags_h)

            text, pos = tw["text"], 0
            if self.kw_re:
                for m in self.kw_re.finditer(text):
                    t.insert("end", text[pos:m.start()], tags_b)
                    is_hot = bool(self.hot_re and self.hot_re.fullmatch(m.group(0)))
                    t.insert("end", m.group(0), ("card", "body", "hot" if is_hot else "kw", tag))
                    pos = m.end()
            t.insert("end", text[pos:] + "\n", tags_b)
            t.insert("end", "\n", "gap")

            t.tag_bind(tag, "<ButtonRelease-1>", lambda e, tw=tw: self.open_tweet(tw))
            t.tag_bind(tag, "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.config(cursor="arrow"))

        t.config(state="disabled")
        t.yview_moveto(top)
        self.last_render = now
        self.update_title()

    def update_title(self):
        n = len(self.unread)
        self.root.title(f"{APP_NAME} ({n} new)" if n else APP_NAME)

    def update_status(self):
        bits = []
        for name, ok, _ in self.status:
            bits.append(f"{name} {'✓' if ok else '✗'}")
        left = "  ·  ".join(bits) if bits else "starting…"
        if self.busy:
            right = "checking…"
        elif self.last_checked:
            wait = max(0, int(self.next_poll - time.time()))
            right = f"checked {datetime.fromtimestamp(self.last_checked).strftime('%H:%M')} · next in {wait // 60}:{wait % 60:02d}"
        else:
            right = ""
        self.status_lbl.config(text=f"{left}    {right}".strip())

    # ---- actions
    def open_tweet(self, tw):
        if self.text.tag_ranges("sel"):
            return  # user was selecting text, not clicking
        self.unread.discard(tw["id"])
        self.save_state()
        self.render()
        webbrowser.open(tw["url"])

    def mark_all_read(self):
        self.unread.clear()
        self.save_state()
        self.render()

    def toggle_sound(self):
        self.cfg["sound"] = not self.cfg["sound"]
        update_config_key("sound", self.cfg["sound"])
        self.update_toggle_labels()

    def toggle_pin(self):
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        update_config_key("always_on_top", self.cfg["always_on_top"])
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        self.update_toggle_labels()

    def open_settings(self):
        path = CONFIG_PATH()
        load_config()  # makes sure the file exists
        try:
            os.startfile(path)  # Windows
        except AttributeError:
            webbrowser.open("file://" + path)

    def refresh_now(self):
        self.next_poll = 0

    # ---- polling
    def start_poll(self):
        self.busy = True
        self.cfg = load_config()  # picks up Settings edits
        self.compile_words()
        self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        self.update_toggle_labels()
        threading.Thread(target=self.worker, args=(self.cfg, self.user_id), daemon=True).start()

    def worker(self, cfg, user_id):
        try:
            res = fetch_all(cfg, user_id)
        except Exception as e:  # never let the thread die silently
            res = {"tweets": [], "status": [("Fetch", False, short_err(e))], "user_id": user_id}
        self.q.put(res)

    def on_result(self, res):
        self.busy = False
        self.last_checked = time.time()
        self.next_poll = self.last_checked + self.cfg["poll_minutes"] * 60
        self.status = res["status"]
        self.user_id = res.get("user_id") or self.user_id

        fetched = res["tweets"]
        if fetched:
            self.notes = ""
            max_seen = max([int(i) for i in self.seen if i.isdigit()] or [0])
            new = []
            if not self.first_run:
                for tw in fetched:
                    is_new = tw["id"] not in self.seen and (
                        int(tw["id"]) > max_seen or time.time() - tw["ts"] < 6 * 3600)
                    if is_new:
                        new.append(tw)
            self.seen |= {tw["id"] for tw in fetched}
            self.first_run = False
            self.tweets = merge_tweets(self.tweets, fetched, int(self.cfg["max_tweets"]))
            if new:
                self.unread |= {tw["id"] for tw in new}
                self.alert(new)
        else:
            fails = [f"{n}: {d}" for n, ok, d in self.status if not ok]
            self.notes = ("Couldn't read Tibo's posts from any source:\n" + "\n".join(fails) +
                          "\n\nFix: add an X API token or a working RSS URL in Settings.")
        self.save_state()
        self.render()

    def alert(self, new):
        hot = any(self.hot_re and self.hot_re.search(t["text"]) for t in new)
        kw = any(self.kw_re and self.kw_re.search(t["text"]) for t in new)
        self.flash_taskbar()
        if self.cfg["sound"] and (hot or kw):
            self.beep(loud=hot)
        if hot:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            pinned = bool(self.cfg["always_on_top"])
            self.root.after(2500, lambda: self.root.attributes("-topmost", pinned))

    def beep(self, loud):
        try:
            import winsound
            winsound.MessageBeep(0x30 if loud else 0x40)
        except Exception:
            self.root.bell()

    def flash_taskbar(self):
        if os.name != "nt":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x0F, 0, 0)  # tray + caption until focused
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def tick(self):
        try:
            while True:
                self.on_result(self.q.get_nowait())
        except queue.Empty:
            pass
        now = time.time()
        if not self.busy and now >= self.next_poll:
            self.start_poll()
        if now - self.last_render > 60:
            self.render()  # keeps "5m ago" fresh
        self.update_status()
        self.root.after(500, self.tick)

    # ---- state / lifecycle
    def save_state(self):
        keep = sorted(self.seen, key=lambda i: int(i) if i.isdigit() else 0)[-500:]
        self.state.update({
            "seen": keep, "unread": sorted(self.unread), "tweets": self.tweets,
            "user_id": self.user_id, "geometry": self.root.geometry(),
        })
        try:
            save_json(STATE_PATH(), self.state)
        except Exception:
            pass

    def close(self):
        self.save_state()
        self.root.destroy()


def main():
    if tk is None:
        raise SystemExit("Tkinter is missing - install Python from python.org (it ships with Tk).")
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    TiboSays().root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            with open(LOG_PATH(), "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now()} ---\n{traceback.format_exc()}")
        except Exception:
            pass
        raise
