# Delegated model-fit diagnostic regression

Scout PR131's delegated-fit diagnostic behavior is already present in Atlas's
independent readiness implementation. This handoff retains the missing regression
without replacing runtime source, local model policy or manual trader controls.

Original source: `e57d0826b7a696ff305976efa9e11b59e4b29452`.
Original Completion-Record: `20261014T105516Z-f359f17277e24f478d10994bea01ab07`.
Original notice: `20261014T105735Z-2574d99d17b24287b5ff9631a4da3474`.

The [portable patch](patches/intraday-readiness-model-delegate.patch) adds
`tests/test_intraday_readiness_model_delegate.py`. Its exact SHA-256 is
`8239f560519b212cc6ff791877a2f1db8e4e7c371b759c2445f45c3b3b62c703`. It copies public source into a temporary directory and first verifies
the actual delegated helper. Replacing that helper with a string and `pass` must
make the forecasting diagnostic unverified, with no required calls present.
The test never imports or fits model estimators, reads operating data, connects
to providers or brokers, starts workers, or places orders.

The publication base `078e07390864efc1f94ba4d9f34f2180e80e9d7a` does not contain
`ml/intraday/readiness.py`. Consequently the regression is supplied as an exact
composition patch for existing installations of that module. Do not install an
older readiness module or its dependency chain just to run this test. Review the
current local implementation, then apply the patch and run the local suites.

Atlas retains readiness v3, both actors' own `intraday/execution` bindings,
the newer `fit_chronological` wiring check, and order-cutoff-independent manual
preparation. PR131's old Gameplan compatibility probe, shared-capacity assumptions
and separate Scout prestart gate are superseded by independent local execution.
No cross-trader dependency or operational verification stage is added.

Installed offline checks on October 25, 2026 passed **68 tests**, with no skips,
warnings or network/import violations; the strict harness bound 26 imported
repository sources. The selected suites were `test_intraday_readiness.py`,
`test_intraday_readiness_model_delegate.py` and `test_intraday_local_execution.py`.
This is installed-source evidence, not a claim about code loaded by a process.
The exact patch also reconstructs the tested file byte for byte. Main publication
checks cover this document's hygiene; the runtime regression is tested against
the applicable local source, not against an absent module on main.

Nightly production continues independently. The direction-only Gameplan recipe,
original forecasts, model selections, publication ancestry and all manual stops
remain unchanged. Each trader still requires Jeremy's manual start.
