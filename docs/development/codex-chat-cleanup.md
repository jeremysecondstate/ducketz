# Completed scheduled chat cleanup

Jeremy requested recoverable cleanup of completed scheduled chats
on Atlas and Scout, including the updated calendar-based schedules. The
one-hour clock starts at successful native turn
completion, not chat creation, dispatcher launch, or a saved workflow receipt.
An ongoing workflow can still have a successfully completed status-check chat.

Each responsibility finishes normally and saves its final response. It must
never archive itself. A separate deterministic Windows task checks for eligible
chats every five minutes. It makes no model calls and does not alter the nightly
dispatcher, workers, trading controls, or native automation schedules.

## Selection and native safety

`tools/codex_chat_cleanup.py` reads native local SQLite metadata in read-only
mode. `automation_selection: local_cron` includes every local cron schedule,
across projects and cadences, including accepted/read results. Exact project
and automation-ID bindings remain supported for narrower installations.
Heartbeat chats and cloud runs are outside this local cleanup.

The opt-in `archive_policy: completed_or_idle` archives successfully completed
chats one hour after the latest turn finishes, including continuation turns and
all spawned subagents. For terminal failed/interrupted turns or missing final
completion metadata, `fallback_idle_seconds: 7200` requires two hours since the
latest native activity across the family. Creation age never overrides an
in-progress turn, queued input, or an unfinished goal. Pinned, organized and
previously restored chats remain visible. Ordinary manually created chats are
excluded; a continued scheduled chat becomes eligible after its latest turn.
Legacy bindings retain their single-successful-turn policy.

The family inspection is bounded to 64 chats and 64 turns per chat. Ambiguous,
partly archived or malformed trees stay visible. Chat completion is independent
of the underlying nightly workflow: cleanup never changes operating controls.

The helper rechecks eligibility immediately before invoking the exact UUID:

```text
codex.exe --no-daemon archive CHAT_UUID
```

The separate native instance uses the thread writer lock. If the desktop or
another process holds that lock, native archival fails without interrupting it.
Before an attempt, an advisory Windows check opens only existing writer-lock
files and briefly checks the same full-range lease without changing file bytes.
A held root or child lease defers the chat without recording a failed attempt;
later wakes revisit it. Completed idle subagents can retain such leases. This
preflight does not replace the native atomic guard. A definite nonzero failure
is retried only under `completed_or_idle`, after fresh eligibility checks and
confirmation that every known family member was unarchived immediately after
the failure and is still unarchived. The receipt must carry that explicit proof;
legacy failures without it need reviewed reconciliation. Each attempt
retains its previous attempt ID and append-only historical events. Unknown,
timed-out, partially archived and restored outcomes are never blindly retried.

Windows Codex can retain writer locks for idle subagents even after completion.
The standalone task defers these families until Codex releases the leases. It
cannot guarantee the one-hour deadline for those chats and never shuts down the
app or an agent to force it. A human-requested cleanup can archive already
finished families through the app's native archive tool after checking current
turns, but that tool is not a standalone Windows scheduler interface.
The helper does not use the shared daemon, `set_thread_archived`, permanent
deletion, or direct native database writes. Native archival retains the
transcript so it can be restored. Once the helper has attempted a chat, its
durable receipt prevents blind retries after uncertain outcomes and prevents
archiving it again after the user restores it.

The installed CLI must be pinned by SHA-256 and locally tested. Atlas verified
versions `0.162.0-alpha.17.2` and `0.163.0-alpha.10.2` against isolated fixtures: idle archive, active root
writer rejection, transitive descendant writer rejection before any moves,
and successful archive after releasing the locks, with legacy and paginated
metadata. These are native storage tests, not model runs. The upstream
[CLI implementation](https://github.com/openai/codex/blob/main/codex-rs/tui/src/session_archive_commands.rs)
and [archive locking implementation](https://github.com/openai/codex/blob/main/codex-rs/thread-store/src/local/archive_thread.rs)
explain the route; locally recorded exact-binary probes establish the installed
version's behavior. A pre-archive read alone is not the active-writer guard.

## Local installation

Keep the reviewed helper in a durable, versioned local directory. Preserve all
native task IDs, prompts, current memory, history, notification preferences and
their existing settings. Source publication and each PC's installation are
separate evidence stages. A peer notice supplies no private operating authority.

1. Review the published source and run its offline fixture tests. Run
   `tools/probe_codex_archive.py --codex-exe ABSOLUTE_EXE --output-root PRIVATE_DIR`
   using local Python. It creates only fresh synthetic homes, blocks network
   through local proxy settings, copies no credentials, and tests the exact
   native binary. Retain its successful proof and binary hash locally.
2. Create a private JSON binding with `codex_home`, `codex_exe`, `codex_sha256`,
   `helper_sha256`, `state_dir`, `minimum_age_seconds: 3600`, `max_per_run: 10`,
   `automation_selection: local_cron`, `archive_policy: completed_or_idle` and
   `fallback_idle_seconds: 7200`. Paths, native IDs, receipts and metadata stay
   local. For narrower scope use `automation_project_id` or `automation_ids`
   instead of `automation_selection`. Keep old receipts when upgrading.
3. Run `python -I -B ABSOLUTE_HELPER --config PRIVATE_CONFIG` and inspect its
   read-only eligibility report. Unexpected schema, ambiguous state or failed
   integrity checks must prevent archival. Do not repair native databases.
4. Register with `tools/register_codex_chat_cleanup.ps1 -Pythonw PYTHONW_EXE
   -Script ABSOLUTE_HELPER -Config PRIVATE_CONFIG`. This owns only the dedicated
   `Ducketz Completed Scheduled Chat Cleanup` Windows task. It runs hidden as the
   current interactive user, every five minutes and at logon, with duplicate
   invocations suppressed and a four-minute execution limit. Upgrading an
   existing owned task changes only its action and description, retaining its
   triggers, enabled state, principal and settings.
5. Inspect the task's saved action, triggers and local `last.json` receipt.
   Report a zero-eligible wake as such; registration is not proof that a real
   chat has been archived. Later native readback must establish that separately.

After a CLI or native schema change, keep archival blocked until local review,
the native probes and exact binding verification pass again. The executable and
helper hashes are checked on each wake. An app update can remove its previous
versioned executable directory; locate and test the current binary, then update
the local executable path and hash. Do not automatically trust the newest
executable or use its displayed version as a substitute for a byte hash.
Preserve the cleanup state directory and receipts when upgrading so restored
chats and previous attempts retain their protection. No raw chat text or local
databases are published. To stop cleanup, disable this one Windows task; native responsibility
tasks continue returning normal final responses.
