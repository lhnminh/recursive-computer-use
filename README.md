# Recursive Computer-Use Harness

**🏆 3rd place — MongoDB Harness Engineering Hackathon**

The hackathon demonstration ran through the **WebUI frontend**, where users
submit tasks and see the agent's responses in a local browser chat.

Recursive Computer-Use is a local-first agent that learns reusable workflows
from successful web navigation. It completes a task, captures the steps, and
uses that experience when the task comes up again. When a saved workflow fails,
the agent falls back to navigation and can learn a replacement.

MongoDB stores the reusable workflows, task memories, and evaluation evidence.
Atlas Search and Vector Search help retrieve relevant experience. Mouse and
keyboard actions run on your machine.

## How it learns

1. **Navigate.** The agent observes screenshots and operates the local browser
   or desktop to complete a task.
2. **Remember.** The desktop path saves an ordered action guide. The separate
   network path learns a parameterized API recipe from a recorded browser flow.
3. **Reuse.** On a matching task, the harness tries the saved guide or recipe
   before navigating from scratch.
4. **Recover.** If replay fails, it falls back to navigation. A completed run
   can refresh the guide; a verified browser recording can teach a new recipe.
5. **Measure.** Deterministic verification supplies success and failure evidence
   for recipe outcomes and constrained policy changes.

```text
Navigate → Capture → Save a workflow → Retrieve → Replay → Verify
   ▲                                               │
   └──────────── Fall back and relearn on failure ──┘
```

Guides are tied to a site, task, and machine environment. API recipes replay
same-site requests with new task parameters. These are separate execution paths;
the desktop command does not automatically switch to API replay.

A guide can be saved after a completed run without a verifier, so a saved guide
alone is not proof of success. With a verifier configured, failed runs do not
produce guides. Policy evolution uses only verified metrics: a candidate must
improve on its accepted parent without a safety regression before promotion.
The model weights remain unchanged; learning updates the surrounding harness.

## Current capabilities

- **WebUI task control:** submit tasks through a local browser chat and watch
  the agent operate your desktop.
- **Learn navigation once:** capture completed workflows as ordered action
  guides for a matching site, task, and machine environment.
- **Faster repeat tasks:** replay saved guides to reduce repeated model
  reasoning and navigation from scratch.
- **Recover and refresh:** fall back to screenshot-guided navigation when
  replay fails, then refresh the guide after a completed run.
- **Persistent workflow memory:** keep guides in MongoDB with a local JSON
  fallback so they can be reused across sessions.
- **Measure results:** record sanitized action telemetry and use an optional
  local verifier to assess success, action count, and duration.

## Repository layout

```text
src/recursive_computer_use/
  __init__.py              CLI argument parsing
  __main__.py              python -m entry point
  agent.py                 model loop and evolution integration
  auth.py                  Codex proxy or API-key credential resolution
  chat_runtime.py          chat options, validation, and safe error text
  webui.py                 stdlib http.server computer-use chat surface
  sandbox.py               restricted execution and policy enforcement
  store.py                 MongoDB run and action persistence
  schema.py                validators, indexes, search indexes for all collections
  learning.py              episodes, sites, skills with lift and retirement
  recipes.py               API recipe retrieval, lifecycle, and change stream
  verification.py          localhost verifier client
  evolution/
    embedding.py           legacy hash vectors (unused by the runtime)
    evaluator.py           deterministic candidate promotion gate
    memory.py              Atlas Vector Search and fallback retrieval
    models.py              validated persistence contracts
    policy.py              constrained policy mutation and repository
    runtime.py             two-run evolution coordinator
  network/
    capture.py             headed Chromium HAR recording
    har.py                 filtering and redaction
    learner.py             HAR-to-recipe model call
    recipe.py              validated recipe contract
    runner.py              scoped HTTP replay and verification
    loop.py                recipe-first fallback and relearning
demo/
  app.py                   deterministic local form and verifier
dashboard/
  app.py                   read-only evidence dashboard
  fixtures.json            offline before/after demonstration
scripts/
  setup_atlas.py           runs schema.py (collections and search indexes)
  network_demo.py          learn, replay, break, and heal judge demo
tests/
  test_recording.py        action interception and sanitization
  test_store.py            persistence and run lifecycle
  test_evolution.py        memory, policy, and evaluation behavior
  test_learning_atlas.py   opt-in Atlas integration tests (RCU_ATLAS_TESTS=1)
```

