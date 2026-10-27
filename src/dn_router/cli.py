# 命令行入口：读取请求、求解，并分别输出业务路线、诊断信息及离线报告；serve 子命令启动在线可视化服务。
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .solver import solve
from .visualization import write_html

FAILURE_STATUSES = frozenset({"INVALID_INPUT", "INFEASIBLE_INPUT"})


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open-route mandatory-DN planner",
        epilog="Run `dn-router serve --help` for the web visualization service.",
    )
    parser.add_argument("input", help="JSON input path, or - for stdin")
    parser.add_argument("--time-limit-ms", type=_positive_int, default=200)
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


def solve_text(raw: str, time_limit_ms: int) -> tuple[Any, dict[str, Any]]:
    """解析 JSON 文本并求解；解析失败时返回 INVALID_INPUT 结果而不是抛错。

    Args:
        raw: 请求 JSON 文本。
        time_limit_ms: 求解时间预算，单位毫秒。

    Returns:
        (payload, result)。payload 是解析出的 JSON 值，不一定是对象；解析失败时
        为空字典，供错误状态的报告使用。result 是 solve() 的结果。
    """
    try:
        # JSON 小数直接解析为 Decimal，避免先转浮点造成精度损失。
        payload = json.loads(raw, parse_float=Decimal)
    except ValueError as error:
        result = {
            "status": "INVALID_INPUT",
            "errors": [{"message": str(error)}],
            "routes": [],
        }
        return {}, result
    # 只捕获解析错误；求解内部异常不能伪装成输入无效。
    return payload, solve(payload, time_limit_ms=time_limit_ms)


def main(argv: list[str] | None = None) -> int:
    """命令行入口；首个参数为 serve 时转交在线可视化服务。

    Args:
        argv: 命令行参数，不含程序名；为 None 时读取 sys.argv。

    Returns:
        退出码：2 表示输入无效或整批不可行，1 表示求解结果未通过审计，
        其他业务状态为 0；serve 子命令返回其自身的退出码。参数错误时由
        argparse 直接以退出码 2 退出。
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        from .server import main as serve_main

        return serve_main(argv[1:])
    parser = _parser()
    args = parser.parse_args(argv)
    # 先检查输出路径，防止 HTML 覆盖输入文件或 JSON 结果。
    if args.html:
        html = Path(args.html).resolve()
        others = [
            p for p in (None if args.input == "-" else args.input, args.output) if p
        ]
        if any(html == Path(p).resolve() for p in others):
            parser.error("--html must differ from the input and JSON output paths")

    try:
        raw = (
            sys.stdin.read()
            if args.input == "-"
            else Path(args.input).read_text(encoding="utf-8")
        )
    except (ValueError, OSError) as error:
        payload: Any = {}
        result = {
            "status": "INVALID_INPUT",
            "errors": [{"message": str(error)}],
            "routes": [],
        }
    else:
        payload, result = solve_text(raw, args.time_limit_ms)

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
    if result["status"] == "SOLVER_ERROR":
        return 1
    return 2 if result["status"] in FAILURE_STATUSES else 0


if __name__ == "__main__":
    sys.exit(main())
