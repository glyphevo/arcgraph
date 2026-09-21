from pydantic import BaseModel, Field


class Base:
    pass


class Payload(BaseModel):
    content: str = Field(..., min_length=1)


class Memory(Base):
    __tablename__ = "memories"

    def __init__(self, content: str) -> None:
        self.content = content
