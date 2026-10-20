import copy
import json
from pathlib import Path
import pytest
from dn_router import solve
from dn_router.models import Request, group_key
from dn_router.problem import prepare, validate_routes, InputError

ROOT = Path(__file__).resolve().parents[1]


def case(n=3):
    p = json.loads((ROOT/'examples/input-feasible.json').read_text())
    p['dnSiteVoList'] = [{**copy.deepcopy(p['dnSiteVoList'][0]), 'orderReleaseId': f'D{i}', 'siteCode': f'S{i}',
                         'timeWindow': None} for i in range(n)]
    p['vehicleTypeVoList'] = [{'totalVolume': 10, 'totalWeight': 10000, 'vehicleTypeName': 'large'},
                              {'totalVolume': 2, 'totalWeight': 1000, 'vehicleTypeName': 'small'}]
    p['pathDistanceVoList'] = []
    ids = [p['sourceLocation']] + [d['orderReleaseId'] for d in p['dnSiteVoList']]
    p['allPathDistanceVoList'] = [{'from': a, 'to': b, 'planDistance': 10 if a == ids[0] else 1,
                                 'duration': 60} for a in ids for b in ids if a != b]
    p['quotationModel'] = 'city'
    p['maxSiteQty'] = 0
    return p


def run(p):
    r = solve(p, time_limit_ms=180)
    assert r['status'] == 'FEASIBLE', r
    expected = {d['orderReleaseId'] for d in p['dnSiteVoList']} - {d['orderReleaseId'] for d in r['rejectedOrders']}
    actual = [dn for route in r['routes'] for dn in route['siteAddresses']]
    assert set(actual) == expected and len(actual) == len(expected)
    return r


def test_original_is_capacity_error():
    p = json.loads((ROOT/'examples/input-original.json').read_text())
    r = solve(p)
    assert r['status'] == 'INFEASIBLE_INPUT'
    assert r['errors'][0]['code'] == 'DN_EXCEEDS_ALL_VEHICLES'
    assert not r['routes']


def test_open_route_small_vehicle():
    r = run(case(1))
    assert r['totalDistance'] == 10
    assert r['routes'][0]['vehicle'] == 'small'


def test_extra_fields_are_ignored():
    p = case(1)
    expected = Request.model_validate(p)
    p['extra'] = {'metadata': True}
    for field in ('dnSiteVoList', 'vehicleTypeVoList', 'allPathDistanceVoList'):
        p[field][0]['extra'] = 'ignored'
    p['pathDistanceVoList'] = [dict(p['allPathDistanceVoList'][0])]
    expected.pathDistanceVoList = expected.allPathDistanceVoList[:1]
    assert Request.model_validate(p) == expected
    assert run(p)['servedOrderCount'] == 1


def test_vehicle_count_primary():
    r = run(case())
    assert r['vehicleCount'] == 1
    assert r['routes'][0]['vehicle'] == 'large'


@pytest.mark.parametrize('mode,expected', [(0,1),(1,1),(2,3),(None,3)])
def test_distinct_count(mode, expected):
    p = case()
    for d in p['dnSiteVoList']:
        d['siteCode'] = 'S'
    p['lonAndLatCombination'] = mode
    p['maxSiteQty'] = 1
    r = run(p)
    assert r['vehicleCount'] == expected


def test_geo_half_up():
    p = case(2)
    p['dnSiteVoList'][0]['deliveryLatitude'] = '-20.1234565'
    p['dnSiteVoList'][1]['deliveryLatitude'] = '-20.123457'
    q = Request.model_validate(p)
    assert group_key(q.dnSiteVoList[0], 1) == group_key(q.dnSiteVoList[1], 1)


def test_unload_once_same_site_tight_window():
    p = case(2)
    for d in p['dnSiteVoList']:
        d['siteCode'] = 'S'
        d['timeWindow'] = ['2026-10-21 11:01', '2026-10-21 11:02']
    r = run(p)
    assert r['vehicleCount'] == 1
    details = r['routes'][0]['siteDetails']
    assert len(details) == 2
    assert details[0]['eta'] == details[1]['eta']
    schedule = prepare(Request.model_validate(p)).schedule([1, 2])
    assert schedule[0] == schedule[1]
    assert schedule[0][2] - schedule[0][1] == 1800


def test_same_site_disjoint_windows_use_two_vehicles():
    p = case(2)
    for d in p['dnSiteVoList']:
        d['siteCode'] = 'S'
    p['dnSiteVoList'][0]['timeWindow'] = ['2026-10-21 12:00','2026-10-21 12:10']
    p['dnSiteVoList'][1]['timeWindow'] = ['2026-10-21 14:00','2026-10-21 14:10']
    r = run(p)
    assert r['vehicleCount'] == 2
    assert all(len(x['siteDetails']) == 1 for x in r['routes'])


def test_inter_stop_limit_is_hard_and_not_depot_limit():
    p = case(2)
    p['siteDistanceLimit'] = 0.5
    assert run(p)['vehicleCount'] == 2


def test_first_leg_constraint():
    p = case(2)
    for leg in p['allPathDistanceVoList']:
        leg['planDistance'] = 1 if leg['from'] == p['sourceLocation'] else 2
    p['distanceLessThanFirstLeg'] = True
    assert run(p)['vehicleCount'] == 2
    p['distanceLessThanFirstLeg'] = False
    assert run(p)['vehicleCount'] == 1


