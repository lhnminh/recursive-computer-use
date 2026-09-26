# AGENTS.md

This file applies to the entire repository. It is the working contract for AI
coding agents and human contributors.

## Mission

Build a local-first computer-use harness that improves task-specific behavior
from verified experience while keeping capability changes constrained,
auditable, reversible, and measurable.

The project is not merely a desktop agent and not merely an action logger. A
valid contribution should strengthen at least one part of this evidence chain:

```text
local execution
  -> sanitized telemetry
  -> deterministic verification
  -> redacted durable experience
  -> relevant memory retrieval
  -> constrained candidate policy
  -> candidate trial
  -> deterministic accept or reject decision
```

Do not describe a feature as self-improvement unless it closes this loop or
provides evidence used by the loop.

## Read before editing

Read these files in order when starting substantial work:

1. `README.md` for product behavior and operator workflow.
2. `design-decision.md` for the original agent-loop constraints.
3. `src/recursive_computer_use/agent.py` for runtime orchestration.
4. `src/recursive_computer_use/sandbox.py` for the execution boundary.
5. `src/recursive_computer_use/store.py` for persistence behavior.
6. `src/recursive_computer_use/evolution/` for policy and memory contracts.
7. `src/recursive_computer_use/schema.py` for every MongoDB collection shape.
8. `goals/GOALS.md` for current tasks, file ownership, and Atlas facts.

Also inspect `git status` before editing. Preserve unrelated user or teammate
changes. Never discard a dirty working tree to simplify your task.

## Architecture map

### CLI and authentication

- `src/recursive_computer_use/__init__.py`
  - Owns CLI flags and error presentation.
  - Keep startup imports light.
  - Pass explicit options into `agent.run` rather than reading CLI globals.
- `src/recursive_computer_use/__main__.py`
  - Thin module launcher only.
- `src/recursive_computer_use/auth.py`
  - Resolves Codex proxy credentials first, then `OPENAI_API_KEY`.
  - Never print tokens or authentication file contents.

### Runtime

- `src/recursive_computer_use/agent.py`
  - Owns the bounded model loop.
  - Loads and applies the policy before model execution.
  - Finalizes every run in `finally`.
  - Invokes evolution only from verifier-produced metrics.
- `src/recursive_computer_use/sandbox.py`
  - Owns the persistent namespace and `pyautogui` proxy.
  - Enforces policy before delegating desktop actions.
  - Captures screenshots for model observations.
  - Restricts built-ins and imports available to generated code.
- `src/recursive_computer_use/verification.py`
  - Fetches metrics only from localhost HTTP endpoints.
- `src/recursive_computer_use/recovery.py`
  - `RecoveryMonitor` watches each tool result for three stuck signals:
    actions with no screen change, repeated code, and repeated errors.
  - Two signals in a row add a recovery hint to the tool result. More than
    the policy's `retry_limit` hints abort the run.
  - A failed verification sends the model back to fix the task, up to
    `retry_limit` times. The evolution engine sees only the final metrics.
  - Stuck aborts and turn-limit timeouts still run the verifier, so they
    become verified failures.
  - Keeps screen hashes and code digests in memory only. Persists counts.

### Persistence

- `src/recursive_computer_use/store.py`
  - Owns `runs` and `actions`.
  - Logging must remain non-fatal.
  - Typed content, secrets, and screenshot bytes must not enter telemetry.
- `src/recursive_computer_use/schema.py`
  - Single source of truth for every collection: `$jsonSchema` validators,
    indexes, the `run_metrics` time series, and Atlas Search / Vector Search
    indexes.
  - Idempotent. Run `uv run python -m recursive_computer_use.schema` after
    any change; existing documents are kept.
- `scripts/setup_atlas.py`
  - Thin entry point that calls `schema.main()`.
- `src/recursive_computer_use/learning.py`
  - Owns `episodes`, `sites`, and `skills`.
  - Called from two hook points in `agent.py`: `sync_policy_skills` before
    the loop and `record_episode` in `finally`.
  - Every method is non-fatal, like `store.py`.

### Evolution

- `evolution/models.py`
  - Source of truth for validated documents and bounds.
- `evolution/embedding.py`
  - Legacy deterministic 64-dim hash vectors. The runtime no longer uses
    them; kept only for old documents and tests.
- `evolution/memory.py`
  - Uses Atlas Vector Search with Voyage Automated Embedding
    (`query_text`), then the legacy vector, then recent-memory fallback.
- `evolution/policy.py`
  - Limits mutation surface and persists policy versions.
- `evolution/evaluator.py`
  - Pure deterministic promotion gate.
