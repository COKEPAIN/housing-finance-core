"""인쇄용 정식 보고서(공문서 양식) 렌더러.

목적:
    대시보드(`html.py`)가 화면에서 "되는지 안 되는지"를 한눈에 보여준다면, 이쪽은
    **인쇄해서 남기는 문서**다. 수치를 전부 표로 두고 각 값에 근거를 붙인다.

무엇을 넣지 않는가:
    문서번호·관인·기관 로고·"승인" 표현을 넣지 않는다. 그게 들어가면 은행이나
    공사가 한도를 확정해 준 문서처럼 읽히는데, 실제로는 **이 서비스의 계산
    결과**다. 발행 주체를 서비스로 명시하고 §20이 요구하는 "승인·실제 금리
    미보장" 문구를 말미에 싣는다.

페이지 번호에 대해:
    ``@page`` 여백 상자에 쪽 번호 규칙을 넣어 뒀다. WeasyPrint 같은 인쇄 엔진은
    이를 렌더링하지만 **브라우저는 하지 않는다**(브라우저는 @page 여백 상자의
    content를 지원하지 않는다). 브라우저로 인쇄할 때는 인쇄 대화상자의
    "머리글/바닥글" 옵션을 켜면 쪽 번호가 붙는다. 지금 없는 기능을 있는 것처럼
    적지 않으려고 여기 남긴다.

근거:
    표시 항목은 §19(추천·탈락 사유, 사용 금리·정책 기준일, 우대조건 예상
    달성률, 기본 vs 스트레스 차이, 목표 미달 시 필요 연장 폭, 사용자가 직접
    확인할 조건)를 그대로 따르고, 조합안 절과 붙임을 더한다.
"""

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape

from app.reports.ai_explanation.pipeline import FinalReport
from app.schemas.simulation import SectionRunStatus, SimulationResult

# 화면 대시보드와 달리 색으로 정보를 전달하지 않는다. 흑백 인쇄에서 모든 정보가
# 남아야 하므로 상태는 **글자**로 적는다.
_STYLE = """
@page { size: A4; margin: 20mm 18mm 22mm; }
@page { @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; } }
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; max-width: 190mm; padding: 12mm 8mm 20mm;
  font-family: "Malgun Gothic", "맑은 고딕", "Noto Serif KR", serif;
  font-size: 10.5pt; line-height: 1.65; color: #111; background: #fff;
}
h1 { font-size: 17pt; text-align: center; margin: 0 0 2mm; letter-spacing: -0.02em; }
.subtitle { text-align: center; font-size: 10pt; color: #444; margin: 0 0 8mm; }
h2 {
  font-size: 12pt; margin: 9mm 0 3mm; padding-bottom: 1.5mm;
  border-bottom: 1.6pt solid #111; page-break-after: avoid;
}
h3 { font-size: 10.5pt; margin: 5mm 0 2mm; page-break-after: avoid; }
p { margin: 0 0 2.5mm; }
table { width: 100%; border-collapse: collapse; margin: 0 0 4mm; font-size: 10pt; }
caption {
  caption-side: top; text-align: left; font-weight: 700;
  padding: 0 0 1.5mm; font-size: 10pt;
}
th, td { border: 0.6pt solid #999; padding: 1.6mm 2.2mm; vertical-align: top; }
thead th { background: #eee; font-weight: 700; text-align: center; }
th.rowhead { background: #f6f6f6; text-align: left; width: 34%; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.mid { text-align: center; }
.masthead {
  border: 1.2pt solid #111; padding: 4mm 5mm; margin: 0 0 7mm;
}
.masthead dl { display: grid; grid-template-columns: 32mm 1fr; gap: 1.2mm 4mm; margin: 0; }
.masthead dt { font-weight: 600; color: #333; }
.masthead dd { margin: 0; }
.toc ul { margin: 0; padding-left: 6mm; list-style: none; }
.toc li { margin-bottom: 0.8mm; }
/* 구성 대출 표: 상품명이 세로로 깨지지 않게 열 폭을 정한다. */
table.legs th:nth-child(1), table.legs td:nth-child(1) { width: 21%; }
table.legs th:nth-child(2), table.legs td:nth-child(2) { width: 27%; }
table.legs td:nth-child(3), table.legs td:nth-child(7) { white-space: nowrap; }
.narration {
  margin: 2mm 0 4mm; padding: 2.5mm 3.5mm;
  border-left: 2.4pt solid #666; background: #fafafa; font-size: 10pt;
}
.narration .tag { display: block; font-size: 8.5pt; color: #555; margin-bottom: 1mm; }
.note { font-size: 9.5pt; color: #333; margin: 0 0 3mm; }
.missing { font-style: italic; color: #444; }
ul.plain { margin: 0 0 3mm; padding-left: 5mm; }
ul.plain li { margin-bottom: 1mm; }
.attach { page-break-before: always; }
.disclaimer { margin-top: 8mm; border-top: 1.2pt solid #111; padding-top: 3mm; font-size: 9.5pt; }
.issuer { margin-top: 6mm; text-align: right; font-size: 10pt; }
tr, table, .narration { page-break-inside: avoid; }
@media print { body { padding: 0; max-width: none; } .noprint { display: none; } }
"""

