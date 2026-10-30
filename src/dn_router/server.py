"""在线可视化服务：浏览器里选择服务器上的请求文件或上传 JSON，求解后直接查看报告。

只依赖标准库。默认监听所有网卡，远程服务器上可直接用服务器地址访问；服务无鉴权，
只读取 --dir 内的 JSON，公网环境请加 --host 127.0.0.1 并改用 SSH 端口转发。
"""

import argparse
import os
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .cli import solve_text
from .visualization import dumps, report_data

# 页面模板包含样式、结构和交互脚本，数据全部通过接口获取。
_PAGE = files(__package__).joinpath("report.html")

# 列目录时跳过的目录，避免把虚拟环境和缓存里的 JSON 混进候选列表。
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__", "build", "dist"})
_MAX_FILES = 500
_MAX_BODY_BYTES = 64 * 1024 * 1024
_MAX_TIME_LIMIT_MS = 120_000


def list_json_files(root: Path) -> list[dict[str, Any]]:
    """列出根目录下的 JSON 文件，最近修改的排在前面，跳过隐藏目录。"""
    found = []
    for directory, subdirs, names in os.walk(root):
        # 原地剪枝，不进入 .venv 等目录，避免遍历后再过滤。
        subdirs[:] = [
            d for d in subdirs if not d.startswith(".") and d not in _SKIPPED_DIRS
        ]
        for name in names:
            path = Path(directory, name)
            if path.suffix == ".json" and path.is_file():
                relative = path.relative_to(root).as_posix()
                found.append((path.stat().st_mtime, relative))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [{"path": name, "mtime": mtime} for mtime, name in found[:_MAX_FILES]]


def resolve_json_file(root: Path, name: str) -> Path:
    """把相对路径解析到根目录内的 JSON 文件；越界或不存在时抛 ValueError。"""
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path.suffix != ".json" or not path.is_file():
        raise ValueError(f"not a JSON file under the served directory: {name}")
    return path


class _Handler(BaseHTTPRequestHandler):
    """路由：/ 页面，/api/files 文件列表，/api/solve 求解并返回报告数据。"""

    root: Path
    default_time_limit_ms: int
    server_version = "dn-router"

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            self._send(
                status=HTTPStatus.OK,
                text=_PAGE.read_text(encoding="utf-8"),
                content_type="text/html; charset=utf-8",
            )
        elif url.path == "/api/files":
            self._json(
                status=HTTPStatus.OK,
                body={
                    "root": str(self.root),
                    "defaultTimeLimitMs": self.default_time_limit_ms,
                    "files": list_json_files(self.root),
                },
            )
        elif url.path == "/api/solve":
            name = query.get("file", [""])[0]
            try:
                raw = resolve_json_file(root=self.root, name=name).read_text(
                    encoding="utf-8"
                )
            except (ValueError, OSError) as error:
                self._json(status=HTTPStatus.BAD_REQUEST, body={"error": str(error)})
                return
            self._solve(raw=raw, query=query, source={"file": name})
        else:
            self._json(status=HTTPStatus.NOT_FOUND, body={"error": "not found"})

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/api/solve":
            self._json(status=HTTPStatus.NOT_FOUND, body={"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= _MAX_BODY_BYTES:
            self._json(
                status=HTTPStatus.BAD_REQUEST, body={"error": "empty or oversized body"}
            )
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as error:
            self._json(status=HTTPStatus.BAD_REQUEST, body={"error": str(error)})
            return
        query = parse_qs(url.query)
        self._solve(
            raw=raw, query=query, source={"name": query.get("name", ["上传的请求"])[0]}
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[dn-router] {fmt % args}", file=sys.stderr)

    def _solve(self, raw: str, query: dict[str, list[str]], source: Any) -> None:
        """求解请求文本；业务失败照常返回报告，只有展示数据整理失败才算服务错误。"""
        try:
            limit = int(query.get("time_limit_ms", [self.default_time_limit_ms])[0])
        except ValueError:
            limit = self.default_time_limit_ms
        limit = min(max(limit, 1), _MAX_TIME_LIMIT_MS)
        try:
            payload, result = solve_text(raw=raw, time_limit_ms=limit)
        except Exception as error:  # noqa: BLE001 - HTTP 边界兜底
            # 求解内部异常也要给页面返回可读错误，而不是直接断开连接。
            self._json(
                status=HTTPStatus.INTERNAL_SERVER_ERROR, body={"error": repr(error)}
            )
            return
        try:
            data = report_data(
                payload=payload if isinstance(payload, dict) else {}, result=result
            )
        except (KeyError, TypeError, ValueError) as error:
            self._json(
                status=HTTPStatus.INTERNAL_SERVER_ERROR, body={"error": repr(error)}
            )
            return
        # output 保留求解器原始结果，页面导出时与 CLI --report 一致。
        body = {**data, "output": result, "source": {**source, "timeLimitMs": limit}}
        self._json(status=HTTPStatus.OK, body=body)

    def _json(self, status: HTTPStatus, body: Any) -> None:
        self._send(
            status=status,
            text=dumps(body),
            content_type="application/json; charset=utf-8",
        )

    def _send(self, status: HTTPStatus, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(
    root: str | Path,
    host: str = "0.0.0.0",
    port: int = 8787,
    time_limit_ms: int = 1000,
) -> ThreadingHTTPServer:
    """创建绑定好目录与默认时限的服务实例。

    Args:
        root: 请求 JSON 所在目录，服务只读取其中的 .json 文件。
        host: 监听地址；默认监听所有网卡。
        port: 监听端口；为 0 时由系统分配。
        time_limit_ms: 页面未指定时的默认求解时限，单位毫秒。

    Returns:
        已绑定但尚未启动的服务实例，调用 serve_forever() 开始服务。

    Raises:
        OSError: 无法绑定地址或端口。
    """
    handler = type(
        "Handler",
        (_Handler,),
        {"root": Path(root).resolve(), "default_time_limit_ms": time_limit_ms},
    )
    return ThreadingHTTPServer((host, port), handler)


def _display_hosts(host: str) -> list[str]:
    """监听全部网卡时列出本机主机名与出口 IP，方便直接复制访问地址。"""
    if host not in ("0.0.0.0", "::"):
        return [host]
    hosts = [socket.gethostname()]
    try:
        # UDP connect 只选路由、不发包，用来取得对外网卡的 IP。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            hosts.append(probe.getsockname()[0])
    except OSError:
        pass
    return [*hosts, "localhost"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dn-router serve", description="Serve the web visualization"
    )
    parser.add_argument("--dir", default=".", help="Directory of request JSON files")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--time-limit-ms", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        server = make_server(
            root=args.dir,
            host=args.host,
            port=args.port,
            time_limit_ms=args.time_limit_ms,
        )
    except OSError as error:
        parser.exit(
            1,
            f"无法监听 {args.host}:{args.port}（{error.strerror}），可用 --port 换一个端口\n",
        )
    host, port = args.host, server.server_port
    print(f"dn-router 可视化服务（目录 {Path(args.dir).resolve()}）", flush=True)
    for address in _display_hosts(host):
        print(f"  http://{address}:{port}/", flush=True)
    if host in ("127.0.0.1", "localhost"):
        print(
            f"远程服务器上运行时，本地执行：ssh -L {port}:localhost:{port} <服务器>",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
