#!/usr/bin/env python3
"""
FairDrop - AirDrop-style file sharing for everyone on your local network.

Run it:      python fairdrop.py
Then open:   http://<your-lan-ip>:5000  on any device on the same Wi-Fi.

Zero dependencies: standard library only.
"""

from __future__ import annotations

import argparse
import atexit
import json
import mimetypes
import os
import queue
import random
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
SPOOL = tempfile.mkdtemp(prefix="fairdrop-")
atexit.register(lambda: shutil.rmtree(SPOOL, ignore_errors=True))

CHUNK = 256 * 1024
PING_SECONDS = 15
PEER_TIMEOUT = 40
TRANSFER_TTL = 60 * 60  # keep finished transfers around for an hour


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

ADJECTIVES = [
    "Amber", "Arctic", "Azure", "Bold", "Brave", "Bright", "Calm", "Citrus",
    "Clever", "Cobalt", "Coral", "Cosmic", "Crimson", "Curious", "Daring",
    "Dusty", "Eager", "Electric", "Ember", "Fearless", "Gentle", "Golden",
    "Happy", "Hidden", "Indigo", "Ivory", "Jade", "Jolly", "Kind", "Lively",
    "Lucky", "Lunar", "Marble", "Mellow", "Midnight", "Mint", "Neon", "Nimble",
    "Noble", "Olive", "Opal", "Pastel", "Plum", "Polar", "Prism", "Quiet",
    "Rapid", "Royal", "Ruby", "Sage", "Sandy", "Scarlet", "Silent", "Silver",
    "Solar", "Sonic", "Spry", "Sunny", "Swift", "Teal", "Tidy", "Topaz",
    "Velvet", "Violet", "Vivid", "Warm", "Wild", "Zesty",
]

CREATURES = [
    "Albatross", "Antelope", "Badger", "Beacon", "Beluga", "Bison", "Bobcat",
    "Buffalo", "Canary", "Caribou", "Cheetah", "Comet", "Condor", "Cougar",
    "Coyote", "Cricket", "Dolphin", "Dragon", "Eagle", "Falcon", "Ferret",
    "Finch", "Fox", "Gazelle", "Gecko", "Glacier", "Harbor", "Hawk", "Heron",
    "Ibis", "Jaguar", "Kestrel", "Koala", "Lantern", "Lemur", "Leopard",
    "Lynx", "Magpie", "Manta", "Marlin", "Meadow", "Meerkat", "Narwhal",
    "Ocelot", "Orca", "Osprey", "Otter", "Panther", "Pelican", "Penguin",
    "Puffin", "Quail", "Rabbit", "Raven", "Robin", "Salmon", "Seal", "Sparrow",
    "Stingray", "Swallow", "Tiger", "Toucan", "Turtle", "Walrus", "Wombat",
    "Yak", "Zebra",
]


def make_name(taken: set[str]) -> str:
    for _ in range(400):
        name = f"{random.choice(ADJECTIVES)} {random.choice(CREATURES)}"
        if name not in taken:
            return name
    return f"{random.choice(ADJECTIVES)} {random.choice(CREATURES)} {random.randint(2, 99)}"


