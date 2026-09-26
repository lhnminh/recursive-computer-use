# Fast Web Browsing — Design

## Goal

Make the computer-use agent **fast** on sites it has visited before.

Today, every action goes through a slow perception loop: take a screenshot,
send it to the model, let the model reason about where to click, act, repeat.
That is robust but expensive — a screenshot and a full model turn per step.

The idea: once the agent has successfully completed a task on a site, **remember
the exact coordinates it clicked** and store them in MongoDB. Next time the same
task is requested on the same site, **replay those clicks directly** — no
screenshot, no visual reasoning — so it runs near-instantly.

Coordinates are an *optimization with a safety net*, not a source of truth. When
they don't apply (unknown site, different environment, layout changed), the
agent falls back to the existing free-navigation loop and re-learns.

## Model flows

Two flows, mirroring how the agent uses MongoDB.

### Flow 1 — Navigation (read path)

When the agent is about to navigate to a site, it first asks MongoDB whether we
already have a guide for that `(site, task, env)`.

```
Navigate to site
      │
      ▼
find_guide(site, task, env_fingerprint) ──► MongoDB `guides`
      │
 ┌────┴─────┐
miss        hit  (env matches this machine)
 │           │
 ▼           ▼
FREE      FAST PATH
NAVIGATION  replay stored x,y clicks directly
(model +    (no screenshots), with sparse
screenshots)  checkpoint verification
 │           │
 │      ┌────┴────┐
 │   checkpoint  checkpoint
 │     ok          fails
 │      │           │
 │      ▼           ▼
 │   continue    ABORT fast path
 │   fast          → fall back to
 │      │          FREE NAVIGATION
 │      │          + re-record
 └──────┴────┬──────┘
             ▼
        task done → distill / update guide (write path)
```

- **Fast path:** the guide's `env` matches the current machine → the sandbox
  fires `pyautogui.click(x, y)` for each step with small sleeps between. Zero
  screenshots. Near-instant.
- **Sparse checkpoint verification:** we do *not* verify every step. We verify
  only checkpoints — the first action and any step after a page transition —
  using a cheap signal (URL bar region / page title). If a checkpoint fails, we
  abort the fast path and fall back to free navigation, then re-record.
- **Free navigation:** no guide, or env mismatch, or a checkpoint failed → the
  existing screenshot + reason + act loop drives the task, and we record a fresh
  guide for this env on success.

### Flow 2 — Learning (write path)

This answers "what do we put into MongoDB."

- **Always:** raw `runs` + `actions` are logged during a run (the existing
  recording substrate in `store.py`).
- **On success:** distill the successful action sequence for the `(site, task)`
  into a clean, ordered **coordinate guide** and upsert it, keyed by
  `(site, task, env_fingerprint)`.
  - The **first** run for a `(site, task, env)` always goes through free
    navigation (the slow learning run). It captures the coordinates.
  - Run #2+ on the same machine hits the fast path.
  - A successful reuse bumps `success_count`; a checkpoint failure bumps
    `fail_count`.

## Key decisions (locked)

### 1. Coordinate guides, not just semantic hints

The whole point is speed, so guides store **actual captured coordinates**
(`x, y`) that the fast path replays directly without a screenshot. Semantic
anchors (`target_text`) are kept too, but only as a cheap verification signal
and a fallback — not the primary mechanism.

### 2. Maximized window (not true fullscreen)

The agent enforces a **maximized** browser/app window before capture and before
replay. Maximized makes window geometry deterministic (position and size are
fixed by the screen), which is what makes captured coordinates reproducible.

We deliberately chose **maximized over true macOS fullscreen** because true
fullscreen hides the URL bar, and we rely on the URL bar region for the cheap
prerequisite / checkpoint check. Maximized keeps the URL bar visible.

What maximized fixes: window position, window size, chrome offset.
What it does **not** fix: screen resolution/scaling differences, page-internal
layout shifts (cookie banners, A/B tests, auth state), scroll position. Those
are handled by env-scoping and checkpoint verification below.

### 3. Resolution is per-user → guides are per-env

Captured coordinates are only valid in the environment they were captured in.
Resolution and display scaling are properties of the user's machine, so guides
are scoped by an **env fingerprint** and looked up as
`find_guide(site, task, env_fingerprint)`.

```
env_fingerprint = f"{os}-{screen_width}x{screen_height}@{scaling}x"
# e.g. "darwin-2560x1440@2x"
```

- Same machine, same resolution → env always matches → fast path is the common
  case, and absolute coordinates are authoritative.
