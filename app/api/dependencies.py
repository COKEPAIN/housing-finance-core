"""여러 라우트가 함께 쓰는 의존성.

상품 후보 조회를 여기 한 곳에 둔다. `/simulations`·`/reports`·`/properties`가
각자 DB를 읽으면 유효기간 필터나 오류 처리가 갈라지고, 그러면 같은 상품이
경로마다 다르게 보인다.
"""

from collections.abc import Sequence
from datetime import date
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.db.repositories.loan_product_repository import fetch_loan_product_candidates
from app.db.session import get_connection
from app.rule_engine.product_packs.handoff import ProductCandidate


def load_loan_candidates(
    connection: Connection,
    *,
    as_of: date,
) -> Sequence[ProductCandidate]:
    """계산일에 유효한 대출 상품 후보를 읽는다.

    **DB 오류를 빈 목록으로 바꾸지 않는다.** 빈 목록은 상위 계층에서 "후보를 받지
    못함"이라는 결측으로 처리되는데, 그건 "DB가 죽었다"와 다른 상태다. 둘을 합치면
    장애가 정상적인 결측처럼 보여 아무도 알아채지 못한다.

    대신 503으로 올린다 — 코드 버그(500)와도 구분되고, 무엇이 문제인지 호출자가
    바로 안다.
    """
    try:
        return fetch_loan_product_candidates(connection, as_of=as_of)
    except SQLAlchemyError as error:
        # 예외 본문에는 DSN·사용자명이 실릴 수 있어 그대로 노출하지 않는다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "상품 데이터베이스에서 대출 상품을 읽지 못했습니다. "
                "이 응답은 '조건을 만족하는 상품이 없음'이 아니라 조회 실패입니다. "
                f"({type(error).__name__})"
            ),
        ) from error


DbConnection = Annotated[Connection, Depends(get_connection)]


__all__ = ["DbConnection", "load_loan_candidates"]
