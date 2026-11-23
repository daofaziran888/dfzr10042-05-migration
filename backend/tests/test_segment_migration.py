"""圈选条件迁移、回放、日更和值班复查测试。

覆盖：
- 旧版 ``vip 包含 "1"`` 与新版布尔 ``true`` 的语义差异；
- 文本 / 布尔 / 缺失属性 / 缺字段类型的执行 gate；
- 事件匹配和人群圈选共用类型化条件；
- 人群比较用实际圈中成员生成报告，并能复算旧成员与旧指标；
- 同一次比较版本一致、旧口径标注、缺证字段保留原文；
- 单群迁完即中断时的日更策略与值班复查。
"""
from __future__ import annotations

import pytest

from app.models import (
    AggregationMethod,
    EventTrack,
    MetricDef,
    MetricPeriod,
    Segment,
    UserSegmentCompare,
)
from app.segment_conditions import (
    ConditionError,
    ConditionKind,
    ConditionSet,
    ConditionVersion,
    FieldType,
    FieldTypeMissingError,
    FieldTypeSchema,
    VersionConflictError,
    check_conditions,
    execute_conditions,
    legacy_conditions_from_mapping,
    migrate_legacy_conditions,
    typed_conditions_from_mapping,
)
from app.segment_release import (
    DeploymentStatus,
    PublishPolicy,
    build_daily_comparison,
    on_call_review,
    replay_event_matching,
    replay_history,
    replay_segment,
    run_deployment,
)


# ============================================================
# 共享数据：vip 等级字段混用了布尔、字符串和缺失
# ============================================================

@pytest.fixture
def vip_users():
    return [
        {"user_id": "u_bool_true", "vip": True, "conversion_rate": 0.30},
        {"user_id": "u_str_one", "vip": "1", "conversion_rate": 0.10},
        {"user_id": "u_str_ten", "vip": "10", "conversion_rate": 0.05},
        {"user_id": "u_str_true", "vip": "true", "conversion_rate": 0.20},
        {"user_id": "u_int_one", "vip": 1, "conversion_rate": 0.25},
        {"user_id": "u_bool_false", "vip": False, "conversion_rate": 0.02},
        {"user_id": "u_missing", "conversion_rate": 0.01},
    ]


@pytest.fixture
def comparison_metric():
    return MetricDef(
        metric_id="M_CVR",
        metric_name="转化率",
        calculation_formula="AVG(conversion_rate)",
        aggregation=AggregationMethod.AVG,
        data_source="clickhouse",
        period=MetricPeriod.DAILY,
        owner="analyst",
        field_name="conversion_rate",
        table_name="user_events",
    )


def _vip_schema() -> FieldTypeSchema:
    return FieldTypeSchema(types={"vip": FieldType.BOOLEAN})


# ============================================================
# 一、旧条件与新版条件语义
# ============================================================

class TestLegacyVersusTypedSemantics:
    def test_legacy_contains_selects_one_ten_int_and_true_string(self, vip_users):
        legacy = legacy_conditions_from_mapping({"vip": "1"})
        execution = execute_conditions(legacy, vip_users)
        ids = {u["user_id"] for u in execution.matched}
        # 曾经把等级 "1"、"10" 和整数 1 都选了进来，旧报告人数因此偏大。
        assert ids == {"u_str_one", "u_str_ten", "u_int_one"}
        assert execution.version == ConditionVersion.LEGACY_V1

    def test_typed_boolean_true_only_selects_real_boolean(self, vip_users):
        typed = typed_conditions_from_mapping({"vip": True}, _vip_schema())
        execution = execute_conditions(typed, vip_users, schema=_vip_schema())
        assert [u["user_id"] for u in execution.matched] == ["u_bool_true"]

    @pytest.mark.parametrize("raw", ["true", "1", 1, 0, "false"])
    def test_strings_and_ints_cannot_impersonate_boolean(self, vip_users, raw):
        with pytest.raises(ConditionError):
            typed_conditions_from_mapping({"vip": raw}, _vip_schema())

    def test_missing_attribute_never_matches_typed_condition(self, vip_users):
        typed = typed_conditions_from_mapping({"vip": False}, _vip_schema())
        execution = execute_conditions(typed, vip_users, schema=_vip_schema())
        assert [u["user_id"] for u in execution.matched] == ["u_bool_false"]
        assert "u_missing" not in {u["user_id"] for u in execution.matched}

    def test_missing_attribute_matches_legacy_empty_value_only(self):
        users = [{"user_id": "u1"}]
        empty = execute_conditions(legacy_conditions_from_mapping({"vip": ""}), users)
        nonempty = execute_conditions(legacy_conditions_from_mapping({"vip": "1"}), users)
        assert empty.member_count == 1
        assert nonempty.member_count == 0

    def test_text_condition_requires_real_string(self):
        users = [
            {"user_id": "u1", "level": "vip"},
            {"user_id": "u2", "level": 1},
            {"user_id": "u3"},
        ]
        conditions = ConditionSet(conditions=[
            _text("level", "vip"),
        ])
        execution = execute_conditions(
            conditions, users,
            schema=FieldTypeSchema(types={"level": FieldType.TEXT}),
        )
        assert [u["user_id"] for u in execution.matched] == ["u1"]


