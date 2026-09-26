# Paper retuning and verified fresh mirror

The user requested training adjustments and a fresh one-for-one Paper start
against the actual accounts. Three bounded training alternatives were tested
and rejected because both probability-loss measures worsened. The retained
model recipe was freshly fitted. Paper's ordinary rebalance threshold changed
from 10% to **20% of target notional** (still at least $25); complete exits and
forced risk reductions retain their bypass. Shared 49%/51% entries/exits,
qualification, exposure limits, stops, fees and zero added slippage are unchanged.

This is a modest forward turnover experiment, not a demonstrated profitable
training improvement. [Training comparison](2026-09-26-training-regularization-and-budget.md)
documents 56 new fits and the repeated-history limitations. The
[matched-price diagnosis](../../../artifacts/analysis/hyper-paper-20260926-retune/DIAGNOSIS.md)
separates startup costs from subsequent position performance. At its 15:40:47 UTC
observation Paper lagged opening-hold by $75.62 excluding funding; fees were
$42.59, of which $26.91 came from the initial seven portfolio adjustments.
Screening old decisions identified only $1.44 of direct fees below the new
threshold; this is not a sequential replay or forecast of savings.

## Preservation

Paper PID53036 and models PID56520 stopped gracefully; both lifetime locks were
available and no competing owner remained. Data PID57004 continued throughout.
The existing watch was left active with an `in_progress` maintenance marker
requiring observational behavior during the transition.

The complete previous `_paper` and `_models` trees are preserved at
`C:/DATASTORE/hyperliquid/_paper_archives/20260926T154800Z-before-training-retune`.
All **404 files / 256,957,217 bytes** match the pre-move SHA-256 manifest and the
archived ledger passed integrity checking. No historical losses, forecast
records or fill journals were rewritten. The permanently excluded predecessor
was not accessed, restored or evaluated.

The last old-run observation, 15:45:34 UTC, was equity **$42,007.41**, opening P/L
**-$80.84**, fees **$45.89**, estimated funding **+$0.24**, 41 fills and 8 virtual
transfers. That final cycle lacked a closed HYPE perpetual mark; the earlier
fully matched benchmark was retained rather than inventing a final benchmark.

## Fresh models and opening

Fresh fitting used the original uncapped recipe, minimum 1,000 fitting rows,
192 calibration rows, 288 assessment rows and four 15-minute bars. The new
model namespace retains no old active fallback or old forecast deduplication
keys; all prior artifacts remain in the archive. At the first 15:45 decision
publication, ZEC qualified; BTC, ETH and HYPE were Research. Qualification was
not relaxed to produce trades. Continuous model retraining remains every 900s.

Experiment **`20260926T154957Z-rebalance20-fresh-models`** opened at
**2026-09-26 15:49:57.767916203 UTC / 08:49:57 PT** under policy
**`fdf124acdc8fc9a6`**.

| Account | Opening equity |
| --- | ---: |
| Alex | $5,782.635843127269 |
| Jeremy | $6,222.392986396880 |
| Clear Pond | $30,076.178407994368 |
| Pool | **$42,081.207237518516** |

Before trading, independent read-only verification found **nine inherited
positions, one opening cycle, four equity rows, zero fills, decisions, transfers,
funding and fees, and $0 experiment P/L**. Signed inventory, cash/collateral,
historical perpetual entries and separate opening-mark stop references reconcile.
Source-to-opening quote-valuation differences were Alex -$0.006585, Jeremy
+$0.004615 and Clear Pond $0. Public account/quote requests are sequential;
no artificial cash adjustment forces their different-time displays to agree.
The verifier uses retained normalized source data; separate raw account responses
are not preserved as a second independent inventory observation.

## Resumed trading and validation

Continuous Paper PID67548 and models PID49636 were verified as exact module/
configuration owners, alongside continuing data PID57004. The first trading
sweep committed at **15:50:43 UTC**. Seven simulated fills cost **$27.01637455**
in fees and left equity **$42,049.95430344**, P/L **-$31.25293408** and gross
exposure **$2,437.27672232**. Initial risk/target adjustments still cost money;
the fresh opening is preserved separately from those subsequent trades.

The first four fills reduced BTC/HYPE risk; the last three adjusted ZEC from
its Qualified forecast. Research forecasts did not drive signal trades. All
fills used executable-depth VWAP and the configured 0.045% perp / 0.070% spot
fees. Independent replay reconciled every committed cycle's inventory, cash,
cost basis, stop reference, fees and marked equity to the unchanged opening.
Powder remained inactive, with no pending intents or real-account mutations.

**609 relevant tests passed**: 231 policy/runtime tests, plus 378 seed, ledger,
market, model, model-config/runtime/artifact and forecast-reader tests.
Evidence, verifiers, source/config backups, model fit output, opening proof,
first-cycle accounting and process/freshness checks are in
`C:/DATASTORE/hyperliquid/_operations/paper-retune-20260926/`.
Operations Watch contract v10 adopts this baseline; ordinary future recovery
must resume it without reseeding. Current process and subsequent health evidence
remain timestamped observations rather than permanent guarantees.

Final health at **15:55:27 UTC** verified all projected sources fresh and no
warnings, with the same exact three owners and committed Paper advancement to
**15:54:57 UTC**, equity **$42,048.86987740**. Opening hashes remained unchanged.
The existing watch's schedule, model, effort, project and ACTIVE state were
verified unchanged after its prompt update. Maintenance was atomically completed
at **15:55:28.333240 UTC**, followed by accepted v10 memory; prior watch memory was
preserved separately. Final operating identity is in `deployment.json`.
