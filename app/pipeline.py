"""Приём записей: проверка → сырые данные → дедупликация и слияние → журнал. Один путь для файлов, API, парсеров и админки.

* Каждая запись источника сохраняется «как пришла» в raw_records (в том числе отклонённая), поэтому нормализацию можно
  переиграть без повторного сбора (reprocess_raw).
* Плохая запись не обрывает загрузку: она отклоняется с причиной по-русски, остальные грузятся (ТЗ §6.1).
* Каждое создание и изменение события пишется в журнал с именем того, кто его сделал (audit_log)."""
import hashlib
import json
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import now_local
from .errors import humanize_validation
from .ingest import upsert_detailed
from .models import Event, RawRecord
from .schemas import EventIn


@dataclass
class Rejected:
    row: int
    title: str
    reasons: list[str]


@dataclass
class Result:
    status: str  # created | merged | unchanged | rejected
    event: Event | None = None
    cross_source: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class Report:
    created: int = 0
    updated: int = 0  # обновлено: то же событие пришло снова или его уточнил другой источник
    unchanged: int = 0
    cross_source: int = 0  # из обновлённых: подтверждено другим источником (найденные дубли)
    rejected: list[Rejected] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged + len(self.rejected)


def _payload(record) -> dict:
    return record.model_dump(mode="json") if isinstance(record, EventIn) else dict(record)


def payload_hash(payload: dict) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _store_raw(session: Session, payload: dict, outcome: str, event_id: int | None, error: str = "") -> RawRecord:
    source = str(payload.get("source") or "")[:100]
    digest = payload_hash(payload)
    raw = session.scalar(select(RawRecord).where(RawRecord.source == source, RawRecord.payload_hash == digest))
    now = now_local()
    if raw is None:
        raw = RawRecord(source=source, payload_hash=digest, payload=json.loads(json.dumps(payload, default=str)),
                        first_seen_at=now, last_seen_at=now)
        session.add(raw)
    raw.last_seen_at, raw.outcome, raw.error = now, outcome, error
    if event_id is not None:
        raw.event_id = event_id
    return raw


def process_record(session: Session, record, *, actor: str = "system", publish: bool = True, auto_queue: bool = False) -> Result:
    """Одна запись (словарь или EventIn). publish=False: новое событие сохраняется черновиком для модерации.
    auto_queue=True (вместе с publish=False): черновик ждёт автоматической проверки (app/verify.py), а не ручной."""
    payload = _payload(record)
    try:
        data = record if isinstance(record, EventIn) else EventIn.model_validate(payload)
    except ValidationError as exc:
        _, details = humanize_validation(exc.errors(include_url=False))
        reasons = [f"{d['field']}: {d['message']}" for d in details]
        _store_raw(session, payload, "rejected", None, "; ".join(reasons))
        session.commit()
        return Result("rejected", reasons=reasons)
    if not publish:
        data = data.model_copy(update={"publication": "draft"})
    event, outcome, cross = upsert_detailed(session, data, actor=actor)
    if auto_queue and not publish and outcome == "created":
        event.review_reason = "авто: ждёт проверки"
    _store_raw(session, payload, outcome, event.id)
    session.commit()
    return Result(outcome, event, cross)


def process_batch(session: Session, records, *, actor: str = "system", publish: bool = True, first_row: int = 1) -> Report:
    """Пачка записей → отчёт: создано, обновлено, без изменений, отклонено и почему. first_row: номер первой записи в файле."""
    report = Report()
    for row, record in enumerate(records, first_row):
        if not isinstance(record, (dict, EventIn)):
            report.rejected.append(Rejected(row, "", ["запись должна быть объектом с полями события"]))
            continue
        result = process_record(session, record, actor=actor, publish=publish)
        if result.status == "rejected":
            title = str(record.get("title", "") if isinstance(record, dict) else record.title)[:80]
            report.rejected.append(Rejected(row, title, result.reasons))
            continue
        if result.status == "created":
            report.created += 1
        elif result.status == "merged":
            report.updated += 1
            report.cross_source += int(result.cross_source)
        else:
            report.unchanged += 1
        report.event_ids.append(result.event.id)
    return report


def reprocess_raw(session: Session, *, source: str | None = None, actor: str = "reprocess") -> Report:
    """Переигрывает нормализацию и слияние по сохранённым сырым записям без повторного сбора (ТЗ §6.1).

    Идут по возрастанию last_seen_at, поэтому более свежая версия записи побеждает. Новые raw-записи не создаются."""
    stmt = select(RawRecord).where(RawRecord.outcome != "rejected").order_by(RawRecord.last_seen_at, RawRecord.id)
    if source:
        stmt = stmt.where(RawRecord.source == source)
    report = Report()
    for raw in session.scalars(stmt):
        try:
            data = EventIn.model_validate(raw.payload)
        except ValidationError:
            report.rejected.append(Rejected(raw.id, str(raw.payload.get("title", ""))[:80], ["запись больше не проходит проверку"]))
            continue
        event, outcome, cross = upsert_detailed(session, data, actor=actor)
        raw.event_id = event.id
        if outcome == "created":
            report.created += 1
        elif outcome == "merged":
            report.updated += 1
            report.cross_source += int(cross)
        else:
            report.unchanged += 1
    session.commit()
    return report
