"""Deterministic Adaptive Step Budget policy tests."""

from app.agent.budget import (
    BudgetProgress,
    StepBudgetTelemetry,
    StepBudgetPolicyName,
    create_step_budget_policy,
    production_step_budget_name,
)


def _progress(**overrides) -> BudgetProgress:
    payload = {
        "step_count": 20,
        "current_step_limit": 20,
        "final_test_passed": False,
        "plan_active": False,
        "plan_stale": True,
        "has_seen_test_failure": False,
        "repair_rounds": 0,
    }
    payload.update(overrides)
    return BudgetProgress(**payload)


def test_no_progress_does_not_extend():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(_progress(recent_execution_events=[]))
    assert decision.extend is False
    assert decision.reason == "exploration_only"


def test_recent_plan_allows_extension():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(
        _progress(
            plan_active=True,
            last_plan_step=19,
            recent_execution_events=["step 19: create_repair_plan"],
        )
    )
    assert decision.extend is True
    assert decision.extra_steps == 5
    assert decision.reason == "recent_repair_plan_pending"


def test_patch_awaiting_test_allows_extension():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(
        _progress(
            last_successful_patch_step=19,
            recent_execution_events=["step 19: apply_patch"],
        )
    )
    assert decision.extend is True
    assert decision.reason == "patch_attempted_and_unverified"


def test_repair_cycle_allows_extension():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(
        _progress(
            has_seen_test_failure=True,
            repair_rounds=1,
            last_successful_patch_step=18,
            last_test_step=18,
            last_test_failed=True,
            recent_execution_events=[
                "step 18: apply_patch",
                "step 18: run_tests",
            ],
        )
    )
    assert decision.extend is True
    assert decision.reason == "patch_attempted_and_unverified"


def test_success_never_extends():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(
        _progress(
            final_test_passed=True,
            recent_execution_events=["step 20: run_tests"],
        )
    )
    assert decision.extend is False
    assert decision.reason == "success"


def test_hard_cap_never_extends():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(
        _progress(
            step_count=30,
            current_step_limit=30,
            last_successful_patch_step=29,
            recent_execution_events=["step 29: apply_patch"],
        )
    )
    assert decision.extend is False
    assert decision.reason == "hard_cap_reached"


def test_two_stage_policy_rechecks_after_five_steps():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    first = policy.should_extend(
        _progress(
            last_successful_patch_step=19,
            recent_execution_events=["step 19: apply_patch"],
        )
    )
    assert first.extend is True
    second = policy.should_extend(
        _progress(
            step_count=25,
            current_step_limit=25,
            last_successful_patch_step=19,
            recent_execution_events=[],
        )
    )
    assert second.extend is False
    assert second.reason == "exploration_only"


def test_extension_telemetry_serializes():
    policy = create_step_budget_policy(StepBudgetPolicyName.ADAPTIVE_20_30)
    decision = policy.should_extend(
        _progress(
            last_successful_patch_step=19,
            recent_execution_events=["step 19: apply_patch"],
        )
    )
    telemetry = StepBudgetTelemetry(
        policy=policy.name,
        base_step_limit=20,
        hard_step_limit=30,
        effective_step_limit=25,
        extensions_granted=1,
        extension_steps_granted=decision.extra_steps,
        extension_reason=decision.reason,
        step_at_extension=20,
        last_progress_event=decision.last_progress_event,
        termination_reason="adaptive_extended_then_limit",
    )
    restored = StepBudgetTelemetry.model_validate_json(
        telemetry.model_dump_json()
    )
    assert restored.extensions_granted == 1
    assert restored.extension_reason == "patch_attempted_and_unverified"


def test_budget_progress_has_no_ground_truth_fields():
    forbidden = {
        "ground_truth",
        "ground_truth_files",
        "hidden_tests",
        "developer_patch",
        "relevant_tests",
    }
    assert forbidden.isdisjoint(BudgetProgress.model_fields)


def test_fixed_policies_share_interface():
    fixed20 = create_step_budget_policy(StepBudgetPolicyName.FIXED_20)
    fixed30 = create_step_budget_policy(StepBudgetPolicyName.FIXED_30)
    assert fixed20.base_step_limit == 20
    assert fixed20.hard_step_limit == 20
    assert fixed20.should_extend(_progress()).extend is False
    assert fixed30.base_step_limit == 30
    assert fixed30.hard_step_limit == 30


def test_production_default_is_adaptive_with_legacy_test_limits():
    assert production_step_budget_name(20) == "adaptive_20_30"
    assert production_step_budget_name(3) == "fixed_3"
