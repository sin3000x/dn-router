# 业务回归测试：用小样本固定输入、输出及边界规则。
# pyright: reportTypedDictNotRequiredAccess=false
# 直接断言具体输出；缺少字段时测试本身会失败。
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from dn_router import Leg, Request, solve
from dn_router.problem import InputError, Problem, Route, validate_routes

ROOT = Path(__file__).resolve().parents[1]


# 构造可复用的完整小矩阵样本，各 DN 默认独立站点且不设时间窗。
def case(n=3):
    p = json.loads((ROOT / "examples/input-feasible.json").read_text())
    p["dnSiteVoList"] = [
        {
            **copy.deepcopy(p["dnSiteVoList"][0]),
            "orderReleaseId": f"D{i}",
            "siteCode": f"S{i}",
            "timeWindow": None,
        }
        for i in range(n)
    ]
    p["vehicleTypeVoList"] = [
        {"totalVolume": 10, "totalWeight": 10000, "vehicleTypeName": "large"},
        {"totalVolume": 2, "totalWeight": 1000, "vehicleTypeName": "small"},
    ]
    ids = [p["sourceLocation"]] + [d["orderReleaseId"] for d in p["dnSiteVoList"]]
    # 无向矩阵每对点一条，仓库总在 from 一侧。
    p["pathDistanceVoList"] = [
        {"from": a, "to": b, "planDistance": 10 if i == 0 else 1, "duration": 1}
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
    ]
    p["quotationModel"] = "city"
    p["maxSiteQty"] = 0
    return p


# 统一验证可行状态，并核对保留 DN 无漏单、无重复。
def run(p):
    r = solve(p, time_limit_ms=180)
    assert r["status"] == "FEASIBLE", r
    expected = {d["orderReleaseId"] for d in p["dnSiteVoList"]} - {
        d["orderReleaseId"] for d in r["rejectedOrders"]
    }
    actual = [dn for route in r["routes"] for dn in route["siteAddresses"]]
    assert set(actual) == expected and len(actual) == len(expected)
    return r


# 原始示例超过全部车型容量，必须整批失败并返回容量错误。
def test_original_is_capacity_error():
    p = json.loads((ROOT / "examples/input-original.json").read_text())
    r = solve(p)
    assert r["status"] == "INFEASIBLE_INPUT"
    assert r["errors"][0]["code"] == "DN_EXCEEDS_ALL_VEHICLES"
    assert not r["routes"]


# 单单路线只计出仓距离，且选择容量足够的最小车型。
def test_open_route_small_vehicle():
    r = run(case(1))
    assert r["totalDistance"] == 10
    assert r["routes"][0]["vehicle"] == "small"


# 请求及各层嵌套对象允许额外字段，忽略后模型和求解行为保持一致。
def test_extra_fields_are_ignored():
    p = case(1)
    expected = Request.model_validate(p)
    p["extra"] = {"metadata": True}
    for field in ("dnSiteVoList", "vehicleTypeVoList", "pathDistanceVoList"):
        p[field][0]["extra"] = "ignored"
    assert Request.model_validate(p) == expected
    assert run(p)["servedOrderCount"] == 1


# 直接传入 Request 与传入 JSON 值结果一致；业务规则错误仍返回 INVALID_INPUT。
def test_request_object_input():
    p = case()
    as_dict, as_model = run(p), solve(Request.model_validate(p))
    for key in ("status", "taskNo", "rejectedOrders", "servedOrderCount"):
        assert as_model[key] == as_dict[key]
    q = case(2)
    q["pathDistanceVoList"] = q["pathDistanceVoList"][1:]
    assert solve(Request.model_validate(q))["status"] == "INVALID_INPUT"


# 矩阵边在 Python 里用 origin 构造，序列化回 JSON 时仍为 from。
def test_leg_constructed_by_name():
    leg = Leg(origin="W", to="D1", planDistance=Decimal("1.5"))
    assert leg == Leg(**{"from": "W", "to": "D1", "planDistance": 1.5})
    request = Request.model_validate(case(1))
    request.pathDistanceVoList[0] = leg
    assert request.model_dump(by_alias=True)["pathDistanceVoList"][0]["from"] == "W"


# 车辆数优先时允许升级车型，将多单合并到一辆车。
def test_vehicle_count_primary():
    r = run(case())
    assert r["vehicleCount"] == 1
    assert r["routes"][0]["vehicle"] == "large"


