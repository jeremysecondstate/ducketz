# Completed scheduled chat cleanup

Jeremy requested recoverable cleanup of the five-minute responsibility chats
on Atlas and Scout. The one-hour clock starts at successful native turn
completion, not chat creation, dispatcher launch, or a saved workflow receipt.
An ongoing workflow can still have a successfully completed status-check chat.

Each responsibility finishes normally and saves its final response. It must
never archive itself. A separate deterministic Windows task checks for eligible
chats every five minutes. It makes no model calls and does not alter the nightly
dispatcher, workers, trading controls, or native automation schedules.

## Selection and native safety

`tools/codex_chat_cleanup.py` reads native local SQLite metadata in read-only
mode. Its private allowlist contains only the two local five-minute cron
identities. A chat must have one successfully completed original automation
turn, no native error, a final response, and at least 3,600 seconds since its
recorded completion. Manual or resumed turns, queued input, pinned or organized
chats, incomplete goals, and chats with spawned descendants are excluded.
Failures and interrupted or unfinished runs remain visible. Selection does not
interpret a final response as proof that the underlying business workflow has
completed.

The helper rechecks eligibility immediately before invoking the exact UUID:

```text
codex.exe --no-daemon archive CHAT_UUID
```

The separate native instance uses the thread writer lock. If the desktop or
another process holds that lock, native archival fails without interrupting it.
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
   `helper_sha256`, `automation_ids`, `state_dir`, `minimum_age_seconds: 3600`
   and `max_per_run: 10`. Paths, native IDs, receipts and metadata stay local.
   Use the local handoff/synthesis and reconciliation cron identities; no
   title matching or inference of a peer's IDs.
3. Run `python -I -B ABSOLUTE_HELPER --config PRIVATE_CONFIG` and inspect its
   read-only eligibility report. Unexpected schema, ambiguous state or failed
   integrity checks must prevent archival. Do not repair native databases.
4. Register with `tools/register_codex_chat_cleanup.ps1 -Pythonw PYTHONW_EXE
   -Script ABSOLUTE_HELPER -Config PRIVATE_CONFIG`. This owns only the dedicated
   `Ducketz Completed Scheduled Chat Cleanup` Windows task. It runs hidden as the
   current interactive user, every five minutes and at logon, with duplicate
   invocations suppressed and a four-minute execution limit.
5. Inspect the task's saved action, triggers and local `last.json` receipt.
   Report a zero-eligible wake as such; registration is not proof that a real
   chat has been archived. Later native readback must establish that separately.

After a CLI or native schema change, keep archival blocked until local review,
the native probes and exact binding verification pass again. The executable and
helper hashes are checked on each wake. No raw chat text or local databases are
published. To stop cleanup, disable this one Windows task; native responsibility
tasks continue returning normal final responses.
