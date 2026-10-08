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
        "chat": deque(maxlen=CHAT_MAX),   # deque drops the oldest automatically
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
    """Send 'seconds left' instead of a timestamp, so client clock skew doesn't matter."""
    return {"id": m["id"], "name": m["name"], "text": m["text"],
            "left": max(0, CHAT_TTL - (now - m["ts"]))}


async def janitor():
    """Removes expired chat messages and abandoned rooms so memory never piles up."""
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
    yield
    task.cancel()


api = FastAPI(lifespan=lifespan)


# ---------------- HTTP routes ----------------

@api.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@api.get("/create")
async def create():
    while True:  # random 5-char code, no look-alike characters
        code = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=5))
        if code not in rooms:
            break
    rooms[code] = new_room()
    return {"room": code}


def _yt_search(q):
    from yt_dlp import YoutubeDL  # no API key needed
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch8:{q}", download=False)
    return [{"id": e["id"], "title": e.get("title"),
             "channel": e.get("channel") or e.get("uploader") or ""}
            for e in info.get("entries", []) if e]


@api.get("/search")
async def search(q: str = ""):
    q = q.strip()
    if not q:
        return {"results": []}
    try:
        # yt-dlp is blocking, so run it in a thread to keep the server responsive
        results = await asyncio.to_thread(_yt_search, q)
    except Exception:
        results = []
    return {"results": results}


# ---------------- Socket.IO events ----------------

@sio.event
async def join(sid, data):
    code = (data or {}).get("room")
    if code not in rooms:
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
async def chat(sid, d):
    code = sid_room.get(sid)
    r = rooms.get(code)
    if r is None:
        return
    text = str((d or {}).get("text", "")).strip()[:CHAT_MAX_LEN]
    name = str((d or {}).get("name", "")).strip()[:20] or "Guest"
    now = time.time()
    if not text or now - last_msg.get(sid, 0) < 0.5:  # empty or too fast
        return
    last_msg[sid] = now
    m = {"id": f"{now:.6f}-{random.randint(0, 9999)}", "name": name, "text": text, "ts": now}
    r["chat"].append(m)
    await sio.emit("chat", for_client(m, now), room=code)


@sio.event
async def action(sid, d):
    code, kind = d.get("room"), d.get("type")
    r = rooms.get(code)
    if r is None:
        return
    n = len(r["playlist"])

    if kind == "add":
        r["playlist"].append({"id": d["id"], "title": d.get("title") or d["id"]})
        if r["index"] == -1:  # first song starts automatically
            r["index"] = 0
            set_pos(r, True, 0)
    elif kind == "remove":
        i = d.get("i", -1)
        if 0 <= i < n:
            r["playlist"].pop(i)
            if i < r["index"]:
                r["index"] -= 1
            elif i == r["index"]:
                r["index"] = min(i, len(r["playlist"]) - 1)
                set_pos(r, r["index"] >= 0, 0)
    elif kind == "play_index":
        if 0 <= d.get("i", -1) < n:
            r["index"] = d["i"]
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
        # only the first "ended" for the current song counts
        if d.get("i") == r["index"]:
            if r["index"] + 1 < n:
                r["index"] += 1
                set_pos(r, True, 0)
            else:
                set_pos(r, False, 0)
    elif kind == "play":
        set_pos(r, True, d.get("time", 0))
    elif kind == "pause":
        set_pos(r, False, d.get("time", 0))
    elif kind == "seek":
        set_pos(r, r["playing"], d.get("time", 0))

    await sio.emit("state", snapshot(r), room=code)


# Socket.IO handles /socket.io/*, everything else goes to FastAPI
app = socketio.ASGIApp(sio, other_asgi_app=api)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)