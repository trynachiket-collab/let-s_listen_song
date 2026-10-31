import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import socketio
from fastapi import FastAPI
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"

CHAT_TTL = 300          # seconds a chat message lives (5 min)
CHAT_MAX = 50           # max messages kept per room, oldest dropped first
CHAT_MAX_LEN = 300      # max characters per message
ROOM_IDLE_TTL = 600     # delete a room 10 min after the last person leaves
CLEANUP_EVERY = 30      # how often the janitor runs (seconds)

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")

rooms = {}       # room code -> state
sid_room = {}    # socket id -> room code (to know who left which room)
last_msg = {}    # socket id -> time of last chat message (simple spam guard)


def new_room():
    return {
        "playlist": [], "index": -1, "playing": False, "time": 0.0, "ts": time.time(),
        "chat": deque(maxlen=CHAT_MAX),
        "members": set(),
        "empty_since": time.time(),
    }


def snapshot(r):
    """Current state, with time advanced if the room is playing."""
    t = r["time"] + (time.time() - r["ts"] if r["playing"] else 0)
    return {"playlist": r["playlist"], "index": r["index"],
            "playing": r["playing"], "time": t}


def set_pos(r, playing, t=0.0):
    r["playing"], r["time"], r["ts"] = playing, float(t or 0), time.time()


def prune_chat(r, now):
    while r["chat"] and now - r["chat"][0]["ts"] > CHAT_TTL:
        r["chat"].popleft()


def for_client(m, now):
    """Send seconds left instead of a timestamp, so client clock skew doesn't matter."""
    return {"id": m["id"], "name": m["name"], "text": m["text"],
            "left": max(0, CHAT_TTL - (now - m["ts"]))}


async def janitor():
    """Removes expired chat messages and abandoned rooms."""
    while True:
        await asyncio.sleep(CLEANUP_EVERY)
        now = time.time()
        for code in list(rooms):
            r = rooms[code]
            prune_chat(r, now)
            if not r["members"] and now - r["empty_since"] > ROOM_IDLE_TTL:
                del rooms[code]


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(janitor())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


api = FastAPI(lifespan=lifespan)


# ---------------- HTTP routes ----------------

@api.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@api.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


@api.get("/sw.js")
async def service_worker():
    # Service workers must be served from the app's root scope.
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@api.get("/icon.svg")
async def app_icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@api.get("/icon-192.png")
async def app_icon_192():
    return FileResponse(STATIC / "icon-192.png", media_type="image/png")


@api.get("/icon-512.png")
async def app_icon_512():
    return FileResponse(STATIC / "icon-512.png", media_type="image/png")


@api.get("/create")
async def create():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # avoids look-alike characters
    while True:
        code = "".join(random.choices(alphabet, k=5))
        if code not in rooms:
            break
    rooms[code] = new_room()
    return {"room": code}


def _yt_search(q):
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}
    with YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch8:{q}", download=False)
    return [{"id": e["id"], "title": e.get("title"),
             "channel": e.get("channel") or e.get("uploader") or ""}
            for e in info.get("entries", []) if e and e.get("id")]


@api.get("/search")
async def search(q: str = ""):
    q = q.strip()[:200]
    if not q:
        return {"results": []}
    try:
        results = await asyncio.to_thread(_yt_search, q)
    except Exception:
        results = []
    return {"results": results}


# ---------------- Socket.IO events ----------------

@sio.event
async def join(sid, data):
    code = (data or {}).get("room")
    if not isinstance(code, str) or code not in rooms:
        await sio.emit("err", "Room not found. Check the code or create a new room.", to=sid)
        return
    r = rooms[code]
    await sio.enter_room(sid, code)
    sid_room[sid] = code
    r["members"].add(sid)
    now = time.time()
    prune_chat(r, now)
    await sio.emit("state", snapshot(r), to=sid)
    await sio.emit("chat_history", [for_client(m, now) for m in r["chat"]], to=sid)


@sio.event
async def disconnect(sid, *args):
    code = sid_room.pop(sid, None)
    last_msg.pop(sid, None)
    r = rooms.get(code)
    if r:
        r["members"].discard(sid)
        if not r["members"]:
            r["empty_since"] = time.time()


@sio.event
async def chat(sid, data):
    code = sid_room.get(sid)
    r = rooms.get(code)
    if r is None:
        return
    data = data if isinstance(data, dict) else {}
    message = str(data.get("text", "")).strip()[:CHAT_MAX_LEN]
    name = str(data.get("name", "")).strip()[:20] or "Guest"
    now = time.time()
    if not message or now - last_msg.get(sid, 0) < 0.5:
        return
    last_msg[sid] = now
    item = {"id": f"{now:.6f}-{random.randint(0, 9999)}", "name": name,
            "text": message, "ts": now}
    r["chat"].append(item)
    await sio.emit("chat", for_client(item, now), room=code)


@sio.event
async def action(sid, data):
    data = data if isinstance(data, dict) else {}
    # A client may only control the room it has joined.
    code = sid_room.get(sid)
    if not code or data.get("room") != code:
        return
    kind = data.get("type")
    r = rooms.get(code)
    if r is None:
        return
    n = len(r["playlist"])

    if kind == "add":
        video_id = str(data.get("id", ""))
        if not video_id or len(video_id) > 64:
            return
        title = str(data.get("title") or video_id)[:300]
        r["playlist"].append({"id": video_id, "title": title})
        if r["index"] == -1:
            r["index"] = 0
            set_pos(r, True, 0)
    elif kind == "remove":
        i = data.get("i", -1)
        if isinstance(i, int) and 0 <= i < n:
            r["playlist"].pop(i)
            if i < r["index"]:
                r["index"] -= 1
            elif i == r["index"]:
                r["index"] = min(i, len(r["playlist"]) - 1)
                set_pos(r, r["index"] >= 0, 0)
    elif kind == "play_index":
        i = data.get("i", -1)
        if isinstance(i, int) and 0 <= i < n:
            r["index"] = i
            set_pos(r, True, 0)
    elif kind == "next":
        if r["index"] + 1 < n:
            r["index"] += 1
            set_pos(r, True, 0)
    elif kind == "prev":
        if r["index"] > 0:
            r["index"] -= 1
            set_pos(r, True, 0)
    elif kind == "ended":
        if data.get("i") == r["index"]:
            if r["index"] + 1 < n:
                r["index"] += 1
                set_pos(r, True, 0)
            else:
                set_pos(r, False, 0)
    elif kind in ("play", "pause", "seek"):
        try:
            position = max(0.0, min(float(data.get("time", 0) or 0), 86400.0))
        except (TypeError, ValueError):
            position = 0.0
        if kind == "play":
            set_pos(r, True, position)
        elif kind == "pause":
            set_pos(r, False, position)
        else:
            set_pos(r, r["playing"], position)

    await sio.emit("state", snapshot(r), room=code)


# Socket.IO handles /socket.io/*, everything else goes to FastAPI.
app = socketio.ASGIApp(sio, other_asgi_app=api)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(__import__("os").environ.get("PORT", "5000")))
