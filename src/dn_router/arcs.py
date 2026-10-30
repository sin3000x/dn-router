"""求解器回调用的弧矩阵：整体向量化计算，避免逐元素调用 Python 函数。

三个矩阵都按节点号索引，边长为 N + 2：0 是仓库，1..N 是 DN，N + 1 是虚拟终点 sink。
进出 sink 的距离为零，所以开放路线的最后一个 DN 之后不需要返仓。
"""

import numpy as np

from .problem import DEPOT, Matrix, Problem


def distance_matrix(problem: Problem) -> np.ndarray:
    """节点间距离（已缩放为整数）；sink 一行一列全为零。"""
    return _pad(matrix=problem.distance, size=len(problem.dns) + 2)


def time_matrix(problem: Problem) -> np.ndarray:
    """从 i 出发到 j 的时间：行驶，加上离开 DN 站点时的一次卸货。

    同站 DN 之间不计卸货，所以连续访问同一站点只卸一次。
    """
    size = len(problem.dns) + 2
    is_dn = _is_dn(size)
    # 站点 → 编号：同站的 DN 编号相同即可，具体数值无意义，只用于判断同站。
    site_of = {dn.siteCode: k for k, dn in enumerate(problem.dns)}
    site = np.full(size, -1, dtype=np.int64)  # 仓库和 sink 的 -1 不与任何站点相等
    site[1:-1] = [site_of[dn.siteCode] for dn in problem.dns]
    same_site = is_dn[:, None] & is_dn[None, :] & (site[:, None] == site[None, :])
    leaves_site = is_dn[:, None] & ~same_site
    return _pad(matrix=problem.duration, size=size) + problem.unload * leaves_site


def first_leg_budget(distance: np.ndarray) -> np.ndarray:
    """首段预算：仓库到首站存入首段距离，之后每个站间段扣减，余额不得为负。

    去向不是 DN（仓库、sink）的弧转移量为零。
    """
    budget = -distance
    budget[DEPOT] = distance[DEPOT]
    budget[:, DEPOT] = 0
    budget[:, -1] = 0
    return budget


def _pad(matrix: Matrix, size: int) -> np.ndarray:
    """把 (N+1) 阶矩阵补成 (N+2) 阶，新增的 sink 行列为零。"""
    padded = np.zeros((size, size), dtype=np.int64)
    padded[: size - 1, : size - 1] = matrix
    return padded


def _is_dn(size: int) -> np.ndarray:
    nodes = np.arange(size)
    return (nodes > DEPOT) & (nodes < size - 1)
