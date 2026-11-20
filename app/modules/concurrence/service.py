from app.errors import RegisterError
from app.reasons import REASONS

RELEASE_CONDITION = '超限争议见证复核结论形成前，两幅图共同暂扣，不得按多数点平均或按单幅拆分放行'


def station_evidence(station):
    # 保留源行与逐列原值，图幅状态可据此回查原件
    return {'name': station['name'], 'verdict': station['verdict'], 'verdict_label': REASONS[station['verdict']],
            'gap_m': station['gap_m'], 'normal_m': station['normal_m'], 'tangent_m': station['tangent_m'],
            'a_pixel': station['a_pixel'], 'b_pixel': station['b_pixel'], 'source': station['source']}


def build_concurrence(snapshot, edge_code, reviewer):
    edge = next((item for item in snapshot['seams']['edges'] if item['code'] == edge_code), None)
    if edge is None:
        raise RegisterError('EDGE_NOT_FOUND', '该批次没有此拼缝', 404)
    if edge['max_m'] is None:
        raise RegisterError('EDGE_NOT_MEASURABLE', '拼缝缺模型或次序不合，暂不能会签', 409, edge['reason'])
    stations = edge['stations']
    adopted = [station_evidence(row) for row in stations if row['verdict'] == 'WITHIN_LIMIT']
    disputed = [station_evidence(row) for row in stations if row['verdict'] == 'OVER_LIMIT']
    if not adopted or not disputed:
        raise RegisterError('NO_MIXED_EVIDENCE', '会签需同时存在限内采信点与超限争议点', 409, edge_code)
    titles = {sheet['code']: sheet['title'] for sheet in snapshot['catalog']['sheets']}
    return {
        'run_id': snapshot['manifest']['run_id'], 'edge': edge_code, 'reviewer': reviewer,
        'a_side': edge['a_side'], 'b_side': edge['b_side'], 'limit_m': edge['limit_m'],
        'max_m': edge['max_m'], 'median_m': edge['median_m'],
        'summary': {'station_count': len(stations), 'adopted_count': len(adopted),
                    'disputed_count': len(disputed)},
        'adopted_stations': adopted, 'disputed_stations': disputed,
        'disposition': {
            'code': 'JOINT_HOLD', 'label': REASONS['JOINT_HOLD'],
            'sheets': [{'code': edge['a'], 'title': titles.get(edge['a'], edge['a'])},
                       {'code': edge['b'], 'title': titles.get(edge['b'], edge['b'])}],
            'release_condition': RELEASE_CONDITION,
        },
    }