## Requirements

- Windows, macOS, or Linux desktop session accessible to `pyautogui`.
- Python 3.13 or newer.
- A vision-capable OpenAI-compatible model endpoint.
- MongoDB Atlas for the complete demo, or local MongoDB for telemetry and the
  non-vector fallback.
- Node.js only when using the optional `codex-as-api` proxy.

The app controls the real desktop. Run it only in a disposable or understood
environment, and keep `pyautogui`'s fail-safe enabled.

## Installation

From the repository root in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
Copy-Item .env.example .env
```

If `py -3.13` is unavailable, use any installed Python version that satisfies
the `>=3.13` requirement.

## Configuration

`.env` supports:

```dotenv
OPENAI_API_KEY=sk-...
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=recursive_computer_use
MONGODB_DASHBOARD_URI=mongodb://readonly-user:password@localhost:27017
```

Do not commit `.env`. It is excluded by `.gitignore`.

Use a read-only MongoDB user for `MONGODB_DASHBOARD_URI`. The dashboard falls
back to `MONGODB_URI` for local development, but the shared write credential is
not the recommended demo configuration.

### Model authentication

Credential resolution uses this order:

1. If the local Codex authentication file contains ChatGPT OAuth credentials,
   the app uses `http://127.0.0.1:18080/v1` through `codex-as-api`.
2. Otherwise it uses `OPENAI_API_KEY` with the standard OpenAI endpoint.

Start the proxy in a separate terminal when using Codex OAuth:

```powershell
npx codex-as-api
```

If the proxy is expected but not listening, the CLI fails before starting a
desktop run and prints the startup command.

The web chat (`recursive-computer-use-chat`) is more convenient: when
credentials resolve to Codex OAuth and the proxy is not already running, it
starts `npx codex-as-api` for you, waits for it, then serves the site, and
stops the proxy again on exit. With `OPENAI_API_KEY` set, no proxy is started.
This requires Node.js on `PATH`; otherwise start the proxy manually.

## WebUI frontend

The primary product surface is a simple web chat that runs on the local
machine. It uses only the Python standard library, no front-end framework:

```powershell
recursive-computer-use-chat
```

Alternatively:

```powershell
python -m recursive_computer_use.webui
```

Open `http://127.0.0.1:8600`, enable **Arm desktop**, describe a task, and select
**Run task**. Each submitted task starts one bounded run through
the same `agent.run` path used by the CLI. Set `RCU_WEB_HOST` or `RCU_WEB_PORT`
to override the bind address or port.

For the local computer-use demo, start `python -m demo.app` in another
terminal. The conference check-in page is at `http://127.0.0.1:8765/`.

The chat surface:

- binds to `127.0.0.1`, so another computer cannot remotely trigger mouse or
  keyboard control;
- allows only one desktop task at a time, even when several browser tabs are
  open;
- passes the submitted task text to the same runtime prompt used by the
  terminal command;
- never displays or accepts MongoDB credentials, which remain in `.env`;
- keeps chat history in the current browser tab;
- uses existing sanitized telemetry and verifier-only promotion rules.

The frontend uses the runtime's logging and evolution defaults. Its current
chat form does not expose a verifier setting, so runs without a verifier are
recorded as unverified and cannot promote a candidate policy. The runtime's
optional verifier must use plain HTTP on `localhost`, `127.0.0.1`, or `::1`.

### MongoDB modes

Local MongoDB works for:

- runs and actions;
- experiences and policies;
- candidate evaluations;
- recent task-memory fallback.

