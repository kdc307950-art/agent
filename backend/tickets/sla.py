"""SLA 业务日历计算（工作日/工作时段/节假日感知）。

职责：
    - BusinessCalendar：把「工作日 + 上下班时段 + 节假日」封装成一个业务时间日历
    - add_business_minutes：从某个时刻起累加 N 个「业务分钟」，跳过非工作时间
      （下班后、周末、节假日），得到 SLA 截止时间

关键设计：
    - 所有计算基于租户配置的时区（timezone_name），对外统一返回 UTC
    - dataclass frozen=True + slots=True：不可变、省内存，供并发扫描安全复用
    - 时间算术不依赖第三方库（如 business-duration），纯 datetime 实现，逻辑显式可控
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    """业务日历：决定 SLA 的「有效工作时间」。

    business_days 用 ISO weekday 集合表示（0=周一 ... 6=周日）；
    work_start/work_end 是每日工作时段；holidays 为额外停摆日期。
    构造时校验配置合法性，避免脏数据进入 SLA 计算。
    """

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
        """返回 value 之后（含等于）的第一个「业务时刻」。

        若 value 在业务时段内则原样返回；否则跳到下一个工作日的工作开始时刻。
        上限 370 天防止配置错误导致死循环，超过则视为无法排程。
        """
        local = self._aware(value).astimezone(self.tz)
        day = local.date()
        for _ in range(370):
            if self.is_business_day(day):
                start = datetime.combine(day, self.work_start, self.tz)
                end = datetime.combine(day, self.work_end, self.tz)
                if local < start:
                    return start.astimezone(UTC)
                if local < end:
                    return local.astimezone(UTC)
            day += timedelta(days=1)
            local = datetime.combine(day, time.min, self.tz)
        raise RuntimeError("无法在一年内找到下一个工作时间")

    def add_business_minutes(self, started_at: datetime, minutes: int) -> datetime:
        """从 started_at 起累加 minutes 个业务分钟，返回 UTC 截止时间。

        算法：先落到业务时刻，再按「当天剩余业务分钟」逐日扣减；
        当天不够则顺延到下一个业务日的开始继续。minutes 为 0 时返回
        下一个业务时刻本身（保证截止时间落在工作时段内）。
        """
        if minutes < 0:
            raise ValueError("业务分钟不能为负数")
        current = self.next_business_instant(started_at)
        remaining = minutes
        while remaining > 0:
            local = current.astimezone(self.tz)
            end = datetime.combine(local.date(), self.work_end, self.tz)
            available = max(0, int((end - local).total_seconds() // 60))
            if remaining <= available:
                return (local + timedelta(minutes=remaining)).astimezone(UTC)
            remaining -= available
            current = self.next_business_instant(end + timedelta(microseconds=1))
        return current

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("SLA 时间必须包含时区")
        return value
