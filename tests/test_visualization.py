# 业务回归测试：用小样本固定输入、输出及边界规则。
import json
from decimal import Decimal
from urllib.parse import urljoin

import pytest

from dn_router import report_data


# 同站坐标必须全部齐全且一致；生成报告不能改写调用方结果。
def test_stop_coordinates_require_all_orders_to_agree():
    payload = {
        "dnSiteVoList": [
            {
                "orderReleaseId": "A",
                "deliveryLatitude": Decimal(20),
                "deliveryLongitude": Decimal(30),
            },
            {"orderReleaseId": "B", "deliveryLatitude": 21, "deliveryLongitude": 30},
        ],
        "sourceLatitude": 0,
        "sourceLongitude": 0,
    }
    result = {
        "routes": [
            {
                "stops": [
                    {"orderReleaseIds": ["A"]},
                    {"orderReleaseIds": ["A", "B"]},
                    {"orderReleaseIds": ["missing"]},
                ]
            }
        ]
    }
    data = report_data(payload=payload, result=result)
    assert data["depot"]["coordinate"] == [0, 0]
    assert [s["coordinate"] for s in data["result"]["routes"][0]["stops"]] == [
        [30, 20],
        None,
        None,
    ]
    assert "coordinate" not in result["routes"][0]["stops"][0]


# 输入字段无效且没有路线时也能生成可读状态报告。
def test_invalid_input_can_render_without_routes():
    data = report_data(
        payload={
            "dnSiteVoList": None,
            "vehicleTypeVoList": None,
            "sourceLatitude": "bad",
        },
        result={"status": "INVALID_INPUT", "routes": []},
    )
    assert data["depot"]["coordinate"] is None


# 错误集合类型和非有限数值不能破坏报告 JSON。
def test_malformed_input_collections_and_nonfinite_values():
    data = report_data(
        payload={
            "dnSiteVoList": 42,
            "vehicleTypeVoList": [{"totalVolume": float("nan")}],
        },
        result={"status": "INVALID_INPUT", "routes": []},
    )
    assert data["vehicles"][0]["totalVolume"] is None
    assert data["request"]["dnCount"] == 0 and data["orders"] == {}


# 页面回显关键入参与逐 DN 明细；缺省字段保留为 null，由页面说明默认值。
def test_request_summary_and_order_details():
    payload = {
        "sourceLocation": "WH",
        "lonAndLatCombination": 0,
        "maxSiteQty": 2,
        "unloadingTime": 30,
        "dnSiteVoList": [
            {"orderReleaseId": "A", "siteCode": "S1", "totalGrossVolume": 1},
            {"orderReleaseId": "B", "siteCode": "S1", "timeWindow": []},
            {"orderReleaseId": "C", "siteCode": "S2"},
        ],
        "pathDistanceVoList": [{}, {}],
    }
    data = report_data(
        payload=payload, result={"status": "INVALID_INPUT", "routes": []}
    )
    request = data["request"]
    assert request["sourceLocation"] == "WH" and request["unloadingTime"] == 30
    assert request["lonAndLatCombination"] == 0 and request["maxSiteQty"] == 2
    assert request["earliestShippingDate"] is None
    assert (request["dnCount"], request["siteCodeCount"], request["legCount"]) == (
        3,
        2,
        2,
    )
    assert data["orders"]["A"] == {
        "siteCode": "S1",
        "totalGrossVolume": 1,
        "totalGrossWeight": None,
        "timeWindow": None,
    }
    assert data["orders"]["B"]["timeWindow"] == []


@pytest.mark.parametrize("report", [False, True])
# 两种 CLI 输出模式在输入无效时都写出 JSON 并返回退出码 2。
def test_cli_writes_json_for_invalid_input(tmp_path, monkeypatch, report):
    import sys

    from dn_router.cli import main

    source = tmp_path / "input.json"
    source.write_text("{}")
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dn-router",
            str(source),
            *(["--report"] if report else []),
            "-o",
            str(output),
        ],
    )
    assert main() == 2
    body = json.loads(output.read_text())
    assert body["status"] == "INVALID_INPUT" if report else body == []


