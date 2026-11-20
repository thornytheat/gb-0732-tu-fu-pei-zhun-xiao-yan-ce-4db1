import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.concurrence.service import RELEASE_CONDITION, build_concurrence
from app.store import Store

HEADER = '拼缝,证点,甲列,甲行,乙列,乙行,备注\n'
# 1 个超限争议点（HX-QS-06 乙侧沿边挪动 8 px）+ 5 个限内采信点；去掉一站即题面的 4 限内组合
MIXED_WITNESS = HEADER + (
    'HX-QS,HX-QS-01,1200,74,0,74,接边独立尺量\n'
    'HX-QS,HX-QS-02,1200,231,0,231,接边独立尺量\n'
    'HX-QS,HX-QS-03,1200,402,0,402,接边独立尺量\n'
    'HX-QS,HX-QS-04,1200,584,0,584,接边独立尺量\n'
    'HX-QS,HX-QS-05,1200,760,0,760,接边独立尺量\n'
    'HX-QS,HX-QS-06,1200,839,0,847,接边独立尺量\n'
    'HX-LW,HX-LW-01,93,900,93,0,x\nHX-LW,HX-LW-02,265,900,265,0,x\nHX-LW,HX-LW-03,538,900,538,0,x\n'
    'HX-LW,HX-LW-04,719,900,719,0,x\nHX-LW,HX-LW-05,976,900,976,0,x\nHX-LW,HX-LW-06,1101,900,1101,0,x\n'
    'QS-SJ,QS-SJ-01,93,900,93,0,x\nQS-SJ,QS-SJ-02,265,900,265,0,x\nQS-SJ,QS-SJ-03,538,900,538,0,x\n'
    'QS-SJ,QS-SJ-04,719,900,719,0,x\nQS-SJ,QS-SJ-05,976,900,976,0,x\nQS-SJ,QS-SJ-06,1101,900,1101,0,x\n')


@pytest.fixture
def mixed_client(settings, tmp_path):
    # 用独立样例目录构造「一超四/五限内」的混合证据批次
    import shutil
    root = tmp_path / 'inputs'
    shutil.copytree(settings.sample_root, root)
    (root / 'reference' / 'witness.csv').write_text(MIXED_WITNESS, encoding='utf-8')
    settings.sample_root = root
    with TestClient(create_app(settings)) as client:
        yield client


def mixed_run(client):
    payload = client.post('/api/review/runs', json={'dataset': 'reference'}).json()
    edge = next(item for item in payload['seams']['edges'] if item['code'] == 'HX-QS')
    return payload, edge


def test_mixed_edge_keeps_each_station_verdict_and_fails_as_a_whole(mixed_client):
    payload, edge = mixed_run(mixed_client)
    assert edge['status'] == 'FAIL'
    verdicts = [station['verdict'] for station in edge['stations']]
    assert verdicts.count('WITHIN_LIMIT') == 5 and verdicts.count('OVER_LIMIT') == 1
    over = next(station for station in edge['stations'] if station['verdict'] == 'OVER_LIMIT')
    assert (over['name'], over['source']['id'], over['source']['line']) == ('HX-QS-06', 'witness.csv:7', 7)
    assert over['gap_m'] > edge['limit_m']
    # 机器判定仍是整边失败：多数限内点不会把真实错缝平均掉
    reviews = {row['code']: row for row in payload['reviews']}
    assert reviews['0421']['failed_edges'] == ['HX-QS']
    assert reviews['0422']['failed_edges'] == ['HX-QS']


