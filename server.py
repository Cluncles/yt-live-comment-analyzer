#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
import json
import queue
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "mentions.db"
CONFIG_PATH = ROOT / "hashtags.json"
KEYWORD_CONFIG_PATH = ROOT / "keywords.json"
DASHBOARD = ROOT / "dashboard.html"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
VIDEO_RE = re.compile(r"(?:v=|youtu\.be/|embed/|live/|shorts/)([A-Za-z0-9_-]{11})")


PHL = timezone(timedelta(hours=8), "PHT")


def ph_now() -> str:
    return datetime.now(PHL).isoformat(timespec="seconds")


def video_id_from(value: str) -> str | None:
    value = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    match = VIDEO_RE.search(value)
    return match.group(1) if match else None


class Store:
    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS mentions ("
            "id TEXT PRIMARY KEY, user TEXT NOT NULL, message TEXT NOT NULL, "
            "variant TEXT NOT NULL, video_id TEXT, source TEXT, ts TEXT NOT NULL)"
        )
        self.db.commit()

    def delete(self, record_id: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM mentions WHERE id = ?", (record_id,))
            self.db.commit()
            return cur.rowcount > 0

    def clear(self) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM mentions")
            self.db.commit()
            return cur.rowcount

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def add(self, match: dict) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO mentions VALUES (?,?,?,?,?,?,?)",
                (
                    match["id"], match["user"], match["message"],
                    match["variant"], match.get("video_id"),
                    match.get("source"), match["ts"],
                ),
            )
            self.db.commit()
            return cur.rowcount > 0

    def all(self, limit: int = 500) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,user,message,variant,video_id,source,ts "
                "FROM mentions ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ("id", "user", "message", "variant", "video_id", "source", "ts")
        return [dict(zip(keys, row)) for row in rows]

    def stats(self) -> dict:
        with self.lock:
            total, users = self.db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT user) FROM mentions"
            ).fetchone()
            latest = self.db.execute(
                "SELECT ts FROM mentions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return {"mentions": total, "users": users, "latest": latest[0] if latest else None}

    def aggregates(self) -> dict:
        rows = self.all(100000)
        users = {}
        terms = {}
        for row in rows:
            name = row["user"]
            key = (row["variant"] or "").lower()
            kind = "keyword" if ":keyword:" in row["id"] else "hashtag"
            entry = users.setdefault(name, {"hashtags": 0, "keywords": 0, "total": 0, "terms": {}})
            entry[kind + "s"] += 1
            entry["total"] += 1
            entry["terms"][key] = entry["terms"].get(key, 0) + 1
            term = terms.setdefault(key, {"term": key, "kind": kind, "users": set(), "mentions": 0})
            term["users"].add(name)
            term["mentions"] += 1
        return {
            "mentions": len(rows),
            "users": len(users),
            "by_user": [
                {"user": name, "hashtags": data["hashtags"], "keywords": data["keywords"],
                 "total": data["total"], "terms": list(data["terms"].items())}
                for name, data in sorted(users.items(), key=lambda item: -item[1]["total"])
            ],
            "terms": [
                {"term": data["term"], "kind": data["kind"], "users": len(data["users"]),
                 "mentions": data["mentions"]}
                for data in sorted(terms.values(), key=lambda data: -len(data["users"]))
            ],
        }

    def jsonl(self) -> str:
        return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.all(100000))


