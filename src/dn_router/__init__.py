# 对外公共接口：求解，以及报告数据整理。
from .solver import Result, solve
from .visualization import report_data

__all__ = ["Result", "report_data", "solve"]