def device_hint(user_agent: str) -> tuple[str, str]:
    """Return (label, kind) for a user agent. Kind drives the icon shown."""
    ua = user_agent or ""
    system, kind = "Device", "desktop"
    for needle, label, form in (
        ("iPhone", "iPhone", "phone"),
        ("iPod", "iPod", "phone"),
        ("iPad", "iPad", "tablet"),
        ("Android", "Android", "phone"),
        ("Macintosh", "Mac", "desktop"),
        ("Mac OS X", "Mac", "desktop"),
        ("Windows", "Windows", "desktop"),
        ("CrOS", "Chromebook", "desktop"),
        ("Linux", "Linux", "desktop"),
    ):
        if needle in ua:
            system, kind = label, form
            break
    if kind == "phone" and "Android" in ua and "Mobile" not in ua:
        kind = "tablet"

    browser = ""
    for needle, label in (
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"),
        ("Firefox/", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if needle in ua:
            browser = label
            break

    return (f"{system} · {browser}" if browser else system), kind


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class Peer:
    def __init__(self, peer_id: str, name: str, device: str, kind: str = "desktop"):
        self.id = peer_id
        self.name = name
        self.device = device
        self.kind = kind
        self.seen = time.time()
        self.conn = 0  # bumped per event-stream connection
        # One queue per live event stream. A reloading page briefly has two,
        # and a single shared queue would let the dying one eat the events.
        self.streams: dict[int, queue.Queue] = {}

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "device": self.device, "kind": self.kind}

    def send(self, event: str, data: dict) -> None:
        for q in list(self.streams.values()):
            q.put((event, data))


class TransferFile:
    def __init__(self, index: int, name: str, size: int, mime: str):
        self.index = index
        self.name = name
        self.size = size
        self.mime = mime or "application/octet-stream"
        self.path = os.path.join(SPOOL, f"{secrets.token_hex(12)}.part")
        self.ready = threading.Event()
        self.received = 0

    def public(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
            "ready": self.ready.is_set(),
        }


class Transfer:
    def __init__(self, tid: str, sender: Peer, recipient: Peer, files: list[TransferFile]):
        self.id = tid
        self.sender_id = sender.id
        self.sender_name = sender.name
        self.recipient_id = recipient.id
        self.recipient_name = recipient.name
        self.files = files
        self.status = "pending"  # pending | accepted | declined | cancelled | done
        self.created = time.time()
        self.zip_path: str | None = None
        self.lock = threading.Lock()

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def public(self) -> dict:
        return {
            "id": self.id,
            "from": {"id": self.sender_id, "name": self.sender_name},
            "to": {"id": self.recipient_id, "name": self.recipient_name},
            "files": [f.public() for f in self.files],
            "totalSize": self.total_size,
            "status": self.status,
        }

    def cleanup(self) -> None:
        for f in self.files:
            try:
                os.remove(f.path)
            except OSError:
                pass
        if self.zip_path:
            try:
                os.remove(self.zip_path)
            except OSError:
                pass
            self.zip_path = None


class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.peers: dict[str, Peer] = {}
        self.transfers: dict[str, Transfer] = {}

    # -- peers ----------------------------------------------------------
    def join(self, peer_id: str | None, name: str | None, device: str,
             kind: str) -> tuple[Peer, int, queue.Queue]:
        """Register a connection: returns the peer, its token and its queue."""
        with self.lock:
            self.reap()
            if peer_id and peer_id in self.peers:
                peer = self.peers[peer_id]
                peer.device = device
                peer.kind = kind
                peer.seen = time.time()
            else:
                pid = peer_id or secrets.token_urlsafe(9)
                taken = {p.name for p in self.peers.values()}
                peer = Peer(pid, (name or "").strip()[:32] or make_name(taken), device, kind)
                self.peers[pid] = peer
            peer.conn += 1
            stream: queue.Queue = queue.Queue()
            peer.streams[peer.conn] = stream
            return peer, peer.conn, stream

    def leave(self, peer_id: str, token: int) -> bool:
        """Drop a peer, but only if this is still its newest connection.

        A reloading page opens the new stream before the old one errors out, so
        an unconditional removal here would evict the peer that just returned.
        """
        with self.lock:
            peer = self.peers.get(peer_id)
            if not peer:
                return False
            peer.streams.pop(token, None)
            if peer.conn != token:
                return False  # a newer stream took over; keep the peer online
            del self.peers[peer_id]
        self.broadcast_peers()
        return True

    def reap(self) -> None:
        now = time.time()
        with self.lock:
            stale = [p.id for p in self.peers.values() if now - p.seen > PEER_TIMEOUT]
            for pid in stale:
                self.peers.pop(pid, None)
            dead = [
                t.id for t in self.transfers.values()
                if now - t.created > TRANSFER_TTL
            ]
            for tid in dead:
                t = self.transfers.pop(tid, None)
                if t:
                    t.cleanup()

    def roster(self) -> list[dict]:
        with self.lock:
            return sorted(
                (p.public() for p in self.peers.values()),
                key=lambda p: p["name"].lower(),
            )

    def broadcast_peers(self) -> None:
        roster = self.roster()
        with self.lock:
            targets = list(self.peers.values())
        for peer in targets:
            peer.send("peers", {"peers": roster, "you": peer.id})

    def get(self, peer_id: str) -> Peer | None:
        with self.lock:
            return self.peers.get(peer_id)

    # -- transfers ------------------------------------------------------
    def create_transfer(self, sender: Peer, recipient: Peer, files: list[dict]) -> Transfer:
        entries = [
            TransferFile(
                i,
                os.path.basename(str(f.get("name") or f"file-{i + 1}"))[:180],
                max(0, int(f.get("size") or 0)),
                str(f.get("mime") or ""),
            )
            for i, f in enumerate(files)
        ]
        transfer = Transfer(secrets.token_urlsafe(12), sender, recipient, entries)
        with self.lock:
            self.transfers[transfer.id] = transfer
        return transfer

    def transfer(self, tid: str) -> Transfer | None:
        with self.lock:
            return self.transfers.get(tid)


HUB = Hub()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class FairDropHandler(BaseHTTPRequestHandler):
    server_version = "FairDrop/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers --------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter console
        if self.path.startswith("/api/events"):
            return
        sys.stderr.write("  %s\n" % (fmt % args))

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # -- routing --------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/events":
            return self.events()
        if path.startswith("/api/transfers/"):
            return self.download(path)
        return self.static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/transfers":
            return self.create_transfer()
        m = re.fullmatch(r"/api/transfers/([\w\-]+)/respond", path)
        if m:
            return self.respond(m.group(1))
        m = re.fullmatch(r"/api/transfers/([\w\-]+)/cancel", path)
        if m:
            return self.cancel(m.group(1))
        if path == "/api/rename":
            return self.rename()
        return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def do_PUT(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/transfers/([\w\-]+)/files/(\d+)", path)
        if m:
            return self.upload(m.group(1), int(m.group(2)))
        return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    # -- static ---------------------------------------------------------
    def static(self, path: str):
        rel = "index.html" if path == "/" else unquote(path.lstrip("/"))
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
        size = os.path.getsize(target)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") else ""))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(target, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, CHUNK)

    # -- SSE ------------------------------------------------------------
    def events(self):
        q = self.query()
        device, kind = device_hint(self.headers.get("User-Agent", ""))
        peer, token, stream = HUB.join(q.get("id"), q.get("name"), device, kind)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.write_event("hello", {"you": peer.public()})
        except OSError:
            return

        HUB.broadcast_peers()
        print(f"  + {peer.name} joined ({peer.device})")

        try:
            while True:
                try:
                    event, data = stream.get(timeout=PING_SECONDS)
                except queue.Empty:
                    event, data = "ping", {"t": int(time.time())}
                peer.seen = time.time()
                self.write_event(event, data)
        except (OSError, ValueError):
            pass
        finally:
            if HUB.leave(peer.id, token):
                print(f"  - {peer.name} left")

    def write_event(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()

    # -- API ------------------------------------------------------------
    def rename(self):
        body = self.read_json()
        peer = HUB.get(str(body.get("id") or ""))
        if not peer:
            return self.fail(HTTPStatus.NOT_FOUND, "You are no longer connected")
        name = str(body.get("name") or "").strip()[:32]
        if not name:
            return self.fail(HTTPStatus.BAD_REQUEST, "Name cannot be empty")
        peer.name = name
        HUB.broadcast_peers()
        return self.send_json({"ok": True, "name": name})

    def create_transfer(self):
        body = self.read_json()
        sender = HUB.get(str(body.get("from") or ""))
        recipient = HUB.get(str(body.get("to") or ""))
        files = body.get("files")
        if not sender:
            return self.fail(HTTPStatus.FORBIDDEN, "You are no longer connected")
        if not recipient:
            return self.fail(HTTPStatus.NOT_FOUND, "That person is no longer nearby")
        if recipient.id == sender.id:
            return self.fail(HTTPStatus.BAD_REQUEST, "You cannot send to yourself")
        if not isinstance(files, list) or not files:
            return self.fail(HTTPStatus.BAD_REQUEST, "No files selected")
        if len(files) > 100:
            return self.fail(HTTPStatus.BAD_REQUEST, "Up to 100 files at a time")

        transfer = HUB.create_transfer(sender, recipient, files)
        recipient.send("offer", transfer.public())
        return self.send_json(transfer.public(), HTTPStatus.CREATED)

    def upload(self, tid: str, index: int):
        transfer = HUB.transfer(tid)
        if not transfer:
            return self.fail(HTTPStatus.NOT_FOUND, "Transfer expired")
        if index >= len(transfer.files):
            return self.fail(HTTPStatus.NOT_FOUND, "No such file")
        if transfer.status in ("declined", "cancelled"):
            return self.fail(HTTPStatus.GONE, "Transfer was cancelled")

        entry = transfer.files[index]
        remaining = int(self.headers.get("Content-Length") or 0)
        written = 0
        try:
            with open(entry.path, "wb") as out:
                while remaining > 0:
                    block = self.rfile.read(min(CHUNK, remaining))
                    if not block:
                        break
                    out.write(block)
                    remaining -= len(block)
                    written += len(block)
        except OSError as exc:
            return self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"Upload failed: {exc}")

        entry.received = written
        entry.size = written
        entry.ready.set()

        recipient = HUB.get(transfer.recipient_id)
        if recipient:
            recipient.send("file-ready", {"id": transfer.id, "index": index, "size": written})
        return self.send_json({"ok": True, "index": index, "size": written})

    def respond(self, tid: str):
        body = self.read_json()
        transfer = HUB.transfer(tid)
        if not transfer:
            return self.fail(HTTPStatus.NOT_FOUND, "Transfer expired")
        if str(body.get("id") or "") != transfer.recipient_id:
            return self.fail(HTTPStatus.FORBIDDEN, "Not your transfer")

        action = "accepted" if body.get("action") == "accept" else "declined"
        with transfer.lock:
            if transfer.status == "pending":
                transfer.status = action
        if action == "declined":
            transfer.cleanup()

        sender = HUB.get(transfer.sender_id)
        if sender:
            sender.send("response", {"id": transfer.id, "status": transfer.status,
                                     "by": transfer.recipient_name})
        return self.send_json({"ok": True, "status": transfer.status})

    def cancel(self, tid: str):
        body = self.read_json()
        transfer = HUB.transfer(tid)
        if not transfer:
            return self.fail(HTTPStatus.NOT_FOUND, "Transfer expired")
        if str(body.get("id") or "") != transfer.sender_id:
            return self.fail(HTTPStatus.FORBIDDEN, "Not your transfer")
        with transfer.lock:
            if transfer.status == "pending":
                transfer.status = "cancelled"
        transfer.cleanup()
        recipient = HUB.get(transfer.recipient_id)
        if recipient:
            recipient.send("cancelled", {"id": transfer.id})
        return self.send_json({"ok": True})

    # -- downloads ------------------------------------------------------
    def download(self, path: str):
        q = self.query()
        m = re.fullmatch(r"/api/transfers/([\w\-]+)/files/(\d+)/download", path)
        zip_m = re.fullmatch(r"/api/transfers/([\w\-]+)/zip", path)
        if not m and not zip_m:
            return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        tid = (m or zip_m).group(1)
        transfer = HUB.transfer(tid)
        if not transfer:
            return self.fail(HTTPStatus.NOT_FOUND, "Transfer expired")
        if q.get("id") != transfer.recipient_id:
            return self.fail(HTTPStatus.FORBIDDEN, "Not your transfer")
        if transfer.status != "accepted":
            return self.fail(HTTPStatus.CONFLICT, "Transfer has not been accepted")

        if zip_m:
            return self.send_zip(transfer)

        index = int(m.group(2))
        if index >= len(transfer.files):
            return self.fail(HTTPStatus.NOT_FOUND, "No such file")
        entry = transfer.files[index]
        if not entry.ready.wait(timeout=300):
            return self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Still uploading, try again")
        return self.send_file(entry.path, entry.name, entry.mime)

    def send_zip(self, transfer: Transfer):
        for entry in transfer.files:
            if not entry.ready.wait(timeout=300):
                return self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "Still uploading, try again")
        with transfer.lock:
            if not transfer.zip_path or not os.path.exists(transfer.zip_path):
                zip_path = os.path.join(SPOOL, f"{transfer.id}.zip")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                    used: dict[str, int] = {}
                    for entry in transfer.files:
                        name = entry.name
                        if name in used:
                            used[name] += 1
                            stem, ext = os.path.splitext(name)
                            name = f"{stem} ({used[name]}){ext}"
                        else:
                            used[name] = 0
                        zf.write(entry.path, name)
                transfer.zip_path = zip_path
        stamp = time.strftime("%Y-%m-%d", time.localtime(transfer.created))
        return self.send_file(transfer.zip_path, f"FairDrop {stamp}.zip", "application/zip")

    def send_file(self, path: str, filename: str, mime: str):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self.fail(HTTPStatus.NOT_FOUND, "File is no longer available")
        ascii_name = re.sub(r'[^\x20-\x7e]', "_", filename).replace('"', "'")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote_name(filename)}',
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, CHUNK)


def quote_name(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="FairDrop - local network file sharing")
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    parser.add_argument("--port", type=int, default=5000, help="port (default: 5000)")
    args = parser.parse_args()

    if not os.path.isdir(WEB_DIR):
        print(f"Missing web assets at {WEB_DIR}", file=sys.stderr)
        return 1

    # On Windows SO_REUSEADDR lets a second instance silently bind the same
    # port and receive nothing, so refuse instead of starting a ghost server.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        server = ThreadingHTTPServer((args.host, args.port), FairDropHandler)
    except OSError as exc:
        print(f"  Cannot listen on port {args.port}: {exc}", file=sys.stderr)
        print("  Is FairDrop already running? Try --port 5001.", file=sys.stderr)
        return 1
    server.daemon_threads = True
    ip = lan_ip()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    print()
    print("  FairDrop - drop files to anyone on this network")
    print("  " + "-" * 46)
    print(f"  On this computer   http://localhost:{args.port}")
    print(f"  On the network     http://{ip}:{args.port}")
    print("  " + "-" * 46)
    print("  Press Ctrl+C to stop.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  FairDrop stopped. Spooled files removed.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
