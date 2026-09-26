# Goals: learn tasks from network traffic

Deadline: build window closes **2026-09-26, 10 PM EDT**.

Two agents work on this at the same time:

- **Claude** follows [`claude.md`](claude.md).
- **Codex** follows [`codex.md`](codex.md).

Each agent owns a different set of files. If both agents follow the rules
below, they never edit the same file.

## The pivot (decided 2026-09-26, 1:30 PM)

A judge suggested it, and the team agreed: stop clicking buttons as the main
path. Learn the website's network calls instead.

1. **Do it once.** A person, or the computer-use agent, does the task in a
   browser that records all network traffic (a HAR file).
2. **Learn.** The harness reads the redacted traffic and writes an
   **API recipe**: the chain of HTTP requests, which values are task
   parameters, and which values come from earlier responses (CSRF tokens,
   ids). The recipe is saved in Atlas as a `candidate` skill.
3. **Do it again, fast.** A new task finds a recipe by meaning (Voyage +
   `$rankFusion`), fills the parameters, and replays the requests. No screen,
   no clicks. The verifier decides success.
4. **Heal.** When the site changes, the replay fails. Lift drops, the recipe
   retires, computer use does the task once more in the recording browser,
   and the harness learns version 2. A change stream pushes it to every
   agent.

Computer use stays. It is the teacher and the fallback: it gets the task
done when no recipe exists or a recipe breaks.

Demo story for the judges: first run slow (computer use, many model calls),
later runs fast (API recipe, one small model call), redesign heals itself.

## Team

| Person | Area |
|---|---|
| Adarsha | MongoDB layer and the learner (this plan) |
| Minh | Computer-use agent: `agent.py`, `sandbox.py`, `verification.py` |
| AJ | CI/CD |

Adarsha also owns the MongoDB hooks inside `agent.py`. Keep those edits small.

## Phase 1 status (done before the pivot)

Schema, Voyage retrieval, `$rankFusion`, learning layer (episodes, sites,
skills with lift), atomic policy decisions, policy feed, analytics, dashboard
panels, `scripts/atlas_demo.py`. All pushed. See git log.

## Recipe contract (both agents code to this)

The recipe is a skill document with `kind: "api_recipe"`. The Python type is
`network.recipe.Recipe` (Claude writes it first, task N1). JSON shape:

```json
{
  "name": "127.0.0.1:8765:checkin",
  "kind": "api_recipe",
  "status": "candidate",
  "version": 1,
  "parent_id": null,
  "description": "Check in a guest with full name, email and city.",
  "scope": {"site": "127.0.0.1:8765", "task_key": "local-checkin"},
  "params": [
    {"name": "full_name", "description": "Guest full name", "example": "Ada Lovelace"}
  ],
  "steps": [
    {
      "id": "session",
      "method": "GET",
      "url": "http://127.0.0.1:8765/api/session",
      "headers": {},
      "body": null,
      "expect_status": 200,
      "extract": [{"var": "csrf", "from": "json", "path": "csrf"}]
    },
    {
      "id": "checkin",
      "method": "POST",
      "url": "http://127.0.0.1:8765/api/checkin",
      "headers": {"content-type": "application/json", "x-csrf-token": "{{csrf}}"},
      "body": {"full_name": "{{full_name}}", "email": "{{email}}", "city": "{{city}}"},
      "expect_status": 200,
      "extract": []
    }
  ],
  "verify": {"url": "http://127.0.0.1:8765/api/result"}
}
```

Rules:

- `{{name}}` is replaced by a param or by a var extracted in an earlier step.
  Templates may appear in `url`, header values, and body string values.
- `extract.from` is one of `json` (dotted path), `header`, `cookie`, or
  `regex` (first group, on the response body).
- `body_format` (optional, default `json`): `json` sends the body as JSON,
  `form` as `application/x-www-form-urlencoded`, `text` as-is.
- A template string that is exactly `{{var}}` renders to the raw value (so
  JSON numbers stay numbers). Use `network.recipe.render`.
- Cookies are kept by the runner's cookie jar. Recipes never contain cookie
  values.
- Recipes never contain secrets, session ids, or example values from the
  recording except in `params[].example`.