def test_concurrence_records_adopted_disputed_and_joint_hold_for_both_folios(mixed_client):
    payload, _ = mixed_run(mixed_client)
    run_id = payload['manifest']['run_id']
    response = mixed_client.post(f'/api/review/runs/{run_id}/concurrences',
                                 json={'edge': 'HX-QS', 'reviewer': ' 值班长老周 '})
    assert response.status_code == 200
    document = response.json()['concurrences'][0]
    assert document['reviewer'] == '值班长老周'
    assert document['summary'] == {'station_count': 6, 'adopted_count': 5, 'disputed_count': 1}
    assert [row['name'] for row in document['adopted_stations']] == [f'HX-QS-0{i}' for i in range(1, 6)]
    disputed = document['disputed_stations'][0]
    assert (disputed['verdict'], disputed['source']['id']) == ('OVER_LIMIT', 'witness.csv:7')
    assert document['disposition']['code'] == 'JOINT_HOLD'
    assert [sheet['code'] for sheet in document['disposition']['sheets']] == ['0421', '0422']
    assert document['disposition']['release_condition'] == RELEASE_CONDITION

    # 回读批次：会签随快照留存，图幅状态可回查原件行
    reread = mixed_client.get(f'/api/review/runs/{run_id}').json()
    assert reread['concurrences'] == [document]
    by_code = {row['code']: row for row in reread['reviews']}
    assert by_code['0421']['concurrence_edges'] == ['HX-QS']
    assert by_code['0422']['concurrence_edges'] == ['HX-QS']
    assert by_code['0431']['concurrence_edges'] == []
    exported = mixed_client.get(f'/api/review/runs/{run_id}/export').json()
    assert exported['concurrences'][0]['disputed_stations'][0]['source']['raw'][1] == 'HX-QS-06'


def test_concurrence_requires_one_over_limit_and_one_within_limit_station(client):
    payload = client.post('/api/review/runs', json={'dataset': 'reference'}).json()
    run_id = payload['manifest']['run_id']
    response = client.post(f'/api/review/runs/{run_id}/concurrences',
                           json={'edge': 'HX-QS', 'reviewer': '老周'})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'NO_MIXED_EVIDENCE'


def test_blocked_edge_without_measurements_cannot_be_concurred(client):
    payload = client.post('/api/review/runs', json={}).json()
    run_id = payload['manifest']['run_id']
    response = client.post(f'/api/review/runs/{run_id}/concurrences',
                           json={'edge': 'QS-SJ', 'reviewer': '老周'})
    assert (response.status_code, response.json()['detail']['code']) == (409, 'EDGE_NOT_MEASURABLE')
    assert response.json()['detail']['evidence'] == 'MODEL_UNAVAILABLE'


def test_unknown_edge_and_unknown_run_are_404(mixed_client):
    payload, _ = mixed_run(mixed_client)
    run_id = payload['manifest']['run_id']
    missing = mixed_client.post(f'/api/review/runs/{run_id}/concurrences',
                                json={'edge': 'NO-SUCH', 'reviewer': '老周'})
    assert missing.status_code == 404 and missing.json()['detail']['code'] == 'EDGE_NOT_FOUND'
    gone = mixed_client.post(f'/api/review/runs/{"0" * 64}/concurrences',
                             json={'edge': 'HX-QS', 'reviewer': '老周'})
    assert gone.status_code == 404 and gone.json()['detail']['code'] == 'RUN_NOT_FOUND'


def test_contract_inputs_are_validated_and_stale_version_on_empty_record_conflicts(mixed_client):
    payload, _ = mixed_run(mixed_client)
    run_id = payload['manifest']['run_id']
    url = f'/api/review/runs/{run_id}/concurrences'
    bad_pattern = mixed_client.post(url, json={'edge': 'hx qs', 'reviewer': '老周'})
    assert bad_pattern.status_code == 422 and bad_pattern.json()['detail']['code'] == 'INVALID_INPUT'
    blank_name = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '   '})
    assert blank_name.status_code == 422
    extra = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '老周', 'unexpected': 1})
    assert extra.status_code == 422
    stale = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '老周', 'expected_version': 3})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'CONCURRENCE_VERSION_CONFLICT'


