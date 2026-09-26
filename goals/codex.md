# Codex's goals: live updates, analytics and demo

Read [`GOALS.md`](GOALS.md) first. It has the rules, file ownership and
Atlas facts.

Your files: `evolution/policy.py`, `evolution/feed.py` (new),
`evolution/analytics.py` (new), `dashboard/app.py`, `scripts/atlas_demo.py`
(new), and your tests.

Start with X1. It does not depend on Claude. X2 and X3 need Claude's task
C1. **C1 is done**: `run_metrics` and `agent_state` exist in Atlas. Check
`GOALS.md` → **Contract** for their shapes. Create them in your
`rcu_test_codex` database with `ensure_schema(db)` from `schema.py`.

The learning layer (`learning.py`) now writes `episodes`, `sites` and
`skills` with `lift`. X3 and X4 can show those too: the lift per skill and
retired skills make a good dashboard panel.

## X1. Atomic policy decisions (~30 min)

- [x] In `PolicyRepository.record_evaluation`, write the evaluation and
      update the policy status in one transaction. Use a session when the
      collection supports it. Fall back to the current two writes for test
      doubles and standalone MongoDB.
- [x] Guard the status change: update only if `status` is still
      `candidate`. If no document matched, abort. Another agent decided
      first.
- [x] In `PolicyRepository.save`, catch `DuplicateKeyError` on
      (`task_key`, `version`). Two agents proposed the same version. Return
      a clear result instead of crashing.
- [x] Test against `rcu_test_codex`: an aborted transaction leaves no
      evaluation and no status change.

## X2. Live policy feed with change streams (~45 min)

- [x] New `evolution/feed.py`: `PolicyFeed(db, agent_id)`.
      - `watch(task_key, on_policy)` opens a change stream on `policies`
        filtered to `status: "accepted"` for that `task_key`.
      - After each event, save the resume token to `agent_state`
        (`{_id: agent_id, resume_token, updated_at}`).
      - On start, resume from the saved token, so a restarted agent catches
        up on policies accepted while it was down.
      - Run in a background thread. Stop cleanly.
- [x] If change streams are not available, return a no-op feed and log one
      warning.
- [x] Test: start feed, accept a policy, receive it. Stop feed, accept
      another, restart feed, receive the missed one.
- [x] Do not wire this into `runtime.py` or `agent.py`. Add a request for
      Minh under **Requests** in `GOALS.md` with a 3-line usage example.

## X3. Metrics history and learning curve (~45 min)

- [x] New `evolution/analytics.py`:
      - `record_metrics(db, task_key, policy_version, metrics)` inserts one
        point into `run_metrics`.
      - `learning_curve(db, task_key)` is one aggregation. It returns, per
        policy version in order: runs, mean `success_rate`, mean
        `action_count`, total `policy_violations`, and the decision from
        `evaluations`.
      - `policy_lineage(db, task_key)` uses `$graphLookup` on `policies`
        (`parent_version` → `version`) to return the chain from v1 to the
        latest accepted policy.
- [x] Add a request for Minh in `GOALS.md` to call `record_metrics` after
      each verified run.
- [x] Test against `rcu_test_codex` with seeded data.

## X4. Dashboard shows the Atlas features (~45 min)

- [ ] In `dashboard/app.py`, add panels for:
      - the learning curve from X3,
      - the policy lineage from X3,
      - the top lessons for a query. Call Claude's
        `ExperienceMemory.find_similar(..., query_text=...)` from C2 and C3.
        If it is not merged yet, show the recent-lessons fallback.
- [ ] Keep the fixture fallback for when Atlas is down.
- [ ] Keep the dashboard read-only. It must work with a `read`-role user.

## X5. Judge-facing demo script (~30 min)

- [ ] New `scripts/atlas_demo.py`. It runs against a scratch database
      `rcu_demo` and prints one clear section per feature:
      1. Voyage `autoEmbed` finds a lesson by meaning, not by words.
      2. `$rankFusion` ranks useful lessons above merely similar ones.
      3. A change stream delivers an accepted policy to a second agent.
      4. `$jsonSchema` rejects a bad policy.
      5. The learning curve and lineage aggregations.
- [ ] It waits for search indexes to be READY and cleans up at the end.
- [ ] One command: `uv run python scripts/atlas_demo.py`.

## Done log

<!-- One line per finished task: task id, commit hash, one-line result. -->
- X1, `7a1e860`, atomic verdict/status update, duplicate save result, Atlas rollback check.
- X2, `61b1835`, accepted-policy change stream with persisted resume and restart delivery.
