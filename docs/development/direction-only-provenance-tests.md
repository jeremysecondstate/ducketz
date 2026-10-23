# Direction-only provenance regression maintenance

Scout PR177 source `d938d647c026e812956abce1c590eda4bb836638`, Completion-Record `20261016T061608Z-b9c226dde4514f059e17f96eac1a07d5`, preserves late-publication context before package hashing. Atlas already carries that behavior and the newer direction-only recipe. Keep the original source and completion ancestry.

The late round-trip regression now reconstructs packages with optional historical price data. Current packages require neither a price artifact nor its hash. Both future-input and publication-after-package rejection assertions still run.

The current-plan regression accepts inert retired projection metadata without changing the original forecast or its direction. New output still strips that metadata. The narrow shared validator change removes only the obsolete key-presence rejection; actual quantity, ledger, probability and policy checks remain in place. Atlas already has this behavior, so its local application module is preserved.

## Existing local handoff regression

`tests/test_direction_only_joint_handoff.py` and its nightly readiness dependency are absent from publication base `49440c22f10e093f3f45798ed92ce29be0426d6b`. Apply the adjacent `patches/direction-only-provenance-tests.patch` only to an existing installation with those components, reviewing surrounding differences. The patch corrects the fixture expectation: a current contribution made from historical scientific inputs remains valid after an unused estimated-price file changes or disappears. Original forecast-tamper rejection remains tested. Do not recreate absent components from this patch.

The test source SHA-256 before the patch is `b51822ed3a6bb43477d5d52b2ec5d02a86814505d80dd35d87c78150e4a8d923` and after is `db6fa8702cbe4d2c0bd539a112e8332c074c65344d63cfe116635659d2250f9e`. The patch uses LF; retain the installation's existing line endings when applying its reviewed hunk. This changes synthetic tests only and performs no operating verification, starts or account/provider requests.
