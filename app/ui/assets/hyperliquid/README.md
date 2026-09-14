# Hyperliquid UI assets

The Hyperliquid Duckets tab automatically uses these optional transparent PNGs:

- `hype.png` — HYPE mark used by the market tile.
- `jeremy.png` — Jeremy portrait used by the account card and position rows.
- `alex.png` — Alex portrait used by the account card and position rows.
- `clearpond-avatar.png` — circular Clear Pond Consulting CP mark used by the account card, composer, cash panel, and position rows.
- `btc.png`, `eth.png`, `zec.png` — supplied market logos copied from the September 13 concept directory.

`clearpond.png` is the original supplied logo sheet. The standalone avatar was
extracted with built-in ImageGen; its prompt and provenance are recorded in
`docs/hyperliquid-duckets-design-ui/2026-09-13-clearpond-multi-asset/IMPLEMENTATION.md`.

Use square 512×512 PNGs with transparency and keep the visible artwork away from
the outermost few pixels. Pillow scales them with antialiasing at display time;
the original files are preserved. If
an image is absent or unreadable, the live UI renders its built-in HYPE glyph or
account-initial fallback instead.