- `evolution/runtime.py`
  - Coordinates initial policy, failure memory, candidate creation, and trial
    evaluation.

### Demo surfaces

- `src/recursive_computer_use/chatbot_app.py`
  - Primary local Streamlit product surface for arbitrary desktop tasks.
  - Must remain bound to loopback and use `agent.run`; never duplicate or
    bypass sandbox, telemetry, policy, recovery, or verification logic.
  - Must serialize desktop runs process-wide so browser sessions cannot issue
    competing mouse and keyboard actions.
- `src/recursive_computer_use/chat_runtime.py`
  - Owns UI input normalization, local verifier validation, safe error text,
    and the packaged Streamlit launcher.
  - MongoDB credentials remain environment-only and must not enter UI state.
- `demo/app.py`
  - Deterministic local task and verifier.
  - Must not depend on external services.
- `dashboard/app.py`
  - Read-only evidence view.
  - Must label whether data comes from MongoDB or fixtures.
- `dashboard/fixtures.json`
  - Presentation fallback, never proof that a live run improved.

## Non-negotiable invariants

### 1. Execution remains local

Mouse and keyboard actions must execute on the user's machine. Do not introduce
remote desktop execution, hosted browser control, or a server that can trigger
desktop actions over the network.

### 2. Verification controls evolution

Model self-assessment is not an acceptance signal. A policy may be proposed
after a verified failure, but may be accepted only from deterministic metrics
returned by the localhost verifier.

Do not infer success from:

- the assistant's final prose;
- absence of an exception;
- number of actions;
- the model claiming that the task is done.

### 3. Candidates do not silently become active baselines

Policy lifecycle is:

```text
accepted parent -> candidate -> accepted or rejected
```

A candidate can be used for one matching trial. The evaluator must compare it
with its direct accepted parent. Preserve every version and evaluation record.
Do not overwrite policy history in place.

### 4. Policy mutation is schema-constrained

Candidates may modify only:

- rules;
- maximum actions without a screenshot;
- total action budget;
- retry limit;
- a subset of the parent's tool allowlist.

Candidates must never:

- rewrite repository source;
- replace the base system prompt;
- add tools absent from the parent;
- change authentication or persistence settings;
- disable telemetry, verification, or enforcement;
- grant file, process, shell, or arbitrary network access.

### 5. Tool access can shrink but not expand

`candidate.tool_allowlist` must be a subset of
`parent.tool_allowlist`. Any expansion is a safety regression and must be
rejected before a desktop run.

### 6. Persistence is privacy-filtered

Never persist these values to MongoDB:

- raw typed text;
- passwords, tokens, or API keys;
- screenshot bytes or data URLs;
- full unredacted prompts;
- full unredacted final responses;
- arbitrary desktop or browser contents.

Allowed durable data includes:

- bounded redacted summaries;
- prompt digests;
- action type, coordinates, and sanitized metadata;
- verifier metrics;
- Atlas-generated Voyage embeddings of redacted text fields;
- policy versions and evaluation reasons.

Fields indexed with `autoEmbed` are sent by Atlas to Voyage AI for embedding:
`experiences.lesson`, `skills.description`, `episodes.task`, and
`page_templates.summary`. Only put redacted, bounded text in these fields.

When adding a field, decide whether it is safe for Atlas before writing the
code. Default to local-only or omit it if uncertain.

### 7. Screenshots are not stored in MongoDB

Screenshots may be returned to the configured model for visual reasoning, but
must not be written to `runs`, `actions`, `experiences`, policies, evaluations,
fixtures generated from real data, or logs.

### 8. Telemetry must be non-fatal

A MongoDB outage or failed telemetry write must not block an otherwise allowed
desktop action. Catch persistence errors at the store boundary and degrade to
disabled logging. Do not broadly suppress policy violations.

### 9. Policy violations block before delegation

Check tool access, action budget, and screenshot cadence before calling real
`pyautogui`. Record a sanitized `policy_violation` event when possible, then
raise `PolicyViolationError`.

### 10. The verifier stays local

Only allow plain HTTP verifier URLs whose host is `localhost`, `127.0.0.1`, or
`::1`. Do not weaken this validation or follow redirects to remote hosts.

### 11. The Python namespace is not a security sandbox

Maintain the current blocked file APIs and import allowlist, but do not claim
that in-process Python is hardened isolation. If adversarial prompt resistance
is required, propose OS or process isolation as a separate feature.

## Stable data contracts

### Run statuses

Use only:

- `running`
- `completed`
- `failed`
- `interrupted`

Ensure `finish_run` executes for every terminal path.

### Experience outcomes

