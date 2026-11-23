"""群体圈选领域服务：旧条件迁移、类型化条件读取和成员复算。"""
from __future__ import annotations

from typing import Any

from app.models import Segment
from app.segment_conditions import (
    ConditionVersion,
    FieldTypeSchema,
    MigrationReport,
    migrate_legacy_conditions,
)
from app.services.base import BaseModuleService


class SegmentBuilderService(BaseModuleService):
    MODULE = "segment_builder"
    ENTITY = "人群包"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["人群编号", "人群名称", "圈选条件", "成员数量", "更新策略", "生效区间", "依赖指标", "最近计算"]
    REQUIRED_FIELDS = ["人群编号", "人群名称", "圈选条件"]
    STATUS_ORDER = ["草稿", "计算中", "可用", "已过期"]
    ACTION_RULES = {"启动计算": "计算中", "标记可用": "可用", "清理过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def migrate_conditions(
        self,
        entry_id: int,
        field_types: dict[str, str],
        *,
        text_match: str = "equals",
    ) -> tuple[dict[str, Any] | None, MigrationReport | None, str]:
        """对登记行里的旧圈选条件出具迁移结论；缺证字段保留原文本。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        raw_text = str(entry.get("圈选条件", ""))
        schema = FieldTypeSchema(
            types={name: _field_type(value) for name, value in field_types.items()}
        )
        parsed = _parse_legacy_text(raw_text, field_types)
        if raw_text.strip() and not parsed:
            # 原文无法解析成「字段:值」：不猜测结构，整条保留待核。
            entry["条件版本"] = ConditionVersion.LEGACY_V1.value
            entry["待核字段"] = raw_text
            entry["迁移说明"] = "圈选条件原文无法解析为字段条件，保留原文本待核"
            return entry, None, f"圈选条件原文无法解析，保留原文本待核：{raw_text}"
        report = migrate_legacy_conditions(parsed, schema, text_match=text_match)
        entry["条件版本"] = ConditionVersion.TYPED_V1.value if not report.blocked else ConditionVersion.LEGACY_V1.value
        entry["待核字段"] = ",".join(report.unverified_fields)
        entry["迁移说明"] = "；".join(d.reason for d in report.decisions)
        return entry, report, (
            "旧条件已全部迁移为类型化条件"
            if not report.blocked
            else f"仍有字段缺证，保留原文本待核：{'、'.join(report.unverified_fields)}"
        )

    def typed_segment(self, entry_id: int, field_types: dict[str, str]) -> Segment | None:
        """把人群包登记行读成可执行的人群对象（缺证时保持旧版）。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        raw_text = str(entry.get("圈选条件", ""))
        schema = FieldTypeSchema(
            types={name: _field_type(value) for name, value in field_types.items()}
        )
        segment = Segment(
            segment_id=str(entry.get("人群编号", entry_id)),
            segment_name=str(entry.get("人群名称", "")),
            conditions=_parse_legacy_text(raw_text, field_types),
        )
        if not segment.conditions:
            return segment
        report = migrate_legacy_conditions(segment.conditions, schema)
        if not report.blocked and report.typed_conditions is not None:
            segment.with_typed_conditions(
                report.typed_conditions,
                schema=schema,
                notes=[decision.describe() for decision in report.decisions],
            )
        return segment


def _field_type(value: str):
    from app.segment_conditions import FieldType

    return value if isinstance(value, FieldType) else FieldType(str(value))


def _parse_legacy_text(raw_text: str, field_types: dict[str, str]) -> dict[str, str]:
    """登记行用 ``字段:值`` 或 ``字段:值;字段:值`` 保存旧条件原文。"""
    pair: dict[str, str] = {}
    for chunk in raw_text.replace("；", ";").split(";"):
        if ":" not in chunk:
            continue
        name, value = chunk.split(":", 1)
        pair[name.strip()] = value.strip()
    if not pair:
        for name in field_types:
            if f" 包含 " in raw_text:
                left, right = raw_text.split(" 包含 ", 1)
                pair[left.strip()] = right.strip()
                break
    return pair