_UNKNOWN = '<span class="missing">확인되지 않음</span>'

# 양식(`form.py`)과 **같은 표를 쓴다.** 두 곳이 갈리면 같은 결과에 다른 이름이 붙는다.
_GOAL_LABELS: dict[str, str] = {
    "HOME_PURCHASE": "주택 구입",
    "JEONSE_DEPOSIT": "전세 보증금",
    "MONTHLY_RENT_DEPOSIT": "월세 보증금",
}

_KIND_LABELS = {
    "MORTGAGE": "주택담보",
    "CREDIT": "신용",
    "OTHER": "기타",
}

_REGIME_LABELS = {
    "NOT_APPLICABLE": "해당 없음(신용대출 미포함)",
    "BELOW": "신용대출 잔액이 스트레스 문턱 이하",
    "ABOVE": "신용대출 잔액이 스트레스 문턱 초과",
}

_SCORE_STATUS_LABELS = {
    "COMPLETE": "전 항목 확인",
    "PROVISIONAL": "일부 항목 미확인(확인된 항목만 재가중)",
    "UNAVAILABLE": "산출 불가",
}

_COMPONENT_LABELS = (
    ("repayment_capacity", "상환가능성", "0.30"),
    ("total_cost", "총비용", "0.25"),
    ("crisis_resilience", "위기대응력", "0.20"),
    ("interest_stability", "금리안정성", "0.15"),
    ("repayment_flexibility", "상환유연성", "0.10"),
)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _won(value: object) -> str:
    """원 단위로 내림 없이 반올림해 표시한다. 확정하지 못한 값은 숨기지 않는다."""
    number = _decimal(value)
    if number is None:
        return _UNKNOWN
    return f"{number.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}원"


def _pct(value: object, places: str = "0.01") -> str:
    number = _decimal(value)
    if number is None:
        return _UNKNOWN
    scaled = (number * 100).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return f"{scaled}%"


def _score(value: object) -> str:
    number = _decimal(value)
    if number is None:
        return _UNKNOWN
    return f"{number.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}점"


