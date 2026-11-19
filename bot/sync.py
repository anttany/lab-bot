from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import DEFAULT_LAB_SEATS
from bot.models import Lab, QueueEntry, Subject
from bot.queue import as_utc
from bot.schedule_source import LabDraft, ScheduleSource


async def _subject_by_name(session: AsyncSession, name: str) -> Subject:
    subject = await session.scalar(select(Subject).where(Subject.name == name))
    if subject is None:
        subject = Subject(name=name)
        session.add(subject)
        await session.flush()
    return subject


def _lab_keep_score(lab: Lab) -> tuple[int, int, int]:
    queued = len(lab.queue_entries)
    posted = 1 if lab.posted_at is not None else 0
    return (queued, posted, -lab.id)


async def _lab_for_draft(session: AsyncSession, draft: LabDraft, subject: Subject, starts_at) -> Lab | None:
    candidates = list(
        await session.scalars(
            select(Lab).options(selectinload(Lab.queue_entries)).where(Lab.subject_id == subject.id)
        )
    )
    matches = [lab for lab in candidates if as_utc(lab.starts_at) == starts_at]
    by_external = await session.scalar(
        select(Lab).options(selectinload(Lab.queue_entries)).where(Lab.external_id == draft.external_id)
    )
    if by_external is not None and all(lab.id != by_external.id for lab in matches):
        matches.append(by_external)
    if not matches:
        return None
    keep = max(matches, key=_lab_keep_score)
    for extra in matches:
        if extra.id == keep.id:
            continue
        await _merge_queue(session, keep, extra)
        extra.cancelled = True
        extra.external_id = f"cancelled-{extra.id}"
        if keep.opened_at is None:
            keep.opened_at = extra.opened_at
        if keep.posted_at is None:
            keep.posted_at = extra.posted_at
        if keep.notified_at is None:
            keep.notified_at = extra.notified_at
    await session.flush()
    return keep


async def _merge_queue(session: AsyncSession, keep: Lab, extra: Lab) -> None:
    taken = {entry.place for entry in keep.queue_entries}
    keep_users = {entry.user_id for entry in keep.queue_entries}
    extras = list(
        await session.scalars(select(QueueEntry).where(QueueEntry.lab_id == extra.id))
    )
    for entry in extras:
        if entry.user_id in keep_users:
            continue
        if entry.place in taken:
            continue
        entry.lab_id = keep.id
        taken.add(entry.place)
        keep_users.add(entry.user_id)
        keep.queue_entries.append(entry)


async def sync_schedule(session: AsyncSession, source: ScheduleSource) -> tuple[int, int]:
    drafts = await source.fetch()
    seen_external: set[str] = set()
    seen_keys: set[tuple[int, object]] = set()
    created = 0
    updated = 0

    for draft in drafts:
        seen_external.add(draft.external_id)
        subject = await _subject_by_name(session, draft.subject)
        starts_at = as_utc(draft.starts_at)
        seen_keys.add((subject.id, starts_at))
        lab = await _lab_for_draft(session, draft, subject, starts_at)
        if lab is None:
            session.add(
                Lab(
                    external_id=draft.external_id,
                    subject_id=subject.id,
                    starts_at=starts_at,
                    classroom=draft.classroom,
                    teacher=draft.teacher,
                    max_seats=DEFAULT_LAB_SEATS,
                )
            )
            created += 1
            continue
        lab.external_id = draft.external_id
        lab.subject_id = subject.id
        lab.starts_at = starts_at
        lab.classroom = draft.classroom
        lab.teacher = draft.teacher
        lab.max_seats = DEFAULT_LAB_SEATS
        lab.cancelled = False
        updated += 1

    existing = await session.scalars(
        select(Lab).options(selectinload(Lab.subject)).where(Lab.cancelled.is_(False))
    )
    for lab in existing:
        if lab.external_id.startswith("test-"):
            continue
        key = (lab.subject_id, as_utc(lab.starts_at))
        if lab.external_id not in seen_external and key not in seen_keys:
            lab.cancelled = True
            updated += 1

    await session.flush()
    return created, updated