def _text(field_name: str, value: str):
    from app.segment_conditions import TypedCondition

    return TypedCondition(
        field=field_name, kind=ConditionKind.TEXT_EQUALS, value=value, raw_text=value,
    )


# ============================================================
# 二、旧条件迁移与缺证保留
# ============================================================

class TestLegacyMigration:
    def test_migrates_boolean_field_and_keeps_raw_text(self):
        report = migrate_legacy_conditions({"vip": "1"}, _vip_schema())
        assert report.blocked is False
        assert report.decisions[0].status == "migrated_boolean"
        assert report.decisions[0].raw_text == "1"
        assert report.typed_conditions.conditions[0].value is True

    def test_migrates_text_field_to_equals(self):
        schema = FieldTypeSchema(types={"level": FieldType.TEXT})
        report = migrate_legacy_conditions({"level": "10"}, schema)
        condition = report.typed_conditions.conditions[0]
        assert condition.kind == ConditionKind.TEXT_EQUALS
        assert report.decisions[0].status == "migrated_text"

    def test_missing_field_type_blocks_and_keeps_original(self):
        report = migrate_legacy_conditions({"vip": "1"}, FieldTypeSchema())
        assert report.blocked is True
        assert report.typed_conditions is None
        assert report.unverified_fields == ["vip"]
        assert report.decisions[0].status == "needs_review"
        assert report.decisions[0].raw_text == "1"
        assert report.pending_evidence[0].reason == "缺字段类型"
        assert report.legacy_snapshot == [
            {"field": "vip", "value": "1", "raw_text": "1"}
        ]

    def test_typed_execution_without_field_type_fails_gate(self, vip_users):
        typed = ConditionSet(conditions=[
            migrate_legacy_conditions({"vip": "1"}, _vip_schema()).typed_conditions.conditions[0]
        ])
        with pytest.raises(FieldTypeMissingError):
            execute_conditions(typed, vip_users, schema=FieldTypeSchema())

    def test_legacy_conditions_still_execute_when_field_type_missing(self, vip_users):
        legacy = legacy_conditions_from_mapping({"vip": "1"})
        check = check_conditions(legacy, FieldTypeSchema())
        assert check.ok is True  # 旧条件不因缺证被阻断
        execution = execute_conditions(legacy, vip_users, schema=FieldTypeSchema())
        assert execution.member_count == 3


# ============================================================
# 三、事件匹配读取类型化条件
# ============================================================

