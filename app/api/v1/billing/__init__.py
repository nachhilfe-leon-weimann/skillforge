from fastapi import APIRouter

from . import calendar, lessons, payers, rates

router = APIRouter(prefix="/billing", tags=["billing"])
router.include_router(lessons.router)
router.include_router(rates.router)
router.include_router(payers.router)
router.include_router(calendar.router)


__all__ = [
    "router",
]
