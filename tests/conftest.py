"""테스트 전역 안전망.

테스트는 **살아 있는 데이터베이스에 의존하지 않는다.** 상품 DB는 SSH 터널 너머에
있어 CI에서 닿지 않고, 로컬에서도 터널이 없으면 접속이 타임아웃까지 매달린다.
실제로 상품 후보 의존성을 DB에 배선한 직후 전체 스위트가 그렇게 멈췄다.

두 겹으로 막는다.

1. 상품 후보 의존성의 기본값을 **빈 목록**으로 되돌린다. 배선 전 동작과 같으므로
   기존 테스트가 그대로 통과하고, 후보가 필요한 테스트는 지금처럼 직접
   오버라이드한다.
2. 커넥션 의존성 자체를 막는다. 1번을 우회해 DB를 부르는 경로가 생기면 **멈추는
   대신 즉시 실패**해야 원인을 찾을 수 있다.

빈 목록이 "상품 없음"으로 오해되지 않는 이유: 상위 계층이 후보 0건을 결측
(`loan_product_candidates`)으로 보고하며, 그게 "조건을 만족하는 상품이 없음"과
다르다는 것은 오케스트레이터가 사유 문구로 남긴다.
"""

from collections.abc import Iterator

import pytest

from app.api.routes.properties import get_property_loan_candidates
from app.api.routes.simulations import get_loan_candidates
from app.db.session import get_connection
from app.main import app


def _blocked_connection() -> Iterator[None]:
    raise AssertionError(
        "테스트가 실제 데이터베이스 커넥션을 요청했습니다. "
        "상품 후보가 필요하면 `get_loan_candidates`(또는 "
        "`get_property_loan_candidates`)를 오버라이드해 후보를 직접 주입하십시오."
    )
    yield  # pragma: no cover - 위에서 항상 예외를 낸다


def _no_candidates() -> list[object]:
    """후보 없음. **인자 없는 함수여야 한다** — FastAPI가 오버라이드의 시그니처를
    보고 하위 의존성을 키워드로 넘기므로, `list`를 그대로 두면 `TypeError`가 난다.
    """
    return []


_DEFAULTS = {
    get_connection: _blocked_connection,
    get_loan_candidates: _no_candidates,
    get_property_loan_candidates: _no_candidates,
}


@pytest.fixture(autouse=True)
def isolate_from_the_product_database() -> Iterator[None]:
    """DB를 쓰지 않는 기본 상태로 모든 테스트를 시작한다."""
    previous = {key: app.dependency_overrides.get(key) for key in _DEFAULTS}
    app.dependency_overrides.update(_DEFAULTS)
    yield
    for key, value in previous.items():
        if value is None:
            app.dependency_overrides.pop(key, None)
        else:
            app.dependency_overrides[key] = value
