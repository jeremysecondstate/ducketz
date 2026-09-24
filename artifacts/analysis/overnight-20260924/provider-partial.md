# Partial provider evidence

Native run 20260924T040733.615371Z is CANCELLED in loop_a_close_fetch after 22 Schwab captures reported invalid_grant. This is a blocking authorization failure. The root supervisor requested controlled stop; this audit made no provider requests or process changes. Original September 24 04:00 Pacific deadline and archive_history policy remain saved.

The Databento watchlist stage completed with status=ok. Fresh continuous ES/NQ/RTY/CL/GC and raw ESZ6/NQZ6/CLX6/GCZ6 are present in all six OHLCV/BBO/MBP capture scopes. The optional derived CME context remains excluded by source/staleness/MBP saturation gates; see cme-derived-diagnosis.md.

Production OPRA has 0/33 cursors through exclusive 2026-09-24. All 33 remain through exclusive 2026-09-23; tonight's OPRA scope completion logs are absent. Full provider completion verification was not run and no current partition data files were hashed. No downstream training started and no Gameplan was published. Retained cursors are preserved; no completion is claimed for this stopped source session.

Resume requires fresh authorization evidence and the original native attempt. Detailed stop state, exact capture failures, provider progress and cursor dates are in provider-partial.json. No broker calls, retries, configuration changes, code repairs, history fabrication or gate changes occurred.
