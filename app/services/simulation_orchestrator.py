"""``SimulationInput`` 하나를 ``SimulationResult`` JSON까지 통과시키는 계층.

목적:
    현금흐름 → 대출 → 종합추천 → 생활 스트레스 → 전략 비교를 정해진 순서로 호출하고
    ``build_simulation_result()``로 단일 JSON을 만든다. HTTP 경계(``/simulations``)는
    이 함수만 부르면 되고, 엔진 호출 순서를 다시 알 필요가 없다.

기능:
    실행할 수 없는 구간은 **추측으로 채우지 않고** ``NOT_RUN``으로 남기며,
    무엇이 없어서 못 했는지를 ``missing_inputs``에 이름으로 담는다.

근거:
    공식 설계안 §22 파이프라인 순서와 저장소 전역의 UNKNOWN 계약(§22.1)을 따른다.
    특히 지역·주택보유상태·만기는 확정하지 못했을 때 기본값으로 물러서면 한도가
    **커지는** 축이므로(§22.4·§22.7) 계산을 시작하지 않는다.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.data_pipeline.adapters.loan_engine_adapter import BorrowerFinancialState
from app.data_pipeline.adapters.savings_portfolio_policy_adapter import (
    SavingsPortfolioPolicyValidation,
)
from app.engines.savings.portfolio_models import SavingsPortfolioResult
from app.engines.strategy.models import (
    DEFAULT_STRATEGY_POLICY,
    HousingCostScenario,
    StrategyPolicy,
)
from app.engines.stress.models import DEFAULT_STRESS_SCENARIOS, StressScenario
from app.regulations.regulated_regions import ResolvedRegion
from app.rule_engine.product_packs.handoff import ProductCandidate
from app.rule_engine.product_packs.registry import ProductRulePackRegistry
from app.schemas.simulation import GoalType, LoanRequestInput, SimulationInput, SimulationResult
from app.services.cashflow_diagnosis import diagnose_cashflow
from app.services.loan_combination import combine_loan_options
from app.services.loan_simulation import (
    LoanSimulationRequest,
    LoanSimulationResult,
    build_request_for_region,
    resolve_dti_region,
    simulate_loan_options,
)
from app.services.recommendation import LoanRecommendationSupplement, recommend_from_results
from app.services.simulation_result import build_simulation_result
from app.services.strategy_comparison import compare_recommended_purchase_strategies
from app.services.stress_simulation import stress_recommendation

# 대출 이후 구간을 실행하지 못했을 때 붙이는 사유. 사용자에게 "계산 불가"가 아니라
# "무엇을 알려주면 계산되는지"로 읽히게 한다(§19).
_LOAN_BLOCK_MISSING = "loan_request"
_CANDIDATES_MISSING = "loan_product_candidates"
_REGION_MISSING = "regulation_region"


def _region_code(payload: SimulationInput) -> str | None:
    """규제지역 판정에 쓸 지역코드.

    목표 주택의 소재지가 규제지역을 정하므로 ``housing_goal``이 우선이고,
    없을 때만 거주지(``profile``)로 물러선다. 둘을 섞지 않는다 — 지역 사실의
    출처가 둘이면 어느 쪽이 맞는지 알 수 없다(`685b9ec`와 같은 이유).
    """
    return payload.housing_goal.region_code or payload.profile.region_code


def _required_amount(
    payload: SimulationInput,
    loan_request: LoanRequestInput,
) -> tuple[Decimal, tuple[str, ...]]:
    """필요 대출금액과 그때 남긴 가정."""
    if loan_request.required_amount is not None:
        return loan_request.required_amount, ()
    derived = max(
        payload.housing_goal.resolved_target_amount - payload.financial_snapshot.liquid_assets,
        Decimal(0),
    )
    return derived, (
        "필요 대출금액을 목표금액 − 유동자산으로 파생했습니다. "
        "취득세·중개보수 등 부대비용이 빠져 있어 실제 필요금액은 더 클 수 있습니다.",
    )


def _user_facts(
    payload: SimulationInput,
    loan_request: LoanRequestInput,
    *,
    required_amount: Decimal,
) -> dict[str, object]:
    """Rule Pack에 넘길 차주 사실. **아는 것만 채운다.**

    여기서 채우지 않은 facts(``owned_house_count``, ``employment_months``,
    ``is_newlywed``, ``child_count`` 등)는 Rule Pack이 ``UNKNOWN``으로 돌려주고
    해당 상품은 ``unresolved``에 어떤 facts가 없는지와 함께 남는다. 그게 정상
    동작이므로 여기서 그럴듯한 값을 만들어 넣지 않는다 — 자유텍스트 자격조건을
    임의 판정하지 않는다는 규약과 같은 이유다(부록 B-3).

    ``owned_house_count``는 ``MULTI_HOUSE``일 때 채우지 않는다. 다주택은 2채
    이상이라는 뜻일 뿐이어서 2로 적으면 3채 차주를 통과시킬 수 있고, 그것은
    한도가 커지는 방향의 오류다.
    """
    status = loan_request.housing_status
    owns_house = status.value.startswith("ONE_HOUSE") or status.value == "MULTI_HOUSE"

    facts: dict[str, object] = {
        "age": payload.profile.age,
        "annual_income": payload.profile.annual_income,
        "is_first_home_buyer": payload.profile.is_first_home_buyer,
        "owns_house": owns_house,
        "requested_amount": required_amount,
        "loan_term_years": loan_request.months // 12,
    }
    if status.value in ("NO_HOUSE", "FIRST_HOME_BUYER"):
        facts["owned_house_count"] = 0
    elif status.value.startswith("ONE_HOUSE"):
        facts["owned_house_count"] = 1

    goal = payload.housing_goal
    if goal.goal_type is GoalType.JEONSE_DEPOSIT:
        facts["lease_deposit"] = goal.resolved_target_amount
    elif goal.goal_type is GoalType.MONTHLY_RENT_DEPOSIT:
        facts["lease_deposit"] = goal.resolved_target_amount
        if goal.monthly_rent is not None:
            facts["monthly_payment_amount"] = goal.monthly_rent
    return facts


def _borrower(
    payload: SimulationInput,
    loan_request: LoanRequestInput,
) -> tuple[BorrowerFinancialState, tuple[str, ...]]:
    snapshot = payload.financial_snapshot
    assumptions: list[str] = []

    existing_annual = loan_request.existing_annual_debt_service
    if existing_annual is None:
        existing_annual = snapshot.monthly_debt_payment * Decimal(12)
        if existing_annual > 0:
            assumptions.append(
                "기존 대출 연 원리금을 월 상환액 × 12로 파생했습니다. "
                "거치기간이나 만기일시상환이 섞여 있으면 실제와 다를 수 있습니다."
            )

    post_income = loan_request.post_purchase_monthly_income
    if post_income is None:
        post_income = snapshot.monthly_income
        assumptions.append("구매 후 월소득을 현재 월소득과 같다고 보았습니다.")

    post_expense = loan_request.post_purchase_monthly_expense
    if post_expense is None:
        post_expense = snapshot.monthly_expense
        assumptions.append(
            "구매 후 월지출을 현재 월지출과 같다고 보았습니다. "
            "관리비·재산세 증가는 생활 스트레스 시나리오에서 확인합니다."
        )

    borrower = BorrowerFinancialState(
        annual_income=payload.profile.annual_income,
        existing_annual_debt_service=existing_annual,
        post_purchase_monthly_income=post_income,
        post_purchase_monthly_expense=post_expense,
        other_existing_monthly_debt_service=snapshot.monthly_debt_payment,
        monthly_essential_expense=loan_request.monthly_essential_expense,
        safe_dsr=loan_request.safe_dsr,
        existing_annual_interest=loan_request.existing_annual_interest,
    )
    assumptions.append(
        f"안전 DSR {loan_request.safe_dsr * 100:.0f}%는 법정 상한이 아니라 "
        "서비스 내부 추천 기준입니다."
    )
    return borrower, tuple(assumptions)


def build_loan_request(
    payload: SimulationInput,
    *,
    as_of: date,
) -> tuple[LoanSimulationRequest | None, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``SimulationInput``에서 대출 시뮬레이션 요청을 만든다.

    반환값은 ``(요청, 결측, 사유, 가정)``이다. 요청이 ``None``이면 대출 구간을
    실행하지 않는다. **비규제로 물러서거나 만기를 기본값으로 채우지 않는다** —
    둘 다 한도를 크게 만드는 방향이라 보수적 하한이 존재하지 않는다.
    """
    loan_request = payload.loan_request
    if loan_request is None:
        return (
            None,
            (_LOAN_BLOCK_MISSING,),
            (
                "대출 계산에 필요한 만기·주택보유상태·필수생활비가 없어 "
                "대출 구간을 실행하지 않았습니다.",
            ),
            (),
        )

    regulation_as_of = loan_request.regulation_as_of or as_of
    required_amount, amount_assumptions = _required_amount(payload, loan_request)
    borrower, borrower_assumptions = _borrower(payload, loan_request)

    # 지역 해석과 `dti_region` 결정은 `build_request_for_region`에 맡긴다. 여기서
    # 다시 구현하면 지역 사실의 출처가 둘로 갈리고, 특히 `dti_region`을 빠뜨리면
    # 경기 차주에게 더 엄격한 서울 50%가 붙는다(`a65b81c`가 고친 문제와 같은 축).
    built = build_request_for_region(
        region_code=_region_code(payload),
        as_of=regulation_as_of,
        borrower=borrower,
        user_facts=_user_facts(payload, loan_request, required_amount=required_amount),
        house_price=payload.housing_goal.resolved_target_amount,
        housing_status=loan_request.housing_status,
        required_amount=required_amount,
        months=loan_request.months,
        rate_selection=loan_request.rate_selection,
        for_house_purchase=loan_request.for_house_purchase,
        credit_loan_balance=loan_request.credit_loan_balance,
    )
    if isinstance(built, ResolvedRegion):
        reason = (
            built.note or "지역을 규제지역 구분으로 확정하지 못해 대출 구간을 실행하지 않았습니다."
        )
        return (
            None,
            (_REGION_MISSING,),
            (
                reason,
                "지역을 확인하지 못한 상태에서 비규제지역으로 보면 LTV가 과대평가됩니다.",
            ),
            (),
        )

    reasons = (
        f"규제 기준일 {regulation_as_of.isoformat()}, "
        f"지역 판정 {built.zone.value}, "
        f"DTI 지역 {resolve_dti_region(built).value}",
    )
    return built, (), reasons, amount_assumptions + borrower_assumptions


