from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class MemoryPayload(BaseModel):
    content: str = Field(..., min_length=1)


class Settings(BaseSettings):
    log_level: str = Field("INFO", env="APP_LOG_LEVEL")
