# XNAS.BASIC native zero-volume observations

Read-only check, September 13, 2026 UTC. No new provider API request or production
change was made. This note concerns the isolated 192-row COST feasibility probe,
not the frozen XNAS.ITCH publications.

## Documented facts and remaining uncertainty

Databento documents OHLCV as aggregates of trade prices and volume, timestamps at
the start of each interval, and no record for intervals without trades. The volume
field is an unsigned integer described as total traded volume. Its best-practices
note cautions that aggregation can differ in trade-condition handling, breaks,
timestamps, and publication timing. The page does **not** explain why an
XNAS.BASIC bar can have a populated changing price but zero volume.
[Official OHLCV specification](https://databento.com/docs/schemas-and-data-formats/ohlcv).

The dataset-specific documentation says XNAS.BASIC trade records are normalized
from NLS+ trade reports, while quotes come from QBBO. This establishes documented
feed provenance, not the exact cause of the zero-volume aggregate rows in this
probe. [Official dataset documentation](https://databento.com/docs/venues-and-datasets#nasdaq-basic-with-nls-plus).

The bounded documentation search did not establish dataset-specific zero-volume
semantics. We therefore cannot infer that each such row proves a newly executed
positive-size trade, or that it is a synthetic carry-forward. The provider-native
DBN establishes that our application did not create those rows.

## Preserved probe evidence

Decoding the three original DBNs gives:

| Probe | Rows | Positive volume | Zero volume |
|---|---:|---:|---:|
| September 11 premarket | 35 | 28 | 7 |
| September 11 afterhours | 140 | 106 | 34 |
| September 10 prior close | 17 | 15 | 2 |
| Total | 192 | 149 | 43 |

The September 11 15:00 Pacific native bar has open/high 904.82, low/close 904.66,
and volume 0. Its changing OHLC values do not support describing this specific bar
as a flat copy of one preceding price. They do not identify its underlying trade
condition or establish a normalized share quantity.

## Positive-volume sensitivity

This sensitivity uses a separate in-memory filter, preserves raw files, and is
**not** a new production eligibility rule.

| September 11 entry clock | Original next bar | First positive-volume bar | Delay | Pass five-minute rule |
|---|---|---|---:|---|
| 14:00 | 14:01, volume 1, open 904.82 | Same | 1 min | Yes |
| 15:00 | 15:00, volume 0, open 904.82 | 15:01, volume 1, open 904.90 | 1 min | Yes |
| 16:00 | 16:00, volume 1, open 904.82 | Same | 0 min | Yes |

All six closing boundaries in the original probe also remain within the
five-minute limit using positive-volume rows. The selected close observations and
prices are unchanged. Thus the specific September 11 boundary improvement does
not depend on accepting any zero-volume bar. This does not establish every
120-session boundary or resolve source semantics for all prospective model labels.

The longer candidate audit was asked to report positive-volume-only coverage as
an independent sensitivity. A future source policy must state its source and
observation semantics explicitly; the isolated probe does not authorize replacing
historical XNAS.ITCH outcomes or altering native volume values.

Evidence: the unchanged `source-probe/*/provider.dbn` files, their checksum-bound
receipts, and `native-zero-volume-sensitivity.json` in this analysis directory.
