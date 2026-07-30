"""인쇄용 정식 보고서: 문서가 스스로 모순되지 않는가.

이 렌더러의 가장 큰 위험은 **같은 문서에 조달액·부족액이 두 개 나오는 것**이다.
§19 양식은 대출 1건 기준으로 쓰였고 조합 절은 조합 기준이라 값이 다르다. 둘 다
맞는 값이므로 지우지 않고, 무엇을 기준으로 한 숫자인지 문서가 밝혀야 한다.
"""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.engines.loan.combination_models import (
    CombinationScoreComponents,
    CombinationStatus,
    CreditStressRegime,
    LoanCombinationPlan,
    LoanCombinationResult,
    LoanLegAllocation,
    LoanLegKind,
)
from app.engines.recommendation.models import ScoreStatus
from app.reports.ai_explanation.gemini import GenerationResult
from app.reports.ai_explanation.pipeline import FinalReport, build_final_report
from app.reports.templates.form import FORM_SECTIONS
from app.reports.templates.official import render_official_report
from app.schemas.report import ReportAIInput
from app.schemas.simulation import SectionRunStatus, SimulationResult
from app.services.simulation_result import build_calculation_section

_KEYS = tuple(key for key, _title in FORM_SECTIONS)
_CLEAN = "표시된 수치의 의미를 설명하는 문장입니다."


@dataclass
class FakeClient:
    result: GenerationResult
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> GenerationResult:
        self.calls.append({"user_prompt": user_prompt})
        return self.result


def _settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={"gemini_api_key": None, "report_ai_egress_guard": True}
    )


def _report(report_input: ReportAIInput, narration: str = _CLEAN, **overrides: str) -> FinalReport:
    writer_payload = {key: narration for key in _KEYS}
    writer_payload.update(overrides)
    judge_payload = {key: {"verdict": "OK", "reason": ""} for key in _KEYS}
    return build_final_report(
        report_input,
        writer_client=FakeClient(
            GenerationResult(text=json.dumps(writer_payload, ensure_ascii=False), model="w")
        ),
        judge_client=FakeClient(
            GenerationResult(text=json.dumps(judge_payload, ensure_ascii=False), model="j")
        ),
        settings=_settings(),
    )


def _combination_result() -> LoanCombinationResult:
    leg_mortgage = LoanLegAllocation(
        candidate_id="leg:1:KB 주택담보대출",
        product_id="KB 주택담보대출",
        product_name="KB 주택담보대출",
        option_name="아파트/주택 / 분할상환 / 변동금리",
        kind=LoanLegKind.MORTGAGE,
        amount=Decimal("237620706"),
        monthly_payment=Decimal("1148179"),
        assessment_monthly_payment=Decimal("1596887"),
        total_interest=Decimal("175724734"),
        annual_rate=Decimal("0.0410"),
        assessment_annual_rate=Decimal("0.0710"),
        months=360,
    )
    leg_credit = LoanLegAllocation(
        candidate_id="leg:2:KB 신용대출",
        product_id="KB 신용대출",
        product_name="KB 신용대출",
        option_name="무보증신용 / 분할상환 / 변동금리",
        kind=LoanLegKind.CREDIT,
        amount=Decimal("90000000"),
        monthly_payment=Decimal("436446"),
        assessment_monthly_payment=Decimal("436446"),
        total_interest=Decimal("67120560"),
        annual_rate=Decimal("0.0413"),
        assessment_annual_rate=Decimal("0.0413"),
        months=360,
    )
    plan = LoanCombinationPlan(
        plan_id="p1",
        legs=(leg_mortgage, leg_credit),
        total_amount=Decimal("327620706"),
        funding_shortfall=Decimal("22379294"),
        covers_required_amount=False,
        monthly_payment=Decimal("1584625"),
        assessment_monthly_payment=Decimal("2033333"),
        expected_dsr=Decimal("0.3231"),
        assessment_dsr=Decimal("0.4000"),
        post_purchase_monthly_surplus=Decimal("1315375"),
        stress_monthly_surplus=Decimal("866667"),
        total_interest=Decimal("242845294"),
        total_financial_cost=None,
        credit_regime=CreditStressRegime.BELOW,
        binding_constraints=("신용대출 스트레스 문턱", "DSR 예산"),
        score=Decimal("39.6447"),
        score_status=ScoreStatus.PROVISIONAL,
        score_completeness=Decimal("0.65"),
        score_components=CombinationScoreComponents(
            repayment_capacity=Decimal("0.192"),
            total_cost=None,
            crisis_resilience=Decimal("1.000"),
            interest_stability=Decimal("0.000"),
            repayment_flexibility=None,
        ),
        missing_score_components=("total_cost", "repayment_flexibility"),
    )
    return LoanCombinationResult(status=CombinationStatus.PARTIAL, plans=(plan,))


def _without_combination(simulation: SimulationResult) -> SimulationResult:
    """조합 절을 실행하지 않은 결과. 공용 픽스처는 조합까지 계산하므로 되돌린다."""
    return simulation.model_copy(
        update={
            "loan_combination": build_calculation_section(
                None,
                section_schema_version="loan-combination@1.0.0",
            )
        }
    )


def _with_combination(simulation: SimulationResult) -> SimulationResult:
    return simulation.model_copy(
        update={
            "loan_combination": build_calculation_section(
                _combination_result(),
                section_schema_version="loan-combination@1.0.0",
            )
        }
    )


