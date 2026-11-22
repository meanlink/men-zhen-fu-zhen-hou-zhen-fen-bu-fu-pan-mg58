import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ['DATABASE_URL'] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
os.environ['REDIS_URL'] = 'redis://localhost:6399/0'

import pytest
from fastapi.testclient import TestClient

from src.auth import hash_password
from src.database import Base, SessionLocal, engine, init_db
from src.main import app
from src.models import User, UserRole


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    session = SessionLocal()
    for username, display, role in [
        ('analyst', '林晓岚', UserRole.ANALYST),
        ('reviewer', '周医生', UserRole.DEPARTMENT_REVIEWER),
        ('admin', '顾运营', UserRole.OPERATIONS_ADMIN),
    ]:
        session.add(User(username=username, display_name=display, role=role,
                         password_hash=hash_password('demo123')))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    return TestClient(app)
