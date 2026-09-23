# Trading monitors enabled with lower usage settings

The user requested re-enabling paused trading monitoring and lowering model or reasoning settings on frequent tasks. Updated the existing automations through the app's automation tool; no duplicate schedules were created.

| Automation | Status | Model / reasoning | Schedule and scope |
| --- | --- | --- | --- |
| Loops Operations Watch | ACTIVE | GPT-5.6 Luna / low | Existing 90-minute recurrence; compact status checks, incident work only when justified |
| Loops Stock Trader — Daytime Supervision | ACTIVE | GPT-5.6 Luna / low | Existing weekday 03:55 Pacific recurrence; bounded pre-opening check |

The optional choice between brief checks and all-day supervision received no answer before implementation. After allowing time to reply, the lower-usage brief-check approach was explicitly stated and applied. The native trader continues its own continuous session; the AI daytime task no longer polls every minute until 17:00.

Prompts now refer to current operating contracts and read only recent relevant memory/status/logs on routine checks. Removed obsolete duplicate instructions about completed onboarding, expired September 14 exceptions, legacy fixed sizing and forced expiry sales. Kept current manual Gameplan policy, exact ownership, YG source selection, broker/quote/ledger safeguards and incident supervision requirements. This does not authorize another live session or enable the disabled legacy Windows launcher.

Combined prompt length decreased from 24,515 to 9,301 characters (about 62%). This is a context-size reduction, not a measured percentage saving in account usage. Both monitors moved from Terra / medium to Luna / low. OpenAI's current model guidance recommends Luna for clear repeatable tasks and low reasoning for quick, well-scoped work: https://learn.chatgpt.com/docs/models.

Preserved automation IDs, names (including the existing stored daytime-name encoding), recurrences, project, working directories, execution environment, notification settings and creation timestamps. Verification passed against saved before/after TOML snapshots. Only status, model, reasoning, prompt and update timestamp changed.

The 13:05 transition, legacy options paper task and expired September 9 review remain paused. The overnight task remains ACTIVE at 21:05 with its existing Astra / ultra settings; the weekly tasks are unchanged. The Windows Independent Stock Session task remains disabled. The live manual trader was not restarted or modified; a read-only check at 08:48 Pacific showed RUNNING with CURRENT broker state and zero failed cycles.

Evidence: before/after TOML files and verification.json in this directory.
