"""
agent.py — Computer-use agentic loop using the Chat Completions API.

Uses client.chat.completions.create() with tool calling, which is supported
by both the standard OpenAI API and the local codex-as-api proxy.

The model is given an exec_py function tool backed by a Sandbox instance.
It writes Python that uses pyautogui to operate the desktop, calls display()
to send screenshots back, and calls log() to emit text.

The loop runs until:
  - The model returns a final message with no tool calls and the verifier
    (if any) accepts it or no verifier retries remain,
  - The recovery monitor aborts a stuck run, or
  - The turn limit is reached.

Every ending except an interrupt runs the verifier, so a stuck or timed-out
run still produces a verified failure for the evolution engine.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI

from .auth import resolve as resolve_auth
from .evolution.models import EvaluationMetrics, HarnessPolicy
from .evolution.runtime import DEFAULT_LIMITS, DEFAULT_RULES, EvolutionRuntime
from .learning import LearningStore
from .recovery import RecoveryMonitor
from .sandbox import Sandbox
from .store import ActionStore
from .verification import fetch_local_metrics

# Maximum round-trips before we give up.
MAX_TURNS = 30

# Tool definition exposed to the model.
EXEC_PY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "exec_py",
        "description": (
            "Run Python in a persistent desktop environment. "
            "Variables and imports persist across calls within the same session. "
            "Pre-imported: pyautogui, time. "
            "Helpers: log(value) to print text, display(pil_image) to attach a screenshot. "
            "Always inspect the screen first with display(pyautogui.screenshot()) before "
            "taking any action, then verify after each short group of actions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute.",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def _check_proxy(base_url: str) -> None:
    """Raise a clear error if the local codex-as-api proxy is not reachable."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 18080), timeout=2):
            pass
    except (OSError, ConnectionRefusedError):
        raise RuntimeError(
            "Cannot reach the local Codex proxy at 127.0.0.1:18080.\n"
            "Start it in a separate terminal first:\n\n"
            "    npx codex-as-api\n\n"
            "Then re-run your command."
        )


