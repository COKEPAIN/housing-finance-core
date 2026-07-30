"""각 계산 엔진의 결과를 버전형 ``SimulationResult`` JSON 계약으로 조립한다.

목적:
    엔진 dataclass와 외부 API·보고서 계약 사이에 명시적인 변환 경계를 둔다.
기능:
    Decimal을 문자열로, 날짜를 ISO 형식으로, Enum을 값으로 변환하고 엔진별
    실행 여부·결측·근거·정책 출처를 공통 형식으로 모은다.
근거:
    계산 엔진은 정밀한 Python 타입을 유지하고 외부 계층은 안정적인 JSON만
    소비해야 한다. 변환 중 ``None``을 삭제하거나 0으로 바꾸지 않는다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.engines.cashflow.models import CashflowResult
from app.engines.loan.combination_models import LoanCombinationResult
from app.engines.recommendation.models import CombinedRecommendationResult
from app.engines.savings.portfolio_models import SavingsPortfolioResult
from app.engines.strategy.models import StrategyComparisonResult
from app.engines.stress.models import StressTestResult
from app.schemas.simulation import (
    CalculationSection,
    FinancialSnapshot,
    HousingGoalSummary,
    PublicUserSummary,
    SectionRunStatus,
    SimulationInput,
    SimulationResult,
)
from app.services.loan_simulation import LoanSimulationResult


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def to_json_value(value: object) -> Any:
    """엔진 값을 정밀도 손실 없이 JSON 기본형으로 바꾼다.

    float로 바꾸면 원 단위·비율 계산값이 달라질 수 있으므로 Decimal은 문자열로
    직렬화한다. 지원하지 않는 임의 객체는 조용히 문자열화하지 않고 예외를 내
    계약 누락을 테스트 단계에서 발견하게 한다.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if isinstance(value, BaseModel):
        return to_json_value(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_json_value(item) for item in sorted(value, key=repr)]
    raise TypeError(f"JSON으로 변환할 수 없는 계산 결과 타입입니다: {type(value)!r}")


