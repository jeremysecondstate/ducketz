# System functionality and current limits

Last verified: **2026-09-25**. [Index](README.md)

## Implemented capabilities

| Area | Current behavior | Evidence / detailed reference |
| --- | --- | --- |
| Market maintenance | Independent per-symbol updates, bounded parallel work, completed-candle publications, retained local history and revision catch-up | [Market-data loop](loops/market-data.md) |
| Features | Shared causal formulas, per-build calculation reuse, separate symbol datasets, explicit warmup/null/gap behavior and feature catalog | [Feature implementation](../../technicals/hyperliquid_features.py) |
| Model fitting | Separate per-symbol/horizon bundles; chronological fitting, calibration and assessment; target-maturity exclusions at boundaries | [Model loop](loops/models.md) |
| Qualification | Saved candidate and qualified-active distinction; probability-score comparisons with baselines; fresh active preference and labeled research fallback | [Model artifacts](../../ml/hyperliquid_model_artifacts.py) |
| Forecast history | Recorded not-down/down probabilities before outcomes, once per symbol/interval/horizon/candle, with matured forward evaluation | [Model loop](loops/models.md) |
| Opening Paper inventory | Configured mirror of real marked holdings/cash, validated roles, immutable opening baseline, existing-ledger resume | [Paper initialization](../../ml/hyperliquid_paper_seed.py) |
| Portfolio allocation | Pool-level volatility/confidence sizing, directional account roles, per-symbol/pool/account constraints, virtual collateral transfers | [Accounting and risk](PAPER_ACCOUNTING_AND_RISK.md) |
| Simulated execution | Spot/perp book validation, finite shared depth, rounded partial fills, fee/slippage assumptions, retained execution details | [Paper market simulation](../../ml/hyperliquid_paper_market.py) |
| Paper journal | Transactional cycles, fills, transfers, decisions, funding, equity, events and opening inventory | [Paper ledger](../../ml/hyperliquid_paper_ledger.py) |
| Risk observations | Polled stops, recorded cooldowns and reduction paths; missing held-market marks can prevent an entire tick | [Paper loop](loops/paper.md) |
| Performance | Opening-baseline P/L, costs, gross exposure, pooled drawdown and flat cash comparator in exported report | [Accounting and risk](PAPER_ACCOUNTING_AND_RISK.md) |
| Operations | Per-source clocks and errors, lifecycle status, persistent data/model timing evidence, on-demand duration reports | [Monitoring](MONITORING.md) |
| Powder execution | Explicit user activation, matching existing-position adoption, actual collateral sizing, durable order IDs, reconciliation and shared manual ownership guards | [Activation and recovery](POWDER_ACTIVATION.md) |
| H.Y.P.E.R. | Read-only Paper account cards/charts/journals/inspector and separate Powder observations/intents/actual fills | [UI contract](UI_AND_DATA_CONTRACTS.md) |

## Decisions that remain separate

1. A **published feature snapshot** can be valid while an estimator's fitting
   cutoff is several days older. Publication time, prediction input close,
   fitting cutoff and calibration cutoff are different clocks.
2. A **qualified model** has satisfied the implemented retrospective probability
   criteria. It has not established profitable execution after fees, funding
   and risk. A research model can participate in the currently configured
   Paper policy without being relabeled qualified.
3. A **forecast** is an observation of a rolling horizon. Four 15-minute bars
   mean one hour, not four scheduled trades or a mandatory one-hour exit.
4. A **desired target** does not prove cash is available or an executable fill
   exists. A requested simulated quantity can be partly filled or skipped.
5. An **alive process** can publish stale or degraded data. A saved `running`
   flag without current timestamps/process evidence is insufficient.
6. A **gracefully stopped Paper process** preserves its ledger and holdings but
   performs no continued quote/risk checks. It does not flatten positions.

## Current limits that matter when extending the system

| Limit | Practical implication |
| --- | --- |
| Powder requires explicit activation and supported verified account routing | No default/background activation; subaccounts, vaults and portfolio margin are rejected in V1. |
| Powder cashflow performance and automatic transfers are not implemented | Actual equity/positions/fills are available after activation; P/L/drawdown/funding totals stay unavailable rather than using Paper estimates. |
| No exchange liquidation/maintenance-margin simulation | Paper exposure/collateral checks and polled stops do not establish exchange liquidation protection. |
| Visible-book simulation | No maker queue, guaranteed future liquidity or full exchange latency model; recorded partial fills remain partial. |
| Funding uses a completed-perpetual-close proxy | Funding is labeled estimated and unavailable settlements remain pending errors. |
| Models are evaluated bundles without a final all-data refit | Reserved calibration/assessment data are not silently added to estimator fitting after qualification. |
| Runtime configuration is broader than the current UI | Data/models support configured membership; the Paper view currently discovers BTC/ETH/HYPE/ZEC at `15m/h4`. Universe/interval/horizon changes require a UI/adapter review. |
| Paper uses the first configured model horizon | Adding model horizons does not create independent Paper horizon portfolios. |
| Full snapshot rebuild/publication | Data writes complete changed Parquet snapshots; persistent incremental feature state and automatic snapshot retention are not implemented. |
| Bounded UI journal reads | The current page shows the latest 500 records per journal by default, then filters those loaded rows; it is not an exhaustive historical query browser. |
| Forward evaluation is not automatic strategy optimization | Paper results and forward model metrics do not automatically retune sizing, ensemble weights or trading policy. |
| Process entry points are not startup registration | Reboot supervision and any external scheduler must be inspected separately. |

## Future work is recorded as planned

Extending Powder requires complete cashflow/funding accounting, reviewed
transfer routes and durable transfer reconciliation. See the current
[Powder runbook](POWDER_ACTIVATION.md) and historical
[Powder design contract](../hyperliquid-duckets-design-ui/hyper-2026-09-25/HYPER_DESIGN.md).
The historical design defines intended evidence, not current runtime state.

Changes to the experiment should carry their own source/configuration identity
and verification. Extend these pages when the behavior ships, and record the
change in [CHANGELOG.md](CHANGELOG.md); avoid moving planned capabilities into
the implemented table in advance.
