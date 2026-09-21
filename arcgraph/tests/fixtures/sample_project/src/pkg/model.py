class Base:
    pass


class Memory(Base):
    __tablename__ = "memories"

    def __init__(self, content: str) -> None:
        self.content = content
