# Recursive Computer-Use Harness

A local-first desktop agent whose task-specific harness can improve from
verified experience. Desktop actions execute locally through `pyautogui`.
MongoDB stores redacted experience summaries, local embeddings, evaluation
metrics, and versioned policies. Raw typed content and screenshot bytes are not
persisted to MongoDB.

## What changes across runs

1. The runtime loads the latest accepted policy, or a candidate awaiting trial.
2. A local verifier measures task success and safety outcomes.
3. A verified failure is summarized, embedded locally, and stored in MongoDB.
4. Atlas Vector Search retrieves similar lessons.
5. The engine proposes a constrained candidate policy.
6. The next verified run promotes that policy only if success improves without
   increasing wrong clicks, wrong-field entries, policy violations, or tool
   access.

Policies may change rules, screenshot cadence, action budgets, retry limits,
and the existing tool allowlist. They cannot rewrite source code, replace the
base prompt, create tools, or grant new tools.

## Setup

Requires Python 3.13+ and the dependencies in `pyproject.toml`.

```powershell
Copy-Item .env.example .env
# Edit .env with MONGODB_URI and either OPENAI_API_KEY or Codex proxy access.
```

For Atlas, create the collection indexes and 64-dimensional vector index:

```powershell
python scripts/setup_atlas.py
```

When using Codex OAuth, start the compatible local proxy separately:

```powershell
npx codex-as-api
```

## Hackathon demo

Start the deterministic local form task:

```powershell
python demo/app.py
```

Open `http://127.0.0.1:8765`, or ask the agent to complete it:

```powershell
python -m recursive_computer_use `
  --task-key local-form-v1 `
  --verifier-url http://127.0.0.1:8765/api/result `
  --verbose `
  "Open http://127.0.0.1:8765 and complete the check-in using the exact values shown."
```

The first verified failure creates policy v2. Reset the form with
`POST http://127.0.0.1:8765/api/reset`, then repeat the same command. The
candidate is applied during the second run and is accepted only when the local
verifier reports improved success with no safety regression.

Start the read-only evidence dashboard:

```powershell
python dashboard/app.py
```

Open `http://127.0.0.1:8787`. It reads MongoDB when configured and falls back
to included demonstration fixtures when the database is unavailable.

## General CLI

```powershell
python -m recursive_computer_use [options] "desktop task"
```

Important options:

- `--model`: model identifier.
- `--task-key`: stable task family for scoped memory and policies.
- `--verifier-url`: localhost endpoint returning deterministic metrics.
- `--mongodb-uri` and `--mongodb-db`: persistence overrides.
- `--no-log`: disable MongoDB telemetry.
- `--no-evolve`: enforce the local fallback policy without evolution.
- `--verbose`: show turn-by-turn activity.

## Privacy and control

- Desktop execution remains local.
- Typed text is represented only by length in action telemetry.
- Prompt and final text are redacted and truncated before persistence.
- Screenshot bytes are never persisted to MongoDB. Screenshots selected by the
  agent can still be sent to the configured model endpoint for visual reasoning.
- The verifier URL is restricted to localhost.
- Model code runs with blocked file access and a restricted import allowlist.
- Policy enforcement happens outside model-generated code.
