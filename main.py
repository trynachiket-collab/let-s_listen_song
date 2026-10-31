import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import socketio
from fastapi import FastAPI
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"

CHAT_TTL = 300
CHAT_MAX = 50
CHAT_MAX_LEN = 300
ROOM_IDLE_TTL = 600
CLEANUP_EVERY = 30

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
rooms = {}
sid_room = {}
last_msg = {}


def new_room():
    return {
        "playlist": [], "index": -1, "playing": False, "time": 0.0, "ts": time.time(),
        "chat": deque(maxlen=CHAT_MAX), "members": set(), "empty_since": time.time(),
    }


def snapshot(r):
    position = r["time"] + (time.time() - r["ts"] if r["playing"] else 0)
    return {"playlist": r["playlist"], "index": r["index"],
            "playing": r["playing"], "time": position}


def set_pos(r, playing, position=0.0):
    r["playing"], r["time"], r["ts"] = playing, float(position or 0), time.time()


def prune_chat(r, now):
    while r["chat"] and now - r["chat"][0]["ts"] > CHAT_TTL:
        r["chat"].popleft()


def for_client(message, now):
    return {"id": message["id"], "name": message["name"], "text": message["text"],
            "left": max(0, CHAT_TTL - (now - message["ts"]))}


async def janitor():
    while True:
        await asyncio.sleep(CLEANUP_EVERY)
        now = time.time()
        for code in list(rooms):
            current = rooms[code]
            prune_chat(current, now)
            if not current["members"] and now - current["empty_since"] > ROOM_IDLE_TTL:
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


@api.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@api.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


@api.get("/sw.js")
async def service_worker():
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
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        code = "".join(random.choices(alphabet, k=5))
        if code not in rooms:
            break
    rooms[code] = new_room()
    return {"room": code}


@sio.event
async def join(sid, data):
    code = (data or {}).get("room")
    if not isinstance(code, str) or code not in rooms:
        await sio.emit("err", "Room not found. Check the code or create a new room.", to=sid)
        return
    current = rooms[code]
    await sio.enter_room(sid, code)
    sid_room[sid] = code
    current["members"].add(sid)
    now = time.time()
    prune_chat(current, now)
    await sio.emit("state", snapshot(current), to=sid)
    await sio.emit("chat_history", [for_client(m, now) for m in current["chat"]], to=sid)


@sio.event
async def disconnect(sid, *args):
    code = sid_room.pop(sid, None)
    last_msg.pop(sid, None)
    current = rooms.get(code)
    if current:
        current["members"].discard(sid)
        if not current["members"]:
            current["empty_since"] = time.time()


@sio.event
async def chat(sid, data):
    code = sid_room.get(sid)
    current = rooms.get(code)
    if current is None:
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
    current["chat"].append(item)
    await sio.emit("chat", for_client(item, now), room=code)


@sio.event
async def action(sid, data):
    data = data if isinstance(data, dict) else {}
    code = sid_room.get(sid)
    if not code or data.get("room") != code:
        return
    kind = data.get("type")
    current = rooms.get(code)
    if current is None:
        return
    count = len(current["playlist"])

    if kind == "add":
        source_url = str(data.get("url", "")).strip()
        try:
            parsed = urlsplit(source_url)
            valid_url = (parsed.scheme == "https" and bool(parsed.hostname)
                         and not parsed.username and not parsed.password
                         and len(source_url) <= 2048)
        except ValueError:
            valid_url = False
        if not valid_url:
            await sio.emit("err", "Please add a valid HTTPS direct audio URL.", to=sid)
            return
        title = str(data.get("title") or "Untitled audio")[:200]
        artist = str(data.get("artist") or "")[:120]
        track_id = f"{time.time_ns()}-{random.randint(1000, 9999)}"
        current["playlist"].append({"id": track_id, "type": "audio", "url": source_url,
                                    "title": title, "artist": artist})
        if current["index"] == -1:
            current["index"] = 0
            set_pos(current, True, 0)

    elif kind == "remove":
        index = data.get("i", -1)
        if isinstance(index, int) and 0 <= index < count:
            current["playlist"].pop(index)
            if index < current["index"]:
                current["index"] -= 1
            elif index == current["index"]:
                current["index"] = min(index, len(current["playlist"]) - 1)
                set_pos(current, current["index"] >= 0, 0)

    elif kind == "play_index":
        index = data.get("i", -1)
        if isinstance(index, int) and 0 <= index < count:
            current["index"] = index
            set_pos(current, True, 0)

    elif kind == "next":
        if current["index"] + 1 < count:
            current["index"] += 1
            set_pos(current, True, 0)

    elif kind == "prev":
        if current["index"] > 0:
            current["index"] -= 1
            set_pos(current, True, 0)

    elif kind == "ended":
        if data.get("i") == current["index"]:
            if current["index"] + 1 < count:
                current["index"] += 1
                set_pos(current, True, 0)
            else:
                set_pos(current, False, 0)

    elif kind in ("play", "pause", "seek"):
        try:
            position = max(0.0, min(float(data.get("time", 0) or 0), 86400.0))
        except (TypeError, ValueError):
            position = 0.0
        if kind == "play":
            set_pos(current, True, position)
        elif kind == "pause":
            set_pos(current, False, position)
        else:
            set_pos(current, current["playing"], position)

    await sio.emit("state", snapshot(current), room=code)


app = socketio.ASGIApp(sio, other_asgi_app=api)

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
