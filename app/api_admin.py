"""Методы модератора (нужен ключ X-API-Key): список событий с черновиками, правка карточки, публикация, импорт файла,
журнал изменений, обращения. Интерфейс к ним — страница /admin. Подключается под /api/v1/admin."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from . import audit, config
from .auth import is_account_actor, require_admin
from .config import now_local
from .deps import SessionDep
from .feedback import MAX_REPLY, FeedbackOut, list_feedback, set_feedback_status
from .formatting import event_to_out
from .ingest import parse_records
from .moderation import EditError, edit_event, set_publication
from .normalize import STATUSES
from .pipeline import process_batch
from .schemas import (
    AdminSessionOut,
    AuditEntryOut,
    SyncRunOut,
    BulkIngestResult,
    ErrorOut,
    EventList,
    EventOut,
    EventPatch,
    ImportIn,
    PublicationPatch,
    RejectedRecord,
)
from .search import SearchFilters, get_event, search
from .tracing import get_trace, list_traces

router = APIRouter(
    prefix="/admin", tags=["Администрирование"], dependencies=[Depends(require_admin)],
    responses={401: {"model": ErrorOut, "description": "Нет или неверен ключ X-API-Key"},
               404: {"model": ErrorOut}, 422: {"model": ErrorOut}},
)
EventId = Annotated[int, Path(ge=1, le=2**31 - 1)]
MAX_IMPORT_RECORDS = 1000


@router.get("/session", response_model=AdminSessionOut, summary="Проверка ключа: кто вошёл")
def admin_session(actor: Annotated[str, Depends(require_admin)]) -> AdminSessionOut:
    """`via`: key (ключ X-API-Key) или account (вход в аккаунт с ролью «модератор»). Роли и ключи Министерства меняет только ключ."""
    account = is_account_actor(actor)
    return AdminSessionOut(actor=actor, via="account" if account else "key", can_manage_users=not account)


@router.get("/events", response_model=EventList, summary="События для модератора (с черновиками, отменёнными и прошедшими)")
def admin_events(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    publication: Annotated[str, Query(pattern="^(all|published|draft)$", description="draft — очередь модерации")] = "all",
    status: Annotated[str, Query(description="Статус или all")] = "all",
    city: Annotated[str | None, Query(max_length=64)] = None,
    include_past: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> EventList:
    if status != "all" and status not in STATUSES:
        raise HTTPException(422, f"Неизвестный статус: {status}")
    filters = SearchFilters(q=q, city=city, statuses=[status], include_past=include_past, include_drafts=True,
                            publication=None if publication == "all" else publication, sort="date", limit=limit, offset=offset)
    now = now_local()
    events, total = search(session, filters, now)
    return EventList(items=[event_to_out(e, now) for e in events], total=total, limit=limit, offset=offset)


@router.get("/events/{event_id}", response_model=EventOut, summary="Карточка события для модератора (в том числе черновик)")
def admin_event(event_id: EventId, session: SessionDep) -> EventOut:
    event = get_event(session, event_id, include_drafts=True)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    return event_to_out(event)


@router.patch("/events/{event_id}", response_model=EventOut, summary="Править карточку",
              description="Меняются только переданные поля, правка попадает в журнал. Если источник пришлёт своё значение "
                          "того же поля, оно перезапишет правку: источник остаётся авторитетом для собственных данных.")
def patch_event(event_id: EventId, payload: EventPatch, session: SessionDep, actor: Annotated[str, Depends(require_admin)]) -> EventOut:
    event = get_event(session, event_id, include_drafts=True)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    try:
        edit_event(session, event, payload, actor)
    except EditError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    return event_to_out(event)


@router.post("/events/{event_id}/publication", response_model=EventOut, summary="Опубликовать или вернуть в черновики")
def publish_event(event_id: EventId, payload: PublicationPatch, session: SessionDep,
                  actor: Annotated[str, Depends(require_admin)]) -> EventOut:
    event = get_event(session, event_id, include_drafts=True)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    return event_to_out(set_publication(session, event, payload.publication, actor))


@router.post("/import", response_model=BulkIngestResult, summary="Загрузить файл JSON или CSV",
             description="Отчёт: создано, обновлено, без изменений и отклонённые записи с причинами. Повторная загрузка того же файла дублей не создаёт.")
def import_file(payload: ImportIn, session: SessionDep, actor: Annotated[str, Depends(require_admin)]) -> BulkIngestResult:
    name = payload.filename.strip()
    kind = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        records = parse_records(payload.content, kind)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(records) > MAX_IMPORT_RECORDS:
        raise HTTPException(413, f"В файле {len(records)} записей, за один раз принимается не больше {MAX_IMPORT_RECORDS}")
    safe_name = "".join(ch for ch in name if ch.isalnum() or ch in "._- ")[:60] or "file"
    report = process_batch(session, records, actor=f"{actor}:import:{safe_name}"[:100], publish=payload.publish,
                           first_row=2 if kind == "csv" else 1)
    ids = list(dict.fromkeys(report.event_ids))[:100]
    from sqlalchemy import select

    from .models import Event

    events = {e.id: e for e in session.scalars(select(Event).where(Event.id.in_(ids)))} if ids else {}
    now = now_local()
    return BulkIngestResult(
        created=report.created, updated=report.updated, merged=report.updated, unchanged=report.unchanged,
        rejected=[RejectedRecord(row=r.row, title=r.title, reasons=r.reasons) for r in report.rejected],
        total=report.total, events=[event_to_out(events[i], now) for i in ids if i in events],
    )


@router.get("/sync-runs", response_model=list[SyncRunOut], summary="Запуски сбора по расписанию (app/scheduler.py)")
def sync_runs(session: SessionDep, job: Annotated[str | None, Query(max_length=64)] = None,
              limit: Annotated[int, Query(ge=1, le=200)] = 30) -> list[SyncRunOut]:
    from sqlalchemy import select

    from .models import SyncRun

    stmt = select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(limit)
    if job:
        stmt = stmt.where(SyncRun.job == job)
    return [SyncRunOut.model_validate(r, from_attributes=True) for r in session.scalars(stmt)]


@router.get("/audit", response_model=list[AuditEntryOut], summary="Журнал изменений: кто, когда и что изменил")
def audit_log(
    session: SessionDep,
    event_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    actor: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[AuditEntryOut]:
    return [AuditEntryOut(id=e.id, at=e.at, actor=e.actor, action=e.action, event_id=e.event_id, changes=e.changes or {},
                          request_id=e.request_id or "")
            for e in audit.entries(session, event_id=event_id, actor=actor, limit=limit, offset=offset)]


@router.get("/traces", summary="Трассы ответов агента (последние первыми)")
def admin_traces(
    session: SessionDep,
    mode: Annotated[str | None, Query(pattern="^(llm|rules)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[dict]:
    return [{"id": t.id, "created_at": t.created_at, "mode": t.mode, "query": t.query[:200], "events": len(t.event_ids or []),
             "latency_ms": t.latency_ms, "tokens": (t.input_tokens or 0) + (t.output_tokens or 0), "cost_rub": t.cost_rub,
             "flags": sorted((t.flags or {}).keys())} for t in list_traces(session, mode=mode, limit=limit, offset=offset)]


@router.get("/feedback", response_model=list[FeedbackOut], summary="Обращения через форму обратной связи")
def feedback_inbox(
    session: SessionDep,
    status: Annotated[str, Query(pattern="^(all|new|processed)$")] = "new",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[FeedbackOut]:
    return list_feedback(session, status=status, limit=limit)


@router.patch("/feedback/{feedback_id}", response_model=FeedbackOut, summary="Статус обращения и ответ автору",
              description="`status`: new, in_progress или processed; `reply` — ответ автору (до 1000 знаков). Автору-аккаунту ответ придёт "
                          "в Telegram от бота. Можно передать что-то одно.")
def feedback_status(feedback_id: Annotated[int, Path(ge=1, le=2**31 - 1)], payload: dict, session: SessionDep,
                    actor: Annotated[str, Depends(require_admin)]) -> FeedbackOut:
    status, reply = payload.get("status"), payload.get("reply")
    if status is None and reply is None:
        raise HTTPException(422, "Передайте status или reply")
    if status is not None and status not in ("new", "in_progress", "processed"):
        raise HTTPException(422, "status должен быть new, in_progress или processed")
    if reply is not None and (not isinstance(reply, str) or len(reply) > MAX_REPLY):
        raise HTTPException(422, f"reply должен быть строкой не длиннее {MAX_REPLY} знаков")
    item = set_feedback_status(session, feedback_id, status, actor, reply)
    if item is None:
        raise HTTPException(404, "Обращение не найдено")
    return item