# 构造包含等待的路线，供时间轴边界测试复用。
def timed_route(start=1000):
    return {
        "departureTime": start,
        "completionTime": 1040,
        "stops": [
            {
                "arrivalTime": 1020,
                "departureTime": 1030,
                "waitingBeforeArrival": 10,
                "orderReleaseIds": [],
            },
            {
                "arrivalTime": 1035,
                "departureTime": 1040,
                "waitingBeforeArrival": 0,
                "orderReleaseIds": [],
            },
        ],
    }


# 时间轴正确拆分行驶、等待、卸货，累计时长不重复。
def test_timeline_splits_wait_once():
    timeline = report_data(payload={}, result={"routes": [timed_route()]})["timeline"]
    assert timeline["startTime"] == 1000
    assert timeline["duration"] == 40
    segments = timeline["routes"][0]["segments"]
    assert [(s["kind"], s["start"], s["end"]) for s in segments] == [
        ("travel", 0, 10),
        ("wait", 10, 20),
        ("unload", 20, 30),
        ("travel", 30, 35),
        ("wait", 35, 35),
        ("unload", 35, 40),
    ]
    assert sum(s["end"] - s["start"] for s in segments) == 40
    assert [s["stopIndex"] for s in segments] == [0, 0, 0, 1, 1, 1]


# 不同发车时间共用全局起点，零时长行驶仍保留边界。
def test_timeline_multiple_departures_share_one_origin():
    timeline = report_data(
        payload={},
        result={"routes": [timed_route(), timed_route(1010)]},
    )["timeline"]
    assert [r["start"] for r in timeline["routes"]] == [0, 10]
    assert [r["end"] for r in timeline["routes"]] == [40, 40]
    assert (
        timeline["routes"][1]["segments"][0]["start"]
        == timeline["routes"][1]["segments"][0]["end"]
        == 10
    )


# 无效路线单独禁用播放，不能补造时间，也不能影响其他有效路线。
def test_timeline_invalid_or_absent_times_do_not_invent_a_schedule():
    route = timed_route()
    route["stops"][0]["waitingBeforeArrival"] = (
        30  # 等待过长会使行驶起点早于出发时刻，应判为无效。
    )
    timeline = report_data(payload={}, result={"routes": [route, timed_route()]})[
        "timeline"
    ]
    assert timeline["routes"][0] is None
    assert timeline["routes"][1] is not None
    assert report_data(payload={}, result={"routes": []})["timeline"] is None


# 瞬时路线总时长保持为零，不为动画扩造时间。
def test_instantaneous_route_has_zero_duration_without_fabricated_time():
    route = {
        "departureTime": 660,
        "completionTime": 660,
        "stops": [
            {
                "arrivalTime": 660,
                "departureTime": 660,
                "waitingBeforeArrival": 0,
                "orderReleaseIds": [],
            }
        ],
    }
    timeline = report_data(payload={}, result={"routes": [route]})["timeline"]
    assert timeline["duration"] == 0
    assert all(s["start"] == s["end"] == 0 for s in timeline["routes"][0]["segments"])


# 逐 DN 结果在展示时合并同站停靠，卸货只计一次且不改写公开结果。
def test_public_route_contract_keeps_eta_and_counts_shared_unloading_once():
    import copy
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "examples/input-multi.json").read_text()
    )
    first, second, third = [dn["orderReleaseId"] for dn in payload["dnSiteVoList"]]
    public_route = {
        "dist": 110,
        "siteAddresses": [first, second, third],
        "departure_date": 660,
        "siteDetails": [
            {"siteAddress": first, "eta": 840},
            {"siteAddress": second, "eta": 840},
            {"siteAddress": third, "eta": 880},
        ],
        "vehicle": "VAN-BRA",
        "volume": 3,
        "weight": 300,
    }
    result = {"status": "FEASIBLE", "routes": [public_route]}
    original = copy.deepcopy(result)
    data = report_data(payload=payload, result=result)
    route = data["result"]["routes"][0]
    assert result == original
    assert route["physicalStopCount"] == route["siteCount"] == 2
    assert route["stops"][0]["orderReleaseIds"] == [first, second]
    assert route["stops"][0]["arrivalTime"] == 840
    assert route["stops"][0]["departureTime"] == 870
    assert route["stops"][0]["waitingBeforeArrival"] == 170
    assert route["stops"][1]["waitingBeforeArrival"] == 0
    assert route["completionTime"] == 910
    segments = data["timeline"]["routes"][0]["segments"]
    assert sum(s["end"] - s["start"] for s in segments if s["kind"] == "unload") == 60
    assert sum(s["end"] - s["start"] for s in segments if s["kind"] == "travel") == 20
    # 每次停靠带上一停靠过来的距离与耗时，取求解使用的矩阵。
    assert [(s["legDistance"], s["legDuration"]) for s in route["stops"]] == [
        (100, 10),
        (10, 10),
    ]
    matrix = data["matrix"]
    assert matrix["ids"] == ["PA_CROSSDO", first, second, third]
    assert matrix["distance"][0][3] == 100 and matrix["duration"][3][0] == 10