def run(
    prompt: str,
    *,
    model: str = "gpt-5.5",
    verbose: bool = False,
    mongodb_uri: str | None = None,
    mongodb_db: str | None = None,
    log_actions: bool = True,
    action_store: ActionStore | None = None,
    task_key: str = "general-desktop",
    verifier_url: str | None = None,
    evolve: bool = True,
) -> str:
    """
    Run a computer-use task described by *prompt*.

    Parameters
    ----------
    prompt:
        Natural-language description of the task to complete.
    model:
        Model to use. Defaults to ``gpt-5.5`` (available via Codex proxy).
        Use ``gpt-5.6-sol`` or ``gpt-6-astra`` for more capable models.
    verbose:
        Print turn-by-turn activity to stderr.
    mongodb_uri / mongodb_db:
        Optional MongoDB connection overrides used for action telemetry.
    log_actions:
        Disable all MongoDB telemetry when false.
    action_store:
        Optional injected store used by tests and embedding applications.
    task_key:
        Stable task family used to scope memories and policies.
    verifier_url:
        Optional localhost endpoint returning deterministic task metrics.
    evolve:
        Allow verified runs to propose and evaluate policy versions.

    Returns
    -------
    str
        The model's final text response.
    """
    creds = resolve_auth()

    # If using the local proxy, check it's actually running before the first call.
    if creds.base_url and "127.0.0.1" in creds.base_url:
        _check_proxy(creds.base_url)

    client = OpenAI(api_key=creds.api_key, base_url=creds.base_url)
    owns_store = action_store is None
    if action_store is None:
        action_store = (
            ActionStore.connect(mongodb_uri, mongodb_db, verbose=verbose)
            if log_actions
            else ActionStore.disabled(mongodb_db)
        )

    run_id = action_store.start_run(prompt, model)
    sandbox = Sandbox(store=action_store)
    evolution_runtime: EvolutionRuntime | None = None
    policy = HarnessPolicy(
        task_key=task_key,
        version=1,
        parent_version=None,
        status="accepted",
        rules=DEFAULT_RULES,
        limits=DEFAULT_LIMITS,
        reason="Local fallback policy.",
    )
    if (
        evolve
        and getattr(action_store, "enabled", False)
        and action_store.database is not None
    ):
        try:
            evolution_runtime = EvolutionRuntime(action_store.database)
            policy = evolution_runtime.policy_for_run(task_key)
        except Exception as exc:
            if verbose:
                print(f"[evolution] policy load failed: {exc}", file=sys.stderr)
            evolution_runtime = None
    if hasattr(sandbox, "apply_policy"):
        sandbox.apply_policy(policy)

    # Learning layer: episodes, sites and skills (see learning.py).
    started_at = datetime.now(timezone.utc)
    learning = LearningStore.for_store(action_store) if evolve else None
    skills_used = learning.sync_policy_skills(policy) if learning else []
    verified_metrics: EvaluationMetrics | None = None
    llm_calls = tokens_in = tokens_out = 0
    timed_out = False
    recovery = RecoveryMonitor(int(policy.limits.get("retry_limit", 2)))

    def check() -> EvaluationMetrics | None:
        """Read verifier metrics merged with local counts. Never raises."""
        if not verifier_url:
            return None
        try:
            verified = fetch_local_metrics(verifier_url)
        except Exception as exc:
            if verbose:
                print(f"[evolution] verifier failed: {exc}", file=sys.stderr)
            return None
        return EvaluationMetrics(
            success_rate=verified.success_rate,
            wrong_clicks=verified.wrong_clicks,
            wrong_field_entries=verified.wrong_field_entries,
            policy_violations=(
                verified.policy_violations
                + int(getattr(action_store, "policy_violation_count", 0))
            ),
            action_count=max(
                verified.action_count,
                int(getattr(action_store, "action_count", 0)),
            ),
            duration_ms=verified.duration_ms,
        )

    def record(metrics: EvaluationMetrics | None) -> None:
        """Feed final verified metrics to evolution and telemetry once."""
        nonlocal verified_metrics
        if metrics is None:
            return
        verified_metrics = metrics
        try:
            evolution_result = (
                evolution_runtime.record_verified_run(policy, metrics)
                if evolution_runtime is not None
                else None
            )
            if hasattr(action_store, "attach_verification"):
                action_store.attach_verification(
                    run_id,
                    task_key=task_key,
                    policy_version=policy.version,
                    metrics=metrics.to_document(),
                    evolution_result=evolution_result,
                )
            if verbose:
                print(
                    f"[evolution] verified metrics={metrics.to_document()} "
                    f"result={evolution_result}",
                    file=sys.stderr,
                )
        except Exception as exc:
            if verbose:
                print(f"[evolution] record failed: {exc}", file=sys.stderr)

    # Rules come from Atlas. Keep each to one bounded line so a stored rule
    # cannot smuggle extra instructions into the system prompt.
    policy_text = "\n".join(f"- {' '.join(str(rule).split())[:300]}" for rule in policy.rules)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"Active local harness policy v{policy.version} for {policy.task_key}. "
                "These rules are mandatory and enforced by the runtime:\n"
                f"{policy_text}"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    status = "failed"
    final_text: str | None = None

    try:
        for turn in range(1, MAX_TURNS + 1):
            if verbose:
                print(f"[turn {turn}] calling model …", file=sys.stderr)

            response = client.chat.completions.create(
                model=model,
                tools=[EXEC_PY_TOOL],
                messages=messages,
            )
            llm_calls += 1
            usage = getattr(response, "usage", None)
            tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
            tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)

            choice = response.choices[0]
            msg = choice.message

            # Append assistant message to history.
            # exclude_unset=True + exclude_none=True drops fields like `annotations`
            # that are present in newer SDK versions but rejected by some API endpoints.
            messages.append(msg.model_dump(exclude_unset=True, exclude_none=True))

            # No tool calls → model says it is done. Only the verifier decides.
            if not msg.tool_calls:
                final_text = msg.content or ""
                metrics = check()
                retry = (
                    recovery.verifier_retry_prompt(metrics)
                    if metrics is not None and turn < MAX_TURNS
                    else None
                )
                if retry:
                    if verbose:
                        print(f"[recovery] {retry}", file=sys.stderr)
                    messages.append({"role": "user", "content": retry})
                    continue
                status = "completed"
                record(metrics)
                if verbose:
                    print(f"[done] {final_text}", file=sys.stderr)
                return final_text

            if turn == MAX_TURNS:
                timed_out = True
                record(check())
                raise RuntimeError(
                    f"Reached the {MAX_TURNS}-turn limit without a final answer."
                )

            # Execute each tool call and append results
            sandbox.set_action_context(run_id, turn)
            for tool_call in msg.tool_calls:
                if tool_call.function.name != "exec_py":
                    raise ValueError(
                        f"Model requested unexpected tool: {tool_call.function.name!r}"
                    )

                args = json.loads(tool_call.function.arguments)
                code: str = args["code"]

                if verbose:
                    preview = code.splitlines()[0][:80]
                    print(f"  exec_py: {preview!r}", file=sys.stderr)

                actions_before = _desktop_actions(action_store)
                result = sandbox.run(code)
                hint = recovery.observe(
                    code, result, _desktop_actions(action_store) - actions_before
                )

                if verbose and result.get("error"):
                    print(f"  error: {result['error'].splitlines()[-1]}", file=sys.stderr)
                if verbose:
                    n_imgs = len(result.get("images", []))
                    if n_imgs:
                        print(f"  captured {n_imgs} screenshot(s)", file=sys.stderr)

                # Build tool result — text only in the `tool` message.
                # Some API endpoints (e.g. the local Codex proxy) reject image_url
                # blocks inside tool-role messages, so screenshots are appended as a
                # follow-up `user` message instead.
                text_content, image_blocks = _build_tool_content(result)
                if hint:
                    text_content += f"\n\n{hint}"
                    if verbose:
                        print(f"  [recovery] nudge {recovery.nudges}", file=sys.stderr)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": text_content,
                })

                # Inject screenshots as a user message so the model can see them.
                if image_blocks:
                    messages.append({
                        "role": "user",
                        "content": image_blocks,
                    })

            if recovery.should_abort:
                record(check())
                raise RuntimeError(
                    f"Run aborted: still stuck after {recovery.retry_limit} "
                    "recovery attempt(s)."
                )
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        action_store.finish_run(run_id, status, final_text)
        if learning is not None:
            learning.record_episode(
                run_id=run_id,
                prompt=prompt,
                task_key=task_key,
                model=model,
                outcome=_episode_outcome(status, verified_metrics, timed_out),
                started_at=started_at,
                policy_version=policy.version,
                metrics=verified_metrics.to_document() if verified_metrics else None,
                skills_used=skills_used,
                llm_calls=llm_calls,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                recovery=recovery.to_document(),
            )
        if owns_store:
            action_store.close()

    raise RuntimeError("Agentic loop exited unexpectedly.")


