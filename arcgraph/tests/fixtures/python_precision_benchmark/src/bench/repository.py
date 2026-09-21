from sqlalchemy import select

from .models import Memory, Payload


class MemoryRepository:
    def __init__(self, session: object) -> None:
        self.session = session

    def create(self, payload: Payload) -> Memory:
        memory = Memory(content=payload.content)
        self.session.add(memory)
        return memory

    def get(self, memory_id: int) -> object:
        result = self.session.execute(select(Memory).where(Memory.id == memory_id))
        return result.scalar_one_or_none()
