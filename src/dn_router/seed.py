"""在有限预算内构造可行插入初解，再交给 OR-Tools 优化。"""

from time import perf_counter

from .problem import Problem, Route

# 增量成本元组，按目标优先级逐位比较；全零表示与另开一辆直送车持平。
Delta = tuple[int, int, int]
NO_GAIN: Delta = (0, 0, 0)


def build_seed(problem: Problem, deadline: float) -> list[Route]:
    """逐单尝试插入已有路线；预算耗尽后仍逐单直送，保证不漏单。"""
    routes: list[Route] = []
    for node in range(1, len(problem.dns) + 1):
        direct = Route([node], problem.fitting_vehicles([node])[0])
        best = _best_insertion(problem, routes, direct, deadline)
        if best is None:
            # 未找到改善插入或预算已耗尽时，为该单保留可行直送路线。
            routes.append(direct)
        else:
            index, route = best
            routes[index] = route
    return routes


def _best_insertion(
    problem: Problem, routes: list[Route], direct: Route, deadline: float
) -> tuple[int, Route] | None:
    """在所有路线的所有位置中找目标严格优于另开直送车的插入。"""
    (node,) = direct.nodes
    best_delta, best = NO_GAIN, None
    for index, route in enumerate(routes):
        if perf_counter() >= deadline:
            break
        for position in range(len(route.nodes) + 1):
            nodes = route.nodes[:position] + [node] + route.nodes[position:]
            vehicle = problem.smallest_vehicle(nodes)
            if vehicle is None:
                continue
            delta = _delta(problem, Route(nodes, vehicle), route, direct)
            if delta < best_delta:
                best_delta, best = delta, (index, Route(nodes, vehicle))
    return best


def _delta(problem: Problem, merged: Route, old: Route, direct: Route) -> Delta:
    """合并后成本减去旧路线和直送路线成本。"""
    distance = problem.length(merged.nodes)
    distance -= problem.length(old.nodes) + problem.length(direct.nodes)
    # 每辆车的等级成本是车型下标加一，与求解器中的车辆固定成本一致。
    rank = merged.vehicle - old.vehicle - direct.vehicle - 1
    if problem.request.count_first:
        return (-1, distance, rank)
    return (distance, rank, 0)
