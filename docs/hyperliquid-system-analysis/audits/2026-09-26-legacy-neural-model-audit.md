# Legacy BERA neural models: audit and compact research reuse

Read-only source audit on September 26, 2026 of
`C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/` and its callers,
preprocessing, and target generation. No applicable `AGENTS.md` was found in
the directory ancestors or BERA tree. No legacy script was imported or run;
no saved model, dataset, account, or process was changed. Architecture names
below describe the actual source, rather than assuming the directory name
implements a published architecture.

The reusable ideas are convolution over recent observations, gated recurrent
state, and a convolution/recurrent combination. Reusing the original saved
models or the original training/evaluation flow is unsuitable: their target,
feature schema, preprocessing, and test use differ from the current pipeline.
There is no measured evidence here that adding all thirteen variants would
improve forecasts or returns.

## What is actually present

There are thirteen variants in twelve folders; the transformer folder has
two variants. The dispatch list is
[modelsavecaller.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/trainingDataBERA/modelsavenn/modelsavecaller.py:3).
Outputs are binary sigmoid scores; the name `target_three` and a TFT comment
about three classes do not describe the active binary target/output.

| Variant | Actual layers and distinctive costs | Source |
| --- | --- | --- |
| CNN | Conv1D 32/128/64, two max pools, average plus maximum pooling, dense 64/32 | [cnnmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/cnn/cnnmodel.py:6) |
| LSTM | LSTM 64/32, custom temporal attention, dense 64/128 | [lstmmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/lstm/lstmmodel.py:35) |
| GRU | Bidirectional GRU 64/128, ReLU recurrence and recurrent dropout, custom attention, dense 128/64 | [grumodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/gru/grumodel.py:37) |
| RNN | Actually three bidirectional LSTMs 128/32/128, attention, dense 32/64 | [rnnmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/rnn/rnnmodel.py:9) |
| TCN | Two third-party TCN blocks with 32/128 filters, dense 32/64; kernel/dilation defaults come from the installed dependency | [tcnmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/tcn/tcnmodel.py:7) |
| DC-LSTM | Causal dilated convolution 64, LSTMs 128/64/32, residual pooling branch, dense 32/128; hardcoded 32 by 50 input | [dclstmmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/dclstm/dclstmmodel.py:6) |
| AFFN | Two-head attention, key dimension 64, average pooling, two dense 128 layers and projected residual; no positional encoding | [affnmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/affn/affnmodel.py:6) |
| ATTN | Explicit sinusoidal position encoding, two-head attention, residual normalization, pooling, dense 128/128 | [attnmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/attn/attnmodel.py:48) |
| CSANN | Four-head attention, average pooling, dense 128/128 with projected residual; close to AFFN and no position encoding | [csannmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/csann/csannmodel.py:6) |
| GARN | One bidirectional GRU 64, custom attention and gate, dense 128 | [garnmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/garn/garnmodel.py:49) |
| TFT | Gated dense residual, three-head attention, flatten, sigmoid; a compact gated-attention network, not a full Temporal Fusion Transformer implementation | [tftmodel.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/tft/tftmodel.py:7) |
| Transformer A | Two attention blocks, four heads, feedforward width 512, flatten and dense 64/32; caller fixes 32 timesteps and 50 features | [transmod.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/transformers/transmod.py:6), [caller](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/transformers/transformer_a.py:15) |
| Transformer B | Four attention blocks, four heads with key dimension 256, feedforward width 4, dense 128; very wide attention projections and a pooling-axis inconsistency | [transformermod.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/transformers/transformermod.py:23) |

Bidirectionality within a fully past-only input window is not itself future
leakage. Its costs here are extra parameters and computation. AFFN and CSANN
have permutation-invariant attention plus pooling without position encoding:
they cannot distinguish permutations of the input rows through sequence
position alone. Individual engineered features can still encode history.

## Data flow and evaluation problems

The model-group callers import the BERA
`preprocessing.preprodatas` module after an earlier similarly named import;
the later import determines the invoked `chunkload_preprocess_nn` function.
For example, see
[modelgroup_a.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/modelfunctioncalls/modelgroup_a.py:13)
and its [NN call](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/modelfunctioncalls/modelgroup_a.py:47).

1. **Scaling sees the later blocks.**
   [preprodatas.py:107](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/preprocessing/preprodatas.py:107)
   fits MinMaxScaler on all combined features before creating the
   [75/10/15 positional split](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/preprocessing/preprodatas.py:112).
   Thus validation and test feature extrema influence fitting inputs.
   Chronological ordering is assumed from storage rather than checked by
   timestamps. There is no label-horizon purge at either boundary.
2. **The label differs and immature labels become zero.**
   [targetval.py:30](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/target/targetval.py:30)
   defines `target` as next close greater than current open. The actually
   selected `target_three` is the sign of a three-bar forward log return
   divided by an exponentially weighted past volatility estimate
   [at lines 32 onward](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/target/targetval.py:32).
   With positive finite volatility this is three-bar strict-up, whereas the
   current Ducketz label is four-bar not-down. Comparison with NaN in the
   final immature rows yields false and then integer zero. The temporary
   forward-return helper columns are dropped at line 37, so those particular
   helpers do not directly leak into the feature matrix.
