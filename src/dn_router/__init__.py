# 对外公共接口：求解，以及离线报告的渲染和文件写入。
from .solver import solve
from .visualization import render_html, write_html

__all__ = ["solve", "render_html", "write_html"]
