import pytest

from app.logging_utils import configure_refresh_logging


@pytest.fixture(autouse=True, scope="session")
def _install_refresh_log_record_factory():
    configure_refresh_logging()