def test_time_exclusions_only():
    p = case(3)
    p['dnSiteVoList'][0]['timeWindow'] = []
    p['dnSiteVoList'][1]['timeWindow'] = ['2026-10-21 10:00','2026-10-21 11:00']
    r = run(p)
    assert {x['code'] for x in r['rejectedOrders']} == {'EMPTY_TIME_WINDOW','DIRECT_TIME_WINDOW_UNREACHABLE'}
    assert r['servedOrderCount'] == 1


def test_disable_time_windows():
    p = case(1)
    p['dnSiteVoList'][0]['timeWindow'] = []
    p['enableTimeWindows'] = False
    assert run(p)['servedOrderCount'] == 1


def test_waiting_and_latest_departure():
    p = case(1)
    p['latestShippingDate'] = '2026-10-21 11:05'
    p['dnSiteVoList'][0]['timeWindow'] = ['2026-10-21 14:00','2026-10-21 15:00']
    r = run(p)
    assert r['routes'][0]['departure_date'] == '2026-10-21 11:00:00'
    assert r['routes'][0]['siteDetails'][0]['eta'] == '2026-10-21 14:00:00'


def test_missing_matrix_is_error():
    p = case(2)
    p['allPathDistanceVoList'] = [l for l in p['allPathDistanceVoList'] if not (l['from']=='D0' and l['to']=='D1')]
    assert solve(p)['status'] == 'INVALID_INPUT'


def test_conflicting_matrix_is_error():
    p = case(1)
    p['allPathDistanceVoList'].append({**p['allPathDistanceVoList'][0], 'duration': 100})
    assert solve(p)['status'] == 'INVALID_INPUT'


def test_non_nested_vehicle_capacities():
    p = case(2)
    p['vehicleTypeVoList'] = [{'vehicleTypeName':'bulky','totalVolume':10,'totalWeight':100},
                              {'vehicleTypeName':'heavy','totalVolume':1,'totalWeight':1000}]
    p['dnSiteVoList'][0].update(totalGrossVolume=5,totalGrossWeight=90)
    p['dnSiteVoList'][1].update(totalGrossVolume=1,totalGrossWeight=500)
    assert run(p)['vehicleCount'] == 2


def test_distance_precedes_vehicle_count():
    p = case(2)
    for leg in p['allPathDistanceVoList']:
        leg['planDistance'] = 1 if leg['from']==p['sourceLocation'] else 10
    p['quotationModel'] = 'distance'
    assert run(p)['vehicleCount'] == 2
    p['quotationModel'] = 'province'
    assert run(p)['vehicleCount'] == 1


def test_independent_validator_detects_omission():
    p = prepare(Request.model_validate(case(2)))
    with pytest.raises(InputError, match='omitted'):
        validate_routes(p, [([1],0)])


def test_tiny_budget_keeps_all_orders():
    p = case(15)
    r = solve(p, time_limit_ms=1)
    assert r['status'] == 'FEASIBLE'
    assert r['servedOrderCount'] == 15


def test_empty_input():
    assert solve(case(0))['status'] == 'NO_ELIGIBLE_ORDERS'


def test_precision_not_silently_rounded():
    p = case(1)
    p['dnSiteVoList'][0]['totalGrossVolume'] = '1.0000001'
    assert solve(p)['status'] == 'INVALID_INPUT'


def test_early_arrival_waits_and_outputs_service_start_only():
    p = case(3)
    for dn in p['dnSiteVoList'][:2]:
        dn['siteCode'] = 'shared'
        dn['timeWindow'] = ['2026-10-21 14:00','2026-10-21 14:01']
    p['dnSiteVoList'][2]['timeWindow'] = ['2026-10-21 14:31','2026-10-21 14:31']
    r = run(p)
    assert r['vehicleCount'] == 1
    route = r['routes'][0]
    assert set(route) == {'dist', 'siteAddresses', 'departure_date', 'siteDetails', 'vehicle', 'volume', 'weight'}
    assert route['siteAddresses'] == [d['siteAddress'] for d in route['siteDetails']]
    details = {d['siteAddress']: d['eta'] for d in route['siteDetails']}
    assert details == {'D0': '2026-10-21 14:00:00', 'D1': '2026-10-21 14:00:00', 'D2': '2026-10-21 14:31:00'}
    for detail in route['siteDetails']:
        assert set(detail) == {'siteAddress', 'eta'}
    visits = prepare(Request.model_validate(p)).schedule([1, 2, 3])
    assert visits[0][0] < visits[0][1]


def test_cli_default_array_and_full_report(capsys):
    import sys
    from unittest.mock import patch
    from dn_router.cli import main
    for option in ([], ['--report']):
        with patch.object(sys, 'argv', ['dn-router', str(ROOT/'examples/input-feasible.json'), *option]):
            assert main() == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        if option:
            assert output['status'] == 'FEASIBLE'
            assert not captured.err
        else:
            assert isinstance(output, list)
            assert 'eta' in output[0]['siteDetails'][0]
            assert json.loads(captured.err)['status'] == 'FEASIBLE'


def test_single_point_window_is_valid():
    p = case(1)
    p['dnSiteVoList'][0]['timeWindow'] = ['2026-10-21 12:00','2026-10-21 12:00']
    assert run(p)['rejectedOrders'] == []