def test_same_batch_rerun_cannot_add_a_second_formal_concurrence(mixed_client):
    payload, _ = mixed_run(mixed_client)
    run_id = payload['manifest']['run_id']
    url = f'/api/review/runs/{run_id}/concurrences'
    first = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '老周'})
    assert first.status_code == 200 and len(first.json()['concurrences']) == 1
    # 保持参数不变重跑：复用同一批次，不会产生第二份正式会签
    rerun = mixed_client.post('/api/review/runs', json={'dataset': 'reference'}).json()
    assert rerun['manifest']['run_id'] == run_id and rerun['reused'] is True
    another = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '小陈'})
    assert another.status_code == 409 and another.json()['detail']['code'] == 'CONCURRENCE_EXISTS'
    stale = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '小陈', 'expected_version': 7})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'CONCURRENCE_EXISTS'
    own_stale = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '老周', 'expected_version': 9})
    assert own_stale.status_code == 409
    assert own_stale.json()['detail']['code'] == 'CONCURRENCE_VERSION_CONFLICT'
    # 同人重复请求幂等，仍只保留一份
    repeat = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '老周'})
    assert repeat.status_code == 200 and len(repeat.json()['concurrences']) == 1
    repeat_versioned = mixed_client.post(url, json={'edge': 'HX-QS', 'reviewer': '老周', 'expected_version': 1})
    assert repeat_versioned.status_code == 200 and len(repeat_versioned.json()['concurrences']) == 1
    stored = mixed_client.get(f'/api/review/runs/{run_id}').json()['concurrences']
    assert [item['reviewer'] for item in stored] == ['老周'] and stored[0]['version'] == 1


def test_failed_save_rolls_back_and_keeps_the_previous_concurrence(mixed_client, settings):
    # 先在正常库里形成旧版会签
    payload, _ = mixed_run(mixed_client)
    run_id = payload['manifest']['run_id']
    snapshot = Store(settings.database).get(run_id)
    document = build_concurrence(snapshot, 'HX-QS', '老周')
    document['expected_version'] = 0
    Store(settings.database).concur(run_id, 'HX-QS', document)

    # 破坏写库语句，模拟保存失败：事务必须回滚，旧版会签原样保留
    class FailingStore(Store):
        def concur(self, run_id, edge, payload):
            with self.connect('IMMEDIATE') as connection:
                self._read(connection, run_id)
                connection.execute('INSERT INTO concurrences VALUES (?, ?, ?, ?, ?, 1)',
                                   (run_id, 'HX-LW', '{}', '小陈', '2026-01-01T00:00:00+00:00'))
                connection.execute('INSERT INTO no_such_table VALUES (1)')

    with pytest.raises(sqlite3.OperationalError):
        FailingStore(settings.database).concur(run_id, 'HX-QS', document)
    with sqlite3.connect(settings.database) as connection:
        rows = connection.execute('SELECT edge, reviewer, version FROM concurrences').fetchall()
    assert rows == [('HX-QS', '老周', 1)]
    restored = Store(settings.database).get(run_id)['concurrences']
    assert len(restored) == 1 and restored[0]['reviewer'] == '老周'


def test_concurrence_survives_application_restart(settings, tmp_path):
    import shutil
    root = tmp_path / 'inputs'
    shutil.copytree(settings.sample_root, root)
    (root / 'reference' / 'witness.csv').write_text(MIXED_WITNESS, encoding='utf-8')
    settings.sample_root = root
    with TestClient(create_app(settings)) as first:
        payload = first.post('/api/review/runs', json={'dataset': 'reference'}).json()
        run_id = payload['manifest']['run_id']
        response = first.post(f'/api/review/runs/{run_id}/concurrences',
                              json={'edge': 'HX-QS', 'reviewer': '老周'})
        assert response.status_code == 200
    with TestClient(create_app(settings)) as second:
        persisted = second.get(f'/api/review/runs/{run_id}/export').json()
        assert persisted['concurrences'][0]['edge'] == 'HX-QS'
        assert persisted['concurrences'][0]['disposition']['sheets'][0]['code'] == '0421'
