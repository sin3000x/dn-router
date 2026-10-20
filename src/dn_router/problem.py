"""Input normalization. All integer scaling is exact: unsupported precision is rejected."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from .models import Request, DN, Vehicle, group_key

DIST_SCALE = 1000
LOAD_SCALE = 1_000_000
LIMIT = 2**60


class InputError(ValueError):
    pass


def scaled(value, scale):
    result = Decimal(value) * scale
    if result != result.to_integral_value() or abs(result) >= LIMIT:
        raise InputError(f"unsupported numeric precision/range: {value} (scale={scale})")
    return int(result)


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

    def types_for(self, nodes):
        volume = sum(self.dns[i-1].totalGrossVolume for i in nodes)
        weight = sum(self.dns[i-1].totalGrossWeight for i in nodes)
        return [k for k, v in enumerate(self.vehicles)
                if volume <= v.totalVolume and weight <= v.totalWeight]

    def schedule(self, nodes):
        # Same-vehicle same-site DNs form one contiguous stop with a common window.
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
            physical = time + self.duration[previous][block[0]]
            start = max([physical] + [self.windows[i][0] for i in block])
            if any(start > self.windows[i][1] for i in block):
                raise InputError("route time window violation")
            visits.extend([(physical, start, start+self.service)]*len(block))
            time, previous = start+self.service, block[-1]
            offset = end
        return visits


def prepare(request: Request):
    edges, warnings = {}, []
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
    service = int((request.unloadingTime * 3600).to_integral_value(rounding=ROUND_CEILING))
    dns, rejected, bounds = [], [], []
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
    vehicles = sorted(request.vehicleTypeVoList, key=lambda v: (v.totalVolume, v.totalWeight, v.vehicleTypeName))
    ids = [origin] + [d.orderReleaseId for d in dns]
    size = len(ids)
    distance = [[0]*size for _ in ids]
    duration = [[0]*size for _ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j or j == 0:  # Open route: return legs are never needed.
                continue
            leg = edges.get((a, b))
            if leg is None or leg.duration is None:
                raise InputError(f"missing directed matrix leg/duration: {a} -> {b}")
            if i and dns[i-1].siteCode == dns[j-1].siteCode:
                if leg.planDistance or leg.duration:
                    warnings.append(f"SAME_SITE_TRAVEL_NORMALIZED: {a} -> {b}")
                continue
            distance[i][j] = scaled(leg.planDistance, DIST_SCALE)
            duration[i][j] = leg.duration
    latest = int((request.latestShippingDate-base).total_seconds()) if request.latestShippingDate else 0
    max_bound = max([max(0, b[1]) for b in bounds if b] + [0, latest])
    horizon = max_bound + size*(max(map(max, duration), default=0)+service)+1
    if horizon >= LIMIT:
        raise InputError("time range too large")
    windows = [(0, horizon)] + [(max(0, b[0]), b[1]) if b else (0, horizon) for b in bounds]
    for d in dns:
        scaled(d.totalGrossVolume, LOAD_SCALE)
        scaled(d.totalGrossWeight, LOAD_SCALE)
    for v in vehicles:
        scaled(v.totalVolume, LOAD_SCALE)
        scaled(v.totalWeight, LOAD_SCALE)
    return Problem(request, dns, vehicles, distance, duration, windows, horizon,
                   latest if request.latestShippingDate else horizon, service, rejected, warnings)


def validate_routes(p: Problem, routes):
    """Independent route audit, also applied to the fallback. Raises rather than shipping invalid plans."""
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
    if sorted(seen) != list(range(1, len(p.dns)+1)):
        raise InputError("mandatory DN omitted or duplicated")