Use only `success` or `failure`. Success requires verifier
`success_rate == 1.0` in the current demo contract.

### Policy statuses

Use only:

- `candidate`
- `accepted`
- `rejected`

### Evaluation decisions

Use only `accepted` or `rejected`. A missing baseline is pending runtime state,
not a persisted acceptance decision.

### Metric names

Keep these names stable across verifier, run, experience, evaluation, fixtures,
and dashboard:

- `success_rate`
- `wrong_clicks`
- `wrong_field_entries`
- `policy_violations`
- `action_count`
- `duration_ms`

Do not introduce aliases such as `wrong_field_count` inside persistence models.
The verifier adapter is responsible for translating endpoint fields.

### Episode outcomes

Use only:

- `success`, `failure`: set only from verifier metrics.
- `unverified`: the model finished but no verifier ran. Never count as
  success.
- `timeout`, `error`, `interrupted`: the run did not finish.

Only `success` and `failure` count toward skill `uses`, `wins`, and `lift`.

Each episode also has `recovery`: `nudges`, `no_effect`, `repeated_code`,
`repeated_errors`, `tool_errors`, `verifier_retries` (counts) and
`abort_reason` (`"stuck"` or null).

### Skill statuses

Use only `candidate`, `active`, or `retired`. A rule in an accepted policy
becomes `active`. A learned skill with `lift < -0.2` after 3 verified uses
becomes a retirement candidate. It becomes `retired` only after replay
ablation preserves success and safety while reducing token or action cost.
Default safety rules are protected and cannot be retired.

### Search indexes

All embeddings use Atlas Automated Embedding with Voyage `voyage-4`. The
harness never computes vectors itself. Definitions live in
`schema.SEARCH_INDEXES`:

| Collection | Index | Type | Embedded path | Filter fields |
|---|---|---|---|---|
| `experiences` | `experience_auto` | vectorSearch (autoEmbed) | `lesson` | `task_key`, `outcome` |
| `experiences` | `experience_text` | search | `summary`, `lesson` | |
| `skills` | `skill_auto` | vectorSearch (autoEmbed) | `description` | `status`, `scope.task_key` |
| `episodes` | `episode_auto` | vectorSearch (autoEmbed) | `task` | `site`, `outcome` |
| `page_templates` | `template_auto` | vectorSearch (autoEmbed) | `summary` | `platform`, `kind` |

Query an autoEmbed index with text, not a vector:

```python
{"$vectorSearch": {"index": "experience_auto", "path": "lesson",
                   "query": {"text": "..."}, "model": "voyage-4",
                   "numCandidates": 50, "limit": 5,
                   "filter": {"task_key": "..."}}}
```

Search indexes build asynchronously (~2 minutes). Poll
`list_search_indexes()` until `status == "READY"` before querying new ones.

The old `experience_embedding` (64-dim) index is no longer created.

### Atlas cluster facts

The cluster runs MongoDB 8.0. These were tested on it:

- Works: Automated Embedding, Vector Search filters, `$rankFusion`, change
  streams with resume tokens, `$jsonSchema`, transactions, time series, TTL.
- Does not work: `$scoreFusion` (needs 8.2+). Use `$rankFusion` with weights.

### Schema validation

Validators run in `error` mode (`schema.VALIDATION_ACTION`, since
2026-09-26). MongoDB rejects a write whose shape does not match. Unknown
extra fields are allowed; wrong types, bad enum values and missing required
fields are not. When you add or rename a persisted field or enum value,
update its validator in `schema.py` in the same commit and rerun it against
Atlas. The store and learning layers swallow write errors and print a
`[store]`/`[learning]` warning, so watch the logs. To debug a shape change,
set `VALIDATION_ACTION = "warn"` locally and rerun `schema.py` on a scratch
database.

## Action recording rules

The recording boundary is the injected `RecordingPyAutoGUI` object. Do not
parse generated source code to infer actions.

Supported action families:

- click, double click, right click;
- move and drag;
- write and typewrite;
- press and hotkey;
- scroll;
- screenshot cadence tracking.

Typing telemetry stores only length. Ordinary character keys become
`[character]`. Control-key names may be stored because they are needed to
understand agent behavior.

If you add a new `pyautogui` action:

1. Add it to the recorded action set.
2. Map it to a normalized policy tool name.
3. Sanitize every argument.
4. Add it to the initial allowlist only if required.
5. Add fake-module tests that do not touch the real desktop.
6. Update README and this file.

## Evolution changes

When modifying the evolution engine:

