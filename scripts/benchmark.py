"""Reproducible synthetic smoke benchmark; not a production SLA benchmark."""
import argparse
import json
import math
import random
import statistics
from dn_router import solve


def generate(n, seed):
    rng = random.Random(seed)
    points = [(0,0)] + [(rng.uniform(-50,50),rng.uniform(-50,50)) for _ in range(n)]
    ids = ['DEPOT'] + [f'DN{i}' for i in range(n)]
    matrix = []
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i != j and j:
                distance = round(math.dist(points[i],points[j]),3)
                matrix.append({'from':a,'to':b,'planDistance':distance,'duration':math.ceil(distance*60)})
    return {'sourceLocation':'DEPOT','sourceLatitude':0,'sourceLongitude':0,
            'earliestShippingDate':'2026-10-21 08:00', 'unloadingTime':0.25,
            'maxSiteQty':5,'quotationModel':'city','lonAndLatCombination':0,
            'distanceLessThanFirstLeg':True, 'siteDistanceLimit':40,
            'allPathDistanceVoList':matrix,
            'dnSiteVoList':[{'orderReleaseId':ids[i+1], 'siteCode':f'S{i}',
                'deliveryLatitude':points[i+1][0], 'deliveryLongitude':points[i+1][1],
                'totalGrossVolume':1, 'totalGrossWeight':100,
                'timeWindow':['2026-10-21 10:00','2026-10-21 18:00']} for i in range(n)],
            'vehicleTypeVoList':[{'vehicleTypeName':f'T{i}','totalVolume':v,'totalWeight':v*500}
                                for i,v in enumerate([2,4,18,27,39,65])]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runs',type=int,default=5)
    parser.add_argument('--dn',type=int,default=100)
    parser.add_argument('--time-limit-ms',type=int,default=200)
    args=parser.parse_args()
    records=[]
    for seed in range(args.runs):
        result=solve(generate(args.dn,seed),time_limit_ms=args.time_limit_ms)
        assert result['status']=='FEASIBLE' and result['servedOrderCount']==args.dn, result
        records.append({key:result[key] for key in ('elapsedMs','vehicleCount','totalDistance','solutionSource','timingsMs')})
    print(json.dumps({'dn':args.dn,'runs':args.runs,'budgetMs':args.time_limit_ms,
                      'medianMs':statistics.median(x['elapsedMs'] for x in records),
                      'maxMs':max(x['elapsedMs'] for x in records),'records':records},indent=2))


if __name__=='__main__':
    main()
