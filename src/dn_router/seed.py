"""在有限预算内构造可行插入初解，再交给 OR-Tools 优化。"""
from time import perf_counter
from .models import group_key
from .problem import DIST_SCALE, InputError


# 逐单尝试插入已有路线；预算耗尽后仍逐单直送，保证不漏单。
def build_seed(p, deadline):
    routes = []
    count_first = p.request.quotationModel in ('city','zipCode','province')
    # 开放路线长度只累计仓库到首站及站间路段，不计返仓。
    def length(nodes):
        return sum(p.distance[a][b] for a,b in zip([0]+nodes,nodes))
    # 候选插入同时检查车型、站点数、距离约束和完整时间表。
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
        # 车型已按等级排序，选择能够装下整条路线的最小等级。
        return kinds[0]
    for node in range(1,len(p.dns)+1):
        single_kind = p.types_for([node])[0]
        best = None
        # 以另开一辆直送车为比较基准，只接受目标严格改善的插入。
        best_delta = (0,0,0)
        for r,(nodes,kind) in enumerate(routes):
            if perf_counter() >= deadline:
                break
            for pos in range(len(nodes)+1):
                candidate = nodes[:pos]+[node]+nodes[pos:]
                chosen = feasible(candidate)
                if chosen is None:
                    continue
                # 合并后减去旧路线和新单直送的成本，按目标优先级比较增量元组。
                delta_distance = length(candidate)-length(nodes)-length([node])
                delta_rank = chosen-kind-single_kind-1
                delta = (-1,delta_distance,delta_rank) if count_first else (delta_distance,delta_rank,0)
                if delta < best_delta:
                    best_delta, best = delta,(r,candidate,chosen)
        if best:
            r,nodes,kind=best
            routes[r]=(nodes,kind)
        else:
            # 未找到改善插入或预算已耗尽时，为该单保留可行直送路线。
            routes.append(([node],single_kind))
    return routes