def run_simulation(
    payload: SimulationInput,
    *,
    simulation_id: UUID,
    as_of: date,
    calculated_at: datetime,
    loan_candidates: Sequence[ProductCandidate] = (),
    registry: ProductRulePackRegistry | None = None,
    loan_supplements: Mapping[str, LoanRecommendationSupplement] | None = None,
    savings_portfolio_result: SavingsPortfolioResult | None = None,
    savings_validation: SavingsPortfolioPolicyValidation | None = None,
    stress_scenarios: tuple[StressScenario, ...] = DEFAULT_STRESS_SCENARIOS,
    housing_scenarios: tuple[HousingCostScenario, ...] = (),
    target_purchase_date: date | None = None,
    additional_accumulation_equity: Decimal = Decimal(0),
    strategy_policy: StrategyPolicy = DEFAULT_STRATEGY_POLICY,
) -> SimulationResult:
    """실행할 수 있는 구간을 모두 계산해 단일 ``SimulationResult``로 조립한다.

    현금흐름 진단은 공용 금융 스냅샷으로 먼저 실행한다. 세부 이력처럼 공용 계약에
    아직 없는 값은 만들지 않고 현금흐름 결과의 ``missing_inputs``에 남긴다.
    전략 비교는 ``housing_scenarios``를 호출자가 근거와 함께 넘길 때만 실행한다 —
    미래 집값을 엔진이 만들어내지 않는다는 규약(§15) 때문이다.
    """
    cashflow_result = diagnose_cashflow(payload, as_of=as_of)
    loan_request, loan_missing, loan_reasons, loan_assumptions = build_loan_request(
        payload,
        as_of=as_of,
    )

    loan_result: LoanSimulationResult | None = None
    if loan_request is not None and not loan_candidates:
        # 후보 0건과 "자격을 통과한 상품이 없음"은 다른 상태다. 빈 목록으로
        # 계산하면 "가능한 상품이 없다"로 읽히므로 결측으로 남긴다.
        loan_missing = (_CANDIDATES_MISSING,)
        loan_reasons = (
            *loan_reasons,
            "대출 상품 후보가 전달되지 않아 계산하지 않았습니다. "
            "후보 0건은 '조건을 만족하는 상품이 없음'과 다른 상태입니다.",
        )
    elif loan_request is not None:
        loan_result = simulate_loan_options(
            loan_request,
            loan_candidates,
            registry=registry,
        )

    # 조합안은 옵션별 한도가 나온 뒤에야 만들 수 있다. 후보를 함께 넘겨야
    # 전액 요청으로 탈락한 상품(신용대출 등)을 자기 한도만큼의 금액으로 다시
    # 판정한다 — 그러지 않으면 한도가 작은 상품이 조합에서 통째로 빠진다.
    combination = None
    if loan_request is not None and loan_result is not None:
        combination = combine_loan_options(
            loan_request,
            loan_result,
            supplements=loan_supplements,
            candidates=loan_candidates,
            registry=registry,
        )

    recommendation = None
    stress = None
    strategy = None
    if loan_result is not None or savings_portfolio_result is not None:
        recommendation = recommend_from_results(
            as_of=as_of,
            loan_request=loan_request if loan_result is not None else None,
            loan_result=loan_result,
            savings_result=savings_portfolio_result,
            savings_validation=savings_validation,
            loan_supplements=loan_supplements,
        )

    if recommendation is not None and loan_request is not None:
        stress = stress_recommendation(
            recommendation,
            loan_request=loan_request,
            scenarios=stress_scenarios,
        )

    if recommendation is not None and housing_scenarios:
        strategy = compare_recommended_purchase_strategies(
            recommendation,
            target_purchase_date=target_purchase_date or payload.housing_goal.target_date,
            housing_scenarios=housing_scenarios,
            early_purchase_equity=payload.financial_snapshot.liquid_assets,
            additional_accumulation_equity=additional_accumulation_equity,
            stress_result=stress,
            policy=strategy_policy,
        )

    result = build_simulation_result(
        payload,
        simulation_id=simulation_id,
        as_of=as_of,
        calculated_at=calculated_at,
        cashflow_result=cashflow_result,
        loan_simulation_result=loan_result,
        loan_combination_result=combination,
        recommendation_result=recommendation,
        stress_test_result=stress,
        savings_portfolio_result=savings_portfolio_result,
        strategy_comparison_result=strategy,
    )
    if not loan_missing and not loan_assumptions and not loan_reasons:
        return result
    # 대출 구간을 못 돌린 이유와 파생 가정은 조립 결과에 담기지 않으므로
    # 여기서 그 구간에 덧붙인다. 숫자만 내보내면 근거 없는 확언이 된다(§20).
    return result.model_copy(
        update={
            "loan_simulation": result.loan_simulation.model_copy(
                update={
                    "missing_inputs": tuple(
                        dict.fromkeys(result.loan_simulation.missing_inputs + loan_missing)
                    ),
                    "reasons": tuple(dict.fromkeys(result.loan_simulation.reasons + loan_reasons)),
                    "assumptions": tuple(
                        dict.fromkeys(result.loan_simulation.assumptions + loan_assumptions)
                    ),
                }
            ),
            "missing_inputs": tuple(dict.fromkeys(result.missing_inputs + loan_missing)),
        }
    )


__all__ = ["build_loan_request", "run_simulation"]