@pytest.mark.parametrize("mode,expected", [(0, 1), (1, 1), (2, 3), (None, 3)])
# 同一输入按站点、坐标或 DN 计数时，应得到对应的车辆拆分结果。
def test_distinct_count(mode, expected):
    p = case()
    for d in p["dnSiteVoList"]:
        d["siteCode"] = "S"
    p["lonAndLatCombination"] = mode
    p["maxSiteQty"] = 1
    r = run(p)
    assert r["vehicleCount"] == expected


# 负坐标临界小数按 ROUND_HALF_UP 舍入后应归入同组。
def test_geo_half_up():
    p = case(2)
    p["dnSiteVoList"][0]["deliveryLatitude"] = "-20.1234565"
    p["dnSiteVoList"][1]["deliveryLatitude"] = "-20.123457"
    q = Request.model_validate(p)
    assert q.dnSiteVoList[0].group_key(1) == q.dnSiteVoList[1].group_key(1)


# 同车同站共享 eta，紧窗口下只计算一次卸货时间。
def test_unload_once_same_site_tight_window():
    p = case(2)
    for d in p["dnSiteVoList"]:
        d["siteCode"] = "S"
        d["timeWindow"] = [661, 662]
    r = run(p)
    assert r["vehicleCount"] == 1
    details = r["routes"][0]["siteDetails"]
    assert len(details) == 2
    assert details[0]["eta"] == details[1]["eta"]
    schedule = Problem.from_request(Request.model_validate(p)).schedule([1, 2])
    assert schedule[0] == schedule[1]
    assert schedule[0][2] - schedule[0][1] == 30


# 同站时间窗不相交时不能共用一次停靠，需要分车。
def test_same_site_disjoint_windows_use_two_vehicles():
    p = case(2)
    for d in p["dnSiteVoList"]:
        d["siteCode"] = "S"
    p["dnSiteVoList"][0]["timeWindow"] = [720, 730]
    p["dnSiteVoList"][1]["timeWindow"] = [840, 850]
    r = run(p)
    assert r["vehicleCount"] == 2
    assert all(len(x["siteDetails"]) == 1 for x in r["routes"])


# 站间距离是硬限制，但不限制仓库到首站。
def test_inter_stop_limit_is_hard_and_not_depot_limit():
    p = case(2)
    p["siteDistanceLimit"] = 0.5
    assert run(p)["vehicleCount"] == 2


# 站间距离和超过首段时拆车，关闭首段约束后允许合并。
def test_first_leg_constraint():
    p = case(2)
    for leg in p["pathDistanceVoList"]:
        leg["planDistance"] = 1 if leg["from"] == p["sourceLocation"] else 2
    p["distanceLessThanFirstLeg"] = True
    assert run(p)["vehicleCount"] == 2
    p["distanceLessThanFirstLeg"] = False
    assert run(p)["vehicleCount"] == 1


# 首段预算跨多段站间距离累计扣减：恰好用完允许，超出一点即须拆车。
# 要求解来自 OR-Tools，确认预算维度既不误放行也不误拒绝初解。
@pytest.mark.parametrize(("first_leg", "vehicles"), [(2, 1), (1, 2)])
def test_first_leg_budget_spans_multiple_legs(first_leg, vehicles):
    p = case(3)
    for leg in p["pathDistanceVoList"]:
        is_depot = leg["from"] == p["sourceLocation"]
        leg["planDistance"] = first_leg if is_depot else 1
    p["distanceLessThanFirstLeg"] = True
    r = run(p)
    assert r["vehicleCount"] == vehicles
    assert r["solutionSource"] == "ortools"


# 仅空窗及最早出仓直送仍超时的 DN 可被逐单剔除。
def test_time_exclusions_only():
    p = case(3)
    p["dnSiteVoList"][0]["timeWindow"] = []
    p["dnSiteVoList"][1]["timeWindow"] = [600, 660]
    r = run(p)
    assert {x["code"] for x in r["rejectedOrders"]} == {
        "EMPTY_TIME_WINDOW",
        "DIRECT_TIME_WINDOW_UNREACHABLE",
    }
    assert r["servedOrderCount"] == 1


# 关闭时间窗后，原本空窗的 DN 仍必须配送。
def test_disable_time_windows():
    p = case(1)
    p["dnSiteVoList"][0]["timeWindow"] = []
    p["enableTimeWindows"] = False
    assert run(p)["servedOrderCount"] == 1


# 允许最早出仓后等待，出发上限不应误当成到货上限。
def test_waiting_and_latest_departure():
    p = case(1)
    p["latestShippingDate"] = 665
    p["dnSiteVoList"][0]["timeWindow"] = [840, 900]
    r = run(p)
    assert r["routes"][0]["departure_date"] == 660
    assert r["routes"][0]["siteDetails"][0]["eta"] == 840


