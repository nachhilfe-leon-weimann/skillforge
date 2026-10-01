from fastapi import APIRouter

from . import auth, crm

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(crm.router)
