# Schwab Duckets security marks

The Schwab Duckets UI loads optional, locally bundled PNG security marks from
this directory. At runtime it never downloads marks. Missing, invalid, and
unknown-symbol assets use the built-in ticker-monogram fallback, with the ticker
also shown as text.

## Current local assets

| Ticker | Runtime file | User-supplied source |
| --- | --- | --- |
| AAPL | `aapl.png` | `docs/logos-icons/appl-logo.png` |
| AMZN | `amzn.png` | `docs/logos-icons/amzn-logo.png` |
| GOOG, GOOGL | `goog.png` | `docs/logos-icons/goog-logo.png` |
| MU | `mu.png` | `docs/logos-icons/micron-logo.png` |
| NVDA | `nvda.png` | `docs/logos-icons/nvda-logo.png` |
| SNDK | `sndk.png` | `docs/logos-icons/sndk-logo.png` |

These 512x512 transparent PNGs were supplied by the project owner on
2026-08-30 for local display in this project. This provenance record does not
grant or independently verify trademark or redistribution rights; confirm those
rights before redistributing the image files.

The UI preserves aspect ratio and downsamples each image to a maximum of 18x18
pixels in portfolio rows and 38x38 pixels in the Selected Holding card. New
assets should use a transparent 1:1 canvas and be added to
`SECURITY_MARK_FILENAMES` in `app/ui/schwab_duckets.py`.
