from sqlalchemy import select

from .model import Memory


class MemoryRepository:
    def __init__(self, session: object) -> None:
        self.session = session

    async def create(self, content: str) -> Memory:
        memory = Memory(content=content)
        self.session.add(memory)
        return memory

    async def get(self, memory_id: int) -> object:
        result = await self.session.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        return result.scalar_one_or_none()
