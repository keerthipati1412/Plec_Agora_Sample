# Simple Agora Web Video Call

This is a very small web sample that uses the Agora Web SDK to do 1-to-many video calling.

## Files

- `index.html`: UI and Agora Web SDK script include.
- `styles.css`: Minimal page styling.
- `app.js`: Join/leave flow and local/remote track handling.

## Run

1. Open a terminal in this folder.
2. Start any static web server (example using Python):

```bash
python -m http.server 8080
```

3. Open http://localhost:8080 in your browser.
4. Enter your Agora App ID.
5. Enter a channel name.
6. Add a token if your Agora project has App Certificate enabled.
7. Click Join.

## Notes

- For camera/microphone access, run from `localhost` or HTTPS.
- Open the page in two browser tabs (or different devices) with the same channel to test remote video.
