# Native recovery adoption and continuation

Scout PR194, source `b1a209e1cdaf9f8aa01ce609b2b7dfe76df4d913`, retains
Completion-Record `20261016T112553Z-f7280f53b1494d9bb9f6bc138219c2fc`.
Its recovery-path inheritance, frozen session defaults, single child route and
legacy two-deadline planning support are already present on common main.

Jeremy's standing October 13 continuation direction takes precedence over the
old handoff's added metadata checks. Historical recovery annotations do not
provide cancellation or readiness authority. This correction removes the
per-stage recovery/checksum gates and saved-exception whole-record equality
gate. Actual source, receipt and session validation at input consumption stays
in place. Two synthetic regressions confirm that reason/text annotation changes
allow all remaining stages to complete, preserve earlier attempt evidence and
place no orders. No new verification stage or approval is introduced.

Atlas retains a broader runtime with workflow recovery, account and research
roles. The [selective runtime composition](patches/native-recovery-atlas-composition.patch)
preserves those branches while restoring the advertised native retry behavior.
Explicit conflicting native/workflow inputs remain an input error; inherited
native recovery plus its saved planning-tail exception remains supported.
The original source completion identity remains the adoption identity.

The [retained recovery fixtures](patches/native-recovery-retained-fixtures.patch)
replace incomplete fake feedback/source publications with genuine temporary
Stats, feedback and checksummed inputs. The assertions still enforce the
original information cutoff and child recovery arguments. This optional local
test file is absent from the shared base; apply its precise patch only where
that existing fixture is present.

The [direction-only fixture adaptation](patches/native-recovery-direction-fixtures.patch)
applies to an installation that already implements direction-live-quote-v1 and
live_broker_capacity. It replaces the old estimated-quantity assertion with
unsized directions and exact original probability/target preservation, and
adds the mixed-route regression for the broader runtime. Do not install a
historical projected-quantity expectation into a current direction-only system.
Neither patch changes scientific model recipes or fitted outputs.

Each patch reconstructs the reviewed LF-normalized final file from the stated
LF-normalized base. Review individual hunks against differing local source and
preserve concurrent writers and local bindings. The main planner's legacy
fixture is retained here solely for that existing source contract; this change
does not generate or promote any operating plan under an obsolete recipe.

| Path | Raw before | Raw after | LF before | LF after |
| --- | --- | --- | --- | --- |
| ml/overnight_runtime.py | `5bd0763e4ba439ea3c0d1fcb85950f2ce35933826c64fe71d995b29024a22d65` | `ba218e1a07159c81cabb2d92fd0a5af07ae695a22459792f2ea0bd3f0d873651` | `7cee35bb2696d5cbb62d334b1dc0932dcbad336723f4943e21d551dc44a95504` | `9c838027719bd535ce06ef15ae227461dfbb0613d100cdca061dc78474f87a2d` |
| tests/test_nightly_recovery.py | `957c5243c94018bd1465029700fe2f28cb78fb583c6a2587f51a630aaa99e242` | `ed336dc34523ca751da81e2f7c75c4abbf5e73e8d20d2b772bcfedad358cc426` | `957c5243c94018bd1465029700fe2f28cb78fb583c6a2587f51a630aaa99e242` | `ed336dc34523ca751da81e2f7c75c4abbf5e73e8d20d2b772bcfedad358cc426` |
| tests/test_native_recovery_spec.py | `16f5ac1ce9800c0ddaaac2833e49a62629acc4d4e864419ae15762143ba7a3c8` | `8a05c67279b30790bcdbd731077c52496f9fe702241486f13e0d768f92c7d9d2` | `17b0aec9a2ddaf10cbee9a4104274e72462b443f35db6ecc250be6d59c264405` | `7ac74ade053566932e1b838e0d7fe0282e33586cbf7413a88c3fb56ea8da88b8` |

These are offline source and test changes. Production continues independently;
no provider/broker request, model training, trader start/restart, manual-stop
change or native schedule change is part of adoption. Local installation and
loaded-process behavior remain separate facts. Completed data, original
probabilities, targets, publications and revision ancestry remain intact.
