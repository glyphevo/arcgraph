import pytest


@pytest.fixture
def authenticated_user():
    return "system"


@pytest.fixture(autouse=True)
def audit_context():
    return {"trace_id": "matrix"}
