"""
recursive-computer-use — OpenAI Responses API computer-use harness.

CLI usage::

    recursive-computer-use "open Safari and go to openai.com"
    recursive-computer-use --model computer-use-preview --verbose "click the search box and type hello"
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()  # loads .env from cwd or any parent directory


def main() -> None:
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
        default="gpt-5.5",
        help="Model to use (default: gpt-5.5). Available via proxy: gpt-5.5, gpt-5.6-sol, gpt-6-astra.",
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
    args = parser.parse_args()

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
        )
        print(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
