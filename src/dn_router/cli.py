import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from .solver import solve
from .visualization import write_html


def main():
    parser = argparse.ArgumentParser(description="Open-route mandatory-DN planner")
    parser.add_argument("input", help="JSON input path, or - for stdin")
    parser.add_argument("--time-limit-ms", type=int, default=200)
    parser.add_argument("--output", "-o")
    parser.add_argument("--report", action="store_true", help="Output full status, errors and routing report")
    parser.add_argument("--html", help="Write a self-contained offline HTML visualization")
    args = parser.parse_args()
    if args.html:
        html_path = Path(args.html).resolve()
        if ((args.input != "-" and html_path == Path(args.input).resolve())
                or (args.output and html_path == Path(args.output).resolve())):
            parser.error("--html must differ from the input and JSON output paths")
    payload = {}
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        payload = json.loads(raw, parse_float=Decimal)
        result = solve(payload, time_limit_ms=args.time_limit_ms)
    except (ValueError, OSError) as error:
        result = {"status": "INVALID_INPUT", "errors": [{"message": str(error)}], "routes": []}
    if not args.report:
        diagnostics = {key: value for key, value in result.items() if key != "routes"}
        print(json.dumps(diagnostics, ensure_ascii=False), file=sys.stderr)
    text = json.dumps(result if args.report else result["routes"], ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    if args.html:
        write_html(payload if isinstance(payload, dict) else {}, result, args.html)
    return 2 if result["status"] in ("INVALID_INPUT", "INFEASIBLE_INPUT") else 0


if __name__ == "__main__":
    sys.exit(main())
