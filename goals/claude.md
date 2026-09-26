# Claude's goals: recipe contract, learner, recipe store

Read [`GOALS.md`](GOALS.md) first. It has the pivot, the recipe contract,
checkpoints, rules and file ownership.

Your files: `network/__init__.py`, `network/recipe.py`, `network/har.py`,
`network/learner.py`, `recipes.py`, `schema.py`, `learning.py`,
`evolution/memory.py`, `scripts/setup_atlas.py`, and your tests.

Phase 1 (C1 to C3: schema, Voyage, learning layer, `$rankFusion`) is done.

## N1. Recipe type — done

- [x] Create `network/__init__.py` and `network/recipe.py`.
- [x] `Recipe`, `Step`, `Extract`, `Param` dataclasses matching the JSON in
      `GOALS.md`. `from_dict`, `to_dict`, `validate()`.
- [x] `validate()` rejects: unknown `extract.from`, a `{{var}}` that no param
      or earlier extract defines, a step URL whose host is not
      `scope.site`, methods other than GET/POST/PUT/PATCH/DELETE, cookie or
      authorization headers, more than 20 steps.
- [x] `render(template, values)` helper for `{{var}}`: shared by the runner.
- [x] Unit tests with fakes. Push, then tick the box so Codex knows.

## N2. Schema for recipes and recordings — done

- [x] `skills` validator accepts `kind: "api_recipe"` with `params`,
      `steps`, `verify`. Keep other kinds valid.
- [x] New `recordings` collection: metadata only. `{site, task, task_key,
      har_sha256, exchange_count, endpoints: [{method, path, status}],
      source: "human" | "agent", created_at}`. TTL 14 days.
- [x] `site_map` accepts `kind: "api_call"` edges with `recipe_id` (writing them is in N5). Edges from recipes: `from` step path → `to` step path,
      `via` the extracted var. `$graphLookup` shows the call chain.
- [x] Run `schema.py` against Atlas.
- [x] Search filters: `skill_auto` and `skill_text` now also filter on `kind` and `scope.site`.

## N3. HAR reader and redaction — done

- [x] `network/har.py`: `load_exchanges(har_path)` returns `Exchange`
      records for same-site API calls only. Drop images, CSS, fonts, JS,
      analytics hosts, and `OPTIONS`.
- [x] Redact: cookie and authorization values, `set-cookie`, tokens in
      headers, emails and secrets in bodies (reuse `redact_text` patterns).
      Keep structure: header names, JSON keys, value types, short values.
- [x] Mark redacted token values with a stable placeholder like
      `<token:1>`, the same placeholder wherever the same value appears, so
      the learner can link a response value to a later request.
- [x] Unit tests with a small hand-written HAR fixture.

## N4. Learner — done

- [x] `network/learner.py`: `learn_recipe(har_path, task, *, site,
      task_key)`. One model call (`gpt-5.6-terra` via `auth.resolve`) with
      the redacted exchanges and the task text. Output must be JSON for
      `Recipe`. Validate; on failure, one repair call with the error.
- [x] Prompt rules: pick only the calls needed for the task; values the
      user typed become `params`; values from earlier responses become
      `extract` + `{{var}}`; never copy a token value.
- [x] `fill_params(recipe, task)`: one small model call that maps the task
      text to param values. Returns only declared param names.
- [x] Test with a fake OpenAI client and the fixture HAR.
- [x] Live test against the real demo API (headless Playwright, no
      desktop): record Ada → learn v1 (11 s) → replay for Grace succeeds
      (2 s fill + 9 ms HTTP) → redesign → 2 × 410 → retired → re-record →
      v2 learns `/api/v2/check-in` and `name` → Grace succeeds. Script used
      a stand-in replayer; Codex's runner (X8) replaces it.
- [x] Live model test on the fixture HAR (`gpt-5.6-terra`): learned the 2-step session → checkin chain with csrf extraction in 11.7 s; `fill_params` 3.9 s.

## N5. Recipe store — done

- [x] Write `site_map` `api_call` edges when a recipe is saved.

- [x] `recipes.py`: `RecipeStore(db)` per the contract in `GOALS.md`.
- [x] `find_for_task`: `$rankFusion` over Voyage on `description`, keyword
      on `description`, and positive `lift`, filtered to
      `kind: "api_recipe"`, live status, and `scope.site` when given.
- [x] `record_result`: writes an episode (`learning.py`), `$inc` uses/wins,
      lift. First verified success promotes `candidate` → `active`. Two
      failures in a row on an `active` recipe → `retired`.
- [x] `supersede`: new version with `parent_id`, old one retired.
- [x] `watch(on_recipe)`: change stream on `skills` for new `active`
      recipes, resume token in `agent_state`.
- [x] Atlas test in `tests/test_recipes_atlas.py`.
- [x] Relevance gate: lift/"proven" only re-order recipes within 0.05 of the
      best Voyage score, so a proven check-in recipe never answers a pizza
      order (found by the Atlas test).
- [x] Episodes get `mode` (`computer_use` | `api_recipe`) and explicit `site`.

## C4. Strict validation — done

- [x] Checked every live collection: 0 invalid documents.
- [x] Ran every Atlas test (learning, recipes, policy tx, feed, analytics)
      with error mode on scratch databases: all pass, no rejected writes.
- [x] `VALIDATION_ACTION = "error"`, applied to Atlas. A policy with
      `status: "magic"` is rejected with `WriteError`.
- [x] Atlas search tests retry for up to 60 s: a READY index can lag new
      documents by a few seconds.

## Done log

<!-- One line per finished task: task id, commit hash, one-line result. -->
- Phase 1: C1, C2, C2b (`5514ede`), C3 (`87d6d57`).
- N1: `network/recipe.py` — `Recipe`, `render`, `template_vars`, `validate()`; 13 tests.
- N2: `schema.py` — `api_recipe` skills, `recordings` (TTL 14 d), `api_call` edges; applied to Atlas.
- N3: `network/har.py` — filter + redact HAR, stable `<token:N>`/`<email:N>`/`<secret:N>` placeholders shared with the task text; 8 tests.
- N4: `network/learner.py` — `learn_recipe` (1 call + 1 repair), `fill_params`; 7 tests + live model check.
- N5: `recipes.py` — RecipeStore (save/supersede/record_result/find_for_task/watch/call_chain); 5 Atlas tests.
- C4: validation in error mode on Atlas; AGENTS.md updated.
- WebShop eval: `choose` steps + learner support; learned recipe vs browsing on 105 human tasks: 0.714 vs 0.519 avg score, 52% vs 36% success, 8.6 s vs 22.5 s, 2.6 vs 7.9 model calls. See docs/webshop-eval.md.
