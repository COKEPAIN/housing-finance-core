"""계산 결과 → 조합 후보 어댑터: 확정하지 못한 값을 추측으로 채우지 않는가.

이 계층은 규제표와 검수표를 아는 유일한 자리다. 그래서 결측이 여기로 모이고,
결측을 만나면 다리를 **넣지 않고** 이름을 남기는지가 핵심이다.
"""

from datetime import date
from decimal import Decimal

from app.data_pipeline.adapters.loan_engine_adapter import (
    BorrowerFinancialState,
    LoanComputation,
)
from app.data_pipeline.curated.loan_combinations import (
    HF_BOGEUMJARI,
    KB_CREDIT,
    KB_MORTGAGE,
)
from app.data_pipeline.normalizers.loan_product import NormalizedLoanOption
from app.engines.loan.combination_models import CombinationStatus, LoanLegKind
from app.regulations.mortgage_limits import (
    DtiRegion,
    HousingStatus,
    RegulationZone,
    ResolvedPolicyLimit,
)
from app.rule_engine.product_packs.handoff import ProductCandidate
from app.rule_engine.product_packs.models import EvaluationStatus
from app.services.loan_combination import (
    build_combination_budget,
    build_combination_legs,
    combine_loan_options,
    probe_amount_limited_products,
)
from app.services.loan_simulation import (
    LoanSimulationRequest,
    LoanSimulationResult,
    simulate_loan_options,
)
from app.services.recommendation import LoanRecommendationSupplement

_AS_OF = date(2026, 7, 30)


def _borrower() -> BorrowerFinancialState:
    return BorrowerFinancialState(
        annual_income=Decimal("70000000"),
        existing_annual_debt_service=Decimal("3600000"),
        post_purchase_monthly_income=Decimal("5800000"),
        post_purchase_monthly_expense=Decimal("2600000"),
        other_existing_monthly_debt_service=Decimal("300000"),
        monthly_essential_expense=Decimal("2000000"),
        safe_dsr=Decimal("0.40"),
    )


def _request(**overrides: object) -> LoanSimulationRequest:
    defaults: dict[str, object] = {
        "borrower": _borrower(),
        "user_facts": {},
        "house_price": Decimal("900000000"),
        "zone": RegulationZone.SPECULATION_OVERHEATED,
        "housing_status": HousingStatus.FIRST_HOME_BUYER,
        "is_capital_region": True,
        "dti_region": DtiRegion.SEOUL,
        "required_amount": Decimal("350000000"),
        "months": 360,
        "as_of": _AS_OF,
        "credit_loan_balance": Decimal("10000000"),
    }
    defaults.update(overrides)
    return LoanSimulationRequest(**defaults)  # type: ignore[arg-type]


def _option(name: str, rate_type: str = "변동금리") -> NormalizedLoanOption:
    return NormalizedLoanOption(
        product_name=name,
        mortgage_type_name="아파트",
        repayment_type_name="분할상환",
        rate_type_name=rate_type,
        annual_rate_min=Decimal("0.04"),
        annual_rate_max=Decimal("0.05"),
        annual_rate_avg=Decimal("0.045"),
    )


def _computation(
    name: str = KB_MORTGAGE,
    *,
    amount: Decimal = Decimal("300000000"),
    annual_rate: Decimal | None = Decimal("0.045"),
    dsr_annual_rate: Decimal | None = Decimal("0.075"),
    minimum: Decimal | None = None,
) -> LoanComputation:
    return LoanComputation(
        product_name=name,
        option=_option(name),
        status=EvaluationStatus.PASS,
        amount=amount,
        product_minimum_amount=minimum,
        annual_rate=annual_rate,
        dsr_annual_rate=dsr_annual_rate,
        months=360,
    )


def _result(*computations: LoanComputation, ltv: Decimal | None = Decimal("360000000")):
    return LoanSimulationResult(
        executable=computations,
        ltv=ResolvedPolicyLimit(amount=ltv, binding_reason="LTV 40%"),
        policy_as_of=_AS_OF,
    )


