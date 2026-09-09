# Shared equity security marks

The Schwab Duckets and Rolling Forecasts UIs load optional, locally bundled PNG
security marks from this directory. At runtime they never download marks.
Missing, invalid, and unknown-symbol assets use a built-in ticker-monogram
fallback, with the ticker also shown as text.

## Current local assets

| Ticker | Runtime file | User-supplied source |
| --- | --- | --- |
| AAPL | `aapl.png` | `docs/logos-icons/appl-logo.png` |
| AMZN | `amzn.png` | `docs/logos-icons/amzn-logo.png` |
| EWY | `ewy.png` | `docs/logos-icons/ewy-logo.png` |
| GOOG, GOOGL | `goog.png` | `docs/logos-icons/goog-logo.png` |
| MRNA | `mrna.png` | `docs/logos-icons/mrna-logo.png` |
| MU | `mu.png` | `docs/logos-icons/micron-logo.png` |
| NBIS | `nbis.png` | `docs/logos-icons/nbis-logo.png` |
| NVDA | `nvda.png` | `docs/logos-icons/nvda-logo.png` |
| SLS | `sls.png` | `docs/logos-icons/sls-logo.png` |
| SNDK | `sndk.png` | `docs/logos-icons/sndk-logo.png` |
| TENB | `tenb.png` | `docs/logos-icons/tenb-logo.png` |
| VXUS | `vxus.png` | `docs/logos-icons/vxus-logo.png` |
| ZETA | `zeta.png` | `docs/logos-icons/zeta-logo.png` |

These 512x512 transparent PNGs were supplied by the project owner on
2026-08-30 and 2026-08-31 for local display in this project. This provenance
record does not grant or independently verify trademark or redistribution
rights; confirm those rights before redistributing the image files.

The UIs preserve aspect ratio and downsample each image to a maximum of 18x18
pixels in portfolio rows, 38x38 pixels in the Selected Holding card, and 80x80
pixels in the Rolling Forecasts Prediction Pulse matrix. PNG transparency is
preserved without adding a white or black backing plate. New assets should use
a transparent 1:1 canvas, a lowercase ticker filename, and be added to
`SECURITY_MARK_FILENAMES` in `app/ui/schwab_duckets.py` when Schwab Duckets also
uses the symbol.