def _desktop_actions(action_store: Any) -> int:
    """Desktop actions that reached pyautogui (policy violations excluded)."""
    return int(getattr(action_store, "action_count", 0)) - int(
        getattr(action_store, "policy_violation_count", 0)
    )


def _episode_outcome(
    status: str, metrics: EvaluationMetrics | None, timed_out: bool
) -> str:
    """Map a run's end state to an episode outcome. Only a verifier says success."""
    if metrics is not None:
        return "success" if metrics.success_rate >= 1.0 else "failure"
    if status == "completed":
        return "unverified"
    if status == "interrupted":
        return "interrupted"
    return "timeout" if timed_out else "error"


def _build_tool_content(
    result: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Build the content for a tool result.

    Returns
    -------
    text_content:
        Plain-text summary suitable for the ``tool`` role message.
    image_blocks:
        List of ``image_url`` content blocks (may be empty) to be sent in a
        follow-up ``user`` role message, because many API endpoints reject
        image content inside tool-role messages.
    """
    text_parts: list[str] = []
    stdout = result.get("stdout", "").strip()
    if stdout:
        text_parts.append(f"[stdout]\n{stdout}")
    error = result.get("error")
    if error:
        text_parts.append(f"[error]\n{error.strip()}")
    if not text_parts:
        text_parts.append("[ok]")

    text_content = "\n\n".join(text_parts)

    image_blocks: list[dict[str, Any]] = []
    for data_url in result.get("images", []):
        image_blocks.append({
            "type": "image_url",
            "image_url": {"url": data_url, "detail": "high"},
        })

    return text_content, image_blocks