class TermStore:

    def __init__(self, path: Path, default: list[str], config_key: str,
                 include_hash: bool, allow_spaces: bool, extra_chars: str = ""):
        self.path = path
        self.default = default
        self.config_key = config_key
        self.include_hash = include_hash
        self.allow_spaces = allow_spaces
        self.extra_chars = extra_chars
        self.lock = threading.Lock()
        self.terms = list(default)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raw = None
        if raw is None:
            return
        if not raw.strip():
            self.terms = []
            return
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if isinstance(data, dict) and isinstance(data.get(config_key), list):
            self.terms = self._clean(data[config_key])

    def _clean(self, values) -> list[str]:
        terms: list[str] = []
        pattern = r"[^\w " + re.escape(self.extra_chars) + r"]" if self.allow_spaces else r"[^\w]"
        for value in values:
            term = re.sub(r"^#+", "", str(value).strip()).strip().lower()
            term = re.sub(r"\s+", " ", term)
            term = re.sub(pattern, "", term).strip()
            if term and term not in terms:
                terms.append(term)
        return terms

    def list(self) -> list[str]:
        with self.lock:
            return list(self.terms)

    def set(self, values) -> list[str]:
        terms = self._clean(values)
        with self.lock:
            self.terms = terms
            self.path.write_text(
                json.dumps({self.config_key: terms}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return list(terms)

    def regex(self):
        terms = self.list()
        if not terms:
            return None
        ordered = sorted(terms, key=len, reverse=True)
        body = "|".join(re.escape(term) for term in ordered)
        lookbehind = r"(?<!\w)" if self.include_hash else r"(?<![#\w])"
        prefix = "#" if self.include_hash else ""
        return re.compile(lookbehind + prefix + r"(?:" + body + r")\b", re.IGNORECASE)


class HashtagStore(TermStore):
    def __init__(self, path: Path):
        super().__init__(path, ["palawan"], "hashtags", True, False)


class KeywordStore(TermStore):
    def __init__(self, path: Path):
        super().__init__(path, [], "keywords", False, True, "-.")


class ChatFeed:
    def __init__(self, limit: int = 300):
        self.limit = limit
        self.items: deque[dict] = deque(maxlen=limit)
        self.seen: deque[str] = deque(maxlen=2000)
        self.ids: set[str] = set()
        self.lock = threading.Lock()

    def add(self, text: str, user: str, message_id: str,
            video_id: str, source: str) -> dict | None:
        key = message_id or (user + "|" + text)
        with self.lock:
            if key in self.ids:
                return None
            if len(self.seen) == self.seen.maxlen:
                self.ids.discard(self.seen[0])
            self.ids.add(key)
            self.seen.append(key)
            item = {
                "id": key,
                "user": user or "unknown",
                "message": text,
                "source": source,
                "video_id": video_id,
                "ts": ph_now(),
            }
            self.items.append(item)
            return item

    def list(self) -> list[dict]:
        with self.lock:
            return list(self.items)

    def clear(self) -> None:
        with self.lock:
            self.items.clear()
            self.seen.clear()
            self.ids.clear()


class Hub:

    def __init__(self):
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(client)

    def publish(self, kind: str, payload: dict) -> None:
        frame = ("event: " + kind + "\ndata: " +
                 json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.put_nowait(frame)
            except queue.Full:
                pass


def http_get(url: str, headers: dict | None = None, timeout: int = 20) -> str:
    base = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        base.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=base), timeout=timeout) as res:
        return res.read().decode("utf-8", "ignore")


def http_post(url: str, payload: dict, headers: dict | None = None, timeout: int = 25) -> dict:
    base = {
        "User-Agent": UA, "Content-Type": "application/json",
        "Origin": "https://www.youtube.com", "Referer": "https://www.youtube.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        base.update(headers)
    body = json.dumps(payload).encode("utf-8")
    with urllib.request.urlopen(
        urllib.request.Request(url, data=body, headers=base, method="POST"), timeout=timeout
    ) as res:
        return json.loads(res.read().decode("utf-8", "ignore"))


def balanced_object(text: str, start: int) -> dict | None:
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for pos in range(i, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:pos + 1])
                except json.JSONDecodeError:
                    return None
    return None


def video_details(video_id: str, api_key: str = "") -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = {"title": "", "channel": "", "url": url}
    if api_key:
        try:
            params = urllib.parse.urlencode({
                "part": "snippet", "id": video_id, "key": api_key,
            })
            data = json.loads(http_get(f"https://www.googleapis.com/youtube/v3/videos?{params}"))
            snippet = data["items"][0]["snippet"]
            info["title"] = snippet.get("title", "")
            info["channel"] = snippet.get("channelTitle", "")
            return info
        except Exception:
            pass
    try:
        html = http_get(url)
        player = balanced_object(html, html.find("ytInitialPlayerResponse"))
        details = player.get("videoDetails", {}) if player else {}
        info["title"] = details.get("title", "")
        info["channel"] = details.get("author", "")
    except Exception:
        pass
    return info


def walk(node, fn):
    if isinstance(node, dict):
        fn(node)
        for value in node.values():
            walk(value, fn)
    elif isinstance(node, list):
        for value in node:
            walk(value, fn)


class InnertubeChat:

    def __init__(self, video_id: str):
        self.video_id = video_id
        self.key = ""
        self.version = "2.20250312.04.00"
        self.token = ""

    def bootstrap(self) -> None:
        html = http_get(f"https://www.youtube.com/watch?v={self.video_id}")
        key = re.search(r'INNERTUBE_API_KEY\s*["\']\s*:\s*"([^"]+)"', html)
        ver = re.search(r'INNERTUBE_CONTEXT_CLIENT_VERSION\s*["\']\s*:\s*"([^"]+)"', html)
        if not key:
            raise RuntimeError("Could not read YouTube page configuration (INNERTUBE_API_KEY).")
        self.key = key.group(1)
        if ver:
            self.version = ver.group(1)
        initial = balanced_object(html, html.find("ytInitialData"))
        if not initial:
            raise RuntimeError("Could not read YouTube initial data (ytInitialData).")
        renderer = {}
        walk(initial, lambda n: renderer.update(n) if "liveChatRenderer" in n else None)
        tokens: list[str] = []
        source = renderer.get("liveChatRenderer") or initial
        walk(source, lambda n: tokens.append(n["continuation"]) if
             isinstance(n.get("continuation"), str) and len(n["continuation"]) > 60 else None)
        if not tokens:
            raise RuntimeError("No live chat continuation found — is the stream live with chat enabled?")
        self.token = max(tokens, key=len)

    def poll(self) -> tuple[list[dict], str]:
        response = http_post(
            f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={self.key}",
            {
                "context": {"client": {
                    "clientName": "WEB", "clientVersion": self.version, "hl": "en",
                }},
                "continuation": self.token,
            },
        )
        renderers: list[dict] = []
        walk(response, lambda n: renderers.append(n["liveChatTextMessageRenderer"])
             if "liveChatTextMessageRenderer" in n else None)
        tokens: list[str] = []
        walk(response, lambda n: tokens.append(n["continuation"]) if
             isinstance(n.get("continuation"), str) and len(n["continuation"]) > 60 else None)
        return renderers, (max(tokens, key=len) if tokens else self.token)


class OfficialChat:

    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, video_id: str, api_key: str):
        self.video_id = video_id
        self.key = api_key
        self.chat_id = ""

    def bootstrap(self) -> None:
        url = (f"{self.BASE}/videos?part=liveStreamingDetails&id={self.video_id}"
               f"&key={urllib.parse.quote(self.key)}")
        data = json.loads(http_get(url))
        items = data.get("items", [])
        if not items:
            raise RuntimeError("Video not found or API key rejected.")
        details = items[0].get("liveStreamingDetails", {})
        self.chat_id = details.get("activeLiveChatId", "")
        if not self.chat_id:
            raise RuntimeError("No active live chat — the stream may be offline or chat disabled.")

    def poll(self) -> tuple[list[dict], int]:
        params = urllib.parse.urlencode({
            "part": "snippet,authorDetails", "liveChatId": self.chat_id,
            "key": self.key, "maxResults": "200",
        })
        data = json.loads(http_get(f"{self.BASE}/liveChat/messages?{params}"))
        return data.get("items", []), int(data.get("pollingIntervalMillis") or 10000)


