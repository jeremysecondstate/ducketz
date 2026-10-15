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
mode. Its private binding selects an exact native project UUID, discovering
that project's local cron definitions on each wake. Existing exact automation
ID allowlists remain supported. Cadence, task display names and read/unread
state do not determine eligibility. Newly created local schedules in the bound
project are included automatically; heartbeat chats, cloud schedules and other
projects are excluded. Pausing a definition does not hide its completed history.
A chat must have one successfully completed original automation
turn, no native error, a final response, and at least 3,600 seconds since its
recorded completion. Manual or resumed turns, queued input, pinned or organized
chats and incomplete goals are excluded. Spawned subagents are checked
transitively: every turn must have succeeded with a final response and be at
least one hour old. Manual, pinned, organized, archived, queued, unfinished,
failed or interrupted descendants block the whole tree. The proof is bounded
to 64 chats and 64 turns per child; larger or cyclic trees remain visible.
Failures and interrupted or unfinished runs remain visible. Selection does not
interpret a final response as proof that the underlying business workflow has
completed.

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
preflight does not replace the native atomic guard. Uncertain prior failures
still require reviewed reconciliation rather than blind retries.
The helper does not use the shared daemon, `set_thread_archived`, permanent
deletion, or direct native database writes. Native archival retains the
transcript so it can be restored. Once the helper has attempted a chat, its
durable receipt prevents blind retries after uncertain outcomes and prevents
archiving it again after the user restores it.

The installed CLI must be pinned by SHA-256 and locally tested. Atlas verified
version `0.162.0-alpha.17.2` against isolated fixtures: idle archive, active root
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
   `helper_sha256`, `automation_project_id`, `state_dir`, `minimum_age_seconds: 3600`
   and `max_per_run: 10`. Paths, native IDs, receipts and metadata stay local.
   Bind the exact local project identity from native metadata; no title matching
   or inference of a peer's IDs. Alternatively, supply `automation_ids` for a
   bounded exact ID allowlist. Opening a scheduled result does not protect it
   from cleanup; manually continuing, pinning or organizing it does.
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
