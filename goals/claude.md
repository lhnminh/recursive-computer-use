# Claude's goals: recipe contract, learner, recipe store

Read [`GOALS.md`](GOALS.md) first. It has the pivot, the recipe contract,
checkpoints, rules and file ownership.

Your files: `network/__init__.py`, `network/recipe.py`, `network/har.py`,
`network/learner.py`, `recipes.py`, `schema.py`, `learning.py`,
`evolution/memory.py`, `scripts/setup_atlas.py`, and your tests.

Phase 1 (C1 to C3: schema, Voyage, learning layer, `$rankFusion`) is done.

## N1. Recipe type (first, ~30 min, unblocks Codex)

- [ ] Create `network/__init__.py` and `network/recipe.py`.
- [ ] `Recipe`, `Step`, `Extract`, `Param` dataclasses matching the JSON in
      `GOALS.md`. `from_dict`, `to_dict`, `validate()`.
- [ ] `validate()` rejects: unknown `extract.from`, a `{{var}}` that no param
      or earlier extract defines, a step URL whose host is not
      `scope.site`, methods other than GET/POST/PUT/PATCH/DELETE, cookie or
      authorization headers, more than 20 steps.
- [ ] `render(template, values)` helper for `{{var}}`: shared by the runner.
- [ ] Unit tests with fakes. Push, then tick the box so Codex knows.

## N2. Schema for recipes and recordings (~20 min)

- [ ] `skills` validator accepts `kind: "api_recipe"` with `params`,
      `steps`, `verify`. Keep other kinds valid.
- [ ] New `recordings` collection: metadata only. `{site, task, task_key,
      har_sha256, exchange_count, endpoints: [{method, path, status}],
      source: "human" | "agent", created_at}`. TTL 14 days.
- [ ] `site_map` edges from recipes: `from` step path → `to` step path,
      `via` the extracted var. `$graphLookup` shows the call chain.
- [ ] Run `schema.py` against Atlas.

## N3. HAR reader and redaction (~45 min)

- [ ] `network/har.py`: `load_exchanges(har_path)` returns `Exchange`
      records for same-site API calls only. Drop images, CSS, fonts, JS,
      analytics hosts, and `OPTIONS`.
- [ ] Redact: cookie and authorization values, `set-cookie`, tokens in
      headers, emails and secrets in bodies (reuse `redact_text` patterns).
      Keep structure: header names, JSON keys, value types, short values.
- [ ] Mark redacted token values with a stable placeholder like
      `<token:1>`, the same placeholder wherever the same value appears, so
      the learner can link a response value to a later request.
- [ ] Unit tests with a small hand-written HAR fixture.

## N4. Learner (~60 min)

- [ ] `network/learner.py`: `learn_recipe(har_path, task, *, site,
      task_key)`. One model call (`gpt-5.6-terra` via `auth.resolve`) with
      the redacted exchanges and the task text. Output must be JSON for
      `Recipe`. Validate; on failure, one repair call with the error.
- [ ] Prompt rules: pick only the calls needed for the task; values the
      user typed become `params`; values from earlier responses become
      `extract` + `{{var}}`; never copy a token value.
- [ ] `fill_params(recipe, task)`: one small model call that maps the task
      text to param values. Returns only declared param names.
- [ ] Test with a fake OpenAI client and the fixture HAR.
- [ ] Live test against Codex's demo API once X6 is in: record → learn →
      Codex's runner succeeds.

## N5. Recipe store (~60 min)

- [ ] `recipes.py`: `RecipeStore(db)` per the contract in `GOALS.md`.
- [ ] `find_for_task`: `$rankFusion` over Voyage on `description`, keyword
      on `description`, and positive `lift`, filtered to
      `kind: "api_recipe"`, live status, and `scope.site` when given.
- [ ] `record_result`: writes an episode (`learning.py`), `$inc` uses/wins,
      lift. First verified success promotes `candidate` → `active`. Two
      failures in a row on an `active` recipe → `retired`.
- [ ] `supersede`: new version with `parent_id`, old one retired.
- [ ] `watch(on_recipe)`: change stream on `skills` for new `active`
      recipes, resume token in `agent_state`.
- [ ] Atlas test in `tests/test_recipes_atlas.py`.

## C4. Strict validation (last, ~15 min, before 9 PM freeze)

- [ ] List documents that fail each validator. Fix, then
      `VALIDATION_ACTION = "error"`.

## Done log

<!-- One line per finished task: task id, commit hash, one-line result. -->
- Phase 1: C1, C2, C2b (`5514ede`), C3 (`87d6d57`).
