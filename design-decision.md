# Design Decisions

This document records what we built and why. It is meant for future
maintainers who want to understand the reasoning behind the current shape of
the code, not just the code itself.

## Goal

Let an OpenAI-compatible model drive a real desktop: look at the screen, decide
what to do, and act (click, type, scroll) — repeating until the task is done.

## Overview

The system is a small agentic loop with three parts:

- `agent.py` — the loop: calls the model, runs the tool it asks for, feeds the
  result back, repeats.
- `sandbox.py` — a persistent Python execution environment where the model's
  generated code runs against `pyautogui`.
- `auth.py` — resolves credentials (direct API key or local Codex proxy).

The CLI entry point lives in `__init__.py`.

## Key Decisions

### 1. Chat Completions API + function calling (not the Responses API)

We drive the model through `client.chat.completions.create()` with a single
function tool. Chat Completions is supported by both the standard OpenAI API
**and** the local `codex-as-api` proxy, so the same code works against either
backend without change. The Responses API is not universally supported by the
proxy, so we avoided depending on it.

### 2. One tool: `exec_py`

Rather than exposing many narrow tools (click, type, screenshot, …), we expose
a single `exec_py(code)` tool that runs arbitrary Python. The model writes code
against `pyautogui` directly.

Why: it is far more expressive and keeps the tool surface tiny. The model can
compose actions, add waits, loop, and inspect state without us anticipating
every primitive. It also matches how these models are trained to "write code to
accomplish a task."

### 3. Persistent sandbox namespace

`Sandbox` keeps one Python namespace alive for the whole session. Variables,
imports, and `pyautogui` state persist across turns. `pyautogui` and `time` are
pre-imported, and two helpers are injected:

- `log(value)` — print text back to the model.
- `display(pil_image)` — attach a screenshot to the observation.

Why: multi-step desktop tasks naturally build on prior state (e.g. a coordinate
computed last turn). A persistent namespace makes that ergonomic and avoids
re-importing / re-computing on every call.

### 4. Screenshots are returned via a follow-up `user` message, not inside the tool result

This was a deliberate correction after hitting a proxy limitation.

The natural design is to put the screenshot `image_url` blocks directly in the
`tool` role message alongside the text output. But the Codex proxy rejects image
content inside tool-role messages:

    message 2 has unsupported content block image_url for role tool

So the loop now splits the observation into two messages:

1. A `tool` message with **plain text only** (stdout / error / `[ok]`), keyed to
   the `tool_call_id`.
2. A follow-up `user` message carrying the `image_url` blocks (the screenshots).

Why: the `user` role universally supports image content across endpoints, while
the `tool` role does not. The model still sees the screenshots — they just
arrive on the user turn. Keeping the `tool` message text-only also keeps it
maximally compatible.

### 5. Strip SDK-only fields before echoing assistant messages back

When appending the assistant's message to history we use:

    msg.model_dump(exclude_unset=True, exclude_none=True)

Newer `openai` SDK versions attach fields such as `annotations` to assistant
messages. Echoing those back verbatim caused the proxy to reject the request:

    message 1 does not support field "annotations"

Excluding unset/none fields sends back only what the model actually populated
(`role`, `content`, `tool_calls`, …), which every endpoint accepts.

### 6. Credential resolution: Codex proxy first, API key fallback

`auth.py` resolves credentials in priority order:

1. If `~/.codex/auth.json` has a valid ChatGPT OAuth token → route through the
   local `codex-as-api` proxy at `http://127.0.0.1:18080/v1` (default).
2. Otherwise, `OPENAI_API_KEY` from the environment / `.env` → talk to OpenAI
   directly.

Why: the proxy is the primary supported path — it handles the Codex OAuth flow
(and the Cloudflare challenge / request shaping) for us, so users logged in with
the Codex CLI get a zero-config default. A plain API key remains available as a
fallback. We also fail fast with a clear message if the proxy is expected but
not reachable (`_check_proxy`).

### 7. Bounded loop with a turn limit

The loop runs up to `MAX_TURNS = 30`. It ends early when the model returns a
message with no tool calls (its final answer), and raises if it hits the limit.

Why: a hard cap prevents runaway sessions and unbounded cost if the model gets
stuck.

## Observation Format

Each tool result the model sees contains:

- `stdout` — captured `print` / `log` output.
- `error` — a traceback string if the executed code raised.
- `images` — base64 PNG data URLs from `display()` calls.

Text goes in the tool message; images go in the follow-up user message (see
decision 4).

## Known Trade-offs

- **No real sandboxing.** The model's code runs with `exec` in-process against
  the real desktop. This is intentional for a local tool, but it means the model
  can do anything the user can. Do not run against untrusted prompts.
- **Proxy-specific workarounds.** Decisions 4 and 5 exist because of the Codex
  proxy's stricter message validation. They are harmless against the standard
  OpenAI API, so we keep them unconditionally rather than branching per backend.
