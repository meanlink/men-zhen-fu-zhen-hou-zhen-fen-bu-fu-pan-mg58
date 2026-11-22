from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from src.models import DepartmentSlot, ReportStatus, ReviewReport, SampleStatus, WaitSample
from src.recovery_models import QueueRecovery, RecoveryStatus
from src.services.workflow import archive_report, create_report, review_report

DAY = date(2026, 11, 20)


def _slot(db, name, label, start, end, threshold):
    slot = db.scalar(select(DepartmentSlot).where(
        DepartmentSlot.department_name == name, DepartmentSlot.slot_date == DAY,
        DepartmentSlot.slot_label == label))
    if not slot:
        slot = DepartmentSlot(department_name=name, slot_date=DAY, slot_label=label,
                              start_time=start, end_time=end, threshold_minutes=threshold, capacity=12)
        db.add(slot)
        db.flush()
    return slot


def _fill(db, slot, analyst, prefix, waits):
    base = datetime.combine(DAY, slot.start_time)
    for i, wait in enumerate(waits, start=1):
        code = f'{prefix}-{i:02}'
        if db.scalar(select(WaitSample.id).where(WaitSample.sample_code == code)):
            continue
        appointment = base + timedelta(minutes=10 * (i - 1))
        checkin = appointment + timedelta(minutes=3)
        called = checkin + timedelta(minutes=wait)
        db.add(WaitSample(sample_code=code, patient_alias=f'分流演示 {i}', department_slot_id=slot.id,
                          appointment_time=appointment, checkin_time=checkin, called_time=called,
                          completed_time=called + timedelta(minutes=10), status=SampleStatus.COMPLETED,
                          source_channel='线上预约', created_by=analyst.id))
    db.commit()


def _archive(db, slot, analyst, reviewer, admin, title):
    existing = db.scalar(select(ReviewReport).where(
        ReviewReport.department_slot_id == slot.id, ReviewReport.status == ReportStatus.ARCHIVED))
    if existing:
        return existing
    report = create_report(db, slot.id, title, analyst)
    review_report(db, report, reviewer, '已逐条核对候诊记录与时段分布。')
    return archive_report(db, report, admin)


def seed_diversion(db, analyst, reviewer, admin):
    origin = _slot(db, '心内科', '周五夜间复诊', time(17), time(19), 25)
    admitted = _slot(db, '呼吸内科', '周五夜间复诊', time(17), time(19, 30), 30)
    _fill(db, origin, analyst, 'DV-A', [12, 16, 20])
    _fill(db, admitted, analyst, 'DV-B', [9, 14, 18])
    _archive(db, origin, analyst, reviewer, admin, '心内科夜间复诊 · 归档基线')
    _archive(db, admitted, analyst, reviewer, admin, '呼吸内科夜间复诊 · 归档基线')
    # 归档后再放入一条超时待分流样本，演示确认 → 发布失败 → 叫号 → 重试成功的链路。
    sample = db.scalar(select(WaitSample).where(WaitSample.sample_code == 'DV-A-09'))
    if not sample:
        appointment = datetime.combine(DAY, time(17, 30))
        sample = WaitSample(sample_code='DV-A-09', patient_alias='分流演示患者', department_slot_id=origin.id,
                            appointment_time=appointment, checkin_time=appointment + timedelta(minutes=4),
                            status=SampleStatus.CHECKED_IN, source_channel='现场加号', created_by=analyst.id)
        db.add(sample)
        db.flush()
    if not db.scalar(select(QueueRecovery.id).where(QueueRecovery.sample_id == sample.id)):
        observed = datetime.combine(DAY, time(18, 2))
        waited = int((observed - sample.checkin_time).total_seconds() // 60)
        db.add(QueueRecovery(sample_id=sample.id, status=RecoveryStatus.PENDING, observed_at=observed,
                             observed_wait_minutes=waited, threshold_minutes=origin.threshold_minutes,
                             incident_note='候诊超过心内科阈值，现场等待接诊安排。', opened_by=analyst.id))
    db.commit()
    print('diversion seed ok: 心内科/呼吸内科夜间复诊已归档，DV-A-09 待分流')
