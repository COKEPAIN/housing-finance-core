"""중도상환 조건 검수표: 원문과 어긋나지 않고, 모르는 것을 유리하게 두지 않는가."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.data_pipeline.curated.loan_limits import PRODUCT_LOAN_LIMITS
from app.data_pipeline.curated.loan_prepayment_terms import (
    PREPAYMENT_TERMS,
    PrepaymentTerms,
    get_prepayment_terms,
    known_product_names,
    resolve_prepayment_terms,
)

_KB_MORTGAGE = "KB 주택담보대출"
_KB_CREDIT = "KB 신용대출"
_EMERGENCY = "KB 비상금대출"
_SOURCE = Path("selected_23_products.json")


class TestTableIntegrity:
    def test_product_names_match_the_limit_table(self) -> None:
        """두 표가 다른 이름을 쓰면 한쪽이 조용히 결측으로 빠진다."""
        limit_names = {limit.product_name for limit in PRODUCT_LOAN_LIMITS}

        for name in known_product_names():
            assert name in limit_names, f"한도표에 없는 상품명: {name}"

    def test_every_loan_product_is_covered(self) -> None:
        """9건 전부 검수했다. 빠진 상품이 있으면 그 상품만 점수를 못 받는다."""
        assert len(PREPAYMENT_TERMS) == 9

    def test_every_entry_carries_its_source_text(self) -> None:
        for terms in PREPAYMENT_TERMS:
            assert terms.source_text.strip()

    def test_a_blank_source_text_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="source_text"):
            PrepaymentTerms(
                product_name=_KB_MORTGAGE, source_text="  ", fee_rate=Decimal("0.005")
            )


class TestAgainstTheSourceData:
    """원천 데이터가 바뀌면 이 검수표가 낡았다는 사실이 드러나야 한다."""

    @pytest.mark.skipif(
        not _SOURCE.exists(),
        reason="원천 상품 데이터가 없는 환경(CI)에서는 대조하지 않는다",
    )
    def test_the_recorded_rates_still_appear_in_the_raw_text(self) -> None:
        data = json.loads(_SOURCE.read_text(encoding="utf-8"))
        raw_by_name: dict[str, str] = {}
        for category, items in data["categories"].items():
            if "대출" not in category:
                continue
            for product in items:
                base = product.get("baseList") or {}
                if isinstance(base, list):
                    base = base[0] if base else {}
                name = base.get("fin_prdt_nm")
                if name:
                    raw_by_name[name] = str(base.get("erly_rpay_fee") or "")

        for terms in PREPAYMENT_TERMS:
            raw = raw_by_name.get(terms.product_name)
            assert raw is not None, f"원천에 없는 상품: {terms.product_name}"
            if terms.fee_rate is None or terms.fee_rate == 0:
                continue
            # 0.0055 → "0.55%" 형태로 원문에 남아 있어야 한다.
            percent = (terms.fee_rate * 100).normalize()
            assert f"{percent}%" in raw, (
                f"{terms.product_name}: 검수표 요율 {percent}%가 원문에 없습니다"
            )


class TestResolution:
    def test_an_exempt_product_is_a_confirmed_zero(self) -> None:
        """면제는 **확인된 0%**이며 미확인이 아니다."""
        resolved = resolve_prepayment_terms(_EMERGENCY, {})

        assert resolved.fee_rate == Decimal(0)
        assert resolved.is_resolved

    def test_an_unreviewed_product_is_not_resolved(self) -> None:
        """검수되지 않은 상품에 임의 요율을 부여하지 않는다."""
        resolved = resolve_prepayment_terms("검수표에 없는 대출", {})

        assert resolved.fee_rate is None
        assert not resolved.is_resolved

    def test_an_overdraft_loan_has_no_fee(self) -> None:
        resolved = resolve_prepayment_terms(_KB_CREDIT, {"is_overdraft_type": True})

        assert resolved.fee_rate == Decimal(0)

    def test_an_unknown_overdraft_flag_assumes_the_fee_applies(self) -> None:
        """모르면 수수료가 붙는 쪽으로 가정한다.

        가능한 값이 요율 아니면 0인데, 낮은 쪽을 고르면 유연성을 **과대평가**한다.
        높은 쪽은 과소평가일 뿐이라 안전하다.
        """
        resolved = resolve_prepayment_terms(_KB_CREDIT, {})

        assert resolved.fee_rate == Decimal("0.0011")
        assert resolved.missing_facts == ("is_overdraft_type",)
        assert any("가정했습니다" in note for note in resolved.notes)

    def test_conditional_waivers_are_notes_not_scores(self) -> None:
        """면제 조건은 차주의 향후 행동·자유텍스트 자격이라 판정하지 않는다."""
        terms = get_prepayment_terms("KB스타 아파트담보대출(주택자금)")

        assert terms is not None
        assert terms.fee_rate == Decimal("0.0055"), "면제 조건이 요율을 낮추면 안 된다"
        assert any("10%" in note for note in terms.notes)

    def test_the_social_consideration_waiver_is_not_scored(self) -> None:
        terms = get_prepayment_terms("한국주택금융공사 아낌e-보금자리론")

        assert terms is not None
        assert terms.fee_rate == Decimal("0.005")
        assert any("사회적배려층" in note for note in terms.notes)


class TestRelativeOrdering:
    """점수화는 상위 계층이 하지만, 비교의 근거인 요율 순서는 여기서 고정한다."""

    def test_credit_loans_are_more_flexible_than_mortgages(self) -> None:
        credit = resolve_prepayment_terms(_KB_CREDIT, {"is_overdraft_type": False})
        mortgage = resolve_prepayment_terms(_KB_MORTGAGE, {})

        assert credit.fee_rate is not None and mortgage.fee_rate is not None
        assert credit.fee_rate < mortgage.fee_rate

    def test_the_exempt_product_is_the_most_flexible(self) -> None:
        rates = [
            resolve_prepayment_terms(terms.product_name, {}).fee_rate
            for terms in PREPAYMENT_TERMS
        ]
        assert all(rate is not None for rate in rates)
        assert resolve_prepayment_terms(_EMERGENCY, {}).fee_rate == min(
            rate for rate in rates if rate is not None
        )
