"""车辆数优先时的路线消除：拆掉整条路线并把其中的 DN 重插进其他路线。

OR-Tools 内置局部搜索一次只移动少量节点，清不空一条路线；这里补上这种复合移动。
只在车辆数高于下界时运行；所有中间方案都经完整约束检查，并始终保留车辆数最少的方案。
"""

import random
from fractions import Fraction
from math import ceil
from time import perf_counter

from .models import group_key
from .problem import InputError, Problem, Route

# 每轮只尝试拆最短的几条路线；连续失败过多说明下界很可能不可达，及时放弃。
CANDIDATES = 6
MAX_STALLS = 200
SHAKE_MOVES = 20

Plans = list[list[int]]


def reduce_routes(
    problem: Problem, routes: list[Route], deadline: float
) -> list[Route]:
    """在预算内尽量减少路线数；无法改善时原样返回。"""
    bound = vehicle_lower_bound(problem)
    rng = random.Random(0)  # 固定种子，结果可复现
    plans = best = [list(route.nodes) for route in routes]
    stalls = 0
    while len(best) > bound and stalls < MAX_STALLS and perf_counter() < deadline:
        if smaller := _empty_one_route(problem, plans, rng, deadline):
            plans, stalls = smaller, 0
        else:
            plans, stalls = _shake(problem, plans, rng), stalls + 1
        best = min(best, plans, key=len)
    if len(best) == len(routes):
        return routes
    return [Route(nodes, problem.fitting_vehicles(nodes)[0]) for nodes in best]


def vehicle_lower_bound(problem: Problem) -> int:
    """车辆数下界：总体积、总重量按最大车型装载，以及去重站点数按 maxSiteQty 装载。"""
    dns = problem.dns
    volume = sum(dn.totalGrossVolume for dn in dns)
    weight = sum(dn.totalGrossWeight for dn in dns)
    bounds = [
        ceil(Fraction(volume) / Fraction(max(v.totalVolume for v in problem.vehicles))),
        ceil(Fraction(weight) / Fraction(max(v.totalWeight for v in problem.vehicles))),
    ]
    if limit := problem.request.maxSiteQty:
        mode = problem.request.lonAndLatCombination
        bounds.append(ceil(len({group_key(dn, mode) for dn in dns}) / limit))
    return max(bounds, default=0)


def _empty_one_route(
    problem: Problem, plans: Plans, rng: random.Random, deadline: float
) -> Plans | None:
    """尝试清空最短的几条路线之一，成功则返回少一条路线的方案。"""
    order = sorted(range(len(plans)), key=lambda i: (len(plans[i]), rng.random()))
    for victim in order[:CANDIDATES]:
        if perf_counter() >= deadline:
            break
        rest = [nodes for i, nodes in enumerate(plans) if i != victim]
        displaced = rng.sample(plans[victim], len(plans[victim]))
        if all(_insert_cheapest(problem, rest, node) for node in displaced):
            return rest
    return None


def _insert_cheapest(problem: Problem, plans: Plans, node: int) -> bool:
    """把 node 插入使距离增量最小的可行位置；无处可插返回 False。"""
    best: tuple[int, int, list[int]] | None = None
    for index, nodes in enumerate(plans):
        if not problem.admits([*nodes, node]):
            continue
        base = problem.length(nodes)
        for candidate in problem.insertions(nodes, node):
            delta = problem.length(candidate) - base
            if best is None or delta < best[0]:
                best = delta, index, candidate
    if best is None:
        return False
    plans[best[1]] = best[2]
    return True


def _shake(problem: Problem, plans: Plans, rng: random.Random) -> Plans:
    """随机把节点挪到别的路线，保持可行，用来在满载路线间腾出空位。"""
    plans = list(plans)
    for _ in range(SHAKE_MOVES):
        if len(plans) < 2:
            break
        source, target = rng.sample(range(len(plans)), 2)
        node = rng.choice(plans[source])
        rest = [x for x in plans[source] if x != node]
        if rest and not _sequence_ok(problem, rest):
            continue
        if not problem.admits([*plans[target], node]):
            continue
        for candidate in problem.insertions(plans[target], node):
            plans[target], plans[source] = candidate, rest
            break
    return [nodes for nodes in plans if nodes]


def _sequence_ok(problem: Problem, nodes: list[int]) -> bool:
    try:
        problem.check_sequence(nodes)
    except InputError:
        return False
    return True