def _ratio(value: object) -> str:
    number = _decimal(value)
    if number is None:
        return _UNKNOWN
    return str(number.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _months(value: object) -> str:
    number = _decimal(value)
    if number is None:
        return _UNKNOWN
    months = int(number)
    return f"{months}개월({months // 12}년)" if months % 12 == 0 else f"{months}개월"


def _section_facts(simulation: SimulationResult, name: str) -> dict[str, object]:
    section = getattr(simulation, name, None)
    if section is None or section.run_status is not SectionRunStatus.COMPLETED:
        return {}
    return dict(section.result or {})


def _rows(*pairs: tuple[str, str]) -> str:
    return "".join(
        f'<tr><th class="rowhead">{escape(label)}</th><td>{value}</td></tr>'
        for label, value in pairs
    )


def _masthead(simulation: SimulationResult) -> str:
    goal = simulation.goal
    return (
        '<div class="masthead"><dl>'
        f"<dt>보고서 종류</dt><dd>주택자금 대출 조달방안 산출 결과</dd>"
        f"<dt>산출 기준일</dt><dd>{escape(simulation.as_of.isoformat())}</dd>"
        f"<dt>산출 일시</dt><dd>{escape(simulation.calculated_at.isoformat())}</dd>"
        f"<dt>산출 번호</dt><dd>{escape(str(simulation.simulation_id))}</dd>"
        "<dt>목표 유형</dt><dd>"
        f"{escape(_GOAL_LABELS.get(goal.goal_type.value, goal.goal_type.value))}</dd>"
        f"<dt>목표 금액</dt><dd>{_won(goal.target_amount)}</dd>"
        f"<dt>목표 시점</dt><dd>{escape(goal.target_date.isoformat())}</dd>"
        f"<dt>계약 버전</dt><dd>{escape(simulation.schema_version)}</dd>"
        "</dl></div>"
    )


def _toc(has_combination: bool, narrated_titles: Sequence[str]) -> str:
    """목차. **자동 번호를 쓰지 않는다** — 제목이 이미 번호를 갖고 있어서 겹친다.

    처음에 `<ol>`로 만들었더니 "3. 3. 추천·탈락 사유"처럼 번호가 두 번 찍혔다.
    """
    items = ["1. 산출 조건"]
    if has_combination:
        items.append("2. 대출 조달방안")
    items.extend(narrated_titles)
    items.extend(["붙임 1. 적용 규제·정책 근거", "붙임 2. 확인되지 않은 항목"])
    body = "".join(f"<li>{escape(title)}</li>" for title in items)
    return f'<section class="toc"><h2>목차</h2><ul>{body}</ul></section>'


def _conditions(simulation: SimulationResult) -> str:
    user = simulation.user_summary
    loan = _section_facts(simulation, "loan_simulation")
    combination = _section_facts(simulation, "loan_combination")
    plans = combination.get("plans") or []
    required = None
    if isinstance(plans, list) and plans:
        first = plans[0]
        if isinstance(first, Mapping):
            total = _decimal(first.get("total_amount"))
            short = _decimal(first.get("funding_shortfall"))
            if total is not None and short is not None:
                required = total + short

    return (
        "<h2>1. 산출 조건</h2>"
        "<table><caption>가. 차주 재무 사실</caption><tbody>"
        + _rows(
            ("연 소득", _won(user.annual_income)),
            ("월 소득", _won(user.monthly_income)),
            ("월 지출", _won(user.monthly_expense)),
            ("보유 유동자산", _won(user.liquid_assets)),
            ("기존 부채 총액", _won(user.total_debt)),
            ("기존 월 상환액", _won(user.monthly_debt_payment)),
            ("가구원 수", f"{user.household_size}명"),
            ("생애최초 주택구입 여부", "예" if user.is_first_home_buyer else "아니오"),
        )
        + "</tbody></table>"
        "<table><caption>나. 적용 규제 판정</caption><tbody>"
        + _rows(
            ("필요 대출금액", _won(required)),
            (
                "LTV 한도액",
                _won((loan.get("ltv") or {}).get("amount"))
                if isinstance(loan.get("ltv"), Mapping)
                else _UNKNOWN,
            ),
            ("규제 기준일", escape(str(loan.get("policy_as_of") or "")) or _UNKNOWN),
        )
        + "</tbody></table>"
        + _notes_list(loan.get("notes"), "다. 산출 시 적용한 판정 근거")
    )


def _notes_list(value: object, title: str) -> str:
    if not isinstance(value, list) or not value:
        return ""
    body = "".join(f"<li>{escape(str(item))}</li>" for item in value)
    return f"<h3>{escape(title)}</h3><ul class='plain'>{body}</ul>"


def _payment_increase(plan: Mapping[str, object]) -> str:
    """심사 금리를 적용했을 때 월 상환액이 몇 % 늘어나는가(§14.4가 보는 값).

    계산해서 보여줄 뿐 새 판단을 하지 않는다. 두 상환액 모두 조합 엔진이 이미
    산출한 값이다.
    """
    base = _decimal(plan.get("monthly_payment"))
    stressed = _decimal(plan.get("assessment_monthly_payment"))
    if base is None or stressed is None or base <= 0:
        return _UNKNOWN
    return _pct((stressed - base) / base)


def _plan_table(index: int, plan: Mapping[str, object]) -> str:
    legs = plan.get("legs")
    rows = ""
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            kind = _KIND_LABELS.get(str(leg.get("kind")), str(leg.get("kind")))
            rows += (
                "<tr>"
                f"<td>{escape(str(leg.get('product_name', '')))}</td>"
                f"<td>{escape(str(leg.get('option_name', '')))}</td>"
                f'<td class="mid">{escape(kind)}</td>'
                f'<td class="num">{_won(leg.get("amount"))}</td>'
                f'<td class="num">{_pct(leg.get("annual_rate"))}</td>'
                f'<td class="num">{_pct(leg.get("assessment_annual_rate"))}</td>'
                f'<td class="mid">{_months(leg.get("months"))}</td>'
                f'<td class="num">{_won(leg.get("monthly_payment"))}</td>'
                "</tr>"
            )
    covers = plan.get("covers_required_amount")
    verdict = "필요 대출금액 전액 조달" if covers is True else "필요 대출금액 일부 조달"
    binding = plan.get("binding_constraints")
    binding_text = (
        ", ".join(str(item) for item in binding)
        if isinstance(binding, list) and binding
        else "확인되지 않음"
    )
    regime = _REGIME_LABELS.get(str(plan.get("credit_regime")), str(plan.get("credit_regime")))

    return (
        f"<h3>나-{index}. 방안 {index} — {_score(plan.get('score'))}</h3>"
        "<table class='legs'><caption>구성 대출</caption>"
        "<thead><tr><th>상품</th><th>옵션</th><th>담보</th><th>대출금액</th>"
        "<th>실제 금리</th><th>심사 금리</th><th>기간</th><th>월 상환액</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<table><caption>방안 요약</caption><tbody>"
        + _rows(
            ("조달 합계", _won(plan.get("total_amount"))),
            ("부족액(필요 대출금액 대비)", _won(plan.get("funding_shortfall"))),
            ("판정", escape(verdict)),
            ("월 상환액 합계(실제 금리)", _won(plan.get("monthly_payment"))),
            ("월 상환액 합계(심사 금리)", _won(plan.get("assessment_monthly_payment"))),
            # 이 줄이 없으면 아래 점수표의 "금리안정성 0.000"에 근거가 없다.
            # §14.4가 보는 값이 바로 이 증가율이다.
            ("심사 금리 적용 시 상환액 증가율", _payment_increase(plan)),
            ("DSR(실제 금리 기준)", _pct(plan.get("expected_dsr"))),
            ("DSR(심사 금리 기준)", _pct(plan.get("assessment_dsr"))),
            ("구매 후 월 잉여자금", _won(plan.get("post_purchase_monthly_surplus"))),
            ("심사 금리 적용 시 월 잉여자금", _won(plan.get("stress_monthly_surplus"))),
            ("총 이자", _won(plan.get("total_interest"))),
            ("총 금융비용", _won(plan.get("total_financial_cost"))),
            ("이 금액을 제한한 조건", escape(binding_text)),
            ("신용대출 스트레스 구간", escape(regime)),
        )
        + "</tbody></table>"
        + _score_table(plan)
    )


def _score_table(plan: Mapping[str, object]) -> str:
    components = plan.get("score_components")
    if not isinstance(components, Mapping):
        return ""
    missing = plan.get("missing_score_components")
    missing_names = set(missing) if isinstance(missing, list) else set()
    rows = ""
    for key, label, weight in _COMPONENT_LABELS:
        value = components.get(key)
        cell = _ratio(value) if value is not None else _UNKNOWN
        note = "미확인 — 가중치 재정규화 대상" if key in missing_names else ""
        rows += (
            f"<tr><th class='rowhead'>{escape(label)}</th>"
            f"<td class='mid'>{weight}</td>"
            f"<td class='num'>{cell}</td>"
            f"<td>{escape(note)}</td></tr>"
        )
    status = _SCORE_STATUS_LABELS.get(
        str(plan.get("score_status")), str(plan.get("score_status"))
    )
    return (
        "<table><caption>점수 산출 내역 (설계안 §14)</caption>"
        "<thead><tr><th>평가 항목</th><th>가중치</th><th>점수(0~1)</th><th>비고</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class='note'>종합 {_score(plan.get('score'))} · 산출 상태: {escape(status)} · "
        f"확인된 가중치 합계 {_ratio(plan.get('score_completeness'))}. "
        "가중치는 공식 설계안 §14의 값이며, 결측 항목은 부록 A-10에 따라 "
        "확인된 항목만으로 재정규화했습니다.</p>"
    )


def _combination(simulation: SimulationResult) -> str:
    section = simulation.loan_combination
    if section.run_status is not SectionRunStatus.COMPLETED or not section.result:
        reasons = "".join(f"<li>{escape(str(item))}</li>" for item in section.reasons)
        missing = "".join(f"<li>{escape(str(item))}</li>" for item in section.missing_inputs)
        return (
            "<h2>2. 대출 조달방안</h2>"
            "<p>대출 조달방안을 산출하지 않았습니다.</p>"
            + (f"<h3>가. 사유</h3><ul class='plain'>{reasons}</ul>" if reasons else "")
            + (f"<h3>나. 필요한 정보</h3><ul class='plain'>{missing}</ul>" if missing else "")
        )

    facts = dict(section.result)
    plans = facts.get("plans")
    body = "<h2>2. 대출 조달방안</h2>"
    body += (
        "<p class='note'>여러 대출을 동시에 실행하는 조합입니다. DSR·구매 후 "
        "현금흐름은 차주 1인당, LTV는 담보주택 1건당 하나의 예산이므로 상품별 "
        "한도를 단순히 더한 금액은 실행할 수 없습니다. 아래 금액은 그 공유 예산 "
        "안에서 산출한 값입니다.</p>"
    )
    if isinstance(plans, list) and plans:
        body += _basis_comparison(simulation, plans)
        body += "<h3>나. 방안별 내역</h3>"
        for index, plan in enumerate(plans, start=1):
            if isinstance(plan, Mapping):
                body += _plan_table(index, plan)
    else:
        body += "<p>제시할 조달방안이 없습니다.</p>"

    body += _excluded_table(facts)
    return body


def _single_loan_best(simulation: SimulationResult) -> Decimal | None:
    """대출 1건만 실행할 때의 최대 조달액. 뒤 절들이 쓰는 기준이다."""
    loan = _section_facts(simulation, "loan_simulation")
    executable = loan.get("executable")
    if not isinstance(executable, list):
        return None
    amounts = [
        value
        for item in executable
        if isinstance(item, Mapping) and (value := _decimal(item.get("amount"))) is not None
    ]
    return max(amounts) if amounts else None


def _basis_comparison(simulation: SimulationResult, plans: Sequence[object]) -> str:
    """단일 대출 기준과 조합 기준을 나란히 둔다.

    이 표가 없으면 문서가 스스로 모순돼 보인다. 뒤의 §19 절들은 대출 **1건**
    기준으로 쓰여 있고(그 양식이 조합보다 먼저 만들어졌다) 이 절은 조합 기준이라,
    같은 문서에 부족액이 두 개 나온다. 둘 다 맞는 값이므로 지우는 대신 **무엇을
    기준으로 한 숫자인지** 문서가 밝히게 한다.
    """
    single = _single_loan_best(simulation)
    best = plans[0] if plans else None
    combined = _decimal(best.get("total_amount")) if isinstance(best, Mapping) else None
    required = None
    if isinstance(best, Mapping):
        short = _decimal(best.get("funding_shortfall"))
        if combined is not None and short is not None:
            required = combined + short

    def shortfall(value: Decimal | None) -> str:
        if value is None or required is None:
            return _UNKNOWN
        return _won(max(required - value, Decimal(0)))

    return (
        "<h3>가. 산출 기준별 비교</h3>"
        "<table><thead><tr><th>산출 기준</th><th>최대 조달액</th>"
        "<th>필요 대출금액 대비 부족액</th></tr></thead><tbody>"
        f'<tr><th class="rowhead">대출 1건만 실행</th>'
        f'<td class="num">{_won(single)}</td>'
        f'<td class="num">{shortfall(single)}</td></tr>'
        f'<tr><th class="rowhead">여러 대출을 조합</th>'
        f'<td class="num">{_won(combined)}</td>'
        f'<td class="num">{shortfall(combined)}</td></tr>'
        "</tbody></table>"
        "<p class='note'>이 보고서의 3항 이하는 <strong>대출 1건만 실행하는 "
        "경우</strong>를 기준으로 작성됐습니다. 조합을 실행하면 위 표의 아래 줄과 "
        "같이 달라집니다. 두 값은 서로 다른 실행 방식을 가정한 것이며 어느 한쪽이 "
        "틀린 것이 아닙니다.</p>"
    )


def _excluded_table(facts: Mapping[str, object]) -> str:
    """제외된 조합을 사유와 함께 남긴다 — "왜 이 조합이 없는가"에 답하는 자리."""
    parts = ""
    for key, title, kind in (
        ("blocked", "다. 동시 실행이 불가한 조합", "확인 결과 불가"),
        ("unresolved", "라. 확인하지 못한 조합", "확인 필요"),
    ):
        items = facts.get(key)
        if not isinstance(items, list) or not items:
            continue
        rows = ""
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            names = item.get("product_names")
            label = " + ".join(str(name) for name in names) if isinstance(names, list) else ""
            if label in seen:
                continue
            seen.add(label)
            reasons = item.get("reasons")
            reason_text = (
                " / ".join(str(reason) for reason in reasons)
                if isinstance(reasons, list) and reasons
                else "사유가 기록되지 않았습니다."
            )
            sources = item.get("sources")
            source_text = (
                " ".join(str(source) for source in sources)
                if isinstance(sources, list) and sources
                else ""
            )
            rows += (
                f"<tr><td>{escape(label)}</td>"
                f'<td class="mid">{escape(kind)}</td>'
                f"<td>{escape(reason_text)}"
                + (f"<br><small>{escape(source_text)}</small>" if source_text else "")
                + "</td></tr>"
            )
        if rows:
            parts += (
                f"<h3>{escape(title)}</h3>"
                "<table><thead><tr><th>조합</th><th>구분</th><th>사유</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
            )
    return parts


# 대출 **1건** 기준으로 쓰인 절. 조합 절이 함께 실릴 때 기준을 밝히지 않으면
# 같은 문서에 조달액·부족액이 두 개 나와 모순으로 읽힌다.
_SINGLE_LOAN_BASIS_SECTIONS = frozenset({"decision_reasons", "shortfall_and_extension"})

_SINGLE_LOAN_BASIS_NOTE = (
    "아래 수치는 <strong>대출 1건만 실행하는 경우</strong>를 기준으로 합니다. "
    "여러 대출을 조합한 결과는 2항을 보십시오."
)


def _narrated_sections(
    report: FinalReport,
    start: int,
    *,
    has_combination: bool,
) -> tuple[str, tuple[str, ...]]:
    """SSOT §19의 여섯 항목. 수치는 엔진이, 문장은 검증을 통과한 AI 서술만."""
    body = ""
    titles: list[str] = []
    outcomes = {item.key: item for item in report.outcomes}
    for offset, section in enumerate(report.form.sections):
        number = start + offset
        # 양식 제목이 "1. 추천·탈락 사유"처럼 자체 번호를 갖고 있어 문서 번호로 바꾼다.
        raw_title = section.title.split(". ", 1)[-1]
        title = f"{number}. {raw_title}"
        titles.append(title)
        body += f"<h2>{escape(title)}</h2>"
        if has_combination and section.key in _SINGLE_LOAN_BASIS_SECTIONS:
            body += f"<p class='note'>{_SINGLE_LOAN_BASIS_NOTE}</p>"
        figures = section.figures or ("아직 계산하지 않았습니다.",)
        body += "<ul class='plain'>"
        for line in figures:
            body += f"<li>{escape(str(line).lstrip('- '))}</li>"
        body += "</ul>"

        outcome = outcomes.get(section.key)
        if section.narration:
            body += (
                "<div class='narration'><span class='tag'>[AI 설명 — 기계 검증과 "
                "검증 에이전트를 모두 통과한 문장입니다. 수치는 포함하지 않습니다.]</span>"
                f"{escape(section.narration.strip())}</div>"
            )
        elif outcome is not None and outcome.blocked_by is not None:
            reason = outcome.machine_reason or outcome.judge_reason or ""
            blocked = "기계 검증" if outcome.blocked_by == "machine" else "검증 에이전트"
            body += (
                f"<p class='note'>AI 설명은 {escape(blocked)}에서 채택되지 않아 "
                "싣지 않았습니다"
                + (f" ({escape(reason)})" if reason else "")
                + ". 위 수치는 계산 엔진 산출값이므로 그대로 유효합니다.</p>"
            )
    return body, tuple(titles)


def _attachment_sources(simulation: SimulationResult, report: FinalReport) -> str:
    sources = list(simulation.policy_sources) + [
        source for source in report.form.policy_sources if source
    ]
    unique = list(dict.fromkeys(sources))
    rows = "".join(f"<li>{escape(str(item))}</li>" for item in unique)
    return (
        "<section class='attach'><h2>붙임 1. 적용 규제·정책 근거</h2>"
        + (
            f"<ul class='plain'>{rows}</ul>"
            if rows
            else "<p>기록된 정책 출처가 없습니다.</p>"
        )
        + "<p class='note'>각 규제 상수는 시행일과 출처를 함께 관리하며, 산출 "
        "기준일에 유효한 값만 사용했습니다. 기준일이 지난 값은 사용하지 않고 "
        "확인되지 않음으로 처리합니다.</p></section>"
    )


def _attachment_missing(simulation: SimulationResult) -> str:
    names = list(simulation.missing_inputs)
    rows = "".join(f"<li>{escape(str(item))}</li>" for item in names)
    warnings = "".join(f"<li>{escape(str(item))}</li>" for item in simulation.warnings)
    return (
        "<section class='attach'><h2>붙임 2. 확인되지 않은 항목</h2>"
        "<p class='note'>아래 항목은 값이 없어 계산에 사용하지 않았습니다. "
        "<strong>확인되지 않음은 0이나 해당 없음이 아니며</strong>, 확인하면 "
        "결과가 달라질 수 있습니다.</p>"
        + (f"<ul class='plain'>{rows}</ul>" if rows else "<p>없습니다.</p>")
        + (f"<h3>가. 경고</h3><ul class='plain'>{warnings}</ul>" if warnings else "")
        + "</section>"
    )


def _closing(report: FinalReport) -> str:
    items = "".join(f"<li>{escape(item)}</li>" for item in report.form.disclaimers)
    models = []
    if report.writer_model:
        models.append(f"설명 작성 {escape(report.writer_model)}")
    if report.judge_model:
        models.append(f"설명 검증 {escape(report.judge_model)}")
    model_line = (
        f"<p class='note'>문장 설명 생성에 사용한 모델: {' / '.join(models)}.</p>"
        if models
        else ""
    )
    return (
        f"<div class='disclaimer'><h3>유의사항</h3><ul class='plain'>{items}</ul>"
        + model_line
        + "<p class='note'>이 문서는 금융기관의 대출 승인 통지가 아니며, 기관이 "
        "발행한 공적 증명이 아닙니다. 실제 대출 가능 여부와 적용 금리는 취급 "
        "금융기관의 심사로 결정됩니다.</p></div>"
        "<p class='issuer'>주택구매 금융 라이프 컨설팅 서비스 (계산 엔진 산출)</p>"
    )


def render_official_report(
    report: FinalReport,
    simulation: SimulationResult,
) -> str:
    """인쇄용 정식 보고서 HTML을 만든다.

    ``SimulationResult``를 읽는 이유는 조합안·규제 판정·결측 목록이 그쪽에만 있기
    때문이다. ``report``에서는 검증을 통과한 서술과 §19 양식만 가져온다.
    """
    has_combination = (
        simulation.loan_combination.run_status is SectionRunStatus.COMPLETED
    )
    narrated, titles = _narrated_sections(
        report,
        start=3 if has_combination else 2,
        has_combination=has_combination,
    )
    return (
        # 완전한 문서로 낸다. 이 보고서의 정상 사용법이 **파일로 저장해 열고
        # 인쇄하는 것**이라, 문서 자체에 charset이 없으면 한글이 깨진다. API
        # 응답 헤더에만 기대면 저장한 파일에서 깨진다(실제로 그렇게 나왔다).
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>주택자금 대출 조달방안 산출 결과 보고서</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<h1>주택자금 대출 조달방안 산출 결과 보고서</h1>"
        f"<p class='subtitle'>산출 기준일 {escape(simulation.as_of.isoformat())} · "
        "본 문서는 계산 결과이며 대출 승인을 의미하지 않습니다</p>"
        + _masthead(simulation)
        + _toc(has_combination, titles)
        + _conditions(simulation)
        + (_combination(simulation) if has_combination else "")
        + narrated
        + _attachment_sources(simulation, report)
        + _attachment_missing(simulation)
        + _closing(report)
        + "</body></html>"
    )


__all__ = ["render_official_report"]
