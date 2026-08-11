from fastapi import APIRouter

from app.schemas import QueueDepthResponse
from shared.queue import queue_depth

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/depth", response_model=QueueDepthResponse)
async def get_queue_depth():
    depth = await queue_depth()
    return QueueDepthResponse(depth=depth)
