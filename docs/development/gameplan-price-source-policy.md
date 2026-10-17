# Dated Gameplan price-source migration

`ml.gameplan_price_policy.resolve_gameplan_price_source` selects a source contract
for an exact action date. A local configuration may retain its existing
`stock_price_source` and add `stock_price_source_policy` with three exact fields:
`version: gameplan-price-source-effective-date-v1`, `effective_from_action_date`
(ISO date), and `source_contract` (one of the supported explicit source contracts).

The workflow must use the resolved source consistently for configuration binding,
acquisition-stage selection, native pipeline arguments and new publication. Runs
before the effective date retain the baseline source. Existing saved states,
completed outputs and target definitions are never rewritten by this helper.
Invalid policy schemas and dates fail closed even for earlier sessions.

Select an effective action date after already completed workflow states when
migrating a live installation. An explicitly requested reassessment of an earlier
date is a separate immutable model/data revision, not a rewritten completion.
Keep original state and configuration backups as local evidence. Use each PC's
own watchlist and saved model configuration; this source policy conveys no
authority to import peer tuning or private data.

The observed v2 source contract retains XNAS archive features and tries genuine
same-source target pairs in the frozen XNAS, EQUS, Schwab minute, Schwab five-minute,
then FMP five-minute order. Acquiring a successful response is not complete market
coverage. Unsupported endpoints remain missing; no tolerance, quality or model
qualification gate is relaxed. The recurring workflow must retain XNAS history
maintenance and opt into both Schwab history and FMP history acquisition.