- Keep `evaluate_candidate` pure and deterministic.
- Keep persistence adapters small and replaceable with test doubles.
- Preserve recent-memory fallback when Atlas Vector Search fails.
- Embeddings use Atlas Automated Embedding (Voyage). The project owner
  approved this provider on 2026-09-26. Do not add another remote embedding
  provider without approval.
- Bound text, rule counts, vector dimensions, and retrieval limits.
- Redact experience summaries and lessons before insertion.
- Reject unknown policy keys rather than silently ignoring them.

Success improvement alone is insufficient. The candidate must also avoid every
safety regression checked by the evaluator.

## Demo requirements

The live demonstration should show this sequence:

1. Reset the local task.
2. Run accepted policy v1.
3. Display the verified failure metrics.
4. Display the stored redacted experience and retrieved lesson.
5. Display candidate v2 and its policy diff.
6. Reset and repeat the identical task.
7. Display the candidate trial metrics.
8. Display the deterministic acceptance or rejection reason.

Never present fixture data as a live run. The dashboard source label must remain
visible.

## Testing rules

Tests must not:

- move the real pointer;
- type on the real keyboard;
- capture the real screen;
- require live MongoDB or Atlas;
- call a real model endpoint;
- rely on network access.

Use fake `pyautogui`, fake Mongo collections, injected stores, mocked clients,
and local deterministic data.

Exception: opt-in Atlas integration tests. They skip unless
`RCU_ATLAS_TESTS=1` is set, use their own scratch database (for example
`rcu_test_claude`), and drop it at the end. Never write test data to
`recursive_computer_use`.

```bash
RCU_ATLAS_TESTS=1 uv run python -m unittest tests.test_learning_atlas -v
```

Standard validation commands:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src demo dashboard scripts
git diff --check
```

When the user explicitly says not to run tests, do not run them. State that the
result is unverified in the handoff.

## Parallel work ownership

For multiple agents, split by file boundaries:

### Runtime and telemetry lane

Owns:

- `agent.py`
- `sandbox.py`
- `store.py`
- CLI files
- recording and store tests

### Evolution lane

Owns:

- `src/recursive_computer_use/evolution/`
- evolution tests
- Atlas index contract

### Demo and presentation lane

Owns:

- `demo/`
- `dashboard/`
- fixture data
- demo documentation

### MongoDB lane

Owns:

- `schema.py`, `learning.py`, `scripts/setup_atlas.py`
- `evolution/memory.py` and the Voyage retrieval path
- the learning-layer hook points in `agent.py` (not the rest of the file)
- Atlas integration tests

Only one lane should edit a shared file at a time. Assign final integration in
`agent.py` to the runtime lane after other APIs stabilize.

Current task split and per-file owners for this sprint are in
`goals/GOALS.md`. Check it before editing a file you do not own. If you need
a change in someone else's file, add a line under **Requests** there.

## Editing discipline

- Use `rg` and `rg --files` for discovery.
- Use patch-based edits for hand-written source and documentation.
- Preserve unrelated changes in dirty worktrees.
- Do not use destructive Git commands such as `reset --hard` or checkout-based
  file restoration without explicit user authorization.
- Keep public APIs typed and documented.
- Prefer dependency injection for stores, clients, and desktop modules.
- Avoid new dependencies when the standard library or an existing dependency
  is sufficient.
- Keep dashboard operations read-only.
- Keep demo services bound to `127.0.0.1` by default.

## Git and handoff

Before committing, unless the user says not to validate:

1. Inspect `git status`.
2. Run focused tests.
3. Run compile checks.
4. Run `git diff --check`.
5. Review the diff for raw secrets, screenshots, generated files, and unrelated
   changes.

Several people and agents push to `main` often, and some agents share one
working tree:

- Stage files by path. Do not use `git add -A` or `git add .`: another
  agent's unfinished files may be in the same working tree.
- Run `git pull --rebase --autostash` right before you push. Never force-push
  `main`.
- Never commit `.env` or print `MONGODB_URI`.
- If you changed `schema.py`, run it against Atlas before pushing so the
  live cluster matches the code.

Use a concise commit message describing observable behavior. Push only to the
branch requested by the user. If the repository has no configured author
identity, do not impersonate an existing contributor; use a transparent local
agent identity or ask the user which identity to use.

The final handoff must state:

- files or systems changed;
- commit and branch;
- commands run;
- commands not run;
- remaining limitations or setup requirements.

## Definition of done

A feature is done when:

- it preserves every invariant above;
- its persistent fields are documented and privacy-reviewed;
- its failure mode is non-destructive;
- its behavior is visible through metrics or the dashboard when relevant;
- its tests use fakes rather than the real desktop;
- operator instructions are updated;
- no unsupported claim of autonomous improvement or security is introduced.
