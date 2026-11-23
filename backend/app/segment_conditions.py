"""圈选条件类型化：文本 / 布尔 / 缺失属性 / 旧版字符串包含。

这个模块只放条件语义和迁移规则，不依赖业务模型，方便事件匹配（EventTrack）
和人群圈选（Segment）共用同一套判断逻辑。

关键语义
--------
旧版条件（legacy_v1）
    ``{"vip": "1"}`` 表示 ``str(用户属性.get("vip", ""))`` 包含子串 ``"1"``。
    因此 ``vip`` 为 ``"1"``、``"10"``（甚至整数 ``1``）都会命中；
    属性缺失时按空串处理，只有旧条件值为空串才会命中缺失属性。

新版条件（typed_v1）
    - 文本：字段值必须是真正的字符串；缺失、数字、布尔都不命中。
    - 布尔：字段值必须是真正的 ``bool``，且与条件值一致；
      整数 ``1``、字符串 ``"1"`` / ``"true"``、缺失属性都不能冒充 ``True``。
    - 缺失属性：任何新版条件都不命中缺失属性。

执行 gate
    新版条件执行前必须通过字段类型校验。字段类型缺失（缺证）时旧条件仍可执行，
    但必须保留原文本和待核原因，不能把缺证字段悄悄当成某个类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping


# ============================================================
# 枚举与异常
# ============================================================

class FieldType(str, Enum):
    TEXT = "text"
    BOOLEAN = "boolean"


class ConditionKind(str, Enum):
    LEGACY_CONTAINS = "legacy_contains"   # 人群圈选的旧语义：字符串包含
    LEGACY_EQUALS = "legacy_equals"       # 事件匹配的旧语义：精确相等
    TEXT_EQUALS = "text_equals"
    TEXT_CONTAINS = "text_contains"
    BOOLEAN_EQUALS = "boolean_equals"


class ConditionVersion(str, Enum):
    LEGACY_V1 = "legacy_v1"
    TYPED_V1 = "typed_v1"

    @classmethod
    def parse(cls, value: "ConditionVersion | str | None", default: "ConditionVersion") -> "ConditionVersion":
        if value is None or value == "":
            return default
        return value if isinstance(value, cls) else cls(str(value))


class ConditionError(ValueError):
    """条件或版本不满足执行要求。"""


class ConditionValidationError(ConditionError):
    """字段类型校验失败，新版条件不能执行。"""

    def __init__(self, issues: Iterable["ConditionIssue"]) -> None:
        self.issues = tuple(issues)
        detail = "；".join(issue.detail for issue in self.issues) or "条件校验失败"
        super().__init__(f"条件不可执行：{detail}")


class VersionConflictError(ConditionError):
    """同一次比较的两组条件混用了版本。"""


class FieldTypeMissingError(ConditionValidationError):
    """字段类型缺证：保留原文本和待核原因，等待补证。"""


# ============================================================
# 条件、校验问题、执行结果
# ============================================================

@dataclass(frozen=True)
class ConditionIssue:
    """字段类型缺证或非法时保留的证据。"""

    field: str
    reason: str
    raw_text: str = ""

    @property
    def detail(self) -> str:
        suffix = f"（原文本：{self.raw_text}）" if self.raw_text else ""
        return f"字段 {self.field} {self.reason}{suffix}"


@dataclass(frozen=True)
class TypedCondition:
    """一条圈选条件。``raw_text`` 始终保留人工填写的原文，便于缺证时回看。"""

    field: str
    kind: ConditionKind
    value: Any
    raw_text: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "kind": self.kind.value,
            "value": self.value,
            "raw_text": self.raw_text,
        }

    def evaluate(self, attributes: Mapping[str, Any]) -> bool:
        present = self.field in attributes
        raw = attributes.get(self.field)
        if self.kind == ConditionKind.LEGACY_CONTAINS:
            return self.value in str(raw if present else "")
        if self.kind == ConditionKind.LEGACY_EQUALS:
            return present and str(raw) == str(self.value)
        if not present:
            # 新版条件一律不命中缺失属性。
            return False
        if self.kind == ConditionKind.TEXT_EQUALS:
            return isinstance(raw, str) and raw == self.value
        if self.kind == ConditionKind.TEXT_CONTAINS:
            return isinstance(raw, str) and str(self.value) in raw
        if self.kind == ConditionKind.BOOLEAN_EQUALS:
            return isinstance(raw, bool) and raw is self.value
        raise ConditionError(f"未知条件类型：{self.kind}")


@dataclass
class ConditionSet:
    """一组按 AND 组合的条件，自身记录版本和原始文本。"""

    conditions: list[TypedCondition] = field(default_factory=list)
    version: ConditionVersion = ConditionVersion.TYPED_V1

    def __len__(self) -> int:
        return len(self.conditions)

    @property
    def fields(self) -> list[str]:
        return [c.field for c in self.conditions]

    def evaluate(self, attributes: Mapping[str, Any]) -> bool:
        return all(c.evaluate(attributes) for c in self.conditions)

    def describe(self) -> list[dict[str, Any]]:
        return [c.describe() for c in self.conditions]


@dataclass
class ConditionCheck:
    """一次字段类型校验的结果，同时供事件匹配和圈选执行读取。"""

    version: ConditionVersion
    field_types: dict[str, FieldType] = field(default_factory=dict)
    issues: list[ConditionIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def pending_evidence(self) -> list[str]:
        return [issue.field for issue in self.issues]

    def require_executable(self) -> "ConditionCheck":
        """新版执行的 gate：字段类型必须齐全。旧版只告警不阻断。"""
        if self.version == ConditionVersion.TYPED_V1 and self.issues:
            raise FieldTypeMissingError(self.issues)
        return self

    def describe(self) -> dict[str, Any]:
        return {
            "version": self.version.value,
            "ok": self.ok,
            "field_types": {k: v.value for k, v in sorted(self.field_types.items())},
            "pending_evidence": [
                {"field": i.field, "reason": i.reason, "raw_text": i.raw_text}
                for i in self.issues
            ],
        }


@dataclass
class ConditionExecution:
    """条件执行结果：命中成员 + 用到的版本 + 校验证据。"""

    version: ConditionVersion
    conditions: ConditionSet
    matched: list[dict[str, Any]] = field(default_factory=list)
    check: ConditionCheck | None = None

    @property
    def member_count(self) -> int:
        return len(self.matched)


# ============================================================
# 字段类型 schema
# ============================================================

@dataclass
class FieldTypeSchema:
    """字段类型登记表；缺失的字段就是缺证字段，不允许推断。"""

    types: dict[str, FieldType] = field(default_factory=dict)
    raw_texts: dict[str, str] = field(default_factory=dict)
    missing_reason: str = "缺字段类型"

    def has(self, name: str) -> bool:
        return name in self.types

    def type_of(self, name: str) -> FieldType | None:
        return self.types.get(name)

    def with_fields(self, extra: Mapping[str, FieldType | str]) -> "FieldTypeSchema":
        merged = dict(self.types)
        for name, value in extra.items():
            merged[name] = value if isinstance(value, FieldType) else FieldType(str(value))
        return replace(self, types=merged)

    def merge(self, other: "FieldTypeSchema") -> "FieldTypeSchema":
        types = {**self.types, **other.types}
        raw_texts = {**self.raw_texts, **other.raw_texts}
        return FieldTypeSchema(types=types, raw_texts=raw_texts)

    def describe(self) -> dict[str, Any]:
        return {k: v.value for k, v in sorted(self.types.items())}


def check_conditions(
    conditions: ConditionSet | Iterable[TypedCondition] | None,
    schema: FieldTypeSchema,
    version: ConditionVersion | str | None = None,
) -> ConditionCheck:
    """按字段类型校验条件；缺证时记录原文本和待核原因，不猜测类型。"""
    if isinstance(conditions, ConditionSet):
        condition_list = conditions.conditions
        resolved_version = conditions.version
    else:
        condition_list = list(conditions or [])
        resolved_version = ConditionVersion.TYPED_V1
    resolved_version = ConditionVersion.parse(version, resolved_version)

    issues: list[ConditionIssue] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for condition in condition_list:
        if condition.field in seen:
            continue
        seen.add(condition.field)
        if condition.kind in (ConditionKind.LEGACY_CONTAINS, ConditionKind.LEGACY_EQUALS):
            continue  # 旧条件对字段类型不设限，缺失只影响新版迁移
        expected = _expected_field_type(condition)
        if expected is None:
            unknown.append(f"{condition.field}({condition.kind})")
            continue
        actual = schema.type_of(condition.field)
        if actual is None:
            issues.append(ConditionIssue(
                field=condition.field,
                reason=schema.missing_reason,
                raw_text=condition.raw_text or schema.raw_texts.get(condition.field, ""),
            ))
        elif actual != expected:
            issues.append(ConditionIssue(
                field=condition.field,
                reason=f"字段类型为 {actual.value}，条件需要 {expected.value}",
                raw_text=condition.raw_text,
            ))
    if unknown:
        raise ConditionError(f"未知条件类型：{'、'.join(sorted(unknown))}")
    return ConditionCheck(
        version=resolved_version,
        field_types={name: schema.types[name] for name in sorted(seen) if schema.has(name)},
        issues=issues,
    )


def _expected_field_type(condition: TypedCondition) -> FieldType | None:
    if condition.kind == ConditionKind.BOOLEAN_EQUALS:
        return FieldType.BOOLEAN
    if condition.kind in (ConditionKind.TEXT_EQUALS, ConditionKind.TEXT_CONTAINS):
        return FieldType.TEXT
    if condition.kind in (ConditionKind.LEGACY_CONTAINS, ConditionKind.LEGACY_EQUALS):
        return None  # 旧条件对字段类型不限制
    return None


# ============================================================
# 旧版 / 新版条件构造
# ============================================================

def coerce_boolean(raw: Any) -> bool | None:
    """只接受真正的布尔字面量；字符串 "true"、整数 1 一律不认。"""
    if isinstance(raw, bool):
        return raw
    return None


def legacy_condition(field_name: str, raw: Any) -> TypedCondition:
    return TypedCondition(
        field=field_name,
        kind=ConditionKind.LEGACY_CONTAINS,
        value="" if raw is None else str(raw),
        raw_text=str(raw),
    )


def typed_condition(field_name: str, field_type: FieldType | str, raw: Any) -> TypedCondition:
    """把新版条件值转成强类型；布尔条件绝不从字符串/整数推断。"""
    resolved = field_type if isinstance(field_type, FieldType) else FieldType(str(field_type))
    if resolved == FieldType.BOOLEAN:
        boolean = coerce_boolean(raw)
        if boolean is None:
            raise ConditionError(
                f"字段 {field_name} 是布尔类型，条件值 {raw!r} 不是布尔字面量"
            )
        return TypedCondition(
            field=field_name,
            kind=ConditionKind.BOOLEAN_EQUALS,
            value=boolean,
            raw_text=str(raw).lower() if isinstance(raw, bool) else str(raw),
        )
    if not isinstance(raw, str):
        raise ConditionError(f"字段 {field_name} 是文本类型，条件值必须是字符串")
    return TypedCondition(
        field=field_name,
        kind=ConditionKind.TEXT_EQUALS,
        value=raw,
        raw_text=raw,
    )


def legacy_conditions_from_mapping(
    mapping: Mapping[str, Any] | None,
    *,
    match: str = "contains",
) -> ConditionSet:
    """旧版条件集：人群圈选默认字符串包含，事件匹配使用 ``match="equals"``。"""
    conditions = []
    for key, value in (mapping or {}).items():
        if match == "equals":
            conditions.append(TypedCondition(
                field=key,
                kind=ConditionKind.LEGACY_EQUALS,
                value="" if value is None else str(value),
                raw_text=str(value),
            ))
        else:
            conditions.append(legacy_condition(key, value))
    return ConditionSet(conditions=conditions, version=ConditionVersion.LEGACY_V1)


def typed_conditions_from_mapping(
    mapping: Mapping[str, Any] | None,
    schema: FieldTypeSchema,
    *,
    string_contains: bool = False,
) -> ConditionSet:
    """按字段类型构造新版条件；缺证字段抛 FieldTypeMissingError，保留原文。"""
    conditions: list[TypedCondition] = []
    issues: list[ConditionIssue] = []
    for name, raw in (mapping or {}).items():
        expected = schema.type_of(name)
        if expected is None:
            issues.append(ConditionIssue(
                field=name,
                reason=schema.missing_reason,
                raw_text=raw if isinstance(raw, str) else str(raw),
            ))
            continue
        if expected == FieldType.BOOLEAN:
            boolean = coerce_boolean(raw)
            if boolean is None:
                raise ConditionError(
                    f"字段 {name} 是布尔类型，条件值 {raw!r} 不是布尔字面量"
                )
            conditions.append(TypedCondition(
                field=name,
                kind=ConditionKind.BOOLEAN_EQUALS,
                value=boolean,
                raw_text=str(raw).lower() if isinstance(raw, bool) else str(raw),
            ))
        elif string_contains:
            conditions.append(TypedCondition(
                field=name,
                kind=ConditionKind.TEXT_CONTAINS,
                value=str(raw),
                raw_text=str(raw),
            ))
        else:
            if not isinstance(raw, str):
                raise ConditionError(f"字段 {name} 是文本类型，条件值必须是字符串")
            conditions.append(TypedCondition(
                field=name,
                kind=ConditionKind.TEXT_EQUALS,
                value=raw,
                raw_text=raw,
            ))
    if issues:
        raise FieldTypeMissingError(issues)
    return ConditionSet(conditions=conditions, version=ConditionVersion.TYPED_V1)


# ============================================================
# 旧条件迁移到类型化条件
# ============================================================

@dataclass
class FieldMigration:
    """单个字段的迁移结论。``status`` 取值见 ``MIGRATION_DECISIONS``。"""

    field: str
    raw_text: str
    status: str
    reason: str
    legacy_value: str = ""
    typed_value: Any = None

    def describe(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "raw_text": self.raw_text,
            "status": self.status,
            "reason": self.reason,
            "legacy_value": self.legacy_value,
            "typed_value": self.typed_value,
        }


MIGRATION_DECISIONS = (
    "migrated_text",       # 文本字段：新条件改为等值匹配
    "migrated_boolean",    # 布尔字段：新条件改为布尔等值
    "needs_review",        # 缺字段类型或缺证原文：保留原文本条数
    "invalid",             # 条件值与字段类型不兼容
)


@dataclass
class MigrationReport:
    """一次旧条件迁移的完整证据，所有字段都保留原文本。"""

    blocked: bool
    decisions: list[FieldMigration] = field(default_factory=list)
    typed_conditions: ConditionSet | None = None
    pending_evidence: list[ConditionIssue] = field(default_factory=list)
    legacy_snapshot: list[dict[str, Any]] = field(default_factory=list)

    @property
    def version(self) -> ConditionVersion:
        return ConditionVersion.TYPED_V1

    @property
    def unverified_fields(self) -> list[str]:
        return [d.field for d in self.decisions if d.status != "migrated_text" and d.status != "migrated_boolean"]

    def describe(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "decisions": [d.describe() for d in self.decisions],
            "typed_conditions": self.typed_conditions.describe() if self.typed_conditions else [],
            "pending_evidence": [
                {"field": i.field, "reason": i.reason, "raw_text": i.raw_text}
                for i in self.pending_evidence
            ],
            "legacy_snapshot": list(self.legacy_snapshot),
        }


def migrate_legacy_conditions(
    legacy: ConditionSet | Mapping[str, Any],
    schema: FieldTypeSchema,
    *,
    text_match: str = "equals",
) -> MigrationReport:
    """旧条件 -> 类型化条件。

    - 文本字段：旧版包含语义迁移为文本等值（``text_match="contains"`` 可保留包含）；
    - 布尔字段：``"true"``/``"false"``/真正的布尔值迁移为布尔等值；
    - 缺字段类型：不猜测，保留原文本和待核原因，整组阻断新版执行；
    - 条件值与字段类型不兼容：标记 invalid，同样阻断。
    """
    if isinstance(legacy, ConditionSet):
        legacy_conditions = legacy.conditions
        version = legacy.version
    else:
        legacy_conditions = legacy_conditions_from_mapping(legacy).conditions
        version = ConditionVersion.LEGACY_V1
    if version != ConditionVersion.LEGACY_V1:
        raise ConditionError("只有旧版条件可以迁移；已迁移版本请直接构造类型化条件")

    decisions: list[FieldMigration] = []
    typed: list[TypedCondition] = []
    pending: list[ConditionIssue] = []
    blocked = False

    for condition in legacy_conditions:
        raw_text = condition.raw_text or str(condition.value)
        actual = schema.type_of(condition.field)
        if actual is None:
            blocked = True
            decision = FieldMigration(
                field=condition.field,
                raw_text=raw_text,
                status="needs_review",
                reason=f"{schema.missing_reason}，保留原文本待核",
                legacy_value=condition.value,
            )
            decisions.append(decision)
            pending.append(ConditionIssue(
                field=condition.field,
                reason=schema.missing_reason,
                raw_text=raw_text,
            ))
            continue
        if actual == FieldType.BOOLEAN:
            boolean = coerce_boolean(condition.value)
            if boolean is None:
                # 旧版条件值只有 "1"/"0"/"true"/"false" 能迁移成布尔；
                # 迁移后执行仍只认真实布尔值，数据里的 "true"/1 不会命中。
                boolean = {
                    "1": True, "true": True, "0": False, "false": False,
                }.get(str(condition.value).strip().lower())
            if boolean is None:
                blocked = True
                decisions.append(FieldMigration(
                    field=condition.field,
                    raw_text=raw_text,
                    status="invalid",
                    reason=f"字段是布尔类型，旧条件值 {condition.value!r} 无法迁移",
                    legacy_value=condition.value,
                ))
                continue
            typed.append(TypedCondition(
                field=condition.field,
                kind=ConditionKind.BOOLEAN_EQUALS,
                value=boolean,
                raw_text=raw_text,
            ))
            decisions.append(FieldMigration(
                field=condition.field,
                raw_text=raw_text,
                status="migrated_boolean",
                reason="布尔字段迁移为布尔等值；字符串/整数不再冒充布尔",
                legacy_value=condition.value,
                typed_value=boolean,
            ))
            continue
        kind = ConditionKind.TEXT_CONTAINS if text_match == "contains" else ConditionKind.TEXT_EQUALS
        typed.append(TypedCondition(
            field=condition.field,
            kind=kind,
            value=str(condition.value),
            raw_text=raw_text,
        ))
        decisions.append(FieldMigration(
            field=condition.field,
            raw_text=raw_text,
            status="migrated_text",
            reason=(
                "文本字段迁移为等值匹配；旧版包含命中的额外成员不再计入"
                if kind == ConditionKind.TEXT_EQUALS
                else "文本字段保留包含匹配"
            ),
            legacy_value=condition.value,
            typed_value=str(condition.value),
        ))

    typed_set = None
    if not blocked:
        typed_set = ConditionSet(conditions=typed, version=ConditionVersion.TYPED_V1)
    return MigrationReport(
        blocked=blocked,
        decisions=decisions,
        typed_conditions=typed_set,
        pending_evidence=pending,
        legacy_snapshot=[
            {"field": c.field, "value": c.value, "raw_text": c.raw_text}
            for c in legacy_conditions
        ],
    )


# ============================================================
# 执行入口：事件匹配和群体圈选共用
# ============================================================

def execute_conditions(
    conditions: ConditionSet,
    attributes_list: Iterable[Mapping[str, Any]],
    *,
    schema: FieldTypeSchema | None = None,
    allow_legacy: bool = True,
) -> ConditionExecution:
    """按版本执行条件。

    - ``typed_v1``：必须有 schema 且通过字段类型校验，否则抛 FieldTypeMissingError；
    - ``legacy_v1``：带 schema 时只做告警（``check.issues`` 非空），不阻断执行；
    - 也可以用 ``allow_legacy=False`` 在迁移完成后强制拒绝旧条件。
    """
    version = conditions.version
    if version == ConditionVersion.LEGACY_V1 and not allow_legacy:
        raise ConditionError("旧版圈选条件已停用，请先完成迁移")
    check = check_conditions(conditions, schema or FieldTypeSchema(), version)
    check.require_executable()
    matched = [dict(item) for item in attributes_list if conditions.evaluate(item)]
    return ConditionExecution(
        version=version,
        conditions=conditions,
        matched=matched,
        check=check,
    )


def require_same_version(*versions: ConditionVersion | str | None) -> ConditionVersion:
    """同一次正式比较的两组条件必须取一个版本。"""
    parsed = [ConditionVersion.parse(v, ConditionVersion.TYPED_V1) for v in versions]
    if not parsed:
        raise VersionConflictError("没有可比对的条件版本")
    if len(set(parsed)) > 1:
        labelled = "、".join(v.value for v in parsed)
        raise VersionConflictError(f"同一次比较混用了条件版本：{labelled}")
    return parsed[0]


def conditions_text(conditions: ConditionSet) -> str:
    """条件的人类可读文本；旧条件保留原文本。"""
    parts = []
    for condition in conditions.conditions:
        if condition.kind == ConditionKind.LEGACY_CONTAINS:
            parts.append(f"{condition.field} 包含 {condition.value}")
        elif condition.kind == ConditionKind.LEGACY_EQUALS:
            parts.append(f"{condition.field} = {condition.value}")
        elif condition.kind == ConditionKind.BOOLEAN_EQUALS:
            parts.append(f"{condition.field} = {str(condition.value).lower()}")
        elif condition.kind == ConditionKind.TEXT_CONTAINS:
            parts.append(f"{condition.field} 包含 {condition.value}")
        else:
            parts.append(f"{condition.field} = {condition.value}")
    return " AND ".join(parts)
