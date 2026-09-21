from fastapi import Depends, FastAPI

from matrix_app import api
from matrix_app.service import get_service

app = FastAPI()


@app.get("/health", tags=["system"])
def health_check(service=Depends(get_service)) -> str:
    return "ok"


app.include_router(api.router, prefix="/v1")
