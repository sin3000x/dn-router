# 业务回归测试：用小样本固定输入、输出及边界规则。
import copy
import json
from pathlib import Path

import pytest

from dn_router import solve
from dn_router.models import Request
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
    p["pathDistanceVoList"] = []
    ids = [p["sourceLocation"]] + [d["orderReleaseId"] for d in p["dnSiteVoList"]]
    p["allPathDistanceVoList"] = [
        {"from": a, "to": b, "planDistance": 10 if a == ids[0] else 1, "duration": 60}
        for a in ids
        for b in ids
        if a != b
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
    for field in ("dnSiteVoList", "vehicleTypeVoList", "allPathDistanceVoList"):
        p[field][0]["extra"] = "ignored"
    p["pathDistanceVoList"] = [dict(p["allPathDistanceVoList"][0])]
    expected.pathDistanceVoList = expected.allPathDistanceVoList[:1]
    assert Request.model_validate(p) == expected
    assert run(p)["servedOrderCount"] == 1


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
        d["timeWindow"] = ["2026-10-21 11:01", "2026-10-21 11:02"]
    r = run(p)
    assert r["vehicleCount"] == 1
    details = r["routes"][0]["siteDetails"]
    assert len(details) == 2
    assert details[0]["eta"] == details[1]["eta"]
    schedule = Problem.from_request(Request.model_validate(p)).schedule([1, 2])
    assert schedule[0] == schedule[1]
    assert schedule[0][2] - schedule[0][1] == 1800


# 同站时间窗不相交时不能共用一次停靠，需要分车。
def test_same_site_disjoint_windows_use_two_vehicles():
    p = case(2)
    for d in p["dnSiteVoList"]:
        d["siteCode"] = "S"
    p["dnSiteVoList"][0]["timeWindow"] = ["2026-10-21 12:00", "2026-10-21 12:10"]
    p["dnSiteVoList"][1]["timeWindow"] = ["2026-10-21 14:00", "2026-10-21 14:10"]
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
    for leg in p["allPathDistanceVoList"]:
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
    for leg in p["allPathDistanceVoList"]:
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
    p["dnSiteVoList"][1]["timeWindow"] = ["2026-10-21 10:00", "2026-10-21 11:00"]
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
    p["latestShippingDate"] = "2026-10-21 11:05"
    p["dnSiteVoList"][0]["timeWindow"] = ["2026-10-21 14:00", "2026-10-21 15:00"]
    r = run(p)
    assert r["routes"][0]["departure_date"] == "2026-10-21 11:00:00"
    assert r["routes"][0]["siteDetails"][0]["eta"] == "2026-10-21 14:00:00"


# 缺少必需的有向矩阵边时返回输入错误，不能默认距离为零。
def test_missing_matrix_is_error():
    p = case(2)
    p["allPathDistanceVoList"] = [
        l
        for l in p["allPathDistanceVoList"]
        if not (l["from"] == "D0" and l["to"] == "D1")
    ]
    assert solve(p)["status"] == "INVALID_INPUT"


# 同一有向边的重复记录若耗时冲突，必须拒绝输入。
def test_conflicting_matrix_is_error():
    p = case(1)
    p["allPathDistanceVoList"].append(
        {**p["allPathDistanceVoList"][0], "duration": 100}
    )
    assert solve(p)["status"] == "INVALID_INPUT"


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
    for leg in p["allPathDistanceVoList"]:
        leg["planDistance"] = 1 if leg["from"] == p["sourceLocation"] else 10
    p["quotationModel"] = "distance"
    assert run(p)["vehicleCount"] == 2
    p["quotationModel"] = "province"
    assert run(p)["vehicleCount"] == 1


# 独立审计必须发现路线遗漏的有效 DN。
def test_independent_validator_detects_omission():
    p = Problem.from_request(Request.model_validate(case(2)))
    with pytest.raises(InputError, match="omitted"):
        validate_routes(p, [Route([1], 0)])


# 求解流程产出无效路线时报告求解错误，不能伪装成输入无效或交付路线。
def test_audit_failure_is_solver_error(monkeypatch):
    from dn_router import solver

    monkeypatch.setattr(
        solver, "build_initial_routes", lambda problem, end: [Route([1], 0)]
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
        dn["timeWindow"] = ["2026-10-21 14:00", "2026-10-21 14:01"]
    p["dnSiteVoList"][2]["timeWindow"] = ["2026-10-21 14:31", "2026-10-21 14:31"]
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
        "D0": "2026-10-21 14:00:00",
        "D1": "2026-10-21 14:00:00",
        "D2": "2026-10-21 14:31:00",
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
    p["dnSiteVoList"][0]["timeWindow"] = ["2026-10-21 12:00", "2026-10-21 12:00"]
    assert run(p)["rejectedOrders"] == []
