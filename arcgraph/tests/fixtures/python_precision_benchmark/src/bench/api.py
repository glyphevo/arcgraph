from fastapi import APIRouter, Depends

from .models import Payload
from .service import WorkService

router = APIRouter()


def get_service() -> WorkService:
    return WorkService()


@router.post("/items")
async def create_item(
    payload: Payload,
    service: WorkService = Depends(get_service),
) -> Payload:
    return service.create(payload)
