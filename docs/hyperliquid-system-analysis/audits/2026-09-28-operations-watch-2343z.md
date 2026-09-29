# Hyperliquid operations watch — 2026-09-28 23:43 UTC

Contract **2026-09-28/v13** is unchanged from the accepted handoff provenance. The maintenance marker was completed at first read, so this pass remained observational and made no recovery or ledger changes.

The accepted experiment is 20260928-paper-5m-01, seeded at 2026-09-28T23:17:05.472032070Z with opening equity **43,937.0212871266**. The current _paper/experiment.json matches the accepted receipt. The referenced final health report hash matches the receipt. The current seed, opening snapshot, and opening cycle match their accepted immutable hashes.

At 2026-09-28T23:43:47.558226Z, the read-only strict slot check passed for BTC, ETH, HYPE, and ZEC at **5m/h1**, with forecasts targeting the 23:45Z close. BTC, ETH, and ZEC were Qualified; HYPE was Research and remains ineligible for signal entries under the qualified-only Paper policy. Data and forecasts were fresh, the Paper lifecycle was trading, and the committed valuation advanced to 23:43:18.460225Z. No runtime or slot errors, warnings, or stop requests were present. The four current market/model/Paper/Powder configuration hashes match the accepted handoff.

Saved owner PIDs were verified against exact module, config path, project cwd, process creation time, and .venv launcher ancestry: coordinator **36524** (launcher **77772**), models **75320** (launcher **56136**), and Paper **76188** (launcher **58324**). Each is one verified runtime chain. No Powder runtime was found; Powder is disconnected with zero pending intents, and its missing observation ledger is expected while off.

**Evidence discrepancy:** the accepted canonical SQLite initial_positions hash is 79ac73dc1f0dd683b3a26b0c3f73fd4656f346f66cd2eeaf8fc1f706d454f928; the current canonical row hash is 18679b862588e064c4a0024250dc14de83772dede64cace14534551fa5eae1d3. The current table has nine rows, and each row's account/market keys, quantity, entry, mark, timestamp, and decoded details match the accepted seed. The seed, opening snapshot, and opening cycle hashes match. The reason for the row-table hash difference remains unresolved.

No recovery, reseed, or other runtime action was attempted. Before any future ledger recovery or reset, the Paper Improvement owner should reconcile the accepted opening receipt against the current SQLite row serialization. The discrepancy does not authorize initialization or alteration of the accepted ledger.
