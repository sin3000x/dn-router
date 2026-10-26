"""输入归一化与独立路线审计；整数缩放必须精确，超出精度直接报错。

内部约定：
- 节点编号 0 为仓库，1..N 对应 Problem.dns 的下标加一；
- 车型用 Problem.vehicles（已按等级从小到大排序）的下标表示；
- 时间均为相对最早出仓时刻的整数秒。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from itertools import groupby, pairwise
from typing import NamedTuple

from .models import DN, Leg, Request, Vehicle, group_key

# 距离保留三位小数，载荷保留六位小数；范围上限为后续整数运算留余量。
DIST_SCALE = 1000
LOAD_SCALE = 1_000_000
LIMIT = 2**60

DEPOT = 0

Window = tuple[int, int]
Matrix = list[list[int]]
Edges = dict[tuple[str, str], Leg]


class InputError(ValueError):
    """可向调用方报告的业务输入或路线约束错误。"""


class Route(NamedTuple):
    nodes: list[int]
    vehicle: int


class Visit(NamedTuple):
    """一次停靠的时间点；公开 eta 取 start（开始卸货）。"""

    arrival: int
    start: int
    finish: int


def scaled(value: Decimal | int, scale: int) -> int:
    """精确转成求解器整数单位，禁止静默舍入改变容量或距离约束。"""
    result = Decimal(value) * scale
    if result != result.to_integral_value() or abs(result) >= LIMIT:
        message = f"unsupported numeric precision/range: {value} (scale={scale})"
        raise InputError(message)
    return int(result)


@dataclass
class Problem:
    request: Request
    dns: list[DN]
    vehicles: list[Vehicle]
    distance: Matrix
    duration: Matrix
    windows: list[Window]
    horizon: int
    latest_departure: int
    unload_seconds: int
    rejected: list[dict[str, str]]
    warnings: list[str]

    def dn(self, node: int) -> DN:
        return self.dns[node - 1]

    def length(self, nodes: Sequence[int]) -> int:
        """开放路线长度：仓库到首站及站间路段，不计返仓。"""
        return sum(self.distance[a][b] for a, b in pairwise([DEPOT, *nodes]))

    def fitting_vehicles(self, nodes: Iterable[int]) -> list[int]:
        """同时装得下体积和重量的车型，按等级从小到大。"""
        dns = [self.dn(i) for i in nodes]
        volume = sum(d.totalGrossVolume for d in dns)
        weight = sum(d.totalGrossWeight for d in dns)
        return [
            k
            for k, vehicle in enumerate(self.vehicles)
            if volume <= vehicle.totalVolume and weight <= vehicle.totalWeight
        ]

    def schedule(self, nodes: Sequence[int]) -> list[Visit]:
        """按最早出仓顺推时间表，每个 DN 对应一个 Visit。

        同车同站的 DN 必须连续访问，组成一次停靠：共用到达和开始卸货时间，
        整组只计一次卸货时长；离站后不能再次访问该站。
        """
        time, previous, visits, seen = 0, DEPOT, [], set()
        for site, group in groupby(nodes, key=lambda i: self.dn(i).siteCode):
            if site in seen:
                raise InputError("same vehicle revisits a site")
            seen.add(site)
            stop = list(group)
            # 先到达，再等待至该停靠全部 DN 的窗口交集。
            arrival = time + self.duration[previous][stop[0]]
            start = max(arrival, *(self.windows[i][0] for i in stop))
            if any(start > self.windows[i][1] for i in stop):
                raise InputError("route time window violation")
            finish = start + self.unload_seconds
            visits += [Visit(arrival, start, finish)] * len(stop)
            time, previous = finish, stop[-1]
        return visits

    def distinct_stops(self, nodes: Iterable[int]) -> int:
        """按请求的站点计数口径去重后的站点数。"""
        mode = self.request.lonAndLatCombination
        return len({group_key(self.dn(i), mode) for i in nodes})

    def within_stop_limit(self, nodes: Iterable[int]) -> bool:
        limit = self.request.maxSiteQty
        return not limit or self.distinct_stops(nodes) <= limit

    def check_route(self, nodes: Sequence[int]) -> None:
        """检查容量以外的路线约束，违规时抛错。"""
        if not self.within_stop_limit(nodes):
            raise InputError("distinct stop limit violation")
        self.check_sequence(nodes)

    def check_sequence(self, nodes: Sequence[int]) -> None:
        """检查与访问顺序相关的约束：站间距离、首段关联距离和时间窗。"""
        request = self.request
        between = [self.distance[a][b] for a, b in pairwise(nodes)]
        limit = request.siteDistanceLimit * DIST_SCALE
        if limit > 0 and any(d > limit for d in between):
            raise InputError("inter-stop distance violation")

        first_leg = self.distance[DEPOT][nodes[0]]
        if request.distanceLessThanFirstLeg and sum(between) > first_leg:
            raise InputError("first-leg distance constraint violation")

        self.schedule(nodes)


def validate_routes(problem: Problem, routes: Iterable[Route]) -> None:
    """独立审计所有输出，包括兜底方案；违规时抛错，禁止交付无效路线。"""
    served: list[int] = []
    for nodes, vehicle in routes:
        if not nodes:
            raise InputError("empty route in output")
        if vehicle not in problem.fitting_vehicles(nodes):
            raise InputError("capacity violation")
        problem.check_route(nodes)
        served += nodes
    # 保留的 DN 必须恰好出现一次，同时检查漏单和重复配送。
    if sorted(served) != list(range(1, len(problem.dns) + 1)):
        raise InputError("mandatory DN omitted or duplicated")


def prepare(request: Request) -> Problem:
    """建立权威矩阵、筛除允许剔除的时间窗异常，并构造有限时间范围。"""
    warnings: list[str] = []
    edges = _edges(request, warnings)
    dns, bounds, rejected = _eligible_dns(request, edges)
    distance, duration = _matrices(request.sourceLocation, dns, edges, warnings)
    # 以容积、载重、名称确定稳定车型等级，不假定容量互相嵌套。
    vehicles = sorted(
        request.vehicleTypeVoList,
        key=lambda v: (v.totalVolume, v.totalWeight, v.vehicleTypeName),
    )

    # 小时换算成整数秒后向上取整，避免低估卸货占用时间。
    hours = request.unloadingTime * 3600
    unload_seconds = int(hours.to_integral_value(rounding=ROUND_CEILING))

    latest = request.latestShippingDate
    latest_departure = _seconds(request, latest) if latest else 0

    # 时间上界覆盖最晚窗口、全部可能行驶及卸货，允许无时间窗订单等待。
    window_ends = [max(0, b[1]) for b in bounds if b]
    longest_leg = max(map(max, duration), default=0)
    horizon = max([0, latest_departure, *window_ends])
    horizon += len(distance) * (longest_leg + unload_seconds) + 1
    if horizon >= LIMIT:
        raise InputError("time range too large")
    _check_load_precision(dns, vehicles)

    unbounded = (0, horizon)
    windows = [unbounded] + [(max(0, b[0]), b[1]) if b else unbounded for b in bounds]

    return Problem(
        request=request,
        dns=dns,
        vehicles=vehicles,
        distance=distance,
        duration=duration,
        windows=windows,
        horizon=horizon,
        latest_departure=latest_departure if latest else horizon,
        unload_seconds=unload_seconds,
        rejected=rejected,
        warnings=warnings,
    )


def _seconds(request: Request, moment: datetime) -> int:
    return int((moment - request.earliestShippingDate).total_seconds())


def _edges(request: Request, warnings: list[str]) -> Edges:
    """按 (from, to) 索引矩阵；允许完全相同的重复边，冲突时拒绝请求。"""
    edges: Edges = {}
    for leg in request.allPathDistanceVoList:
        key = (leg.origin, leg.to)
        cost = (leg.planDistance, leg.duration)
        if key in edges and (edges[key].planDistance, edges[key].duration) != cost:
            raise InputError(f"conflicting matrix entries: {key}")
        edges[key] = leg
        if leg.origin == leg.to and any(cost):
            warnings.append(f"DIAGONAL_NORMALIZED: {leg.origin}")
    if request.pathDistanceVoList:
        warnings.append(
            "pathDistanceVoList is compatibility-only; "
            "allPathDistanceVoList is authoritative"
        )
    return edges


def _eligible_dns(
    request: Request, edges: Edges
) -> tuple[list[DN], list[Window | None], list[dict[str, str]]]:
    """仅剔除空时间窗、最早出仓直送仍超时的订单；矩阵缺失必须报错。

    返回保留的 DN、对应时间窗（秒，None 表示不限）和被剔除的订单。
    """
    dns: list[DN] = []
    bounds: list[Window | None] = []
    rejected: list[dict[str, str]] = []

    def reject(dn: DN, code: str) -> None:
        rejected.append({"orderReleaseId": dn.orderReleaseId, "code": code})

    for dn in request.dnSiteVoList:
        window = dn.timeWindow if request.enableTimeWindows else None
        if window == [] or (window and window[0] > window[1]):
            reject(dn, "EMPTY_TIME_WINDOW")
            continue

        path = (request.sourceLocation, dn.orderReleaseId)
        edge = edges.get(path)
        if edge is None or edge.duration is None:
            raise InputError(f"missing depot leg/duration: {' -> '.join(path)}")

        bound = None
        if window:
            bound = (_seconds(request, window[0]), _seconds(request, window[1]))
            if edge.duration > bound[1]:
                reject(dn, "DIRECT_TIME_WINDOW_UNREACHABLE")
                continue

        dns.append(dn)
        bounds.append(bound)
    return dns, bounds, rejected


def _matrices(
    depot: str, dns: list[DN], edges: Edges, warnings: list[str]
) -> tuple[Matrix, Matrix]:
    """构造按节点编号索引的距离（已缩放）和耗时矩阵。"""
    ids = [depot, *(d.orderReleaseId for d in dns)]
    distance = [[0] * len(ids) for _ in ids]
    duration = [[0] * len(ids) for _ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            # 开放路线不返仓，因此无需返仓边。
            if i == j or j == DEPOT:
                continue
            leg = edges.get((a, b))
            if leg is None or leg.duration is None:
                raise InputError(f"missing directed matrix leg/duration: {a} -> {b}")
            # 同 siteCode 视为一次物理停靠，内部距离及行驶耗时统一归零。
            if i != DEPOT and dns[i - 1].siteCode == dns[j - 1].siteCode:
                if leg.planDistance or leg.duration:
                    warnings.append(f"SAME_SITE_TRAVEL_NORMALIZED: {a} -> {b}")
                continue
            distance[i][j] = scaled(leg.planDistance, DIST_SCALE)
            duration[i][j] = leg.duration
    return distance, duration


def _check_load_precision(dns: list[DN], vehicles: list[Vehicle]) -> None:
    """预先检查载荷精度，避免仅在进入某个求解分支后才发现非法数值。"""
    for dn in dns:
        scaled(dn.totalGrossVolume, LOAD_SCALE)
        scaled(dn.totalGrossWeight, LOAD_SCALE)
    for vehicle in vehicles:
        scaled(vehicle.totalVolume, LOAD_SCALE)
        scaled(vehicle.totalWeight, LOAD_SCALE)
