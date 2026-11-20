"""CLI: python -m scanner <path> [--out report.json] [--summary] [--sarif out.sarif] [--fail-above N]"""
from __future__ import annotations

import argparse
import json
import sys

from .engine import scan
from .sarif import to_sarif


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scanner", description="Detect AWS lock-in in a repo.")
    ap.add_argument("path", help="repo directory or single file")
    ap.add_argument("--out", help="also write JSON report to this file")
    ap.add_argument("--summary", action="store_true", help="print a human-readable summary instead of JSON")
    ap.add_argument("--sarif", metavar="FILE", help="also write a SARIF 2.1.0 file (GitHub Code Scanning)")
    ap.add_argument("--uri-prefix", default="", help="prefix for SARIF paths when scanning a subdirectory")
    ap.add_argument("--exclude-dir", action="append", default=[], metavar="DIR",
                    help="skip a directory (name or path relative to the target); repeatable")
    ap.add_argument("--fail-above", type=int, metavar="N",
                    help="exit with code 1 if the overall score is above N (CI gate)")
    args = ap.parse_args(argv)

    try:
        report = scan(args.path, exclude_dirs=set(args.exclude_dir))
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    data = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(data + "\n")
    if args.sarif:
        with open(args.sarif, "w", encoding="utf-8") as fh:
            json.dump(to_sarif(report, args.uri_prefix), fh, indent=2)

    if args.summary:
        print(f"Target: {report['target']}  ({report['scanned_files']} files scanned)")
        print(f"Overall lock-in score: {report['overall_score']}/100 ({report['rating']})\n")
        print(f"{'SERVICE':<12}{'SCORE':>6}{'FINDINGS':>10}  ALTERNATIVE")
        for svc, s in report["services"].items():
            print(f"{svc:<12}{s['score']:>6}{s['findings']:>10}  {s['alternative']}")
    elif not args.sarif or args.out:
        print(data)

    if args.fail_above is not None and report["overall_score"] > args.fail_above:
        print(f"FAIL: lock-in score {report['overall_score']} > {args.fail_above}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
