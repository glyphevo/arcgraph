from fastapi import APIRouter

v2_router = APIRouter(prefix="/api/v2")
v3_router = APIRouter(prefix="/api/v3")
api_router = APIRouter(prefix="/api")


@v2_router.get("/shared/status")
def shared_status_v2() -> dict[str, str]:
    return {"version": "v2"}


@v3_router.get("/shared/status")
def shared_status_v3() -> dict[str, str]:
    return {"version": "v3"}


@api_router.get("/plain/status")
def plain_status() -> dict[str, str]:
    return {"status": "ok"}


@api_router.get("/videos/list")
def video_list() -> dict[str, list[str]]:
    return {"videos": []}
