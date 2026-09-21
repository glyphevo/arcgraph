from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/pets")
def listPets() -> list[dict[str, object]]:
    return []


@router.post("/api/v1/pets")
def createPet(payload: dict[str, object]) -> dict[str, object]:
    return payload
