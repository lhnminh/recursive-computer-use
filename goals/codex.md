# Codex's goals: demo API, capture, runner, loop

Read [`GOALS.md`](GOALS.md) first. It has the pivot, the recipe contract,
checkpoints, rules and file ownership.

Your files: `network/capture.py`, `network/runner.py`, `network/loop.py`,
`demo/app.py`, `dashboard/app.py`, `scripts/atlas_demo.py`,
`scripts/network_demo.py`, new CLI commands in `__init__.py`,
`pyproject.toml`, `uv.lock`, `.gitignore`, `evolution/*` except
`memory.py`, and your tests.

Claude creates `network/__init__.py` and `network/recipe.py` first (N1).
Until N1 is pushed, write against the JSON shape in `GOALS.md` and import
`Recipe` once it lands. Do not create `network/__init__.py` yourself.

## X6. Demo site with a real API (first, ~45 min)

The form now trusts the browser's `submit` event. A replay could fake
success. Move the decision to the server.

- [x] `GET /api/session` → `{"csrf": "<random>"}` and a `sid` cookie.
- [x] `POST /api/checkin` with JSON `{full_name, email, city}` and header
      `X-CSRF-Token`. The server checks csrf + sid and the values, then sets
      `success`. Wrong csrf → 403.
- [x] The page's JS uses these two endpoints, so a recording captures them.
- [x] Keep `/api/result` and `/api/reset` as they are. Keep the event
      metrics for the computer-use path.
- [x] Accept any guest, not only Ada: the page shows the expected values
      per reset (`POST /api/reset {"guest": {...}}` sets them). So a replay
      with new params is a real test.
- [x] Redesign switch: `POST /api/redesign {"on": true}` moves the call to
      `POST /api/v2/check-in`, renames `full_name` → `name`, and the old
      endpoint returns 410. The page's JS follows the switch.
- [x] Tests in `tests/test_demo_api.py`.

## X7. Capture (~45 min)

- [x] Add `playwright` to `pyproject.toml`. Document
      `uv run playwright install chromium`.
- [x] Add `.recordings/` to `.gitignore`.
- [x] `network/capture.py`: `record(url, *, task, har_path,
      agent_prompt=None, timeout_s=300)`. Headed Chromium with
      `record_har_path`. Without `agent_prompt`, a person does the flow.
      With it, call `agent.run(...)` so computer use does it in that window
      (see the Minh request in `GOALS.md`). Stop when the verifier reports
      success or on timeout. Return path, duration, verifier result.
- [x] Write only metadata to Atlas (`recordings` collection, Claude's N2).
- [x] Do not run the computer-use path on the real desktop without asking
      Adarsha.

## X8. Runner (~45 min)

- [x] `network/runner.py`: `run_recipe(recipe, params) -> RunResult`.
      Standard library `urllib` + `http.cookiejar`. Render `{{var}}` with
      Claude's `network.recipe.render`.
- [x] Apply `extract` rules after each step. Stop at the first step whose
      status is not `expect_status`.
- [x] Only call hosts in `scope.site`. No redirects off-site. Timeout per
      step 10 s.
- [x] Then GET `recipe.verify.url` and return its verdict.
- [x] Tests against a local `ThreadingHTTPServer` fixture.

## X9. The loop (~60 min)

- [x] `network/loop.py`: `do_task(task, *, site, task_key)`:
      1. `RecipeStore.find_for_task` → best recipe.
      2. `fill_params` → `run_recipe` → `record_result`.
      3. No recipe, or it failed: capture with computer use (or a person),
         `learn_recipe`, `save_candidate` or `supersede`, then replay once
         to verify.
      Return what happened and timings for each path.
- [x] CLI: `recursive-computer-use learn --url URL --task "..."` and
      `recursive-computer-use do --site HOST "task"`.
- [x] Tests with fakes for store, learner and runner.

## X10. Dashboard and judge demo (~45 min)

- [ ] Dashboard panel: recipes per site with version, status, uses, wins,
      lift, and the call chain (`site_map` via `$graphLookup`).
- [ ] Panel: time and model calls per run, computer use vs recipe.
- [ ] `scripts/network_demo.py`: reset → learn from one recording → run 3
      new guests by recipe → flip redesign → replay fails → relearn → v2
      works. Print timings. Uses a scratch database.

## Done log

<!-- One line per finished task: task id, commit hash, one-line result. -->
- X6: `8057ccd`, server-verified check-in API, CSRF sessions, reset guests, and v2 redesign.
- X7: `b7399be`, headed HAR capture with local-only traces and redacted Atlas metadata.
- X8: `cd0d207`, standard-library recipe replay with scoped redirects and verifier checks.
- X9: pending, recipe-first loop with capture-and-learn fallback, superseding and CLI commands.
- Phase 1: X1 `7a1e860`, X2 `61b1835`, X3 `f08a8da`, X4 `c35fc8f`, X5 `9e2ead7`.
