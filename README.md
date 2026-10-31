# SyncPlay

A small real-time shared listening-room web app built with FastAPI, Socket.IO, and a browser-based YouTube player. This package includes an installable PWA shell and Media Session integration.

## Project structure

```text
syncplay/
├── main.py
├── requirements.txt
├── Procfile
├── README.md
├── .gitignore
└── static/
    ├── index.html
    ├── manifest.json
    ├── sw.js
    └── icon.svg
```

## Run locally (Windows PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

Then open `http://localhost:5000` in your browser. Open a second browser window or device to test joining the same room. For access from a phone on the same Wi-Fi, use your computer's LAN IP and port 5000; service workers/PWA installation generally require HTTPS except on localhost, so use an HTTPS deployment to install it on a phone.

## Deploy

The included `Procfile` is compatible with common Python web-app hosts that support Procfiles. Set the start command to:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Use an HTTPS domain for PWA installation and service-worker support. Free hosting tiers may sleep when idle and may not be suitable for uninterrupted shared rooms.

## Background playback: important limitation

This project still uses the YouTube IFrame Player API for playback. The PWA manifest and service worker do **not** keep audio alive in the background. The page adds Media Session metadata and media action handlers where supported, but actual background playback depends on the browser, operating system, YouTube embed behavior, and device power settings. It is not guaranteed when switching apps or locking the phone.

For reliable background audio, the next architectural step is to use an audio source that explicitly permits background playback and play it through a persistent HTML `<audio>` element. YouTube search results and embed IDs are not direct audio-stream URLs, and this project does not extract or proxy YouTube audio.

## Prototype notes

- Rooms and chat are held in server memory. Restarting the server clears them.
- Run a single server process/instance unless you add shared storage and a Socket.IO message queue.
- The app is a prototype; add authentication, rate limiting, and production security controls before exposing it to a large public audience.
