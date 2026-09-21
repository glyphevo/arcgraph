import pytest

from bench.models import Payload
from bench.service import WorkService, normalize_label


@pytest.fixture
def service() -> WorkService:
    return WorkService()


def test_service_create(service: WorkService) -> None:
    payload = Payload(content="hello")
    assert service.create(payload).content == "hello"


def test_normalize_label() -> None:
    assert normalize_label(" hi ") == "HI"
