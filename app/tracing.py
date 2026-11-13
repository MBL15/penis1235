"""Трассировка ответов агента (ТЗ §7.4): что спросили, как поняли, что вызвали, на чём основан ответ, сколько это стоило.

Трасса пишется для каждого ответа (и в режиме модели, и в режиме правил) и открывается по ссылке /trace/{id}, из админки
или через GET /api/v1/traces/{id}. Хранится RETENTION_DAYS дней; в ней текст вопроса, но нет ни адреса, ни номера чата."""
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import config
from .config import now_local
from .models import AgentTrace
from .observability import request_id_var

RETENTION_DAYS = 30
MAX_QUERY_CHARS = 1000
MAX_REPLY_CHARS = 6000


def cost_rub(input_tokens: int, output_tokens: int) -> float | None:
    """Стоимость ответа в рублях по тарифам из настроек; None, если тариф не задан."""
    pin, pout = config.LLM_PRICE_INPUT_RUB_PER_M, config.LLM_PRICE_OUTPUT_RUB_PER_M
    if pin is None or pout is None:
        return None
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 4)


@dataclass
class TraceBuilder:
    """Собирает трассу по ходу ответа; в базу попадает один раз, в конце (finish)."""

    query: str
    history_turns: int = 0
    mode: str = "rules"
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    plan: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    flags: dict = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    started: float = field(default_factory=time.perf_counter)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def step(self, tool: str, args: dict, *, ok: bool = True, ids: list[int] | None = None, total: int | None = None,
             ms: float = 0.0, error: str = "") -> None:
        entry = {"tool": tool, "args": args, "ok": ok, "event_ids": ids or [], "total": total, "ms": round(ms, 1)}
        if error:
            entry["error"] = error[:300]
        self.steps.append(entry)

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))

    def flag(self, name: str, value=True) -> None:
        self.flags[name] = value

    def finish(self, session: Session, *, reply: str, event_ids: list[int], needs_clarification: bool) -> str:
        """Сохраняет трассу и возвращает её идентификатор. Ошибка записи ответ не ломает."""
        try:
            session.add(AgentTrace(
                id=self.id, created_at=now_local(), request_id=request_id_var.get() if request_id_var.get() != "-" else "",
                mode=self.mode, provider=self.provider, model=self.model, prompt_version=self.prompt_version,
                query=self.query[:MAX_QUERY_CHARS], history_turns=self.history_turns, plan=self.plan, steps=self.steps,
                event_ids=event_ids, reply=reply[:MAX_REPLY_CHARS], needs_clarification=needs_clarification, flags=self.flags,
                latency_ms=int((time.perf_counter() - self.started) * 1000), input_tokens=self.input_tokens,
                output_tokens=self.output_tokens, cost_rub=cost_rub(self.input_tokens, self.output_tokens) if self.mode == "llm" else 0.0,
            ))
            session.commit()
        except Exception:  # трасса нужна для проверки, но ответ пользователю важнее
            session.rollback()
        return self.id


def get_trace(session: Session, trace_id: str) -> AgentTrace | None:
    return session.get(AgentTrace, trace_id) if len(trace_id) == 32 and trace_id.isalnum() else None


def list_traces(session: Session, *, mode: str | None = None, limit: int = 50, offset: int = 0) -> list[AgentTrace]:
    stmt = select(AgentTrace).order_by(AgentTrace.created_at.desc())
    if mode in ("llm", "rules"):
        stmt = stmt.where(AgentTrace.mode == mode)
    return list(session.scalars(stmt.limit(max(1, min(limit, 200))).offset(max(0, offset))))


def purge_traces(session: Session, now: datetime | None = None) -> int:
    removed = session.execute(delete(AgentTrace).where(AgentTrace.created_at < (now or now_local()) - timedelta(days=RETENTION_DAYS))).rowcount
    session.commit()
    return removed or 0