class TestLegMapping:
    def test_an_executable_option_becomes_a_leg(self) -> None:
        legs, skipped = build_combination_legs(_request(), _result(_computation()))

        assert len(legs) == 1
        assert skipped == ()
        leg = legs[0]
        assert leg.product_name == KB_MORTGAGE
        assert leg.kind is LoanLegKind.MORTGAGE
        assert leg.annual_rate == Decimal("0.045")
        assert leg.assessment_annual_rate == Decimal("0.075")
        assert leg.months == 360

    def test_the_leg_cap_is_the_single_loan_maximum_and_says_so(self) -> None:
        """상한이 상품 한도가 아니라 여러 상한의 최솟값이므로 이름을 정확히 붙인다.

        "상품 한도"라고 표시하면 사용자에게 **틀린 사유**가 간다.
        """
        legs, _ = build_combination_legs(
            _request(), _result(_computation(amount=Decimal("299926758")))
        )

        assert legs[0].maximum_amount == Decimal("299926758")
        assert "단일 대출 한도" in legs[0].maximum_amount_label
        assert legs[0].dti_limit_amount is None

    def test_the_product_name_is_the_product_id(self) -> None:
        """원천 데이터에 상품 ID가 없다. 같은 상품의 옵션 중복 선택을 이 값이 막는다."""
        legs, _ = build_combination_legs(_request(), _result(_computation()))

        assert legs[0].product_id == KB_MORTGAGE

    def test_supplements_fill_the_cost_and_flexibility_scores(self) -> None:
        legs, _ = build_combination_legs(
            _request(),
            _result(_computation()),
            supplements={
                KB_MORTGAGE: LoanRecommendationSupplement(
                    additional_financial_cost=Decimal("500000"),
                    repayment_flexibility_score=Decimal("0.8"),
                )
            },
        )

        assert legs[0].additional_financial_cost == Decimal("500000")
        assert legs[0].repayment_flexibility_score == Decimal("0.8")

    def test_a_missing_supplement_leaves_the_scores_unknown(self) -> None:
        """0으로 채우면 총비용 0원·유연성 0점이라는 거짓이 된다."""
        legs, _ = build_combination_legs(_request(), _result(_computation()))

        assert legs[0].additional_financial_cost is None
        assert legs[0].repayment_flexibility_score is None


class TestNothingIsGuessed:
    def test_an_unclassified_product_is_dropped_with_a_reason(self) -> None:
        """담보 구분을 모르면 LTV 예산에 넣을지 알 수 없다.

        OTHER로 떨어뜨리면 LTV에서 빠져 한도가 커지는 방향으로 틀린다.
        """
        legs, skipped = build_combination_legs(
            _request(), _result(_computation("검수표에 없는 대출"))
        )

        assert legs == ()
        assert any("담보 구분" in reason for reason in skipped)

    def test_a_missing_assessment_rate_drops_the_leg(self) -> None:
        """심사금리를 모르면 DSR 예산을 얼마나 먹는지 모른다."""
        legs, skipped = build_combination_legs(
            _request(), _result(_computation(dsr_annual_rate=None))
        )

        assert legs == ()
        assert any("심사금리" in reason for reason in skipped)

    def test_a_missing_annual_rate_drops_the_leg(self) -> None:
        legs, skipped = build_combination_legs(
            _request(), _result(_computation(annual_rate=None))
        )

        assert legs == ()
        assert any("적용 금리" in reason for reason in skipped)

    def test_dropped_legs_are_reported_in_the_combination_result(self) -> None:
        """제외한 다리를 조용히 버리지 않는다."""
        result = combine_loan_options(
            _request(),
            _result(_computation(), _computation("검수표에 없는 대출")),
        )

        assert any("담보 구분" in reason for reason in result.reasons)

    def test_an_unresolved_ltv_blocks_the_whole_combination(self) -> None:
        """LTV를 모르고 조합하면 주담대 상한이 사라져 과대평가된다."""
        assert build_combination_budget(_request(), _result(ltv=None)) is None

        result = combine_loan_options(_request(), _result(_computation(), ltv=None))
        assert result.status is CombinationStatus.UNRESOLVED
        assert result.missing_inputs == ("ltv_limit_amount",)
        assert result.plans == ()

    def test_no_executable_option_is_missing_not_infeasible(self) -> None:
        result = combine_loan_options(_request(), _result())

        assert result.status is CombinationStatus.UNRESOLVED
        assert result.missing_inputs == ("loan_leg_candidates",)


