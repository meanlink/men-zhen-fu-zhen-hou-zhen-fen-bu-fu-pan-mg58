from src.models import ReportStatus, ReviewReport
from src.recovery_models import QueueRecovery, RecoveryStatus

from helpers import (
    auth, diversion_of, fill_slot, generate_and_archive, make_sample, make_slot, open_recovery,
)

OBSERVED = '2026-11-20T15:10:00'
CALL_AT = '2026-11-20T15:40:00'


def setup_two_slots(db):
    analyst = 1  # seed 顺序：analyst=1, reviewer=2, admin=3
    slot_a = make_slot(db, '心内科', '午后复诊', threshold=25)
    slot_b = make_slot(db, '呼吸内科', '午后复诊', threshold=30, end='17:30')
    return slot_a, slot_b, analyst


def test_confirm_diversion_recounts_sample_to_admitted_slot(client, db):
    slot_a, slot_b, analyst = setup_two_slots(db)
    fill_slot(db, slot_a, analyst, 'A', [10, 12])
    fill_slot(db, slot_b, analyst, 'B', [8, 9])
    diverted = make_sample(db, 'A-DIV', slot_a, analyst, checkin=(14, 40))  # 已签到未叫号

    recovery = open_recovery(client, auth(client, 'analyst'), diverted.id, OBSERVED)
    reviewer = auth(client, 'reviewer')
    confirmed = client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=reviewer,
                            json={'admitted_slot_id': slot_b.id, 'note': '分流到呼吸内科 2 号诊室'})
    assert confirmed.status_code == 201, confirmed.text
    outcome = confirmed.json()
    assert outcome['status'] == 'confirmed'
    assert outcome['origin_slot_id'] == slot_a.id
    assert outcome['admitted_slot_id'] == slot_b.id

    # 处置单进入已分流状态，沿用原签到叫号。
    assert db.get(QueueRecovery, recovery['id']).status == RecoveryStatus.DIVERTED

    # 分流后叫号（沿用原签到）。
    called = client.post(f"/samples/{diverted.id}/call", headers=auth(client, 'analyst'),
                         json={'event_time': CALL_AT})
    assert called.status_code == 200, called.text

    # 样本改计入接诊科：B 报告包含该样本，A 报告不包含。
    report_b = client.post('/reports', headers=auth(client, 'analyst'),
                           json={'department_slot_id': slot_b.id, 'title': '呼吸内科分布'})
    assert report_b.json()['sample_count'] == 3  # B 原有 2 条 + 分流 1 条
    report_a = client.post('/reports', headers=auth(client, 'analyst'),
                           json={'department_slot_id': slot_a.id, 'title': '心内科分布'})
    assert report_a.json()['sample_count'] == 2  # 分流样本不再计入原科

    # 候诊时长沿用原签到叫号：15:40 - 14:40 = 60 分钟，计入 B 的 60+ 桶。
    bucket = {row['label']: row['count'] for row in report_b.json()['distribution']}
    assert bucket['60 分钟以上'] == 1


def test_autopublish_when_both_slots_archived(client, db):
    slot_a, slot_b, analyst = setup_two_slots(db)
    fill_slot(db, slot_a, analyst, 'A', [10, 12, 14])
    fill_slot(db, slot_b, analyst, 'B', [8, 9, 11])
    prev_a = generate_and_archive(client, slot_a.id, '心内科午后分布')
    prev_b = generate_and_archive(client, slot_b.id, '呼吸内科午后分布')

    diverted = make_sample(db, 'A-DIV', slot_a, analyst, checkin=(14, 40))
    recovery = open_recovery(client, auth(client, 'analyst'), diverted.id, OBSERVED)
    client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'reviewer'),
                json={'admitted_slot_id': slot_b.id, 'note': '分流呼吸内科'})
    client.post(f"/samples/{diverted.id}/call", headers=auth(client, 'analyst'), json={'event_time': CALL_AT})

    outcome = diversion_of(client, diverted.id)
    assert outcome['status'] == 'published'
    assert outcome['prev_origin_report_id'] == prev_a
    assert outcome['prev_admitted_report_id'] == prev_b
    assert outcome['published_at'] is not None

    # 旧报告留档仍可查，两科各多一份已归档重算报告。
    reports = client.get('/reports', headers=auth(client, 'analyst')).json()
    by_id = {row['id']: row for row in reports}
    assert by_id[prev_a]['status'] == 'archived'
    assert by_id[prev_b]['status'] == 'archived'
    new_a = by_id[outcome['origin_report_id']]
    new_b = by_id[outcome['admitted_report_id']]
    assert new_a['status'] == new_b['status'] == 'archived'
    assert new_a['department_slot_id'] == slot_a.id
    assert new_b['department_slot_id'] == slot_b.id
    # 更正后：原科不含分流样本，接诊科包含。
    assert new_a['sample_count'] == 3
    assert new_b['sample_count'] == 4


