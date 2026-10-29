# 路线消除阶段：只在车辆数优先且高于下界时减少路线，并保持所有约束可行。
from time import perf_counter

from test_solver import case

from dn_router.models import Request
from dn_router.problem import Problem, Route, validate_routes
from dn_router.reduce import reduce_routes, vehicle_lower_bound
from dn_router.solver import _seed_stages


def problem(n=6, max_site_qty=2, model="city"):
    payload = case(n)
    payload.update(maxSiteQty=max_site_qty, lonAndLatCombination=0)
    payload["quotationModel"] = model
    return Problem.from_request(Request.model_validate(payload))


# 下界取站点数、体积、重量三者的最大值；maxSiteQty=0 时不计站点数。
def test_lower_bound():
    assert vehicle_lower_bound(problem(n=6, max_site_qty=2)) == 3
    assert vehicle_lower_bound(problem(n=6, max_site_qty=0)) == 1


# 逐单直送的 6 条路线应被合并到下界，且通过独立审计。
def test_merges_direct_routes_down_to_bound():
    p = problem()
    direct = [
        Route(nodes=[i], vehicle_type=p.fitting_types([i])[0]) for i in range(1, 7)
    ]
    routes = reduce_routes(problem=p, routes=direct, deadline=perf_counter() + 1)
    assert len(routes) == vehicle_lower_bound(p) == 3
    validate_routes(problem=p, routes=routes)


# 已达下界时不做任何改动，原对象原样返回。
def test_noop_at_lower_bound():
    p = problem()
    routes = [
        Route(nodes=[1, 2], vehicle_type=0),
        Route(nodes=[3, 4], vehicle_type=0),
        Route(nodes=[5, 6], vehicle_type=0),
    ]
    assert (
        reduce_routes(problem=p, routes=routes, deadline=perf_counter() + 1) is routes
    )


# 预算已耗尽时同样原样返回，不破坏现有方案。
def test_expired_deadline_keeps_routes():
    p = problem()
    direct = [
        Route(nodes=[i], vehicle_type=p.fitting_types([i])[0]) for i in range(1, 7)
    ]
    assert (
        reduce_routes(problem=p, routes=direct, deadline=perf_counter() - 1) is direct
    )


# 阶段按报价模式挂接：仅车辆数优先时才包含路线消除。
def test_stage_only_for_count_first():
    count_first = Request.model_validate({**case(), "quotationModel": "city"})
    distance_first = Request.model_validate({**case(), "quotationModel": "distance"})
    assert [s.run for s in _seed_stages(count_first)][-1] is reduce_routes
    assert reduce_routes not in [s.run for s in _seed_stages(distance_first)]
