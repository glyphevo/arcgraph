class Greeter:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def greet(self, name: str) -> str:
        return f"{self.prefix}, {name}"


def build_message(value: str) -> str:
    return value.upper()


def normalize_title(value: str) -> str:
    cleaned = value.strip()
    return cleaned.lower()


def normalize_label(value: str) -> str:
    cleaned = value.strip()
    return cleaned.lower()


class MemoryService:
    def __init__(self, repo: object) -> None:
        self.repo = repo

    async def create_memory(self, content: str) -> object:
        return await self.repo.create(content)