MongoDB Atlas adds `$vectorSearch` with Automated Embedding: Atlas embeds
text fields with Voyage `voyage-4` on write and embeds the query text on
search. The harness computes no vectors.

Create the collections, validators, and indexes after setting an Atlas URI:

```powershell
python scripts/setup_atlas.py
```

The script runs `recursive_computer_use.schema`. It is idempotent and
creates or updates:

- every collection with a `$jsonSchema` validator (error mode) and indexes;
- the `run_metrics` time series collection and `agent_state`;
- Voyage `autoEmbed` vector indexes: `experiences.experience_auto`
  (`lesson`), `skills.skill_auto` (`description`), `episodes.episode_auto`
  (`task`), `page_templates.template_auto` (`summary`);
- the `experiences.experience_text` search index for keyword retrieval.

See `schema.py` and the **Search indexes** section of `AGENTS.md`.

Atlas search indexes provision asynchronously. Until the index is available,
retrieval safely falls back to recent memories for the same task key.

## Coordinate guide replay

Run with `--verbose` to see turn-by-turn activity. Guide lookup, replay, and
learning are enabled by default:

```bash
uv run recursive-computer-use --verbose "your task here"
```

The agent looks for a matching guide before navigating. It replays a saved
guide when available; otherwise, it navigates from screenshots and saves
successful actions. If replay fails, it falls back to screenshot-guided
navigation and replaces the guide after a successful run. Use `--no-guides` to
disable guide lookup, replay, and capture. `--guides` and `--replay-guides` are
available as explicit reminders.

Guides are stored in MongoDB's `guides` collection in the database configured
by `MONGODB_DB` (default `recursive_computer_use`) and in a local JSON fallback
at `~/.recursive_computer_use/guides.json`. Set `MONGODB_URI` to your MongoDB
connection string. If MongoDB is unavailable, the run completes and saves the
guide locally. Private LinkedIn text is stored as a placeholder; replay fills
the site and requested text from the current prompt.

For example, to draft a post on Reddit:

```bash
uv run recursive-computer-use --verbose "On Reddit, draft a post saying hello"
```

The task key currently uses the prompt text, so repeat the same prompt to reuse
the guide. A guide stores its site, task and environment key, ordered action
steps, model, timestamps, and success/failure counters. It does not store the
full prompt or action history; those remain in the existing `runs` and
`actions` collections.

## Network recipe path

The network path turns one local browser demonstration into a reusable API
recipe. Raw HAR files remain local and are ignored by Git. Atlas receives only
redacted recipes, recording metadata, endpoint shapes, and measured outcomes.

Learn from a human demonstration:

```powershell
python -m recursive_computer_use learn `
  --url http://127.0.0.1:8765 `
  --task-key network-checkin `
  --task "Check in Ada Lovelace with email ada@example.com and city New York."
```

Chromium opens visibly. Complete the task once. The harness records the flow,
redacts it, learns a candidate recipe, validates it, and stores it in Atlas.

Run a new task recipe-first:

```powershell
python -m recursive_computer_use do `
  --site 127.0.0.1:8765 `
  --task-key network-checkin `
  "Check in Grace Hopper with email grace@example.com and city New York."
```

When `do` finds no working recipe, it uses a local headless browser agent to
record a verified task and teach a replacement. For the `learn` command, the
default opens a visible browser and leaves control with the person; add
`--agent` only when you intentionally want computer use to drive that recording.

For the complete break-and-heal presentation, run:

```powershell
python scripts/network_demo.py
```

The script starts the demo server, learns v1, replays three guests, enables the
redesign, records the failed v1, learns v2, and verifies the healed recipe. It
uses the disposable `rcu_demo` database and drops it during cleanup. Pass
`--agent` only to opt into computer use for both recording phases.

## Quick start without evolution

Start the model proxy if needed, then run:

```powershell
python -m recursive_computer_use `
  --no-log `
  --no-evolve `
  --verbose `
  "Take a screenshot and describe what is visible. Do not click anything."
