# September 27 evening bounded prior-publication check

Checked at 2026-09-28T04:10:28.915034+00:00. Result: PASS_WITH_EXISTING_COVERAGE_NOTES, 95 checks passed, no errors.

Friday-source overnight `20260926T040723.834511Z` remains COMPLETE, with all eight stages, original Monday September 28 04:00 Pacific deadline, archive-history/XNAS/raw-direction contracts and zero orders. Current Monday Gameplan `20260926T061604.318956Z`, trade plan `20260926T062723.283372Z`, and Friday actuals review `20260926T063044.178275Z` still match their native pinned sources.

Rehashed 33 manifest-bound output files across Gameplan, trade plan, actuals, sizing and current evaluation; nine prior full-audit evidence files; native stage report and eight logs; pointer-to-receipt and receipt-to-manifest bindings; and six sizing input bindings. All hashes and applicable byte sizes match. Current four pointers and watchlist remain byte-identical to the September 26 evening no-op evidence.

Forecasts, stock-only intents and augmented trade-plan rows each contain 264 rows, exactly 24 for each configured symbol. All four directional groups and all 264 forecast rows retain PROMOTED status under saved v2 tolerances. Only 4h and 1d beat both baselines. All four sizing groups remain FITTED with zero qualified scopes. No retraining or relabeling is warranted by this unchanged evidence.

Friday actuals remain 160 evaluated, 38 mature awaiting data and 66 pending maturity; 137 prices compared and 17 missing. Latest cumulative evaluation is the already-existing Saturday read-only generation `20260926T160907.135417Z`: 5,568 forecasts, 4,245 evaluated, 894 mature missing and 429 pending, including Monday's 264 pending rows. This is unchanged since the prior no-op wake. The older native completion summary's 5,304-row count described its earlier prepublication refresh.

This bounded check reused the unchanged original deep-audit evidence. It did not replay archive records, reconstruct cohorts, run inference, fetch provider/broker data, acquire supervision, launch/stop pipelines, or change code, schedules, controls, ownership or production artifacts. Root handled the current native calendar no-op and supervision.

Evidence: `bounded-verification.json`; reproducible read-only helper: `bounded_audit.py` in this directory. The initial audit-only date comparison was corrected to normalize a Parquet Python date to ISO text; the final helper exits zero.
