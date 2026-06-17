import pytest

from app.logging_utils import configure_refresh_logging
from app.rule_config import clear_seeded_users_cache


@pytest.fixture(autouse=True, scope="session")
def _install_refresh_log_record_factory():
    configure_refresh_logging()


@pytest.fixture(autouse=True)
def _clear_rule_config_seed_cache():
    """Reset the module-level seeded-users cache before each test.

    Each test gets a fresh in-memory database, so the seeded-users cache
    (which persists for the process lifetime) must be cleared to ensure
    ensure_strategy_rule_defaults re-seeds on the first call.
    """
    clear_seeded_users_cache()
    yield
