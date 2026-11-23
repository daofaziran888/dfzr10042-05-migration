"""圈选条件迁移上线：回放、日更、版本一致性与值班复查。

把 ``segment_conditions`` 的条件语义接到真实执行链路上：

1. 迁移前先在旧环境回放，保留旧成员集合和人数，旧报告可复算；
2. 新版执行（事件匹配、人群圈选）都必须先过字段类型校验；
3. 一次正式比较的两组条件只能取一个版本，混版直接拒绝；
4. 迁移未齐、日更又到点时，要么整组沿用旧口径并标注 ``legacy_v1``，
   要么等新版齐备再发，不能把旧结论写成新版；
5. 缺证字段保留原文本和待核原因，部署中断时给出待迁清单和新版可发时间。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from app.models import EventTrack, Segment, UserSegmentCompare
from app.segment_conditions import (
    ConditionCheck,
    ConditionError,
    ConditionIssue,
    ConditionVersion,
    FieldTypeSchema,
    MigrationReport,
    conditions_text,
    execute_conditions,
    migrate_legacy_conditions,
)


# ============================================================
# 发布策略
# ============================================================

class PublishPolicy(str, Enum):
    """日更到点时，新旧版本尚未齐备的处理方式。"""

    LEGACY_FALLBACK = "legacy_fallback"   # 整组沿用旧口径，报告标注 legacy_v1
    REQUIRE_TYPED = "require_typed"       # 不发新版，等两群都迁完


class DeploymentStatus(str, Enum):
    MIGRATED = "migrated"
    PENDING_EVIDENCE = "pending_evidence"
    INVALID = "invalid"
    NOT_STARTED = "not_started"


# ============================================================
# 回放
# ============================================================

@dataclass
class ReplayResult:
    """一个圈选对象在旧环境的回放结果和新版执行结果。"""

    segment_id: str
    as_of: str
    legacy_member_ids: list[str]
    legacy_member_count: int
    legacy_conditions_text: str
    typed_member_ids: list[str] | None = None
    typed_conditions_text: str = ""
    pending_evidence: list[ConditionIssue] = field(default_factory=list)
    typed_check: ConditionCheck | None = None

    @property
    def dropped_member_ids(self) -> list[str]:
        if self.typed_member_ids is None:
            return []
        return [uid for uid in self.legacy_member_ids if uid not in set(self.typed_member_ids)]

    @property
    def added_member_ids(self) -> list[str]:
        if self.typed_member_ids is None:
            return []
        return [uid for uid in self.typed_member_ids if uid not in set(self.legacy_member_ids)]

    def describe(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "as_of": self.as_of,
            "legacy": {
                "member_count": self.legacy_member_count,
                "member_ids": list(self.legacy_member_ids),
                "conditions_text": self.legacy_conditions_text,
                "version": ConditionVersion.LEGACY_V1.value,
            },
            "typed": (
                {
                    "member_count": len(self.typed_member_ids),
                    "member_ids": list(self.typed_member_ids),
                    "conditions_text": self.typed_conditions_text,
                    "version": ConditionVersion.TYPED_V1.value,
                    "field_type_check": self.typed_check.describe() if self.typed_check else None,
                }
                if self.typed_member_ids is not None else None
            ),
            "dropped_member_ids": self.dropped_member_ids,
            "added_member_ids": self.added_member_ids,
            "pending_evidence": [
                {"field": issue.field, "reason": issue.reason, "raw_text": issue.raw_text}
                for issue in self.pending_evidence
            ],
            "old_report_recomputable": True,
        }


def member_key(user: Mapping[str, Any]) -> str:
    for key in ("user_id", "id", "member_id"):
        if key in user:
            return str(user[key])
    return str(user)


def legacy_clone(segment: Segment) -> Segment:
    """保留旧条件原文的旧版人群副本，用于复算旧报告和整组回退。"""
    if not segment.conditions:
        raise ConditionError(f"人群 {segment.segment_id} 没有旧版条件原文，无法回放旧口径")
    clone = replace(segment)
    clone.condition_version = ConditionVersion.LEGACY_V1
    clone.typed_conditions = None
    return clone


def replay_segment(
    segment: Segment,
    users: list[dict[str, Any]],
    *,
    schema: FieldTypeSchema | None = None,
    as_of: str = "",
    migrate: bool = True,
) -> ReplayResult:
    """回放旧环境圈选结果；字段类型齐备时再给出新版执行结果和差异。"""
    legacy_exec = legacy_clone(segment).execute(users, allow_legacy=True)
    legacy_ids = [member_key(u) for u in legacy_exec.matched]
    result = ReplayResult(
        segment_id=segment.segment_id,
        as_of=as_of,
        legacy_member_ids=legacy_ids,
        legacy_member_count=len(legacy_ids),
        legacy_conditions_text=conditions_text(legacy_exec.conditions),
    )
    if not migrate:
        return result

    merged_schema = segment.field_types.merge(schema) if schema else segment.field_types
    report = migrate_legacy_conditions(segment.conditions, merged_schema)
    result.pending_evidence = list(report.pending_evidence)
    if report.blocked or report.typed_conditions is None:
        return result

    typed_exec = execute_conditions(report.typed_conditions, users, schema=merged_schema)
    result.typed_member_ids = [member_key(u) for u in typed_exec.matched]
    result.typed_conditions_text = conditions_text(report.typed_conditions)
    result.typed_check = typed_exec.check
    return result


# ============================================================
# 部署
# ============================================================

@dataclass
class SegmentDeployment:
    """单个人群的迁移状态。"""

    segment_id: str
    status: DeploymentStatus
    segment: Segment
    legacy_member_ids: list[str] = field(default_factory=list)
    legacy_member_count: int = 0
    typed_member_ids: list[str] | None = None
    migration: MigrationReport | None = None
    pending_evidence: list[ConditionIssue] = field(default_factory=list)
    checked: ConditionCheck | None = None

    @property
    def migrated(self) -> bool:
        return self.status == DeploymentStatus.MIGRATED

    @property
    def can_fallback_legacy(self) -> bool:
        return bool(self.segment.conditions)

    def describe(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "status": self.status.value,
            "version": self.segment.condition_version.value,
            "legacy_member_count": self.legacy_member_count,
            "legacy_member_ids": list(self.legacy_member_ids),
            "typed_member_count": len(self.typed_member_ids) if self.typed_member_ids is not None else None,
            "typed_member_ids": list(self.typed_member_ids) if self.typed_member_ids is not None else None,
            "pending_evidence": [
                {"field": i.field, "reason": i.reason, "raw_text": i.raw_text}
                for i in self.pending_evidence
            ],
            "migration": self.migration.describe() if self.migration else None,
        }


@dataclass
class DeploymentRun:
    """一次迁移部署（可以从中间中断）。"""

    release_id: str
    report_date: str
    deployments: dict[str, SegmentDeployment] = field(default_factory=dict)
    interrupted: bool = False
    interrupted_after: str | None = None
    pending_ready_at: str | None = None
    pending_segments: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return bool(self.deployments) and all(d.migrated for d in self.deployments.values())

    @property
    def migrated_segments(self) -> list[str]:
        return [sid for sid, d in self.deployments.items() if d.migrated]

    @property
    def blocked_segments(self) -> list[str]:
        return [
            sid for sid, d in self.deployments.items()
            if not d.migrated
        ]

    def deployment(self, segment_id: str) -> SegmentDeployment | None:
        return self.deployments.get(segment_id)

    def describe(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "report_date": self.report_date,
            "completed": self.completed,
            "interrupted": self.interrupted,
            "interrupted_after": self.interrupted_after,
            "migrated_segments": self.migrated_segments,
            "pending_segments": list(self.pending_segments),
            "pending_ready_at": self.pending_ready_at,
            "deployments": {sid: d.describe() for sid, d in self.deployments.items()},
        }


def run_deployment(
    release_id: str,
    segments: Sequence[Segment],
    users_by_segment: Mapping[str, list[dict[str, Any]]],
    schemas_by_segment: Mapping[str, FieldTypeSchema],
    *,
    report_date: str = "",
    order: Sequence[str] | None = None,
    stop_after: int | None = None,
    pending_ready_at: str | None = None,
) -> DeploymentRun:
    """按顺序迁移人群；``stop_after`` 模拟迁移中途中断。

    中断后未处理的人群标为 ``not_started``，已经迁完的人群保持 ``migrated``。
    每个成功迁移的人群都会先回放旧成员集合，旧报告的人数因此仍可复算。
    """
    by_id = {segment.segment_id: segment for segment in segments}
    sequence = list(order or [segment.segment_id for segment in segments])
    run = DeploymentRun(
        release_id=release_id,
        report_date=report_date,
        pending_ready_at=pending_ready_at,
    )
    processed = 0
    for segment_id in sequence:
        segment = by_id.get(segment_id)
        if segment is None:
            raise ConditionError(f"未知人群：{segment_id}")
        users = users_by_segment.get(segment_id, [])
        schema = schemas_by_segment.get(segment_id, segment.field_types)

        legacy_exec = legacy_clone(segment).execute(users, allow_legacy=True)
        legacy_ids = [member_key(u) for u in legacy_exec.matched]
        deployment = SegmentDeployment(
            segment_id=segment_id,
            status=DeploymentStatus.NOT_STARTED,
            segment=segment,
            legacy_member_ids=legacy_ids,
            legacy_member_count=len(legacy_ids),
        )

        if stop_after is not None and processed >= stop_after:
            # 中断后只探字段类型，保留缺证原文本和待核原因，不迁移、不改状态。
            probe = migrate_legacy_conditions(
                segment.conditions, segment.field_types.merge(schema)
            )
            deployment.pending_evidence = list(probe.pending_evidence)
            deployment.migration = None
            run.deployments[segment_id] = deployment
            run.interrupted = True
            run.interrupted_after = sequence[processed - 1] if processed else None
            run.pending_segments.append(segment_id)
            continue

        merged_schema = segment.field_types.merge(schema)
        migration = migrate_legacy_conditions(segment.conditions, merged_schema)
        deployment.migration = migration
        deployment.pending_evidence = list(migration.pending_evidence)
        if migration.blocked or migration.typed_conditions is None:
            deployment.status = (
                DeploymentStatus.PENDING_EVIDENCE
                if migration.pending_evidence else DeploymentStatus.INVALID
            )
            run.deployments[segment_id] = deployment
            run.pending_segments.append(segment_id)
            continue

        execution = execute_conditions(migration.typed_conditions, users, schema=merged_schema)
        deployment.typed_member_ids = [member_key(u) for u in execution.matched]
        deployment.checked = execution.check
        # 迁移结果写到副本上，调用方的旧版人群对象保持原样（原文仍可回放）。
        deployment.segment = replace(segment).with_typed_conditions(
            migration.typed_conditions,
            schema=merged_schema,
            notes=[d.describe() for d in migration.decisions],
        )
        deployment.status = DeploymentStatus.MIGRATED
        run.deployments[segment_id] = deployment
        processed += 1

    if not run.interrupted and run.pending_segments:
        run.interrupted = True
        run.interrupted_after = run.migrated_segments[-1] if run.migrated_segments else None
    return run


def replay_event_matching(
    events: Sequence[EventTrack],
    users: list[dict[str, Any]],
    schema: FieldTypeSchema,
    *,
    as_of: str = "",
) -> list[dict[str, Any]]:
    """回放旧环的事件匹配，并在字段类型齐备时给出新版匹配。"""
    results = []
    for event in events:
        legacy = execute_conditions(event.typed_conditions(), users, allow_legacy=True)
        legacy_ids = [member_key(u) for u in legacy.matched]
        entry: dict[str, Any] = {
            "event_id": event.event_id,
            "event_name": event.event_name,
            "as_of": as_of,
            "legacy": {
                "member_count": len(legacy_ids),
                "member_ids": legacy_ids,
                "version": ConditionVersion.LEGACY_V1.value,
            },
            "typed": None,
            "pending_evidence": [],
        }
        report = migrate_legacy_conditions(event.event_properties, schema)
        if not report.blocked and report.typed_conditions is not None:
            typed = execute_conditions(report.typed_conditions, users, schema=schema)
            typed_ids = [member_key(u) for u in typed.matched]
            entry["typed"] = {
                "member_count": len(typed_ids),
                "member_ids": typed_ids,
                "version": ConditionVersion.TYPED_V1.value,
                "field_type_check": typed.check.describe() if typed.check else None,
            }
            entry["dropped_member_ids"] = [uid for uid in legacy_ids if uid not in set(typed_ids)]
        entry["pending_evidence"] = [
            {"field": i.field, "reason": i.reason, "raw_text": i.raw_text}
            for i in report.pending_evidence
        ]
        results.append(entry)
    return results


# ============================================================
# 日更比较
# ============================================================

@dataclass
class DailyComparison:
    """当天的两群比较报告。``version`` 决定它能不能自称新版结论。"""

    report_date: str
    compare_id: str
    version: ConditionVersion
    published: bool
    is_new_version: bool
    version_label: str
    reason: str
    segment_a: dict[str, Any] | None = None
    segment_b: dict[str, Any] | None = None
    metric: dict[str, Any] | None = None
    pending_evidence: list[dict[str, Any]] = field(default_factory=list)
    pending_segments: list[str] = field(default_factory=list)
    earliest_new_version_at: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date,
            "compare_id": self.compare_id,
            "version": self.version.value,
            "version_label": self.version_label,
            "published": self.published,
            "is_new_version": self.is_new_version,
            "reason": self.reason,
            "segment_a": self.segment_a,
            "segment_b": self.segment_b,
            "metric": self.metric,
            "pending_evidence": list(self.pending_evidence),
            "pending_segments": list(self.pending_segments),
            "earliest_new_version_at": self.earliest_new_version_at,
        }


def _can_run_typed(deployment: SegmentDeployment | None) -> bool:
    if deployment is None or not deployment.migrated:
        return False
    check = deployment.checked
    return check is None or check.ok


def build_daily_comparison(
    compare: UserSegmentCompare,
    deployment: DeploymentRun,
    users: list[dict[str, Any]],
    *,
    report_date: str = "",
    policy: PublishPolicy | str = PublishPolicy.LEGACY_FALLBACK,
    schema: FieldTypeSchema | None = None,
) -> DailyComparison:
    """生成当天两群比较。

    - 两群都迁完：用 ``typed_v1`` 出新版报告；
    - 只迁了一群（或另一群缺字段类型）：不能混版。选择 ``legacy_fallback``
      时整组回退旧口径并标注版本；选择 ``require_typed`` 时当天不出新报告；
    - 旧口径继续使用时保留缺证字段的原文本和待核原因。
    """
    policy = policy if isinstance(policy, PublishPolicy) else PublishPolicy(str(policy))
    dep_a = deployment.deployment(compare.segment_a.segment_id)
    dep_b = deployment.deployment(compare.segment_b.segment_id)
    typed_ready = _can_run_typed(dep_a) and _can_run_typed(dep_b)
    pending_evidence = _collect_evidence(dep_a, dep_b)

    if typed_ready:
        typed_a = dep_a.segment if dep_a else compare.segment_a
        typed_b = dep_b.segment if dep_b else compare.segment_b
        typed_compare = UserSegmentCompare(
            compare_id=compare.compare_id,
            segment_a=typed_a,
            segment_b=typed_b,
            compare_metric=compare.compare_metric,
        )
        try:
            report = typed_compare.generate_report(users, schema=schema)
        except ConditionError as exc:
            return DailyComparison(
                report_date=report_date or deployment.report_date,
                compare_id=compare.compare_id,
                version=ConditionVersion.TYPED_V1,
                published=False,
                is_new_version=False,
                version_label="新版 typed_v1 未通过字段类型校验",
                reason=str(exc),
                pending_evidence=pending_evidence,
                pending_segments=list(deployment.pending_segments),
                earliest_new_version_at=deployment.pending_ready_at,
            )
        return DailyComparison(
            report_date=report_date or deployment.report_date,
            compare_id=compare.compare_id,
            version=ConditionVersion.TYPED_V1,
            published=True,
            is_new_version=True,
            version_label="新版 typed_v1",
            reason="两群均已完成迁移，字段类型校验通过",
            segment_a=report["segment_a"],
            segment_b=report["segment_b"],
            metric=report["test"],
            pending_evidence=pending_evidence,
            pending_segments=list(deployment.pending_segments),
        )

    # 不能混版：只要有一群没迁完，整组只能一起取旧版或一起不发。
    if policy == PublishPolicy.REQUIRE_TYPED:
        return DailyComparison(
            report_date=report_date or deployment.report_date,
            compare_id=compare.compare_id,
            version=ConditionVersion.TYPED_V1,
            published=False,
            is_new_version=False,
            version_label="新版 typed_v1（未发布）",
            reason="两群版本不齐，按策略等待新版齐备后再发；不得混版比较",
            pending_evidence=pending_evidence,
            pending_segments=list(deployment.pending_segments),
            earliest_new_version_at=deployment.pending_ready_at,
        )

    # 旧口径整组回退：两组都必须还有旧条件原文，且不能把结果标成新版。
    if not (compare.segment_a.conditions and compare.segment_b.conditions):
        return DailyComparison(
            report_date=report_date or deployment.report_date,
            compare_id=compare.compare_id,
            version=ConditionVersion.LEGACY_V1,
            published=False,
            is_new_version=False,
            version_label="旧口径不可用",
            reason="至少一组已没有旧条件原文，无法整组回退旧口径",
            pending_evidence=pending_evidence,
            pending_segments=list(deployment.pending_segments),
            earliest_new_version_at=deployment.pending_ready_at,
        )

    legacy_compare = UserSegmentCompare(
        compare_id=compare.compare_id,
        segment_a=legacy_clone(compare.segment_a),
        segment_b=legacy_clone(compare.segment_b),
        compare_metric=compare.compare_metric,
    )
    report = legacy_compare.generate_report(users)
    return DailyComparison(
        report_date=report_date or deployment.report_date,
        compare_id=compare.compare_id,
        version=ConditionVersion.LEGACY_V1,
        published=True,
        is_new_version=False,
        version_label="旧口径 legacy_v1（继续沿用，不等同新版结论）",
        reason="迁移未齐且日更到点，整组沿用旧口径；新版需等两群都迁完",
        segment_a=report["segment_a"],
        segment_b=report["segment_b"],
        metric=report["test"],
        pending_evidence=pending_evidence,
        pending_segments=list(deployment.pending_segments),
        earliest_new_version_at=deployment.pending_ready_at,
    )


def _collect_evidence(*deployments: SegmentDeployment | None) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for deployment in deployments:
        if deployment is None:
            continue
        for issue in deployment.pending_evidence:
            key = (deployment.segment_id, issue.field)
            if key in seen:
                continue
            seen.add(key)
            evidence.append({
                "segment_id": deployment.segment_id,
                "field": issue.field,
                "reason": issue.reason,
                "raw_text": issue.raw_text,
            })
    return evidence


# ============================================================
# 值班复查
# ============================================================

def on_call_review(
    deployment: DeploymentRun,
    reports: Iterable[DailyComparison],
    *,
    engineer: str = "值班员",
) -> dict[str, Any]:
    """汇总值班员当天收到的报告：版本、两群指标、新版可发时间。

    模拟“单群迁完就中断”时，当天通常只有旧口径报告能出；这里明确给出：
    哪些报告是新版、哪些只是旧口径、缺哪些字段类型、新版最早什么时候能发。
    """
    report_list = [r.describe() for r in reports]
    typed_reports = [r for r in report_list if r["is_new_version"]]
    legacy_reports = [
        r for r in report_list
        if r["version"] == ConditionVersion.LEGACY_V1.value and r["published"]
    ]
    blocked_reports = [r for r in report_list if not r["published"]]
    pending_segments = list(deployment.pending_segments)
    if deployment.completed:
        status = "new_version_ready"
        message = "两群均已迁移，可发新版报告"
    elif deployment.pending_ready_at:
        status = "waiting_for_evidence"
        missing = _collect_evidence(
            *(deployment.deployment(sid) for sid in pending_segments)
        )
        reason = "仍缺字段类型" if missing else "尚未开始迁移"
        message = (
            f"{'、'.join(pending_segments)} {reason}；若今天必须出数，"
            f"只能整组沿用旧口径并标注 legacy_v1。新版需等两群齐备，"
            f"最早 {deployment.pending_ready_at} 可发。"
        )
    else:
        status = "deployment_interrupted"
        message = (
            f"迁移在 {'、'.join(pending_segments)} 中断，且没有新版可发时间；"
            "今天可先用旧口径并标注版本，不能把旧结论当成新版。"
        )
    return {
        "engineer": engineer,
        "report_date": deployment.report_date,
        "release_id": deployment.release_id,
        "status": status,
        "message": message,
        "reports_received": report_list,
        "new_version_reports": [r["compare_id"] for r in typed_reports],
        "legacy_version_reports": [r["compare_id"] for r in legacy_reports],
        "blocked_reports": [r["compare_id"] for r in blocked_reports],
        "pending_segments": pending_segments,
        "pending_evidence": _collect_evidence(
            *(deployment.deployment(sid) for sid in pending_segments)
        ),
        "migrated_segments": deployment.migrated_segments,
        "interrupted_after": deployment.interrupted_after,
        "earliest_new_version_at": deployment.pending_ready_at,
        "can_publish_new_version_today": deployment.completed and not blocked_reports,
    }


def replay_history(
    records: Iterable[dict[str, Any]],
    segments: Mapping[str, Segment],
    users_by_segment: Mapping[str, list[dict[str, Any]]],
    schemas_by_segment: Mapping[str, FieldTypeSchema],
) -> list[dict[str, Any]]:
    """回放历史执行记录：逐日给出旧成员集合、新版成员集合和差异。"""
    replays: list[dict[str, Any]] = []
    for record in records:
        segment_id = str(record.get("segment_id", ""))
        segment = segments.get(segment_id)
        if segment is None:
            continue
        users = users_by_segment.get(segment_id, record.get("users", []))
        schema = schemas_by_segment.get(segment_id, segment.field_types)
        replay = replay_segment(
            segment,
            users,
            schema=schema,
            as_of=str(record.get("as_of", "")),
        )
        replays.append({
            "segment_id": segment_id,
            "as_of": replay.as_of,
            "legacy_member_count": replay.legacy_member_count,
            "legacy_member_ids": replay.legacy_member_ids,
            "typed_member_count": len(replay.typed_member_ids) if replay.typed_member_ids is not None else None,
            "typed_member_ids": replay.typed_member_ids,
            "dropped_member_ids": replay.dropped_member_ids,
            "added_member_ids": replay.added_member_ids,
            "pending_evidence": [
                {"field": i.field, "reason": i.reason, "raw_text": i.raw_text}
                for i in replay.pending_evidence
            ],
        })
    return replays
