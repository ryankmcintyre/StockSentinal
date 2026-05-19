"""Tests for refresh route and refresh-status API endpoint."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_authenticated_uow, get_optional_uow, get_uow
from app.main import _market_service, app
from app.models import Base, Position, StrategyRuleConfig, User
from app.unit_of_work import SqlAlchemyUnitOfWork
from tests.csrf_utils import csrf_form_data


@pytest.fixture(autouse=True)
def _setup_db():
    """Use an in-memory SQLite database for each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    @event.listens_for(TestingSession.class_, "before_flush")
    def _assign_test_user_id(session, _flush_context, _instances):
        for obj in session.new:
            if isinstance(obj, User) and not obj.id:
                obj.id = "test-user-id"
            if isinstance(obj, Position) and obj.user_id is None:
                obj.user_id = "test-user-id"
            if isinstance(obj, StrategyRuleConfig) and obj.user_id is None:
                obj.user_id = "test-user-id"

    db = TestingSession()
    db.add(User(id="test-user-id", email="test@example.com", display_name="Test User", created_at=datetime.now()))
    db.commit()
    db.close()

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    def override_get_authenticated_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session, user_id="test-user-id")
        finally:
            session.close()

    def override_get_optional_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session, user_id="test-user-id")
        finally:
            session.close()

    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
    app.dependency_overrides[get_optional_uow] = override_get_optional_uow
    yield TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


