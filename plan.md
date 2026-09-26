# Plan: Recursive Computer-Use Harness

## Goal

Extend the current computer-use agent so that every computer-use action —
specifically every click (and other pointer/keyboard actions) — is persisted to
a MongoDB database. This builds an auditable, queryable history of what the
agent did on the desktop. A GUI to browse and replay this history comes later.

The word "recursive" here means the harness feeds its own past actions back into
future runs: the action log is not just an audit trail, it becomes context the
agent can learn from and reuse.

## Where we are today

The existing system (see `design-decision.md`) is a bounded agentic loop:

- `agent.py` — the loop. Calls the model via Chat Completions with a single
  `exec_py(code)` tool, runs the returned code, feeds results back, repeats up
  to `MAX_TURNS = 30`.
- `sandbox.py` — a persistent Python namespace where model-generated code runs
  against `pyautogui`. Helpers `log()` and `display()` are injected;
  `pyautogui` and `time` are pre-imported.
- `auth.py` — resolves credentials (Codex proxy first, `OPENAI_API_KEY`
  fallback).
- `__init__.py` — CLI entry point.

Key fact for this plan: the model does not call discrete `click`/`type` tools.
It writes arbitrary Python that calls `pyautogui.click(...)`, `pyautogui.write(...)`,
etc. inside the sandbox. So to capture "where we click," we must intercept
`pyautogui` calls at the sandbox boundary, not at the tool-call boundary.

## Design overview

Three new concerns, each isolated:

1. **Action capture** — intercept `pyautogui` actions inside the sandbox and
   emit structured `Action` records.
2. **Persistence** — write those records (plus a screenshot reference and run
   metadata) to MongoDB.
3. **Recursion / replay + GUI (future)** — feed past actions back into new runs,
   and eventually a GUI to browse/replay.

### 1. Action capture

Rather than parsing the model's code, wrap `pyautogui` in the sandbox namespace
with a thin proxy that records each call before delegating to the real function.

- Add a `RecordingPyAutoGUI` wrapper (or a set of wrapped functions) injected
  into the sandbox namespace in place of the raw `pyautogui` module.
- Wrapped functions of interest: `click`, `doubleClick`, `rightClick`,
  `moveTo`, `dragTo`, `write`/`typewrite`, `press`, `hotkey`, `scroll`.
- Each wrapped call produces an `Action`:

  ```python
  @dataclass
  class Action:
      run_id: str          # groups actions from one harness run
      turn: int            # which agentic turn produced it
      seq: int             # order within the run
      kind: str            # "click", "write", "scroll", ...
      x: int | None        # pointer x at time of action (resolved via pyautogui.position())
      y: int | None        # pointer y
      args: dict           # normalized call arguments
      screenshot_ref: str | None  # id/path of the screenshot at this step
      ts: datetime         # timestamp (UTC)
  ```

- For clicks specifically, resolve `(x, y)` from the call args if given,
  otherwise from `pyautogui.position()`.

Capture must be resilient: a logging failure must never break the desktop
action. Wrap the record-emit in try/except and degrade to a warning.

### 2. Persistence (MongoDB)

- Add `pymongo` to `pyproject.toml` dependencies.
- New module `store.py`:
  - `ActionStore` class wrapping a `MongoClient`.
  - Config via env vars (loaded through existing `.env` flow):
    `MONGODB_URI` (default `mongodb://localhost:27017`),
    `MONGODB_DB` (default `recursive_computer_use`).
  - Collections:
    - `runs` — one document per harness invocation: `run_id`, `prompt`,
      `model`, `started_at`, `finished_at`, `status`, `final_text`.
    - `actions` — one document per captured action (the `Action` above).
  - Methods: `start_run(prompt, model) -> run_id`, `record_action(action)`,
    `finish_run(run_id, status, final_text)`.
  - Indexes: `actions.run_id`, `actions.ts`.
- Make persistence optional and non-fatal: if Mongo is unreachable, log a
  warning and continue (the harness should still drive the desktop). A
  `--no-log` CLI flag can disable it entirely.

Screenshots: store the base64 PNGs out-of-band (GridFS or on-disk under a
`runs/<run_id>/` dir) and keep only a `screenshot_ref` in the action doc, to
keep documents small. Decide GridFS vs disk during implementation; default to
disk for simplicity first.

### 3. Wiring into the loop

- `agent.run(...)` creates/receives an `ActionStore`, calls `start_run` before
  the loop, passes `run_id` + current `turn` into the `Sandbox` so captured
  actions are tagged, and calls `finish_run` in a `finally`.
- `Sandbox` gains a reference to the store (or a callback) and the current
  `run_id`/`turn`, set per turn by the agent.
- CLI (`__init__.py`) gains `--no-log` and optionally `--mongodb-uri`.

### 4. Recursion / replay (future)

- On a new run, optionally query recent `actions`/`runs` for the same or a
  similar prompt and inject a summary into the system/user context so the model
  can reuse prior successful action sequences.
- A replay utility that re-issues logged actions via `pyautogui` without the
  model, for deterministic re-runs and testing.

### 5. GUI (future)

- Read-only first: a small web UI (FastAPI + a static frontend, or Streamlit)
  that lists `runs`, shows the action timeline for a run, and renders each
  step's screenshot with the click coordinates overlaid.
- Later: trigger replay and start new runs from the GUI.

## Milestones

- **M1 — Persistence skeleton.** Add `pymongo`, `store.py` with
  `ActionStore` (`start_run`/`record_action`/`finish_run`), env-based config,
  graceful degradation when Mongo is down. Unit-test against a local Mongo (or
  `mongomock`).
- **M2 — Action capture.** `RecordingPyAutoGUI` wrapper in the sandbox emitting
  `Action` records for clicks + core actions. Verify records are well-formed.
- **M3 — Wire into the loop.** `agent.run` starts/finishes runs, tags turns,
  actions land in Mongo end-to-end. `--no-log` flag.
- **M4 — Screenshot storage + refs.** Persist screenshots (disk first), store
  `screenshot_ref` on actions.
- **M5 — Recursion.** Feed prior actions back into new runs; add a replay
  utility.
- **M6 — GUI.** Read-only run/action browser with screenshot + click overlay.

## Open questions

- Screenshot storage: GridFS vs on-disk? (Leaning on-disk for M4, revisit.)
- Do we capture every `pyautogui` call, or only a curated set of "meaningful"
  actions (clicks, types, scrolls)? Curated set first.
- Recursion granularity: reuse whole action sequences, or just surface them as
  hints to the model? Start with hints.
- GUI stack: FastAPI + static vs Streamlit? Decide at M6.

## Non-goals (for now)

- Real sandboxing of model code (the existing known trade-off stands).
- Multi-user / remote deployment.
- Authentication on the GUI.