- Different user / resolution → **miss** → clean fallback to free navigation,
  which records that user's own coordinate guide. No bad blind clicks across
  environments.

### 4. Absolute coords are authoritative in v1; store normalized as future-proofing

Because guides are per-env and we enforce maximized, within one machine the
environment is stable. So the fast path uses **absolute `x, y`** directly and
needs no rescaling logic.

We still **store** normalized coordinates (`nx = x/width`, `ny = y/height`) on
each step. They are unused in v1 but cost almost nothing and let us add
cross-environment guide sharing later without a schema migration.

### 5. Sparse checkpoint verification (not fully blind, not fully verified)

Firing entirely blind clicks risks a stale guide clicking the wrong things.
Verifying every click would erase the speed win. The compromise: verify only
**checkpoints** — the first action and any action after a page transition —
using a cheap check (URL bar / title), and run the rest blind. On a checkpoint
mismatch, abort and fall back to free navigation.

### 6. Layer on top of existing recording, don't replace it

The raw `runs` / `actions` logging in `store.py` stays as the recording
substrate and source material. The `guides` collection is a new distilled layer
built from successful runs.

## Data model

### Existing (unchanged): `runs`, `actions`

See `store.py` — one document per harness invocation (`runs`) and one per
captured desktop action (`actions`). Actions gain env capture (below) to feed
guide distillation.

### New: `guides`

```jsonc
{
  "guide_id": "…uuid…",
  "site": "openai.com",            // normalized hostname — part of lookup key
  "task": "log in",                // short normalized intent — part of lookup key
  "env": {
    "fingerprint": "darwin-2560x1440@2x",  // the matching key
    "os": "darwin",
    "screen_width": 2560,
    "screen_height": 1440,
    "scaling": 2.0,
    "browser": "Safari"
  },
  "title": "How to log into openai.com",
  "steps": [
    {
      "seq": 1,
      "kind": "click",             // click | doubleClick | write | press | hotkey | scroll
      "x": 2290, "y": 128,         // absolute coords — the fast path uses these
      "nx": 0.894, "ny": 0.089,    // normalized fallback (stored, unused in v1)
      "target_text": "Log in",     // cheap verification anchor
      "description": "Log in button, top-right",
      "checkpoint": true,          // verify before firing this step
      "screenshot_ref": "runs/…/step1.png"
    },
    {
      "seq": 2,
      "kind": "write",
      "text": "<email>",
      "x": 1280, "y": 620, "nx": 0.5, "ny": 0.43,
      "target_text": "Email address",
      "description": "Email field",
      "checkpoint": false
    }
    // …
  ],
  "source_run_id": "…",            // run that produced this guide
  "success_count": 0,
  "fail_count": 0,
  "model": "gpt-5.5",
  "created_at": "…",
  "updated_at": "…"
}
```

Lookup key / index: `(site, task, env.fingerprint)`.

## Components & changes

### `store.py` (additions)

- `guides` collection + index on `(site, task, env.fingerprint)`.
- `find_guide(site, task, env_fingerprint) -> Guide | None` — returns a guide
  only when the env fingerprint matches.
- `upsert_guide(guide)` — insert or replace the guide for
  `(site, task, env_fingerprint)`; on reuse, `bump_guide_stats(guide_id, ok: bool)`
  to increment `success_count` / `fail_count`.
- All writes stay non-fatal, consistent with the existing store philosophy
  (Mongo down → warn and continue with free navigation).

### Guide persistence: MongoDB primary, local file fallback

MongoDB is the primary store, but guides must **survive Mongo being
unavailable** — otherwise a Mongo outage silently disables the fast path and
every run relearns from scratch. So guide storage has a two-tier backend behind
one interface (`GuideStore`):

```
GuideStore.find_guide / upsert_guide / bump_guide_stats
        │
        ▼
   is MongoDB reachable?
   ┌────┴────┐
  yes         no
   │           │
   ▼           ▼
 MongoDB    Local JSON file
 `guides`   ~/.recursive_computer_use/guides.json
   │           │
   └─────┬─────┘
         ▼
   both kept in sync when possible (write-through)
```

Behavior:

- **Local cache path:** `~/.recursive_computer_use/guides.json` (override via
  `GUIDES_LOCAL_PATH`). A simple JSON document keyed by
  `(site, task, env_fingerprint)` — same schema as the Mongo `guides` doc.
- **Write-through when Mongo is up:** `upsert_guide` / `bump_guide_stats` write
  to **both** Mongo and the local file. The local file is always a warm copy, so
  if Mongo later goes down the fast path still has its guides.