# 旧版请求不带出仓时间、卸货时长、耗时和时间窗，输出也不含时间字段。
def test_legacy_request_without_time():
    p = case(3)
    for field in ("earliestShippingDate", "unloadingTime", "latestShippingDate"):
        p.pop(field, None)
    for leg in p["pathDistanceVoList"]:
        del leg["duration"]
    for dn in p["dnSiteVoList"]:
        del dn["timeWindow"]
    r = run(p)
    assert r["vehicleCount"] == 1 and r["totalDistance"] == 12
    route = r["routes"][0]
    assert list(route) == ["dist", "siteAddresses", "vehicle", "volume", "weight"]


# 旧版请求忽略时间窗，即使给出空窗或不可达窗口也不剔除。
def test_legacy_request_ignores_time_windows():
    p = case(2)
    del p["earliestShippingDate"]
    p["dnSiteVoList"][0]["timeWindow"] = []
    p["dnSiteVoList"][1]["timeWindow"] = [0, 1]
    r = run(p)
    assert r["servedOrderCount"] == 2 and not r["rejectedOrders"]
    assert "siteDetails" not in r["routes"][0]


# 旧版请求不建时间维度，同车同站 DN 仍须连续停靠。
def test_legacy_request_keeps_same_site_contiguous():
    p = case(6)
    del p["earliestShippingDate"]
    for i, dn in enumerate(p["dnSiteVoList"]):
        dn["siteCode"] = f"S{i % 2}"
    route = run(p)["routes"][0]["siteAddresses"]
    sites = [int(dn[1:]) % 2 for dn in route]
    assert sites in ([0, 0, 0, 1, 1, 1], [1, 1, 1, 0, 0, 0])


# 带出仓时间的请求仍要求耗时；只有出仓上限没有下限时拒绝。
def test_timed_request_still_requires_duration():
    p = case(2)
    del p["pathDistanceVoList"][0]["duration"]
    assert solve(p)["status"] == "INVALID_INPUT"
    p = case(1)
    del p["earliestShippingDate"]
    p["latestShippingDate"] = 665
    assert solve(p)["status"] == "INVALID_INPUT"


# 缺少必需的矩阵边时返回输入错误，不能默认距离为零。
def test_missing_matrix_is_error():
    p = case(2)
    p["pathDistanceVoList"] = [
        leg
        for leg in p["pathDistanceVoList"]
        if not (leg["from"] == "D0" and leg["to"] == "D1")
    ]
    assert solve(p)["status"] == "INVALID_INPUT"


# 同一对点的重复记录（任一方向）若取值冲突，必须拒绝输入。
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_matrix_is_error(reverse):
    p = case(1)
    leg = p["pathDistanceVoList"][0]
    duplicate = {**leg, "duration": 100}
    if reverse:
        duplicate.update({"from": leg["to"], "to": leg["from"]})
    p["pathDistanceVoList"].append(duplicate)
    assert solve(p)["status"] == "INVALID_INPUT"


# 无向边两个方向共用：反向书写或两向都给同值，结果一致。
def test_undirected_matrix_either_direction():
    p = case(3)
    expected = run(p)
    q = case(3)
    for leg in q["pathDistanceVoList"]:
        leg["from"], leg["to"] = leg["to"], leg["from"]
    q["pathDistanceVoList"] += [
        {**leg, "from": leg["to"], "to": leg["from"]} for leg in q["pathDistanceVoList"]
    ]
    actual = run(q)
    assert actual["totalDistance"] == expected["totalDistance"]
    assert actual["vehicleCount"] == expected["vehicleCount"]
    assert not actual["warnings"]


# 全距离矩阵不参与求解：取值再离谱也不影响结果，只给一条中文告警。
def test_full_matrix_is_ignored():
    p = case(2)
    expected = run(p)
    p["allPathDistanceVoList"] = [
        {**leg, "planDistance": 999, "duration": 999} for leg in p["pathDistanceVoList"]
    ]
    actual = run(p)
    assert actual["totalDistance"] == expected["totalDistance"]
    assert actual["warnings"] == [
        "FULL_MATRIX_IGNORED: 已忽略 allPathDistanceVoList，距离和耗时只取 pathDistanceVoList"
    ]


