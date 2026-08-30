# Schwab Duckets security marks

The Schwab Duckets UI looks here for optional, locally bundled `aapl.png` and
`msft.png` security marks. No trademarked logo files are currently bundled
because the repository does not contain verified source or redistribution
permission for them.

At runtime the UI never downloads marks. Missing, invalid, and unknown-symbol
assets use the built-in ticker-monogram fallback, with the ticker also shown as
text. A future asset may be added only after its source and permitted use are
documented in this file.
