from fastapi import APIRouter
from app.api.v1.endpoints.process_messages import router as process_messages_router
from app.api.v1.endpoints.analyze_user import router as analyze_user_router
from app.api.v1.endpoints.search_details import router as search_details_router
from app.api.v1.endpoints.user_summary import router as user_summary_router
from app.api.v1.endpoints.intent_analysis import router as intent_analysis_router

api_router = APIRouter()
api_router.include_router(process_messages_router, prefix="/v1", tags=["v1"])
api_router.include_router(analyze_user_router, prefix="/v1", tags=["v1"])
api_router.include_router(search_details_router, prefix="/v1", tags=["v1"])
api_router.include_router(user_summary_router, prefix="/v1", tags=["v1"])
api_router.include_router(intent_analysis_router, prefix="/v1", tags=["v1"])
