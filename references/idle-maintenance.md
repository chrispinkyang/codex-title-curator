# Quiet maintenance during idle time

Use only after the user requests recurring maintenance. Reuse a matching automation instead of creating a duplicate.

Suggested defaults, which the user may change: check hourly, finish at most once per local calendar day, and wait for 15 minutes without keyboard/mouse input.

Use the app's automation tool, preferably attached to the current task. Do not install a cron job, login item, or global hook as an implicit fallback. If scheduling is unavailable, keep the manual workflow and explain the limitation.

Resolve the installed skill directory and private state path locally, then insert those paths into the user's automation prompt. Do not publish that rendered prompt. Honor the user's timezone; the helper defaults to the operating system's local date and supports `--timezone` with an IANA name.

The first action on each wakeup is:

```sh
python3 scripts/title_maintenance.py gate
```

Run from the skill directory or use its absolute path. `completed_today` or `user_active` means end quietly without scanning tasks. A check error means skip title writes and report a new actionable failure according to the notification preference.

`gate` uses macOS `HIDIdleTime`. This measures keyboard/mouse inactivity, not low CPU load, an empty work queue, or whether the user is reading. Other operating systems support manual mode; automatic idle checks fail closed until a trusted adapter exists. Never simulate an idle result in production.

After eligibility passes, inventory tasks and inspect each target's live status. Skip running tasks even if the user is away. Before each small write batch, recheck:

```sh
python3 scripts/title_maintenance.py gate --idle-only
```

If activity resumes, checkpoint completed work and stop. Leave the day incomplete so a later wakeup resumes pending work. Running or unreadable targets remain pending. When no failed or deferred work remains, use `complete` from [state-and-recovery.md](state-and-recovery.md). A completed review with no changes can also finish the day.

## Automation prompt template

Adapt after resolving paths, scope, timezone, cadence, and notification choices:

> Use $codex-title-curator to maintain titles of local, unarchived main Codex tasks active within the last 30 days. Run the installed helper's gate command first. End quietly if today's review is complete or the user is active. Otherwise read the skill, review new or changed task content, and update only titles that materially improve recognition. Keep explicit and externally edited titles. Skip this maintenance task, running tasks, child agents, and scheduled-run noise. Check idle status before each write batch; checkpoint progress and stop when activity resumes. Rename only through the app tool and verify each title. Store audit and checkpoint data privately, outside the skill repository. Mark the local day complete only when no work is deferred. Stay quiet on success or no changes; report only new failures or necessary user action.

Set private state's `maintenance_thread_id` to exclude this task's recurring wakeups. Preserve existing automation fields when updating one.

Hourly checks still wake the agent and may consume usage. This is scheduled maintenance with an idle gate, not a native idle-event trigger. Local scheduled work needs the computer and app to be available. Source: [official scheduled-task documentation](https://developers.openai.com/codex/app/automations).
