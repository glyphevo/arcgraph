from fastapi import APIRouter, Depends

from .container import get_container
from .service import Greeter, MemoryService, build_message

router = APIRouter()


def get_memory_service() -> MemoryService:
    return get_container().get_memory_service(None)


@router.get("/hello")
def hello(name: str) -> str:
    greeter = Greeter(prefix="hello")
    return build_message(greeter.greet(name))


@router.post("/memories")
async def create_memory(
    content: str,
    service: MemoryService = Depends(get_memory_service),
) -> object:
    return await service.create_memory(content)
