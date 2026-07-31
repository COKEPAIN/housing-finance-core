"""``/simulations``가 계산 결과 JSON을 실제로 만드는지 검증한다.

여기 4건은 `LOAN_LIMIT_REVIEW.md` §4가 "반드시 추가할 테스트"로 지정한 항목이다.
공통 주제는 하나다 — **확정하지 못한 것을 임의 숫자로 채우지 않고 사유로 답한다.**
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.routes.simulations import (
    get_calculated_at,
    get_loan_candidates,
    get_loan_rule_registry,
    get_simulation_id,
)
from app.main import app
from app.rule_engine.product_packs.handoff import ProductCandidate
from app.rule_engine.product_packs.models import ProductCategory, ProductRulePack
from app.rule_engine.product_packs.registry import ProductRulePackRegistry
from app.rule_engine.product_packs.rules import ComparisonOperator, ComparisonRule

# 지정 목록 유효기한(2026-07-28) 안쪽으로 고정한다. 그 뒤 기준일로 조회하면
# 비규제지역을 확정하지 못하는 것이 정상 동작이며, 그건 별도 테스트로 다룬다.
_AS_OF = date(2026, 7, 28)
_CALCULATED_AT = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
_SIMULATION_ID = "0f9b21e4-4c1a-4d7f-9c3e-2b6a5d8e1f30"

_SEOUL_GANGNAM = "11680"
_DAEJEON_DONG = "30110"

_MORTGAGE = "KB 주택담보대출"
_MORTGAGE_BASE = {
    "source_type": "manual_pdf",
    "fin_prdt_nm": _MORTGAGE,
    "loan_lmt": "담보조사가격 및 소득금액에 따른 대출가능금액 이내",
}
_MORTGAGE_OPTIONS = (
    {
        "fin_prdt_nm": _MORTGAGE,
        "mrtg_type_nm": "아파트",
        "rpay_type_nm": "분할상환방식",
        "lend_rate_type_nm": "변동금리",
        "lend_rate_min": 3.0,
        "lend_rate_max": 3.0,
        "lend_rate_avg": 3.0,
    },
)

# 최소 실행금액(50만원)이 상품 한도표에 있는 상품이다. 이름을 바꾸면 한도표를
# 찾지 못해 최소금액이 사라지므로 실제 상품명을 그대로 쓴다.
_EMERGENCY = "KB 비상금대출"
_EMERGENCY_BASE = {
    "source_type": "manual_pdf",
    "fin_prdt_nm": _EMERGENCY,
    "loan_lmt": "최저 50만원 ~ 최고 300만원",
}
_EMERGENCY_OPTIONS = (
    {
        "fin_prdt_nm": _EMERGENCY,
        "rpay_type_nm": "만기일시상환방식",
        "lend_rate_type_nm": "변동금리",
        "lend_rate_min": 5.0,
        "lend_rate_max": 7.0,
        "lend_rate_avg": 6.0,
    },
)


def _pack(product_name: str, category: ProductCategory, *rules: ComparisonRule) -> ProductRulePack:
    return ProductRulePack(
        product_name=product_name,
        category=category,
        version="test-1",
        effective_start_date=date(2026, 1, 1),
        effective_end_date=None,
        rules=rules,
    )


_MIN_AGE = ComparisonRule(
    code="TEST_MIN_AGE",
    field_name="age",
    operator=ComparisonOperator.GTE,
    expected=19,
    failure_reason="미성년자는 신청할 수 없습니다.",
)
_UNREACHABLE_INCOME = ComparisonRule(
    code="TEST_INCOME_FLOOR",
    field_name="annual_income",
    operator=ComparisonOperator.GTE,
    expected=Decimal("100000000000"),
    failure_reason="연소득 요건을 충족하지 않습니다.",
)


def _candidate(product_name: str) -> ProductCandidate:
    if product_name == _EMERGENCY:
        return ProductCandidate(
            product_name=_EMERGENCY,
            base_data=_EMERGENCY_BASE,
            option_list=_EMERGENCY_OPTIONS,
        )
    return ProductCandidate(
        product_name=_MORTGAGE,
        base_data=_MORTGAGE_BASE,
        option_list=_MORTGAGE_OPTIONS,
    )


def _payload(**loan_overrides: object) -> dict[str, object]:
    loan_request: dict[str, object] = {
        "months": 360,
        "housing_status": "FIRST_HOME_BUYER",
        "monthly_essential_expense": "1800000",
    }
    loan_request.update(loan_overrides)
    return {
        "profile": {
            "age": 34,
            "annual_income": "60000000",
            "is_first_home_buyer": True,
        },
        "housing_goal": {
            "target_amount": "500000000",
            "target_date": "2028-07-30",
            "region_code": _SEOUL_GANGNAM,
        },
        "financial_snapshot": {
            "monthly_income": "5000000",
            "monthly_expense": "2000000",
            "liquid_assets": "150000000",
            "monthly_debt_payment": "300000",
        },
        "loan_request": loan_request,
    }


@pytest.fixture
def client() -> Iterator[TestClient]:
    """계산 시각과 식별자를 고정한 클라이언트. 상품 후보는 기본 없음이다.

    후보를 **명시적으로** 비운다. 예전에는 의존성 기본값이 빈 목록이라 이 줄이
    없어도 됐지만, 이제 기본값이 상품 DB 조회다. 명시하지 않으면 테스트가 SSH
    터널 너머 DB에 붙으려 한다.
    """
    app.dependency_overrides[get_calculated_at] = lambda: _CALCULATED_AT
    app.dependency_overrides[get_simulation_id] = lambda: UUID(_SIMULATION_ID)
    app.dependency_overrides[get_loan_candidates] = lambda: []
    yield TestClient(app)
    app.dependency_overrides.clear()


def _serve(product_name: str, *rules: ComparisonRule, category: ProductCategory) -> None:
    """상품 후보와 판정 규칙을 의존성으로 주입한다.

    실제 레지스트리 대신 테스트 팩을 쓰는 이유는, 여기서 확인할 것이 개별 상품의
    자격조건이 아니라 **판정 결과가 응답에 사유로 남는지**이기 때문이다.
    """
    app.dependency_overrides[get_loan_candidates] = lambda: [_candidate(product_name)]
    app.dependency_overrides[get_loan_rule_registry] = lambda: ProductRulePackRegistry(
        (_pack(product_name, category, *rules),)
    )


def test_response_carries_the_regulation_date_and_sources(client: TestClient) -> None:
    """§4-1. 규제 기준일과 출처가 응답에 남는다.

    숫자만 내보내면 근거 없는 확언이 된다(SSOT §20). 기준일·출처는 결과의
    일부이며 사용자에게 도달해야 한다.
    """
    _serve(_MORTGAGE, _MIN_AGE, category=ProductCategory.MORTGAGE_LOAN)
    response = client.post("/api/v1/simulations", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["as_of"] == _AS_OF.isoformat()
    assert body["loan_simulation"]["run_status"] == "COMPLETED"
    assert body["loan_simulation"]["result"]["policy_as_of"] == _AS_OF.isoformat()
    # 6·27 방안과 DTI 세칙이 출처로 남아야 한다.
    assert body["policy_sources"]
    assert any("2025-06-27" in source for source in body["policy_sources"])
    # 안전 DSR이 법정 상한이 아니라는 표시가 남는다.
    assert any("내부 추천 기준" in note for note in body["loan_simulation"]["assumptions"])


def test_unresolvable_ltv_answers_with_a_missing_reason_not_a_number(
    client: TestClient,
) -> None:
    """§4-2. LTV를 확정하지 못하면 임의 숫자가 아니라 결측 사유로 답한다.

    비규제지역 다주택자 LTV는 1차 출처를 확인하지 못해 ``verified=False``로
    막혀 있다. 그 조합은 계산을 시작하지 않아야 한다.
    """
    app.dependency_overrides[get_loan_candidates] = lambda: [_candidate(_MORTGAGE)]
    payload = _payload(housing_status="MULTI_HOUSE")
    payload["housing_goal"]["region_code"] = _DAEJEON_DONG  # type: ignore[index]

    body = client.post("/api/v1/simulations", json=payload).json()

    section = body["loan_simulation"]
    assert section["run_status"] == "COMPLETED"
    assert section["result"]["executable"] == []
    # 없는 것은 환산된 금액이 아니라 **비율**이다. 결측 이름이 그 사실을 가리켜야
    # 무엇을 확인해야 하는지 알 수 있다.
    assert "ltv_ratio" in section["missing_inputs"]
    assert "ltv_ratio" in body["missing_inputs"]
    # 계산을 포기했으므로 어떤 금액도 만들지 않았다.
    assert section["result"]["not_executable"] == []


def test_rejected_product_keeps_its_rejection_reason(client: TestClient) -> None:
    """§4-3. 자격 탈락 상품은 탈락 사유를 응답에 남긴다."""
    _serve(_MORTGAGE, _UNREACHABLE_INCOME, category=ProductCategory.MORTGAGE_LOAN)
    body = client.post("/api/v1/simulations", json=_payload()).json()

    result = body["loan_simulation"]["result"]
    assert result["executable"] == []
    assert len(result["rejected"]) == 1
    rejected = result["rejected"][0]
    assert rejected["status"] == "FAIL"
    assert any("연소득 요건" in reason for reason in rejected["reasons"])


def test_below_minimum_amount_is_separated_from_rejection(client: TestClient) -> None:
    """§4-4. 최소 실행금액 미달과 자격 탈락은 구분되어 응답된다.

    둘을 뭉개면 사용자에게 하는 말이 달라진다 — 하나는 "금액을 올리세요",
    다른 하나는 "이 상품은 대상이 아닙니다"다.
    """
    _serve(_EMERGENCY, _MIN_AGE, category=ProductCategory.CREDIT_LOAN)
    # 신용대출 스트레스 금리는 잔액 1억원 초과에만 붙는다. 잔액을 모르면 계산
    # 자체를 포기하므로(§22.6) 최소금액 경로를 보려면 잔액을 명시해야 한다.
    body = client.post(
        "/api/v1/simulations",
        json=_payload(required_amount="300000", credit_loan_balance="0"),
    ).json()

    result = body["loan_simulation"]["result"]
    assert result["rejected"] == []
    assert len(result["not_executable"]) == 1
    below = result["not_executable"][0]
    assert below["status"] == "FAIL"
    assert Decimal(below["amount"]) < Decimal("500000")
    assert any("최소 실행금액" in reason for reason in below["reasons"])
