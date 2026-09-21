VINYL HUNTER — MOBILE + CAMERA + SCREENSHOT + DISCOGS SYNC V4

Replace:
  app.py
  static/app.js
  static/style.css
  templates/index.html

Add:
  static/manifest.webmanifest
  static/service-worker.js

WHAT'S NEW
1. Mobile/PWA layout
   - Bottom navigation on phones
   - Installable-style web app shell
   - iPhone home-screen metadata
   - Responsive cards and Identify UI

2. Scan tab
   - Apple Music / music-app screenshot upload
   - Browser OCR extracts visible text
   - Artist/album fields stay editable before search
   - Rear-camera capture for barcode / catalog / matrix / serial
   - OCR guesses identifiers and sends them to Identify

3. Discogs Wantlist sync
   - Exact Discogs release IDs saved in Vinyl Hunter are mirrored to Discogs
   - Removing the exact Discogs item locally also removes it from Discogs
   - Identify results get a “Want + Discogs” button

PERSONAL DISCOGS SETUP
You already use DISCOGS_TOKEN. Add your Discogs username once:

PowerShell for current session:
  $env:DISCOGS_USERNAME="YOUR_DISCOGS_USERNAME"

Persist for future terminals:
  setx DISCOGS_USERNAME "YOUR_DISCOGS_USERNAME"

Restart PowerShell / Flask after setx.

IMPORTANT
- Never put the Discogs token in app.js or HTML. Keep it server-side.
- Screenshot/serial OCR runs in the browser through Tesseract.js.
- OCR is intentionally editable. Matrix/runout photos can be difficult; use sharp close-up light.
- Camera input uses the phone's file/camera picker, which is more reliable on iPhone web apps than relying only on getUserMedia.
- For other users later, Discogs sync should move from one server token to per-user OAuth.

PHONE ACCESS
For the real phone/PWA experience, deploy the Flask app behind HTTPS.
If running only on your Windows PC, the app remains usable on desktop; phone access over the LAN depends on your server/network setup.

After replacing files:
1. Restart Flask.
2. Ctrl+F5 on desktop.
3. On iPhone, open the HTTPS site in Safari and use Share → Add to Home Screen.
