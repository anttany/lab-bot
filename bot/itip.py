from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import DEFAULT_LAB_SEATS
from bot.models import Lab, QueueEntry, Subject
from bot.queue import as_utc

ITIP_SUBJECT = "Информационные технологии и программирование"
ITIP_SHORT = "ИТиП"
ITIP_TEACHER = "Харрасов Камиль Раисович"
QUEUE_MAIN = "main"
QUEUE_CURRENT = "current"
QUEUE_BACKLOG = "backlog"
CALENDAR_ID_PREFIX = "itip-cal-"

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class ItipSession:
    day: date
    current_lab: int
    headline: str
    starts: time


@dataclass(frozen=True)
class QueueSpec:
    kind: str
    title: str
    short: str


ITIP_SESSIONS: tuple[ItipSession, ...] = (
    ItipSession(date(2026, 11, 24), 5, "Первый день сдачи 5 лабы", time(15, 10)),
    ItipSession(date(2026, 11, 27), 5, "Второй день сдачи 5 лабы", time(9, 30)),
    ItipSession(date(2026, 12, 1), 6, "Первый день сдачи 6 лабы", time(13, 0)),
    ItipSession(date(2026, 12, 8), 6, "Второй день сдачи 6 лабы", time(15, 10)),
    ItipSession(date(2026, 12, 15), 7, "Первый день сдачи 7 лабы", time(13, 0)),
    ItipSession(date(2026, 12, 19), 7, "Второй день сдачи 7 лабы", time(15, 10)),
    ItipSession(date(2026, 12, 22), 8, "Первый день сдачи 8 лабы", time(15, 10)),
    ItipSession(date(2026, 12, 25), 8, "Дополнительный день сдачи", time(9, 30)),
    ItipSession(date(2026, 12, 29), 8, "Второй день сдачи 8 лабы", time(13, 0)),
)

_SESSIONS_BY_DAY = {item.day: item for item in ITIP_SESSIONS}


def _fold_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def is_itip_name(name: str) -> bool:
    folded = _fold_name(name)
    return folded in {_fold_name(ITIP_SUBJECT), _fold_name(ITIP_SHORT), "итип"}


def is_itip_lab(lab: Lab) -> bool:
    subject = getattr(lab, "subject", None)
    name = subject.name if subject is not None else ""
    return is_itip_name(name)


def local_day(dt: datetime, tz: ZoneInfo) -> date:
    return as_utc(dt).astimezone(tz).date()


def itip_session_for(lab: Lab, tz: ZoneInfo | None = None) -> ItipSession | None:
    if not is_itip_lab(lab):
        return None
    tz = tz or MSK
    return _SESSIONS_BY_DAY.get(local_day(lab.starts_at, tz))


def has_dual_queues(lab: Lab, tz: ZoneInfo | None = None) -> bool:
    return itip_session_for(lab, tz) is not None


def _lab_word(number: int) -> str:
    if number == 1:
        return "1 лаба"
    return f"{number} лаба"


def _backlog_label(current: int) -> str:
    last = current - 1
    if last <= 0:
        return "старые лабы"
    if last == 1:
        return "1 лаба"
    return f"1–{last} лабы"


def queue_specs(lab: Lab, tz: ZoneInfo | None = None) -> list[QueueSpec]:
    session = itip_session_for(lab, tz)
    if session is None:
        return [QueueSpec(QUEUE_MAIN, "Очередь", "очередь")]
    current = _lab_word(session.current_lab)
    backlog = _backlog_label(session.current_lab)
    return [
        QueueSpec(QUEUE_CURRENT, f"Очередь 1 · {current}", current),
        QueueSpec(QUEUE_BACKLOG, f"Очередь 2 · {backlog}", backlog),
    ]


def spec_by_kind(lab: Lab, kind: str, tz: ZoneInfo | None = None) -> QueueSpec | None:
    for spec in queue_specs(lab, tz):
        if spec.kind == kind:
            return spec
    return None


def default_kind(lab: Lab, tz: ZoneInfo | None = None) -> str:
    return queue_specs(lab, tz)[0].kind


def entries_for(lab: Lab, kind: str) -> list[QueueEntry]:
    return [entry for entry in lab.queue_entries if entry.queue_kind == kind]


def short_subject(lab: Lab) -> str:
    if is_itip_lab(lab):
        return ITIP_SHORT
    return lab.subject.name


def is_calendar_lab(lab: Lab) -> bool:
    return lab.external_id.startswith(CALENDAR_ID_PREFIX)


def session_starts_at(session: ItipSession, tz: ZoneInfo | None = None) -> datetime:
    tz = tz or MSK
    return datetime.combine(session.day, session.starts, tzinfo=tz)


async def _subject(session: AsyncSession) -> Subject:
    subject = await session.scalar(select(Subject).where(Subject.name == ITIP_SUBJECT))
    if subject is None:
        subject = Subject(name=ITIP_SUBJECT)
        session.add(subject)
        await session.flush()
    return subject


def _same_day(lab: Lab, day: date, tz: ZoneInfo) -> bool:
    return local_day(lab.starts_at, tz) == day


async def ensure_itip_labs(session: AsyncSession, tz: ZoneInfo | None = None) -> int:
    tz = tz or MSK
    subject = await _subject(session)
    labs = list(
        await session.scalars(
            select(Lab).options(selectinload(Lab.queue_entries)).where(Lab.subject_id == subject.id)
        )
    )
    created = 0
    for spec in ITIP_SESSIONS:
        matches = [lab for lab in labs if _same_day(lab, spec.day, tz)]
        if matches:
            keep = min(matches, key=lambda lab: (lab.cancelled, lab.id))
            keep.cancelled = False
            for extra in matches:
                if extra.id != keep.id and extra.external_id.startswith(CALENDAR_ID_PREFIX):
                    extra.cancelled = True
            for entry in keep.queue_entries:
                if entry.queue_kind == QUEUE_MAIN:
                    entry.queue_kind = QUEUE_CURRENT
            continue
        lab = Lab(
            external_id=f"{CALENDAR_ID_PREFIX}{spec.day.isoformat()}",
            subject_id=subject.id,
            starts_at=as_utc(session_starts_at(spec, tz)),
            classroom=None,
            teacher=ITIP_TEACHER,
            max_seats=DEFAULT_LAB_SEATS,
        )
        session.add(lab)
        labs.append(lab)
        created += 1
    await session.flush()
    return created
