"""整理求解结果的展示数据，并渲染为自包含的离线 HTML 或供在线服务使用。"""

import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

from .models import Request
from .problem import prepare

Json = dict[str, Any]
Coordinate = list[float]

# 模板包含样式、页面结构和交互脚本；离线报告通过占位符嵌入数据，在线服务嵌入 null。
_TEMPLATE = files(__package__).joinpath("report.html")


def _timeline(routes: list[Json]) -> Json | None:
    """按无时区的业务当地时间构建时间轴，不受主机时区或夏令时影响。

    先行驶后等待只是展示约定，真实等待位置未知。
    零时长阶段保留边界，不为播放虚构持续时间。
    """
    parsed: list[Json | None] = []
    for route in routes:
        try:
            parsed.append(_route_segments(route))
        except (KeyError, TypeError, ValueError, OverflowError):
            # 单条路线时间异常只禁用该路线播放，不影响其余路线。
            parsed.append(None)
    valid = [r for r in parsed if r is not None]
    if not valid:
        return None
    # 全部有效路线共享最早出发时刻，支持不同发车时间和跨日播放。
    origin = min(r["start"] for r in valid)
    finish = max(r["end"] for r in valid)

    def offset(t: datetime) -> float:
        return (t - origin).total_seconds()

    return {
        "startTime": origin.isoformat(sep=" "),
        "durationSeconds": offset(finish),
        "routes": [
            None
            if r is None
            else {
                "start": offset(r["start"]),
                "end": offset(r["end"]),
                "segments": [
                    {**seg, "start": offset(seg["start"]), "end": offset(seg["end"])}
                    for seg in r["segments"]
                ],
            }
            for r in parsed
        ],
    }


def _route_segments(route: Json) -> Json:
    """把一条路线拆成行驶、等待、卸货三段；时间不连贯时抛 ValueError。"""
    start = datetime.fromisoformat(route["departureTime"])
    end = datetime.fromisoformat(route["completionTime"])
    if start.tzinfo is not None or end.tzinfo is not None or end < start:
        raise ValueError("invalid local route times")
    previous, segments = start, []
    for index, stop in enumerate(route["stops"]):
        arrival = datetime.fromisoformat(stop["arrivalTime"])
        departure = datetime.fromisoformat(stop["departureTime"])
        wait = float(stop.get("waitingBeforeArrivalSeconds", 0))
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("invalid waiting duration")
        travel_end = arrival - timedelta(seconds=wait)
        if not previous <= travel_end <= arrival <= departure <= end:
            raise ValueError("nonchronological stop")
        # 每次停靠拆成行驶、等待、卸货三段，避免等待被重复计算。
        for kind, a, b in (
            ("travel", previous, travel_end),
            ("wait", travel_end, arrival),
            ("unload", arrival, departure),
        ):
            segments.append({"kind": kind, "start": a, "end": b, "stopIndex": index})
        previous = departure
    if previous != end:
        raise ValueError("completion must match the last stop")
    return {"start": start, "end": end, "segments": segments}


def _display_routes(payload: Json, routes: list[Json]) -> list[Json]:
    """仅在报告内部把逐 DN 输出合并为实际停靠，保持公开结果对象不变。

    eta 是开始卸货时间；等待由权威矩阵耗时反推，同站只计一次卸货。
    """
    if not any("siteDetails" in route for route in routes):
        return routes
    request = Request.model_validate(payload)
    problem = prepare(request)
    orders = {dn.orderReleaseId: (node, dn) for node, dn in enumerate(problem.dns, 1)}
    converted = []
    for number, route in enumerate(routes, 1):
        if "siteDetails" not in route:
            converted.append(route)
            continue
        previous_time = datetime.fromisoformat(route["departure_date"])
        previous_node = 0
        stops: list[Json] = []
        for detail in route["siteDetails"]:
            node, dn = orders[detail["siteAddress"]]
            eta = datetime.fromisoformat(detail["eta"])
            # 相邻同站 DN 合并展示，并检查共用 eta；不重复增加卸货时长。
            if stops and stops[-1]["siteCode"] == dn.siteCode:
                if stops[-1]["arrivalTime"] != eta.isoformat(sep=" "):
                    raise ValueError("same-stop DNs must share eta")
                stops[-1]["orderReleaseIds"].append(dn.orderReleaseId)
            else:
                # 从两次卸货间隔扣除行驶时间，剩余部分是展示用等待时间。
                travel = problem.duration[previous_node][node]
                wait = (eta - previous_time).total_seconds() - travel
                if wait < 0:
                    raise ValueError("eta precedes unloading and travel completion")
                departure = eta + timedelta(seconds=problem.unload_seconds)
                stops.append(
                    {
                        "siteCode": dn.siteCode,
                        "orderReleaseIds": [dn.orderReleaseId],
                        "arrivalTime": eta.isoformat(sep=" "),
                        "serviceStartTime": eta.isoformat(sep=" "),
                        "departureTime": departure.isoformat(sep=" "),
                        "waitingBeforeArrivalSeconds": wait,
                        "unloadingSeconds": problem.unload_seconds,
                    }
                )
                previous_time = departure
            previous_node = node
        site_count = problem.distinct_stops(
            orders[dn][0] for dn in route["siteAddresses"]
        )
        converted.append(
            {
                "routeId": f"R{number}",
                "vehicleTypeName": route["vehicle"],
                "departureTime": route["departure_date"],
                "completionTime": previous_time.isoformat(sep=" "),
                "totalDistance": route["dist"],
                "totalVolume": route["volume"],
                "totalWeight": route["weight"],
                "stops": stops,
                "physicalStopCount": len(stops),
                "siteCount": site_count,
            }
        )
    return converted


