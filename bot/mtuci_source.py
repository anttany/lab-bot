from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from mtuci_private_api import Mtuci
from mtuci_private_api.http.client_factory.base_client import BaseHttpClientFactory

from bot.config import Settings
from bot.schedule_source import LabDraft

logger = logging.getLogger(__name__)

LAB_TYPE = "Лабораторные работы"
TIMETABLE_URL = "https://lk.mtuci.ru/api/timetable/get"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


class ProxyHttpClientFactory(BaseHttpClientFactory):
    def __init__(self, proxy: str | None) -> None:
        self.proxy = proxy or None

    def _get_httpx_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "headers": BROWSER_HEADERS,
            "follow_redirects": True,
            "timeout": 60.0,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.AsyncClient(**kwargs)


class MtuciScheduleSource:
    def __init__(self, settings: Settings) -> None:
        if not settings.mtuci_login or not settings.mtuci_password:
            raise ValueError("Задайте MTUCI_LOGIN и MTUCI_PASSWORD в .env")
        self.login = settings.mtuci_login
        self.password = settings.mtuci_password
        self.group = settings.mtuci_group.strip() or None
        self.proxy = settings.mtuci_proxy.strip() or None
        self.months_ahead = max(0, settings.mtuci_months_ahead)
        self.tz = ZoneInfo(settings.timezone)

    async def fetch(self) -> list[LabDraft]:
        factory = ProxyHttpClientFactory(self.proxy)
        client = Mtuci(self.login, self.password, client_factory=factory)
        session = client.client.session
        try:
            await client.auth()
            user = await client.get_user_info()
            group = self.group or user.group
            logger.info("ЛК МТУСИ: %s, группа %s", user.name, group)
            drafts = await self._labs_for_months(session, group)
            logger.info("Лабораторных из ЛК: %s", len(drafts))
            return drafts
        finally:
            await session.aclose()

    async def _labs_for_months(self, session: httpx.AsyncClient, group: str) -> list[LabDraft]:
        now = datetime.now(self.tz)
        drafts: dict[str, LabDraft] = {}
        for offset in range(self.months_ahead + 1):
            month_index = (now.month - 1 + offset) % 12
            payload = await self._month_payload(session, group, month_index)
            for draft in self._parse_labs(payload):
                drafts[draft.external_id] = draft
        return list(drafts.values())

    async def _month_payload(self, session: httpx.AsyncClient, group: str, month_index: int) -> dict:
        response = await session.get(
            TIMETABLE_URL,
            params={"value": group, "month": month_index, "type": "group"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"ЛК вернул ошибку расписания: {payload}")
        return payload

    def _parse_labs(self, payload: dict) -> list[LabDraft]:
        days = payload.get("data", {}).get("days", {}) or {}
        drafts: list[LabDraft] = []
        for day_key, lessons in days.items():
            for lesson in lessons or []:
                if lesson.get("UF_TYPE") != LAB_TYPE:
                    continue
                drafts.append(self._to_draft(day_key, lesson))
        return drafts

    def _to_draft(self, day_key: str, lesson: dict) -> LabDraft:
        day = datetime.strptime(day_key, "%d.%m.%Y")
        start = datetime.strptime(str(lesson.get("UF_TIME_START") or "00:00"), "%H:%M")
        starts_at = datetime(
            day.year,
            day.month,
            day.day,
            start.hour,
            start.minute,
            tzinfo=self.tz,
        )
        lesson_id = lesson.get("ID") or f"{day_key}-{lesson.get('UF_TIME_START')}-{lesson.get('UF_DISCIPLINE')}"
        classrooms = _join(lesson.get("UF_AUDIENCE"))
        teachers = _join(lesson.get("UF_TEACHER"))
        return LabDraft(
            external_id=f"mtuci-{lesson_id}",
            subject=str(lesson.get("UF_DISCIPLINE") or "Лабораторная").strip(),
            starts_at=starts_at,
            classroom=classrooms,
            teacher=teachers,
        )


def _join(value: object) -> str | None:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(parts) or None
    if value:
        text = str(value).strip()
        return text or None
    return None