class TestTheDocumentNamesItsBasis:
    """가장 중요한 불변식 — 두 기준의 숫자가 섞이지 않는다."""

    def test_both_bases_appear_side_by_side(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "산출 기준별 비교" in html
        assert "대출 1건만 실행" in html
        assert "여러 대출을 조합" in html
        # 조합 기준 금액과 부족액이 비교표에 함께 실린다.
        assert "327,620,706원" in html
        assert "22,379,294원" in html

    def test_single_loan_sections_say_so(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """§19 절들이 대출 1건 기준임을 밝힌다 — 밝히지 않으면 모순으로 읽힌다."""
        html = render_official_report(_report(report_input), _with_combination(simulation))

        # 목차에도 같은 제목이 있으므로 **본문 제목**으로 위치를 잡는다.
        board = html.index("산출 기준별 비교")
        reasons = html.index("<h2>3. 추천·탈락 사유</h2>")
        assert board < reasons, "비교표가 개별 절보다 먼저 나와야 한다"
        assert html.count("<strong>대출 1건만 실행하는 경우</strong>") >= 2

    def test_no_basis_note_when_there_is_no_combination(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """조합이 없으면 기준이 하나뿐이라 주석이 필요 없다."""
        html = render_official_report(
            _report(report_input), _without_combination(simulation)
        )

        assert "산출 기준별 비교" not in html
        assert "2항을 보십시오" not in html


class TestCombinationSection:
    def test_each_plan_shows_its_legs_and_score_breakdown(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "구성 대출" in html
        assert "KB 주택담보대출" in html
        assert "KB 신용대출" in html
        assert "점수 산출 내역 (설계안 §14)" in html
        for label in ("상환가능성", "총비용", "위기대응력", "금리안정성", "상환유연성"):
            assert label in html

    def test_the_payment_increase_explains_a_zero_stability_score(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """금리안정성 점수의 근거가 되는 증가율을 함께 보여준다(§14.4)."""
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "심사 금리 적용 시 상환액 증가율" in html
        # (2,033,333 − 1,584,625) / 1,584,625 = 28.32%
        assert "28.32%" in html

    def test_binding_constraints_are_named(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "이 금액을 제한한 조건" in html
        assert "신용대출 스트레스 문턱" in html

    def test_an_unknown_cost_is_not_shown_as_zero(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """확인되지 않음은 0원이 아니다."""
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "확인되지 않음" in html
        assert "총 금융비용</th><td>0원" not in html

    def test_a_not_run_section_states_why(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        bare = _without_combination(simulation)
        html = render_official_report(_report(report_input), bare)

        assert bare.loan_combination.run_status is SectionRunStatus.NOT_RUN
        assert "방안별 내역" not in html


class TestOfficialDocumentShape:
    def test_it_is_a_complete_document_with_a_charset(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """파일로 저장해 열고 인쇄하는 것이 정상 사용법이라 charset이 문서에 있어야 한다.

        헤더에만 기대면 저장한 파일에서 한글이 깨진다(실제로 그렇게 나왔다).
        """
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert html.startswith("<!doctype html>")
        assert "<meta charset='utf-8'>" in html
        assert html.rstrip().endswith("</html>")

    def test_it_declares_a4_print_geometry(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "@page" in html
        assert "size: A4" in html

    def test_the_table_of_contents_does_not_double_number(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """제목이 이미 번호를 갖고 있어 자동 번호를 쓰면 "3. 3." 이 된다."""
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "<ol>" not in html
        assert not re.search(r"<li>\s*\d+\.\s*\d+\.", html)

    def test_enum_values_are_shown_in_korean(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "주택 구입" in html
        assert "HOME_PURCHASE" not in html

    def test_tags_are_balanced(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))
        body = html.split("</head>", 1)[1]

        for tag in ("div", "section", "table", "thead", "tbody", "tr", "td", "th", "ul", "li"):
            opened = len(re.findall(rf"<{tag}[\s>]", body))
            closed = len(re.findall(rf"</{tag}>", body))
            assert opened == closed, f"{tag}: 열림 {opened} 닫힘 {closed}"


class TestNarrationAndDisclaimer:
    def test_adopted_narration_is_labelled_as_ai(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "AI 설명" in html
        assert _CLEAN in html

    def test_model_output_is_escaped(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(
            _report(report_input, narration="<script>alert(1)</script>"),
            _with_combination(simulation),
        )

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_blocked_section_keeps_the_figures_and_says_why(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(
            _report(report_input, base_vs_stress="최악 DSR은 45.55%입니다."),
            _with_combination(simulation),
        )

        assert "채택되지 않아" in html
        assert "계산 엔진 산출값이므로 그대로 유효합니다" in html

    def test_it_never_claims_to_be_an_official_approval(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        """공문서 양식이지만 기관이 발행한 승인 문서가 아님을 밝힌다(§20)."""
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "대출 승인을 의미하지 않습니다" in html
        assert "공적 증명이 아닙니다" in html
        assert "주택구매 금융 라이프 컨설팅 서비스" in html
        # 기관 사칭으로 읽힐 요소를 넣지 않는다.
        assert "문서번호" not in html
        assert "관인" not in html

    def test_missing_inputs_are_listed_as_an_attachment(
        self,
        report_input: ReportAIInput,
        simulation: SimulationResult,
    ) -> None:
        html = render_official_report(_report(report_input), _with_combination(simulation))

        assert "붙임 2. 확인되지 않은 항목" in html
        assert "0이나 해당 없음이 아니며" in html
