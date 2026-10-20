import json
import re
from decimal import Decimal
import pytest

from dn_router import render_html


def report_data(html):
    return json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S).group(1))


def test_embedded_data_cannot_close_script_or_inject_html():
    attack = '</script><img src=x onerror=alert(1)>'
    html = render_html({}, {'status': 'INVALID_INPUT', 'errors': [{'message': attack}], 'routes': []})
    assert attack not in html
    assert report_data(html)['result']['errors'][0]['message'] == attack
    assert '<script src=' not in html


def test_stop_coordinates_require_all_orders_to_agree():
    payload = {'dnSiteVoList': [
        {'orderReleaseId': 'A', 'deliveryLatitude': Decimal('20'), 'deliveryLongitude': Decimal('30')},
        {'orderReleaseId': 'B', 'deliveryLatitude': 21, 'deliveryLongitude': 30}],
        'sourceLatitude': 0, 'sourceLongitude': 0}
    result = {'routes': [{'stops': [{'orderReleaseIds': ['A']}, {'orderReleaseIds': ['A', 'B']},
                                    {'orderReleaseIds': ['missing']}]}]}
    data = report_data(render_html(payload, result))
    assert data['depot']['coordinate'] == [0, 0]
    assert [s['coordinate'] for s in data['result']['routes'][0]['stops']] == [[30, 20], None, None]
    assert 'coordinate' not in result['routes'][0]['stops'][0]


def test_invalid_input_can_render_without_routes():
    html = render_html({'dnSiteVoList': None, 'vehicleTypeVoList': None, 'sourceLatitude': 'bad'},
                       {'status': 'INVALID_INPUT', 'routes': []})
    assert report_data(html)['depot']['coordinate'] is None


def test_malformed_input_collections_and_nonfinite_values():
    html = render_html({'dnSiteVoList': 42, 'vehicleTypeVoList': [{'totalVolume': float('nan')}]},
                       {'status': 'INVALID_INPUT', 'routes': []})
    assert report_data(html)['vehicles'][0]['totalVolume'] is None


@pytest.mark.parametrize('report', [False, True])
def test_cli_generates_json_and_html(tmp_path, monkeypatch, report):
    import sys
    from dn_router.cli import main
    source = tmp_path / 'input.json'
    source.write_text('{}')
    output, html = tmp_path / 'result.json', tmp_path / 'result.html'
    monkeypatch.setattr(sys, 'argv', ['dn-router', str(source), *(['--report'] if report else []), '--output', str(output), '--html', str(html)])
    assert main() == 2
    body = json.loads(output.read_text())
    assert body['status'] == 'INVALID_INPUT' if report else body == []
    assert report_data(html.read_text())['result']['status'] == 'INVALID_INPUT'


