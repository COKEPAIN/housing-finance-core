"""보고서 HTTP 경계.

계산 → 보고서 입력 → 작성 에이전트 → 검증 에이전트 → 조합까지를 한 요청으로
처리한다. 두 에이전트가 모두 통과한 절만 서술이 실린다.

``format=html``이면 화면에 바로 띄울 수 있는 페이지를 돌려준다. 프론트엔드가
붙기 전에도 결과를 눈으로 확인할 수 있어야 하기 때문이다.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from app.api.routes.simulations import (
    get_calculated_at,
    get_loan_candidates,
    get_loan_rule_registry,
    get_simulation_id,
)
from app.reports.ai_explanation.pipeline import build_final_report
from app.reports.context import build_report_ai_input
from app.reports.templates.html import collect_product_terms, render_report_html
from app.reports.templates.official import render_official_report
from app.rule_engine.product_packs.handoff import ProductCandidate
from app.rule_engine.product_packs.registry import ProductRulePackRegistry
from app.schemas.simulation import SimulationInput, SimulationResult
from app.services.simulation_orchestrator import run_simulation

router = APIRouter()


class SectionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    title: str
    # 기계 검증(서술 칸 수치 금지) 통과 여부.
    machine_ok: bool
    machine_reason: str = ""
    # 검증 에이전트 판정. NOT_JUDGED는 통과가 아니다.
    judge_verdict: str
    judge_reason: str = ""
    adopted: bool


class ReportResponse(BaseModel):
    """조합된 최종 보고서와 그 검증 내역."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_id: UUID
    markdown: str
    fully_verified: bool
    adopted_sections: tuple[str, ...]
    figures_only_sections: tuple[str, ...]
    verifications: tuple[SectionVerification, ...]
    writer_model: str | None = None
    judge_model: str | None = None
    notes: tuple[str, ...] = ()
    simulation: SimulationResult


@router.post("", response_model=None)
def create_report(
    payload: SimulationInput,
    loan_candidates: Annotated[Sequence[ProductCandidate], Depends(get_loan_candidates)],
    registry: Annotated[ProductRulePackRegistry | None, Depends(get_loan_rule_registry)],
    calculated_at: Annotated[datetime, Depends(get_calculated_at)],
    simulation_id: Annotated[UUID, Depends(get_simulation_id)],
    response_format: Annotated[
        Literal["json", "html", "print"], Query(alias="format")
    ] = "json",
) -> ReportResponse | HTMLResponse:
    """계산하고, 두 에이전트를 돌리고, 통과한 것만 조합해 보고서를 돌려준다."""
    simulation = run_simulation(
        payload,
        simulation_id=simulation_id,
        as_of=calculated_at.date(),
        calculated_at=calculated_at,
        loan_candidates=loan_candidates,
        registry=registry,
    )
    report_input = build_report_ai_input(simulation)
    report = build_final_report(report_input)

    if response_format == "print":
        # 인쇄용 정식 보고서. 우대조건 원문은 넘기지 않는다 — 자유텍스트 자격조건을
        # 문서에 확정 사실처럼 싣지 않는다는 규약(부록 B-3)과 같은 이유다.
        return HTMLResponse(render_official_report(report, simulation))

    if response_format == "html":
        # 우대조건 원문은 화면 표시 전용이다. 계산에도 AI 전송에도 쓰지 않으므로
        # 보고서 입력이 아니라 상품 후보에서 직접 뽑아 렌더러에만 넘긴다.
        return HTMLResponse(
            render_report_html(
                report,
                simulation,
                product_terms=collect_product_terms(loan_candidates),
            )
        )

    return ReportResponse(
        simulation_id=simulation_id,
        markdown=report.to_markdown(),
        fully_verified=report.fully_verified,
        adopted_sections=report.adopted_sections,
        figures_only_sections=report.figures_only_sections,
        verifications=tuple(
            SectionVerification(
                key=item.key,
                title=item.title,
                machine_ok=item.machine_ok,
                machine_reason=item.machine_reason,
                judge_verdict=item.judge_verdict.value,
                judge_reason=item.judge_reason,
                adopted=item.adopted,
            )
            for item in report.outcomes
        ),
        writer_model=report.writer_model,
        judge_model=report.judge_model,
        notes=report.notes,
        simulation=simulation,
    )
