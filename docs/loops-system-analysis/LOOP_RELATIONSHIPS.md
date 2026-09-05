# Loop relationships

## Current sequence

Current relationships are sequential artifact handoffs inside one overnight
workflow, not independently timed live-loop relationships.

```text
provider/session data
  -> Loop A close fetch and OPRA cursors
  -> Loop B causal feature/sample authority
  -> Options Strategy profit models
  -> Strategy exact candidates
  -> immutable next-session gameplan
  -> Duckets clock-rotating forecast UI
  -> daytime exact-once stock trader and optional paper reader
  -> next-close directional evaluation
```

`D` means data, `C` control/readiness, `M` model, and `E` evaluation.

## R1 — Providers and prior storage → Loop A close fetch

- **Type:** D + C.
- **Exchange:** newest completed-session EQUS equity data, macro/cross-asset
  inputs, and incremental OPRA `definition`, `cbbo-1m`, and `ohlcv-1h` for all
  six parent symbols.
- **Boundary:** OPRA provider estimate, byte/cost cap, lock, partition receipt,
  and exclusive `completed_through` cursor.
- **Failure:** any required production cursor lag or invalid receipt stops the
  overnight chain. A closed market with no newer session is not a lag.

## R2 — Loop A → Directional Loop B

- **Type:** D + C.
- **Exchange:** complete provider cycle, normalized equity bars, feature-family
  artifacts, availability clocks, and route readiness.
- **Boundary:** Loop B uses only evidence available by the overnight cutoff and
  publishes one checksum-bound generation.
- **Failure:** missing required routes or incompatible evidence blocks later
  stages; no previous/partial generation is silently relabeled current.

## R3 — Loop A OPRA history + Loop B → Strategy-profit training

- **Type:** D + M.
- **Exchange:** Loop B samples/directional features and verified historical OPRA
  contract/quote/surface partitions.
- **Boundary:** every one of the 18 production cursors must cover the newest
  required completed session. Historical candidate entry/exit economics use
  nearest causal `cbbo-1m` BBO snapshots where available. `1h` requires exact
  CBBO; older `4h`/`1d`/`1w` targets may use a separately labeled conservative
  hourly fallback, never an OHLC bar presented as an executable quote.
- **Model:** separate `1h`, `4h`, `1d`, and `1w` HGB/MLP challenger paths with
  purged chronological selection, calibration, and assessment cohorts.
- **Failure:** a rejected horizon prevents publication of a new complete
  multi-horizon authority and preserves the prior pointer.

## R4 — Loop B + Strategy-profit authority → Strategy generation

- **Type:** D + M.
- **Exchange:** directional probability, causal features, registered candidate
  definitions, exact option legs, completed-session BBO/liquidity evidence, and
  promoted profitable-outcome models.
- **Planning freshness:** the final quote from the completed session may be
  hours old after close and remains valid for overnight selection.
- **Execution boundary:** planning validity does not grant order validity. A
  future executor must revalidate the same legs with a current tradable quote
  and may execute or skip only.

## R5 — Loop B + Strategy → nightly gameplan

- **Type:** D + M + C.
- **Exchange:** current causal source rows, historical one-minute equity labels,
  four path-model groups, and exact Strategy candidates.
- **Output:** 144 directional forecasts and 144 options intents for one action
  date, plus model reports and prior-plan directional evaluations.
- **Boundary:** publication occurs atomically before 04:00 PT. The plan cannot be
  republished once that action window begins.

## R6 — immutable gameplan → daytime consumers

- **Type:** D + C.
- **Exchange:** exact action date, route, probability, model status, frozen
  candidate/legs, and execution constraints.
- **Paper consumer:** `ml.gameplan_executor` validates and records
  advisory/paper decisions only.
- **Live stock consumer:** `ml.gameplan_stock_trader` uses the same immutable
  forecast as signal input to the established stock risk engine. Two persistent
  controls, `--execute`, current Schwab state/quotes, session/deadline checks,
  exposure/cash/spread limits, and exact-once reservations are mandatory.
- **Horizon collision:** the 1-hour signal controls entry timing. An active
  4-hour signal is confirmation; an opposite actionable direction vetoes a new
  entry and agreement still permits at most one order per symbol.
- **Prohibitions:** no fetch, training, candidate substitution, intraday plan
  update, short sale, or option order.

The Duckets `Rolling Forecasts` tab is a separate read-only projection of the
same authority. It selects the active frozen 1h/4h target window at load time,
refreshes five seconds after each wall-clock hour, and exposes all five daily
components without changing the underlying plan. It joins every displayed
forecast to the same-route frozen options intent and exposes the Strategy,
profit probability, pricing source, and explicit decision reason.

## R7 — completed action day → next nightly evaluation

- **Type:** E.
- **Exchange:** the prior immutable directional forecasts and newly completed
  one-minute equity bars/outcomes.
- **Boundary:** evaluate only matured targets and preserve unmatched targets as
  pending. Because the source plan was frozen, results are reproducible.
  Realized option P/L additionally requires an exact-leg execution receipt;
  absent that, no trade outcome is inferred.

## Evidence-family boundaries

- EQUS operational bars, Schwab price history, and XNAS archive provenance stay
  separate. Similar timestamps are not proof of identical rows or authority.
- OPRA DBN, normalized Parquet, manifest, and receipt form one checksum-bound
  lineage.
- Historical OPRA is valid for offline training and completed-session planning.
  It is not silently converted into a current live quote.
- The stock action grid is 04:00–17:00 PT. Option contracts retain their own
  tradable-session restriction even when their underlyings trade extended hours.

## Legacy timing relationships

The old 15-minute/hourly phase relationships remain documented in per-loop git
history and implementation code for explicit recovery. They are not current
production scheduling authority and must not be used to restart the stopped
stack.
