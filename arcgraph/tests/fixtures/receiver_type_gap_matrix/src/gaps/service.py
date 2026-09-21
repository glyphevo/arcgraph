from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import cast

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Repository:
    def save(self, payload: str) -> str:
        return payload

    def load(self, key: str) -> str:
        return key


class RepoFactory:
    @classmethod
    def create(cls) -> Repository:
        return Repository()


def provide_repository() -> Repository:
    return Repository()


class UnitOfWork:
    repo: Repository

    def __init__(self) -> None:
        self.repo = RepoFactory.create()

    @property
    def property_repo(self) -> Repository:
        return self.repo

    @cached_property
    def cached_repo(self) -> Repository:
        return provide_repository()

    def from_class_annotation(self, payload: str) -> str:
        return self.repo.save(payload)

    def from_property(self, payload: str) -> str:
        return self.property_repo.save(payload)

    def from_cached_property(self, payload: str) -> str:
        return self.cached_repo.save(payload)

    def from_local_alias(self, payload: str) -> str:
        repo = self.repo
        return repo.save(payload)

    def from_provider_local(self, payload: str) -> str:
        repo = provide_repository()
        return repo.save(payload)


class QueryBuilder:
    def where(self, expression: str) -> QueryBuilder:
        return self

    def order_by(self, field: str) -> QueryBuilder:
        return self

    def execute(self) -> Repository:
        return Repository()


def fluent_chain() -> str:
    repo = QueryBuilder().where("active").order_by("name").execute()
    return repo.load("one")


def from_cast(raw: object, payload: str) -> str:
    repo = cast(Repository, raw)
    return repo.save(payload)


class AsyncRepoManager:
    async def __aenter__(self) -> Repository:
        return Repository()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


async def from_async_context_manager(
    manager: AsyncRepoManager,
    payload: str,
) -> str:
    async with manager as repo:
        return repo.save(payload)


class RepoManager:
    def __enter__(self) -> Repository:
        return Repository()

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def from_sync_context_manager(manager: RepoManager, payload: str) -> str:
    with manager as repo:
        return repo.save(payload)


@dataclass
class DataclassRepository:
    prefix: str

    def save(self, payload: str) -> str:
        return f"{self.prefix}:{payload}"


def from_dataclass_constructor(payload: str) -> str:
    repo = DataclassRepository("dc")
    return repo.save(payload)


class Payload(BaseModel):
    value: str

    def model_dump(self) -> dict[str, object]:
        return {"value": self.value}


def from_pydantic_model_validate(raw: dict[str, object]) -> dict[str, object]:
    payload = Payload.model_validate(raw)
    return payload.model_dump()


class Settings(BaseSettings):
    mode: str

    def as_dict(self) -> dict[str, object]:
        return {"mode": self.mode}


def from_settings_model_validate(raw: dict[str, object]) -> dict[str, object]:
    settings = Settings.model_validate(raw)
    return settings.as_dict()


def dynamic_negative(cache, bag, plugin) -> None:
    cache.get("x")
    bag.add("y")
    plugin.run()
