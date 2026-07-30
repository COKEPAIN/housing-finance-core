import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.engines.loan.combination_models import (
    CombinationStatus,
    CreditStressRegime,
    LoanCombinationPlan,
    LoanCombinationResult,
    LoanLegAllocation,
    LoanLegKind,
)
from app.engines.recommendation import (
    CombinedRecommendationInput,
    DecisionStatus,
    SavingsPlanInput,
    SavingsPlanStatus,
    build_combined_recommendation,
)
from app.engines.savings.portfolio_models import (
    SavingsPortfolioResult,
    SavingsPortfolioStatus,
)
from app.engines.strategy import (
    HousingCostScenario,
    StrategyCandidateInput,
    StrategyComparisonInput,
    StrategyKind,
    compare_strategies,
)
from app.engines.stress import (
    InterestRateShockApplicability,
    StressScenario,
    StressScenarioKind,
    StressTestInput,
    run_stress_test,
)
from app.reports.context import build_report_ai_input
from app.schemas.simulation import (
    FinancialSnapshot,
    GoalType,
    HousingGoal,
    SectionRunStatus,
    SimulationInput,
    UserProfile,
)
from app.services.loan_simulation import LoanSimulationResult
from app.services.simulation_result import build_simulation_result, to_json_value

_AS_OF = date(2026, 7, 30)
_SIMULATION_ID = UUID("00000000-0000-0000-0000-000000000001")
_CALCULATED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _input(*, rent_goal: bool = False) -> SimulationInput:
    goal = (
        HousingGoal(
            goal_type=GoalType.MONTHLY_RENT_DEPOSIT,
            target_amount=Decimal("5000000"),
            target_date=date(2028, 7, 30),
            region_code="30200",
            monthly_rent=Decimal("200000"),
            management_fee=Decimal("50000"),
        )
        if rent_goal
        else HousingGoal(
            target_price=Decimal("300000000"),
            target_date=date(2028, 7, 30),
            region_code="11000",
        )
    )
    return SimulationInput(
        profile=UserProfile(
            persona_name="계약 테스트 페르소나",
            age=25,
            household_size=3,
            annual_income=Decimal("9600000"),
            employment_type="part_time",
            is_first_home_buyer=True,
            region_code="30200",
        ),
        housing_goal=goal,
        financial_snapshot=FinancialSnapshot(
            monthly_income=Decimal("800000"),
            monthly_expense=Decimal("700000"),
            liquid_assets=Decimal("1000000"),
            emergency_reserve=Decimal("700000"),
        ),
    )


def _savings_portfolio() -> SavingsPortfolioResult:
    return SavingsPortfolioResult(
        status=SavingsPortfolioStatus.NO_ALLOCATION_REQUIRED,
        allocations=(),
        monthly_allocated=Decimal(0),
        monthly_unallocated=Decimal(0),
        lump_sum_allocated=Decimal(0),
        lump_sum_unallocated=Decimal(0),
        coverage_ratio=Decimal(1),
        expected_total_principal=Decimal(0),
        expected_maturity_amount=Decimal(0),
        expected_net_interest=Decimal(0),
        weighted_product_score=None,
        expected_return_score=None,
        maturity_risk=None,
        concentration_risk=None,
        liquidity_shortfall=None,
        objective_score=None,
        reasons=("배분할 예산이 없습니다.",),
    )


def _recommendation():
    return build_combined_recommendation(
        CombinedRecommendationInput(
            as_of=_AS_OF,
            savings=SavingsPlanInput(
                status=SavingsPlanStatus.NO_ALLOCATION_REQUIRED,
                policy_status=DecisionStatus.PASS,
                allocations=(),
                coverage_ratio=Decimal(1),
                monthly_allocated=Decimal(0),
                monthly_unallocated=Decimal(0),
                lump_sum_allocated=Decimal(0),
                lump_sum_unallocated=Decimal(0),
                expected_total_principal=Decimal(0),
                expected_maturity_amount=Decimal(0),
                expected_net_interest=Decimal(0),
            ),
        )
    )


def _stress():
    baseline = StressScenario(
        code="BASE",
        name="기준",
        kind=StressScenarioKind.BASELINE,
    )
    return run_stress_test(
        StressTestInput(
            as_of=_AS_OF,
            loan_principal=Decimal(0),
            annual_rate=Decimal(0),
            months=12,
            annual_income=Decimal("9600000"),
            existing_annual_debt_service=Decimal(0),
            post_purchase_monthly_income=Decimal("800000"),
            post_purchase_monthly_expense=Decimal("300000"),
            other_existing_monthly_debt_service=Decimal(0),
            monthly_essential_expense=Decimal("300000"),
            safe_dsr=Decimal("0.4"),
            monthly_savings_commitment=Decimal("100000"),
            interest_rate_shock_applicability=(
                InterestRateShockApplicability.NOT_APPLIES
            ),
            scenarios=(baseline,),
        )
    )


