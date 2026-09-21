from fastapi import APIRouter, Depends

from matrix_app.models import Payload
from matrix_app.service import MemoryService, get_service

router = APIRouter(prefix="/api")


def get_current_user() -> str:
    return "system"


@router.post(
    "/items",
    dependencies=[Depends(get_current_user)],
    tags=["items"],
)
def create_item(
    payload: Payload,
    service: MemoryService = Depends(get_service),
) -> str:
    return service.create(payload)
