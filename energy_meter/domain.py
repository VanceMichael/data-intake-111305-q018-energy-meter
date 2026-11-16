"""表计读数的领域规范化与适用规则。

本模块只处理供方报文记录的结构化解释，不接触原始归档，也不计算费用。
所有规则带版本号；签发快照与调整建议在生成时固化当时的规则版本，
之后规则升级不改变历史依据。
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MEASURED = "measured"
ESTIMATED = "estimated"
QUALITIES = (MEASURED, ESTIMATED)

# 规则标识与版本一并固化进快照、建议的依据中，财务可从差异回到适用规则。
RULE_SERIES = "R-SERIES-1"
RULE_QUALITY = "R-QUALITY-1"
RULE_MULTIPLIER = "R-MULTIPLIER-1"
RULE_REVISION = "R-REVISION-1"
RULE_ADJUSTMENT = "R-ADJUSTMENT-1"
RULE_ADJUSTMENT_CHAIN = "R-ADJUSTMENT-CHAIN-1"

RULE_CATALOG = {
    RULE_SERIES: {
        "version": 1,
        "title": "表计序列边界",
        "rule": "表计序列由（供方、设备编号、读数标识）共同界定；跨月换表或设备编号复用"
                "产生新序列，旧表尾数与新表首数分别保留，禁止跨序列拼接同一条累计曲线。",
    },
    RULE_QUALITY: {
        "version": 1,
        "title": "读数质量与当前解释",
        "rule": "每条事实保留供方声明的 measured/estimated 质量；同一读数标识下当前解释"
                "取最高修订号的有效事实，估算被实测更正时质量差异可追溯。",
    },
    RULE_MULTIPLIER: {
        "version": 1,
        "title": "倍率生效区间与待核",
        "rule": "倍率按观测时间适用对应生效区间；供方未给倍率即为待核，禁止默认按一倍率"
                "出账，待核读数的账单影响挂起，直到倍率补录并复核。",
    },
    RULE_REVISION: {
        "version": 1,
        "title": "乱序补传与不可变快照",
        "rule": "补传与更正允许乱序到达，结构化事实只追加、当前解释可更新；已签发快照及其"
                "当时依据（事实、倍率、规则版本）冻结，任何后续投递不得改写。",
    },
    RULE_ADJUSTMENT: {
        "version": 1,
        "title": "调整建议幂等",
        "rule": "每个签发快照同时只允许存在一笔未决调整建议；重复投递同一更正不得生成第二笔，"
                "建议与所依据的事实修订绑定。",
    },
    RULE_ADJUSTMENT_CHAIN: {
        "version": 1,
        "title": "调整链与逐次差额",
        "rule": "调整被财务接受后再次更正，新建议必须引用前次已接受建议；新差额按当前解释与"
                "前次调整后口径之差计算，逐次链接，不覆盖历史调整。",
    },
}

CURRENT_RULE_VERSIONS = {key: entry["version"] for key, entry in RULE_CATALOG.items()}


class ReadingRecordError(ValueError):
    """供方记录无法结构化为读数事实。"""


@dataclass(frozen=True)
class ReadingRecord:
    provider: str
    meter_code: str
    reading_id: str
    revision: int
    observed_at: str
    period: str
    register_value: Decimal
    quality: str
    multiplier: Decimal | None
    raw: dict[str, Any]

    @property
    def series_key(self) -> tuple[str, str, str]:
        return self.provider, self.meter_code, self.reading_id


def _text(raw: dict[str, Any], name: str) -> str:
    if name not in raw or raw[name] is None:
        raise ReadingRecordError(f"missing field: {name}")
    value = str(raw[name]).strip()
    if not value:
        raise ReadingRecordError(f"empty field: {name}")
    return value


def _decimal(raw: dict[str, Any], name: str, *, allow_none: bool = False) -> Decimal | None:
    if name not in raw or raw[name] is None:
        if allow_none:
            return None
        raise ReadingRecordError(f"missing field: {name}")
    if isinstance(raw[name], bool):
        raise ReadingRecordError(f"invalid numeric field: {name}")
    try:
        value = Decimal(str(raw[name]).strip())
    except (InvalidOperation, AttributeError):
        raise ReadingRecordError(f"invalid numeric field: {name}") from None
    if not value.is_finite():
        raise ReadingRecordError(f"invalid numeric field: {name}")
    return value


def _parse_at(value: str) -> str:
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ReadingRecordError("invalid timestamp: at") from None
    if parsed.tzinfo is None:
        raise ReadingRecordError("timestamp must carry timezone: at")
    return value.strip()


def _parse_period(value: str) -> str:
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m")
    except ValueError:
        raise ReadingRecordError("period must use YYYY-MM") from None
    return text


def normalize_reading(raw: dict[str, Any]) -> ReadingRecord:
    """把一条供方报文记录校验、规范化为结构化读数。

    未知倍率保留为 ``None``（待核），绝不在这里默认为 1。
    """
    provider = _text(raw, "provider")
    meter_code = _text(raw, "meter")
    reading_id = _text(raw, "reading")

    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ReadingRecordError("revision must be a positive integer")

    observed_at = _parse_at(_text(raw, "at"))
    period = _parse_period(_text(raw, "period"))
    register_value = _decimal(raw, "value")
    if register_value < 0:
        raise ReadingRecordError("register value must not be negative")

    quality = _text(raw, "quality")
    if quality not in QUALITIES:
        raise ReadingRecordError(f"quality must be one of: {', '.join(QUALITIES)}")

    multiplier = _decimal(raw, "multiplier", allow_none=True)
    if multiplier is not None and multiplier <= 0:
        raise ReadingRecordError("multiplier must be positive when provided")

    return ReadingRecord(
        provider=provider,
        meter_code=meter_code,
        reading_id=reading_id,
        revision=revision,
        observed_at=observed_at,
        period=period,
        register_value=register_value,
        quality=quality,
        multiplier=multiplier,
        raw=dict(raw),
    )


def rule_snapshot(rule_ids=None) -> dict[str, int]:
    """生成当时适用规则的版本快照，供签发与调整建议固化依据。"""
    ids = rule_ids or tuple(CURRENT_RULE_VERSIONS)
    return {rule_id: CURRENT_RULE_VERSIONS[rule_id] for rule_id in ids}