def _coordinate(lat: Any, lon: Any) -> Coordinate | None:
    """转成 [经度, 纬度]；非法、非有限或越界坐标返回 None，不绘制。"""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return None
    # NaN 与任何数比较都为假，因此范围检查同时排除了非有限值。
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return [lon, lat]
    return None


def _finite(value: Any) -> Any:
    """递归把非有限数值转成 None，供严格 JSON 序列化。"""
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    if isinstance(value, float | Decimal) and not math.isfinite(value):
        return None
    return value


def report_data(payload: Json, result: Json) -> Json:
    """组装页面所需的路线、坐标、车型与时间轴数据，结果可直接严格 JSON 序列化。

    Args:
        payload: 原始请求 JSON 对象；输入无效时可以是空字典或残缺对象。
        result: solve() 返回的结果对象。

    Returns:
        包含 result（路线已改为按实际停靠展示）、depot、vehicles、timeline 的字典。

    Raises:
        ValueError: 结果含路线但请求无法重新校验，或路线时间与权威矩阵不一致。
        KeyError: 路线引用了请求中不存在或已剔除的 DN。
        TypeError: result 的结构与 solve() 输出不符。
    """

    # 错误输入也要能展示状态，只提取结构合法的字典记录。
    def records(key: str) -> list[Json]:
        value = payload.get(key)
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, dict)]

    orders = {
        d.get("orderReleaseId"): d
        for d in records("dnSiteVoList")
        if isinstance(d.get("orderReleaseId"), str)
    }

    def stop_coordinate(stop: Json) -> Coordinate | None:
        points = [
            _coordinate(
                orders.get(dn, {}).get("deliveryLatitude"),
                orders.get(dn, {}).get("deliveryLongitude"),
            )
            for dn in stop.get("orderReleaseIds", [])
        ]
        # 同站所有 DN 坐标必须齐全且一致，避免把冲突位置误画成一个点。
        if points and points[0] is not None and points.count(points[0]) == len(points):
            return points[0]
        return None

    routes = [
        {
            **route,
            "stops": [
                {**stop, "coordinate": stop_coordinate(stop)}
                for stop in route.get("stops", [])
            ],
        }
        for route in _display_routes(payload, result.get("routes", []))
    ]
    data = {
        "result": {**result, "routes": routes},
        "depot": {
            "name": payload.get("sourceLocation", "仓库"),
            "coordinate": _coordinate(
                payload.get("sourceLatitude"), payload.get("sourceLongitude")
            ),
        },
        "vehicles": records("vehicleTypeVoList"),
        "timeline": _timeline(routes),
    }
    # 往返一次统一数值类型，在线服务可直接再次序列化。
    return json.loads(dumps(data))


def dumps(value: Any) -> str:
    """严格 JSON 序列化：Decimal 转浮点，非有限数值转 null。"""
    return json.dumps(
        _finite(value),
        ensure_ascii=False,
        allow_nan=False,
        default=lambda v: float(v) if isinstance(v, Decimal) else str(v),
    )


def render_page(data: Json | None) -> str:
    """把数据嵌入页面模板；data 为 None 时页面以在线服务模式启动。"""
    encoded = dumps(data)
    # 嵌入前转义 HTML 特殊字符，防止数据提前闭合 script 标签。
    for char, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e")):
        encoded = encoded.replace(char, escaped)
    return _TEMPLATE.read_text(encoding="utf-8").replace("__REPORT_DATA__", encoded)


def render_html(payload: Json, result: Json) -> str:
    """直接嵌入数据与前端资源，生成无需联网的报告。

    Args:
        payload: 原始请求 JSON 对象，同 report_data()。
        result: solve() 返回的结果对象。

    Returns:
        完整 HTML 文本。

    Raises:
        与 report_data() 相同。
    """
    return render_page(report_data(payload, result))


def write_html(payload: Json, result: Json, path: str | Path) -> None:
    """以 UTF-8 保存完整离线报告，页面无需额外资源文件。

    Args:
        payload: 原始请求 JSON 对象，同 report_data()。
        result: solve() 返回的结果对象。
        path: 输出文件路径，已存在时覆盖。

    Raises:
        OSError: 写文件失败。
        其余同 report_data()。
    """
    Path(path).write_text(render_html(payload, result), encoding="utf-8")
