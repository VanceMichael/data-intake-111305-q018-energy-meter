"""把供方来源记录规范化为结构化读数消息。

只做字段级校验与规范化，不写库、不推断当前值、不补默认倍率。
身份解析、倍率核对、修订归并都在台账层完成，保证本模块纯函数可测。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .rules import (
    QUALITY_VALUES,
    canonical_json,
    parse_decimal,
    parse_timestamp,
)

IDENTITY_FIELDS = ("provider", "meter", "reading", "revision")


@dataclass(frozen=True)
class ReadingMessage:
    provider: str
    meter_label: str
    reading_key: str
    revision: int
    observed_at: str
    period: str
    register_value: Decimal
    quality: str
    declared_factor: Decimal | None
    payload_digest: str
    raw: dict[str, Any]

    @property
    def message_key(self) -> tuple[str, str, str, int]:
        return (self.provider, self.meter_label, self.reading_key, self.revision)


def normalize_record(raw: dict[str, Any]) -> ReadingMessage:
    if not isinstance(raw, dict):
        raise ValueError("source record must be an object")
    missing = [name for name in IDENTITY_FIELDS if name not in raw]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    provider = _text(raw, "provider")
    meter_label = _text(raw, "meter")
    reading_key = _text(raw, "reading")
    revision = raw["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision must be a positive integer")
    observed_at = parse_timestamp(raw.get("at"), "at")
    period = _text(raw, "period")
    _check_period(period, observed_at)
    register_value = parse_decimal(raw.get("value"), "value", minimum=Decimal(0))
    quality = _text(raw, "quality")
    if quality not in QUALITY_VALUES:
        raise ValueError("quality must be one of: " + ", ".join(sorted(QUALITY_VALUES)))
    declared_factor = None
    if raw.get("multiplier") is not None:
        declared_factor = parse_decimal(raw.get("multiplier"), "multiplier", positive=True)
    canonical_raw = canonical_json(raw)
    return ReadingMessage(
        provider=provider,
        meter_label=meter_label,
        reading_key=reading_key,
        revision=revision,
        observed_at=observed_at.isoformat().replace("+00:00", "Z"),
        period=period,
        register_value=register_value,
        quality=quality,
        declared_factor=declared_factor,
        payload_digest=hashlib_sha(canonical_raw),
        raw=raw,
    )


def normalize_batch(records: list[dict[str, Any]]) -> list[ReadingMessage]:
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise ValueError("records must be a list of objects")
    return [normalize_record(item) for item in records]


def quality_label(quality: str) -> str:
    from .rules import QUALITY_ESTIMATED, QUALITY_MEASURED

    return {QUALITY_MEASURED: "实测", QUALITY_ESTIMATED: "估算"}.get(quality, quality)


def _text(raw: dict, field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + " must be a non-empty string")
    return value.strip()


def _check_period(period: str, observed_at) -> None:
    # 账期形如 2026-10；读数时间应落在该自然月内，防止串月报文静默写错账期。
    year_text, separator, month_text = period.partition("-")
    if (separator != "-" or len(year_text) != 4 or len(month_text) != 2
            or not (year_text + month_text).isdigit()):
        raise ValueError("period must use YYYY-MM: " + period)
    year, month = int(year_text), int(month_text)
    if not 1 <= month <= 12 or (year, month) != (observed_at.year, observed_at.month):
        raise ValueError("period does not match reading timestamp: " + period)


def hashlib_sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "ReadingMessage",
    "normalize_record",
    "normalize_batch",
    "quality_label",
]
