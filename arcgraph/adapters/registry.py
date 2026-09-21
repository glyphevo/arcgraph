"""Default semantic adapter registry wiring."""

from __future__ import annotations

from arcgraph.adapters.arq_adapter import ARQAdapter
from arcgraph.adapters.celery_adapter import CeleryAdapter
from arcgraph.adapters.common import AdapterRegistry
from arcgraph.adapters.django_adapter import DjangoAdapter
from arcgraph.adapters.fastapi_adapter import FastAPIAdapter
from arcgraph.adapters.logging_adapter import LoggingAdapter
from arcgraph.adapters.mcp_adapter import MCPAdapter
from arcgraph.adapters.pydantic_adapter import PydanticAdapter
from arcgraph.adapters.pytest_adapter import PytestAdapter
from arcgraph.adapters.service_container_adapter import ServiceContainerAdapter
from arcgraph.adapters.sqlalchemy_adapter import SQLAlchemyAdapter
from arcgraph.adapters.typer_click_adapter import TyperClickAdapter


def default_adapter_registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            FastAPIAdapter(),
            DjangoAdapter(),
            MCPAdapter(),
            ARQAdapter(),
            CeleryAdapter(),
            ServiceContainerAdapter(),
            SQLAlchemyAdapter(),
            LoggingAdapter(),
            PydanticAdapter(),
            PytestAdapter(),
            TyperClickAdapter(),
        ]
    )
