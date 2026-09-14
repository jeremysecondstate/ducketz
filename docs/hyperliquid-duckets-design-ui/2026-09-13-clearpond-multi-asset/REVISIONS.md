# Concept refinement prompts

## B — Portfolio workspace

```text
Use case: ui-mockup, precise text correction.
Reference image 1 is the edit target: the Hyperliquid Duckets Concept B Portfolio workspace screenshot.
Preserve everything outside the left "Accounts" rail exactly. In particular, preserve all six navigation tabs, the four-market row, portfolio table, right-hand trade composer, Clearpond Cash & Status panel, Exposure by Market, sample-data label, portraits, and Clearpond CP monogram. Do not change their layout or text.
Correct only the small financial detail rows in the left account cards:
Jeremy's card: three detail rows must read exactly "Spot" "$12,000.00"; "Perps" "$2,400.00"; "Available" "$100.00". His headline Equity remains "$14,400.00" and Unrealized P/L remains "-$1,200.00".
Alex's card: three detail rows must read exactly "Spot" "$12,000.00"; "Perps" "$2,600.00"; "Available" "$120.00". Remove the malformed labels "Aamps" and "Maailable". His headline Equity remains "$14,600.00" and Unrealized P/L remains "+$1,200.00".
Clearpond's card: exactly two detail rows "Available" "$410.80"; "Margin used" "$0.00". Remove the stray duplicate unlabeled "$410.80". Headline Equity remains "$410.80" and Unrealized P/L remains "$0.00"; footer remains "USDC cash · No positions".
Keep the All accounts total "$29,410.80" unchanged. Exact clean typography, aligned label/value columns. No other changes.
```

## C — Market workspace

```text
Use case: ui-mockup, precise text correction.
Reference image 1 is the edit target: Hyperliquid Duckets Concept C Market workspace.
Change ONLY the Clearpond account summary card in the upper-right of the image (above the trade composer). Keep its size, position, background, Clearpond CP monogram, and account name exactly. Replace the malformed or incomplete financial labels inside this card with a clean four-metric layout containing only:
"Equity" "$410.80"
"Available" "$410.80"
"Unrealized P/L" "$0.00"
"Margin used" "$0.00"
Add a small simple caption on the bottom edge of this same card: "USDC cash · No open positions".
Remove the gibberish label and the orphan "USDC Cash" label from the original card. Do not duplicate cash or invent a holding.
Preserve absolutely everything else: Jeremy and Alex cards and all their figures, app navigation, title and sample-data badge, all four markets, the BTC price chart and time axis, trade composer with Clearpond selected, all form labels, positions table, footer, colors, spacing, and image dimensions. This is a one-card text correction, not a new design.
```

Built-in ImageGen was used for a targeted text correction pass after inspecting each first draft.

## A — Expanded overview

```text
Use case: ui-mockup, precise text correction of the supplied Concept A image.
Edit target: reference image 1, the existing Hyperliquid Duckets "A · Expanded overview" mockup.
Keep the entire layout, six navigation tabs, title, sample-data chip, market cards, positions table, avatars, CP logo, colors, and overall proportions unchanged. Do not introduce new panels or data.
Correct these specific text and ticket-label errors:
1. Alex's top account card Available must read exactly "$120.00" (it currently incorrectly shows "$1,200.00"). Keep all his other figures unchanged.
2. In the bottom-left Spot Ticket, the selected side must read exactly "Buy", opposite "Sell". Replace the misspelled "Lony". A spot order is Buy/Sell, not Long/Short.
3. In both bottom trade tickets, change "Limit Price (Optional)" to exactly "Limit Price". Add a clear "Quantity" label for the spot amount field, whose unit stays HYPE, and a "Size" label for the perp amount field, whose unit stays BTC. All amount and limit-price inputs remain blank. Preserve the existing Clearpond account selection, HYPE/USDC spot market, BTC-PERP market, Reduce only checkbox, and Review buttons.
4. Clearpond card subtitle below "No open positions" should read "410.80 USDC available" instead of "Cash account ready for trading".
Do not alter any other text, figures, images, or structure. Exact required buttons are "Review Spot Order" and "Review Perp Order". Preserve the footer "Illustrative values · Layout proposal". Render a crisp flat desktop screenshot at the same aspect ratio and at least the same detail as the original.
```
