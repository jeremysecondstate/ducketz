# Nightly Codex CLI discovery after desktop updates

A desktop update can remove the versioned executable recorded by a local nightly
workflow while an installed `codex` remains discoverable on PATH. The resolver
`ml/codex_cli.py` keeps an existing configured file first, then uses an existing
file returned by `shutil.which("codex")`. It returns `None` when neither exists.
Resolution launches nothing and does not change local configuration.

The reviewed common main `95fcaa56f01854446cae3bf0237e3efcae719e06` has no
`ml/nightly_workflow.py` consumer or `tests/test_nightly_workflow.py` suite. Each
PC's existing workflow can contain additional owned composition. Publish the
portable resolver, its stdlib tests and this exact minimal consumer patch;
preserve those other source changes rather than publishing a full local module.

## Exact consumer change

The patch at `docs/development/patches/nightly-codex-cli-fallback.patch` adds one
resolver import and changes only the executable lookup in `run_reviewer` and
`--check`. Both calls pass `which=shutil.which`, retaining existing caller lookup
injection. The saved reviewed-proposal reuse branch still precedes CLI resolution
and inference. CLI arguments, selected reviewer model, timeout and subprocess
controls are unchanged.

Reference consumer SHA-256 values:

- Before: `2a929332084ccc7852a984465a97a00ee76ea23398a439418b8de9d261df7bbf`
- After: `3a76fecd8a228541b8df28223e11b2380735a3c10fb495ed09572c34a89157d0`
- Patch: `2b40371994070dc946e8caa27ae0624530e5627ff6e8de2e7ecac18633072a17`

## Local adoption

1. Retain the local consumer's exact preimage and its owned-source context. Install
   the portable `ml/codex_cli.py` helper through the existing source-adoption flow.
2. For the reference before hash, apply the three-hunk patch and confirm the after
   hash. The patch preserves that consumer's CRLF bytes. For a different local
   composition, review and compose only the import and two lookup expressions;
   retain its existing behavior and record its own before/after hashes.
3. Run `python -B -m unittest discover -s tests -p test_codex_cli.py` against the
   installed helper. Run the relevant local workflow tests with inference mocked,
   covering stale-path fallback, `--check`, unavailable CLI, and saved-review reuse.
4. Resume the existing unfinished nightly responsibility through its existing
   dispatcher. Keep its run identity, original config, saved diagnostics, completed
   data/Stats and historical error receipts. No configuration or state rewrite is
   needed for executable discovery.

This change adds no nightly approval, expiry, source-publication wait, peer
acceptance or trader dependency. Source publication and local adoption remain
separate facts. Preparation preserves manual trader controls and never starts,
restarts or trades through a trader. The resolver's tests create inert files and
perform no network, provider, broker or model calls.