# 原始矩阵不依赖请求校验：缺失的边保留为 null，未知端点忽略。
def test_raw_matrix_keeps_missing_legs_as_null():
    payload = {
        "sourceLocation": "WH",
        "dnSiteVoList": [{"orderReleaseId": "A"}, {"orderReleaseId": "B"}],
        "pathDistanceVoList": [
            {"from": "WH", "to": "A", "planDistance": Decimal("1.5"), "duration": 60},
            {"from": "A", "to": "X", "planDistance": 9},
        ],
    }
    matrix = report_data(
        payload=payload, result={"status": "INVALID_INPUT", "routes": []}
    )["matrix"]
    assert matrix["ids"] == ["WH", "A", "B"]
    assert matrix["distance"] == [[None, 1.5, None], [1.5, None, None], [None] * 3]
    assert matrix["duration"][0][1] == matrix["duration"][1][0] == 60


# 在线服务：返回页面；可列出并求解目录内文件、接收上传，拒绝越界路径。
@pytest.fixture
def served(tmp_path):
    import shutil
    import threading
    from pathlib import Path

    from dn_router.server import make_server

    shutil.copy(
        Path(__file__).resolve().parents[1] / "examples/input-multi.json", tmp_path
    )
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "skip.json").write_text("{}")
    (tmp_path.parent / "outside.json").write_text("{}")
    server = make_server(tmp_path, port=0, time_limit_ms=200)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fetch(url, body=None):
    from urllib.error import HTTPError
    from urllib.request import urlopen

    try:
        with urlopen(url, data=body) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def test_server_page_files_and_solve(served):
    status, html = fetch(url=urljoin(base=served, url="/"))
    assert status == 200 and "startOnline()" in html
    files = json.loads(fetch(url=urljoin(base=served, url="/api/files"))[1])["files"]
    assert [f["path"] for f in files] == ["input-multi.json"]
    status, body = fetch(
        url=urljoin(
            base=served, url="/api/solve?file=input-multi.json&time_limit_ms=200"
        )
    )
    data = json.loads(body)
    assert status == 200 and data["result"]["status"] == "FEASIBLE"
    assert data["output"]["routes"][0]["siteDetails"] and data["timeline"] is not None
    assert data["source"] == {"file": "input-multi.json", "timeLimitMs": 200}


def test_server_rejects_paths_outside_root_and_reports_bad_uploads(served):
    assert (
        fetch(url=urljoin(base=served, url="/api/solve?file=../outside.json"))[0] == 400
    )
    assert fetch(url=urljoin(base=served, url="/api/solve?file=missing.json"))[0] == 400
    status, body = fetch(url=urljoin(base=served, url="/api/solve"), body=b"{not json")
    assert status == 200 and json.loads(body)["result"]["status"] == "INVALID_INPUT"


# 旧版无时间请求也要按 siteAddresses 展示停靠，不能因缺少 siteDetails 显示 0 DN。
def test_legacy_routes_without_time_still_show_stops():
    from pathlib import Path

    from dn_router.cli import solve_text

    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "examples/input-multi.json").read_text()
    )
    for field in ("earliestShippingDate", "unloadingTime", "latestShippingDate"):
        payload.pop(field, None)
    payload, result = solve_text(raw=json.dumps(payload), time_limit_ms=300)
    data = report_data(payload=payload, result=result)
    routes = data["result"]["routes"]
    assert [dn for r in routes for s in r["stops"] for dn in s["orderReleaseIds"]] == [
        dn for r in result["routes"] for dn in r["siteAddresses"]
    ]
    assert all(r["departureTime"] is None for r in routes)
    assert all(s["legDuration"] is None for r in routes for s in r["stops"])
    assert data["timeline"] is None
