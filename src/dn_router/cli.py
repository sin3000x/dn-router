# 命令行入口：读取请求、求解，并分别输出业务路线、诊断信息及离线报告。
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .solver import solve
from .visualization import write_html

FAILURE_STATUSES = frozenset({"INVALID_INPUT", "INFEASIBLE_INPUT"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open-route mandatory-DN planner")
    parser.add_argument("input", help="JSON input path, or - for stdin")
    parser.add_argument("--time-limit-ms", type=int, default=200)
    parser.add_argument("--output", "-o")
    parser.add_argument(
        "--report",
        action="store_true",
        help="Output full status, errors and routing report",
    )
    parser.add_argument(
        "--html", help="Write a self-contained offline HTML visualization"
    )
    return parser


# 返回码 2 表示输入无效或整批不可行；其他业务状态返回 0。
def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    # 先检查输出路径，防止 HTML 覆盖输入文件或 JSON 结果。
    if args.html:
        html = Path(args.html).resolve()
        others = [
            p for p in (None if args.input == "-" else args.input, args.output) if p
        ]
        if any(html == Path(p).resolve() for p in others):
            parser.error("--html must differ from the input and JSON output paths")

    # 读取失败时也保留空请求，供错误状态的 HTML 报告使用。
    payload: Any = {}
    try:
        raw = (
            sys.stdin.read()
            if args.input == "-"
            else Path(args.input).read_text(encoding="utf-8")
        )
        # JSON 小数直接解析为 Decimal，避免先转浮点造成精度损失。
        payload = json.loads(raw, parse_float=Decimal)
        result = solve(payload, time_limit_ms=args.time_limit_ms)
    except (ValueError, OSError) as error:
        result = {
            "status": "INVALID_INPUT",
            "errors": [{"message": str(error)}],
            "routes": [],
        }

    # 默认 stdout 只交付路线数组，诊断写入 stderr；--report 输出完整报告。
    if not args.report:
        diagnostics = {key: value for key, value in result.items() if key != "routes"}
        print(json.dumps(diagnostics, ensure_ascii=False), file=sys.stderr)
    text = (
        json.dumps(
            result if args.report else result["routes"], ensure_ascii=False, indent=2
        )
        + "\n"
    )
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")

    # 即使输入不可行也生成状态报告；非对象输入不传入可视化字段解析。
    if args.html:
        write_html(payload if isinstance(payload, dict) else {}, result, args.html)
    return 2 if result["status"] in FAILURE_STATUSES else 0


if __name__ == "__main__":
    sys.exit(main())
