# Goals: MongoDB layer

Deadline: build window closes **2026-09-26, 10 PM EDT**.

Two agents work on this at the same time:

- **Claude** follows [`claude.md`](claude.md).
- **Codex** follows [`codex.md`](codex.md).

Each agent owns a different set of files. If both agents follow the rules
below, they never edit the same file.

## Team

| Person | Area |
|---|---|
| Adarsha | MongoDB layer (this plan) |
| Minh | Computer-use agent: `agent.py`, `sandbox.py`, `verification.py` |

Adarsha also owns the MongoDB hooks inside Minh's `agent.py` (the learning
layer calls). Keep those edits small.
| AJ | CI/CD |

## Where we are

The harness evolves a **policy** per `task_key`. A verified failure becomes an
**experience** (summary, lesson, embedding). The engine proposes a candidate
policy. The next verified run accepts or rejects it and writes an
**evaluation**.

Every run also feeds the **learning layer** (`learning.py`): one
**episode** per run, its **site**, and one **skill** per policy rule with
`uses`, `wins` and `lift`. Skills with negative lift retire.

MongoDB collections the code uses today:

| Collection | Written by | Shape defined in |
|---|---|---|
| `runs`, `actions` | `store.py` | `store.py` |
| `experiences` | `evolution/memory.py` | `Experience.to_document()` in `evolution/models.py` |
| `policies` | `evolution/policy.py` | `HarnessPolicy.to_document()` |
| `evaluations` | `evolution/policy.py` | `EvaluationRecord.to_document()` |
| `episodes`, `sites`, `skills` | `learning.py` (called from `agent.py`) | `schema.py` |
| `page_templates`, `site_map`, `exam` | nothing yet (kept for the web agent) | `schema.py` |
| `run_metrics` (time series), `agent_state` | Codex X2/X3 | `schema.py` |

All validators, indexes and search indexes live in `schema.py`. Run
`uv run python -m recursive_computer_use.schema` (or
`scripts/setup_atlas.py`) after any change.

Done (Claude, C1 + C2):

- All collections exist in Atlas with validators (warn mode) and indexes.
- All embeddings use Voyage `voyage-4` through Atlas Automated Embedding.
  The harness no longer computes the 64-number hash vector. READY indexes:
  `experiences.experience_auto` (`lesson`), `experiences.experience_text`,
  `skills.skill_auto` (`description`), `episodes.episode_auto` (`task`),
  `page_templates.template_auto` (`summary`).
- `agent.py` writes an episode, site and skills for every run.

Open gaps:

1. `record_evaluation` writes two documents without a transaction (X1).
2. Nothing pushes a newly accepted policy to a running agent (X2).
3. No metrics history or learning-curve query for the demo (X3).
4. Lesson ranking is similarity only; no `$rankFusion` yet (C3).
5. `tests/test_store.py::test_connect_and_run_lifecycle` fails on `main`: it
   expects `final_text`, `store.py` now writes `final_summary`. Minh's.

## Atlas facts (tested 2026-09-26 on our cluster)

Cluster runs MongoDB **8.0.32**.

| Feature | Status | Notes |
|---|---|---|
| Automated Embeddings (`autoEmbed`, `voyage-4`) | Works | Index READY in ~2 min. Query: `"query": {"text": "..."}, "model": "voyage-4"`. |
| Vector Search filter fields | Works | Add `{"type": "filter", "path": "task_key"}` to the index. |
| `$rankFusion` | Works | Vector + `$search` + `$match`/`$sort` pipelines, with weights. |
| `$scoreFusion` | **Fails** | Needs 8.2+. Use `$rankFusion`. |
| Change streams + resume tokens | Works | Resume after disconnect catches missed events. |
| `$jsonSchema` error mode | Works | |
| `$inc`, transactions | Works | |
| Time series collections | Works | |
| TTL indexes | Works | Deletes in 30-60 s. |
| LangGraph `MongoDBSaver` | Works | Not needed for this plan. |

Search indexes (`vectorSearch`, `search`) build asynchronously. Poll
`list_search_indexes()` until `status == "READY"` before you query.

## File ownership