class Tracker(threading.Thread):
    def __init__(self, hub: Hub, store: Store, hashtags: HashtagStore,
                 keywords: KeywordStore, feed: ChatFeed, video_id: str,
                 source: str, api_key: str = ""):
        super().__init__(daemon=True)
        self.hub, self.store = hub, store
        self.hashtags, self.keywords = hashtags, keywords
        self.feed = feed
        self.video_id = video_id
        self.source = source
        self.api_key = api_key
        self.done = threading.Event()

    def stop(self) -> None:
        self.done.set()

    def emit(self, text: str, user: str, message_id: str, variant: str = "",
             kind: str = "hashtag") -> None:
        match = {
            "id": f"{self.source}:{message_id}:{kind}:{variant}",
            "user": user or "unknown",
            "message": text,
            "variant": variant,
            "video_id": self.video_id,
            "source": self.source,
            "ts": ph_now(),
        }
        if not self.store.add(match):
            return
        self.hub.publish("match", match)
        self.hub.publish("stats", self.store.stats())

    def record_chat(self, text: str, user: str, message_id: str) -> None:
        item = self.feed.add(text, user, message_id, self.video_id, self.source)
        if item:
            self.hub.publish("chat", item)

    def read_text(self, author: str, text: str, message_id: str) -> None:
        patterns = ((self.hashtags.regex(), "hashtag"),
                    (self.keywords.regex(), "keyword"))
        for pattern, kind in patterns:
            if not pattern:
                continue
            for found in pattern.finditer(text or ""):
                self.emit(text, author, message_id, found.group(0), kind)

    def run(self) -> None:
        self.hub.publish("status", {
            "state": "connecting", "source": self.source,
            "video_id": self.video_id, "message": "Connecting to chat…",
        })
        try:
            if self.api_key:
                self.run_official()
            else:
                self.run_innertube()
        except Exception as exc:
            self.hub.publish("status", {
                "state": "error", "source": self.source,
                "video_id": self.video_id, "message": str(exc),
            })

    def run_innertube(self) -> None:
        chat = InnertubeChat(self.video_id)
        chat.bootstrap()
        self.hub.publish("status", {
            "state": "live", "source": "innertube", "video_id": self.video_id,
            "message": "Listening to public live chat",
        })
        while not self.done.is_set():
            try:
                renderers, token = chat.poll()
                if token:
                    chat.token = token
                for renderer in renderers:
                    name = renderer.get("authorName", {})
                    author = name.get("simpleText") or (
                        name.get("runs", [{}])[0].get("text", "") if name.get("runs") else ""
                    )
                    text = "".join(
                        run.get("text", "") for run in
                        renderer.get("message", {}).get("runs", [])
                    )
                    message_id = renderer.get("id", "") or text
                    self.record_chat(text, author, message_id)
                    self.read_text(author, text, message_id)
            except Exception as exc:
                self.hub.publish("status", {
                    "state": "error", "source": "innertube",
                    "video_id": self.video_id, "message": str(exc),
                })
            self.done.wait(6)

    def run_official(self) -> None:
        chat = OfficialChat(self.video_id, self.api_key)
        chat.bootstrap()
        self.hub.publish("status", {
            "state": "live", "source": "youtube", "video_id": self.video_id,
            "message": "Listening via YouTube Data API v3",
        })
        page_token = None
        while not self.done.is_set():
            try:
                url = (
                    f"{OfficialChat.BASE}/liveChat/messages?" + urllib.parse.urlencode({
                        "part": "snippet,authorDetails", "liveChatId": chat.chat_id,
                        "key": self.api_key, "maxResults": "200",
                        **({"pageToken": page_token} if page_token else {}),
                    })
                )
                data = json.loads(http_get(url))
                page_token = data.get("nextPageToken", page_token)
                for item in data.get("items", []):
                    snippet = item.get("snippet", {})
                    if snippet.get("type") != "textMessage":
                        continue
                    text = snippet.get("textMessageDetails", {}).get(
                        "messageText", "".join(
                            run.get("text", "") for run in snippet.get("displayMessage", [])
                        )
                    )
                    author = item.get("authorDetails", {}).get("displayName", "")
                    message_id = item.get("id", "")
                    self.record_chat(text, author, message_id)
                    self.read_text(author, text, message_id)
                delay = max(5, int(data.get("pollingIntervalMillis") or 10000)) / 1000
            except Exception as exc:
                self.hub.publish("status", {
                    "state": "error", "source": "youtube",
                    "video_id": self.video_id, "message": str(exc),
                })
                delay = 10
            self.done.wait(delay)


