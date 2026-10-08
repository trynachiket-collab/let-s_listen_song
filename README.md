# SyncPlay: listen to YouTube together, in sync

## Run it
```bash
pip install -r requirements.txt
python app.py            # starts on http://localhost:5000
```

## Share it with your friend (ngrok)
In a second terminal:
```bash
ngrok http 5000
```
Send the `https://xxxx.ngrok-free.app` link to your friend. Both of you open it,
enter the same room code, and paste YouTube links. Either of you can play,
pause, skip, seek, or pick songs from the shared playlist.

## Notes
- Click **Create new room** for a random code; your friend uses **Join** with that code.
- Type a song name to search (uses yt-dlp, no API key) or paste a link.
- If search stops working, run `pip install -U yt-dlp`.
- Each person's device plays the audio from YouTube itself; only controls are synced.
- Playlist is kept in server memory, so it resets when you restart `app.py`.
- If one of you gets a YouTube ad, you may drift a few seconds. Pressing play or
  clicking the song again re-syncs everyone.
- Some videos disable embedding and won't play; try another upload of the song.