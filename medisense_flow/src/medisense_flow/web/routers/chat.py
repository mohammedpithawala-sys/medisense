"""Chat about a finished report. Everything that can fail with a normal HTTP error runs in
the `prepare_turn` dependency, before the stream starts (an error raised inside a stream
can't become a clean 404/409/429).

POST   /api/reports/{id}/chat   ask a question; the answer streams back as Server-Sent Events
GET    /api/reports/{id}/chat   the saved conversation
DELETE /api/reports/{id}/chat   clear the conversation
"""
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from .. import chat, db
from ..auth import CurrentPatient
from ..schemas import ChatHistory, ChatMessageView, ChatRequest
from .api import SessionId, StoreDep

router = APIRouter(prefix="/api/reports/{session_id}/chat", tags=["chat"])


async def prepare_turn(
    session_id: SessionId, body: ChatRequest, patient: CurrentPatient, store: StoreDep
) -> chat.ChatTurn:
    return await chat.prepare(store, patient, session_id, body.message)


TurnDep = Annotated[chat.ChatTurn, Depends(prepare_turn)]


@router.post("", response_class=EventSourceResponse)
async def ask(turn: TurnDep) -> AsyncIterable[ServerSentEvent]:
    async for event, data in chat.run_turn(turn):
        yield ServerSentEvent(data=data, event=event)


@router.get("")
async def history(session_id: SessionId, patient: CurrentPatient, store: StoreDep) -> ChatHistory:
    await run_in_threadpool(chat.load_context, store, patient, session_id)  # 404 / 409 like the others
    rows = await run_in_threadpool(db.get_chat_messages, patient.id, session_id)
    left = await run_in_threadpool(chat.questions_left, patient.id, session_id)
    return ChatHistory(messages=[ChatMessageView(**row) for row in rows], questions_left=left)


@router.delete("", status_code=204)
async def clear(session_id: SessionId, patient: CurrentPatient, store: StoreDep) -> None:
    await run_in_threadpool(chat.load_context, store, patient, session_id)
    await run_in_threadpool(db.clear_chat, patient.id, session_id)