def _strategy():
    scenario = HousingCostScenario(
        code="BASE",
        name="기준",
        early_purchase_total_cost=Decimal("300000000"),
        asset_accumulation_total_cost=Decimal("320000000"),
        is_baseline=True,
        basis_date=_AS_OF,
        source_note="계약 테스트",
    )
    return compare_strategies(
        StrategyComparisonInput(
            as_of=_AS_OF,
            target_purchase_date=date(2028, 7, 30),
            housing_scenarios=(scenario,),
            asset_accumulation=StrategyCandidateInput(
                kind=StrategyKind.ASSET_ACCUMULATION,
                planned_purchase_date=date(2028, 7, 30),
                available_equity=Decimal("120000000"),
                loan_capacity=Decimal("200000000"),
                total_financial_cost=Decimal("50000000"),
                cashflow_stability_score=Decimal("0.9"),
                plan_flexibility_score=Decimal("0.7"),
            ),
            early_purchase=StrategyCandidateInput(
                kind=StrategyKind.EARLY_PURCHASE,
                planned_purchase_date=_AS_OF,
                available_equity=Decimal("100000000"),
                loan_capacity=Decimal("200000000"),
                total_financial_cost=Decimal("80000000"),
                cashflow_stability_score=Decimal("0.8"),
                plan_flexibility_score=Decimal("0.8"),
            ),
        )
    )


def _stress_with_unknown_rate():
    baseline = StressScenario(
        code="BASE",
        name="기준",
        kind=StressScenarioKind.BASELINE,
    )
    rate_up = StressScenario(
        code="RATE",
        name="금리 상승",
        kind=StressScenarioKind.INTEREST_RATE,
        interest_rate_increase=Decimal("0.01"),
    )
    return run_stress_test(
        StressTestInput(
            as_of=_AS_OF,
            loan_principal=Decimal("100000000"),
            annual_rate=Decimal("0.03"),
            months=360,
            annual_income=Decimal("60000000"),
            existing_annual_debt_service=Decimal(0),
            post_purchase_monthly_income=Decimal("5000000"),
            post_purchase_monthly_expense=Decimal("1800000"),
            other_existing_monthly_debt_service=Decimal(0),
            monthly_essential_expense=Decimal("1800000"),
            safe_dsr=Decimal("0.4"),
            monthly_savings_commitment=Decimal("500000"),
            interest_rate_shock_applicability=(
                InterestRateShockApplicability.UNKNOWN
            ),
            scenarios=(baseline, rate_up),
        )
    )


def _full_result():
    return build_simulation_result(
        _input(),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
        cashflow_result={
            "status": "PARTIAL",
            "monthly_surplus": Decimal("100000"),
            "future_housing_cost": None,
        },
        savings_portfolio_result=_savings_portfolio(),
        loan_simulation_result=LoanSimulationResult(),
        recommendation_result=_recommendation(),
        stress_test_result=_stress(),
        strategy_comparison_result=_strategy(),
    )


def test_legacy_home_price_and_new_rent_goal_are_both_supported() -> None:
    legacy = _input()
    rent = _input(rent_goal=True)

    assert legacy.housing_goal.resolved_target_amount == Decimal("300000000")
    assert rent.housing_goal.resolved_target_amount == Decimal("5000000")
    assert rent.housing_goal.goal_type is GoalType.MONTHLY_RENT_DEPOSIT


def test_goal_rejects_conflicting_amounts_and_incomplete_rent_input() -> None:
    with pytest.raises(ValidationError, match="서로 다릅니다"):
        HousingGoal(
            target_amount=Decimal("1"),
            target_price=Decimal("2"),
            target_date=_AS_OF,
        )
    with pytest.raises(ValidationError, match="monthly_rent"):
        HousingGoal(
            goal_type=GoalType.MONTHLY_RENT_DEPOSIT,
            target_amount=Decimal("5000000"),
            target_date=_AS_OF,
        )


def test_all_engine_results_serialize_without_decimal_precision_loss() -> None:
    result = _full_result()
    raw_json = result.model_dump_json()
    payload = json.loads(raw_json)

    assert payload["schema_version"] == "1.0.0"
    assert payload["user_summary"]["annual_income"] == "9600000"
    assert (
        payload["cashflow"]["result"]["monthly_surplus"]
        == "100000"
    )
    assert payload["cashflow"]["engine_status"] == "PARTIAL"
    assert payload["cashflow"]["result"]["future_housing_cost"] is None
    assert payload["savings_portfolio"]["run_status"] == "COMPLETED"
    assert payload["loan_simulation"]["run_status"] == "COMPLETED"
    assert payload["recommendation"]["result"]["as_of"] == "2026-07-30"
    assert payload["stress_test"]["result"]["pass_ratio"] == "1"
    assert (
        payload["strategy_comparison"]["result"]["asset_accumulation"][
            "available_equity"
        ]
        == "120000000"
    )
    assert raw_json == result.model_dump_json()


def test_not_run_is_distinct_from_completed_empty_result() -> None:
    result = build_simulation_result(
        _input(),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
    )

    assert result.cashflow.run_status is SectionRunStatus.NOT_RUN
    assert result.cashflow.result is None
    assert result.loan_simulation.run_status is SectionRunStatus.NOT_RUN
    assert result.loan_combination.run_status is SectionRunStatus.NOT_RUN
    assert result.loan_combination.result is None


