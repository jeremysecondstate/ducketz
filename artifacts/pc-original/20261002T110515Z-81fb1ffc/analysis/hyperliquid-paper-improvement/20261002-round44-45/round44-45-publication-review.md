# Atlas Artifact Publication Review — Round 44/45

Actor: Atlas. Producer: hyperliquid-paper-improvement. Scope: machine-local `pc-original` operating artifacts; no symbol-specific source or shared application source changed.

Original artifact commit: `daa87b5a7ee84ef773182773dd69a4466cfe974f` (base `7168c02746799eb2710ef098c129dd59448c3fa0`); remote main readback `daa87b5a7ee84ef773182773dd69a4466cfe974f`. The commit added 25 reviewed operating records under `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/`.

## Practical changes

- Added the Round 44 late comparison, cadence assessment, closeout audit, archived Paper SQLite ledger and associated fills, decisions, performance and runtime logs.
- Added the Round 45 fresh opening, first committed-cycle verification and latest read-only health summary.
- The original DataStore outputs remain in place. These are Atlas machine-local records; Scout must not use them to replace its own live state.

## Checks and times (UTC)

- native closing comparison: passed/collected at 2026-10-02T10:13:34.527913+00:00.
- cadence assessment: passed/collected at 2026-10-02T10:14:17.574469+00:00.
- archive preservation and SQLite integrity: passed/collected at 2026-10-02T10:18:54.771362+00:00.
- opening verification: passed/collected at 2026-10-02T10:22:27.574827+00:00.
- first committed cycle verification: passed/collected at 2026-10-02T10:25:34.818400+00:00.
- latest read-only trading health: passed/collected at 2026-10-02T10:51:48.673440+00:00.
- artifact snapshot and .env-value scan: passed/collected at 2026-10-02T11:02:13.065521Z.
- artifact push and remote SHA readback: passed/collected at 2026-10-02T11:02:52.126397Z.

- The artifact publisher verified all 25 source hashes and scanned candidate bytes against local `.env` private values before pushing; no matching value was reported.
- No software tests ran because no source or shared configuration changed. The native comparison was assessed `late_unscored`; its negative edge is descriptive and did not rank as a loss.

## Limitations and runtime implications

- Round 44 observation was 4,404.191666 seconds past due, beyond the five-minute allowance. Comparative external-flow and common-mark gates passed; timing makes the result UNSCORED.
- Round 45 is still the active one-hour Paper observation. Latest health passed with Paper, models and the data coordinator running; Powder remains inactive. This artifact publication changes no process, model, account, provider, real execution, trading or schedule authority.
- The artifacts document Atlas local state only. They do not establish Scout adoption, peer runtime state or deployment of any code/configuration.

## Exact files and original hashes

| Operation | Source path | Original SHA-256 | Bytes | Public main path |
| --- | --- | --- | ---: | --- |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-audit.md` | `03e4a2cb43917e3a74f539af8be46890a02e633c6fa9235dcefef60febe18686` | 4898 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-audit.md` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-native-comparison.json` | `03a4805882c73f8cfff2de79f9062ffd2c4ac828a5be9601d253fd2cd23b3f8c` | 1892172 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-native-comparison.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-cadence-assessment.json` | `ebc8b60afdf64bd84b1fe9efefff27650f664e5f60fdc40bfea8a8b02447573a` | 2664 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-cadence-assessment.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-poststop-closeout.json` | `4f532554abfa028a5b6fdb80a0e55b8cba6414f49e3e836cc3cb01c6b41d403b` | 78008 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-poststop-closeout.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-ledger.sqlite3` | `a846dd480bc53452522d857bca07d709c27f107da4ff3fede2370f6772f69e5d` | 9592832 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-ledger.sqlite3` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-ledger.sqlite3-shm` | `fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb` | 32768 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-ledger.sqlite3-shm` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-ledger.sqlite3-wal` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | 0 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-ledger.sqlite3-wal` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-fills.parquet` | `cc94f0d2a9261954ab5220986428d78cf991a05d73fd9b8637c2bedfdcb83a6d` | 122567 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-fills.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-decisions.parquet` | `20bf1370b25f2213afb345dd87fb59ddfb58d48c4fcee8b02d07ccbfa2084841` | 275657 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-decisions.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-equity.parquet` | `e73ab63bc21d7b5ec6215956fda4de352087c4bd13d60495408830486d56503d` | 81168 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-equity.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-events.parquet` | `d453608c0a24e2215e0717d3b07f61fac99cc9a5b60058d3dfc143c4e9f2488f` | 351567 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-events.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-funding.parquet` | `1dfcda6e883c8cd9e3bcbf1f84e4f1979fc87777b50fffd54ebb86cd86dfce36` | 9499 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-funding.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-initial-positions.parquet` | `a343ad550e607a4051ba27d51b4c561123b3eb4fe4fe2288a76a9ad93e7eafda` | 7657 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-initial-positions.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-transfers.parquet` | `e3a72ad968add80387da2e21356c5506013dbf55e8fd36970ff8b97c8bc52f24` | 4040 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-transfers.parquet` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-experiment.json` | `c2987dca68f286107c8a7169b47f371fedc30846f81325246e7fe1d6bb91aaee` | 9384 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-experiment.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-performance.json` | `50277e8c182befee1909762c0bcd105687ef7400ddb072ec40dae7343b9094fe` | 16084 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-performance.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-opening-snapshot.json` | `654cd06961ab667dfdb6adde428091c3630b3f1d314a9ec7af5b89da4179a370` | 11077 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-opening-snapshot.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-policy.json` | `853308b6e56f515e882ecb0db09264d7f3e66ae03d1be2c14302cab5a07599b1` | 1188 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-archived-paper-policy.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-paper-runtime.stdout.log` | `0fa4bff66b5e907846562224578aace6f1330bd9ae0730d4b60ba575544f90eb` | 15835 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-paper-runtime.stdout.log` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-model-runtime.stdout.log` | `f14354b34304e904b6a2242d950af4e5f906f9b01ac5fa2f2149a52538748964` | 134090 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round44-model-runtime.stdout.log` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-public-summary.json` | `3aeb69701e634cd486b5ba22420bba2a0012bd8a54d97fcc8e31aa934a44e4dc` | 2281 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-public-summary.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-opening-verification.json` | `cfa81f0e86ec388a68fad47b508495b55762def23627e596cf0f261e162269c1` | 23854 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-opening-verification.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-opening-public-account-reads.json` | `fa1deb303d32038789d46da091cf0c93f1d14ce583889464f3c772dc8069e91e` | 12730 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-opening-public-account-reads.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-first-cycle-verification.json` | `8d31b0f68970e4b09ff34e806b5ac94191a770db44c870baebbdad8d5ad186f6` | 31922 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-first-cycle-verification.json` |
| add | `artifacts/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-latest-trading-health.json` | `879d90186a7e7066ba1b8496d799027cbb3a839cd2d8fb543e61ec7c24f793ab` | 31765 | `artifacts/pc-original/20261002T110058Z-673c7823/analysis/hyperliquid-paper-improvement/20261002-round44-45/round45-latest-trading-health.json` |
