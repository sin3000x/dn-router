import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from .solver import solve


def main():
    parser = argparse.ArgumentParser(description="Open-route mandatory-DN planner")
    parser.add_argument("input", help="JSON input path, or - for stdin")
    parser.add_argument("--time-limit-ms", type=int, default=200)
    parser.add_argument("--output", "-o")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        result = solve(json.loads(raw, parse_float=Decimal), time_limit_ms=args.time_limit_ms)
    except (ValueError, OSError) as error:
        result = {"status": "INVALID_INPUT", "errors": [{"message": str(error)}], "routes": []}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 2 if result["status"] in ("INVALID_INPUT", "INFEASIBLE_INPUT") else 0


if __name__ == "__main__":
    sys.exit(main())
