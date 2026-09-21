# Vinyl Hunter — Streamlit Mobile V1

This build is made specifically for **Streamlit Community Cloud**.

## GitHub files
Upload the contents of this ZIP to the root of your `VinylHunter` repository.

Main file:
`streamlit_app.py`

## Streamlit Community Cloud
Create app and use:
- Repository: `vanj1405-cloud/VinylHunter`
- Branch: `main`
- Main file path: `streamlit_app.py`

## Secrets
In Streamlit → App settings → Secrets:

```toml
DISCOGS_TOKEN = "YOUR_DISCOGS_TOKEN"
DISCOGS_USERNAME = "YOUR_DISCOGS_USERNAME"
```

Never commit the real token to GitHub.

## Included
- Israeli store search via the existing Vinyl Hunter core
- International providers
- Discogs marketplace signals
- Smart Value for Artist + Album searches
- Hebrew query resolver
- Artist-only search
- iPhone camera input
- Apple Music / album screenshot OCR
- Barcode / catalog / matrix OCR
- Identify My Pressing with Discogs identifier verification
- Exact Discogs release → Wantlist sync
- Mobile-first UI

## OCR
`packages.txt` installs Tesseract English + Hebrew on Streamlit Community Cloud.
Matrix/runout etching is difficult for OCR; the detected value remains editable.

## Local test
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```
