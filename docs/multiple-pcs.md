# Running Ducketz on two PCs

Both PCs can use the same application code and maintain different stock lists.
Keep shared code in Git and each PC's settings and runtime data on that PC.

## Stock lists

The stock/options runtimes select their watchlist in this order:

1. `DUCKETS_PRODUCTION_WATCHLIST`, when explicitly set for a candidate run.
2. `datafetching/watchlist.local.txt`, when that file exists in this checkout.
3. `datafetching/watchlist.txt`, the shared Git-tracked default.

The local file is ignored by Git. It contains one stock ticker per line, with
optional comments beginning with `#`. It replaces the default list; its symbols
are not appended to that list. An empty or invalid local file fails validation.

To give a PC its own list without changing its current symbols, run from the
repository root:

```powershell
if (-not (Test-Path -LiteralPath 'datafetching\watchlist.local.txt')) {
    Copy-Item -LiteralPath 'datafetching\watchlist.txt' -Destination 'datafetching\watchlist.local.txt'
}
```

Edit the local file when the replacement symbols have been selected. Inspect
what a fresh process will load with:

```powershell
.\.venv\Scripts\python.exe -c "from datafetching.symbol_universe import configured_watchlist_path, read_symbols; print(configured_watchlist_path()); print(', '.join(read_symbols()))"
git check-ignore datafetching/watchlist.local.txt
```

Restart the relevant app/workers after changing the list: some runtime contracts
capture the symbols when Python imports their modules. Explicit command-line
`--watchlist` and `--symbols` arguments still take precedence. When adapting an
old schedule, remove arguments that pin it to `datafetching/watchlist.txt` or to
the old PC's tickers.

Onboarding activation and publication checks use the durable production list
(local file, otherwise shared default). A temporary candidate environment
override does not redirect activation into a candidate file.

### Selecting symbols is only the first step

Changing the watchlist does not download history, train models, publish a
Gameplan, or enable trading. The existing onboarding commands add symbols to an
existing production universe and require reference data or a published research
edition. They do not constitute a fresh replacement-universe bootstrap.

For a new PC with an empty datastore, choose the replacement stocks first, then
prepare their provider mappings, history, model training and readiness checks.
Keep that work distinct from starting scheduled production workers. Hyperliquid
uses its own market configuration and is unaffected by the stock watchlist.

## Sharing code updates

Merge the code that supports local watchlists into the shared branch once, so
both PCs understand the same configuration rules. Thereafter, develop shared
features on the original PC and pull those commits on this PC as usual.

| Item | Shared through Git? |
| --- | --- |
| Python code, launch scripts, operating procedures | Yes |
| `datafetching/watchlist.txt` fallback | Yes |
| `datafetching/watchlist.local.txt` | No; each PC owns its copy |
| `.env` credentials | No; already ignored |
| `C:\DATASTORE` history, models and runtime state | No; outside the checkout |
| Codex scheduled tasks and Windows Task Scheduler registrations | No; local scheduler configuration |

`git fetch` downloads commits without changing the working files. `git pull`
integrates tracked code changes; it normally stops if an update would overwrite
uncommitted tracked changes. Normal pulls leave the ignored local watchlist in
place. Do not use tracked-file flags such as `skip-worktree` as a substitute for
the local file. Destructive cleanup of ignored files can still remove it.

Once the shared support change is merged and the working tree is ready for an
update, `git pull --ff-only` keeps the pull workflow simple and stops when the
branches have diverged. Avoid starting a new runtime while its code is being
updated; restart affected workers after the update.

## Transferring scheduled tasks

A link such as `codex://automations/hyperliquid-paper-improvement?automationSource=local`
identifies a local scheduled task. It does not supply the task's prompt,
recurrence, model settings or local runtime state to another PC. Clone/pull also
does not register schedules on the destination computer.

Export the current definitions on the original PC, then recreate the applicable
tasks through the Codex app on this PC. Use the current definitions as the source
of truth: schedule snapshots committed to this repository are historical audits.

### Request to run in Codex on the original PC

Paste this into a chat for the original PC's Ducketz project:

> Export the current scheduled tasks associated with this Ducketz checkout for
> transfer to a second PC. Read the matching automation.toml files under
> CODEX_HOME/automations (or ~/.codex/automations) and export their full prompts,
> schedules, timezone where present, active/paused state, models, reasoning
> effort, notification preferences and project paths. Include tasks matched by
> their project path or Ducketz/Loops/Gameplan/Hyperliquid purpose; omit unrelated
> projects. Also export any Ducketz Windows Scheduled Tasks as native XML and
> record whether each is enabled. Place the export and a readable inventory in
> one ZIP outside the repository. Leave all source tasks unchanged. Exclude
> credentials, authentication files, execution history and runtime memory. The
> second PC will run its own stock universe while this PC continues operating.

Transfer that ZIP to this PC. Recreate the relevant Codex tasks with this PC's
project selected and local execution. Adapt absolute paths, old symbol lists
and machine-specific instructions. Start imported tasks paused until this PC's
data and models are ready, preserving any tasks that were already paused.

Choose task ownership deliberately:

- Data refresh, local training, Gameplan preparation and operations supervision
  can operate per PC against that PC's watchlist and datastore.
- Keep automatic source-code improvement and shared research publication on
  the original PC unless a separate development workflow is wanted. Their
  prompts need review before being duplicated on a PC that primarily pulls code.
- Date-specific follow-ups should be reviewed for relevance instead of being
  recreated as recurring production work.
- If Hyperliquid is also needed here, transfer its tasks separately and establish
  this PC's own Paper state and accepted baseline first.

For Windows Task Scheduler, import the exported XML only for tasks still needed
here. Adjust the Windows user/principal, executable and working directory, and
preserve disabled state. Do not duplicate an obsolete Windows launcher that has
been replaced by a Codex task.

Local Codex tasks require the computer to be on, the app running and the project
available. See the [official scheduled-task documentation](https://learn.chatgpt.com/docs/automations).

### Verified new-PC inventory, 2026-09-29

- No Ducketz Codex automation definitions were found in the local automation
  directory, and no matching Windows Scheduled Tasks were installed.
- `C:\DATASTORE` was empty. Cloning the project did not bring over its data,
  trained models or operational state.
- The tracked September 29 source-PC audit lists active Overnight Gameplan,
  Operations Watch, Daytime Supervision, Weekly Review and Weekly Opportunity
  Research tasks. It lists Options Strategy Paper Tracking as paused.
- Its Windows `Ducketz Independent Stock Session` task was disabled.

If both PCs will later trade through the same Schwab account, review account
ownership and broker session behavior before activating the second trader.
Different symbol lists and separate datastores do not provide cross-PC account
coordination.