def test_cli_rejects_overwriting_input_with_html(tmp_path, monkeypatch):
    import sys
    import pytest
    from dn_router.cli import main
    source = tmp_path / 'input.json'
    source.write_text('{}')
    monkeypatch.setattr(sys, 'argv', ['dn-router', str(source), '--html', str(source)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert source.read_text() == '{}'


def timed_route(start='2026-10-21 23:50:00'):
    return {'departureTime': start, 'completionTime': '2026-10-22 00:30:00', 'stops': [
        {'arrivalTime': '2026-10-22 00:10:00', 'departureTime': '2026-10-22 00:20:00',
         'waitingBeforeArrivalSeconds': 600, 'orderReleaseIds': []},
        {'arrivalTime': '2026-10-22 00:25:00', 'departureTime': '2026-10-22 00:30:00',
         'waitingBeforeArrivalSeconds': 0, 'orderReleaseIds': []}]}


def test_timeline_crosses_midnight_and_splits_wait_once():
    timeline = report_data(render_html({}, {'routes': [timed_route()]}))['timeline']
    assert timeline['startTime'] == '2026-10-21 23:50:00'
    assert timeline['durationSeconds'] == 2400
    segments = timeline['routes'][0]['segments']
    assert [(s['kind'], s['start'], s['end']) for s in segments] == [
        ('travel', 0, 600), ('wait', 600, 1200), ('unload', 1200, 1800),
        ('travel', 1800, 2100), ('wait', 2100, 2100), ('unload', 2100, 2400)]
    assert sum(s['end']-s['start'] for s in segments) == 2400
    assert [s['stopIndex'] for s in segments] == [0, 0, 0, 1, 1, 1]


def test_timeline_multiple_departures_share_one_origin():
    timeline = report_data(render_html({}, {'routes': [timed_route(), timed_route('2026-10-22 00:00:00')]}))['timeline']
    assert [r['start'] for r in timeline['routes']] == [0, 600]
    assert [r['end'] for r in timeline['routes']] == [2400, 2400]
    assert timeline['routes'][1]['segments'][0]['start'] == timeline['routes'][1]['segments'][0]['end'] == 600


def test_timeline_invalid_or_absent_times_do_not_invent_a_schedule():
    route = timed_route()
    route['stops'][0]['waitingBeforeArrivalSeconds'] = 2000  # Would start travel before departure.
    timeline = report_data(render_html({}, {'routes': [route, timed_route()]}))['timeline']
    assert timeline['routes'][0] is None
    assert timeline['routes'][1] is not None
    assert report_data(render_html({}, {'routes': []}))['timeline'] is None


def test_instantaneous_route_has_zero_duration_without_fabricated_time():
    route = {'departureTime': '2026-10-21 11:00:00', 'completionTime': '2026-10-21 11:00:00',
             'stops': [{'arrivalTime': '2026-10-21 11:00:00', 'departureTime': '2026-10-21 11:00:00',
                        'waitingBeforeArrivalSeconds': 0, 'orderReleaseIds': []}]}
    timeline = report_data(render_html({}, {'routes': [route]}))['timeline']
    assert timeline['durationSeconds'] == 0
    assert all(s['start'] == s['end'] == 0 for s in timeline['routes'][0]['segments'])


def test_public_route_contract_keeps_eta_and_counts_shared_unloading_once():
    import copy
    from pathlib import Path
    payload = json.loads((Path(__file__).resolve().parents[1] / 'examples/input-multi.json').read_text())
    first, second, third = [dn['orderReleaseId'] for dn in payload['dnSiteVoList']]
    public_route = {'dist': 110, 'siteAddresses': [first, second, third],
                    'departure_date': '2026-10-21 11:00:00',
                    'siteDetails': [{'siteAddress': first, 'eta': '2026-10-21 14:00:00'},
                                    {'siteAddress': second, 'eta': '2026-10-21 14:00:00'},
                                    {'siteAddress': third, 'eta': '2026-10-21 14:40:00'}],
                    'vehicle': 'VAN-BRA', 'volume': 3, 'weight': 300}
    result = {'status': 'FEASIBLE', 'routes': [public_route]}
    original = copy.deepcopy(result)
    data = report_data(render_html(payload, result))
    route = data['result']['routes'][0]
    assert result == original
    assert route['physicalStopCount'] == route['siteCount'] == 2
    assert route['stops'][0]['orderReleaseIds'] == [first, second]
    assert route['stops'][0]['arrivalTime'] == '2026-10-21 14:00:00'
    assert route['stops'][0]['departureTime'] == '2026-10-21 14:30:00'
    assert route['stops'][0]['waitingBeforeArrivalSeconds'] == 10200
    assert route['stops'][1]['waitingBeforeArrivalSeconds'] == 0
    assert route['completionTime'] == '2026-10-21 15:10:00'
    segments = data['timeline']['routes'][0]['segments']
    assert sum(s['end'] - s['start'] for s in segments if s['kind'] == 'unload') == 3600
    assert sum(s['end'] - s['start'] for s in segments if s['kind'] == 'travel') == 1200