class TestCreditStressThresholdWiring:
    def test_a_credit_leg_carries_the_above_threshold_rate(self) -> None:
        """조합이 새로 빌려 문턱을 넘을 수 있으므로 그 구간 금리를 함께 싣는다.

        `request.credit_loan_balance`는 **기존** 잔액이라 그 상태의 금리를 담고
        있지 않다. 기존 잔액이 1억 이하이면 가산금리가 0%로 확정되므로 문턱 아래
        심사금리는 실제 금리와 같다 — 실제 규제표가 그렇게 답한다.
        """
        legs, _ = build_combination_legs(
            _request(),
            _result(_computation(KB_CREDIT, dsr_annual_rate=Decimal("0.045"))),
        )

        leg = legs[0]
        assert leg.kind is LoanLegKind.CREDIT
        above = leg.assessment_annual_rate_above_credit_threshold
        assert above is not None
        assert above > leg.annual_rate, "문턱 위에서는 가산금리가 붙어야 한다"

    def test_a_contradictory_above_threshold_rate_is_treated_as_unknown(self) -> None:
        """문턱 위가 아래보다 느슨할 수는 없다. 모순이면 그 구간을 평가하지 않는다.

        억지로 끌어올리면 근거 없는 금리를 만들고, 그대로 넘기면 후보 생성이
        `ValueError`로 터져 조합 전체가 죽는다. None이 보수적이고 안전하다.
        """
        legs, skipped = build_combination_legs(
            _request(),
            # 규제표가 낼 문턱 위 금리(실제 + 1.50%p)보다 높은 심사금리가 들어온 경우.
            _result(_computation(KB_CREDIT, dsr_annual_rate=Decimal("0.20"))),
        )

        assert skipped == ()
        assert len(legs) == 1, "다리 자체는 살아 있어야 한다"
        assert legs[0].assessment_annual_rate_above_credit_threshold is None

    def test_a_mortgage_leg_has_no_threshold_rate(self) -> None:
        legs, _ = build_combination_legs(_request(), _result(_computation()))

        assert legs[0].assessment_annual_rate_above_credit_threshold is None

    def test_the_existing_balance_flows_into_the_budget(self) -> None:
        budget = build_combination_budget(_request(), _result(_computation()))

        assert budget is not None
        assert budget.existing_credit_loan_balance == Decimal("10000000")
        assert budget.credit_stress_threshold == Decimal("100000000")

    def test_an_unknown_balance_stays_unknown(self) -> None:
        """0으로 채우면 문턱을 넘는 조합에 가산금리가 빠진다."""
        budget = build_combination_budget(
            _request(credit_loan_balance=None), _result(_computation())
        )

        assert budget is not None
        assert budget.existing_credit_loan_balance is None
        assert budget.credit_headroom_below_threshold is None


def _candidate(name: str, rate: str = "4.86") -> ProductCandidate:
    """실제 Rule Pack이 붙는 상품 후보. 옵션은 금리 한 줄이면 충분하다."""
    return ProductCandidate(
        product_name=name,
        base_data={"fin_prdt_nm": name},
        option_list=(
            {
                "fin_prdt_nm": name,
                "mrtg_type_nm": "무보증",
                "rpay_type_nm": "분할상환",
                "lend_rate_type_nm": "변동금리",
                "lend_rate_min": rate,
                "lend_rate_max": rate,
                "lend_rate_avg": rate,
            },
        ),
    )


_CREDIT_FACTS: dict[str, object] = {
    "age": 36,
    "annual_income": Decimal("70000000"),
    "combined_annual_income": Decimal("70000000"),
    "is_first_home_buyer": True,
    "owns_house": False,
    "owned_house_count": 0,
    "loan_term_years": 30,
    "employment_months": 60,
    "credit_score": 900,
    "is_foreigner": False,
    "is_household_head": True,
    "salary_transfer_count": 6,
    "is_sole_proprietor": False,
    "is_pension_transfer_recipient": False,
    "financial_cost_burden_ratio": Decimal("0.25"),
    "requested_amount": Decimal("350000000"),
}


