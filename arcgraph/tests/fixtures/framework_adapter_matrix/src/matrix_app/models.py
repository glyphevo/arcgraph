from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class Payload(BaseModel):
    content: str
    priority: int = Field(1, ge=0)


class AppSettings(BaseSettings):
    log_level: str = Field("INFO", env="APP_LOG_LEVEL")


class UserRecord:
    __tablename__ = "user_records"
    id: int
    name: str