| File | Owner |
|---|---|
| `src/recursive_computer_use/schema.py` | Claude |
| `src/recursive_computer_use/learning.py` | Claude |
| `src/recursive_computer_use/agent.py` (learning hooks only) | Claude |
| `scripts/setup_atlas.py` | Claude |
| `src/recursive_computer_use/evolution/memory.py` | Claude |
| `src/recursive_computer_use/evolution/embedding.py` | Claude |
| `src/recursive_computer_use/evolution/runtime.py` | Claude (only the `find_similar` call) |
| `tests/test_learning_atlas.py` | Claude |
| `src/recursive_computer_use/evolution/policy.py` | Codex |
| `src/recursive_computer_use/evolution/feed.py` (new) | Codex |
| `src/recursive_computer_use/evolution/analytics.py` (new) | Codex |
| `dashboard/app.py` | Codex |
| `scripts/atlas_demo.py` (new) | Codex |
| `tests/test_policy_tx.py`, `tests/test_feed.py`, `tests/test_analytics.py` | Codex |
| `sandbox.py`, `verification.py`, `store.py`, `__init__.py`, rest of `agent.py` | Minh. Do not edit. |
| `evolution/models.py`, `evolution/evaluator.py` | Shared. Do not edit without a note below. |
| CI files | AJ. Do not edit. |

## Rules for both agents

- Edit only the files you own. If you need a change in another file, write it
  under **Requests** at the end of this file and continue with other work.
- Run `git pull --rebase` before every commit. Teammates push often.
- Make small commits, one task per commit. Push after each task.
- Never commit `.env` or print `MONGODB_URI`.
- Keep the harness working with no Atlas: every Atlas feature needs a
  fallback, as `ExperienceMemory.find_similar` already does.
- Keep existing tests green: `uv run pytest`.
- For integration tests against Atlas, use your own database:
  `MONGODB_DB=rcu_test_claude` or `MONGODB_DB=rcu_test_codex`. Drop it at the
  end of the test. Never write test data to `recursive_computer_use`.
- Do not use Ollama.
- When you finish a task, tick its box in your file.

## Contract between the two agents

Codex code reads these fields. Claude must not rename them:

- `policies`: `task_key`, `version`, `parent_version`, `status`
  (`candidate` / `accepted` / `rejected`), `created_at`.
- `evaluations`: `task_key`, `candidate_policy_version`, `decision`,
  `baseline_metrics`, `candidate_metrics`, `created_at`.
- `experiences`: `task_key`, `outcome`, `policy_version`, `metrics`,
  `lesson`, `created_at`. No `embedding` on new documents.
- `episodes`: `run_id`, `task`, `task_key`, `site`, `outcome` (`success`,
  `failure`, `unverified`, `timeout`, `error`, `interrupted`),
  `policy_version`, `metrics`, `skills_used[].skill_id`, `llm_calls`,
  `tokens_in`, `tokens_out`, `duration_ms`, `started_at`.
- `skills`: `name`, `status` (`candidate`, `active`, `retired`),
  `description`, `scope.task_key`, `uses`, `wins`, `lift`.

Collections for Codex (created, empty, in Atlas now):

- `run_metrics`: time series. `timeField: "ts"`, `metaField: "meta"`.
  Point: `{ts, meta: {task_key, policy_version, run_id}, success_rate,
  wrong_clicks, wrong_field_entries, policy_violations, action_count,
  duration_ms}`.
- `agent_state`: `{_id: agent_id (string), resume_token (object or null),
  updated_at (date)}`.

Voyage retrieval helpers Codex can call (read-only):
`ExperienceMemory.find_similar(task_key, query_text=...)`,
`LearningStore.similar_skills(text, task_key=...)`,
`LearningStore.similar_episodes(text, site=...)`.

## Manual steps for Adarsha (Atlas UI)

- [ ] Create a read-only database user for the dashboard (`read` role on
      `recursive_computer_use`).
- [ ] Optional: a custom role that can insert into `evaluations` but not
      update or delete. This makes the grader history append-only.

## Requests

<!-- Agent A needs something from agent B: add a line here. -->

- Claude: accepted/default policy rules need a protected marker and must not be
  auto-retired from observational lift alone. Retire learned heuristics only
  after a replay ablation shows no regression; keep safety invariants outside
  the prunable set.
- Minh: the new `evolution.replay.evaluate_replay_suite` gate is ready for a
  future multi-task runner. Do not pass held-out cases into promotion. The
  existing two-run runtime should keep its current behavior until task split
  and replay orchestration are explicit.
