"""Tests for refresh route and refresh-status API endpoint."""

import asyncio
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

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
    db.add(User(id="test-user-id", email="test@example.com", display_name="Test User", created_at=datetime.now(timezone.utc)))
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
    def test_portfolio_marks_refresh_button_spinning_and_banner(self, client, _setup_db, mocker):
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
                    refresh_started_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        # No "Refreshing..." badge in the Reason column anymore; the spinning
        # refresh icon is the only in-progress cue.
        assert "Refreshing..." not in resp.text
        assert "btn-refresh-spinning" in resp.text
        assert "Updating market data — rows will update automatically when finished." in resp.text
        assert 'data-any-refresh-in-progress="true"' in resp.text
        assert "data-poll-timeout-ms=" in resp.text
        assert "/static/refresh-status.js" in resp.text
        assert 'data-refresh-form="true"' in resp.text

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

    def test_portfolio_shows_previous_verdict_below_current_verdict(
        self, client, _setup_db
    ):
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
                    previous_verdict="Hold",
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")

        assert resp.status_code == 200
        assert "Previously Hold" in resp.text

    def test_refresh_status_endpoint_returns_position_flags(self, client, _setup_db, mocker):
        list_all = mocker.patch(
            "app.repositories.SqlAlchemyPositionRepository.list_all",
            side_effect=AssertionError("refresh-status must not load full positions"),
        )

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
                        refresh_started_at=datetime.now(timezone.utc),
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
        list_all.assert_not_called()

    def test_refresh_status_endpoint_clears_stale_flags(self, client, _setup_db, mocker):
        list_stale = mocker.patch(
            "app.repositories.SqlAlchemyPositionRepository.list_stale_refreshing",
            side_effect=AssertionError("stale refresh cleanup must not load full positions"),
        )

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
                refresh_started_at=datetime.now(timezone.utc) - timedelta(minutes=6),
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
        list_stale.assert_not_called()

        verify_db = _setup_db()
        try:
            refreshed = verify_db.query(Position).filter(Position.id == position_id).first()
            assert refreshed is not None
            assert refreshed.refresh_in_progress is False
            assert refreshed.refresh_started_at is None
        finally:
            verify_db.close()

    def test_refresh_status_endpoint_scopes_to_requested_ids(self, client, _setup_db):
        db = _setup_db()
        try:
            refreshing = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                notes=None,
                refresh_in_progress=True,
                refresh_started_at=datetime.now(timezone.utc),
            )
            idle = Position(
                ticker="MSFT",
                company_name="Microsoft Corp.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="short-term",
                current_price=115.0,
                notes=None,
            )
            db.add_all([refreshing, idle])
            db.commit()
            refreshing_id = refreshing.id
            idle_id = idle.id
        finally:
            db.close()

        # Only the idle position is requested -> nothing in progress.
        resp = client.get(f"/api/refresh-status?ids={idle_id}")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["any_in_progress"] is False
        assert [item["id"] for item in payload["positions"]] == [idle_id]

        # Only the refreshing position is requested -> in progress.
        resp = client.get(f"/api/refresh-status?ids={refreshing_id}")
        payload = resp.json()
        assert payload["any_in_progress"] is True
        assert [item["id"] for item in payload["positions"]] == [refreshing_id]

    def test_refresh_status_endpoint_ignores_invalid_ids(self, client, _setup_db):
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
            pos_id = pos.id
        finally:
            db.close()

        resp = client.get(f"/api/refresh-status?ids=abc,{pos_id},")
        assert resp.status_code == 200
        payload = resp.json()
        assert [item["id"] for item in payload["positions"]] == [pos_id]

    def test_position_row_endpoint_returns_fragment_and_summary(self, client, _setup_db, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
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
            pos_id = pos.id
        finally:
            db.close()

        resp = client.get(f"/api/positions/{pos_id}/row")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["id"] == pos_id
        assert payload["in_progress"] is False
        assert f'data-position-id="{pos_id}"' in payload["row_html"]
        assert "AAPL" in payload["row_html"]
        assert set(payload["summary"]) == {"sell", "trim", "hold", "total"}
        assert payload["summary"]["total"] == 1

    def test_position_row_endpoint_returns_404_for_unknown_position(self, client, _setup_db):
        resp = client.get("/api/positions/999999/row")
        assert resp.status_code == 404

    def test_position_rows_batch_returns_rows_and_single_summary(
        self, client, _setup_db, mocker
    ):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        import app.main as main_module

        enrich_spy = mocker.spy(main_module, "_enrich_all_positions")
        db = _setup_db()
        try:
            ids = []
            for ticker in ("AAPL", "MSFT", "GOOG"):
                pos = Position(
                    ticker=ticker,
                    company_name=f"{ticker} Inc.",
                    cost_basis=100.0,
                    initial_purchase_date=date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=115.0,
                    notes=None,
                )
                db.add(pos)
                db.commit()
                ids.append(pos.id)
        finally:
            db.close()

        resp = client.get(
            "/api/positions/rows?ids=" + ",".join(str(i) for i in ids)
        )
        assert resp.status_code == 200
        payload = resp.json()
        # One authoritative summary is returned for the whole batch.
        assert set(payload["summary"]) == {"sell", "trim", "hold", "total"}
        assert payload["summary"]["total"] == 3
        returned_ids = [row["id"] for row in payload["rows"]]
        assert sorted(returned_ids) == sorted(ids)
        for row in payload["rows"]:
            assert f'data-position-id="{row["id"]}"' in row["row_html"]
        # Enriching happens exactly once regardless of how many rows requested,
        # so high-cardinality completion does not fan out into N enrich-all ops.
        assert enrich_spy.call_count == 1

    def test_position_rows_batch_dedupes_and_skips_unknown_ids(
        self, client, _setup_db, mocker
    ):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
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
            pos_id = pos.id
        finally:
            db.close()

        resp = client.get(
            f"/api/positions/rows?ids={pos_id},{pos_id},abc,999999,"
        )
        assert resp.status_code == 200
        payload = resp.json()
        # Duplicate id collapses to one row; unknown/invalid ids are skipped.
        assert [row["id"] for row in payload["rows"]] == [pos_id]

    def test_position_rows_batch_empty_ids_returns_summary_only(
        self, client, _setup_db, mocker
    ):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        resp = client.get("/api/positions/rows")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["rows"] == []
        assert set(payload["summary"]) == {"sell", "trim", "hold", "total"}

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
                        created_at=datetime.now(timezone.utc),
                    ),
                    User(
                        id="bob-user-id",
                        email="bob@example.com",
                        display_name="Bob",
                        created_at=datetime.now(timezone.utc),
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
                        refresh_started_at=datetime.now(timezone.utc) - timedelta(minutes=6),
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
                        refresh_started_at=datetime.now(timezone.utc) - timedelta(minutes=6),
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
                        refresh_started_at=datetime.now(timezone.utc),
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
                refresh_started_at=datetime.now(timezone.utc),
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
        mock_refresh_all.assert_called_once()
        assert mock_refresh_all.call_args.args[:2] == ([position_id], "test-user-id")
        assert mock_refresh_all.call_args.args[2].startswith("refresh-")

        verify_db = _setup_db()
        try:
            pos = verify_db.query(Position).first()
            assert pos is not None
            assert pos.refresh_in_progress is True
            assert pos.refresh_started_at is not None
        finally:
            verify_db.close()

    def test_single_refresh_returns_redirect_before_background_task_completes(
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

        response_sent = threading.Event()
        task_started = threading.Event()
        allow_task_to_finish = threading.Event()
        request_errors = []
        messages = []

        def blocking_refresh_single(_position_id, _user_id, _refresh_id):
            task_started.set()
            assert response_sent.is_set()
            assert allow_task_to_finish.wait(timeout=1)

        mock_refresh_single = mocker.patch(
            "app.main._refresh_single_position_task",
            side_effect=blocking_refresh_single,
        )

        form_data = csrf_form_data(client)
        body = urlencode(form_data).encode()
        cookie_header = "; ".join(
            f"{key}={value}" for key, value in client.cookies.items()
        ).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/refresh/{position_id}",
            "raw_path": f"/refresh/{position_id}".encode(),
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
                (b"cookie", cookie_header),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
        request_messages = iter(
            (
                {"type": "http.request", "body": body, "more_body": False},
                {"type": "http.disconnect"},
            )
        )

        async def receive():
            return next(request_messages)

        async def send(message):
            messages.append(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                response_sent.set()

        def run_request():
            try:
                asyncio.run(app(scope, receive, send))
            except Exception as exc:  # pragma: no cover - assertion surfaced below
                request_errors.append(exc)

        request_thread = threading.Thread(target=run_request)
        request_thread.start()

        assert response_sent.wait(timeout=1)
        assert task_started.wait(timeout=1)
        allow_task_to_finish.set()
        request_thread.join(timeout=1)

        assert not request_thread.is_alive()
        assert not request_errors
        mock_refresh_single.assert_called_once()
        assert mock_refresh_single.call_args.args[:2] == (position_id, "test-user-id")
        assert mock_refresh_single.call_args.args[2].startswith("refresh-")
        response_start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 303
        assert dict(response_start["headers"])[b"location"] == b"/"

        verify_db = _setup_db()
        try:
            pos = verify_db.query(Position).filter(Position.id == position_id).first()
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

        _refresh_all_positions_task(expected_position_ids, "test-user-id", "refresh-test")

        assert created_user_ids == ["test-user-id"]
        refresh_all.assert_called_once_with(session, user_id="test-user-id")
        session.close.assert_called_once()


class TestRefreshRouteTierLimits:
    def _seed_single_position(self, session_maker) -> int:
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
        self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 5)
        mock_refresh_all = mocker.patch("app.main._refresh_all_positions_task", return_value=None)

        resp = client.post("/refresh", data=csrf_form_data(client), follow_redirects=True)

        assert resp.status_code == 200
        assert "used 5 of 5 refreshes today" in resp.text
        mock_refresh_all.assert_not_called()

    def test_single_refresh_consumes_one_refresh(self, client, _setup_db, mocker):
        position_id = self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 0)
        mock_refresh_single = mocker.patch(
            "app.main._refresh_single_position_task", return_value=None
        )

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        mock_refresh_single.assert_called_once()
        assert mock_refresh_single.call_args.args[:2] == (position_id, "test-user-id")
        assert mock_refresh_single.call_args.args[2].startswith("refresh-")
        db = _setup_db()
        try:
            user = db.query(User).filter(User.id == "test-user-id").one()
            assert user.refresh_count_today == 1
        finally:
            db.close()

    def test_full_access_refresh_bypasses_limit(self, client, _setup_db, mocker):
        self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 5, tier="full_access")
        mock_refresh_all = mocker.patch("app.main._refresh_all_positions_task", return_value=None)

        resp = client.post("/refresh", data=csrf_form_data(client), follow_redirects=False)

        assert resp.status_code == 303
        mock_refresh_all.assert_called_once()

    def test_single_refresh_returns_json_202_for_fetch_request(
        self, client, _setup_db, mocker
    ):
        position_id = self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 0)
        mock_refresh_single = mocker.patch(
            "app.main._refresh_single_position_task", return_value=None
        )

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

        assert resp.status_code == 202
        assert resp.json() == {"status": "started", "id": position_id}
        mock_refresh_single.assert_called_once()
        assert mock_refresh_single.call_args.args[:2] == (position_id, "test-user-id")
        assert mock_refresh_single.call_args.args[2].startswith("refresh-")

    def test_single_refresh_returns_json_202_for_x_requested_with_fetch(
        self, client, _setup_db, mocker
    ):
        position_id = self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 0)
        mocker.patch("app.main._refresh_single_position_task", return_value=None)

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            headers={"X-Requested-With": "fetch"},
            follow_redirects=False,
        )

        assert resp.status_code == 202
        assert resp.json()["status"] == "started"

    def test_single_refresh_returns_303_for_standard_form_post(
        self, client, _setup_db, mocker
    ):
        position_id = self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 0)
        mocker.patch("app.main._refresh_single_position_task", return_value=None)

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )

        assert resp.status_code == 303

    def test_single_refresh_fetch_over_limit_returns_429_json(
        self, client, _setup_db, mocker
    ):
        position_id = self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 5)
        mock_refresh_single = mocker.patch(
            "app.main._refresh_single_position_task", return_value=None
        )

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

        assert resp.status_code == 429
        payload = resp.json()
        assert payload["status"] == "limit"
        assert "used 5 of 5 refreshes today" in payload["flash"]
        mock_refresh_single.assert_not_called()

    def test_single_refresh_form_over_limit_redirects(
        self, client, _setup_db, mocker
    ):
        position_id = self._seed_single_position(_setup_db)
        self._set_refresh_count(_setup_db, 5)
        mocker.patch("app.main._refresh_single_position_task", return_value=None)

        resp = client.post(
            f"/refresh/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/?flash=refresh_limit"

    def test_refresh_status_js_uses_delegated_submit_for_patched_rows(self):
        # patchRows() replaces the whole <tr> (including its refresh form), so
        # the async path must be wired via a single delegated submit listener
        # on document rather than per-form binding. Otherwise a replaced row
        # would fall back to a native POST + full page reload on its next
        # refresh. Assert the delegation pattern is present and that we no
        # longer bind submit handlers per refresh form.
        from pathlib import Path

        source = Path("app/static/refresh-status.js").read_text(encoding="utf-8")
        assert 'document.addEventListener("submit"' in source
        assert "data-refresh-form='true'" in source
        assert (
            'querySelectorAll("form[data-refresh-form=\'true\']")'
            not in source
        )
