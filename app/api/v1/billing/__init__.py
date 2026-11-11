from fastapi import APIRouter

from . import lessons, rates

router = APIRouter(prefix="/billing", tags=["billing"])
router.include_router(lessons.router)
router.include_router(rates.router)


__all__ = [
    "router",
]
