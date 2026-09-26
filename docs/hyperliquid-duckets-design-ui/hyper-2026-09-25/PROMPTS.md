# H.Y.P.E.R. image-generation prompts

Generated with the built-in image generation tool (not the API/CLI fallback). The existing concept-a image was used only as a visual reference. No application UI or execution code was changed. All displayed sample values are illustrative.

Final project assets:

- `hyper-paper-command-center.png`
- `hyper-powder-command-center.png`

## Initial Paper concept

Reference: `../concept-a-portfolio-hype-command-center.png` (the source lives one directory above this concept folder).

```text
Use case: ui-mockup
Asset type: a full-screen, high-fidelity desktop financial application concept, 2560x1600 landscape, front-on screenshot, no laptop/device frame.
Primary request: Redesign the supplied reference image into a NEW H.Y.P.E.R. trading-operations tab within the Duckets desktop app. This is a design concept, not a live screenshot. Use the reference ONLY for its elegant dark navy surfaces, fine blue-gray borders, restrained mint accents, compact professional data tables and desktop density. Improve the hierarchy, legibility and spacing. No excessive glow, no cyberpunk, no decorative stock photos.

Top app tabs: Rolling Forecasts | Options Strategies | Schwab Duckets | Hyperliquid Duckets | H.Y.P.E.R.
H.Y.P.E.R. is selected with a mint underline.
Header: large exact title "H.Y.P.E.R." with a small subtitle "Habēre Yper- Prodictum Electrōnicum Rōboris".
Second smaller subtitle: "To have an ultra-productive electronic asset of strength".
Below header, clearly visible sub-tabs "Paper" (selected mint) and "Powder" (unselected). Next to them a mint-tinted badge "SIMULATED", quiet text "Mirrored accounts · Started Sep 25", top-right button "Pause paper" and a subtle "Export" button. Do not add a live trading switch to the Paper screen.

Use a coherent 3-column composition:
LEFT rail about 21% width, CENTER about 54%, RIGHT about 25%.
A narrow full-width operational strip below the subtabs: "Data  •  15m candles"   "Models  •  Ready · Hourly fit"   "Forecasts  •  Every 15m"   "Paper  •  Running · 30s checks"   "Updated 8s ago". Small tasteful green dots. Distinguish model readiness from actively fitting.
A full-width row of five compact metrics above the three columns: "Paper equity  $42,043.68" ; "Since start  −$34.60  (−0.08%)" in restrained red ; "Gross exposure  10.8% / 60%" ; "Fees  $25.68" ; "Max drawdown  0.10%". All numeric figures in the concept are illustrative.

LEFT column heading "Accounts" with account filter "All accounts". Three substantial stacked cards, not photos: circular initial badges AL, JE, CP.
"Alex" with role "SHORT PERPS ONLY"; Equity $5,825.08; P/L since start −$11.69; "Flat · USDC available".
"Jeremy" role "LONG PERPS ONLY"; Equity $6,177.90; P/L since start −$4.66; small holding "HYPE · Long 24.14".
"Clear Pond" role "SPOT ONLY"; Equity $30,040.70; P/L since start −$18.24; small holding "HYPE · Spot 25.04".
Bottom rail: small "Pool allocation" segmented horizontal bar, labels Cash / Spot / Long perps / Short perps. Never imply separate wallets share exchange margin.

CENTER top largest panel "Performance since paper start". Elegant, large readable time series equity chart, x-axis 19:00 19:10 19:20 19:30, y-axis about $42,080 to $42,040. Line begins at $42,078, dips from initial costs then moves slightly. A clearly labeled dashed horizontal "Opening equity" baseline. Do not depict dramatic gains. Chart toolbar Equity | P/L | Drawdown, range 1H | 24H | 7D | All; Equity and All selected. Under chart a narrow cost line "Trading P/L −$8.92   Fees −$25.68   Funding $0.00 · estimated". Not a fake profit dashboard.
CENTER below chart panel "Positions & activity" with tabs "Positions" | "Trades" (selected) | "Transfers" | "Decisions". Dense but legible table with header Time / Account / Market / Action / Qty / Fill / Fee / Model. Four rows:
19:04:12 Clear Pond BTC Spot Sell 0.20013 $84,000.20 $11.77 Research
19:04:12 Alex HYPE Perp Close short 80 $92.19 $3.32 Research
19:04:13 Jeremy HYPE Perp Reduce long 35.86 $92.15 $1.49 Research
19:04:13 Jeremy ZEC Perp Close long 4.50 $1,543.99 $3.13 Qualified
Use distinct small pill badges for "Qualified" (mint) and "Research" (muted amber). The selected HYPE Alex row gets a faint outline; use precise alignments and plenty of readable row height.

RIGHT top panel "Latest forecasts" with small "1h horizon · 19:30 close". BTC 54.4% Research, ETH 50.7% Qualified, HYPE 54.2% Research, ZEC 49.3% Qualified. Column label "P(not-down)". Display short probability bars centered on a 50% tick and faint 45%/55% thresholds, not confidence meters implying expected returns.
RIGHT lower panel "Decision detail" for selected row: "Alex · HYPE" then "Close short" plus a small amber "Entry-based stop". Fields "Trigger  3% adverse move", "Price source  Fresh perp book", "Execution  Simulated taker", "Fee  $3.32", "Model  Research candidate", "Cooldown  48m remaining". A tiny mini-sequence "Forecast → Target → Fill → Ledger", with links "View forecast" and "View ledger".
Bottom of right rail small collapsed "Policy" with "15% / symbol · 60% pool cap".

Footer: left "H.Y.P.E.R. / Paper"; right "DESIGN CONCEPT · Illustrative data". No actual real-money execution button. The entire screen must feel implementable in a real desktop portfolio application, with crisp typography, intentional hierarchy, easy-to-read trades and a calm operations-first feel. Make the Paper/Powder navigation prominent.
```

