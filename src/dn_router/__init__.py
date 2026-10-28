# 对外公共接口：求解，以及报告数据整理、离线报告渲染和文件写入。
from .solver import Result, solve
from .visualization import render_html, report_data, write_html

__all__ = ["Result", "render_html", "report_data", "solve", "write_html"]
