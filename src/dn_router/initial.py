"""在有限预算内构造可行插入初解，再交给 OR-Tools 优化。"""

import math
from time import perf_counter

from .problem import DEPOT, Problem, Route

# 增量成本元组，按目标优先级逐位比较；全零表示与另开一辆直送车持平。
Delta = tuple[int, int, int]
NO_GAIN: Delta = (0, 0, 0)


def build_initial_routes(problem: Problem, deadline: float) -> list[Route]:
    """按扫描序逐单尝试插入已有路线；预算耗尽后仍逐单直送，保证不漏单。"""
    routes: list[Route] = []
    for node in _sweep_order(problem):
        direct = Route([node], problem.fitting_vehicles([node])[0])
        best = _best_insertion(problem, routes, direct, deadline)
        if best is None:
            # 未找到改善插入或预算已耗尽时，为该单保留可行直送路线。
            routes.append(direct)
        else:
            index, route = best
            routes[index] = route
    return routes


def _sweep_order(problem: Problem) -> list[int]:
    """按相对仓库的方位角排序，使相邻插入的 DN 在空间上聚集。

    坐标只决定插入顺序，不参与距离或可行性计算；同角度按首段距离、节点编号定序。
    """
    request = problem.request
    origin = float(request.sourceLatitude), float(request.sourceLongitude)

    def key(node: int) -> tuple[float, int, int]:
        dn = problem.dn(node)
        lat = float(dn.deliveryLatitude) - origin[0]
        lon = float(dn.deliveryLongitude) - origin[1]
        return math.atan2(lat, lon), problem.distance[DEPOT][node], node

    return sorted(range(1, len(problem.dns) + 1), key=key)


def _best_insertion(
    problem: Problem, routes: list[Route], direct: Route, deadline: float
) -> tuple[int, Route] | None:
    """在所有路线的所有位置中找目标严格优于另开直送车的插入。"""
    (node,) = direct.nodes
    best_delta, best = NO_GAIN, None
    for index, route in enumerate(routes):
        if perf_counter() >= deadline:
            break
        # 容量和站点数与插入位置无关，每条路线只检查一次。
        merged = [*route.nodes, node]
        vehicles = problem.fitting_vehicles(merged)
        if not vehicles or not problem.within_stop_limit(merged):
            continue
        base = problem.length(route.nodes) + problem.length(direct.nodes)
        for nodes in problem.insertions(route.nodes, node):
            merged_route = Route(nodes, vehicles[0])
            delta = _delta(problem, merged_route, route, direct, base)
            if delta < best_delta:
                best_delta, best = delta, (index, merged_route)
    return best


def _delta(
    problem: Problem, merged: Route, old: Route, direct: Route, base: int
) -> Delta:
    """合并后成本减去旧路线和直送路线成本；base 为两者距离之和。"""
    distance = problem.length(merged.nodes) - base
    # 每辆车的等级成本是车型下标加一，与求解器中的车辆固定成本一致。
    rank = merged.vehicle - old.vehicle - direct.vehicle - 1
    if problem.request.count_first:
        return (-1, distance, rank)
    return (distance, rank, 0)
