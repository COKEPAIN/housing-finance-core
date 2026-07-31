"""데이터베이스 엔진과 요청 단위 커넥션.

목적:
    `Settings.database_url` 하나로 엔진을 만들고, 요청마다 커넥션을 빌려준다.
    상품 리포지토리(`db/repositories/*`)는 `Connection`만 받으므로 이 모듈이
    그 유일한 공급원이다.

**터널은 이 계층의 책임이 아니다.**
    상품 DB는 원격 서버에 있어 SSH 터널을 거쳐야 닿는다. 그 터널은 애플리케이션
    **밖에서** 유지하고, 앱은 `DATABASE_URL`이 가리키는 로컬 포트만 본다.

    앱이 직접 SSH 터널을 열지 않는 이유:
      - 앱이 SSH 비밀번호를 들고 있게 된다. 배포 때 반드시 걷어내야 하는 부채다.
      - 재연결·타임아웃·프로세스 수명 관리가 앱 책임이 된다. 그건 운영 관심사다.
      - 배포 환경에서는 보통 터널이 아니라 VPC·프록시로 해결한다. 앱이 터널을
        알면 그 이전이 어려워진다.

    로컬 개발에서는 서버를 띄우기 전에 터널을 먼저 올린다:
        ssh -N -L 5432:localhost:5432 -p <SSH_PORT> <사용자>@<SSH_HOST>

결측 규약:
    DB에 닿지 못하면 **빈 목록을 반환하지 않는다.** 빈 목록은 "조건을 만족하는
    상품이 없음"으로 읽히는데 실제로는 "확인하지 못함"이다. 예외를 그대로 올려
    호출자가 결측으로 처리하게 한다(`app/schemas/README.md`의 UNKNOWN 계약).
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import Connection

from app.core.config import Settings, get_settings


@lru_cache
def get_engine() -> Engine:
    """프로세스당 하나의 엔진.

    `pool_pre_ping`을 켜는 이유는 이 배치의 특성 때문이다. 커넥션이 SSH 터널을
    지나가므로 터널이 끊겼다 다시 붙으면 풀에 죽은 커넥션이 남는다. 사전 확인이
    없으면 그 커넥션을 집어 든 요청 하나가 알 수 없는 오류로 실패한다.
    """
    settings: Settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        # 터널이 내려간 뒤 오래 붙잡고 있지 않도록 재활용 주기를 둔다.
        pool_recycle=1800,
    )


def get_connection() -> Iterator[Connection]:
    """요청 하나가 쓰는 커넥션. FastAPI 의존성으로 사용한다.

    커넥션을 얻지 못하면 예외가 그대로 올라간다. **여기서 삼켜 빈 결과로 바꾸지
    않는다** — 그러면 "상품이 없다"와 "DB에 못 붙었다"가 같은 결과가 된다.
    """
    with get_engine().connect() as connection:
        yield connection


__all__ = ["get_connection", "get_engine"]