class TestEventMatching:
    def test_legacy_event_matching_keeps_contains_semantics(self):
        event = EventTrack(event_id="E1", event_name="购买", event_properties={"vip": "1"})
        # 事件匹配的旧语义一直是精确相等，"10" 不会命中 "1"。
        assert event.matches_properties({"vip": "10"}) is False
        assert event.matches_properties({"vip": "1"}) is True

    def test_typed_event_matching_requires_field_type(self, vip_users):
        event = EventTrack(event_id="E1", event_name="购买", event_properties={"vip": True})
        typed = typed_conditions_from_mapping({"vip": True}, _vip_schema())
        assert event.matches_properties(typed, schema=_vip_schema()) is True
        with pytest.raises(FieldTypeMissingError):
            event.matches_properties(typed, schema=FieldTypeSchema())

    def test_event_replay_marks_dropped_members(self, vip_users):
        events = [EventTrack(event_id="E1", event_name="会员事件", event_properties={"vip": "1"})]
        replay = replay_event_matching(events, vip_users, _vip_schema(), as_of="2026-11-22")
        entry = replay[0]
        # 事件匹配旧语义是精确相等："1" 和整数 1 命中，"10" 不命中。
        assert entry["legacy"]["member_count"] == 2
        assert entry["typed"]["member_count"] == 1
        assert set(entry["dropped_member_ids"]) == {"u_str_one", "u_int_one"}

    def test_event_replay_with_missing_type_keeps_evidence(self, vip_users):
        events = [EventTrack(event_id="E1", event_name="会员事件", event_properties={"vip": "1"})]
        entry = replay_event_matching(events, vip_users, FieldTypeSchema())[0]
        assert entry["typed"] is None
        assert entry["pending_evidence"][0]["raw_text"] == "1"


# ============================================================
# 四、人群圈选读取类型化条件、比较使用实际成员
# ============================================================

def _migrated_segment(segment_id: str, vip: bool) -> Segment:
    segment = Segment(
        segment_id=segment_id,
        segment_name=f"人群{segment_id}",
        conditions={"vip": "1" if vip else "0"},
    )
    typed = typed_conditions_from_mapping({"vip": vip}, _vip_schema())
    return segment.with_typed_conditions(typed, schema=_vip_schema())


class TestSegmentComparison:
    def test_segment_executes_typed_conditions(self, vip_users):
        segment = _migrated_segment("SEG_T", True)
        execution = segment.execute(vip_users)
        assert execution.member_count == 1
        assert segment.condition_version == ConditionVersion.TYPED_V1

    def test_report_uses_actual_members_not_prefilled_counts(
        self, vip_users, comparison_metric
    ):
        seg_a = _migrated_segment("SEG_T", True)
        seg_a.member_count = 999  # 故意写错，报告不应采信
        seg_b = _migrated_segment("SEG_F", False)
        compare = UserSegmentCompare(
            compare_id="CMP1", segment_a=seg_a, segment_b=seg_b,
            compare_metric=comparison_metric,
        )
        report = compare.generate_report(vip_users)
        assert report["version"] == ConditionVersion.TYPED_V1.value
        assert report["segment_a"]["member_count"] == 1
        assert report["segment_b"]["member_count"] == 1
        assert report["test"]["reason"] == "样本不足"  # 每组只有 1 名成员
        assert report["segment_a"]["member_ids"] == ["u_bool_true"]
        assert report["segment_b"]["member_ids"] == ["u_bool_false"]
        assert report["recomputable"] is True

    def test_mixed_versions_are_rejected(self, vip_users, comparison_metric):
        typed = _migrated_segment("SEG_T", True)
        legacy = Segment(
            segment_id="SEG_L", segment_name="旧人群", conditions={"vip": "1"},
        )
        compare = UserSegmentCompare(
            compare_id="CMP_MIX", segment_a=typed, segment_b=legacy,
            compare_metric=comparison_metric,
        )
        with pytest.raises(VersionConflictError):
            compare.version()
        with pytest.raises(VersionConflictError):
            compare.generate_report(vip_users)

    def test_legacy_report_is_labelled_and_recomputable(self, vip_users, comparison_metric):
        seg_a = Segment(
            segment_id="SEG_L1", segment_name="旧人群A", conditions={"vip": "1"},
        )
        seg_b = Segment(
            segment_id="SEG_L0", segment_name="旧人群B", conditions={"vip": "0"},
        )
        compare = UserSegmentCompare(
            compare_id="CMP_LEGACY", segment_a=seg_a, segment_b=seg_b,
            compare_metric=comparison_metric,
        )
        report = compare.generate_report(vip_users)
        assert report["version"] == ConditionVersion.LEGACY_V1.value
        assert "旧口径" in report["version_label"]
        assert "不等同新版结论" in report["version_label"]
        assert report["segment_a"]["member_count"] == 3
        assert report["legacy_conditions"]["segment_a"] == "vip 包含 1"
        # 旧报告用同样的旧成员和指标可以复算回原值
        again = UserSegmentCompare(
            compare_id="CMP_LEGACY", segment_a=seg_a, segment_b=seg_b,
            compare_metric=comparison_metric,
        ).generate_report(vip_users)
        assert again["test"] == report["test"]


