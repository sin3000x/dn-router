# 业务回归测试：用小样本固定输入、输出及边界规则。
import json
import re
from decimal import Decimal

import pytest

from dn_router import render_html


# 提取内嵌 JSON，检查展示数据而不依赖页面文字匹配。
def report_data(html):
    return json.loads(
        re.search(r'id="report-data">(.*?)</script>', html, re.DOTALL).group(1)
    )


# 恶意字段不能闭合脚本或注入标签，解码后仍应保留原始业务文本。
def test_embedded_data_cannot_close_script_or_inject_html():
    attack = "</script><img src=x onerror=alert(1)>"
    html = render_html(
        {}, {"status": "INVALID_INPUT", "errors": [{"message": attack}], "routes": []}
    )
    assert attack not in html
    assert report_data(html)["result"]["errors"][0]["message"] == attack
    assert "<script src=" not in html


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
    data = report_data(render_html(payload, result))
    assert data["depot"]["coordinate"] == [0, 0]
    assert [s["coordinate"] for s in data["result"]["routes"][0]["stops"]] == [
        [30, 20],
        None,
        None,
    ]
    assert "coordinate" not in result["routes"][0]["stops"][0]


# 输入字段无效且没有路线时也能生成可读状态报告。
def test_invalid_input_can_render_without_routes():
    html = render_html(
        {"dnSiteVoList": None, "vehicleTypeVoList": None, "sourceLatitude": "bad"},
        {"status": "INVALID_INPUT", "routes": []},
    )
    assert report_data(html)["depot"]["coordinate"] is None


# 错误集合类型和非有限数值不能破坏报告 JSON。
def test_malformed_input_collections_and_nonfinite_values():
    html = render_html(
        {"dnSiteVoList": 42, "vehicleTypeVoList": [{"totalVolume": float("nan")}]},
        {"status": "INVALID_INPUT", "routes": []},
    )
    assert report_data(html)["vehicles"][0]["totalVolume"] is None


@pytest.mark.parametrize("report", [False, True])
# 两种 CLI 输出模式均可同时生成 JSON 与错误状态 HTML。
def test_cli_generates_json_and_html(tmp_path, monkeypatch, report):
    import sys

    from dn_router.cli import main

    source = tmp_path / "input.json"
    source.write_text("{}")
    output, html = tmp_path / "result.json", tmp_path / "result.html"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dn-router",
            str(source),
            *(["--report"] if report else []),
            "--output",
            str(output),
            "--html",
            str(html),
        ],
    )
    assert main() == 2
    body = json.loads(output.read_text())
    assert body["status"] == "INVALID_INPUT" if report else body == []
    assert report_data(html.read_text())["result"]["status"] == "INVALID_INPUT"


