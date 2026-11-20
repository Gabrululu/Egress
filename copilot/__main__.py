"""CLI: python -m copilot <path> [--out output] [--no-llm]"""
from __future__ import annotations

import argparse
import sys

from .generator import DEFAULT_OUT, generate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m copilot", description="Generate an AWS exit migration.")
    ap.add_argument("path", help="repo directory to migrate")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (default: ./output)")
    ap.add_argument("--no-llm", action="store_true", help="force offline templates")
    args = ap.parse_args(argv)
    try:
        r = generate(args.path, out_dir=args.out, use_llm=not args.no_llm)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    v = r["compose_validation"]
    print(f"path used     : {r['path'].upper()}  {r['sources']}")
    print(f"llm           : {r['llm']}")
    print(f"compose valid : {v['ok']} ({v['method']}: {v['message']})")
    for e in r["errors"]:
        print(f"note          : {e}")
    print(f"output        : {r['out_dir']}")
    for f in r["files"]:
        print(f"  - {f}")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
