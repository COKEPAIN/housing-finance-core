"""사용자가 넣은 부대비용이 §14.2 총비용 점수를 여는가.

부대비용 원문에는 국민주택채권 매입·할인비용처럼 **실행 시점 시세**에 달린 항목이
있어 검수표로 확정할 수 없다. 은행이 실행 직전에 안내하므로 사용자가 그 값을 넣을
수 있게 하고, 넣기 전에는 총비용 점수를 산출하지 않는다.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.data_pipeline.curated.loan_combinations import KB_CREDIT, KB_MORTGAGE
from app.regulations.mortgage_limits import HousingStatus
from app.rule_engine.product_packs.handoff import ProductCandidate
from app.schemas.simulation import (
    FinancialSnapshot,
    HousingGoal,
    LoanRequestInput,
    SimulationInput,
    UserProfile,
)
from app.services.simulation_orchestrator import run_simulation

_AS_OF = date(2026, 7, 31)
_CALCULATED_AT = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
_SIMULATION_ID = UUID("00000000-0000-0000-0000-0000000000c1")

_FACTS_RATE = "4.86"


def _candidate(name: str) -> ProductCandidate:
    return ProductCandidate(
        product_name=name,
        base_data={"fin_prdt_nm": name},
        option_list=(
            {
                "fin_prdt_nm": name,
                "mrtg_type_nm": "아파트",
                "rpay_type_nm": "분할상환",
                "lend_rate_type_nm": "변동금리",
                "lend_rate_min": _FACTS_RATE,
                "lend_rate_max": _FACTS_RATE,
                "lend_rate_avg": _FACTS_RATE,
            },
        ),
    )


def _payload(
    costs: dict[str, Decimal] | None = None,
    *,
    liquid_assets: str = "550000000",
) -> SimulationInput:
    return SimulationInput(
        profile=UserProfile(
            age=36,
            annual_income=Decimal("70000000"),
            is_first_home_buyer=True,
            region_code="11680",
        ),
        housing_goal=HousingGoal(
            target_amount=Decimal("900000000"),
            target_date=date(2029, 12, 31),
            region_code="11680",
        ),
        financial_snapshot=FinancialSnapshot(
            monthly_income=Decimal("5800000"),
            monthly_expense=Decimal("2600000"),
            liquid_assets=Decimal(liquid_assets),
            monthly_debt_payment=Decimal("300000"),
        ),
        loan_request=LoanRequestInput(
            months=360,
            housing_status=HousingStatus.FIRST_HOME_BUYER,
            monthly_essential_expense=Decimal("2000000"),
            credit_loan_balance=Decimal("10000000"),
            loan_incidental_costs=costs,
        ),
    )


def _plans(
    costs: dict[str, Decimal] | None,
    *,
    liquid_assets: str = "550000000",
) -> list[dict]:
    result = run_simulation(
        _payload(costs, liquid_assets=liquid_assets),
        simulation_id=_SIMULATION_ID,
        as_of=_AS_OF,
        calculated_at=_CALCULATED_AT,
        loan_candidates=[_candidate(KB_MORTGAGE), _candidate(KB_CREDIT)],
    )
    section = result.loan_combination
    if not section.result:
        return []
    return list(section.result.get("plans") or [])


class TestTheCostGateIsAllOrNothing:
    def test_without_costs_the_total_cost_score_stays_unknown(self) -> None:
        plans = _plans(None)

        assert plans, "조합안이 나와야 비교할 수 있다"
        for plan in plans:
            assert "total_cost" in (plan.get("missing_score_components") or [])
            assert plan["total_financial_cost"] is None

    def test_a_partial_input_does_not_open_the_score(self) -> None:
        """일부만 넣으면 비용을 빠뜨린 상품이 유리해진다 — 그래서 열지 않는다."""
        plans = _plans({KB_MORTGAGE: Decimal("1200000")})

        assert plans
        assert any(
            "total_cost" in (plan.get("missing_score_components") or []) for plan in plans
        )

    def test_full_input_alone_does_not_open_the_score_when_nothing_covers(self) -> None:
        """비용을 다 넣어도 **전액 조달 조합이 없으면** 총비용은 산출되지 않는다.

        §14.2가 "대출금액과 기간을 동일하게 맞추고" 비교하라고 정하기 때문이다.
        덜 빌린 조합은 비용이 작을 뿐이라 같은 기준이 아니다. 게이트가 둘인 셈이며,
        이 사실을 몰라 처음에 "비용만 넣으면 100%"라고 잘못 기대했다.
        """
        plans = _plans({KB_MORTGAGE: Decimal("1200000"), KB_CREDIT: Decimal("70000")})

        assert plans
        assert all(not plan["covers_required_amount"] for plan in plans)
        assert all(
            "total_cost" in (plan.get("missing_score_components") or []) for plan in plans
        )
        # 비용 자체는 계산돼 보고서에 실린다 — 점수화만 하지 않는다.
        assert plans[0]["total_financial_cost"] is not None

    def test_full_input_completes_the_weights_once_a_plan_covers(self) -> None:
        """전액 조달 조합이 있고 비용도 다 있으면 §14 다섯 항목이 모두 찬다."""
        plans = _plans(
            {KB_MORTGAGE: Decimal("1200000"), KB_CREDIT: Decimal("70000")},
            liquid_assets="750000000",
        )

        assert plans
        best = plans[0]
        assert best["covers_required_amount"] is True
        assert best["total_financial_cost"] is not None
        # 상환유연성은 검수표에서, 총비용은 사용자 입력에서 왔다.
        assert best["missing_score_components"] == []
        assert Decimal(str(best["score_completeness"])) == Decimal(1)
        assert best["score_status"] == "COMPLETE"

    def test_the_cost_is_added_to_interest_not_replacing_it(self) -> None:
        """총 금융비용 = 총 이자 + 부대비용(§13.4)."""
        costs = {KB_MORTGAGE: Decimal("1200000"), KB_CREDIT: Decimal("70000")}
        best = _plans(costs)[0]

        interest = Decimal(str(best["total_interest"]))
        total = Decimal(str(best["total_financial_cost"]))
        used = sum(
            (costs[leg["product_name"]] for leg in best["legs"]),
            Decimal(0),
        )
        assert total == interest + used


class TestInputRules:
    def test_a_negative_cost_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="음수"):
            LoanRequestInput(
                months=360,
                housing_status=HousingStatus.FIRST_HOME_BUYER,
                monthly_essential_expense=Decimal("2000000"),
                loan_incidental_costs={KB_MORTGAGE: Decimal("-1")},
            )

    def test_a_blank_product_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="상품명"):
            LoanRequestInput(
                months=360,
                housing_status=HousingStatus.FIRST_HOME_BUYER,
                monthly_essential_expense=Decimal("2000000"),
                loan_incidental_costs={"  ": Decimal("1000")},
            )

    def test_an_explicit_supplement_wins_over_the_user_input(self) -> None:
        """확인된 값을 사용자 입력이 덮으면 근거가 뒤바뀐다."""
        from app.services.recommendation import LoanRecommendationSupplement

        result = run_simulation(
            _payload({KB_MORTGAGE: Decimal("9999999")}),
            simulation_id=_SIMULATION_ID,
            as_of=_AS_OF,
            calculated_at=_CALCULATED_AT,
            loan_candidates=[_candidate(KB_MORTGAGE)],
            loan_supplements={
                KB_MORTGAGE: LoanRecommendationSupplement(
                    additional_financial_cost=Decimal("1200000")
                )
            },
        )

        section = result.loan_combination
        assert section.result
        plans = section.result.get("plans") or []
        assert plans
        best = plans[0]
        interest = Decimal(str(best["total_interest"]))
        assert Decimal(str(best["total_financial_cost"])) == interest + Decimal("1200000")
