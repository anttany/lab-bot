from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.models import Lab, Subject
from bot.queue import as_utc
from bot.schedule_source import ScheduleSource


async def _subject_by_name(session: AsyncSession, name: str) -> Subject:
    subject = await session.scalar(select(Subject).where(Subject.name == name))
    if subject is None:
        subject = Subject(name=name)
        session.add(subject)
        await session.flush()
    return subject


async def sync_schedule(session: AsyncSession, source: ScheduleSource) -> tuple[int, int]:
    drafts = await source.fetch()
    seen: set[str] = set()
    created = 0
    updated = 0

    for draft in drafts:
        seen.add(draft.external_id)
        subject = await _subject_by_name(session, draft.subject)
        starts_at = as_utc(draft.starts_at)
        lab = await session.scalar(select(Lab).where(Lab.external_id == draft.external_id))
        if lab is None:
            lab = Lab(
                external_id=draft.external_id,
                subject_id=subject.id,
                starts_at=starts_at,
                classroom=draft.classroom,
                teacher=draft.teacher,
                max_seats=draft.max_seats,
            )
            session.add(lab)
            created += 1
            continue
        lab.subject_id = subject.id
        lab.starts_at = starts_at
        lab.classroom = draft.classroom
        lab.teacher = draft.teacher
        lab.max_seats = draft.max_seats
        lab.cancelled = False
        updated += 1

    incoming_ids = {draft.external_id for draft in drafts}
    existing = await session.scalars(
        select(Lab).options(selectinload(Lab.subject)).where(Lab.cancelled.is_(False))
    )
    for lab in existing:
        if lab.external_id.startswith("test-"):
            continue
        if lab.external_id not in incoming_ids:
            lab.cancelled = True
            updated += 1

    await session.flush()
    return created, updated
