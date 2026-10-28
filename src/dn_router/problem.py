"""输入归一化与独立路线审计；整数缩放必须精确，超出精度直接报错。

内部约定：
- 节点编号 0 为仓库，1..N 对应 Problem.dns 的下标加一；
- 车型用 Problem.vehicles（已按等级从小到大排序）的下标表示；
- 时间均为相对最早出仓时刻的整数秒；旧版不考虑时间的请求耗时、卸货均为零，
  时间窗不限。
"""

import gc
import math
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import groupby, pairwise
from typing import NamedTuple

from .models import DN, Leg, Request, Vehicle

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
    """一条开放路线。

    Attributes:
        nodes: 按访问顺序排列的 DN 节点编号（1..N），不含仓库。
        vehicle: 车型在 Problem.vehicles 中的下标。
    """

    nodes: list[int]
    vehicle: int


class Visit(NamedTuple):
    """一次停靠的时间点，均为相对最早出仓的秒；公开 eta 取 start（开始卸货）。"""

    arrival: int
    start: int
    finish: int


def scaled(value: Decimal | int, scale: int) -> int:
    """精确转成求解器整数单位，禁止静默舍入改变容量或距离约束。"""
    result = Decimal(value) * scale
    # 先排除非有限值和超范围，再转 int；比 to_integral_value 比较快，矩阵上调用约 N² 次。
    if result.is_finite() and abs(result) < LIMIT:
        integer = int(result)
        if integer == result:
            return integer
    message = f"unsupported numeric precision/range: {value} (scale={scale})"
    raise InputError(message)


@contextmanager
def gc_paused() -> Iterator[None]:
    """暂停循环垃圾回收。

    校验和预处理一次性创建约 N² 个矩阵对象且全部存活，分代回收会反复扫描它们
    而回收不到任何东西；300 DN 时约占这一步耗时的 40%。只恢复进入前的状态，
    嵌套或并发调用时不会提前打开。
    """
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