- **Local-only when Mongo is down:** `connect` already degrades gracefully. When
  Mongo is unavailable, `GuideStore` transparently reads from and writes to the
  local file only. The agent never knows the difference — the fast path keeps
  working.
- **Read precedence:** prefer Mongo when reachable (source of truth, shared
  across processes); fall back to the local file on a miss or when Mongo is
  down.
- **Reconciliation (optional, later):** on the next successful Mongo connect, any
  guides written to the local file while Mongo was down can be pushed up. Not
  required for v1 — write-through already keeps them mostly in sync.
- **Non-fatal end to end:** if *both* Mongo and the local file fail (e.g.
  read-only home dir), the store degrades to in-memory / no-op and the agent
  simply free-navigates. A persistence failure must never break a desktop
  session — same principle as the existing `ActionStore`.

The raw `runs` / `actions` audit log stays Mongo-only for now (it is bulky and
non-critical to the fast path); only **guides** get the local fallback, because
they are small and directly power the speed optimization.

### Sandbox (env capture)

- On session start, capture the env fingerprint: `os`, screen size and scaling
  (via `pyautogui.size()` + platform scaling), and browser.
- Wrap `pyautogui` action functions (`click`, `doubleClick`, `write`, `press`,
  `hotkey`, `scroll`, …) so each call records a structured step: resolved
  `(x, y)` (from args or `pyautogui.position()`), `kind`, args, derived
  `nx, ny`, and the env. This is the raw material guides are distilled from.

### Agent loop (`agent.py`)

- Enforce **maximized** window before navigation/replay.
- Before navigating: compute `(site, task)`, call `find_guide(...)` with the
  current env fingerprint.
  - **Hit + env match** → run the **fast path**: replay steps via `exec_py`
    firing `pyautogui` calls directly, verifying only `checkpoint: true` steps;
    on checkpoint failure, abort to free navigation.
  - **Miss / mismatch / abort** → run the existing free-navigation loop.
- On successful completion of a non-fast run → **distill** the successful action
  sequence into a guide (model-authored is preferred: ask the model to emit the
  ordered steps it just performed, cross-checked against the raw action log) and
  `upsert_guide(...)`. On successful fast-path reuse → bump `success_count`.

### CLI (`__init__.py`)

- `--no-guides` to disable the read/write of guides (pure free navigation).
- Optionally `--relearn` to force a fresh free-navigation run and overwrite the
  stored guide for the `(site, task, env)`.

## Honest trade-offs

- **Coordinate replay is inherently brittle.** Dynamic layouts, A/B tests,
  cookie banners, and auth-state differences shift positions even at the same
  resolution. The design mitigates this by degrading gracefully: env-scoped
  lookup avoids cross-machine misfires, sparse checkpoints catch breakage
  early, and free navigation is always the safety net that also re-records.
- **First run is always slow.** The first `(site, task, env)` must learn via the
  full perception loop before any speed benefit appears. This is by design.
- **Task scoping matters.** Coordinates only make sense for a specific flow, so
  guides are keyed by `(site, task)`, not by site alone.

## Milestones

- **G1 — Schema + store.** `guides` collection, `find_guide` / `upsert_guide` /
  `bump_guide_stats`, env fingerprint helper. **Two-tier `GuideStore`: MongoDB
  primary with write-through to a local JSON fallback
  (`~/.recursive_computer_use/guides.json`)**, so the fast path survives Mongo
  being down. Non-fatal like the rest of the store.
- **G2 — Env capture in sandbox.** Fingerprint on session start; wrap
  `pyautogui` to record coordinate steps.
- **G3 — Maximize enforcement.** Agent maximizes the window before
  navigate/replay.
- **G4 — Fast path replay.** Given a matching guide, replay clicks directly with
  sparse checkpoint verification; fall back on failure.
- **G5 — Guide distillation.** On successful free-navigation runs, author and
  upsert the coordinate guide; bump stats on reuse.
- **G6 — CLI flags.** `--no-guides`, `--relearn`.

## Open questions (deferred)

- Distillation authorship: model-authored steps vs. heuristic filtering of the
  raw action log. (Leaning model-authored, cross-checked.)
- Checkpoint signal specifics: URL-bar OCR vs. page-title read vs. thumbnail
  hash. (Leaning URL/title read since the window is maximized with URL bar
  visible.)
- Cross-environment sharing via `nx, ny` — deferred; schema already supports it.

## Non-goals (for now)

- Cross-machine / cross-resolution replay (schema future-proofs it; logic
  deferred).
- True fullscreen support (we standardize on maximized).
- Element-level (DOM/AX-tree) targeting instead of pixel coordinates.
