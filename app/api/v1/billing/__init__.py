from fastapi import APIRouter

from . import lessons

router = APIRouter(prefix="/billing", tags=["billing"])
router.include_router(lessons.router)


__all__ = [
    "router",
]
