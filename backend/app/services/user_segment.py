"""分群比较领域服务：用实际圈中成员生成报告，并标注条件版本。"""
from __future__ import annotations

from typing import Any

from app.models import Segment, UserSegmentCompare
from app.segment_conditions import FieldTypeSchema, require_same_version
from app.services.base import BaseModuleService


class UserSegmentService(BaseModuleService):
    MODULE = "user_segment"
    ENTITY = "比较方案"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["方案编号", "群体A", "群体B", "比较指标", "差异幅度", "显著性", "样本量A", "样本量B"]
    REQUIRED_FIELDS = ["方案编号", "群体A", "群体B"]
    STATUS_ORDER = ["待计算", "计算中", "已完成", "样本不足"]
    ACTION_RULES = {"启动计算": "计算中", "重算比较": "已完成", "标记不足": "样本不足"}
    NEGATIVE_ACTIONS = []

    def build_report(
        self,
        compare_id: str,
        segment_a: Segment,
        segment_b: Segment,
        users: list[dict[str, Any]],
        *,
        metric: Any,
        schema: FieldTypeSchema | None = None,
        allow_legacy: bool = True,
    ) -> dict[str, Any]:
        """两群比较报告；混版直接拒绝，旧口径会在报告里标注版本。"""
        # 先核对版本，任一群缺证或混版都不会写入报告行。
        require_same_version(segment_a.condition_version, segment_b.condition_version)
        compare = UserSegmentCompare(
            compare_id=compare_id,
            segment_a=segment_a,
            segment_b=segment_b,
            compare_metric=metric,
        )
        report = compare.generate_report(users, schema=schema, allow_legacy=allow_legacy)
        entry = self.get_entry(_numeric_id(compare_id))
        if entry is not None:
            entry["差异幅度"] = report["diff"]
            entry["显著性"] = report["test"].get("significant")
            entry["样本量A"] = report["segment_a"]["member_count"]
            entry["样本量B"] = report["segment_b"]["member_count"]
            entry["条件版本"] = report["version"]
        return report


def _numeric_id(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
