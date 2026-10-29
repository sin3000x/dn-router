"""整理求解结果的展示数据，供在线可视化服务使用。"""

import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from collections.abc import Callable, Mapping
from typing import Any

from .models import Request
from .problem import DIST_SCALE, Problem, gc_paused

Json = dict[str, Any]
Coordinate = list[float]


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
    旧版不考虑时间的请求没有 siteDetails，只按 siteAddresses 合并停靠，不带时间。
    """
    if not any("siteAddresses" in route for route in routes):
        return routes
    with gc_paused():
        request = Request.model_validate(payload)
        problem = Problem.from_request(request)
    orders = {dn.orderReleaseId: (node, dn) for node, dn in enumerate(problem.dns, 1)}
    converted = []
    for number, route in enumerate(routes, 1):
        if "siteAddresses" not in route:
            converted.append(route)
            continue
        if "siteDetails" not in route:
            converted.append(
                _untimed_route(
                    problem=problem, orders=orders, number=number, route=route
                )
            )
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
                        **_leg(
                            problem=problem, origin=previous_node, node=node, timed=True
                        ),
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


def _leg(problem: Problem, origin: int, node: int, *, timed: bool) -> Json:
    """上一停靠到本停靠的距离与耗时，取求解实际使用的归一化矩阵；旧版请求不计耗时。"""
    return {
        "legDistance": Decimal(problem.distance[origin][node]) / DIST_SCALE,
        "legDurationSeconds": problem.duration[origin][node] if timed else None,
    }


def _untimed_route(
    problem: Problem, orders: dict[str, tuple[int, Any]], number: int, route: Json
) -> Json:
    """旧版无时间路线：相邻同站 DN 合并为一次停靠，时间字段留空。"""
    stops: list[Json] = []
    previous_node = 0
    for order in route["siteAddresses"]:
        node, dn = orders[order]
        if stops and stops[-1]["siteCode"] == dn.siteCode:
            stops[-1]["orderReleaseIds"].append(dn.orderReleaseId)
        else:
            stops.append(
                {
                    "siteCode": dn.siteCode,
                    "orderReleaseIds": [order],
                    **_leg(
                        problem=problem, origin=previous_node, node=node, timed=False
                    ),
                }
            )
        previous_node = node
    return {
        "routeId": f"R{number}",
        "vehicleTypeName": route["vehicle"],
        "departureTime": None,
        "completionTime": None,
        "totalDistance": route["dist"],
        "totalVolume": route["volume"],
        "totalWeight": route["weight"],
        "stops": stops,
        "physicalStopCount": len(stops),
        "siteCount": problem.distinct_stops(
            orders[order][0] for order in route["siteAddresses"]
        ),
    }


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


_ORDER_FIELDS = ("siteCode", "totalGrossVolume", "totalGrossWeight", "timeWindow")

# 影响求解口径的顶层参数，原样回显；缺省字段显示为 null，由页面说明默认值。
_REQUEST_FIELDS = (
    "taskNo",
    "sourceLocation",
    "earliestShippingDate",
    "latestShippingDate",
    "unloadingTime",
    "enableTimeWindows",
    "maxSiteQty",
    "lonAndLatCombination",
    "siteDistanceLimit",
    "distanceLessThanFirstLeg",
    "quotationModel",
    "destCountryCode3",
)


def _raw_matrix(payload: Json, ids: list[str], legs: list[Json]) -> Json:
    """按输入原样整理距离与耗时矩阵，行列为仓库加全部 DN；缺失的边为 null。

    不做归一化，也不依赖请求校验通过，便于排查缺边或取值错误。
    """
    source = payload.get("sourceLocation")
    points = [source if isinstance(source, str) else None, *ids]
    index = {point: i for i, point in enumerate(points) if point is not None}
    distance: list[list[Any]] = [[None] * len(points) for _ in points]
    duration: list[list[Any]] = [[None] * len(points) for _ in points]
    for leg in legs:
        a, b = index.get(leg.get("from")), index.get(leg.get("to"))
        if a is not None and b is not None:
            distance[a][b] = leg.get("planDistance")
            duration[a][b] = leg.get("duration")
    return {"ids": points, "distance": distance, "duration": duration}


def _request_summary(payload: Json, records: Callable[[str], list[Json]]) -> Json:
    """回显请求的关键参数和规模，帮助核对求解口径。"""
    return {
        **{key: payload.get(key) for key in _REQUEST_FIELDS},
        "dnCount": len(records("dnSiteVoList")),
        "siteCodeCount": len(
            {d.get("siteCode") for d in records("dnSiteVoList")} - {None}
        ),
        "legCount": len(records("allPathDistanceVoList")),
    }


def report_data(payload: Json, result: Mapping[str, Any]) -> Json:
    """组装页面所需的路线、坐标、车型与时间轴数据，结果可直接严格 JSON 序列化。

    Args:
        payload: 原始请求 JSON 对象；输入无效时可以是空字典或残缺对象。
        result: solve() 返回的结果对象。

    Returns:
        包含 result（路线已改为按实际停靠展示）、depot、vehicles、request（关键入参）、
        orders（逐 DN 明细）、matrix（原始距离矩阵）、timeline 的字典。

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
                lat=orders.get(dn, {}).get("deliveryLatitude"),
                lon=orders.get(dn, {}).get("deliveryLongitude"),
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
        for route in _display_routes(payload=payload, routes=result.get("routes", []))
    ]
    data = {
        "result": {**result, "routes": routes},
        "depot": {
            "name": payload.get("sourceLocation", "仓库"),
            "coordinate": _coordinate(
                lat=payload.get("sourceLatitude"), lon=payload.get("sourceLongitude")
            ),
        },
        "vehicles": records("vehicleTypeVoList"),
        "request": _request_summary(payload=payload, records=records),
        "matrix": _raw_matrix(
            payload=payload, ids=list(orders), legs=records("allPathDistanceVoList")
        ),
        # 停靠明细逐 DN 展示体积、重量和时间窗，只取页面用到的字段。
        "orders": {
            dn: {k: order.get(k) for k in _ORDER_FIELDS} for dn, order in orders.items()
        },
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
