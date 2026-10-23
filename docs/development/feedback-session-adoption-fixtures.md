# Feedback-session adoption fixture compatibility

Scout PR191, source `9de296b24d50560a4d9d94580bf60f526eb467d1` and
Completion-Record `20261016T101908Z-e5a261a4bce44d1cb8ff2fb51680fb65`, binds native
training's reported review date to its actual saved feedback Stats session.
The ten-line runtime correction and session regressions are already on common
main. Preserve that identity when installing its exact hunk into a broader
local runtime; do not replace the whole runtime or restart existing workers.

Some installations retain older supervision cases in addition to the shared
Stats-segment suite. Their placeholder feedback JSON fails the real loader
before the assertions can exercise continuation and feedback identity. The
[supervision fixture patch](patches/feedback-session-supervision-fixtures.patch)
creates genuine temporary Stats and feedback with a controlled matching date.
It preserves all 175 assertions, existing test signatures and both intentional
feedback-corruption steps. Its fixture rejects provider access.

The [direction-only adoption patch](patches/direction-only-adoption-fixtures.patch)
removes an obsolete assertion that a retired midpoint estimate must reject a
valid direction. It checks that absent, ordinary and unusable retired estimates
leave the displayed forecast unchanged, without an estimated price or quantity.
Actual quantity, suppressed direction, threshold and fabricated-fill rejection
assertions remain. This preserves the permanent direction-live-quote-v1 recipe
and live_broker_capacity execution basis; it changes no application reader.

These patches apply only to existing matching local tests. Common main already
has its relevant supervision coverage in the Stats-segment suite and does not
contain the optional adoption test. Do not add an unrelated full local test
file to main or require these fixtures for nightly operation.

Both portable patches were applied to LF-normalized original copies and
reconstructed the reviewed LF-normalized final files byte for byte. Review
individual hunks against differing local versions and preserve their other
writers and line endings. Raw local and normalized fingerprints are below.

| Test | Raw before | Raw after | LF before | LF after |
| --- | --- | --- | --- | --- |
| tests/test_overnight_supervision.py | `6d02020ee3ec131e359a83c307f46f66d1f01d1f4fa315dcbf35aac8ed5794d2` | `1649e6658982924643c29116d6d0adcc46d6cbbc5d73c9225ea6cb387449053e` | `492361c7078382d80815139f2565f8cbcc74b3a6137371cd9b34b5e8c1f7463f` | `d0e4aa898cc7657fdcf280df311c36921e39926108aa78aaee1f64a72cebf9ee` |
| tests/test_direction_only_joint_adoption.py | `d0f476c5daa528edbfe7b0630cdff361a51706c1cf022cc289c7d2a500db3644` | `6c4d9580bf2635c508e410149b0db64302fac9ab23ff58124ed6266960a490d9` | `d0f476c5daa528edbfe7b0630cdff361a51706c1cf022cc289c7d2a500db3644` | `6c4d9580bf2635c508e410149b0db64302fac9ab23ff58124ed6266960a490d9` |

Offline checks cover session binding, frozen planning-tail feedback, model
feedback, continuation, child persistence and direction-only publication and
adoption. No provider/broker call, model fitting, trader start, manual-stop
change or scheduler change is part of this source adoption. Completed data and
models, original probabilities/targets, publications and revision ancestry
remain preserved. Source review and publication do not gate nightly work.
