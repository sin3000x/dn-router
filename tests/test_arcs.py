# 弧矩阵：向量化结果必须与逐元素的业务定义完全一致，尤其是同站 DN 与 sink 的边界。
import itertools

import numpy as np
import pytest
from test_solver import case

from dn_router.arcs import distance_matrix, first_leg_budget, time_matrix
from dn_router.models import Request
from dn_router.problem import DEPOT, Problem


def problem(n=5, shared=True):
    payload = case(n)
    if shared:  # D0、D1 同站，D3、D4 同站
        for k, site in ((1, "S0"), (4, "S3")):
            payload["dnSiteVoList"][k]["siteCode"] = site
    # 让距离和耗时不对称、互不相同，避免巧合相等掩盖错误。
    for k, leg in enumerate(payload["allPathDistanceVoList"]):
        leg["planDistance"] = 1 + k % 7
        leg["duration"] = 30 + k % 11
    return Problem.from_request(Request.model_validate(payload))


def is_dn(problem, node):
    return DEPOT < node <= len(problem.dns)


# 参照实现：直接照业务定义逐元素写，不依赖 numpy 的切片技巧。
def reference_distance(p, i, j):
    sink = len(p.dns) + 1
    return 0 if sink in (i, j) else p.distance[i][j]


def reference_time(p, i, j):
    sink = len(p.dns) + 1
    travel = 0 if sink in (i, j) else p.duration[i][j]
    if not is_dn(p, i):
        return travel
    same_site = is_dn(p, j) and p.dn(i).siteCode == p.dn(j).siteCode
    return travel if same_site else travel + p.unload


def reference_budget(p, i, j):
    if not is_dn(p, j):
        return 0
    if i == DEPOT:
        return p.distance[DEPOT][j]
    return -reference_distance(p, i, j)


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize(
    ("build", "reference"),
    [
        (lambda p: distance_matrix(p), reference_distance),
        (lambda p: time_matrix(p), reference_time),
        (lambda p: first_leg_budget(distance_matrix(p)), reference_budget),
    ],
    ids=["distance", "time", "first_leg_budget"],
)
def test_matches_elementwise_definition(build, reference, shared):
    p = problem(shared=shared)
    matrix = build(p)
    size = len(p.dns) + 2
    assert matrix.shape == (size, size)
    for i, j in itertools.product(range(size), repeat=2):
        assert matrix[i, j] == reference(p, i, j), (i, j)


# 同站 DN 之间不再计卸货，离开站点去往别处才计一次。
def test_unload_counted_once_per_site():
    p = problem()
    time = time_matrix(p)
    assert time[1, 2] == p.duration[1][2] == 0  # 同站：D0 → D1
    assert time[2, 3] == p.duration[2][3] + p.unload
    assert time[len(p.dns), len(p.dns) + 1] == p.unload  # 最后一站 → sink


# 向量化计算不应原地修改距离矩阵（预算矩阵由距离取负得到）。
def test_budget_does_not_mutate_distance():
    p = problem()
    distance = distance_matrix(p)
    before = distance.copy()
    first_leg_budget(distance)
    assert np.array_equal(distance, before)