class TestAmountLimitedProductsAreReProbed:
    """전액 요청으로 탈락한 상품을 사유 해석이 아니라 **재판정**으로 되살린다.

    Rule Pack은 `requested_amount`(필요금액 전액)로 판정하므로 신용대출이
    "대출금액이 한도를 초과합니다"로 탈락한다. 조합의 전제는 그 상품이 일부만
    담당한다는 것이라, 이 탈락은 조합에서 성립하지 않는다.
    """

    def test_a_product_rejected_only_on_amount_comes_back(self) -> None:
        request = _request(user_facts=_CREDIT_FACTS)
        candidate = _candidate("KB 급여이체신용대출")

        # 전액 3.5억으로는 자격에서 탈락한다.
        full = simulate_loan_options(request, [candidate])
        assert not full.executable
        assert full.rejected

        found, notes = probe_amount_limited_products(request, [candidate])

        assert found, "상품 한도 금액으로 다시 물으면 통과해야 한다"
        assert all(item.amount <= Decimal("150000000") for item in found)
        assert any("다시 판정하니 통과" in note for note in notes)

    def test_the_reason_string_is_never_parsed(self) -> None:
        """되살리는 근거는 재판정 결과이지 사유 문자열이 아니다.

        문자열을 읽어 "금액 때문"이라고 판단하면 자유텍스트 임의 판정이 된다
        (부록 B-3이 금지하는 그것). 그래서 진짜 자격 미달은 재판정에서도
        탈락하고 되살아나지 않는다.
        """
        # 개인사업자는 이 상품을 신청할 수 없다(KB_SALARY_CREDIT_EXCLUDED_APPLICANT).
        # 금액과 무관한 조건이므로 금액을 줄여도 답이 바뀌지 않는다.
        request = _request(user_facts={**_CREDIT_FACTS, "is_sole_proprietor": True})

        found, _ = probe_amount_limited_products(
            request, [_candidate("KB 급여이체신용대출")]
        )

        assert found == (), "금액과 무관한 자격 미달은 되살아나면 안 된다"

    def test_an_already_executable_product_is_not_re_probed(self) -> None:
        request = _request(user_facts=_CREDIT_FACTS)
        candidate = _candidate("KB 급여이체신용대출")

        found, _ = probe_amount_limited_products(
            request,
            [candidate],
            already_executable=["KB 급여이체신용대출"],
        )

        assert found == ()

    def test_a_product_outside_the_reviewed_limit_table_is_left_alone(self) -> None:
        """검수되지 않은 상품에 임의 한도를 부여하지 않는다."""
        request = _request(user_facts=_CREDIT_FACTS)

        found, notes = probe_amount_limited_products(
            request, [_candidate("검수표에 없는 대출")]
        )

        assert found == ()
        assert notes == ()

    def test_a_limit_above_the_required_amount_is_not_re_probed(self) -> None:
        """상품 한도가 필요금액 이상이면 금액이 병목이 아니었다."""
        request = _request(
            user_facts={**_CREDIT_FACTS, "requested_amount": Decimal("2000000")},
            required_amount=Decimal("2000000"),
        )

        found, _ = probe_amount_limited_products(
            request, [_candidate("KB 급여이체신용대출")]
        )

        assert found == ()

    def test_probing_is_off_unless_candidates_are_supplied(self) -> None:
        """후보를 주지 않으면 재판정하지 않는다 — 기존 호출자의 동작이 안 바뀐다."""
        request = _request(user_facts=_CREDIT_FACTS)

        without = combine_loan_options(request, _result(_computation()))
        with_probe = combine_loan_options(
            request,
            _result(_computation()),
            candidates=[_candidate("KB 급여이체신용대출")],
        )

        assert all(plan.leg_count == 1 for plan in without.plans)
        assert any(plan.leg_count == 2 for plan in with_probe.plans)

    def test_the_revival_is_recorded_in_the_result(self) -> None:
        """되살린 사실을 조용히 넘기지 않는다 — 근거가 결과에 남아야 한다."""
        request = _request(user_facts=_CREDIT_FACTS)

        result = combine_loan_options(
            request,
            _result(_computation()),
            candidates=[_candidate("KB 급여이체신용대출")],
        )

        assert any("다시 판정하니 통과" in reason for reason in result.reasons)


class TestTheGateIsWired:
    def test_the_reviewed_table_blocks_bogeumjari_with_a_bank_mortgage(self) -> None:
        """어댑터가 검수표를 게이트로 넘기는지 확인한다."""
        result = combine_loan_options(
            _request(),
            _result(_computation(KB_MORTGAGE), _computation(HF_BOGEUMJARI)),
        )

        for plan in result.plans:
            assert plan.leg_count == 1, "차단된 쌍이 조합으로 나왔다"
        assert result.blocked
        assert any("1순위" in reason for item in result.blocked for reason in item.reasons)

    def test_a_verified_cross_collateral_pair_combines(self) -> None:
        result = combine_loan_options(
            _request(),
            _result(
                _computation(KB_MORTGAGE, amount=Decimal("200000000")),
                _computation(
                    KB_CREDIT,
                    amount=Decimal("80000000"),
                    # 잔액 1억 이하면 가산금리 0%가 확정이다(규제표 그대로).
                    dsr_annual_rate=Decimal("0.045"),
                ),
            ),
        )

        assert any(plan.leg_count == 2 for plan in result.plans)

    def test_the_budget_mirrors_the_borrower_and_the_ltv(self) -> None:
        request = _request()
        budget = build_combination_budget(request, _result(_computation()))

        assert budget is not None
        assert budget.ltv_limit_amount == Decimal("360000000")
        assert budget.required_amount == request.required_amount
        assert budget.safe_dsr == request.borrower.safe_dsr
        assert budget.buffer_target == request.borrower.buffer_target
