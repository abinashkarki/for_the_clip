"""Restoration notice; acknowledgment never dispatches background work now."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictBool, ConfigDict

from .storage_safety import restore_notice, acknowledge_restore

router = APIRouter(prefix='/api/system/recovery', tags=['recovery'])


def root():
    from .app import STORE
    return STORE.root


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reviewed: StrictBool


@router.get('')
def status():
    notice = restore_notice(root())
    return {'paused': notice.get('background_resume_disabled') is True,
            'restored_at': notice.get('restored_at'), 'backup_id': notice.get('backup_id'),
            'rescue_id': notice.get('rescue_id')}


@router.post('/acknowledge')
def acknowledge(body: ReviewRequest):
    if not body.reviewed:
        raise HTTPException(422, 'Review restored projects and scheduled posts before enabling background recovery.')
    acknowledge_restore(root())
    return {**status(), 'restart_required': True}