3. **Window alignment adds a row of delay.**
   [preprodatas.py:75](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/preprocessing/preprodatas.py:75)
   uses features `i:i+32` with label `i+32`, one row after the last feature
   row. The range also discards one otherwise available final window. An
   intentional forecasting contract could use that lag, but it must not be
   confused with labeling the final observed bar. The alternate
   [preprodatas_nn.py](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/preprocessing/preprodatas_nn.py:33)
   selects `target` instead, so it is not interchangeable with the active
   model-group preprocessing.
4. **DC-LSTM uses test outcomes during model selection.**
   [modelgroup_c.py:55](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/modelfunctioncalls/modelgroup_c.py:55)
   passes train and test to the wrapper;
   [dclstm.py:15](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/dclstm/dclstm.py:15)
   supplies that test block as validation during training, then evaluates
   the same test block. The trainer uses validation loss for early stopping,
   checkpoint selection and learning-rate reduction
   [at line 23](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/dclstm/dclstmtrain.py:23).
   Those reported test metrics are not an independent holdout estimate.
5. **Best checkpoints can be overwritten.**
   For example, LSTM early stopping does not restore best weights, while
   ModelCheckpoint saves the best epoch
   [at line 22](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/lstm/lstmtrain.py:22).
   The unconditional final
   [save at line 41](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/lstm/lstmtrain.py:41)
   overwrites that path. Similar patterns appear in CNN, GRU, AFFN, ATTN,
   CSANN, TFT and TCN. Some other trainers explicitly restore best weights.
6. **Schema and loading are fragile.**
   Feature selection is an expanding global denylist, not a versioned
   ordered schema. A test prediction helper silently trims extra features
   [to the first 50](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/preprocessing/preprodatas.py:177).
   DC-LSTM and Transformer A assume 50 features. LSTM defines custom layers
   but its training and evaluation call plain `load_model`; absent external
   custom-layer registration, deserialization can fail. This is a static
   loading risk, not a runtime failure reproduced in this audit.
7. **Transformer B pools the wrong axis for its declared input.**
   It builds channel-last input `[rows, timesteps, features]` but invokes
   `GlobalAveragePooling1D(data_format="channels_first")`
   [at line 32](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/models/nn/traintesteval/transformers/transformermod.py:32),
   averaging the feature dimension instead of the temporal dimension.

## Resource costs and minimal reuse

The chunk loader rereads every parquet row group inside each chunk iteration
[at line 90](C:/DATASTORE/hyperliquid/BERA-EXAMPLE/preprocessing/preprodatas.py:90),
then concatenates all arrays and materializes overlapping 32-row windows.
That multiplies memory by roughly the window length, while repeated model
groups repeat preprocessing. Builders save untrained models, trainers load
and repeatedly checkpoint, and evaluators load again. Several variants
train for 64 or 128 epochs. No legacy runtime or memory benchmark was run,
so these are source-identified costs rather than measured speedups.

The new research-only
[hyperliquid_research_sequences.py](C:/dev/ducketz/ml/hyperliquid_research_sequences.py:1)
implements three deliberately small representatives, rather than copying
the legacy models or checkpoints:

| Candidate | Structure | Parameter count for F input features |
| --- | --- | ---: |
| `cnn` | Two causal convolutions, 16 channels, kernel 3, dilations 1/2, mean pooling | `48F + 817` |
| `gru` | One 16-unit GRU, final hidden state | `48F + 881` |
| `cnn_gru` | Causal convolution with 16 channels followed by a 16-unit GRU | `48F + 1665` |

Each uses one logit and BCEWithLogitsLoss, Adam at 0.001, gradient clipping
at 1, a fixed default eight epochs, batch 128, CPU, and seed 42. Float32
contiguous arrays are reused; prediction batches are capped at 256 rows.
PyTorch imports lazily. CPU RNG and intra-op thread count are restored even
on exceptions. The API returns fitted predictors, parameter counts, fitting
and calibration-prediction seconds, training loss history, and explicit
configuration. It writes no files, checkpoints, or production state.

The caller owns timestamp validation, causal features, past-only windows,
train-only preprocessing, horizon purges and chronological 70/15/15 blocks.
Calibration features are only predicted after fitting; no calibration
labels or assessment data are accepted by this training API. Actual
four-market fit-time, prediction-time, and probability-loss measurements
belong to the parent offline comparison; this audit does not predict its
winner.

A CNN/GRU hybrid is a useful bounded test of local patterns plus recurrent
state, not an assumed improvement. A simple fixed probability average is
also a testable candidate if declared before examining assessment outcomes.
It should earn inclusion through paired Brier/log-loss results and
cost-aware trading evaluation. Broad architecture searches or repeatedly
changing mixtures after seeing the test block would invalidate its role as
an independent estimate. Previously examined historical periods must be
identified as reused history even when this run uses a new split.

Validation: `.venv/Scripts/python.exe -m pytest
tests/test_hyperliquid_research_sequences.py -q` passed **21 tests in 2.76
seconds**. Tests cover deterministic fits, calibration-feature independence,
CPU/RNG/thread restoration including failure, bounded batch equivalence,
input/schema/label validation, unchanged input arrays, and normalized binary
probabilities. These are implementation checks, not evidence of profitable
forecasting.
