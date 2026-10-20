from fastapi import APIRouter
from app.controllers.health_controller import health_check

router = APIRouter()

@router.get("/health")
def health():
    return health_check()
