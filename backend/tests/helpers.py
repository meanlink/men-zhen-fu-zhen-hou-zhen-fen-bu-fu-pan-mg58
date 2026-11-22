from datetime import date, datetime, time, timedelta

from src.models import DepartmentSlot, SampleStatus, WaitSample

DAY = date(2026, 11, 20)


def auth(client, username):
    response = client.post('/auth/login', json={'username': username, 'password': 'demo123'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


def make_slot(db, name, label, threshold=25, start='13:30', end='17:00'):
    slot = DepartmentSlot(department_name=name, slot_date=DAY, slot_label=label,
                          start_time=time.fromisoformat(start), end_time=time.fromisoformat(end),
                          threshold_minutes=threshold, capacity=20, status='open')
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


def make_sample(db, code, slot, creator_id, checkin=None, wait=None, alias='测试患者'):
    appointment = datetime(2026, 11, 20, 14, 0)
    checkin_time = appointment.replace(hour=checkin[0], minute=checkin[1]) if checkin else None
    called_time = checkin_time + timedelta(minutes=wait) if checkin_time is not None and wait is not None else None
    status = SampleStatus.CALLED if called_time else (SampleStatus.CHECKED_IN if checkin_time else SampleStatus.BOOKED)
    sample = WaitSample(sample_code=code, patient_alias=alias, department_slot_id=slot.id,
                        appointment_time=appointment, checkin_time=checkin_time, called_time=called_time,
                        status=status, source_channel='测试', created_by=creator_id)
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def fill_slot(db, slot, creator_id, prefix, waits):
    """给时段补一批完整样本，保证报告可归档。"""
    return [make_sample(db, f'{prefix}-{i}', slot, creator_id, checkin=(14, i * 5), wait=wait)
            for i, wait in enumerate(waits)]


def open_recovery(client, headers, sample_id, observed_at):
    response = client.post('/queue-recoveries', headers=headers,
                           json={'sample_id': sample_id, 'observed_at': observed_at,
                                 'incident_note': '候诊超过时段阈值，现场登记'})
    assert response.status_code == 201, response.text
    return response.json()


def generate_and_archive(client, slot_id, title):
    analyst = auth(client, 'analyst')
    reviewer = auth(client, 'reviewer')
    admin = auth(client, 'admin')
    report = client.post('/reports', headers=analyst, json={'department_slot_id': slot_id, 'title': title})
    assert report.status_code == 200, report.text
    report_id = report.json()['id']
    reviewed = client.post(f'/reports/{report_id}/review', headers=reviewer, json={'review_note': '已核对叫号记录'})
    assert reviewed.status_code == 200, reviewed.text
    archived = client.post(f'/reports/{report_id}/archive', headers=admin)
    assert archived.status_code == 200, archived.text
    return report_id


def diversion_of(client, sample_id):
    rows = client.get('/diversions', headers=auth(client, 'analyst')).json()
    return next((row for row in rows if row['sample_id'] == sample_id), None)
