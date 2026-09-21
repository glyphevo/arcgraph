from .db import StoreReader
from .models import Payload
from .repository import MemoryRepository

_REQUESTS_TOTAL = object()


class Repository:
    def save(self, payload: Payload) -> Payload:
        return payload


def make_repository() -> Repository:
    return Repository()


class WorkService:
    def __init__(self) -> None:
        self.repo = make_repository()
        self.reader = StoreReader.from_current("benchmark.db")

    def create(self, payload: Payload) -> Payload:
        return self.repo.save(payload)

    def open_connection(self) -> object:
        return self.reader.connect()

    def load_internal_count(self) -> object:
        return self.reader.connect().execute("select count(*)").fetchone()


def normalize_label(value: str) -> str:
    return value.strip().upper()


def dynamic_boundary(cache, bag, plugin) -> object:
    cache.get("content")
    bag.add("content")
    return plugin.run("content")


def sqlalchemy_boundaries(session, stmt, result) -> object:
    session.execute(stmt).scalars().all()
    stmt.where(True).order_by("id").limit(1)
    _REQUESTS_TOTAL.labels(kind="benchmark").inc()
    return result.scalars().all()


def build_repository(session: object) -> MemoryRepository:
    return MemoryRepository(session)