class TestRefreshLoadingCues:
    def test_portfolio_shows_refreshing_badge_and_banner(self, client, _setup_db, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        db = _setup_db()
        try:
            db.add(
                Position(
                    ticker="AAPL",
                    company_name="Apple Inc.",
                    cost_basis=100.0,
                    initial_purchase_date=date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=115.0,
                    notes=None,
                    refresh_in_progress=True,
                    refresh_started_at=datetime.now(),
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Refreshing..." in resp.text
        assert "Updating market data — this page will reload when finished." in resp.text
        assert 'data-any-refresh-in-progress="true"' in resp.text
        assert "/static/refresh-status.js" in resp.text
        assert "data-api-submit=\"true\"" in resp.text

    def test_refresh_all_form_warns_before_submit(self, client, _setup_db, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        db = _setup_db()
        try:
            db.add(
                Position(
                    ticker="AAPL",
                    company_name="Apple Inc.",
                    cost_basis=100.0,
                    initial_purchase_date=date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=115.0,
                    notes=None,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")

        assert resp.status_code == 200
        assert (
            'data-confirm-message="Refreshing all market data may take several minutes '
            'if you have many positions. Continue?"'
        ) in resp.text

    def test_refresh_status_endpoint_returns_position_flags(self, client, _setup_db):
        db = _setup_db()
        try:
            db.add_all(
                [
                    Position(
                        ticker="AAPL",
                        company_name="Apple Inc.",
                        cost_basis=100.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=115.0,
                        notes=None,
                        refresh_in_progress=True,
                        refresh_started_at=datetime.now(),
                    ),
                    Position(
                        ticker="MSFT",
                        company_name="Microsoft Corp.",
                        cost_basis=100.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="short-term",
                        current_price=115.0,
                        notes=None,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/refresh-status")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["any_in_progress"] is True
        assert len(payload["positions"]) == 2
        assert any(item["in_progress"] is True for item in payload["positions"])
        assert any(item["started_at"] is not None for item in payload["positions"])

    def test_refresh_status_endpoint_clears_stale_flags(self, client, _setup_db):
        db = _setup_db()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                notes=None,
                refresh_in_progress=True,
                refresh_started_at=datetime.now() - timedelta(minutes=6),
            )
            db.add(pos)
            db.commit()
            position_id = pos.id
        finally:
            db.close()

        resp = client.get("/api/refresh-status")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["any_in_progress"] is False

        verify_db = _setup_db()
        try:
            refreshed = verify_db.query(Position).filter(Position.id == position_id).first()
            assert refreshed is not None
            assert refreshed.refresh_in_progress is False
            assert refreshed.refresh_started_at is None
        finally:
            verify_db.close()

    def test_startup_stale_cleanup_clears_positions_for_all_users(
        self, _setup_db, mocker
    ):
        from app.main import _clear_all_stale_refresh_flags

        db = _setup_db()
        try:
            db.add_all(
                [
                    User(
                        id="alice-user-id",
                        email="alice@example.com",
                        display_name="Alice",
                        created_at=datetime.now(),
                    ),
                    User(
                        id="bob-user-id",
                        email="bob@example.com",
                        display_name="Bob",
                        created_at=datetime.now(),
                    ),
                    Position(
                        ticker="AAPL",
                        company_name="Apple Inc.",
                        cost_basis=100.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=115.0,
                        user_id="alice-user-id",
                        refresh_in_progress=True,
                        refresh_started_at=datetime.now() - timedelta(minutes=6),
                    ),
                    Position(
                        ticker="MSFT",
                        company_name="Microsoft Corp.",
                        cost_basis=200.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=215.0,
                        user_id="bob-user-id",
                        refresh_in_progress=True,
                        refresh_started_at=datetime.now() - timedelta(minutes=6),
                    ),
                    Position(
                        ticker="NVDA",
                        company_name="NVIDIA Corp.",
                        cost_basis=300.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=315.0,
                        user_id="test-user-id",
                        refresh_in_progress=True,
                        refresh_started_at=datetime.now(),
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        mocker.patch("app.main.engine", _setup_db.kw["bind"])

        assert _clear_all_stale_refresh_flags() == 2

        verify_db = _setup_db()
        try:
            positions = {pos.ticker: pos for pos in verify_db.query(Position).all()}
            assert positions["AAPL"].refresh_in_progress is False
            assert positions["AAPL"].refresh_started_at is None
            assert positions["MSFT"].refresh_in_progress is False
            assert positions["MSFT"].refresh_started_at is None
            assert positions["NVDA"].refresh_in_progress is True
            assert positions["NVDA"].refresh_started_at is not None
        finally:
            verify_db.close()

    def test_startup_stale_cleanup_uses_unscoped_connection(self, mocker):
        from app.main import _clear_all_stale_refresh_flags

        engine = mocker.Mock()
        connection = mocker.Mock()
        begin_context = mocker.MagicMock()
        begin_context.__enter__.return_value = connection
        engine.begin.return_value = begin_context
        connection.execute.return_value = mocker.Mock(rowcount=3)
        mocker.patch("app.main.engine", engine)

        assert _clear_all_stale_refresh_flags() == 3

        engine.begin.assert_called_once()
        connection.execute.assert_called_once()
        assert "UPDATE positions" in str(connection.execute.call_args.args[0])

    def test_single_refresh_route_noops_when_already_in_progress(
        self, client, _setup_db, mocker
    ):
        db = _setup_db()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                notes=None,
                refresh_in_progress=True,
                refresh_started_at=datetime.now(),
            )
            db.add(pos)
            db.commit()
            position_id = pos.id
        finally:
            db.close()

        mock_refresh = mocker.patch.object(_market_service, "refresh_position")
        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        mock_refresh.assert_not_called()

    def test_refresh_all_marks_positions_in_progress_before_background(
        self, client, _setup_db, mocker
    ):
        db = _setup_db()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                notes=None,
            )
            db.add(pos)
            db.commit()
            position_id = pos.id
        finally:
            db.close()

        mock_refresh_all = mocker.patch("app.main._refresh_all_positions_task", return_value=None)
        resp = client.post("/refresh", data=csrf_form_data(client), follow_redirects=False)
        assert resp.status_code == 303
        mock_refresh_all.assert_called_once_with([position_id], "test-user-id")

        verify_db = _setup_db()
        try:
            pos = verify_db.query(Position).first()
            assert pos is not None
            assert pos.refresh_in_progress is True
            assert pos.refresh_started_at is not None
        finally:
            verify_db.close()

    def test_refresh_all_background_task_uses_user_scoped_uow(self, mocker):
        from app.main import _refresh_all_positions_task

        created_user_ids = []
        expected_position_ids = [123]
        session = mocker.Mock()

        class FakePositions:
            def get_by_ids(self, position_ids):
                assert position_ids == expected_position_ids
                return []

        class FakeUow:
            def __init__(self, _session, user_id=None):
                created_user_ids.append(user_id)
                self.session = session
                self.positions = FakePositions()

            def commit(self):
                pass

            def rollback(self):
                pass

        mocker.patch("app.main.SessionLocal", return_value=session)
        mocker.patch("app.main.SqlAlchemyUnitOfWork", FakeUow)
        refresh_all = mocker.patch.object(
            _market_service,
            "refresh_all_positions",
            return_value=0,
        )

        _refresh_all_positions_task(expected_position_ids, "test-user-id")

        assert created_user_ids == ["test-user-id"]
        refresh_all.assert_called_once_with(session, user_id="test-user-id")
        session.close.assert_called_once()


class TestRefreshTierLimits:
    def _seed_position(self, session_maker) -> int:
        db = session_maker()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                notes=None,
            )
            db.add(pos)
            db.commit()
            return pos.id
        finally:
            db.close()

    def _set_refresh_count(self, session_maker, count: int, tier: str = "free"):
        db = session_maker()
        try:
            user = db.query(User).filter(User.id == "test-user-id").one()
            user.tier = tier
            user.refresh_count_today = count
            user.refresh_count_date = datetime.now(timezone.utc).date()
            db.commit()
        finally:
            db.close()

    def test_sixth_refresh_all_is_blocked_with_banner(self, client, _setup_db, mocker):
        self._seed_position(_setup_db)
        self._set_refresh_count(_setup_db, 5)
        mock_refresh_all = mocker.patch("app.main._refresh_all_positions_task", return_value=None)

        resp = client.post("/refresh", data=csrf_form_data(client), follow_redirects=True)

        assert resp.status_code == 200
        assert "used 5 of 5 refreshes today" in resp.text
        mock_refresh_all.assert_not_called()

    def test_single_refresh_consumes_one_refresh(self, client, _setup_db, mocker):
        position_id = self._seed_position(_setup_db)
        self._set_refresh_count(_setup_db, 0)
        mocker.patch.object(_market_service, "refresh_position", return_value=None)

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        db = _setup_db()
        try:
            user = db.query(User).filter(User.id == "test-user-id").one()
            assert user.refresh_count_today == 1
        finally:
            db.close()

    def test_full_access_refresh_bypasses_limit(self, client, _setup_db, mocker):
        self._seed_position(_setup_db)
        self._set_refresh_count(_setup_db, 5, tier="full_access")
        mock_refresh_all = mocker.patch("app.main._refresh_all_positions_task", return_value=None)

        resp = client.post("/refresh", data=csrf_form_data(client), follow_redirects=False)

        assert resp.status_code == 303
        mock_refresh_all.assert_called_once()
