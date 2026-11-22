from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import require_roles
from ..database import get_db
from ..models import User, UserRole
from ..operations_schemas import DiversionCreate, DiversionOut
from ..services import diversion

router = APIRouter(tags=['分流转归'])
read_roles = require_roles(UserRole.ANALYST, UserRole.DEPARTMENT_REVIEWER, UserRole.OPERATIONS_ADMIN)
reviewer_role = require_roles(UserRole.DEPARTMENT_REVIEWER)
publish_roles = require_roles(UserRole.DEPARTMENT_REVIEWER, UserRole.OPERATIONS_ADMIN)


@router.get('/diversions', response_model=list[DiversionOut])
def list_diversions(db: Session = Depends(get_db), user: User = Depends(read_roles)):
    return diversion.diversion_rows(db)


@router.post('/queue-recoveries/{recovery_id}/diversion', response_model=DiversionOut, status_code=201)
def confirm(recovery_id: int, payload: DiversionCreate, db: Session = Depends(get_db), user: User = Depends(reviewer_role)):
    return diversion.confirm_diversion(db, recovery_id, payload, user)


@router.post('/diversions/{outcome_id}/publish', response_model=DiversionOut)
def publish(outcome_id: int, db: Session = Depends(get_db), user: User = Depends(publish_roles)):
    return diversion.publish_diversion(db, outcome_id, user)
