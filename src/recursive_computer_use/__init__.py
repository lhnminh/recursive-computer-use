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
        default="gpt-5.6-terra",
        help="Model to use (default: gpt-5.6-terra). Available via proxy: gpt-5.5, gpt-5.6-terra, gpt-5.6-luna, gpt-6-astra.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print turn-by-turn activity.",
    )
    parser.add_argument(
        "--no-guides",
        action="store_true",
        help="Disable guide lookup and capture (already disabled by default).",
    )
    parser.add_argument(
        "--guides",
        action="store_true",
        help="Capture a guide after a successful run.",
    )
    parser.add_argument(
        "--replay-guides",
        action="store_true",
        help="Replay a saved guide, filling its site and requested text from the prompt.",
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
    args = parser.parse_args()

    # Lazy import so startup errors are clean.
    from .agent import run

    try:
        result = run(
            args.prompt,
            model=args.model,
            verbose=args.verbose,
            use_guides=args.guides and not args.no_guides,
            replay_guides=args.replay_guides and not args.no_guides,
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
