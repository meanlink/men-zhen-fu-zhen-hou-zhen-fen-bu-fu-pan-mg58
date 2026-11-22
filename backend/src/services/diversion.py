from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..diversion_models import DiversionOutcome, DiversionStatus
from ..models import DepartmentSlot, ReportStatus, ReviewReport, User
from ..operations_schemas import DiversionCreate
from ..recovery_models import QueueRecovery, RecoveryStatus
from .attendance import locked_sample
from .reporting import build_report


def latest_archived_report(db: Session, slot_id: int) -> ReviewReport | None:
    return db.scalar(select(ReviewReport).where(
        ReviewReport.department_slot_id == slot_id,
        ReviewReport.status == ReportStatus.ARCHIVED,
    ).order_by(ReviewReport.archived_at.desc(), ReviewReport.id.desc()).limit(1))


def diversion_rows(db: Session) -> list[DiversionOutcome]:
    return list(db.scalars(select(DiversionOutcome).order_by(DiversionOutcome.confirmed_at.desc())))


def confirm_diversion(db: Session, recovery_id: int, payload: DiversionCreate, user: User) -> DiversionOutcome:
    sample_id = db.scalar(select(QueueRecovery.sample_id).where(QueueRecovery.id == recovery_id))
    if sample_id is None:
        raise HTTPException(404, '超时处置单不存在')
    # Sample events and recovery transitions always acquire the sample lock first.
    sample = locked_sample(db, sample_id)
    recovery = db.get(QueueRecovery, recovery_id, populate_existing=True)
    if recovery.status != RecoveryStatus.PENDING or recovery.opened_by == user.id:
        raise HTTPException(409, '只有独立复核员可以分流待处置单')
    admitted_slot = db.get(DepartmentSlot, payload.admitted_slot_id)
    if not admitted_slot:
        raise HTTPException(404, '接诊时段不存在')
    origin_slot_id = sample.department_slot_id
    if admitted_slot.id == origin_slot_id:
        raise HTTPException(409, '接诊时段不能是样本原时段')
    # Lock both slots in id order so archival and diversion confirmation serialize without deadlock.
    for slot_id in sorted({origin_slot_id, admitted_slot.id}):
        db.get(DepartmentSlot, slot_id, with_for_update=True, populate_existing=True)
    if admitted_slot.status != 'open':
        raise HTTPException(409, '接诊时段已关闭，不能接收分流')
    recovery.status = RecoveryStatus.DIVERTED
    recovery.decided_by = user.id
    recovery.decided_at = datetime.utcnow()
    recovery.decision_note = payload.note
    outcome = DiversionOutcome(
        recovery_id=recovery.id, sample_id=sample.id,
        origin_slot_id=origin_slot_id, admitted_slot_id=admitted_slot.id,
        status=DiversionStatus.CONFIRMED, note=payload.note,
        confirmed_by=user.id, confirmed_at=datetime.utcnow(),
    )
    db.add(outcome)
    db.commit()
    db.refresh(outcome)
    try_autopublish(db, outcome.id, user)
    db.refresh(outcome)
    return outcome


def rebuild_report(db: Session, prev_report: ReviewReport, slot: DepartmentSlot, user: User) -> ReviewReport:
    report = build_report(db, slot, f'{prev_report.title} · 分流重算', user, ReportStatus.ARCHIVED)
    if report.missing_checkin_count > 0:
        raise HTTPException(409, f'{slot.department_name}重算后仍有样本缺签到或叫号，两科保留旧版报告')
    report.reviewed_by = user.id
    report.reviewed_at = datetime.utcnow()
    report.review_note = f'分流转归重算发布（原报告 {prev_report.report_code} 留档）'
    report.archived_at = datetime.utcnow()
    db.flush()
    return report


def publish_diversion(db: Session, outcome_id: int, user: User) -> DiversionOutcome:
    """两科均已归档时把两份重算报告一起发布；失败整体回滚，重试幂等。"""
    outcome = db.get(DiversionOutcome, outcome_id, with_for_update=True, populate_existing=True)
    if not outcome:
        raise HTTPException(404, '转归记录不存在')
    if outcome.status == DiversionStatus.PUBLISHED:
        return outcome
    prev_origin = latest_archived_report(db, outcome.origin_slot_id)
    prev_admitted = latest_archived_report(db, outcome.admitted_slot_id)
    if prev_origin is None or prev_admitted is None:
        raise HTTPException(409, '两科时段均需已有归档报告才能重算发布')
    prev_origin_id, prev_admitted_id = prev_origin.id, prev_admitted.id
    try:
        outcome.prev_origin_report_id = prev_origin_id
        outcome.prev_admitted_report_id = prev_admitted_id
        outcome.publish_attempts += 1
        origin_slot = db.get(DepartmentSlot, outcome.origin_slot_id)
        admitted_slot = db.get(DepartmentSlot, outcome.admitted_slot_id)
        origin_report = rebuild_report(db, prev_origin, origin_slot, user)
        admitted_report = rebuild_report(db, prev_admitted, admitted_slot, user)
        outcome.origin_report_id = origin_report.id
        outcome.admitted_report_id = admitted_report.id
        outcome.status = DiversionStatus.PUBLISHED
        outcome.published_at = datetime.utcnow()
        # 行锁持有到提交，两份重算报告与状态原子生效。
        db.commit()
    except Exception:
        db.rollback()
        failed = db.get(DiversionOutcome, outcome_id, with_for_update=True)
        failed.prev_origin_report_id = prev_origin_id
        failed.prev_admitted_report_id = prev_admitted_id
        failed.publish_attempts += 1
        failed.status = DiversionStatus.FAILED
        db.commit()
        raise
    db.refresh(outcome)
    return outcome


def try_autopublish(db: Session, outcome_id: int, user: User) -> None:
    """两科均已归档时自动重算发布；失败仅落状态，不打断主流程。"""
    outcome = db.get(DiversionOutcome, outcome_id)
    if not outcome or outcome.status == DiversionStatus.PUBLISHED:
        return
    if latest_archived_report(db, outcome.origin_slot_id) is None or latest_archived_report(db, outcome.admitted_slot_id) is None:
        return
    try:
        publish_diversion(db, outcome_id, user)
    except Exception:
        db.rollback()


def autopublish_for_sample(db: Session, sample_id: int, user: User) -> None:
    outcome_id = db.scalar(select(DiversionOutcome.id).where(DiversionOutcome.sample_id == sample_id))
    if outcome_id is not None:
        try_autopublish(db, outcome_id, user)
