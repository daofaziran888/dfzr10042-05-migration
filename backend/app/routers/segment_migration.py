"""圈选迁移与日更复查接口。

- ``POST /api/segment_migration/replay``：回放旧环境执行结果并给出新版差异；
- ``POST /api/segment_migration/deploy``：按顺序迁移，可模拟单群迁完即中断；
- ``POST /api/segment_migration/daily-comparison``：当天两群比较，返回版本与可发时间；
- ``GET  /api/segment_migration/on-call``：最近一次部署后的值班复查。
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, HTTPException

from app.models import EventTrack, Segment, UserSegmentCompare
from app.schemas import ActionResult
from app.segment_conditions import (
    ConditionError,
    FieldTypeSchema,
    FieldType,
    VersionConflictError,
)
from app.segment_release import (
    PublishPolicy,
    build_daily_comparison,
    on_call_review,
    replay_event_matching,
    replay_segment,
    run_deployment,
)

router = APIRouter(prefix="/api/segment_migration", tags=["圈选迁移"])

# 最近一次部署的结果，供值班复查读取。
_LAST_DEPLOYMENT: dict[str, Any] = {"run": None, "reports": [], "replays": []}


def _schema(values: dict[str, str] | None) -> FieldTypeSchema:
    if not values:
        return FieldTypeSchema()
    try:
        return FieldTypeSchema(types={name: FieldType(value) for name, value in values.items()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"字段类型非法：{exc}") from exc


def _users(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload.get("users", []))


def _events(payload: dict[str, Any]) -> list[EventTrack]:
    events = []
    for item in payload.get("events", []):
        events.append(EventTrack(
            event_id=str(item.get("event_id", "")),
            event_name=str(item.get("event_name", "")),
            event_properties=dict(item.get("event_properties", {})),
        ))
    return events


def _schemas_by_segment(
    payload: dict[str, Any], segments: Mapping[str, Any]
) -> dict[str, FieldTypeSchema]:
    """字段类型可以全局给，也可以按人群分开给，便于模拟“一群缺证”。"""
    shared = payload.get("field_types") or {}
    overrides = payload.get("field_types_by_segment") or {}
    return {
        name: _schema({**shared, **overrides.get(name, {})})
        for name in segments
    }


def _segments(payload: dict[str, Any]) -> dict[str, Segment]:
    segments: dict[str, Segment] = {}
    for item in payload.get("segments", []):
        segments[str(item["segment_id"])] = Segment(
            segment_id=str(item["segment_id"]),
            segment_name=str(item.get("segment_name", "")),
            conditions=dict(item.get("conditions", {})),
        )
    return segments


@router.post("/replay", response_model=ActionResult)
def replay(payload: dict[str, Any]) -> ActionResult:
    """回放旧环境：旧成员集合 + 新版成员集合 + 差异 + 缺证字段。"""
    segments = _segments(payload)
    schemas = _schemas_by_segment(payload, segments)
    results = []
    for segment in segments.values():
        replay_result = replay_segment(
            segment,
            _users(payload),
            schema=schemas[segment.segment_id],
            as_of=str(payload.get("as_of", "")),
        )
        results.append(replay_result.describe())
    event_results = replay_event_matching(
        _events(payload), _users(payload), _schema(payload.get("field_types")),
        as_of=str(payload.get("as_of", "")),
    )
    _LAST_DEPLOYMENT["replays"] = results
    return ActionResult(
        ok=True,
        message="旧环境回放完成",
        entry={"replays": results, "event_replay": event_results},
    )


@router.post("/deploy", response_model=ActionResult)
def deploy(payload: dict[str, Any]) -> ActionResult:
    """按顺序迁移人群；``stop_after`` 为已处理数量，模拟迁移中断。"""
    segments = _segments(payload)
    if not segments:
        raise HTTPException(status_code=400, detail="至少需要一个人群")
    schemas = _schemas_by_segment(payload, segments)
    users_by_segment = {
        name: list(payload.get("users_by_segment", {}).get(name, payload.get("users", [])))
        for name in segments
    }
    order = payload.get("order") or list(segments)
    stop_after = payload.get("stop_after")
    run = run_deployment(
        release_id=str(payload.get("release_id", "REL")),
        segments=list(segments.values()),
        users_by_segment=users_by_segment,
        schemas_by_segment=schemas,
        report_date=str(payload.get("report_date", "")),
        order=order,
        stop_after=int(stop_after) if stop_after is not None else None,
        pending_ready_at=payload.get("pending_ready_at"),
    )
    _LAST_DEPLOYMENT["run"] = run
    _LAST_DEPLOYMENT["reports"] = []
    return ActionResult(
        ok=run.completed,
        message=(
            "两群均已迁移" if run.completed
            else f"迁移未完成，待处理：{'、'.join(run.pending_segments)}"
        ),
        entry=run.describe(),
    )


@router.post("/daily-comparison", response_model=ActionResult)
def daily_comparison(payload: dict[str, Any]) -> ActionResult:
    """当天两群比较；只有两群都迁完才出新版，否则按策略整组回退或等待。"""
    run = _LAST_DEPLOYMENT["run"]
    if run is None:
        raise HTTPException(status_code=409, detail="请先执行 /deploy")
    segments = {sid: dep.segment for sid, dep in run.deployments.items()}
    segment_a_id = str(payload.get("segment_a", ""))
    segment_b_id = str(payload.get("segment_b", ""))
    if segment_a_id not in segments or segment_b_id not in segments:
        raise HTTPException(status_code=404, detail="比较方案中的人群不在本次部署里")
    from app.models import AggregationMethod, MetricDef, MetricPeriod

    metric = MetricDef(
        metric_id="M_CMP",
        metric_name=str(payload.get("metric_name", "转化率")),
        calculation_formula="AVG(conversion_rate)",
        aggregation=AggregationMethod.AVG,
        data_source="clickhouse",
        period=MetricPeriod.DAILY,
        owner="analyst",
        field_name=str(payload.get("metric_field", "conversion_rate")),
        table_name="user_events",
    )
    compare = UserSegmentCompare(
        compare_id=str(payload.get("compare_id", "CMP")),
        segment_a=segments[segment_a_id],
        segment_b=segments[segment_b_id],
        compare_metric=metric,
    )
    try:
        report = build_daily_comparison(
            compare,
            run,
            _users(payload),
            report_date=str(payload.get("report_date", run.report_date)),
            policy=PublishPolicy(str(payload.get("policy", PublishPolicy.LEGACY_FALLBACK.value))),
        )
    except (ConditionError, VersionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _LAST_DEPLOYMENT.setdefault("reports", []).append(report)
    return ActionResult(
        ok=report.published,
        message=report.reason,
        entry=report.describe(),
    )


@router.get("/on-call", response_model=dict)
def on_call() -> dict[str, Any]:
    """值班员复查：当天收到的报告版本、两群指标、新版可发时间。"""
    run = _LAST_DEPLOYMENT["run"]
    if run is None:
        raise HTTPException(status_code=404, detail="还没有部署记录")
    reports = [r for r in _LAST_DEPLOYMENT["reports"] if hasattr(r, "describe")]
    return on_call_review(run, reports)
