from fastapi import APIRouter

from . import calendar, lessons, payers, payment_requests, rates

router = APIRouter(prefix="/billing", tags=["billing"])
router.include_router(lessons.router)
router.include_router(rates.router)
router.include_router(payers.router)
router.include_router(calendar.router)
router.include_router(payment_requests.router)


__all__ = [
    "router",
]
