from matrix_app.models import Payload


class MemoryService:
    def create(self, payload: Payload) -> str:
        return payload.content


def get_service() -> MemoryService:
    return MemoryService()
