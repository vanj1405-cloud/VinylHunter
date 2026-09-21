# CrateRadar / Vinyl Hunter — Backend MASTER V2

This package is designed to be dropped into the existing Vinyl Hunter build
without touching the current HTML/CSS/JS or Flask UI.

## What is live now
- Existing proven Israeli engine preserved verbatim in `providers/israel_legacy.py`.
- Vinylia and Holit promoted to LIVE after qualification.
- Rollin' Dise added as LIVE using the proven Shopify adapter.
- The Vinyl Room and My Records remain TESTING.
- Party Concept and all previous stability/prefilter work are preserved.

## New architecture
- `core.py` — compatibility facade. Existing app can keep `import core`.
- `providers/israel.py` — Israeli registry/config.
- `providers/israel_legacy.py` — proven scraping engine. Change only when an Israeli adapter itself breaks.
- `providers/international.py` — international candidates + qualification probes.
- `providers/marketplaces.py` — Discogs/eBay credentials and future adapters.
- `providers/official.py` — official artist/label discovery/ranking foundation.
- `models.py` — global result schema including shipping, delivered price and condition.

## Official-first rule
Priority is: Official Artist → Official Label → Israeli independent → International store → Marketplace.
Price transparency remains separate: delivered price should always be visible.

## Important
International sources are candidates, not production-search providers yet.
They should only be enabled after qualification and source-specific adapters are proven.

## Smoke tests
`python smoke_test.py`
`python tests/test_israel.py`
`python tests/test_international.py`

## Installation into the current project
Back up your current build once, then copy the contents of this package into
the project root. Do NOT overwrite your existing UI files; this package does
not contain or modify them.

## Optional Radio by Genre frontend module
`radio_by_genre.js` is a self-contained, drop-in radio widget. It is deliberately
scoped so it does not alter the existing Vinyl Hunter / CrateRadar layout or CSS.

To use it in the EXISTING Flask UI:
1. Copy `radio_by_genre.js` into your existing `static/` folder.
2. Add this one line immediately before `</body>` in the existing template:

```html
<script src="{{ url_for('static', filename='radio_by_genre.js') }}"></script>
```

The widget starts muted/off and requires an explicit click to play, which follows
modern browser autoplay rules. It supports Rock, Psychedelic, Jazz, Soul, Funk,
Blues, Indie, Alternative, Electronic, House, Techno, Hip-Hop, Reggae, Metal,
Punk, Classical, Ambient and World.

The search UI can later switch genre without redesigning the page:

```js
window.CrateRadio.setGenre('jazz');
```

Or pass discovered metadata:

```js
window.CrateRadio.setSearchContext({artist: 'John Coltrane', genre: 'jazz'});
```
