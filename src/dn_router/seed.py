"""Bounded feasible insertion seed. OR-Tools remains the optimization engine."""
from time import perf_counter
from .models import group_key
from .problem import DIST_SCALE, InputError


def build_seed(p, deadline):
    routes = []
    count_first = p.request.quotationModel in ('city','zipCode','province')
    def length(nodes):
        return sum(p.distance[a][b] for a,b in zip([0]+nodes,nodes))
    def feasible(nodes):
        kinds = p.types_for(nodes)
        if not kinds:
            return None
        if p.request.maxSiteQty and len({group_key(p.dns[i-1],p.request.lonAndLatCombination) for i in nodes}) > p.request.maxSiteQty:
            return None
        between = [p.distance[a][b] for a,b in zip(nodes,nodes[1:])]
        if p.request.siteDistanceLimit > 0 and any(d > p.request.siteDistanceLimit*DIST_SCALE for d in between):
            return None
        if p.request.distanceLessThanFirstLeg and sum(between) > p.distance[0][nodes[0]]:
            return None
        try:
            p.schedule(nodes)
        except InputError:
            return None
        return kinds[0]
    for node in range(1,len(p.dns)+1):
        single_kind = p.types_for([node])[0]
        best = None
        best_delta = (0,0,0)
        for r,(nodes,kind) in enumerate(routes):
            if perf_counter() >= deadline:
                break
            for pos in range(len(nodes)+1):
                candidate = nodes[:pos]+[node]+nodes[pos:]
                chosen = feasible(candidate)
                if chosen is None:
                    continue
                delta_distance = length(candidate)-length(nodes)-length([node])
                delta_rank = chosen-kind-single_kind-1
                delta = (-1,delta_distance,delta_rank) if count_first else (delta_distance,delta_rank,0)
                if delta < best_delta:
                    best_delta, best = delta,(r,candidate,chosen)
        if best:
            r,nodes,kind=best
            routes[r]=(nodes,kind)
        else:
            routes.append(([node],single_kind))
    return routes