def _string_status(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _attribute_strings(result: object, *names: str) -> tuple[str, ...]:
    values: list[str] = []
    for name in names:
        candidate = (
            result.get(name, ()) if isinstance(result, Mapping) else getattr(result, name, ())
        )
        if candidate is None:
            continue
        if isinstance(candidate, str):
            values.append(candidate)
        else:
            values.extend(str(item) for item in candidate)
    return _dedupe(values)


def build_calculation_section(
    result: object | None,
    *,
    section_schema_version: str,
    engine_status: str | None = None,
    missing_inputs: Sequence[str] = (),
    reasons: Sequence[str] = (),
    assumptions: Sequence[str] = (),
    policy_sources: Sequence[str] = (),
) -> CalculationSection:
    """실행하지 않은 구간과 완료된 구간을 같은 공용 구조로 만든다."""

    if result is None:
        return CalculationSection(
            run_status=SectionRunStatus.NOT_RUN,
            section_schema_version=section_schema_version,
            engine_status=None,
            result=None,
            missing_inputs=_dedupe(tuple(missing_inputs)),
            reasons=_dedupe(tuple(reasons)),
            assumptions=_dedupe(tuple(assumptions)),
            policy_sources=_dedupe(tuple(policy_sources)),
        )

    status_value = (
        result.get("status") if isinstance(result, Mapping) else getattr(result, "status", None)
    )
    resolved_status = engine_status or _string_status(status_value)
    if resolved_status is None and isinstance(result, LoanSimulationResult):
        resolved_status = "READY" if result.is_resolved else "NEEDS_REVIEW"
    if resolved_status is None:
        resolved_status = "COMPLETED"

    payload = to_json_value(result)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return CalculationSection(
        run_status=SectionRunStatus.COMPLETED,
        section_schema_version=section_schema_version,
        engine_status=resolved_status,
        result=payload,
        missing_inputs=_dedupe(
            tuple(missing_inputs) + _attribute_strings(result, "missing_inputs")
        ),
        reasons=_dedupe(tuple(reasons) + _attribute_strings(result, "reasons")),
        assumptions=_dedupe(
            tuple(assumptions) + _attribute_strings(result, "assumptions", "notes", "scope_notes")
        ),
        policy_sources=_dedupe(
            tuple(policy_sources) + _attribute_strings(result, "policy_sources")
        ),
    )


def _user_summary(payload: SimulationInput) -> PublicUserSummary:
    profile = payload.profile
    snapshot: FinancialSnapshot = payload.financial_snapshot
    return PublicUserSummary(
        persona_name=profile.persona_name,
        age=profile.age,
        household_size=profile.household_size,
        annual_income=profile.annual_income,
        employment_type=profile.employment_type,
        is_first_home_buyer=profile.is_first_home_buyer,
        is_married=profile.is_married,
        region_code=profile.region_code,
        monthly_income=snapshot.monthly_income,
        monthly_expense=snapshot.monthly_expense,
        liquid_assets=snapshot.liquid_assets,
        housing_assets=snapshot.housing_assets,
        total_debt=snapshot.total_debt,
        monthly_debt_payment=snapshot.monthly_debt_payment,
        emergency_reserve=snapshot.emergency_reserve,
    )


def _goal_summary(payload: SimulationInput) -> HousingGoalSummary:
    goal = payload.housing_goal
    return HousingGoalSummary(
        goal_type=goal.goal_type,
        target_amount=goal.resolved_target_amount,
        target_date=goal.target_date,
        region_code=goal.region_code,
        monthly_rent=goal.monthly_rent,
        management_fee=goal.management_fee,
    )


def build_simulation_result(
    payload: SimulationInput,
    *,
    simulation_id: UUID,
    as_of: date,
    calculated_at: datetime,
    cashflow_result: CashflowResult | Mapping[str, object] | None = None,
    savings_portfolio_result: SavingsPortfolioResult | None = None,
    loan_simulation_result: LoanSimulationResult | None = None,
    loan_combination_result: LoanCombinationResult | None = None,
    recommendation_result: CombinedRecommendationResult | None = None,
    stress_test_result: StressTestResult | None = None,
    strategy_comparison_result: StrategyComparisonResult | None = None,
    warnings: Sequence[str] = (),
) -> SimulationResult:
    """현재까지 실행된 모든 엔진 결과를 단일 JSON 원본으로 조립한다."""

    if recommendation_result is not None and recommendation_result.as_of != as_of:
        raise ValueError("종합추천 결과와 시뮬레이션 기준일이 다릅니다.")
    if stress_test_result is not None and stress_test_result.as_of != as_of:
        raise ValueError("스트레스 결과와 시뮬레이션 기준일이 다릅니다.")
    if strategy_comparison_result is not None and strategy_comparison_result.as_of != as_of:
        raise ValueError("전략 비교 결과와 시뮬레이션 기준일이 다릅니다.")

    stress_missing = (
        tuple(item for scenario in stress_test_result.scenarios for item in scenario.missing_inputs)
        if stress_test_result is not None
        else ()
    )
    sections = {
        "cashflow": build_calculation_section(
            cashflow_result,
            section_schema_version="cashflow@1.0.0",
        ),
        "savings_portfolio": build_calculation_section(
            savings_portfolio_result,
            section_schema_version="savings-portfolio@1.0.0",
        ),
        "loan_simulation": build_calculation_section(
            loan_simulation_result,
            section_schema_version="loan-simulation@1.0.0",
        ),
        "loan_combination": build_calculation_section(
            loan_combination_result,
            section_schema_version="loan-combination@1.0.0",
        ),
        "recommendation": build_calculation_section(
            recommendation_result,
            section_schema_version="combined-recommendation@1.0.0",
        ),
        "stress_test": build_calculation_section(
            stress_test_result,
            section_schema_version="lifestyle-stress@1.0.0",
            missing_inputs=stress_missing,
        ),
        "strategy_comparison": build_calculation_section(
            strategy_comparison_result,
            section_schema_version="purchase-strategy@1.0.0",
        ),
    }
    all_sections = tuple(sections.values())
    missing = _dedupe(tuple(item for section in all_sections for item in section.missing_inputs))
    sources = _dedupe(tuple(item for section in all_sections for item in section.policy_sources))
    return SimulationResult(
        simulation_id=simulation_id,
        as_of=as_of,
        calculated_at=calculated_at,
        user_summary=_user_summary(payload),
        goal=_goal_summary(payload),
        cashflow=sections["cashflow"],
        savings_portfolio=sections["savings_portfolio"],
        loan_simulation=sections["loan_simulation"],
        loan_combination=sections["loan_combination"],
        recommendation=sections["recommendation"],
        stress_test=sections["stress_test"],
        strategy_comparison=sections["strategy_comparison"],
        missing_inputs=missing,
        warnings=_dedupe(tuple(warnings)),
        policy_sources=sources,
    )


__all__ = [
    "build_calculation_section",
    "build_simulation_result",
    "to_json_value",
]
