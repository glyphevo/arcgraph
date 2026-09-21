from .repository import MemoryRepository
from .service import MemoryService


class ServiceContainer:
    def get_memory_service(self, session: object) -> MemoryService:
        return MemoryService(MemoryRepository(session))


def get_container() -> ServiceContainer:
    return ServiceContainer()
