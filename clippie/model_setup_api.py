"""Explicit publisher model downloads; protected by the desktop session boundary."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from .model_setup import SetupError, manager

router = APIRouter(prefix='/api/setup', tags=['setup'])


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accept_terms: StrictBool


def current():
    from .app import STORE
    return manager(STORE.root)


@router.get('/models')
def models():
    return current().snapshot()


@router.post('/models/{ident}/install')
def install(ident: str, body: InstallRequest):
    try:
        return current().start(ident, accept_terms=body.accept_terms)
    except SetupError as error:
        raise HTTPException(error.status, str(error)) from None


@router.delete('/models/{ident}/install')
def cancel(ident: str):
    try:
        return current().cancel(ident)
    except SetupError as error:
        raise HTTPException(error.status, str(error)) from None


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selections: dict[str, str]
    accept_terms: StrictBool


@router.post('/batch')
def start_batch(body: BatchRequest):
    try:
        if not body.accept_terms:
            raise SetupError('Review and accept the displayed terms.', 422)
        return current().start_batch(body.selections)
    except SetupError as error:
        raise HTTPException(error.status, str(error)) from None


@router.post('/batch/resume')
def resume_batch():
    try:
        return current().start_batch(resume=True)
    except SetupError as error:
        raise HTTPException(error.status, str(error)) from None


@router.delete('/batch')
def cancel_batch():
    try:
        return current().cancel_batch()
    except SetupError as error:
        raise HTTPException(error.status, str(error)) from None