```

`--no-log` prevents MongoDB access. `--no-evolve` uses the conservative local
policy without reading or writing policy memory.

## Full hackathon demo

The hackathon run used the WebUI frontend. Start the local service with
`recursive-computer-use-chat`, then interact through the browser:

1. Open `http://127.0.0.1:8600` and enable **Arm desktop**.
2. Describe a web task in the chat and select **Run task**.
3. Watch the agent navigate on the local desktop and read its response in chat.
4. Repeat a matching task to exercise saved guide replay. If replay fails, the
   agent falls back to navigation and can refresh the guide.

Guide reuse demonstrates workflow memory. Verified policy improvement requires
the separate verifier setup below.

## Optional verified policy demo

This developer workflow uses three terminals to demonstrate deterministic
verification and policy promotion.

### Terminal 1: local task and verifier

```powershell
.\.venv\Scripts\Activate.ps1
python demo/app.py
```

Endpoints:

- Task page: `http://127.0.0.1:8765`
- Verified metrics: `http://127.0.0.1:8765/api/result`
- Reset: `POST http://127.0.0.1:8765/api/reset`

The form contains a one-time delayed focus change. An agent that clicks and
types without rechecking focus can place the name in the email field. The page
records success, wrong-field entries, wrong clicks, actions, and duration.

### Terminal 2: agent run

```powershell
.\.venv\Scripts\Activate.ps1
python -m recursive_computer_use `
  --task-key local-form-v1 `
  --verifier-url http://127.0.0.1:8765/api/result `
  --verbose `
  "Open http://127.0.0.1:8765 and complete the check-in using the exact values shown."
```

The first verified failure under policy v1 creates candidate v2. Before the
second run, reset the task:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/reset
```

Run the identical agent command again. Candidate v2 is loaded for the trial.
It is accepted only if success improves and wrong clicks, wrong-field entries,
policy violations, and tool access do not regress.

### Terminal 3: evidence dashboard

```powershell
.\.venv\Scripts\Activate.ps1
python dashboard/app.py
```

Open `http://127.0.0.1:8787`. The page refreshes every five seconds and shows:

- baseline and candidate metrics;
- the retrieved lesson;
- the policy version and rule diff;
- the accept or reject decision;
- the persistence source, Atlas or offline fixtures;
- the privacy boundary.

If Atlas is unavailable or empty, the dashboard uses `dashboard/fixtures.json`
so the presentation surface still works. Clearly tell judges when fixtures are
being shown; the source label appears at the top of the page.

## CLI reference

```text
recursive-computer-use [options] PROMPT
```

| Option | Default | Purpose |
|---|---|---|
| `PROMPT` | required | Natural-language desktop task |
| `--model` | `gpt-5.6-terra` | OpenAI-compatible model identifier |
| `--verbose`, `-v` | off | Print model turns, screenshots, and evolution results |
| `--mongodb-uri` | environment or localhost | Override MongoDB connection URI |
| `--mongodb-db` | environment or `recursive_computer_use` | Override database name |
| `--no-log` | off | Disable run and action persistence |
| `--task-key` | `general-desktop` | Scope memories and policies to a task family |
| `--verifier-url` | none | Local endpoint supplying deterministic metrics |
| `--no-evolve` | off | Disable persistent policy evolution |
| `--policy-version` | latest candidate or accepted | Load an exact stored version for replay evidence |
| `--observe-only` | off | Record verified metrics without changing policy state |

Use a stable `--task-key` for repeated variants of one workflow. Do not reuse a
task key across unrelated applications or objectives, because their memories
and policies would become mixed.

## Evolution lifecycle

### Initial policy

For a new task key, the engine stores policy v1 with conservative defaults:

- inspect before acting;
- use short action groups;
- stop rather than guess;
- maximum three actions without a screenshot;
- 40-action budget;
- two retries;
- allowlisted desktop primitives only.

### Verified failure

After a verifier reports failure:

1. The engine derives failure tags such as `wrong_field`, `wrong_click`, or
   `policy_violation`.
