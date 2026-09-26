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
    args = parser.parse_args()

    # Lazy import so startup errors are clean.
    from .agent import run

    try:
        result = run(args.prompt, model=args.model, verbose=args.verbose)
        print(result)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
