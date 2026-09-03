# Hyperliquid UI assets

The Hyperliquid Duckets tab automatically uses these optional transparent PNGs:

- `hype.png` — HYPE mark used by the market pulse card.
- `jeremy.png` — Jeremy portrait used by the account card and position rows.
- `alex.png` — Alex portrait used by the account card and position rows.

Use square 512×512 PNGs with transparency and keep the visible artwork away from
the outermost few pixels. The UI scales them down to the card and table sizes. If
an image is absent or unreadable, the live UI renders its built-in HYPE glyph or
account-initial fallback instead.
