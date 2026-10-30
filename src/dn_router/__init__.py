# 对外公共接口：请求模型、求解，以及报告数据整理。
__all__ = ["DN", "Leg", "Request", "Result", "VehicleType", "report_data", "solve"]

from .models import DN, Leg, Request, VehicleType
from .solver import Result, solve
from .visualization import report_data