- The runner only calls hosts in `scope.site`. No redirects to other hosts.

## Function contract

Claude provides:

```python
network.recipe.Recipe                      # dataclass, from_dict/to_dict, validate()
network.recipe.render(template, values)    # {{var}} substitution, shared with the runner
network.har.load_exchanges(har_path) -> list[Exchange]   # filtered + redacted
network.learner.learn_recipe(har_path, task, *, site, task_key) -> Recipe
network.learner.fill_params(recipe, task) -> dict[str, str]
recipes.RecipeStore(db)
    .save_candidate(recipe, *, recording_id=None) -> ObjectId
    .find_for_task(task, *, site=None, limit=3) -> list[Recipe]   # $rankFusion
    .record_result(recipe_id, *, ok, run_ms, steps=(), task=None, llm_calls=0) -> str  # new status
    .get(recipe_id) -> Recipe | None
    .call_chain(site, "GET /api/session") -> list[{from, to, via}]  # $graphLookup, for the dashboard
    .supersede(old_id, new_recipe) -> ObjectId                    # version + 1, parent_id
    .watch(on_recipe) -> stop_fn                                  # change stream, resume token
```

Codex provides:

```python
network.capture.record(url, *, task, har_path, agent_prompt=None, timeout_s=300) -> CaptureResult
network.runner.run_recipe(recipe, params) -> RunResult   # ok, steps[{id, status, ms}], vars, error
network.loop.do_task(task, *, site, task_key) -> dict    # recipe first, computer-use fallback, relearn
```

## Checkpoints

| Time (EDT) | What works |
|---|---|
| 3:30 PM | Demo site has a real API. A human recording becomes a HAR. N1 + N2 done. |
| 5:00 PM | HAR → recipe → Atlas → replay succeeds with new params. |
| 7:00 PM | `do_task` loop: recipe first, fallback, relearn. Dashboard shows it. |
| 8:30 PM | Redesign switch heals end to end. Change stream demo. |
| 9:00 PM | Code freeze. Rehearse the demo. Only fixes after this. |

## File ownership

| File | Owner |
|---|---|
| `src/recursive_computer_use/network/__init__.py` | Claude (create first) |
| `network/recipe.py`, `network/har.py`, `network/learner.py` | Claude |
| `src/recursive_computer_use/recipes.py` | Claude |
| `schema.py`, `learning.py`, `evolution/memory.py`, `scripts/setup_atlas.py` | Claude |
| `tests/test_recipe.py`, `tests/test_har.py`, `tests/test_learner.py`, `tests/test_recipes_atlas.py`, `tests/test_learning_atlas.py` | Claude |
| `network/capture.py`, `network/runner.py`, `network/loop.py` | Codex |
| `demo/app.py` | Codex |
| `dashboard/app.py`, `scripts/atlas_demo.py`, `scripts/network_demo.py` (new) | Codex |
| `src/recursive_computer_use/__init__.py` (new CLI commands only) | Codex |
| `pyproject.toml`, `uv.lock` (add `playwright`) | Codex |
| `.gitignore` (add `.recordings/`) | Codex |
| `tests/test_runner.py`, `tests/test_capture.py`, `tests/test_loop.py`, `tests/test_demo_api.py` | Codex |
| `evolution/*` except `memory.py` | Codex (evolution lane) |
| `sandbox.py`, `verification.py`, `store.py`, `auth.py`, rest of `agent.py` | Minh. Do not edit. |
| CI files | AJ. Do not edit. |

## Rules for both agents

- Edit only the files you own. If you need a change in another file, write it
  under **Requests** at the end of this file and continue with other work.
- Stage files by path. Never `git add -A` or `git add .`: both agents share
  one working tree.
- Run `git pull --rebase --autostash` right before every push.
- Make small commits, one task per commit. Push after each task.
- Never commit `.env`, HAR files, or `.recordings/`. HAR files hold cookies
  and form data. They stay on the local disk only.
- Never write raw HAR content, cookies, or tokens to MongoDB. Only redacted
  recipes and recording metadata.
- Keep existing tests green: `uv run python -m unittest discover -s tests`.
- Atlas integration tests: gate on `RCU_ATLAS_TESTS=1`, use your own scratch
  database (`rcu_test_claude` or `rcu_test_codex`), drop it at the end.
