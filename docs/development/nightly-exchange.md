# Private nightly Gameplan exchange

Jeremy's October 13, 2026 direction permanently authorizes this nightly workflow. Atlas and Scout each produce Gameplans for their own eleven symbols. Synthesize those two Gameplans once, using one actual cash and stock balance, into the same 22-symbol Gameplan shown on both PCs. Atlas can perform synthesis directly and return the completed result to Scout without a reciprocal acceptance wait.

Atlas runs the combined Gameplan trader and its own 15-minute trader. Scout runs its own 15-minute trader. All three trade and log independently. There is no 15-minute synthesis, cross-trader reconciliation, compatibility verification, shared trading ledger, distributed grant, or approval prerequisite. Preparing the plans and traders is permanently authorized; the human manually starts each trader.

The current operating contract is [Independent trading systems](independent-traders.md).

## Transport and contents

Keep the existing private `CODEXSTORE/ducketz-nightly-exchange/v1` transport and historical receipts. The local `github_only` coordination policy applies to routine discussion and source handoffs, not this private nightly exchange. Use existing authenticated connections and each PC's local binding.

The nightly operating inputs are Atlas's eleven-symbol Gameplan package, Scout's eleven-symbol Gameplan package, their original Stats, and one actual cash/stock snapshot for the combined account calculation. Combine original forecasts and planning prices once; do not combine independently sized cash projections.

Send the completed 22-symbol Gameplan and combined Stats back to both machines. Atlas may synthesize from the two packages and publish immediately. Do not wait for Scout to accept Atlas's result, or send acknowledgments back and forth as a prerequisite to completion. Preserve one completion identity through retries and retain the original producer/forecast identities.

No 15-minute forecasts, native ledgers, runtime attestations, fill histories or health reports are required by this flow. Optional monitoring is informational and never a trader or synthesis prerequisite. No distributed order-reservation transport is required between the PCs.

## Recovery

Resume the unfinished exchange from its saved dated state. Reuse completed packages and successful delivery stages; handle missing actual inputs or transport failure directly under permanent standing authorization. Preserve the original failure and actual recovery time. Do not fabricate received packages, successful sends, fills, data or Stats, and do not reopen completed model work merely to repair transport.

Shared-source fixes use the installed coordination completion queue and each PC's own source namespace. Retain local originals and historical transport receipts. Source changes and private operating artifacts have separate publication rules; a private snapshot or native database is never a portable GitHub source notice.

## Trading boundary

Every night prepares the three independent traders for the human's manual start. Atlas's Gameplan worker consumes the combined Gameplan; its local 15-minute worker and Scout's local 15-minute worker consume their own forecasts. Each uses actual broker account capacity and logs its own orders. No trader reads or reconciles another trader's local state.
