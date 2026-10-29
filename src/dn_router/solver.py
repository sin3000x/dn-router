"""用 OR-Tools 固定车型容量模型求解开放路线，保留完整可行兜底方案。

求解流程：校验输入 → 逐单直送兜底 → 插入法初解 → 路线消除（仅车辆数优先）
→ OR-Tools 局部搜索 → 独立审计。
任一阶段预算不足时都会保留上一阶段的可行方案。
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import timedelta
from time import perf_counter
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict, Unpack

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from pydantic import ValidationError

from .initial import build_initial_routes
from .models import GroupKey, Request
from .problem import (
    DEPOT,
    DIST_SCALE,
    LIMIT,
    LOAD_SCALE,
    InputError,
    Problem,
    Route,
    gc_paused,
    scaled,
    validate_routes,
)
from .reduce import reduce_routes

Status = Literal[
    "FEASIBLE",
    "NO_ELIGIBLE_ORDERS",
    "INVALID_INPUT",
    "INFEASIBLE_INPUT",
    "SOLVER_ERROR",
]


class SiteDetail(TypedDict):
    siteAddress: str
    eta: str


class RouteOutput(TypedDict):
    """公开路线对象；旧版不考虑时间的请求不含 departure_date 和 siteDetails。"""

    dist: float
    siteAddresses: list[str]
    departure_date: NotRequired[str]
    siteDetails: NotRequired[list[SiteDetail]]
    vehicle: str
    volume: float
    weight: float


class ErrorItem(TypedDict):
    code: str
    message: NotRequired[str]
    orderReleaseId: NotRequired[str]


class Timings(TypedDict):
    buildAndSeed: float
    search: float


class _Context(TypedDict):
    """输入校验通过后才有的字段。"""

    taskNo: str
    rejectedOrders: list[dict[str, str]]
    warnings: list[str]


class _Fields(TypedDict):
    """除 elapsedMs 外的结果字段；统计字段仅在 FEASIBLE/NO_ELIGIBLE_ORDERS 时出现。"""

    taskNo: NotRequired[str]
    rejectedOrders: NotRequired[list[dict[str, str]]]
    warnings: NotRequired[list[str]]
    status: Status
    solutionSource: NotRequired[
        Literal["direct_fallback", "insertion_fallback", "ortools"]
    ]
    routes: list[RouteOutput]
    errors: NotRequired[list[ErrorItem]]
    vehicleCount: NotRequired[int]
    servedOrderCount: NotRequired[int]
    totalDistance: NotRequired[float]
    objectivePolicy: NotRequired[Literal["vehicles_distance_type", "distance_type"]]
    optimalityProven: NotRequired[bool]
    solverStatus: NotRequired[int | None]
    timingsMs: NotRequired[Timings]


class Result(_Fields):
    """solve() 的公开结果对象，字段说明见 docs/api.md。"""

    elapsedMs: float


class _Schedule(TypedDict, total=False):
    departure_date: str
    siteDetails: list[SiteDetail]


class Stage(NamedTuple):
    """初解阶段：在总预算的 share 比例内改进路线。

    Attributes:
        run: 阶段函数 (problem, 当前可行路线, deadline) -> 新的可行路线；
            deadline 是 perf_counter() 的绝对时刻（秒）。
        share: 本阶段可用的总预算比例。
    """

    run: Callable[[Problem, list[Route], float], list[Route]]
    share: float


def _seed_stages(request: Request) -> list[Stage]:
    """按顺序执行的初解阶段；新增阶段只需在此挂接。

    各阶段 share 都按总预算计算，剩余时间全部留给 OR-Tools 局部搜索。
    """
    # 插入法初解：总预算 20%。插入只扫一遍，多给时间只是让它跑完；
    # 被截断的 DN 改为直送，车辆数后续几乎减不回来，所以不设绝对上限。
    insertion = Stage(
        run=lambda problem, _, end: build_initial_routes(problem=problem, deadline=end),
        share=0.2,
    )
    stages = [insertion]
    if request.count_first:
        # 路线消除：总预算 25%，不设上限；仅车辆数优先模式启用。
        stages.append(Stage(run=reduce_routes, share=0.25))
    return stages


def solve(payload: dict[str, Any], *, time_limit_ms: int = 200) -> Result:
    """求解一批配送请求，返回公开结果对象。

    预算覆盖整体计算，尽力遵守；原生建模调用不能保证在任意时点中断。

    Args:
        payload: 原始请求 JSON 值，字段见 schemas/request.schema.json。
        time_limit_ms: 总时间预算，单位毫秒，须为正整数。

    Returns:
        结果字典，始终包含 status、routes 和 elapsedMs。status 为 FEASIBLE 或
        NO_ELIGIBLE_ORDERS 时附带路线统计、solverStatus 等字段；为 INVALID_INPUT、
        INFEASIBLE_INPUT 或 SOLVER_ERROR 时 routes 为空并附带 errors。
        完整字段见 docs/api.md。

    Raises:
        ValueError: time_limit_ms 不是正整数。请求本身不合法时不抛错，
            而是返回 status 为 INVALID_INPUT 的结果。
    """
    started = perf_counter()
    is_int = isinstance(time_limit_ms, int) and not isinstance(time_limit_ms, bool)
    if not is_int or time_limit_ms < 1:
        raise ValueError("time_limit_ms must be a positive integer")
    budget = time_limit_ms / 1000
    deadline = started + budget

    def finish(**fields: Unpack[_Fields]) -> Result:
        return {**fields, "elapsedMs": _ms(perf_counter() - started)}

    try:
        with gc_paused():
            request = Request.model_validate(payload)
            problem = Problem.from_request(request)
    except (ValidationError, InputError) as error:
        errors: list[ErrorItem] = [{"code": "INVALID_INPUT", "message": str(error)}]
        return finish(status="INVALID_INPUT", routes=[], errors=errors)

    base: _Context = {
        "taskNo": request.taskNo,
        "rejectedOrders": problem.rejected,
        "warnings": problem.warnings,
    }
    nodes = range(1, len(problem.dns) + 1)

    # 任一有效 DN 超出全部车型容量时整批不可行，不能通过丢弃该单生成路线。
    oversized: list[ErrorItem] = [
        {
            "code": "DN_EXCEEDS_ALL_VEHICLES",
            "orderReleaseId": problem.dn(i).orderReleaseId,
        }
        for i in nodes
        if not problem.fitting_types([i])
    ]
    if oversized:
        return finish(**base, status="INFEASIBLE_INPUT", routes=[], errors=oversized)

    # 逐单直送保证完整覆盖，之后每一步都只在得到可行解时替换。
    routes = [
        Route(nodes=[i], vehicle_type=problem.fitting_types([i])[0]) for i in nodes
    ]
    source, solver_status, build_ms, search_ms = "direct_fallback", None, 0.0, 0.0

    if nodes and perf_counter() < deadline:
        for stage in _seed_stages(request):
            end = perf_counter() + budget * stage.share
            routes = stage.run(problem, routes, min(deadline, end))
        source = "insertion_fallback"

    if nodes:
        weights = _ObjectiveWeights(problem)
        if weights.overflows:
            message = "distance magnitude too large"
            errors = [{"code": "OBJECTIVE_OVERFLOW", "message": message}]
            return finish(**base, status="INVALID_INPUT", routes=[], errors=errors)

        build_started = perf_counter()
        # 每车型槽位数取初解路线数：总槽位仍是初解车数的 K 倍，足够局部搜索拆分路线；
        # 距离优先若按 N 预留，100 单 6 车型的建模就会耗掉约 0.8 秒预算。
        model = _RoutingModel(
            problem=problem, weights=weights, slots_per_type=len(routes)
        )
        solution, solver_status, search_ms = model.search(
            initial=routes, deadline=deadline
        )
        build_ms = (perf_counter() - build_started) * 1000 - search_ms
        if solution:
            routes, source = model.routes(solution), "ortools"

    # 输出前统一独立审计，不依赖求解器约束是否被正确编码。
    # 输入已通过校验，审计失败说明求解流程有缺陷，不能报告为输入错误。
    try:
        validate_routes(problem=problem, routes=routes)
    except InputError as error:
        errors = [{"code": "ROUTE_AUDIT_FAILED", "message": str(error)}]
        return finish(**base, status="SOLVER_ERROR", routes=[], errors=errors)
    output = [_route_output(problem=problem, route=route) for route in routes]
    total_distance = sum(problem.length(r.nodes) for r in routes) / DIST_SCALE
    policy = "vehicles_distance_type" if request.count_first else "distance_type"
    # 有限时间搜索只能声明可行，不能据此声称全局最优。
    return finish(
        **base,
        status="FEASIBLE" if nodes else "NO_ELIGIBLE_ORDERS",
        solutionSource=source,
        routes=output,
        vehicleCount=len(output),
        servedOrderCount=len(nodes),
        totalDistance=total_distance,
        objectivePolicy=policy,
        optimalityProven=False,
        solverStatus=solver_status,
        timingsMs={"buildAndSeed": round(build_ms, 3), "search": round(search_ms, 3)},
    )


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


def _remaining_ms(deadline: float) -> float:
    return (deadline - perf_counter()) * 1000


def _route_output(problem: Problem, route: Route) -> RouteOutput:
    """生成公开路线对象。"""
    dns = [problem.dn(i) for i in route.nodes]
    return {
        "dist": problem.length(route.nodes) / DIST_SCALE,
        "siteAddresses": [d.orderReleaseId for d in dns],
        **_schedule_output(problem=problem, nodes=route.nodes),
        "vehicle": problem.vehicle_types[route.vehicle_type].vehicleTypeName,
        "volume": float(sum(d.totalGrossVolume for d in dns)),
        "weight": float(sum(d.totalGrossWeight for d in dns)),
    }


def _schedule_output(problem: Problem, nodes: list[int]) -> _Schedule:
    """出仓时间和逐单 eta，eta 取最早出仓时间表中的开始卸货时间。

    旧版不考虑时间的请求返回空字典。
    """
    departure = problem.request.earliestShippingDate
    if departure is None:
        return {}

    def timestamp(seconds: int) -> str:
        return (departure + timedelta(seconds=seconds)).isoformat(sep=" ")

    visits = problem.schedule(nodes)
    return {
        "departure_date": timestamp(0),
        "siteDetails": [
            {"siteAddress": problem.dn(i).orderReleaseId, "eta": timestamp(v.start)}
            for i, v in zip(nodes, visits, strict=True)
        ],
    }


class _ObjectiveWeights:
    """用全局上界构造严格词典序权重：高优先级的一单位超过所有低优先级成本。

    目标从高到低：车辆数（仅车辆数优先模式）、总距离、车型等级之和。
    """

    def __init__(self, problem: Problem) -> None:
        n, k = len(problem.dns), len(problem.vehicle_types)
        self.max_distance = n * max(map(max, problem.distance))
        max_rank = n * k
        self.distance_weight = max_rank + 1
        max_cost = self.max_distance * self.distance_weight + max_rank
        self.per_vehicle = max_cost + 1 if problem.request.count_first else 0
        self.overflows = n * self.per_vehicle + max_cost >= LIMIT


class _RoutingModel:
    """OR-Tools 路由模型。

    每种车型预留 slots_per_type 个车辆槽位，槽位按车型分块排列。
    节点 N+1 是虚拟终点，进出终点的距离和耗时为零，实现开放路线。
    """

    def __init__(
        self, problem: Problem, weights: _ObjectiveWeights, slots_per_type: int
    ) -> None:
        self.problem = problem
        self.slots_per_type = slots_per_type
        self.slot_types = [
            vehicle_type
            for vehicle_type in range(len(problem.vehicle_types))
            for _ in range(slots_per_type)
        ]
        slots = len(self.slot_types)
        self.sink = len(problem.dns) + 1
        self.manager = pywrapcp.RoutingIndexManager(
            self.sink + 1, slots, [DEPOT] * slots, [self.sink] * slots
        )
        self.routing = pywrapcp.RoutingModel(self.manager)
        self.solver = self.routing.solver()
        # 求解器回调收到的是内部索引，统一先映射到节点编号。
        index_count = self.manager.GetNumberOfIndices()
        self.node_of = [self.manager.IndexToNode(i) for i in range(index_count)]

        self._add_cost(weights)
        self._add_capacities()
        # 旧版请求不考虑时间，不建时间维度。
        if problem.request.timed:
            self._add_time()
        self._add_shared_site_blocks()
        self._add_stop_limit()
        self._add_distance_limits()

    # ---- 求解与读取 ----

    def search(
        self, initial: Iterable[Route], deadline: float
    ) -> tuple[Any, int | None, float]:
        """从可行初解启动引导局部搜索，每个耗时阶段结束后重算剩余预算。

        Args:
            initial: 可行初解，每条路线的车型下标决定放入哪一块槽位。
            deadline: 截止时刻，perf_counter() 的绝对值（秒），不是剩余时长。

        Returns:
            (解或 None, 求解器状态或 None, 搜索耗时 ms)。预算不足或初解无法
            载入时解为 None。
        """
        if _remaining_ms(deadline) <= 0:
            return None, None, 0.0
        params = _search_parameters(_remaining_ms(deadline))
        self.routing.CloseModelWithParameters(params)
        assignment = self.routing.ReadAssignmentFromRoutes(
            self._slot_routes(initial), True
        )
        remaining = _remaining_ms(deadline)
        if not assignment or remaining <= 0:
            return None, self.routing.status(), 0.0

        params.time_limit.FromMilliseconds(max(1, int(remaining)))
        started = perf_counter()
        solution = self.routing.SolveFromAssignmentWithParameters(assignment, params)
        search_ms = (perf_counter() - started) * 1000
        return solution, self.routing.status(), search_ms

    def routes(self, solution: Any) -> list[Route]:
        """读取非空路线，并为每条路线选最小可行车型。"""
        routes: list[Route] = []
        for slot in range(len(self.slot_types)):
            index, nodes = self.routing.Start(slot), []
            while not self.routing.IsEnd(index):
                if node := self.node_of[index]:
                    nodes.append(node)
                index = solution.Value(self.routing.NextVar(index))
            if nodes:
                routes.append(
                    Route(
                        nodes=nodes, vehicle_type=self.problem.fitting_types(nodes)[0]
                    )
                )
        return routes

    def _slot_routes(self, routes: Iterable[Route]) -> list[list[int]]:
        """把业务路线放进对应车型的槽位，未用槽位保留为空。"""
        slots: list[list[int]] = [[] for _ in self.slot_types]
        used = [0] * len(self.problem.vehicle_types)
        for nodes, vehicle_type in routes:
            slots[vehicle_type * self.slots_per_type + used[vehicle_type]] = nodes
            used[vehicle_type] += 1
        return slots

    # ---- 节点工具 ----

    def _index(self, node: int) -> int:
        return self.manager.NodeToIndex(node)

    def _is_dn(self, node: int) -> bool:
        return DEPOT < node < self.sink

    def _slot_of(self, node: int) -> Any:
        return self.routing.VehicleVar(self._index(node))

    def _on_slot(self, node: int, slot: int) -> Any:
        """布尔变量：node 是否由车辆槽位 slot 配送。"""
        return self.solver.IsEqualCstVar(self._slot_of(node), slot)

    def _register_arcs(self, arc: Callable[[int, int], int]) -> int:
        """把按节点编号定义的弧成本预先算成矩阵，再注册为求解器回调。"""
        nodes = range(self.sink + 1)
        matrix = [[arc(i, j) for j in nodes] for i in nodes]
        node_of = self.node_of
        return self.routing.RegisterTransitCallback(
            lambda a, b: matrix[node_of[a]][node_of[b]]
        )

    def _register_nodes(self, value: Callable[[int], int]) -> int:
        values = [value(node) for node in self.node_of]
        return self.routing.RegisterUnaryTransitCallback(lambda a: values[a])

    def _distance(self, i: int, j: int) -> int:
        return 0 if self.sink in (i, j) else self.problem.distance[i][j]

    def _travel_and_unload(self, i: int, j: int) -> int:
        """从 i 出发到 j 的时间：离开实际站点时计一次卸货，同站 DN 之间为零。"""
        problem = self.problem
        travel = 0 if self.sink in (i, j) else problem.duration[i][j]
        if not self._is_dn(i):
            return travel
        same_site = self._is_dn(j) and problem.dn(i).siteCode == problem.dn(j).siteCode
        return travel if same_site else travel + problem.unload_seconds

    # ---- 约束 ----

    def _add_cost(self, weights: _ObjectiveWeights) -> None:
        routing = self.routing
        cost = self._register_arcs(
            lambda i, j: self._distance(i=i, j=j) * weights.distance_weight
        )
        routing.SetArcCostEvaluatorOfAllVehicles(cost)
        for slot, vehicle_type in enumerate(self.slot_types):
            routing.SetFixedCostOfVehicle(weights.per_vehicle + vehicle_type + 1, slot)

    def _add_capacities(self) -> None:
        """体积和重量各一个容量维度；每个槽位绑定其车型的两项容量。"""
        problem = self.problem
        for name, demand_field, capacity_field in (
            ("Volume", "totalGrossVolume", "totalVolume"),
            ("Weight", "totalGrossWeight", "totalWeight"),
        ):

            def demand(node: int, field: str = demand_field) -> int:
                if not self._is_dn(node):
                    return 0
                return scaled(value=getattr(problem.dn(node), field), scale=LOAD_SCALE)

            capacities = [
                scaled(
                    value=getattr(problem.vehicle_types[vehicle_type], capacity_field),
                    scale=LOAD_SCALE,
                )
                for vehicle_type in self.slot_types
            ]
            callback = self._register_nodes(demand)
            self.routing.AddDimensionWithVehicleCapacity(
                callback, 0, capacities, True, name
            )

    def _add_time(self) -> None:
        """累计值表示开始卸货时间；松弛量允许提前到达后等待。"""
        problem, routing = self.problem, self.routing
        transit = self._register_arcs(self._travel_and_unload)
        routing.AddDimension(transit, problem.horizon, problem.horizon, False, "Time")
        time = routing.GetDimensionOrDie("Time")
        for node in range(1, self.sink):
            time.CumulVar(self._index(node)).SetRange(*problem.windows[node])
        for slot in range(len(self.slot_types)):
            time.CumulVar(routing.Start(slot)).SetRange(0, problem.latest_departure)

    def _add_shared_site_blocks(self) -> None:
        """同车同站的 DN 必须连续停靠并共用 eta。

        用序位维度约束：同车同站任意两单的序位差不超过本车该站 DN 数减一，
        迫使它们组成连续块；考虑时间时同时令它们的开始卸货时间相等。
        """
        problem, routing, solver = self.problem, self.routing, self.solver
        sites: defaultdict[str, list[int]] = defaultdict(list)
        for node in range(1, self.sink):
            sites[problem.dn(node).siteCode].append(node)
        shared = [group for group in sites.values() if len(group) > 1]
        if not shared:
            return

        n = len(problem.dns)
        step = routing.RegisterTransitCallback(lambda a, b: 1)
        routing.AddDimension(step, 0, n + 1, True, "Position")
        position = routing.GetDimensionOrDie("Position")
        timed = problem.request.timed
        slots = range(len(self.slot_types))

        def order(node: int) -> Any:
            return position.CumulVar(self._index(node))

        def start(node: int) -> Any:
            return routing.GetDimensionOrDie("Time").CumulVar(self._index(node))

        for group in shared:
            on_slot = [
                solver.Sum([self._on_slot(node=i, slot=s) for i in group]).Var()
                for s in slots
            ]
            for i in group:
                same_site_count = solver.Element(on_slot, self._slot_of(i))
                for j in group:
                    if i == j:
                        continue
                    same = solver.IsEqualVar(self._slot_of(i), self._slot_of(j))
                    solver.Add(
                        order(i) - order(j) <= same_site_count - 1 + n * (1 - same)
                    )
                    if timed:
                        solver.Add(start(i) - start(j) <= problem.horizon * (1 - same))

    def _add_stop_limit(self) -> None:
        """每车去重站点数不超过 maxSiteQty。

        每组只有一个 DN 时用计数维度；否则按车辆是否访问该组计数。
        """
        problem, routing, solver = self.problem, self.routing, self.solver
        request = problem.request
        groups: defaultdict[GroupKey, list[int]] = defaultdict(list)
        for node in range(1, self.sink):
            key = problem.dn(node).group_key(request.lonAndLatCombination)
            groups[key].append(node)
        if not request.maxSiteQty or request.maxSiteQty >= len(groups):
            return

        if all(len(group) == 1 for group in groups.values()):
            count = self._register_nodes(lambda node: int(self._is_dn(node)))
            routing.AddDimension(count, 0, request.maxSiteQty, True, "StopCount")
            return

        for slot in range(len(self.slot_types)):
            visited = [
                solver.Max([self._on_slot(node=i, slot=slot) for i in group])
                for group in groups.values()
            ]
            solver.Add(solver.Sum(visited) <= request.maxSiteQty)

    def _add_distance_limits(self) -> None:
        problem, routing = self.problem, self.routing
        request = problem.request

        # 站间距离超限的有向边直接禁用；仓库到首站不受此限制。
        limit = request.siteDistanceLimit * DIST_SCALE
        if limit > 0:
            # 每个节点一次性移除全部禁用后继，避免逐条跨越 Python/C++ 边界。
            index = [self._index(node) for node in range(self.sink)]
            for i in range(1, self.sink):
                row = problem.distance[i]
                banned = [
                    index[j] for j in range(1, self.sink) if i != j and row[j] > limit
                ]
                if banned:
                    routing.NextVar(index[i]).RemoveValues(banned)

        # “站间和 ≤ 首段”写成预算维度：首段存入预算，每段站间距离扣减，余额不得为负。
        # 维度约束能被局部搜索过滤器看到；改用 solver.Add 时，路线插入类算子
        # 看不到该约束，产生的候选几乎都在最终 CP 检查中被否决，无法减车。
        if request.distanceLessThanFirstLeg:

            def budget(i: int, j: int) -> int:
                if not self._is_dn(j):
                    return 0
                if i == DEPOT:
                    return problem.distance[DEPOT][j]
                return -self._distance(i=i, j=j)

            longest_first_leg = max(problem.distance[DEPOT])
            routing.AddDimension(
                self._register_arcs(budget), 0, longest_first_leg, True, "FirstLeg"
            )


def _search_parameters(time_limit_ms: float) -> Any:
    params = pywrapcp.DefaultRoutingSearchParameters()
    strategy = routing_enums_pb2.FirstSolutionStrategy
    metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic
    params.first_solution_strategy = strategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = metaheuristic.GUIDED_LOCAL_SEARCH
    params.use_full_propagation = True
    params.time_limit.FromMilliseconds(max(1, int(time_limit_ms)))
    return params
