import json
import secrets

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..diversion_models import DiversionOutcome
from ..models import (
    AttendanceException, AttendanceStatus, DepartmentSlot, ReportAttendanceExclusion,
    ReportStatus, ReviewReport, User, WaitSample,
)
from .analytics import calculate_review


def slot_sample_rows(db: Session, slot_id: int):
    """按有效归属取时段样本：未分流的归原时段，已确认分流的改计接诊时段。

    候诊时长仍由样本自身的签到、叫号时间算出，归属调整只改变统计口径。
    """
    return db.execute(
        select(WaitSample, AttendanceException)
        .outerjoin(DiversionOutcome, DiversionOutcome.sample_id == WaitSample.id)
        .outerjoin(AttendanceException, and_(
            AttendanceException.sample_id == WaitSample.id,
            AttendanceException.status == AttendanceStatus.CONFIRMED,
        ))
        .where(or_(
            and_(DiversionOutcome.id.is_(None), WaitSample.department_slot_id == slot_id),
            DiversionOutcome.admitted_slot_id == slot_id,
        ))
        .order_by(WaitSample.appointment_time)
    ).all()


def build_report(db: Session, slot: DepartmentSlot, title: str, user: User, status: ReportStatus) -> ReviewReport:
    """在单次 SQL 快照上统计并组装报告，由调用方决定何时提交。"""
    rows = slot_sample_rows(db, slot.id)
    result = calculate_review([sample for sample, attendance in rows if attendance is None], slot)
    report = ReviewReport(
        report_code=f"RPT-{slot.slot_date.strftime('%m%d')}-{secrets.token_hex(2).upper()}",
        title=title,
        department_slot_id=slot.id,
        status=status,
        sample_count=result["sample_count"],
        missing_checkin_count=result["missing_checkin_count"],
        p50_minutes=result["p50_minutes"],
        p90_minutes=result["p90_minutes"],
        median_minutes=result["median_minutes"],
        mad_minutes=result["mad_minutes"],
        anomaly_count=result["anomaly_count"],
        distribution_json=json.dumps(result["distribution"], ensure_ascii=False),
        anomaly_codes_json=json.dumps(result["anomaly_codes"], ensure_ascii=False),
        summary=result["summary"],
        created_by=user.id,
    )
    report.exclusions = [
        ReportAttendanceExclusion(attendance_id=attendance.id, sample_code=sample.sample_code, reason=attendance.reason)
        for sample, attendance in rows if attendance is not None
    ]
    db.add(report)
    db.flush()
    return report


def create_report(db: Session, slot_id: int, title: str, user: User) -> ReviewReport:
    slot = db.get(DepartmentSlot, slot_id)
    if not slot:
        raise HTTPException(status_code=404, detail="科室时段不存在")
    report = build_report(db, slot, title, user, ReportStatus.UNDER_REVIEW)
    db.commit()
    db.refresh(report)
    return report
