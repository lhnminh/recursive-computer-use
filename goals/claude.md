# Claude's goals: schema, Voyage retrieval, learning layer

Read [`GOALS.md`](GOALS.md) first. It has the rules, file ownership and
Atlas facts.

Your files: `schema.py`, `learning.py`, `scripts/setup_atlas.py`,
`evolution/memory.py`, `evolution/embedding.py`, the `find_similar` call in
`evolution/runtime.py`, the learning hooks in `agent.py`, and your tests.

## C1. Schema for all collections — done

- [x] Keep our six collections (`sites`, `page_templates`, `site_map`,
      `skills`, `episodes`, `exam`).
- [x] Validators and indexes for the harness collections: `runs`, `actions`,
      `experiences`, `policies`, `evaluations`. Shapes from
      `evolution/models.py` and `store.py`. Warn mode.
- [x] `run_metrics` time series and `agent_state`.
- [x] `scripts/setup_atlas.py` calls `schema.main()`. One entry point.
- [x] Applied to Atlas twice. Second run reports "updated".
- [x] Contract for Codex written in `GOALS.md`.
- Skipped: TTL on `actions.ts`. `store.py` already creates a plain index on
  `ts`, and a TTL index on the same key conflicts. Ask Minh before changing.

## C2. Voyage embeddings everywhere — done

- [x] `autoEmbed` (`voyage-4`) search indexes in `schema.SEARCH_INDEXES`:
      `experiences.lesson`, `skills.description`, `episodes.task`,
      `page_templates.summary`. Plus `experience_text` (`$search`) for C3.
      Created or updated idempotently. All READY in Atlas.
- [x] `ExperienceMemory.find_similar(..., query_text=...)` queries Voyage.
      Falls back to the legacy vector, then recent documents.
- [x] `runtime.py` stores no hash vector and queries by text.
- [x] Test: "typed into the wrong input box" finds the focused-field lesson
      (score 0.771). "confirm control" finds "Submit button" (0.689).

## C2b. Learning layer wired into the harness — done

- [x] `learning.py`: `LearningStore` writes one episode per run, upserts
      the site, turns each policy rule into a skill, `$inc` uses and wins on
      verified runs, recomputes lift with one `$facet` + `$merge`
      aggregation, and retires skills with lift below -0.2 after 3 uses.
- [x] `agent.py` hooks: skills synced before the loop, LLM calls and
      tokens counted, episode written in `finally`. Outcome is `success` or
      `failure` only when the verifier says so, else `unverified`.
- [x] `tests/test_learning_atlas.py`: 3 tests on a scratch database, pass.
      Run: `RCU_ATLAS_TESTS=1 uv run python -m unittest tests.test_learning_atlas -v`.

## C3. Hybrid ranking with `$rankFusion` — done

- [x] `ExperienceMemory.ranked_lessons` fuses three pipelines:
      `semantic` (Voyage on `lesson`), `keyword` (`experience_text`), and
      `useful` (experiences whose policy version produced an accepted child
      policy). `find_similar(query_text=...)` uses it first, so
      `runtime._propose_candidate` gets fused lessons automatically.
- [x] `$rankFusion` input pipelines cannot use `$lookup`, so the accepted
      parent versions are read from `policies` first, then matched.
- [x] Weights live in `memory.FUSION_WEIGHTS` (2 / 1 / 1).
- [x] Falls back to plain Voyage search, then the legacy paths.
- [x] `LearningStore.ranked_skills`: `semantic` + `keyword` (new
      `skill_text` index) + `lift`. Only skills with positive lift enter the
      `lift` pipeline: rank fusion ignores magnitudes, so a -0.1 skill would
      otherwise get the same kind of boost as a +0.6 skill.
- [x] Tests 4 and 5 in `test_learning_atlas.py`: the proven lesson and the
      positive-lift skill rank first over near-identical rivals.

## C4. Turn on strict validation (last, ~15 min)

- [ ] After real runs, list documents that fail each validator.
- [ ] Fix the validators, then set `VALIDATION_ACTION = "error"`.
- [ ] Test: a policy with `status: "magic"` is rejected.

## Done log

- C1, C2, C2b: uncommitted as of this writing. See `git status`.
