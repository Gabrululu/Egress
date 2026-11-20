"""CLI: python -m scanner <path> [--out report.json] [--summary]"""
from __future__ import annotations

import argparse
import json
import sys

from .engine import scan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scanner", description="Detect AWS lock-in in a repo.")
    ap.add_argument("path", help="repo directory or single file")
    ap.add_argument("--out", help="also write JSON report to this file")
    ap.add_argument("--summary", action="store_true", help="print a human-readable summary instead of JSON")
    args = ap.parse_args(argv)

    try:
        report = scan(args.path)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    data = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(data + "\n")

    if args.summary:
        print(f"Target: {report['target']}  ({report['scanned_files']} files scanned)")
        print(f"Overall lock-in score: {report['overall_score']}/100 ({report['rating']})\n")
        print(f"{'SERVICE':<12}{'SCORE':>6}{'FINDINGS':>10}  ALTERNATIVE")
        for svc, s in report["services"].items():
            print(f"{svc:<12}{s['score']:>6}{s['findings']:>10}  {s['alternative']}")
    else:
        print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