# ============================================================
# 五、历史回放
# ============================================================

class TestReplayHistory:
    def test_replays_old_members_and_new_members(self, vip_users):
        segments = {"SEG_M": _migrated_segment("SEG_M", True)}
        records = [{"segment_id": "SEG_M", "as_of": "2026-11-21"}]
        replays = replay_history(
            records,
            segments,
            {"SEG_M": vip_users},
            {"SEG_M": _vip_schema()},
        )
        assert replays[0]["legacy_member_count"] == 3
        assert replays[0]["typed_member_count"] == 1
        assert len(replays[0]["dropped_member_ids"]) == 3

    def test_replay_segment_without_type_gives_no_typed_result(self, vip_users):
        segment = Segment(
            segment_id="SEG_P", segment_name="待迁人群", conditions={"vip": "1"},
        )
        replay = replay_segment(segment, vip_users, schema=FieldTypeSchema())
        assert replay.legacy_member_count == 3
        assert replay.typed_member_ids is None
        assert replay.pending_evidence[0].field == "vip"


# ============================================================
# 六、部署中断与日更
# ============================================================

@pytest.fixture
def two_segments():
    return [
        Segment(segment_id="SEG_DONE", segment_name="已迁人群", conditions={"vip": "1"}),
        Segment(segment_id="SEG_WAIT", segment_name="待证人群", conditions={"vip": "0"}),
    ]


def test_typed_segment_without_condition_set_is_rejected(vip_users):
    segment = Segment(
        segment_id="SEG_BAD",
        segment_name="标错版本",
        conditions={"vip": "1"},
        condition_version=ConditionVersion.TYPED_V1,
    )
    with pytest.raises(ConditionError):
        segment.execute(vip_users)


def test_deployment_stops_after_one_segment(two_segments, vip_users):
    run = run_deployment(
        release_id="REL_1",
        segments=two_segments,
        users_by_segment={"SEG_DONE": vip_users, "SEG_WAIT": vip_users},
        schemas_by_segment={
            "SEG_DONE": _vip_schema(),
            "SEG_WAIT": FieldTypeSchema(),  # 缺字段类型
        },
        report_date="2026-11-23",
        order=["SEG_DONE", "SEG_WAIT"],
        stop_after=1,
        pending_ready_at="2026-11-24 06:00",
    )
    assert run.interrupted is True
    assert run.interrupted_after == "SEG_DONE"
    assert run.deployment("SEG_DONE").status == DeploymentStatus.MIGRATED
    assert run.deployment("SEG_WAIT").status == DeploymentStatus.NOT_STARTED
    # 中断人群不生成迁移结论，但仍保留缺证原文本和待核原因
    assert run.deployment("SEG_WAIT").migration is None
    assert run.deployment("SEG_WAIT").pending_evidence[0].raw_text == "0"
    assert run.completed is False
    assert run.pending_segments == ["SEG_WAIT"]
    # 迁完的人群保留旧成员集合，旧报告人数仍可复算
    assert run.deployment("SEG_DONE").legacy_member_count == 3
    # 部署不改调用方的旧版人群对象：迁移后仍能按原文回放旧环境
    assert two_segments[0].condition_version == ConditionVersion.LEGACY_V1
    assert two_segments[0].conditions == {"vip": "1"}
    replay = replay_segment(two_segments[0], vip_users, schema=_vip_schema())
    assert replay.legacy_member_count == 3
    assert replay.typed_member_ids == ["u_bool_true"]


def test_daily_comparison_falls_back_to_labelled_legacy(
    two_segments, vip_users, comparison_metric
):
    run = run_deployment(
        release_id="REL_1",
        segments=two_segments,
        users_by_segment={"SEG_DONE": vip_users, "SEG_WAIT": vip_users},
        schemas_by_segment={"SEG_DONE": _vip_schema(), "SEG_WAIT": FieldTypeSchema()},
        report_date="2026-11-23",
        stop_after=1,
        pending_ready_at="2026-11-24 06:00",
    )
    compare = UserSegmentCompare(
        compare_id="CMP_DAILY",
        segment_a=two_segments[0],
        segment_b=two_segments[1],
        compare_metric=comparison_metric,
    )
    report = build_daily_comparison(compare, run, vip_users)
    assert report.published is True
    assert report.version == ConditionVersion.LEGACY_V1
    assert report.is_new_version is False
    assert "旧口径" in report.version_label
    # 整组都取旧版：一组已迁完也必须跟着旧口径，不能混版
    assert report.segment_a["member_count"] == 3
    assert report.segment_b["member_count"] == 1
    assert report.pending_segments == ["SEG_WAIT"]
    assert report.pending_evidence[0]["field"] == "vip"
    assert report.earliest_new_version_at == "2026-11-24 06:00"