2. It stores a redacted summary and lesson. Atlas embeds the lesson with Voyage.
3. It retrieves up to three similar experiences.
4. It converts retrieved lessons into a constrained candidate policy.
5. The candidate remains untrusted until another verified run.

### Candidate trial

The next run for that task key prefers the pending candidate. At completion,
the evaluator compares it with the latest experience from its accepted parent.

Acceptance requires:

- strictly higher success rate;
- no increase in wrong clicks;
- no increase in wrong-field entries;
- no increase in policy violations;
- no expansion of tool access.

Before a candidate is stored, a deterministic regularization critic also
requires that it fit the lineage's edit budget and contain no protected or
task-specific literals. The budget shrinks from three independent edits for a
young lineage to one edit for a mature lineage. This keeps later changes small
enough to attribute to one hypothesis.

The replay gate separates task evidence into `evolve`, `regression`, and
`holdout` splits. Promotion uses only evolve and regression cases. Holdout
results are stored and reported but deliberately excluded from selection so
repeated decisions do not train on the exam.

After testers collect baseline and candidate metrics for a pending policy,
copy `examples/replay_evidence.example.json`, fill in the evidence, and run:

```powershell
python scripts/evaluate_replay.py examples/replay_evidence.example.json
```

Collect each task pair without prematurely promoting the candidate:

```powershell
# Accepted parent evidence
python -m recursive_computer_use `
  --task-key local-form-v1 --policy-version 1 --observe-only `
  --verifier-url http://127.0.0.1:8765/api/result `
  "Complete the current replay task."

# Pending candidate evidence
python -m recursive_computer_use `
  --task-key local-form-v1 --policy-version 2 --observe-only `
  --verifier-url http://127.0.0.1:8765/api/result `
  "Complete the current replay task."
```

Reset or switch the deterministic task between runs. Copy the resulting
`verified_metrics` and episode token counts into the evidence file. Observe-only
runs never propose, accept, or reject a policy.

The command checks lineage, tool non-expansion, evolve improvement, regression
safety, and token cost. It writes the complete suite to `replay_evaluations`,
then atomically records the evaluation and changes the pending candidate to
`accepted` or `rejected`. The evidence file contains metrics only, not task
answers or prompts.

Failed candidates are marked `rejected`. Successful candidates are marked
`accepted`; the highest accepted version becomes the next baseline.

## MongoDB data model

### `runs`

One document per CLI invocation:

```json
{
  "run_id": "uuid-like hex",
  "prompt_summary": "redacted and truncated",
  "prompt_sha256": "stable digest",
  "model": "gpt-5.5",
  "status": "running | completed | failed | interrupted",
  "started_at": "UTC datetime",
  "finished_at": "UTC datetime",
  "final_summary": "redacted and truncated",
  "task_key": "local-form-v1",
  "policy_version": 2,
  "verified_metrics": {},
  "evolution_result": {}
}
```

### `actions`

Ordered sanitized desktop events:

```json
{
  "run_id": "...",
  "turn": 2,
  "seq": 7,
  "kind": "click | write | scroll | policy_violation | ...",
  "x": 640,
  "y": 420,
  "args": {"button": "left"},
  "screenshot_ref": null,
  "ts": "UTC datetime"
}
```

Typing stores `text_length`, never the text. Character key presses are stored
as `[character]`; control keys such as `tab` or `ctrl` may be retained.

### `experiences`

```json
{
  "task_key": "local-form-v1",
  "outcome": "success | failure",
  "failure_tags": ["wrong_field"],
  "summary": "redacted verifier summary",
  "lesson": "verify the focused field before typing",
  "policy_version": 1,
  "metrics": {},
  "created_at": "UTC datetime"
}
```

### `policies`

```json
{
  "task_key": "local-form-v1",
  "version": 2,
  "parent_version": 1,
  "status": "candidate | accepted | rejected",
  "rules": ["..."],
  "limits": {
    "max_actions_without_screenshot": 1,
    "action_budget": 40,
    "retry_limit": 2,
    "tool_allowlist": ["click", "type", "screenshot"]
  },
  "reason": "redacted explanation",
  "created_at": "UTC datetime"
}
```

### `evaluations`

Stores baseline and candidate metrics, the decision, and the deterministic
reason for that decision. Atlas deployments write the evaluation and candidate
status transition in one transaction. Each new record also contains hashes of
the baseline policy, candidate policy, and complete evaluation evidence. The
hashes make later mutation detectable, but they are not signatures and do not
replace separate database credentials for a hostile-writer threat model.

### `replay_evaluations`

Stores the full per-task baseline/candidate evidence for a replay suite,
including split labels and token counts. Holdout cases remain in this audit
record but never affect the promotion decision.

### API recipe skills and recordings

Recipes live in `skills` with `kind: "api_recipe"`, versioned candidate,
active, and retired states, parameter definitions, scoped request steps, use
counts, wins, lift, and failure streak. First verified replay success promotes
a candidate. Two consecutive verified failures retire an active recipe.

`recordings` stores only the site, redacted task summary, HAR digest, endpoint
shapes, source, and recipe reference. Raw HAR content, cookies, request bodies,
and tokens never enter MongoDB.

## Privacy and safety boundaries

The project follows a local-first hybrid design:

- Mouse and keyboard execution occurs on the local machine.
- Raw typed content is excluded from action telemetry.
- Prompt and final response text are redacted and truncated before MongoDB
  persistence.
- Screenshot bytes are never persisted to MongoDB.
- Screenshots explicitly selected by the agent can still transit to the
  configured model endpoint for visual reasoning.
- Embeddings are generated by Atlas with Voyage AI. Only redacted, bounded
  text fields are embedded.
- The verifier client accepts only `http://localhost`, `127.0.0.1`, or `::1`.
- Policy changes cannot rewrite source, replace the base prompt, create tools,
  or add tools absent from the parent policy.
