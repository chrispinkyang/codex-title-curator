# Private state and recovery

Python 3.9+ is required; there are no third-party runtime dependencies. The helper reads the local Codex metadata index and writes only its own state. It does not rename tasks, call models, send network requests, or read credentials.

Defaults:

- Codex data: `$CODEX_HOME`, or `~/.codex` when unset.
- Private state: `<Codex data>/title-curator/state.json`.
- Metadata: highest numbered existing `state_*.sqlite`, opened with `mode=ro` and `PRAGMA query_only=ON`.

Use `--codex-home`, `--state`, or `--db` before a subcommand to override paths. Required columns are checked; unsupported schemas fail clearly. The index covers only the selected local host and cannot establish live task status.

## Inventory

```sh
python3 scripts/title_maintenance.py scan --days 30
```

Output includes candidates, externally edited titles, and counts. No transcript or first-user message is returned. Archived rows, identifiable child agents/scheduled runs, `excluded_titles`, and `maintenance_thread_id` are excluded. Missing scheduler metadata cannot identify every automated run; review remaining candidates with app tools.

Stored title fields can themselves contain text from the user's opening message. Treat all inventory output as private even though the helper does not load conversation history.

Unchanged complete checkpoints are skipped; `partial` records remain candidates. External title changes are preserved conservatively, including changes made by another automation.

## Checkpoint after verification

Before renaming, save a private audit record with ID, old/new titles, observed `updated_at`, and time. After the app tool and readback, append the outcome. Store this outside the checkout with owner-only permissions.

Prepare a private JSON array for verified changes or reviewed-and-kept titles:

```json
[
  {
    "id": "example-task-id",
    "title": "Diagnose checkout request timeouts",
    "observed_updated_at": 1700000000,
    "review_status": "complete"
  }
]
```

These values are fictional. Use the timestamp observed **when content was reviewed**, not a newer timestamp fetched merely to pass validation.

```sh
python3 scripts/title_maintenance.py checkpoint --entries /path/to/private/verified.json
```

Every title is checked against current metadata before state is saved. A title mismatch rejects the batch. If `updated_at` advanced, the checkpoint becomes `partial` and needs another review. This detects a race; it does not make renaming and checkpointing atomic. Keep batches small and check live status before mutation.

Record an unreadable task with its current title and `review_status: "partial"`, or leave it without a checkpoint. Do not mark it complete from its old title alone. Do not overwrite checkpoints for externally edited titles; skipping preserves the choice.

When no failed or deferred work remains:

```sh
python3 scripts/title_maintenance.py complete
```

`complete` rescans and refuses to finish while candidates remain. The agent must also ensure no work is deferred outside this local index. State writes are atomic with owner-only permissions. Run one curator per state file; concurrent writers are unsupported.

Optional private state settings:

```json
{
  "version": 1,
  "maintenance_thread_id": "example-maintenance-task",
  "excluded_titles": ["Example health check"],
  "threads": {}
}
```

## Recovery

- Read failure: preserve title and retain a partial or missing checkpoint.
- Write failure: keep the pre-write audit; reread live title before retrying.
- User activity: checkpoint completed work and stop without `complete`.
- Rollback request: use the private audit and normal app title tool. If a later edit changed the title, preserve it until the user resolves the conflict.
- Never restore or modify an app-owned database to undo titles.

Runtime state and audit records contain private task information. Do not commit or upload them.