# 两项载荷必须同时适配同一种车型，不能拼接不同车型的容量。
def test_non_nested_vehicle_capacities():
    p = case(2)
    p["vehicleTypeVoList"] = [
        {"vehicleTypeName": "bulky", "totalVolume": 10, "totalWeight": 100},
        {"vehicleTypeName": "heavy", "totalVolume": 1, "totalWeight": 1000},
    ]
    p["dnSiteVoList"][0].update(totalGrossVolume=5, totalGrossWeight=90)
    p["dnSiteVoList"][1].update(totalGrossVolume=1, totalGrossWeight=500)
    assert run(p)["vehicleCount"] == 2


# 距离优先可选择多车；车辆数优先时应接受更长的合并路线。
def test_distance_precedes_vehicle_count():
    p = case(2)
    for leg in p["pathDistanceVoList"]:
        leg["planDistance"] = 1 if leg["from"] == p["sourceLocation"] else 10
    p["quotationModel"] = "distance"
    assert run(p)["vehicleCount"] == 2
    p["quotationModel"] = "province"
    assert run(p)["vehicleCount"] == 1


# 独立审计必须发现路线遗漏的有效 DN。
def test_independent_validator_detects_omission():
    p = Problem.from_request(Request.model_validate(case(2)))
    with pytest.raises(InputError, match="omitted"):
        validate_routes(problem=p, routes=[Route(nodes=[1], vehicle_type=0)])


# 求解流程产出无效路线时报告求解错误，不能伪装成输入无效或交付路线。
def test_audit_failure_is_solver_error(monkeypatch):
    from dn_router import solver

    monkeypatch.setattr(
        solver,
        "build_initial_routes",
        lambda problem, deadline: [Route(nodes=[1], vehicle_type=0)],
    )
    r = solve(case(2))
    assert r["status"] == "SOLVER_ERROR" and r["routes"] == []
    assert r["errors"][0]["code"] == "ROUTE_AUDIT_FAILED"


# 极短预算下仍需通过可行兜底覆盖全部订单。
def test_tiny_budget_keeps_all_orders():
    p = case(15)
    r = solve(p, time_limit_ms=1)
    assert r["status"] == "FEASIBLE"
    assert r["servedOrderCount"] == 15


# 无订单时返回无有效订单状态，不虚构配送路线。
def test_empty_input():
    assert solve(case(0))["status"] == "NO_ELIGIBLE_ORDERS"


# 载荷超出支持的小数精度必须报错，不能静默舍入。
def test_precision_not_silently_rounded():
    p = case(1)
    p["dnSiteVoList"][0]["totalGrossVolume"] = "1.0000001"
    assert solve(p)["status"] == "INVALID_INPUT"


# 公开 eta 表示开始卸货，验证提前等待、同站共享时间及输出字段。
def test_early_arrival_waits_and_outputs_service_start_only():
    p = case(3)
    for dn in p["dnSiteVoList"][:2]:
        dn["siteCode"] = "shared"
        dn["timeWindow"] = [840, 841]
    p["dnSiteVoList"][2]["timeWindow"] = [871, 871]
    r = run(p)
    assert r["vehicleCount"] == 1
    route = r["routes"][0]
    assert set(route) == {
        "dist",
        "siteAddresses",
        "departure_date",
        "siteDetails",
        "vehicle",
        "volume",
        "weight",
    }
    assert route["siteAddresses"] == [d["siteAddress"] for d in route["siteDetails"]]
    details = {d["siteAddress"]: d["eta"] for d in route["siteDetails"]}
    assert details == {
        "D0": 840,
        "D1": 840,
        "D2": 871,
    }
    for detail in route["siteDetails"]:
        assert set(detail) == {"siteAddress", "eta"}
    visits = Problem.from_request(Request.model_validate(p)).schedule([1, 2, 3])
    assert visits[0][0] < visits[0][1]


# CLI 默认输出路线数组并分流诊断；报告模式输出完整结果。
def test_cli_default_array_and_full_report(capsys):
    import sys
    from unittest.mock import patch

    from dn_router.cli import main

    for option in ([], ["--report"]):
        with patch.object(
            sys,
            "argv",
            ["dn-router", str(ROOT / "examples/input-feasible.json"), *option],
        ):
            assert main() == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        if option:
            assert output["status"] == "FEASIBLE"
            assert not captured.err
        else:
            assert isinstance(output, list)
            assert "eta" in output[0]["siteDetails"][0]
            assert json.loads(captured.err)["status"] == "FEASIBLE"


# 起止相同的单点时间窗合法，不能当空窗口剔除。
def test_single_point_window_is_valid():
    p = case(1)
    p["dnSiteVoList"][0]["timeWindow"] = [720, 720]
    assert run(p)["rejectedOrders"] == []
