from fastapi import APIRouter, HTTPException, status

from shared.queue import list_dead_letters, purge_dead_letter, replay_dead_letter

from app.schemas import DeadLetterRead, ReplayResponse

router = APIRouter(prefix="/dead-letters", tags=["dead-letters"])


@router.get("", response_model=list[DeadLetterRead])
async def get_dead_letters():
    return await list_dead_letters()


@router.post("/{job_id}/replay", response_model=ReplayResponse, status_code=status.HTTP_202_ACCEPTED)
async def replay(job_id: str):
    replayed = await replay_dead_letter(job_id)
    if not replayed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead letter not found")
    return ReplayResponse(replayed=True, job_id=job_id)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard(job_id: str):
    purged = await purge_dead_letter(job_id)
    if not purged:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead letter not found")