## Final Paper refinement

Reference: initial generated Paper concept. The refinement corrects exposure accounting and committed-ledger status while preserving layout.

```text
Use case: precise-object-edit. Edit the supplied H.Y.P.E.R. Paper dashboard screenshot. Preserve the entire layout, exact dimensions, typography, color palette, header, account cards, chart, trades, probabilities, controls and footer. Make only these two small accuracy corrections:
1. Replace the bottom-left "Pool allocation" stacked-bar mini-panel and its Cash/Spot/Long perps/Short perps percentages with a clean compact "Gross exposure" breakdown, fitting the exact same region. Large total "$4,534 · 10.8%". Below it three small horizontal rows: "Spot  $2,309", "Long perps  $2,225", "Short perps  $0". Use mint/blue/coral swatches sparingly. Do NOT show any pie or allocation that totals 100%; perp notional is not purchased cash inventory. Keep the quiet sentence "Accounts are isolated. Pool exposure does not imply shared exchange margin." wrapped below.
2. In the lower-right Decision detail mini-sequence "Forecast → Target → Fill → Ledger", make Ledger a completed mint check like the other three, since this trade is in the recorded ledger.
Everything else stays exactly as in the image. Keep Paper selected and the SIMULATED badge. It remains a design concept with illustrative data. Crisp high-resolution 2560x1600-style desktop screenshot.
```

## Powder counterpart

Reference: initial generated Paper concept. The user specified that Powder should mirror Paper and track future real executions.

```text
Use case: precise-object-edit / ui-mockup. Use the supplied H.Y.P.E.R. Paper dashboard as the exact structural template for its sibling Powder tab. The user explicitly wants Powder to be a mirror image of Paper, tracking REAL exchange executions when implemented. Preserve exact screen geometry, panel positions, typography, dark navy surfaces, mint accents and data density. Same global top tabs and H.Y.P.E.R. header/subtitle. This is a polished design concept of Powder's honest CURRENT UNCONNECTED state, not a fake live account screenshot.

Changes only:
- Select "Powder"; "Paper" unselected. Replace SIMULATED badge with amber "REAL MONEY". Adjacent quiet label "Not connected". Keep the badge clearly visible. Top-right replace Pause paper button with a disabled "Execution unavailable" button; Export stays muted. Never add a working enable-live toggle.
- Status strip same geometry: "Data • Current" green; "Models • Ready · Hourly fit" green; "Forecasts • Every 15m" green; "Execution • Not connected" amber. End "Awaiting exchange connection".
- Metrics row same5cards: "Account equity —", "Since activation —", "Gross exposure — / 60%", "Fees —", "Max drawdown —". No zeros implying real account balances.
- Left accounts keep Alex SHORT PERPS ONLY, Jeremy LONG PERPS ONLY, Clear Pond SPOT ONLY in exactly the same cards. Equity "—", P/L since activation "—". Quiet line "Awaiting account sync". No sample holdings represented as actual.
- Bottom-left replace allocation panel with "Gross exposure" and three rows "Spot —", "Long perps —", "Short perps —". Quiet note "Accounts are isolated. Pool exposure does not imply shared exchange margin."
- Center top retain large chart panel and all Equity/P&L/Drawdown and time controls in their positions. Heading "Performance since activation". Display subdued chart grid with no fabricated line. Center a restrained tasteful line icon of a chart and text "Real performance will appear here", small secondary line "Starts when exchange execution is connected". Leave baseline values blank. Below retain cost strip "Trading P/L —   Fees —   Funding —".
- Center lower retain "Positions & activity", tabs Positions | Trades (selected) | Transfers | Decisions, filterAll markets and table headers Time / Account / Market / Action / Qty / Fill / Fee / Model. Blank table body with a small faint receipt icon and "No exchange fills yet". Smaller line "Confirmed and partially filled orders will appear here." No fake transactions, no simulated-data text inside this journal.
- Right top retain the EXACT latest forecasts panel from Paper; these forecasts can exist independently of real trading. Add small subtitle "Shared model forecasts" with same P(not-down), BTC54.4 Research / ETH50.7 Qualified / HYPE54.2 Research / ZEC49.3 Qualified example probabilities. Footer concept label makes clear these are illustration. This illustrates shared forecasts with separate paper and real execution.
- Right lower retain Decision detail panel but empty selected-trade state. Header "Execution detail", text "Select an exchange fill", then tidy empty rows "Order status —", "Filled / requested —", "Average fill —", "Fee —", "Exchange order ID —", "Reconciliation —". At bottom small explanation "Tracks acknowledgements, partial fills and confirmed balances." No completed pipeline checks.
- Keep bottom-right collapsed Policy panel "15% / symbol · 60% pool cap".
- Footer left "H.Y.P.E.R. / Powder"; footer right "DESIGN CONCEPT · Live execution not connected".
Do not change component positions or create a different dashboard. This must be recognizably the SAME interface as Paper with a different execution source. Keep it beautifully finished, calm, restrained, legible and implementable.
```

