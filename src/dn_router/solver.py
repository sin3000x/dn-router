"""用 OR-Tools 固定车型容量模型求解开放路线，保留完整可行兜底方案。"""
from collections import defaultdict
from datetime import timedelta
from time import perf_counter
from pydantic import ValidationError
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from .models import Request, group_key
from .seed import build_seed
from .problem import prepare, validate_routes, scaled, InputError, DIST_SCALE, LOAD_SCALE, LIMIT


def solve(payload: dict, *, time_limit_ms: int = 200) -> dict:
    """预算覆盖整体计算，尽力遵守；原生建模调用不能保证在任意时点中断。"""
    started = perf_counter()
    if isinstance(time_limit_ms, bool) or not isinstance(time_limit_ms, int) or time_limit_ms < 1:
        raise ValueError("time_limit_ms must be a positive integer")
    try:
        request = Request.model_validate(payload)
        p = prepare(request)
    except (ValidationError, InputError) as error:
        return {"status": "INVALID_INPUT", "routes": [], "errors": [{"code": "INVALID_INPUT", "message": str(error)}],
                "elapsedMs": round((perf_counter()-started)*1000, 3)}
    n, k = len(p.dns), len(p.vehicles)
    base_result = {"taskNo": request.taskNo, "rejectedOrders": p.rejected, "warnings": p.warnings}
    # 任一有效 DN 超出全部车型容量时整批不可行，不能通过丢弃该单生成路线。
    capacity_errors = [{"code": "DN_EXCEEDS_ALL_VEHICLES", "orderReleaseId": d.orderReleaseId}
                       for i, d in enumerate(p.dns, 1) if not p.types_for([i])]
    if capacity_errors:
        return {**base_result, "status": "INFEASIBLE_INPUT", "routes": [], "errors": capacity_errors,
                "elapsedMs": round((perf_counter()-started)*1000, 3)}
    # 逐单直送保证完整覆盖；插入初解和原生搜索均可在预算不足时回退。
    fallback = [([i], p.types_for([i])[0]) for i in range(1, n+1)]
    routes, solver_status, source = fallback, None, "direct_fallback"
    if n and (perf_counter()-started)*1000 < time_limit_ms:
        routes = build_seed(p, min(started+time_limit_ms/1000, perf_counter()+min(0.04,time_limit_ms/5000)))
        source = "insertion_fallback"
    # 车辆槽位是模型表示：距离优先每车型最多 N 辆；车辆数优先以上界初解车数为限。
    slots_per_type = len(routes) if fixed_mode(request) else n
    vehicle_kinds = [kind for kind in range(k) for _ in range(slots_per_type)]
    m = len(vehicle_kinds)
    # 把业务路线分配给对应车型槽位，未用槽位保留为空，供原生求解器导入。
    seed_routes = [[] for _ in range(m)]
    used_per_type = [0]*k
    for nodes, kind in routes:
        seed_routes[kind*slots_per_type+used_per_type[kind]] = nodes
        used_per_type[kind] += 1
    build_ms = search_ms = 0.0
    if n:
        build_started = perf_counter()
        # 节点 0 是仓库，N+1 是虚拟终点；到终点距离为零，实现不返仓。
        manager = pywrapcp.RoutingIndexManager(n+2, m, [0]*m, [n+1]*m)
        routing = pywrapcp.RoutingModel(manager)
        cp = routing.solver()
        sink = n+1
        # 回调接收求解器索引，必须先映射到业务节点编号。
        def distance(a, b):
            i, j = manager.IndexToNode(a), manager.IndexToNode(b)
            return 0 if j == sink or i == sink else p.distance[i][j]
        distance_cb = routing.RegisterTransitCallback(distance)
        # 用全局上界构造严格词典序权重：高优先级的一单位超过所有低优先级成本。
        bound_distance = n * max(map(max, p.distance))
        rank_bound = n*k
        distance_multiplier = rank_bound+1
        fixed = (bound_distance*distance_multiplier + rank_bound + 1) if request.quotationModel in ("city", "zipCode", "province") else 0
        if n*fixed + bound_distance*distance_multiplier + rank_bound >= LIMIT:
            return {**base_result, "status": "INVALID_INPUT", "routes": [],
                    "errors": [{"code": "OBJECTIVE_OVERFLOW", "message": "distance magnitude too large"}]}
        cost_cb = routing.RegisterTransitCallback(lambda a, b: distance(a, b)*distance_multiplier)
        routing.SetArcCostEvaluatorOfAllVehicles(cost_cb)
        for v in range(m):
            routing.SetFixedCostOfVehicle(fixed+vehicle_kinds[v]+1, v)
        routing.AddDimension(distance_cb, 0, bound_distance, True, "Distance")
        dist_dim = routing.GetDimensionOrDie("Distance")
        # 体积和重量分别建容量维度，但每个槽位绑定同一种车型的两项容量。
        for name, dn_field, vehicle_field in (("Volume", "totalGrossVolume", "totalVolume"), ("Weight", "totalGrossWeight", "totalWeight")):
            demand = [0] + [scaled(getattr(d, dn_field), LOAD_SCALE) for d in p.dns] + [0]
            # 通过默认参数固定本轮 demand，避免循环闭包读取下一轮的载荷列表。
            cb = routing.RegisterUnaryTransitCallback(lambda a, values=demand: values[manager.IndexToNode(a)])
            capacities = [scaled(getattr(p.vehicles[kind], vehicle_field), LOAD_SCALE) for kind in vehicle_kinds]
            routing.AddDimensionWithVehicleCapacity(cb, 0, capacities, True, name)
        # 实际卸货按 siteCode 分组，独立于业务的站点计数口径。
        unload_groups = defaultdict(list)
        for i, dn in enumerate(p.dns, 1):
            unload_groups[dn.siteCode].append(manager.NodeToIndex(i))
        shared_sites = any(len(g) > 1 for g in unload_groups.values())
        # 离开实际站点时计一次卸货；同站 DN 内部不加卸货或行驶时间。
        def transit(a, b):
            i, j = manager.IndexToNode(a), manager.IndexToNode(b)
            same_site = 1 <= i <= n and 1 <= j <= n and p.dns[i-1].siteCode == p.dns[j-1].siteCode
            service = p.service if 1 <= i <= n and not same_site else 0
            return service + (0 if j == sink or i == sink else p.duration[i][j])
        time_cb = routing.RegisterTransitCallback(transit)
        # 时间松弛量允许提前到达后等待，累计值表示开始卸货时间。
        routing.AddDimension(time_cb, p.horizon, p.horizon, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        for i in range(1, n+1):
            time_dim.CumulVar(manager.NodeToIndex(i)).SetRange(*p.windows[i])
        for v in range(m):
            time_dim.CumulVar(routing.Start(v)).SetRange(0, p.latest_start)
        # 同车同站 DN 的序位跨度约束保证连续停靠，时间约束保证共用 eta。
        if shared_sites:
            order_cb = routing.RegisterTransitCallback(lambda a, b: 1)
            routing.AddDimension(order_cb, 0, n+1, True, "Position")
            position = routing.GetDimensionOrDie("Position")
            for group in unload_groups.values():
                if len(group) < 2:
                    continue
                counts = [cp.Sum([cp.IsEqualCstVar(routing.VehicleVar(i), v) for i in group]).Var() for v in range(m)]
                for i in group:
                    count_on_vehicle = cp.Element(counts, routing.VehicleVar(i))
                    for j in group:
                        if i == j:
                            continue
                        same = cp.IsEqualVar(routing.VehicleVar(i), routing.VehicleVar(j))
                        # 同车同站序位跨度不超过本车该站 DN 数减一，迫使它们组成连续块。
                        cp.Add(position.CumulVar(i)-position.CumulVar(j) <= count_on_vehicle-1+n*(1-same))
                        cp.Add(time_dim.CumulVar(i)-time_dim.CumulVar(j) <= p.horizon*(1-same))
        # 站点计数按请求口径去重：单元素组使用计数维度，多元素组按车辆是否访问计数。
        groups = defaultdict(list)
        for i, dn in enumerate(p.dns, 1):
            groups[group_key(dn, request.lonAndLatCombination)].append(manager.NodeToIndex(i))
        if request.maxSiteQty and request.maxSiteQty < len(groups):
            if all(len(g) == 1 for g in groups.values()):
                count_cb = routing.RegisterUnaryTransitCallback(lambda a: int(1 <= manager.IndexToNode(a) <= n))
                routing.AddDimension(count_cb, 0, request.maxSiteQty, True, "StopCount")
            else:
                for v in range(m):
                    present = [cp.Max([cp.IsEqualCstVar(routing.VehicleVar(i), v) for i in g]) for g in groups.values()]
                    cp.Add(cp.Sum(present) <= request.maxSiteQty)
        # 站间距离超限的有向边直接禁用；仓库到首站不受此限制。
        if request.siteDistanceLimit > 0:
            limit = request.siteDistanceLimit * DIST_SCALE
            for i in range(1, n+1):
                for j in range(1, n+1):
                    if i != j and p.distance[i][j] > limit:
                        routing.NextVar(manager.NodeToIndex(i)).RemoveValue(manager.NodeToIndex(j))
        # 总距离含首段，因此“站间和 ≤ 首段”等价于“总距离 ≤ 两倍首段”。
        if request.distanceLessThanFirstLeg:
            first_dist = [0 if manager.IndexToNode(i) in (0, sink) else p.distance[0][manager.IndexToNode(i)]
                          for i in range(manager.GetNumberOfIndices())]
            for v in range(m):
                cp.Add(dist_dim.CumulVar(routing.End(v)) <= 2*cp.Element(first_dist, routing.NextVar(routing.Start(v))))
        # 从可行初解启动引导局部搜索，每个耗时阶段结束后重算剩余预算。
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.use_full_propagation = True
        remaining = time_limit_ms - (perf_counter()-started)*1000
        solution = None
        if remaining > 0:
            params.time_limit.FromMilliseconds(max(1, int(remaining)))
            routing.CloseModelWithParameters(params)
            seed = routing.ReadAssignmentFromRoutes(seed_routes + [[] for _ in range(m-len(seed_routes))], True)
            remaining = time_limit_ms - (perf_counter()-started)*1000
            build_ms = (perf_counter()-build_started)*1000
            if seed and remaining > 0:
                params.time_limit.FromMilliseconds(max(1, int(remaining)))
                search_started = perf_counter()
                solution = routing.SolveFromAssignmentWithParameters(seed, params)
                search_ms = (perf_counter()-search_started)*1000
            solver_status = routing.status()
        else:
            build_ms = (perf_counter()-build_started)*1000
        # 只在原生求解得到解时替换兜底路线，并为每条路线选最小可行车型。
        if solution:
            candidate = []
            for v in range(m):
                idx, nodes = routing.Start(v), []
                while not routing.IsEnd(idx):
                    node = manager.IndexToNode(idx)
                    if node:
                        nodes.append(node)
                    idx = solution.Value(routing.NextVar(idx))
                if nodes:
                    candidate.append((nodes, p.types_for(nodes)[0]))
            routes, source = candidate, "ortools"
    # 输出前统一独立审计，不依赖求解器约束是否被正确编码。
    validate_routes(p, routes)
    # 使用同一当地时间基准恢复整秒时间戳，不做时区换算。
    def timestamp(seconds):
        return (request.earliestShippingDate + timedelta(seconds=seconds)).isoformat(sep=" ")
    output = []
    for nodes, kind in routes:
        # 重新生成最早出仓的可行时间表；公开 eta 取开始卸货时间。
        visits = p.schedule(nodes)
        vehicle = p.vehicles[kind]
        distance_value = sum(p.distance[a][b] for a, b in zip([0]+nodes, nodes))
        output.append({
            "dist": distance_value/DIST_SCALE,
            "siteAddresses": [p.dns[i-1].orderReleaseId for i in nodes],
            "departure_date": timestamp(0),
            "siteDetails": [
                {"siteAddress": p.dns[node-1].orderReleaseId,
                 "eta": timestamp(visit[1])}
                for node, visit in zip(nodes, visits)
            ],
            "vehicle": vehicle.vehicleTypeName,
            "volume": float(sum(p.dns[i-1].totalGrossVolume for i in nodes)),
            "weight": float(sum(p.dns[i-1].totalGrossWeight for i in nodes)),
        })

    # 有限时间搜索只能声明可行，不能据此声称全局最优。
    return {**base_result, "status": "FEASIBLE" if n else "NO_ELIGIBLE_ORDERS", "solutionSource": source,
            "routes": output, "vehicleCount": len(output), "servedOrderCount": n,
            "totalDistance": sum(sum(p.distance[a][b] for a,b in zip([0]+nodes,nodes)) for nodes,_ in routes)/DIST_SCALE,
            "objectivePolicy": "vehicles_distance_type" if fixed_mode(request) else "distance_type",
            "optimalityProven": False, "solverStatus": solver_status,
            "timingsMs": {"buildAndSeed": round(build_ms, 3), "search": round(search_ms, 3)},
            "elapsedMs": round((perf_counter()-started)*1000, 3)}


# 这些报价模式以车辆数为第一目标，distance/null/空串以总距离为第一目标。
def fixed_mode(request):
    return request.quotationModel in ("city", "zipCode", "province")
