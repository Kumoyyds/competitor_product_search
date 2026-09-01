from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from .workflow import rerun, run_new_input


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run PriceScope end-to-end workflows")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Run Search, Scraping, and Matching for a new input file")
    new.add_argument("--input", required=True)
    new.add_argument("--vision", action="store_true")
    new.add_argument("--concurrency", type=int, default=8)
    new.add_argument("--db-path")

    repeat = sub.add_parser("rerun", help="Refresh stored product URLs for a prior batch")
    repeat.add_argument("--batch-id", required=True)
    repeat.add_argument("--search-title", action="append", dest="search_titles")
    vision = repeat.add_mutually_exclusive_group()
    vision.add_argument("--vision", action="store_true", dest="vision_enabled")
    vision.add_argument("--no-vision", action="store_false", dest="vision_enabled")
    repeat.set_defaults(vision_enabled=None)
    repeat.add_argument("--concurrency", type=int, default=8)
    repeat.add_argument("--db-path")
    return parser


async def _run(args: argparse.Namespace):
    if args.command == "new":
        return await run_new_input(
            args.input,
            vision_enabled=args.vision,
            concurrency=args.concurrency,
            db_path=args.db_path,
        )
    return await rerun(
        args.batch_id,
        search_titles=args.search_titles,
        vision_enabled=args.vision_enabled,
        concurrency=args.concurrency,
        db_path=args.db_path,
    )


def main() -> None:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
