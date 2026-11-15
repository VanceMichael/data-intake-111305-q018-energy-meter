"""适用规则的版本化管理：表计安装身份区间与倍率生效区间。

规则是结构化解释的依据之一，每次发布生成不可变版本，解释与快照都引用
具体规则版本。规则只回答两个问题：

* 某时刻供方声明的表计编号对应哪个内部安装身份（设备编号可被复用）；
* 某安装身份在某时刻适用的倍率是多少（未知即为待核，不默认成一）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

QUALITY_MEASURED = "measured"
QUALITY_ESTIMATED = "estimated"
QUALITY_VALUES = frozenset((QUALITY_MEASURED, QUALITY_ESTIMATED))

MULTIPLIER_KNOWN = "known"
MULTIPLIER_PENDING = "pending"

IDENTITY_RESOLVED = "resolved"
IDENTITY_PENDING = "identity_pending"


class RuleConflict(ValueError):
    """规则内容自相矛盾，例如区间重叠或倍率非法。"""


def canonical_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + " must be a non-empty ISO 8601 timestamp")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid " + field + ": " + value) from exc


def parse_decimal(value, field: str, *, minimum: Decimal | None = None,
                  positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(field + " must be a decimal string")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("invalid " + field + ": " + repr(value)) from exc
    if not number.is_finite():
        raise ValueError(field + " must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(field + " must be >= " + str(minimum))
    if positive and number <= 0:
        raise ValueError(field + " must be positive")
    return number


@dataclass(frozen=True)
class MeterInterval:
    provider: str
    meter_label: str
    install_id: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class MultiplierInterval:
    install_id: str
    factor: Decimal
    valid_from: datetime
    valid_to: datetime | None


class RuleBook:
    """一个不可变的规则版本。"""

    def __init__(self, rule_set: str, version: int, published_at: str,
                 content: dict, digest: str):
        self.rule_set = rule_set
        self.version = version
        self.published_at = published_at
        self.content = content
        self.digest = digest
        self.id = rule_set + "@" + str(version)
        self._meters = self._build_meters(content)
        self._multipliers = self._build_multipliers(content)

    @classmethod
    def create(cls, content: dict, *, rule_set: str = "default",
               version: int = 1, published_at: str | None = None) -> "RuleBook":
        if not isinstance(content, dict):
            raise ValueError("rule content must be an object")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("rule version must be a positive integer")
        if not isinstance(rule_set, str) or not rule_set.strip():
            raise ValueError("rule_set must be a non-empty string")
        published_at = published_at or utc_now()
        parse_timestamp(published_at, "published_at")
        meters = cls._build_meters(content)
        multipliers = cls._build_multipliers(content)
        known_installs = {item.install_id for item in meters}
        for item in multipliers:
            if item.install_id not in known_installs:
                raise RuleConflict("multiplier references unknown install: " + item.install_id)
        return cls(rule_set.strip(), version, published_at,
                   json.loads(canonical_json(content)), digest_of(content))

    @staticmethod
    def _build_meters(content) -> tuple[MeterInterval, ...]:
        raw_items = content.get("meters", [])
        if not isinstance(raw_items, list) or not raw_items:
            raise RuleConflict("rules must declare at least one meter interval")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise RuleConflict("meter entry must be an object")
            provider = _required_text(raw, "provider")
            meter_label = _required_text(raw, "meter")
            install_id = _required_text(raw, "install")
            valid_from = parse_timestamp(raw.get("valid_from"), "valid_from")
            valid_to = raw.get("valid_to")
            valid_to = None if valid_to is None else parse_timestamp(valid_to, "valid_to")
            if valid_to is not None and valid_to <= valid_from:
                raise RuleConflict("meter interval must be half-open: " + install_id)
            items.append(MeterInterval(provider, meter_label, install_id, valid_from, valid_to))
        _assert_no_overlap(
            items, lambda item: (item.provider, item.meter_label),
            "meter label is reused in overlapping intervals",
        )
        return tuple(sorted(items, key=lambda item: (item.provider, item.meter_label, item.valid_from)))

    @staticmethod
    def _build_multipliers(content) -> tuple[MultiplierInterval, ...]:
        raw_items = content.get("multipliers", [])
        if not isinstance(raw_items, list):
            raise RuleConflict("multipliers must be a list")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise RuleConflict("multiplier entry must be an object")
            install_id = _required_text(raw, "install")
            factor = parse_decimal(raw.get("factor"), "factor", positive=True)
            valid_from = parse_timestamp(raw.get("valid_from"), "valid_from")
            valid_to = raw.get("valid_to")
            valid_to = None if valid_to is None else parse_timestamp(valid_to, "valid_to")
            if valid_to is not None and valid_to <= valid_from:
                raise RuleConflict("multiplier interval must be half-open: " + install_id)
            items.append(MultiplierInterval(install_id, factor, valid_from, valid_to))
        _assert_no_overlap(
            items, lambda item: (item.install_id,),
            "multiplier intervals overlap for one install",
        )
        return tuple(sorted(items, key=lambda item: (item.install_id, item.valid_from)))

    def resolve_install(self, provider: str, meter_label: str,
                        at: datetime) -> str | None:
        """按声明时刻解析安装身份；查不到就是待核，绝不猜测复用关系。"""
        for item in self._meters:
            if (item.provider == provider and item.meter_label == meter_label
                    and item.valid_from <= at
                    and (item.valid_to is None or at < item.valid_to)):
                return item.install_id
        return None

    def resolve_factor(self, install_id: str, at: datetime) -> Decimal | None:
        """返回已确认倍率；没有生效区间返回 None（待核，调用方不得补一）。"""
        for item in self._multipliers:
            if (item.install_id == install_id and item.valid_from <= at
                    and (item.valid_to is None or at < item.valid_to)):
                return item.factor
        return None

    def meter_intervals(self) -> tuple[MeterInterval, ...]:
        return self._meters


def _required_text(raw: dict, field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuleConflict(field + " is required")
    return value.strip()


def _assert_no_overlap(items, key, message: str) -> None:
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(items, key=lambda item: (key(item), item.valid_from))
    for previous, current in zip(ordered, ordered[1:]):
        if key(previous) != key(current):
            continue
        previous_end = previous.valid_to or far_future
        if current.valid_from < previous_end:
            raise RuleConflict(message + ": " + repr(key(current)))
