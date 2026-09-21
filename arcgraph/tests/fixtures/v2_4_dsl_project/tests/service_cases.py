import pytest

from pkg.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.mark.parametrize("value", ["x"])
def test_settings(settings: Settings, value: str) -> None:
    assert settings.log_level


def test_missing_fixture(missing_service: object) -> None:
    assert missing_service
