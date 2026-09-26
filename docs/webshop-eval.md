# WebShop eval: learned API recipe vs. page-by-page browsing

Run on 2026-09-26. Script: `scripts/webshop_eval.py`. Raw rows:
`.recordings/webshop_eval_20260926_150359.json` (local, not committed).

## Question

Can the harness learn a site's request chain from **one** recorded purchase,
then complete **new** shopping tasks by replaying requests, as well as or
better than a model that browses page by page?

## Setup

- **Benchmark:** [WebShop](https://github.com/princeton-nlp/WebShop), run
  locally. Every purchase is scored 0 to 1 by WebShop's own reward function
  against the task's hidden goal. We never grade ourselves.
- **Tasks:** 105 held-out human-written instructions (`fixed_1` ..
  `fixed_105`) on the 10k-product subset. `fixed_0` is the demonstration.
- **Model:** `gpt-5.6-terra` via the Codex proxy, for both arms.
- **Recipe arm:** one headless recording of `fixed_0` → `learn_recipe` →
  `POST /{session}` (search) → choose product → `GET item_page` → choose
  options → `POST /done/...`. Per task: `fill_params` (1 call) + up to 2
  `choose` calls. No browser.
- **Baseline arm:** the model sees each page as text (up to 6,000 chars) and
  numbered actions (links, option radios, form buttons, search) and picks one
  per turn, up to 15 turns. Same pages, same model, no recipe.
- Each arm uses its own server session per task (`recipe_fixed_N`,
  `baseline_fixed_N`); WebShop maps both to goal N.

## Results (n = 105)

| Arm | Avg score | Success (score = 1) | Score ≥ 0.5 | Score = 0 | Sec/task (mean / median) | Model calls/task |
|---|---|---|---|---|---|---|
| **Learned recipe** | **0.714** | **52%** | 79 | 4 | **8.6 / 7.9** | **2.6** |
| Baseline browsing | 0.519 | 36% | 61 | 38 | 22.5 / 14.5 | 7.9 |

Per task: recipe better on 44, baseline better on 11, tie on 50.

The recipe arm is about 2.6× faster, uses 3× fewer model calls, and scores
higher. Most baseline zeros are runs that hit the 15-turn limit while paging
through results without buying.

## Caveats (say these out loud)

- **Not comparable to published WebShop numbers.** Search is a pure-Python
  BM25 over the same document text instead of the original Lucene index, and
  the product set is the 10k subset.
- **Our baseline is a simple agent we wrote,** not the paper's IL/RL agents or
  a tuned ReAct agent. A stronger browsing agent would narrow the gap.
- **One run, no variance estimate.** Model sampling varies between runs.
- **The recipe only looks at the first results page and never re-searches.**
  It wins here because WebShop's flow is fixed; it would need relearning
  (or a fallback) on sites with branching flows.
- The recipe was learned from one demonstration; the demo itself scored 1.0.

## Reproduce

```bash
scripts/setup_webshop.sh                                   # once
(cd ../webshop && .venv/bin/python -m web_agent_site.app --attrs) &
npx codex-as-api &                                         # model proxy
uv run python scripts/webshop_eval.py --learn              # record + learn
uv run python scripts/webshop_eval.py --tasks 1-105 --workers 6
```
