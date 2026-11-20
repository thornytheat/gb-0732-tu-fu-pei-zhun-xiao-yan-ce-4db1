import unicodedata
from typing import Annotated

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.concurrence.service import build_concurrence

router = APIRouter(prefix='/api/review')
RunId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]


class ConcurrenceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    edge: Annotated[str, Field(pattern=r'^[A-Z0-9][A-Z0-9-]{0,23}$')]
    reviewer: Annotated[str, Field(min_length=1, max_length=30)]
    expected_version: Annotated[int, Field(ge=0)] = 0

    @field_validator('reviewer', mode='before')
    @classmethod
    def reviewer_name(cls, name):
        if not isinstance(name, str):
            return name
        if any(unicodedata.category(char).startswith('C') for char in name):
            raise ValueError('会签员姓名包含控制字符')
        return name.strip()


@router.post('/runs/{run_id}/concurrences')
def concur(request: Request, run_id: RunId, form: ConcurrenceInput):
    store = request.app.state.store
    snapshot = store.get(run_id)
    document = build_concurrence(snapshot, form.edge, form.reviewer)
    document['expected_version'] = form.expected_version
    return {'concurrences': store.concur(run_id, form.edge, document)}
