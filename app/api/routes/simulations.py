"""``/simulations`` HTTP 경계.

계산 순서는 이 파일이 알지 않는다 —
``app/services/simulation_orchestrator.run_simulation()`` 하나만 부른다.

시간·난수 같은 부작용은 경계 모듈로 격리한다는 규약에 따라 ``as_of``와
``simulation_id``를 여기서 만들고, 테스트가 고정할 수 있도록 의존성으로 노출한다.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends

from app.api.dependencies import DbConnection, load_loan_candidates
from app.rule_engine.product_packs.handoff import ProductCandidate
from app.rule_engine.product_packs.registry import ProductRulePackRegistry
from app.schemas.simulation import SimulationInput, SimulationResult
from app.services.simulation_orchestrator import run_simulation

router = APIRouter()


def get_calculated_at() -> datetime:
    """계산 시각. 테스트가 고정할 수 있도록 의존성으로 분리한다."""
    return datetime.now(tz=UTC)


def get_loan_candidates(
    connection: DbConnection,
    calculated_at: Annotated[datetime, Depends(get_calculated_at)],
) -> Sequence[ProductCandidate]:
    """계산에 넣을 대출 상품 후보를 상품 DB에서 읽는다.

    유효기간 필터는 리포지토리가 SQL 단계에서 적용한다(DESIGN §20 "계산일에 유효한
    정책만 사용"). 그래서 계산 시각을 그대로 넘긴다 — 여기서 오늘 날짜를 다시
    만들면 테스트가 고정한 시각과 어긋난다.

    DB에 닿지 못하면 빈 목록이 아니라 503이다. 빈 목록은 상위 계층에서 결측으로
    처리되는데, 조회 실패를 그 상태로 뭉개면 장애가 정상적인 결측처럼 보인다.

    **상품 DB는 SSH 터널 너머에 있다.** 서버를 띄우기 전에 터널이 올라와 있어야
    한다(`app/db/session.py` 참조).
    """
    return load_loan_candidates(connection, as_of=calculated_at.date())


def get_loan_rule_registry() -> ProductRulePackRegistry | None:
    """자격판정에 쓸 Rule Pack 레지스트리.

    ``None``이면 기본 레지스트리(실제 상품 팩 전체)를 쓴다. 상품 후보와 마찬가지로
    협력자이므로 의존성으로 노출해 테스트가 교체할 수 있게 한다.
    """
    return None


def get_simulation_id() -> UUID:
    return uuid4()


@router.post("", response_model=SimulationResult)
def create_simulation(
    payload: SimulationInput,
    loan_candidates: Annotated[Sequence[ProductCandidate], Depends(get_loan_candidates)],
    registry: Annotated[ProductRulePackRegistry | None, Depends(get_loan_rule_registry)],
    calculated_at: Annotated[datetime, Depends(get_calculated_at)],
    simulation_id: Annotated[UUID, Depends(get_simulation_id)],
) -> SimulationResult:
    """입력 하나를 실행 가능한 구간까지 계산해 단일 JSON으로 돌려준다.

    확정하지 못한 구간은 오류가 아니라 ``NOT_RUN`` + 결측 사유로 응답한다.
    임의 숫자로 채우는 것보다 "무엇을 알려주면 계산되는지"를 돌려주는 쪽이
    맞기 때문이다(§19).
    """
    return run_simulation(
        payload,
        simulation_id=simulation_id,
        as_of=calculated_at.date(),
        calculated_at=calculated_at,
        loan_candidates=loan_candidates,
        registry=registry,
    )