@dataclass
class Problem:
    """归一化后的求解输入，矩阵和时间窗都按节点编号索引。

    Attributes:
        request: 原始请求。
        dns: 保留的 DN，dns[i - 1] 对应节点 i。
        vehicles: 车型，按 (容积, 载重, 名称) 从小到大排序，下标即车型等级。
        distance: 距离矩阵 [from][to]，单位为输入距离 × DIST_SCALE 的整数；
            返仓边和同站 DN 之间为 0。
        duration: 行驶耗时矩阵 [from][to]，整数秒；返仓边和同站 DN 之间为 0。
        windows: 节点 0..N 的开始卸货区间 (最早, 最晚)，相对最早出仓的秒；
            仓库及无时间窗的 DN 为 (0, horizon)。
        horizon: 时间上界（秒），覆盖全部可能的行驶、等待与卸货。
        latest_departure: 最晚出仓时刻（秒）；请求未给出时等于 horizon。
        unload_seconds: 每次实际停靠的卸货时长，整数秒（向上取整）。
        rejected: 预处理剔除的订单，元素为 {"orderReleaseId": ..., "code": ...}。
        warnings: 归一化过程中产生的提示文本。
    """

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

    @classmethod
    def from_request(cls, request: Request) -> "Problem":
        """建立权威矩阵、筛除允许剔除的时间窗异常，并构造有限时间范围。"""
        warnings: list[str] = []
        edges = _edges(request, warnings)
        dns, bounds, rejected = _eligible_dns(request, edges)
        distance, duration = _matrices(
            request.sourceLocation, dns, edges, warnings, timed=request.timed
        )
        # 以容积、载重、名称确定稳定车型等级，不假定容量互相嵌套。
        vehicles = sorted(
            request.vehicleTypeVoList,
            key=lambda v: (v.totalVolume, v.totalWeight, v.vehicleTypeName),
        )
        # 小时换算成秒后向上取整，避免低估卸货占用时间。
        # 旧版请求不考虑时间，忽略卸货时长。
        unload_seconds = math.ceil(request.unloadingTime * 3600) if request.timed else 0

        latest = request.latestShippingDate
        latest_departure = _seconds(request, latest) if latest else 0

        # 时间上界覆盖最晚窗口、全部可能行驶及卸货，允许无时间窗订单等待。
        longest_leg = max(map(max, duration))
        horizon = max([0, latest_departure, *(b[1] for b in bounds if b)])
        horizon += len(distance) * (longest_leg + unload_seconds) + 1
        if horizon >= LIMIT:
            raise InputError("time range too large")
        _check_load_precision(dns, vehicles)

        unbounded = (0, horizon)
        windows = [unbounded] + [
            (max(0, b[0]), b[1]) if b else unbounded for b in bounds
        ]
        return cls(
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

        Returns:
            与 nodes 一一对应的 Visit 列表。

        Raises:
            InputError: 同车离站后再次访问该站，或无法满足时间窗。
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

    def insertions(self, nodes: Sequence[int], node: int) -> Iterator[list[int]]:
        """node 插入 nodes 各位置后，满足顺序类约束的候选路线。"""
        for position in range(len(nodes) + 1):
            candidate = [*nodes[:position], node, *nodes[position:]]
            try:
                self.check_sequence(candidate)
            except InputError:
                continue
            yield candidate

    def admits(self, nodes: Sequence[int]) -> bool:
        """容量与站点数可行；二者与访问顺序无关。"""
        return bool(self.fitting_vehicles(nodes)) and self.within_stop_limit(nodes)

    def distinct_stops(self, nodes: Iterable[int]) -> int:
        """按请求的站点计数口径去重后的站点数。"""
        mode = self.request.lonAndLatCombination
        return len({self.dn(i).group_key(mode) for i in nodes})

    def within_stop_limit(self, nodes: Iterable[int]) -> bool:
        limit = self.request.maxSiteQty
        return not limit or self.distinct_stops(nodes) <= limit

    def check_route(self, nodes: Sequence[int]) -> None:
        """检查容量以外的路线约束，违规时抛错。"""
        if not self.within_stop_limit(nodes):
            raise InputError("distinct stop limit violation")
        self.check_sequence(nodes)

    def check_sequence(self, nodes: Sequence[int]) -> None:
        """检查与访问顺序相关的约束：站间距离、首段关联距离和时间窗。

        nodes 不能为空。

        Raises:
            InputError: 任一约束不满足。
        """
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


def _seconds(request: Request, moment: datetime) -> int:
    assert request.earliestShippingDate is not None
    return int((moment - request.earliestShippingDate).total_seconds())


def _edges(request: Request, warnings: list[str]) -> Edges:
    """按 (from, to) 索引矩阵；允许完全相同的重复边，冲突时拒绝请求。"""
    edges: Edges = {}
    for leg in request.allPathDistanceVoList:
        key = (leg.origin, leg.to)
        known = edges.get(key)
        if known is not None and (
            known.planDistance != leg.planDistance or known.duration != leg.duration
        ):
            raise InputError(f"conflicting matrix entries: {key}")
        edges[key] = leg
        if leg.origin == leg.to and (leg.planDistance or leg.duration):
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

    旧版不考虑时间的请求忽略时间窗，也不要求 duration。
    返回保留的 DN、对应时间窗（秒，None 表示不限）和被剔除的订单。
    """
    dns: list[DN] = []
    bounds: list[Window | None] = []
    rejected: list[dict[str, str]] = []

    def reject(dn: DN, code: str) -> None:
        rejected.append({"orderReleaseId": dn.orderReleaseId, "code": code})

    for dn in request.dnSiteVoList:
        window = dn.timeWindow if request.timed and request.enableTimeWindows else None
        if window == [] or (window and window[0] > window[1]):
            reject(dn, "EMPTY_TIME_WINDOW")
            continue

        path = (request.sourceLocation, dn.orderReleaseId)
        edge = edges.get(path)
        if edge is None or (request.timed and edge.duration is None):
            raise InputError(f"missing depot leg/duration: {' -> '.join(path)}")

        bound = None
        if window:
            assert edge.duration is not None
            bound = (_seconds(request, window[0]), _seconds(request, window[1]))
            if edge.duration > bound[1]:
                reject(dn, "DIRECT_TIME_WINDOW_UNREACHABLE")
                continue

        dns.append(dn)
        bounds.append(bound)
    return dns, bounds, rejected


def _matrices(
    depot: str, dns: list[DN], edges: Edges, warnings: list[str], *, timed: bool
) -> tuple[Matrix, Matrix]:
    """构造按节点编号索引的距离（已缩放）和耗时矩阵；timed 为假时耗时全为零。"""
    ids = [depot, *(d.orderReleaseId for d in dns)]
    # 仓库没有 siteCode，用 None 占位，不会与任何 DN 同站。
    sites = [None, *(d.siteCode for d in dns)]
    distance = [[0] * len(ids) for _ in ids]
    duration = [[0] * len(ids) for _ in ids]
    for i, a in enumerate(ids):
        site, distance_row, duration_row = sites[i], distance[i], duration[i]
        # 开放路线不返仓，因此无需返仓边，j 从 1 开始。
        for j in range(1, len(ids)):
            if i == j:
                continue
            b = ids[j]
            leg = edges.get((a, b))
            seconds = leg.duration if leg and timed else 0
            if leg is None or seconds is None:
                raise InputError(f"missing directed matrix leg/duration: {a} -> {b}")
            # 同 siteCode 视为一次物理停靠，内部距离及行驶耗时统一归零。
            if site == sites[j]:
                if leg.planDistance or seconds:
                    warnings.append(f"SAME_SITE_TRAVEL_NORMALIZED: {a} -> {b}")
                continue
            distance_row[j] = scaled(leg.planDistance, DIST_SCALE)
            duration_row[j] = seconds
    return distance, duration


def _check_load_precision(dns: list[DN], vehicles: list[Vehicle]) -> None:
    """预先检查载荷精度，避免仅在进入某个求解分支后才发现非法数值。"""
    for dn in dns:
        scaled(dn.totalGrossVolume, LOAD_SCALE)
        scaled(dn.totalGrossWeight, LOAD_SCALE)
    for vehicle in vehicles:
        scaled(vehicle.totalVolume, LOAD_SCALE)
        scaled(vehicle.totalWeight, LOAD_SCALE)