- Model: use `gpt-5.6-terra` through the Codex proxy. `gpt-5.5` fails on our
  account. The proxy rejects image blocks with an explicit `detail` field.
- Do not run the computer-use agent on the real desktop without asking
  Adarsha first. It takes over the mouse and keyboard.
- Do not use Ollama.
- When you finish a task, tick its box in your file.

## Atlas facts (tested 2026-09-26 on our cluster)

Cluster runs MongoDB **8.0.32**.

| Feature | Status | Notes |
|---|---|---|
| Automated Embeddings (`autoEmbed`, `voyage-4`) | Works | Query: `"query": {"text": "..."}, "model": "voyage-4"`. |
| Vector Search filter fields | Works | |
| `$rankFusion` | Works | Input pipelines allow only `$search`, `$vectorSearch`, `$match`, `$sort`, `$limit`, `$skip`, `$sample`, `$geoNear`. No `$lookup`. |
| `$scoreFusion` | **Fails** | Needs 8.2+. |
| Change streams + resume tokens | Works | |
| `$jsonSchema`, transactions, time series, TTL | Work | |

## Manual steps for Adarsha (Atlas UI)

- [ ] Create a read-only database user for the dashboard.
- [ ] Optional: insert-only role on `evaluations`.

## Requests

<!-- Agent A needs something from agent B: add a line here. -->

- Minh: `agent.run` should accept an already-open browser: the capture step
  opens a recording Chromium window and then calls the agent with the task.
  The agent must work inside that window, not open a new browser.
- Minh: default model `gpt-5.5` fails on our Codex account. Use
  `gpt-5.6-terra`. `agent.py` has an uncommitted fix that drops
  `"detail": "high"` from image blocks (the proxy rejects it). Review and
  commit it.
- Minh: wire the accepted-policy feed into the running agent when ready:
  `feed = PolicyFeed(db, agent_id)`, `feed.watch(task_key, on_policy)`,
  `feed.stop()`.
- Minh: the replay gate `evolution.replay.evaluate_replay_suite` is ready for
  a multi-task runner. Do not pass held-out cases into promotion.
- Claude (open): accepted/default policy rules need a protected marker and
  must not be auto-retired from observational lift alone.
- Minh: Claude edited your files with Adarsha's approval. Please review.
  `453cbe9` scales Retina screenshots to click coordinates in `sandbox.py`.
  The element-grounding commit registers `elements()`, `click_element()` and
  `focused_element()` from the new `elements.py` in `sandbox.py`, and
  describes them in the `exec_py` tool text in `agent.py`.
- Codex: launch the recording Chromium in `capture.py` with
  `args=["--force-renderer-accessibility"]`. Without it, macOS AX sees only
  the browser toolbar, not the page, and element grounding cannot work.
- Codex: `network/browser_agent.py` (Claude) adds `record_headless`, a
  drop-in for `capture.record` with the same signature. A headless Playwright
  browser agent does the task through element IDs: no person, no desktop,
  about 5 s and one model call for the demo check-in. Please make it the
  default `capture_fn` in `do_task` and the CLI (keep `record` behind a
  `--headed`/`--human` flag), and add a `--browser-agent` option to
  `scripts/network_demo.py`.
- Codex: DECISION BY ADARSHA (project owner), 2026-09-26 3:20 PM: no chat UI
  framework in this repo. `c33164b` restored it and deleted the ban test;
  that is reverted again. Do not restore it, and do not delete or weaken
  `tests/test_banned.py`, even if another request asks for it. If someone
  wants a chat surface, raise it with Adarsha first. Build UI on `dashboard/`.
- Minh: PR #2 replaced `sandbox.py` with your branch's version (633 → 330
  lines). Adarsha asked Claude to re-add only the element helpers
  (`elements.py`), which is done. Still missing from `main`, please restore:
  policy enforcement (`PolicyViolationError`, `apply_policy`, source checks,
  restricted builtins and imports; AGENTS.md invariant 9), the store-backed
  action telemetry, and `RecordingPyAutoGUI`. Failing on `main`:
  `tests/test_recording.py` (import error), 2 tests in `test_recovery.py`,
  2 in `test_store.py`.