def test_daily_comparison_can_wait_for_typed(two_segments, vip_users, comparison_metric):
    run = run_deployment(
        release_id="REL_1",
        segments=two_segments,
        users_by_segment={"SEG_DONE": vip_users, "SEG_WAIT": vip_users},
        schemas_by_segment={"SEG_DONE": _vip_schema(), "SEG_WAIT": FieldTypeSchema()},
        report_date="2026-11-23",
        stop_after=1,
        pending_ready_at="2026-11-24 06:00",
    )
    compare = UserSegmentCompare(
        compare_id="CMP_DAILY",
        segment_a=two_segments[0],
        segment_b=two_segments[1],
        compare_metric=comparison_metric,
    )
    report = build_daily_comparison(
        compare, run, vip_users, policy=PublishPolicy.REQUIRE_TYPED
    )
    assert report.published is False
    assert report.is_new_version is False
    assert "等待新版" in report.reason
    assert report.earliest_new_version_at == "2026-11-24 06:00"


def test_daily_comparison_uses_typed_when_both_migrated(
    two_segments, vip_users, comparison_metric
):
    run = run_deployment(
        release_id="REL_2",
        segments=two_segments,
        users_by_segment={"SEG_DONE": vip_users, "SEG_WAIT": vip_users},
        schemas_by_segment={"SEG_DONE": _vip_schema(), "SEG_WAIT": _vip_schema()},
        report_date="2026-11-24",
    )
    assert run.completed is True
    compare = UserSegmentCompare(
        compare_id="CMP_TYPED",
        segment_a=two_segments[0],
        segment_b=two_segments[1],
        compare_metric=comparison_metric,
    )
    report = build_daily_comparison(compare, run, vip_users)
    assert report.published is True
    assert report.is_new_version is True
    assert report.version == ConditionVersion.TYPED_V1
    assert report.segment_a["member_count"] == 1
    assert report.segment_b["member_count"] == 1


def test_on_call_review_reports_versions_and_ready_time(
    two_segments, vip_users, comparison_metric
):
    run = run_deployment(
        release_id="REL_1",
        segments=two_segments,
        users_by_segment={"SEG_DONE": vip_users, "SEG_WAIT": vip_users},
        schemas_by_segment={"SEG_DONE": _vip_schema(), "SEG_WAIT": FieldTypeSchema()},
        report_date="2026-11-23",
        stop_after=1,
        pending_ready_at="2026-11-24 06:00",
    )
    compare = UserSegmentCompare(
        compare_id="CMP_DAILY",
        segment_a=two_segments[0],
        segment_b=two_segments[1],
        compare_metric=comparison_metric,
    )
    legacy_report = build_daily_comparison(compare, run, vip_users)
    review = on_call_review(run, [legacy_report])
    assert review["status"] == "waiting_for_evidence"
    assert review["new_version_reports"] == []
    assert review["legacy_version_reports"] == ["CMP_DAILY"]
    assert review["pending_segments"] == ["SEG_WAIT"]
    assert review["pending_evidence"] == [
        {"segment_id": "SEG_WAIT", "field": "vip", "reason": "缺字段类型", "raw_text": "0"}
    ]
    assert review["earliest_new_version_at"] == "2026-11-24 06:00"
    assert review["can_publish_new_version_today"] is False
    # 两群比较指标来自实际成员，仍是旧口径
    received = review["reports_received"][0]
    assert received["version"] == ConditionVersion.LEGACY_V1.value
    assert received["metric"]["reason"] == "样本不足"
    assert received["segment_a"]["member_count"] == 3
    assert received["segment_b"]["member_count"] == 1