class App:
    def __init__(self):
        self.store = Store(DB_PATH)
        self.hashtags = HashtagStore(CONFIG_PATH)
        self.keywords = KeywordStore(KEYWORD_CONFIG_PATH)
        self.hub = Hub()
        self.chat = ChatFeed(300)
        self.tracker: Tracker | None = None
        self.video_info: dict | None = None
        self.lock = threading.Lock()

    def set_video(self, video_id: str, source: str, api_key: str = "") -> dict:
        info = video_details(video_id, api_key)
        info["status"] = "active"
        self.video_info = info
        self.hub.publish("video", info)
        return info

    def clear_video(self) -> None:
        if not self.video_info:
            return
        info = dict(self.video_info, status="inactive")
        self.video_info = None
        self.hub.publish("video", info)

    def start(self, video_id: str, source: str, api_key: str = "") -> dict:
        with self.lock:
            if self.tracker:
                self.tracker.stop()
            self.chat.clear()
            self.tracker = Tracker(
                self.hub, self.store, self.hashtags, self.keywords,
                self.chat, video_id, source, api_key
            )
            self.tracker.start()
        info = self.set_video(video_id, source, api_key)
        return {"started": True, "video_id": video_id, "source": source, "video": info}

    def stop(self) -> dict:
        with self.lock:
            if self.tracker:
                self.tracker.stop()
                self.tracker = None
            self.hub.publish("status", {
                "state": "idle", "message": "Monitor stopped",
            })
        self.clear_video()
        return {"stopped": True}


