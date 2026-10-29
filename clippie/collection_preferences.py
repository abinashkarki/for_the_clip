"""Workspace form defaults; API callers still submit explicit collection constraints.

Unreadable or unsupported stored documents fail closed, preserving the original row.
"""
import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

from . import collection_api

router = APIRouter(prefix='/api/collection-preferences', tags=['collection-preferences'])
IDENTITY = 'workspace_collection_preferences'
KIND = 'workspace_preferences'
DEFAULTS = dict(form='auto', edit_format='auto', max_shorts=12, aspect_ratio='9:16', captions=True,
                caption_style='auto', canvas_style='plain', max_duration=180, request_calls=16)
PreferenceField = Literal['edit_format', 'form', 'max_shorts', 'aspect_ratio', 'captions', 'caption_style', 'canvas_style', 'max_duration', 'request_calls']


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    form: Literal['auto', 'moment', 'story'] = 'auto'
    edit_format: Literal['auto', 'explanation', 'small_story', 'exchange'] = 'auto'
    max_shorts: StrictInt = Field(default=12, ge=1, le=30)
    aspect_ratio: Literal['9:16', '16:9'] = '9:16'
    captions: StrictBool = True
    caption_style: Literal['auto', 'clean_focus', 'editorial', 'phrase'] = 'auto'
    canvas_style: Literal['plain', 'blurred'] = 'plain'
    max_duration: float = Field(default=180, ge=1, le=180, allow_inf_nan=False)
    request_calls: StrictInt = 16

    @model_validator(mode='after')
    def allowed_calls(self):
        if self.request_calls not in (8, 16, 24):
            raise ValueError('request_calls must be 8, 16, or 24.')
        return self


class PreferencePatch(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: StrictInt = Field(ge=0)
    changes: Preferences = Field(default_factory=Preferences)
    reset_fields: list[PreferenceField] = Field(default_factory=list, max_length=9)

    @model_validator(mode='after')
    def disjoint(self):
        if self.changes.model_fields_set.intersection(self.reset_fields):
            raise ValueError('Changes and resets must not overlap.')
        return self


class StoredPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: Literal['workspace_collection_preferences']
    schema_version: Literal[1]
    revision: StrictInt = Field(ge=0)
    overrides: Preferences


class PreferencesUnavailable(ValueError):
    pass


class RevisionConflict(ValueError):
    pass


def _read(db):
    row = db.execute('SELECT kind,document FROM records WHERE id=?', (IDENTITY,)).fetchone()
    if row is None:
        return 0, {}
    try:
        if row[0] != KIND:
            raise ValueError('Unexpected record kind')
        doc = StoredPreferences.model_validate(json.loads(row[1]))
        return doc.revision, doc.overrides.model_dump(exclude_unset=True)
    except (ValueError, TypeError, ValidationError) as exc:
        raise PreferencesUnavailable('Saved preferences are invalid or use an unsupported schema; original data was preserved.') from exc


def _public(revision, overrides):
    return dict(revision=revision, overrides=overrides, effective={**DEFAULTS, **overrides}, defaults=dict(DEFAULTS))


def load(storage):
    with storage._transaction() as db:
        return _public(*_read(db))


def update(storage, request):
    request = PreferencePatch.model_validate(request)
    with storage._transaction() as db:
        revision, overrides = _read(db)
        if revision != request.expected_revision:
            raise RevisionConflict('Saved preferences changed. Reload before saving.')
        updated = {key: value for key, value in overrides.items() if key not in request.reset_fields}
        updated.update(request.changes.model_dump(exclude_unset=True))
        if updated != overrides:
            revision += 1
            storage._save(db, KIND, dict(id=IDENTITY, schema_version=1, revision=revision, overrides=updated))
        return _public(revision, updated)


def _respond(operation, *args):
    try:
        return operation(collection_api.store(), *args)
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PreferencesUnavailable as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get('')
def get_preferences():
    return _respond(load)


@router.patch('')
def patch_preferences(request: PreferencePatch):
    return _respond(update, request)
