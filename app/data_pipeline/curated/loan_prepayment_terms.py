"""중도상환수수료 조건 검수표 (§14.5 상환유연성의 입력).

왜 검수표인가:
    원문(`erly_rpay_fee`)은 자유텍스트지만 **한도 원문과 달리 규칙적이다.** 9건을
    전부 읽어보면 전부 "수수료율 + 부과기간 + 면제조건" 꼴이라 사람이 표로 옮길 수
    있다. 정규식으로 짜맞추는 대신 읽고 옮긴다(`loan_limits.py`와 같은 방식).

무엇을 점수화하고 무엇을 하지 않는가:
    **수수료율만 비교 대상으로 삼는다.** "매년 10% 이내 원금 상환 시 면제",
    "사회적배려층 적용 대상자 면제" 같은 조건은 차주의 향후 행동이나 자유텍스트
    자격에 달려 있어 우리가 판정할 수 없다(부록 B-3). 그런 조건은 점수에 넣지 않고
    `notes`로 실어 사용자가 직접 확인하게 한다.

점수는 여기서 만들지 않는다:
    이 표는 **비교 가능한 수치(수수료율)** 까지만 낸다. 0~1 점수 변환은 부록 A-1의
    `norm_inv(x; a=후보 최소, b=후보 최대)`로 후보 집합 안에서 하며, 그 계산은
    후보 전체를 아는 계층의 몫이다(`services/loan_combination.py`).

    절대 임계값을 여기 두지 않는 이유는 그 값이 SSOT에 없기 때문이다. 부록 A-1의
    「변수별 기준 범위」표에 상환유연성 행이 없다. "0.55%가 몇 점인가"를 이 파일이
    정하면 근거 없는 내부 상수가 된다. 총금융비용점수가 쓰는 상대 정규화를 그대로
    쓰면 새 상수가 필요 없다.

1차 출처
    각 상품의 `erly_rpay_fee` 원문(`selected_23_products.json` /
    DB `loan_product_details.erly_rpay_fee_raw`, 2026-07-31 확인). `source_text`에
    원문을 그대로 실어 두었으므로 원천이 바뀌면 대조 테스트가 깨진다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

# 검수 기준일. 원천 데이터나 약관이 바뀌면 함께 올린다.
PREPAYMENT_TABLE_VERIFIED_AT = "2026-07-31"


@dataclass(frozen=True)
class ResolvedPrepaymentTerms:
    """차주 facts를 반영해 확정한 중도상환 조건."""

    # 비교에 쓸 수수료율. ``None``이면 확정하지 못한 것이며 0%가 아니다.
    fee_rate: Decimal | None
    # 부과 기간(년). 지나면 수수료가 없다.
    charged_years: int | None = None
    # 점수에 넣지 않고 사용자에게 보여줄 조건.
    notes: tuple[str, ...] = field(default_factory=tuple)
    missing_facts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_resolved(self) -> bool:
        return self.fee_rate is not None


@dataclass(frozen=True)
class PrepaymentTerms:
    """상품 1건의 중도상환 조건.

    ``overdraft_exempt``는 "종합통장자동대출 제외" 문구를 옮긴 것이다. 한도표가
    쓰는 것과 **같은 facts 키**(``is_overdraft_type``)를 사용한다 — 두 표가 다른
    이름을 쓰면 같은 차주에게 앞뒤가 안 맞는 결과가 나온다.
    """

    product_name: str
    source_text: str
    fee_rate: Decimal | None
    charged_years: int | None = None
    # 3년 이상 또는 대출기간과 동일 주기로 상환할 때 적용되는 별도 요율. 비교에는
    # 쓰지 않는다 — 그 조건에 해당하는지는 차주의 향후 상환 방식에 달려 있다.
    extended_fee_rate: Decimal | None = None
    overdraft_exempt: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.product_name.strip():
            raise ValueError("product_name은(는) 비어 있을 수 없습니다.")
        if not self.source_text.strip():
            raise ValueError("source_text는 원문 대조를 위해 반드시 필요합니다.")
        for name, value in (
            ("fee_rate", self.fee_rate),
            ("extended_fee_rate", self.extended_fee_rate),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name}은(는) 음수일 수 없습니다.")
        if self.charged_years is not None and self.charged_years <= 0:
            raise ValueError("charged_years은(는) 0보다 커야 합니다.")

    def resolve(self, facts: Mapping[str, object]) -> ResolvedPrepaymentTerms:
        """차주 facts로 이 상품의 중도상환 조건을 확정한다."""
        if self.overdraft_exempt:
            flag = facts.get("is_overdraft_type")
            if flag is None:
                # 종합통장자동대출이면 수수료가 없다. 모르면 **수수료가 부과되는
                # 쪽**을 가정한다. 가능한 값이 `fee_rate` 아니면 0인데, 낮은 쪽을
                # 고르면 유연성을 과대평가한다. 높은 쪽은 과소평가일 뿐이라
                # 안전하다(한도표가 미확정 규칙을 다루는 방식과 같다).
                return ResolvedPrepaymentTerms(
                    fee_rate=self.fee_rate,
                    charged_years=self.charged_years,
                    notes=(
                        *self.notes,
                        "종합통장자동대출 여부를 확인하지 못해 수수료가 부과되는 "
                        "쪽으로 가정했습니다. 해당한다면 수수료가 없습니다.",
                    ),
                    missing_facts=("is_overdraft_type",),
                )
            if flag is True:
                return ResolvedPrepaymentTerms(
                    fee_rate=Decimal(0),
                    charged_years=None,
                    notes=(*self.notes, "종합통장자동대출은 중도상환수수료가 없습니다."),
                )
        return ResolvedPrepaymentTerms(
            fee_rate=self.fee_rate,
            charged_years=self.charged_years,
            notes=self.notes,
        )


_OVERDRAFT_NOTE = "종합통장자동대출은 중도상환수수료 부과 대상에서 제외됩니다."
_EXTENDED_NOTE = (
    "3년 이상 또는 대출기간과 동일한 주기로 상환하면 더 높은 요율이 적용됩니다. "
    "실제 적용 여부는 상환 방식에 따라 달라지므로 점수에 반영하지 않았습니다."
)

PREPAYMENT_TERMS: tuple[PrepaymentTerms, ...] = (
    PrepaymentTerms(
        product_name="KB 주택담보대출",
        source_text=(
            "중도상환원금 × 수수료율(0.55%) × (잔존일수 ÷ 대출기간) "
            "(최장 3년 부과, 3년이상/동일주기 시 0.75% 적용)"
        ),
        fee_rate=Decimal("0.0055"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0075"),
        notes=(_EXTENDED_NOTE,),
    ),
    PrepaymentTerms(
        product_name="한국주택금융공사 아낌e-보금자리론",
        source_text=(
            "최초 실행일로부터 3년 이내 원금 중도상환 시 잔여일수에 따라 "
            "0.5% 한도 내 부과 (사회적배려층 적용 대상자 면제)"
        ),
        # "0.5% 한도 내"이므로 실제 부담은 이 값 이하다. 상한을 쓰면 유연성을
        # 과소평가할 뿐 과대평가하지 않는다.
        fee_rate=Decimal("0.005"),
        charged_years=3,
        notes=(
            "0.5%는 한도이며 잔여일수에 따라 더 낮을 수 있습니다.",
            "사회적배려층에 해당하면 면제됩니다. 해당 여부는 직접 확인해야 합니다.",
        ),
    ),
    PrepaymentTerms(
        product_name="KB스타 아파트담보대출(주택자금)",
        source_text=(
            "중도상환원금 × 수수료율(0.55%) × (잔존일수 ÷ 대출기간) "
            "(최장 3년 부과, 3년이상/동일주기 시 0.75% 적용, "
            "매년 10% 이내 원금 상환 시 면제)"
        ),
        fee_rate=Decimal("0.0055"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0075"),
        notes=(
            _EXTENDED_NOTE,
            "매년 원금의 10% 이내로 상환하면 면제됩니다. 향후 상환 계획에 달려 "
            "있어 점수에 반영하지 않았습니다.",
        ),
    ),
    PrepaymentTerms(
        product_name="KB스타 전세자금대출(HUG_주택도시보증공사)",
        source_text=(
            "중도상환원금 × 수수료율(0.54%) × (잔존일수 ÷ 대출기간) "
            "(3년이상 또는 대출기간과 동일 시 0.96% 적용)"
        ),
        fee_rate=Decimal("0.0054"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0096"),
        notes=(_EXTENDED_NOTE,),
    ),
    PrepaymentTerms(
        product_name="KB스타 전세자금대출(HF_한국주택금융공사)",
        source_text=(
            "중도상환원금 × 수수료율(0.54%) × (잔존일수 ÷ 대출기간) "
            "(3년이상 또는 대출기간과 동일 시 0.96% 적용)"
        ),
        fee_rate=Decimal("0.0054"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0096"),
        notes=(_EXTENDED_NOTE,),
    ),
    PrepaymentTerms(
        product_name="KB스타 전세자금대출(SGI_서울보증보험)",
        source_text=(
            "중도상환원금 × 수수료율(0.54%) × (잔존일수 ÷ 대출기간) "
            "(3년이상 또는 대출기간과 동일 시 0.96% 적용)"
        ),
        fee_rate=Decimal("0.0054"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0096"),
        notes=(_EXTENDED_NOTE,),
    ),
    PrepaymentTerms(
        product_name="KB 신용대출",
        source_text=(
            "중도상환원금 × 수수료율(0.11%) × (잔존일수 ÷ 대출기간) "
            "(3년이상 또는 대출기간과 동일 시 0.18% 적용, 종합통장자동대출 제외)"
        ),
        fee_rate=Decimal("0.0011"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0018"),
        overdraft_exempt=True,
        notes=(_EXTENDED_NOTE, _OVERDRAFT_NOTE),
    ),
    PrepaymentTerms(
        product_name="KB 급여이체신용대출",
        source_text=(
            "중도상환원금 × 수수료율(0.11%) × (잔존일수 ÷ 대출기간) "
            "(3년이상 또는 대출기간과 동일 시 0.18% 적용, 종합통장자동대출 제외)"
        ),
        fee_rate=Decimal("0.0011"),
        charged_years=3,
        extended_fee_rate=Decimal("0.0018"),
        overdraft_exempt=True,
        notes=(_EXTENDED_NOTE, _OVERDRAFT_NOTE),
    ),
    PrepaymentTerms(
        product_name="KB 비상금대출",
        source_text="면제",
        # 면제는 **확인된 0%**이며 미확인이 아니다. 둘을 구분하는 것이 이 표의
        # 다른 상품과 같은 규율이다.
        fee_rate=Decimal(0),
        charged_years=None,
        notes=("중도상환수수료가 면제되는 상품입니다.",),
    ),
)

_BY_PRODUCT_NAME = {terms.product_name: terms for terms in PREPAYMENT_TERMS}
if len(_BY_PRODUCT_NAME) != len(PREPAYMENT_TERMS):
    raise RuntimeError("중도상환 검수표에 같은 상품이 두 번 선언되었습니다.")


def get_prepayment_terms(product_name: str) -> PrepaymentTerms | None:
    """상품명으로 검수표를 찾는다. 검수되지 않은 상품이면 ``None``."""
    return _BY_PRODUCT_NAME.get(product_name.strip())


def resolve_prepayment_terms(
    product_name: str,
    facts: Mapping[str, object],
) -> ResolvedPrepaymentTerms:
    """상품명 + 차주 facts로 중도상환 조건을 확정한다.

    검수되지 않은 상품은 확정하지 않는다 — 임의 요율을 부여하는 것이 이 표가
    막으려는 사고다.
    """
    terms = get_prepayment_terms(product_name)
    if terms is None:
        return ResolvedPrepaymentTerms(
            fee_rate=None,
            notes=(f"{product_name}은(는) 검수된 중도상환 조건표에 없습니다.",),
        )
    return terms.resolve(facts)


def known_product_names() -> tuple[str, ...]:
    return tuple(sorted(_BY_PRODUCT_NAME))


__all__ = [
    "PREPAYMENT_TABLE_VERIFIED_AT",
    "PREPAYMENT_TERMS",
    "PrepaymentTerms",
    "ResolvedPrepaymentTerms",
    "get_prepayment_terms",
    "known_product_names",
    "resolve_prepayment_terms",
]