APP = App()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def respond(self, code: int, body: bytes, content_type: str,
                extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, payload: dict, code: int = 200) -> None:
        self.respond(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                     "application/json; charset=utf-8")

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not DASHBOARD.exists():
                self.json_response({"error": "dashboard.html missing"}, 500)
                return
            self.respond(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            state = APP.store.stats()
            state.update({
                "history": APP.store.all(300),
                "tracking": bool(APP.tracker and APP.tracker.is_alive()),
                "video_id": APP.tracker.video_id if APP.tracker else None,
                "source": APP.tracker.source if APP.tracker else None,
                "hashtags": APP.hashtags.list(),
                "keywords": APP.keywords.list(),
                "video": APP.video_info,
                "chat": APP.chat.list(),
                "aggregates": APP.store.aggregates(),
            })
            self.json_response(state)
        elif path == "/api/hashtags":
            self.json_response({"hashtags": APP.hashtags.list()})
        elif path == "/api/keywords":
            self.json_response({"keywords": APP.keywords.list()})
        elif path == "/api/events":
            self.stream_events()
        elif path == "/api/export.jsonl":
            self.respond(200, APP.store.jsonl().encode("utf-8"),
                         "application/x-ndjson; charset=utf-8")
        elif path == "/api/ping":
            self.json_response({"ok": True, "time": ph_now()})
        else:
            self.json_response({"error": "not found"}, 404)

    def stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        client = APP.hub.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    frame = client.get(timeout=15)
                except queue.Empty:
                    frame = b": keepalive\n\n"
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            APP.hub.unsubscribe(client)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        payload = self.body_json()
        if path == "/api/start":
            video_id = video_id_from(payload.get("url", ""))
            if not video_id:
                self.json_response({"error": "Provide a YouTube URL or 11-character video ID."}, 400)
                return
            source = payload.get("source") or "auto"
            key = str(payload.get("api_key") or "").strip()
            if source == "auto":
                source = "youtube" if key else "innertube"
            if source not in ("youtube", "innertube"):
                self.json_response({"error": "Unknown source."}, 400)
                return
            if source == "youtube" and not key:
                self.json_response({"error": "An API key is required for the official source."}, 400)
                return
            self.json_response(APP.start(video_id, source, key))
        elif path == "/api/hashtags":
            values = payload.get("hashtags", [])
            if not isinstance(values, list):
                values = []
            tags = APP.hashtags.set(values)
            APP.hub.publish("hashtags", {"hashtags": tags})
            self.json_response({"hashtags": tags})
        elif path == "/api/keywords":
            values = payload.get("keywords", [])
            if not isinstance(values, list):
                values = []
            keywords = APP.keywords.set(values)
            APP.hub.publish("keywords", {"keywords": keywords})
            self.json_response({"keywords": keywords})
        elif path == "/api/stop":
            self.json_response(APP.stop())
        else:
            self.json_response({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/mentions":
            count = APP.store.clear()
            APP.hub.publish("cleared", {"count": count})
            APP.hub.publish("stats", {**APP.store.stats(), "aggregates": APP.store.aggregates()})
            self.json_response({"deleted": count})
            return
        if path.startswith("/api/mentions/"):
            record_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            if not APP.store.delete(record_id):
                self.json_response({"error": "record not found"}, 404)
                return
            APP.hub.publish("deleted", {"id": record_id})
            APP.hub.publish("stats", {**APP.store.stats(), "aggregates": APP.store.aggregates()})
            self.json_response({"deleted": 1})
            return
        self.json_response({"error": "not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Youtube Live Comment Analyzer → http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if APP.tracker:
            APP.tracker.stop()
        server.server_close()
        APP.store.close()


if __name__ == "__main__":
    main()
