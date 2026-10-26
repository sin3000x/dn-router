"""固定随机种子的合成冒烟基准，用于本地对比，不代表生产性能承诺。"""

import argparse
import json
import math
import random
import statistics
from typing import Any

from dn_router import solve


# 生成完整的仓库至 DN、DN 至 DN 有向矩阵，直线距离仅用于合成样本。
def generate(n: int, seed: int) -> dict[str, Any]:
    # 使用局部随机源保证同一种子可复现，且不改变其他代码的随机状态。
    rng = random.Random(seed)
    points = [(0.0, 0.0)] + [
        (rng.uniform(-50, 50), rng.uniform(-50, 50)) for _ in range(n)
    ]
    ids = ["DEPOT"] + [f"DN{i}" for i in range(n)]
    # 不生成对角线和返仓边，符合开放路线输入要求。
    matrix = [
        {
            "from": a,
            "to": b,
            "planDistance": (d := round(math.dist(points[i], points[j]), 3)),
            "duration": math.ceil(d * 60),
        }
        for i, a in enumerate(ids)
        for j, b in enumerate(ids)
        if i != j and j
    ]
    return {
        "sourceLocation": "DEPOT",
        "sourceLatitude": 0,
        "sourceLongitude": 0,
        "earliestShippingDate": "2026-10-21 08:00",
        "unloadingTime": 0.25,
        "maxSiteQty": 5,
        "quotationModel": "city",
        "lonAndLatCombination": 0,
        "distanceLessThanFirstLeg": True,
        "siteDistanceLimit": 40,
        "allPathDistanceVoList": matrix,
        "dnSiteVoList": [
            {
                "orderReleaseId": ids[i + 1],
                "siteCode": f"S{i}",
                "deliveryLatitude": points[i + 1][0],
                "deliveryLongitude": points[i + 1][1],
                "totalGrossVolume": 1,
                "totalGrossWeight": 100,
                "timeWindow": ["2026-10-21 10:00", "2026-10-21 18:00"],
            }
            for i in range(n)
        ],
        "vehicleTypeVoList": [
            {"vehicleTypeName": f"T{i}", "totalVolume": v, "totalWeight": v * 500}
            for i, v in enumerate([2, 4, 18, 27, 39, 65])
        ],
    }


# 逐种子执行并确认没有漏单，输出总体耗时及各阶段指标供人工比较。
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--dn", type=int, default=100)
    # 可给多个预算，观察同一批样本的目标值随时限的变化。
    parser.add_argument("--time-limit-ms", type=int, nargs="+", default=[200])
    args = parser.parse_args()
    payloads = [generate(args.dn, seed) for seed in range(args.runs)]
    summaries = [summarize(args, payloads, budget) for budget in args.time_limit_ms]
    output = summaries[0] if len(summaries) == 1 else summaries
    print(json.dumps(output, indent=2))


def summarize(
    args: argparse.Namespace, payloads: list[dict[str, Any]], budget: int
) -> dict[str, Any]:
    records = []
    for payload in payloads:
        result = solve(payload, time_limit_ms=budget)
        assert (
            result["status"] == "FEASIBLE" and result["servedOrderCount"] == args.dn
        ), result
        records.append(
            {
                key: result[key]
                for key in (
                    "elapsedMs",
                    "vehicleCount",
                    "totalDistance",
                    "solutionSource",
                    "timingsMs",
                )
            }
        )
    elapsed = [x["elapsedMs"] for x in records]
    return {
        "dn": args.dn,
        "runs": args.runs,
        "budgetMs": budget,
        "medianMs": statistics.median(elapsed),
        "maxMs": max(elapsed),
        "meanVehicles": statistics.mean(x["vehicleCount"] for x in records),
        "meanDistance": round(statistics.mean(x["totalDistance"] for x in records), 3),
        "records": records,
    }


if __name__ == "__main__":
    main()
