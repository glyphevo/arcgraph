from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.get("/greeting/status")
def greeting_status() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/greeting/refresh")
def refresh_greeting() -> dict[str, str]:
    return {"status": "refreshed"}
