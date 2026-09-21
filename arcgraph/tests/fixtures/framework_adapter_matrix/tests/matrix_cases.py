import pytest


@pytest.fixture
def sample_payload():
    return {"content": "hello"}


@pytest.mark.usefixtures("authenticated_user")
def test_create_item(sample_payload):
    assert sample_payload["content"]


@pytest.mark.parametrize("value", ["hello", "world"])
def test_parametrized_value(value):
    assert value


def test_missing_fixture(unknown_fixture):
    assert unknown_fixture
