from datetime import datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime, Enum as SqlEnum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class DiversionStatus(str, Enum):
    CONFIRMED = 'confirmed'
    PUBLISHED = 'published'
    FAILED = 'failed'


class DiversionOutcome(Base):
    __tablename__ = 'diversion_outcomes'
    __table_args__ = (
        CheckConstraint(
            "(status = 'PUBLISHED') = (origin_report_id IS NOT NULL AND admitted_report_id IS NOT NULL "
            'AND published_at IS NOT NULL)',
            name='ck_diversion_publish',
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    recovery_id: Mapped[int] = mapped_column(ForeignKey('queue_recoveries.id'), unique=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey('wait_samples.id'), unique=True)
    origin_slot_id: Mapped[int] = mapped_column(ForeignKey('department_slots.id'), index=True)
    admitted_slot_id: Mapped[int] = mapped_column(ForeignKey('department_slots.id'), index=True)
    status: Mapped[DiversionStatus] = mapped_column(SqlEnum(DiversionStatus, name='diversion_status'))
    note: Mapped[str] = mapped_column(String(500))
    confirmed_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    prev_origin_report_id: Mapped[int | None] = mapped_column(ForeignKey('review_reports.id'))
    prev_admitted_report_id: Mapped[int | None] = mapped_column(ForeignKey('review_reports.id'))
    origin_report_id: Mapped[int | None] = mapped_column(ForeignKey('review_reports.id'))
    admitted_report_id: Mapped[int | None] = mapped_column(ForeignKey('review_reports.id'))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    publish_attempts: Mapped[int] = mapped_column(default=0)
