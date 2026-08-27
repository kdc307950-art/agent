"""Business-calendar SLA calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    timezone_name: str
    business_days: frozenset[int]
    work_start: time
    work_end: time
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("未知 SLA 时区") from exc
        if not self.business_days or any(day < 0 or day > 6 for day in self.business_days):
            raise ValueError("business_days 必须使用 0 到 6 的 ISO weekday")
        if self.work_end <= self.work_start:
            raise ValueError("工作结束时间必须晚于开始时间")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def is_business_day(self, value: date) -> bool:
        return value.weekday() in self.business_days and value not in self.holidays

    def next_business_instant(self, value: datetime) -> datetime:
        local = self._aware(value).astimezone(self.tz)
        day = local.date()
        for _ in range(370):
            if self.is_business_day(day):
                start = datetime.combine(day, self.work_start, self.tz)
                end = datetime.combine(day, self.work_end, self.tz)
                if local < start:
                    return start.astimezone(timezone.utc)
                if local < end:
                    return local.astimezone(timezone.utc)
            day += timedelta(days=1)
            local = datetime.combine(day, time.min, self.tz)
        raise RuntimeError("无法在一年内找到下一个工作时间")

    def add_business_minutes(self, started_at: datetime, minutes: int) -> datetime:
        if minutes < 0:
            raise ValueError("业务分钟不能为负数")
        current = self.next_business_instant(started_at)
        remaining = minutes
        while remaining > 0:
            local = current.astimezone(self.tz)
            end = datetime.combine(local.date(), self.work_end, self.tz)
            available = max(0, int((end - local).total_seconds() // 60))
            if remaining <= available:
                return (local + timedelta(minutes=remaining)).astimezone(timezone.utc)
            remaining -= available
            current = self.next_business_instant(end + timedelta(microseconds=1))
        return current

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("SLA 时间必须包含时区")
        return value
