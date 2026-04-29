"""Tests for the key-level CRUD routes (issue #23)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_uow
from app.main import app
from app.models import Base, Position, PositionKeyLevel
from app.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture(autouse=True)
def _setup_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    app.dependency_overrides[get_uow] = override_get_uow
    yield TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


def _seed_position(SessionMaker) -> int:
    db = SessionMaker()
    try:
        pos = Position(
            ticker="NVDA",
            company_name="NVIDIA Corp",
            cost_basis=100.0,
            initial_purchase_date=date(2024, 1, 1),
            investment_type="long-term",
            current_price=150.0,
        )
        db.add(pos)
        db.commit()
        return pos.id
    finally:
        db.close()


class TestKeyLevelRoutes:
    def test_add_key_level(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        resp = client.post(
            f"/edit/{pos_id}/key-levels/add",
            data={"level_price": "120.5", "label": "2024 high", "notes": "from chart"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/edit/{pos_id}"

        db = _setup_db()
        try:
            kls = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.position_id == pos_id
            ).all()
            assert len(kls) == 1
            assert kls[0].level_price == 120.5
            assert kls[0].label == "2024 high"
            assert kls[0].notes == "from chart"
            assert kls[0].is_active is True
        finally:
            db.close()

    def test_add_rejects_non_positive_price(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        resp = client.post(
            f"/edit/{pos_id}/key-levels/add",
            data={"level_price": "0", "label": "bad"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = _setup_db()
        try:
            kls = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.position_id == pos_id
            ).all()
            assert kls == []
        finally:
            db.close()

    def test_add_to_unknown_position_redirects_to_root(self, _setup_db, client):
        resp = client.post(
            "/edit/99999/key-levels/add",
            data={"level_price": "100"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_delete_key_level(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            kl = PositionKeyLevel(position_id=pos_id, level_price=100.0)
            db.add(kl)
            db.commit()
            kl_id = kl.id
        finally:
            db.close()

        resp = client.post(
            f"/edit/{pos_id}/key-levels/{kl_id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            assert db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first() is None
        finally:
            db.close()

    def test_toggle_key_level_active(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            kl = PositionKeyLevel(position_id=pos_id, level_price=100.0, is_active=True)
            db.add(kl)
            db.commit()
            kl_id = kl.id
        finally:
            db.close()

        client.post(f"/edit/{pos_id}/key-levels/{kl_id}/toggle", follow_redirects=False)

        db = _setup_db()
        try:
            kl = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first()
            assert kl.is_active is False
        finally:
            db.close()

        client.post(f"/edit/{pos_id}/key-levels/{kl_id}/toggle", follow_redirects=False)

        db = _setup_db()
        try:
            kl = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first()
            assert kl.is_active is True
        finally:
            db.close()

    def test_delete_position_cascades_to_key_levels(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            db.add_all([
                PositionKeyLevel(position_id=pos_id, level_price=100.0),
                PositionKeyLevel(position_id=pos_id, level_price=120.0),
            ])
            db.commit()
        finally:
            db.close()

        client.post(f"/delete/{pos_id}", follow_redirects=False)

        db = _setup_db()
        try:
            assert db.query(PositionKeyLevel).filter(
                PositionKeyLevel.position_id == pos_id
            ).all() == []
        finally:
            db.close()

    def test_edit_position_renders_key_levels_section(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            db.add(PositionKeyLevel(position_id=pos_id, level_price=120.0, label="LTH"))
            db.commit()
        finally:
            db.close()

        resp = client.get(f"/edit/{pos_id}")
        assert resp.status_code == 200
        assert "Key Levels" in resp.text
        assert "120.00" in resp.text
        assert "LTH" in resp.text