def test_the_combination_section_is_versioned_separately() -> None:
    """조합 절 추가는 하위 호환이라 최상위 `schema_version`을 올리지 않는다.

    소비자는 구간 버전(`section_schema_version`)으로 변경을 감지한다
    (`app/schemas/README.md` 「변경 원칙」).
    """
    result = build_simulation_result(
        _input(),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
    )

    assert result.schema_version == "1.0.0"
    assert result.loan_combination.section_schema_version == "loan-combination@1.0.0"


def test_the_combination_section_carries_plans_without_precision_loss() -> None:
    """조합 금액도 Decimal 문자열로 실린다 — float로 바꾸면 원 단위가 흔들린다."""
    combination = LoanCombinationResult(
        status=CombinationStatus.PARTIAL,
        plans=(
            LoanCombinationPlan(
                plan_id="leg-a|leg-b|BELOW|assessment_cost",
                legs=(
                    LoanLegAllocation(
                        candidate_id="leg-a",
                        product_id="KB 주택담보대출",
                        product_name="KB 주택담보대출",
                        option_name="고정·원리금균등",
                        kind=LoanLegKind.MORTGAGE,
                        amount=Decimal("242671379"),
                        monthly_payment=Decimal("1109000"),
                        assessment_monthly_payment=Decimal("1560000"),
                        total_interest=Decimal("156569621"),
                        annual_rate=Decimal("0.0365"),
                        assessment_annual_rate=Decimal("0.0665"),
                        months=360,
                    ),
                ),
                total_amount=Decimal("242671379"),
                funding_shortfall=Decimal("17328621"),
                covers_required_amount=False,
                monthly_payment=Decimal("1109000"),
                assessment_monthly_payment=Decimal("1560000"),
                expected_dsr=Decimal("0.3232"),
                assessment_dsr=Decimal("0.4000"),
                post_purchase_monthly_surplus=Decimal("890000"),
                stress_monthly_surplus=Decimal("440000"),
                total_interest=Decimal("156569621"),
                total_financial_cost=None,
                credit_regime=CreditStressRegime.BELOW,
            ),
        ),
    )

    result = build_simulation_result(
        _input(),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
        loan_combination_result=combination,
    )
    payload = json.loads(result.model_dump_json())
    section = payload["loan_combination"]

    assert section["run_status"] == "COMPLETED"
    assert section["result"]["status"] == "PARTIAL"
    plan = section["result"]["plans"][0]
    assert plan["total_amount"] == "242671379"
    assert plan["funding_shortfall"] == "17328621"
    assert plan["legs"][0]["amount"] == "242671379"
    assert plan["credit_regime"] == "BELOW"
    # 총비용을 확인하지 못한 상태가 0원으로 바뀌지 않는다.
    assert plan["total_financial_cost"] is None


def test_calculated_at_requires_a_timezone() -> None:
    with pytest.raises(ValidationError, match="시간대"):
        build_simulation_result(
            _input(),
            simulation_id=_SIMULATION_ID,
            as_of=_AS_OF,
            calculated_at=datetime(2026, 7, 30, 12, 0),
        )


def test_nested_stress_missing_inputs_are_aggregated_at_the_top_level() -> None:
    result = build_simulation_result(
        _input(),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
        stress_test_result=_stress_with_unknown_rate(),
    )

    assert "interest_rate_shock_applicability" in result.stress_test.missing_inputs
    assert "interest_rate_shock_applicability" in result.missing_inputs


def test_unknown_python_objects_are_not_silently_stringified() -> None:
    with pytest.raises(TypeError, match="JSON으로 변환할 수 없는"):
        to_json_value(object())


def test_report_input_removes_financial_identifiers_and_raw_transactions() -> None:
    result = build_simulation_result(
        _input(rent_goal=True),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
        cashflow_result={
            "status": "READY",
            "monthly_surplus": Decimal("100000"),
            "account_num": "40100102000001",
            "birth_date": "20010315",
            "nested": {
                "transaction_list": [{"secret": "원천 거래"}],
                "safe_fact": "유지",
            },
        },
    )

    report = build_report_ai_input(result)
    raw = report.model_dump_json()

    assert "40100102000001" not in raw
    assert "20010315" not in raw
    assert "원천 거래" not in raw
    assert "monthly_surplus" in raw
    assert "safe_fact" in raw
    assert report.goal.goal_type is GoalType.MONTHLY_RENT_DEPOSIT
    assert report.sections.savings.run_status is SectionRunStatus.NOT_RUN


def test_report_prefers_policy_validated_recommendation_summary() -> None:
    report = build_report_ai_input(_full_result())

    assert report.sections.savings.engine_status == "NOT_REQUIRED"
    assert report.sections.savings.facts is not None
    assert report.sections.savings.facts["allocations"] == []
    assert report.sections.loan.facts is not None
    assert report.sections.stress_test.facts is not None
    assert report.sections.strategy_comparison.facts is not None
    assert any("다시 계산하지 않습니다" in rule for rule in report.generation_rules)
