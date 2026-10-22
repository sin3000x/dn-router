"""输入归一化与独立路线审计；整数缩放必须精确，超出精度直接报错。"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from .models import Request, DN, Vehicle, group_key

# 距离保留三位小数，载荷保留六位小数；范围上限为后续整数运算留余量。
DIST_SCALE = 1000
LOAD_SCALE = 1_000_000
LIMIT = 2**60


# 可向调用方报告的业务输入或路线约束错误。
class InputError(ValueError):
    pass


# 精确转成求解器整数单位，禁止静默舍入改变容量或距离约束。
def scaled(value, scale):
    result = Decimal(value) * scale
    if result != result.to_integral_value() or abs(result) >= LIMIT:
        raise InputError(f"unsupported numeric precision/range: {value} (scale={scale})")
    return int(result)


# 内部节点编号：0 为仓库，1..N 对应 dns 下标加一；时间为相对出发下限的秒数。
@dataclass
class Problem:
    request: Request
    dns: list[DN]
    vehicles: list[Vehicle]
    distance: list[list[int]]
    duration: list[list[int]]
    windows: list[tuple[int, int]]
    horizon: int
    latest_start: int
    service: int
    rejected: list[dict]
    warnings: list[str]

    # 返回同时满足体积和重量的车型索引，顺序沿用预处理后的车型等级。
    def types_for(self, nodes):
        volume = sum(self.dns[i-1].totalGrossVolume for i in nodes)
        weight = sum(self.dns[i-1].totalGrossWeight for i in nodes)
        return [k for k, v in enumerate(self.vehicles)
                if volume <= v.totalVolume and weight <= v.totalWeight]

    def schedule(self, nodes):
        # 同车同站的 DN 必须连续访问，共用开始卸货时间；离站后不能再次访问该站。
        time, previous, visits = 0, 0, []
        seen_sites = set()
        offset = 0
        while offset < len(nodes):
            site = self.dns[nodes[offset]-1].siteCode
            if site in seen_sites:
                raise InputError("same vehicle revisits a site")
            seen_sites.add(site)
            end = offset+1
            while end < len(nodes) and self.dns[nodes[end]-1].siteCode == site:
                end += 1
            block = nodes[offset:end]
            # 按最早出发顺推：先到达，再等待至该停靠全部 DN 的窗口交集。
            physical = time + self.duration[previous][block[0]]
            start = max([physical] + [self.windows[i][0] for i in block])
            if any(start > self.windows[i][1] for i in block):
                raise InputError("route time window violation")
            # 同一停靠的 DN 共用到达、开始和完成时间，整组只加一次卸货时长。
            visits.extend([(physical, start, start+self.service)]*len(block))
            time, previous = start+self.service, block[-1]
            offset = end
        return visits


# 建立权威矩阵、筛除允许剔除的时间窗异常，并构造有限时间范围。
def prepare(request: Request):
    edges, warnings = {}, []
    # 允许完全相同的重复边，距离或耗时冲突时拒绝请求。
    for leg in request.allPathDistanceVoList:
        key = (leg.origin, leg.to)
        if key in edges and (edges[key].planDistance, edges[key].duration) != (leg.planDistance, leg.duration):
            raise InputError(f"conflicting matrix entries: {key}")
        edges[key] = leg
        if leg.origin == leg.to and (leg.planDistance or leg.duration):
            warnings.append(f"DIAGONAL_NORMALIZED: {leg.origin}")
    if request.pathDistanceVoList:
        warnings.append("pathDistanceVoList is compatibility-only; allPathDistanceVoList is authoritative")
    origin = request.sourceLocation
    base = request.earliestShippingDate
    # 小时换算成整数秒后向上取整，避免低估卸货占用时间。
    service = int((request.unloadingTime * 3600).to_integral_value(rounding=ROUND_CEILING))
    dns, rejected, bounds = [], [], []
    # 仅允许剔除空时间窗、最早出仓直送仍超时的订单；矩阵缺失必须报错。
    for dn in request.dnSiteVoList:
        window = dn.timeWindow if request.enableTimeWindows else None
        if window == [] or (window and window[0] > window[1]):
            rejected.append({"orderReleaseId": dn.orderReleaseId, "code": "EMPTY_TIME_WINDOW"})
            continue
        edge = edges.get((origin, dn.orderReleaseId))
        if edge is None or edge.duration is None:
            raise InputError(f"missing depot leg/duration: {origin} -> {dn.orderReleaseId}")
        bound = None if window is None else tuple(int((x-base).total_seconds()) for x in window)
        if bound and edge.duration > bound[1]:
            rejected.append({"orderReleaseId": dn.orderReleaseId, "code": "DIRECT_TIME_WINDOW_UNREACHABLE"})
            continue
        dns.append(dn)
        bounds.append(bound)
    # 以容积、载重、名称确定稳定车型等级，不假定容量互相嵌套。
    vehicles = sorted(request.vehicleTypeVoList, key=lambda v: (v.totalVolume, v.totalWeight, v.vehicleTypeName))
    ids = [origin] + [d.orderReleaseId for d in dns]
    size = len(ids)
    distance = [[0]*size for _ in ids]
    duration = [[0]*size for _ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j or j == 0:  # 开放路线不返仓，因此无需返仓边。
                continue
            leg = edges.get((a, b))
            if leg is None or leg.duration is None:
                raise InputError(f"missing directed matrix leg/duration: {a} -> {b}")
            # 同 siteCode 视为一次物理停靠，内部距离及行驶耗时统一归零。
            if i and dns[i-1].siteCode == dns[j-1].siteCode:
                if leg.planDistance or leg.duration:
                    warnings.append(f"SAME_SITE_TRAVEL_NORMALIZED: {a} -> {b}")
                continue
            distance[i][j] = scaled(leg.planDistance, DIST_SCALE)
            duration[i][j] = leg.duration
    latest = int((request.latestShippingDate-base).total_seconds()) if request.latestShippingDate else 0
    # 时间上界覆盖最晚窗口、全部可能行驶及卸货，允许无时间窗订单等待。
    max_bound = max([max(0, b[1]) for b in bounds if b] + [0, latest])
    horizon = max_bound + size*(max(map(max, duration), default=0)+service)+1
    if horizon >= LIMIT:
        raise InputError("time range too large")
    windows = [(0, horizon)] + [(max(0, b[0]), b[1]) if b else (0, horizon) for b in bounds]
    # 预先检查载荷精度，避免仅在进入某个求解分支后才发现非法数值。
    for d in dns:
        scaled(d.totalGrossVolume, LOAD_SCALE)
        scaled(d.totalGrossWeight, LOAD_SCALE)
    for v in vehicles:
        scaled(v.totalVolume, LOAD_SCALE)
        scaled(v.totalWeight, LOAD_SCALE)
    return Problem(request, dns, vehicles, distance, duration, windows, horizon,
                   latest if request.latestShippingDate else horizon, service, rejected, warnings)


def validate_routes(p: Problem, routes):
    """独立审计所有输出，包括兜底方案；违规时抛错，禁止交付无效路线。"""
    seen = []
    for nodes, kind in routes:
        if not nodes:
            raise InputError("empty route in output")
        seen.extend(nodes)
        if kind not in p.types_for(nodes):
            raise InputError("capacity violation")
        if p.request.maxSiteQty and len({group_key(p.dns[i-1], p.request.lonAndLatCombination) for i in nodes}) > p.request.maxSiteQty:
            raise InputError("distinct stop limit violation")
        between = [p.distance[a][b] for a, b in zip(nodes, nodes[1:])]
        if p.request.siteDistanceLimit > 0 and any(Decimal(d)/DIST_SCALE > p.request.siteDistanceLimit for d in between):
            raise InputError("inter-stop distance violation")
        if p.request.distanceLessThanFirstLeg and sum(between) > p.distance[0][nodes[0]]:
            raise InputError("first-leg distance constraint violation")
        p.schedule(nodes)
    # 保留的 DN 必须恰好出现一次，同时检查漏单和重复配送。
    if sorted(seen) != list(range(1, len(p.dns)+1)):
        raise InputError("mandatory DN omitted or duplicated")
