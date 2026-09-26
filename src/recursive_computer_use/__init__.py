"""
recursive-computer-use — OpenAI Responses API computer-use harness.

CLI usage::

    recursive-computer-use "open Safari and go to openai.com"
    recursive-computer-use --model computer-use-preview --verbose "click the search box and type hello"
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from dotenv import load_dotenv

load_dotenv()  # loads .env from cwd or any parent directory


def main(argv: Sequence[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"learn", "do"}:
        _network_command(argv)
        return

    parser = argparse.ArgumentParser(
        prog="recursive-computer-use",
        description="Run a desktop task using OpenAI computer use.",
    )
    parser.add_argument(
        "prompt",
        help="Natural-language description of the task to perform.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.6-terra",
        help="Model to use (default: gpt-5.6-terra).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print turn-by-turn activity.",
    )
    parser.add_argument(
        "--mongodb-uri",
        help="MongoDB connection URI (default: MONGODB_URI or localhost).",
    )
    parser.add_argument(
        "--mongodb-db",
        help="MongoDB database name (default: MONGODB_DB or recursive_computer_use).",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Disable MongoDB run and action telemetry.",
    )
    parser.add_argument(
        "--task-key",
        default="general-desktop",
        help="Stable task family used for task-specific memory and policies.",
    )
    parser.add_argument(
        "--verifier-url",
        help="Local HTTP endpoint returning deterministic success and safety metrics.",
    )
    parser.add_argument(
        "--no-evolve",
        action="store_true",
        help="Load the safe local policy without proposing or evaluating changes.",
    )
    parser.add_argument(
        "--policy-version",
        type=int,
        help="Load an exact stored policy version for replay evidence collection.",
    )
    parser.add_argument(
        "--observe-only",
        action="store_true",
        help="Record verified metrics without proposing or promoting a policy.",
    )
    parser.add_argument(
        "--no-guides",
        action="store_true",
        help="Disable guide lookup, replay, and capture (enabled by default).",
    )
    parser.add_argument(
        "--guides",
        action="store_true",
        help="Capture a guide after a successful run (enabled by default).",
    )
    parser.add_argument(
        "--replay-guides",
        action="store_true",
        help="Replay a saved guide (enabled by default), filling its site and requested text from the prompt.",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="Guide key: site/host (inferred from the prompt if omitted).",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Guide key: task/intent (inferred from the prompt if omitted).",
    )
    args = parser.parse_args(argv)

    if args.policy_version is not None and args.no_evolve:
        parser.error("--policy-version requires persistent evolution; remove --no-evolve")
    if args.observe_only and not args.verifier_url:
        parser.error("--observe-only requires --verifier-url")

    # Lazy import so startup errors are clean.
    from .agent import run

    try:
        result = run(
            args.prompt,
            model=args.model,
            verbose=args.verbose,
            mongodb_uri=args.mongodb_uri,
            mongodb_db=args.mongodb_db,
            log_actions=not args.no_log,
            task_key=args.task_key,
            verifier_url=args.verifier_url,
            evolve=not args.no_evolve,
            policy_version=args.policy_version,
            observe_only=args.observe_only,
            use_guides=(args.guides or not args.no_guides),
            replay_guides=(args.replay_guides or not args.no_guides),
            site=args.site,
            task=args.task,
        )
        print(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _network_command(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(prog="recursive-computer-use")
    commands = parser.add_subparsers(dest="command", required=True)
    learn_parser = commands.add_parser("learn", help="Record a task and learn an API recipe.")
    learn_parser.add_argument("--url", required=True, help="Site URL to open in the recording browser.")
    learn_parser.add_argument("--task", required=True, help="Task performed during recording.")
    learn_parser.add_argument("--task-key", default="network-task")
    learn_parser.add_argument("--agent", action="store_true", help="Opt in to computer-use automation.")
    do_parser = commands.add_parser("do", help="Run a task with a learned API recipe first.")
    do_parser.add_argument("--site", required=True, help="Site host[:port] or HTTP URL.")
    do_parser.add_argument("--task-key", default="network-task")
    do_parser.add_argument("task", help="Natural-language task, including values to fill.")
    args = parser.parse_args(argv)

    try:
        from .network.capture import record
        from .network.learner import learn_recipe
        from .network.loop import _default_store, do_task

        store = _default_store()
        if args.command == "learn":
            from pathlib import Path
            from uuid import uuid4
            from urllib.parse import urlsplit

            parts = urlsplit(args.url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                parser.error("--url must be an absolute HTTP or HTTPS URL")
            result = record(
                args.url,
                task=args.task,
                har_path=Path(".recordings") / f"{uuid4().hex}.har",
                agent_prompt=args.task if args.agent else None,
            )
            recipe = learn_recipe(
                result.har_path,
                args.task,
                site=parts.netloc.lower(),
                task_key=args.task_key,
            )
            recipe_id = store.save_candidate(recipe, recording_id=result.recording_id)
            print({"recipe_id": str(recipe_id), "capture_ok": result.ok, "duration_ms": result.duration_ms})
            return

        result = do_task(args.task, site=args.site, task_key=args.task_key, store=store)
        print(result)
        if not result["ok"]:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
