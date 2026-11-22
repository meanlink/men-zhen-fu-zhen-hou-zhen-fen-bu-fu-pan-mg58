from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..diversion_models import DiversionOutcome
from ..models import DepartmentSlot, ReportStatus, ReviewReport, SampleStatus, User, WaitSample
from .attendance import ensure_no_attendance_hold
from .diversion import autopublish_for_sample
from .recovery import capture_recovery_call
from .reporting import create_report  # noqa: F401  (re-exported for routes)


def record_sample_event(db: Session, sample: WaitSample, event: str, event_time: datetime, user: User, note: str | None = None):
    ensure_no_attendance_hold(db, sample.id)
    if event == "checkin":
        if sample.status not in (SampleStatus.BOOKED, SampleStatus.NEEDS_REVIEW):
            raise HTTPException(status_code=409, detail="当前样本不能重复签到")
        if event_time < sample.appointment_time.replace(tzinfo=None):
            raise HTTPException(status_code=409, detail="签到时间不能早于预约时间")
        sample.checkin_time = event_time
        sample.status = SampleStatus.CHECKED_IN
    elif event == "call":
        capture_recovery_call(db, sample.id, event_time, user)
        if not sample.checkin_time:
            raise HTTPException(status_code=409, detail="缺少签到时间，不能叫号")
        if event_time < sample.checkin_time:
            raise HTTPException(status_code=409, detail="叫号时间不能早于签到时间")
        sample.called_time = event_time
        sample.status = SampleStatus.CALLED
    elif event == "complete":
        if not sample.called_time:
            raise HTTPException(status_code=409, detail="缺少叫号时间，不能完成接诊")
        if event_time < sample.called_time:
            raise HTTPException(status_code=409, detail="完成时间不能早于叫号时间")
        sample.completed_time = event_time
        sample.status = SampleStatus.COMPLETED
    else:
        raise HTTPException(status_code=400, detail="未知样本事件")
    if note:
        sample.note = note
    db.commit()
    db.refresh(sample)
    if event == "call":
        # 分流样本叫号后候诊记录完整，若两科均已归档可重算发布。
        autopublish_for_sample(db, sample.id, user)
    return sample


def review_report(db: Session, report: ReviewReport, user: User, note: str):
    if report.status != ReportStatus.UNDER_REVIEW:
        raise HTTPException(status_code=409, detail="只有待科室复核的报告才能复核")
    if report.missing_checkin_count > 0:
        raise HTTPException(status_code=409, detail="存在缺失签到时间的样本，修正后才能发布")
    report.status = ReportStatus.REVIEWED
    report.reviewed_by = user.id
    report.reviewed_at = datetime.utcnow()
    report.review_note = note
    db.commit()
    db.refresh(report)
    return report


def archive_report(db: Session, report: ReviewReport, user: User):
    if report.status != ReportStatus.REVIEWED:
        raise HTTPException(status_code=409, detail="报告需完成科室复核后才能归档")
    if report.missing_checkin_count > 0:
        raise HTTPException(status_code=409, detail="存在缺失签到时间的样本，禁止发布")
    # Lock the slot so archival serializes against diversion confirmation.
    slot = db.get(DepartmentSlot, report.department_slot_id, with_for_update=True, populate_existing=True)
    stale = db.scalar(select(DiversionOutcome.id).where(
        or_(DiversionOutcome.origin_slot_id == slot.id, DiversionOutcome.admitted_slot_id == slot.id),
        DiversionOutcome.confirmed_at > report.created_at,
    ).limit(1))
    if stale is not None:
        raise HTTPException(status_code=409, detail="该时段分布已因分流转归确认而过时，请重新生成报告后再归档")
    report.status = ReportStatus.ARCHIVED
    report.archived_at = datetime.utcnow()
    db.commit()
    db.refresh(report)
    return report