# HTML 输出路径不得覆盖输入文件，失败后原文件保持完整。
def test_cli_rejects_overwriting_input_with_html(tmp_path, monkeypatch):
    import sys

    import pytest

    from dn_router.cli import main

    source = tmp_path / "input.json"
    source.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["dn-router", str(source), "--html", str(source)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert source.read_text() == "{}"


# 构造跨午夜且包含等待的路线，供时间轴边界测试复用。
def timed_route(start="2026-10-21 23:50:00"):
    return {
        "departureTime": start,
        "completionTime": "2026-10-22 00:30:00",
        "stops": [
            {
                "arrivalTime": "2026-10-22 00:10:00",
                "departureTime": "2026-10-22 00:20:00",
                "waitingBeforeArrivalSeconds": 600,
                "orderReleaseIds": [],
            },
            {
                "arrivalTime": "2026-10-22 00:25:00",
                "departureTime": "2026-10-22 00:30:00",
                "waitingBeforeArrivalSeconds": 0,
                "orderReleaseIds": [],
            },
        ],
    }


# 跨日时间轴正确拆分行驶、等待、卸货，累计时长不重复。
def test_timeline_crosses_midnight_and_splits_wait_once():
    timeline = report_data(render_html({}, {"routes": [timed_route()]}))["timeline"]
    assert timeline["startTime"] == "2026-10-21 23:50:00"
    assert timeline["durationSeconds"] == 2400
    segments = timeline["routes"][0]["segments"]
    assert [(s["kind"], s["start"], s["end"]) for s in segments] == [
        ("travel", 0, 600),
        ("wait", 600, 1200),
        ("unload", 1200, 1800),
        ("travel", 1800, 2100),
        ("wait", 2100, 2100),
        ("unload", 2100, 2400),
    ]
    assert sum(s["end"] - s["start"] for s in segments) == 2400
    assert [s["stopIndex"] for s in segments] == [0, 0, 0, 1, 1, 1]


# 不同发车时间共用全局起点，零时长行驶仍保留边界。
def test_timeline_multiple_departures_share_one_origin():
    timeline = report_data(
        render_html({}, {"routes": [timed_route(), timed_route("2026-10-22 00:00:00")]})
    )["timeline"]
    assert [r["start"] for r in timeline["routes"]] == [0, 600]
    assert [r["end"] for r in timeline["routes"]] == [2400, 2400]
    assert (
        timeline["routes"][1]["segments"][0]["start"]
        == timeline["routes"][1]["segments"][0]["end"]
        == 600
    )


# 无效路线单独禁用播放，不能补造时间，也不能影响其他有效路线。
def test_timeline_invalid_or_absent_times_do_not_invent_a_schedule():
    route = timed_route()
    route["stops"][0]["waitingBeforeArrivalSeconds"] = (
        2000  # 等待过长会使行驶起点早于出发时刻，应判为无效。
    )
    timeline = report_data(render_html({}, {"routes": [route, timed_route()]}))[
        "timeline"
    ]
    assert timeline["routes"][0] is None
    assert timeline["routes"][1] is not None
    assert report_data(render_html({}, {"routes": []}))["timeline"] is None


# 瞬时路线总时长保持为零，不为动画扩造时间。
def test_instantaneous_route_has_zero_duration_without_fabricated_time():
    route = {
        "departureTime": "2026-10-21 11:00:00",
        "completionTime": "2026-10-21 11:00:00",
        "stops": [
            {
                "arrivalTime": "2026-10-21 11:00:00",
                "departureTime": "2026-10-21 11:00:00",
                "waitingBeforeArrivalSeconds": 0,
                "orderReleaseIds": [],
            }
        ],
    }
    timeline = report_data(render_html({}, {"routes": [route]}))["timeline"]
    assert timeline["durationSeconds"] == 0
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
        "departure_date": "2026-10-21 11:00:00",
        "siteDetails": [
            {"siteAddress": first, "eta": "2026-10-21 14:00:00"},
            {"siteAddress": second, "eta": "2026-10-21 14:00:00"},
            {"siteAddress": third, "eta": "2026-10-21 14:40:00"},
        ],
        "vehicle": "VAN-BRA",
        "volume": 3,
        "weight": 300,
    }
    result = {"status": "FEASIBLE", "routes": [public_route]}
    original = copy.deepcopy(result)
    data = report_data(render_html(payload, result))
    route = data["result"]["routes"][0]
    assert result == original
    assert route["physicalStopCount"] == route["siteCount"] == 2
    assert route["stops"][0]["orderReleaseIds"] == [first, second]
    assert route["stops"][0]["arrivalTime"] == "2026-10-21 14:00:00"
    assert route["stops"][0]["departureTime"] == "2026-10-21 14:30:00"
    assert route["stops"][0]["waitingBeforeArrivalSeconds"] == 10200
    assert route["stops"][1]["waitingBeforeArrivalSeconds"] == 0
    assert route["completionTime"] == "2026-10-21 15:10:00"
    segments = data["timeline"]["routes"][0]["segments"]
    assert sum(s["end"] - s["start"] for s in segments if s["kind"] == "unload") == 3600
    assert sum(s["end"] - s["start"] for s in segments if s["kind"] == "travel") == 1200


# 在线服务：页面以 null 启动；可列出并求解目录内文件、接收上传，拒绝越界路径。
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
    status, html = fetch(served + "/")
    assert status == 200 and report_data(html) is None
    files = json.loads(fetch(served + "/api/files")[1])["files"]
    assert [f["path"] for f in files] == ["input-multi.json"]
    status, body = fetch(served + "/api/solve?file=input-multi.json&time_limit_ms=200")
    data = json.loads(body)
    assert status == 200 and data["result"]["status"] == "FEASIBLE"
    assert data["output"]["routes"][0]["siteDetails"] and data["timeline"] is not None
    assert data["source"] == {"file": "input-multi.json", "timeLimitMs": 200}


def test_server_rejects_paths_outside_root_and_reports_bad_uploads(served):
    assert fetch(served + "/api/solve?file=../outside.json")[0] == 400
    assert fetch(served + "/api/solve?file=missing.json")[0] == 400
    status, body = fetch(served + "/api/solve", b"{not json")
    assert status == 200 and json.loads(body)["result"]["status"] == "INVALID_INPUT"