def test_publish_failure_keeps_old_versions_and_retry_once(client, db):
    slot_a, slot_b, analyst = setup_two_slots(db)
    fill_slot(db, slot_a, analyst, 'A', [10, 12, 14])
    fill_slot(db, slot_b, analyst, 'B', [8, 9, 11])
    generate_and_archive(client, slot_a.id, '心内科午后分布')
    generate_and_archive(client, slot_b.id, '呼吸内科午后分布')
    report_total = lambda: len(client.get('/reports', headers=auth(client, 'analyst')).json())
    assert report_total() == 2

    # 确认分流时样本尚未叫号，接诊科重算缺叫号 → 发布失败，两科保留旧版。
    diverted = make_sample(db, 'A-DIV', slot_a, analyst, checkin=(14, 40))
    recovery = open_recovery(client, auth(client, 'analyst'), diverted.id, OBSERVED)
    client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'reviewer'),
                json={'admitted_slot_id': slot_b.id, 'note': '分流呼吸内科'})
    outcome = diversion_of(client, diverted.id)
    assert outcome['status'] == 'failed'
    assert report_total() == 2  # 没有生成任何重算报告

    # 补齐叫号后重试发布成功。
    client.post(f"/samples/{diverted.id}/call", headers=auth(client, 'analyst'), json={'event_time': CALL_AT})
    outcome = diversion_of(client, diverted.id)
    assert outcome['status'] == 'published'
    assert report_total() == 4
    published_at = outcome['published_at']

    # 重试只生效一次：再次发布幂等，不重复生成报告。
    retry = client.post(f"/diversions/{outcome['id']}/publish", headers=auth(client, 'admin'))
    assert retry.status_code == 200, retry.text
    assert retry.json()['status'] == 'published'
    assert retry.json()['published_at'] == published_at
    assert report_total() == 4


def test_archive_blocked_after_diversion_but_prior_archive_kept(client, db):
    slot_a, slot_b, analyst = setup_two_slots(db)
    fill_slot(db, slot_a, analyst, 'A', [10, 12, 14])
    fill_slot(db, slot_b, analyst, 'B', [8, 9, 11])
    # 先归档的报告在转归确认后仍可查。
    archived_before = generate_and_archive(client, slot_a.id, '心内科早间归档')

    # 转归确认前生成、确认后才复核的“旧分布”报告。
    stale = client.post('/reports', headers=auth(client, 'analyst'),
                        json={'department_slot_id': slot_a.id, 'title': '心内科旧分布'})
    stale_id = stale.json()['id']
    reviewed = client.post(f'/reports/{stale_id}/review', headers=auth(client, 'reviewer'),
                           json={'review_note': '确认前复核'})
    assert reviewed.status_code == 200

    diverted = make_sample(db, 'A-DIV', slot_a, analyst, checkin=(14, 40))
    recovery = open_recovery(client, auth(client, 'analyst'), diverted.id, OBSERVED)
    client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'reviewer'),
                json={'admitted_slot_id': slot_b.id, 'note': '分流呼吸内科'})

    # 确认后不能再归档旧分布。
    blocked = client.post(f'/reports/{stale_id}/archive', headers=auth(client, 'admin'))
    assert blocked.status_code == 409
    assert '转归' in blocked.json()['detail']

    # 先归档的报告仍可查。
    reports = client.get('/reports', headers=auth(client, 'analyst')).json()
    assert any(row['id'] == archived_before and row['status'] == 'archived' for row in reports)


def test_diversion_permissions_and_validation(client, db):
    slot_a, slot_b, analyst = setup_two_slots(db)
    fill_slot(db, slot_a, analyst, 'A', [10, 12])
    diverted = make_sample(db, 'A-DIV', slot_a, analyst, checkin=(14, 40))
    recovery = open_recovery(client, auth(client, 'analyst'), diverted.id, OBSERVED)

    # 非复核员不能确认分流。
    forbidden = client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'analyst'),
                            json={'admitted_slot_id': slot_b.id, 'note': '越权尝试'})
    assert forbidden.status_code == 403
    # 接诊时段不能是原时段。
    same = client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'reviewer'),
                       json={'admitted_slot_id': slot_a.id, 'note': '原地分流'})
    assert same.status_code == 409
    # 接诊时段不存在。
    missing = client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'reviewer'),
                          json={'admitted_slot_id': 9999, 'note': '不存在时段'})
    assert missing.status_code == 404
    # 登记人本人不能复核自己的处置单。
    mine = client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'analyst'),
                       json={'admitted_slot_id': slot_b.id, 'note': '自己分流自己'})
    assert mine.status_code == 403


def test_diversion_list_exposes_reports_for_review_page(client, db):
    slot_a, slot_b, analyst = setup_two_slots(db)
    fill_slot(db, slot_a, analyst, 'A', [10, 12, 14])
    fill_slot(db, slot_b, analyst, 'B', [8, 9, 11])
    generate_and_archive(client, slot_a.id, '心内科午后分布')
    generate_and_archive(client, slot_b.id, '呼吸内科午后分布')

    diverted = make_sample(db, 'A-DIV', slot_a, analyst, checkin=(14, 40))
    recovery = open_recovery(client, auth(client, 'analyst'), diverted.id, OBSERVED)
    client.post(f"/queue-recoveries/{recovery['id']}/diversion", headers=auth(client, 'reviewer'),
                json={'admitted_slot_id': slot_b.id, 'note': '分流呼吸内科'})
    client.post(f"/samples/{diverted.id}/call", headers=auth(client, 'analyst'), json={'event_time': CALL_AT})

    rows = client.get('/diversions', headers=auth(client, 'reviewer')).json()
    assert len(rows) == 1
    row = rows[0]
    # 复盘页面可拿到转归记录与两科更正前后报告 id。
    assert row['sample_id'] == diverted.id
    assert row['prev_origin_report_id'] and row['prev_admitted_report_id']
    assert row['origin_report_id'] and row['admitted_report_id']
    reports = {item['id']: item for item in client.get('/reports', headers=auth(client, 'analyst')).json()}
    before_b = reports[row['prev_admitted_report_id']]['sample_count']
    after_b = reports[row['admitted_report_id']]['sample_count']
    assert after_b == before_b + 1  # 接诊科更正后计入分流样本