- Telemetry failures are non-fatal and must never block a legitimate desktop
  action.
- Policy violations are blocked before delegation to real `pyautogui`.

### Important limitation

The restricted Python namespace is a guardrail, not a hardened operating-system
sandbox. It blocks common file APIs and non-allowlisted imports, but in-process
Python should not be treated as a security boundary against an adversarial
model or prompt. Run sensitive workflows only with additional OS isolation and
human supervision.

## Development checks

Run from the repository root after installing dependencies:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src demo dashboard scripts
git diff --check
```

The tests use fakes and must not move the real mouse, type on the real keyboard,
or require a live MongoDB deployment.

## Troubleshooting

### `Cannot reach the local Codex proxy`

Start `npx codex-as-api` in another terminal, or remove stale Codex OAuth state
and use `OPENAI_API_KEY`.

### MongoDB unavailable

The agent continues without persistence. Confirm `MONGODB_URI`, Atlas network
access, credentials, and TLS settings. Use `--no-log` when persistence is not
needed.

### Vector search always uses fallback

Run `python scripts/setup_atlas.py`, wait for the Atlas index to become active,
and verify `experience_auto` on `experiences` shows `READY` in
`list_search_indexes()`.

### Dashboard shows fixtures

The dashboard could not read non-empty MongoDB collections. Check its terminal,
the `.env` values inherited by the process, and whether completed verified runs
exist.

### Actions stop with a screenshot-policy violation

The active policy requires a screenshot before another action. The model should
call `display(pyautogui.screenshot())`, inspect the returned image, and then
continue.

### `ModuleNotFoundError`

Activate `.venv` and run `python -m pip install -e .` from the repository root.

## Demo narrative

The concise judge-facing explanation is:

> The desktop agent remains local. A deterministic verifier detects a failure.
> MongoDB Atlas retrieves similar redacted experiences. The harness creates a
> constrained policy candidate, applies it on the next matching run, and accepts
> it only when measured success improves without a safety regression.

That claim should be made only when the live dashboard is reading real MongoDB
data. When it shows offline fixtures, describe them as a presentation fallback.
