from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from bot.config import Settings


@dataclass(slots=True)
class LabDraft:
    external_id: str
    subject: str
    starts_at: datetime
    classroom: str | None = None
    teacher: str | None = None
    max_seats: int | None = None


class ScheduleSource(Protocol):
    async def fetch(self) -> list[LabDraft]:
        ...


def _parse_payload(payload: dict) -> list[LabDraft]:
    drafts: list[LabDraft] = []
    for item in payload.get("labs", []):
        drafts.append(
            LabDraft(
                external_id=str(item["id"]),
                subject=str(item["subject"]).strip(),
                starts_at=datetime.fromisoformat(item["starts_at"]),
                classroom=(str(item["classroom"]).strip() if item.get("classroom") else None),
                teacher=(str(item["teacher"]).strip() if item.get("teacher") else None),
                max_seats=item.get("max_seats"),
            )
        )
    return drafts


class FileScheduleSource:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    async def fetch(self) -> list[LabDraft]:
        import json

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return _parse_payload(payload)


class UrlJsonScheduleSource:
    def __init__(self, url: str) -> None:
        self.url = url

    async def fetch(self) -> list[LabDraft]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            return _parse_payload(response.json())


def build_schedule_source(settings: Settings) -> ScheduleSource:
    source = settings.schedule_source.lower().strip()
    if source == "url":
        if not settings.schedule_url:
            raise ValueError("SCHEDULE_URL пуст")
        return UrlJsonScheduleSource(settings.schedule_url)
    if source == "mtuci":
        from bot.mtuci_source import MtuciScheduleSource

        return MtuciScheduleSource(settings)
    return FileScheduleSource(settings.schedule_path)
