"""
lang_field.py(1차) + trans.py(2차) 결과를 종합하는 Meta-Judge LLM 판정 모듈.
Streamlit 의존 없음 — 순수 함수만 제공.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# lang_field.py 경로 등록
_LANG_FIELD_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "files"
    / "start"
    / "문학,비문학 로직"
)
if str(_LANG_FIELD_DIR) not in sys.path:
    sys.path.insert(0, str(_LANG_FIELD_DIR))

from lang_field import ISDS_LANGUAGE_CODES, LangFieldBuilder  # noqa: E402

KO_LANG_TO_ISDS: Dict[str, str] = {
    "한국어": "kor",
    "영어": "eng",
    "일본어": "jpn",
    "중국어": "chi",
    "러시아어": "rus",
    "프랑스어": "fre",
    "독일어": "ger",
    "이탈리아어": "ita",
    "스페인어": "spa",
    "포르투갈어": "por",
    "아랍어": "ara",
    "터키어": "tur",
    "북유럽어권": "ger",
    "아랍어권": "ara",
    "판별 불가": "und",
}

META_JUDGE_SCHEMA_KEYS = ("thinking_process", "final_isds_code", "reason")


def korean_lang_to_isds(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    s = label.strip()
    if s in KO_LANG_TO_ISDS:
        return KO_LANG_TO_ISDS[s]
    if s in ISDS_LANGUAGE_CODES:
        return s
    for ko, code in KO_LANG_TO_ISDS.items():
        if ko in s:
            return code
    return None


def run_lang_field_analysis(
    item: dict,
    detail: Optional[dict] = None,
    openai_client: Any = None,
    model: str = "gpt-4o",
    debug_log: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    lang_field LangFieldBuilder 1차 판정 실행 후 구조화된 dict 반환.
    """
    log: List[str] = debug_log if debug_log is not None else []

    def _dbg(*args: Any) -> None:
        log.append(" ".join(str(a) for a in args))

    builder = LangFieldBuilder(
        openai_client=openai_client,
        model=model,
        dbg_fn=_dbg,
        dbg_err_fn=lambda *a: log.append("❌ " + " ".join(str(x) for x in a)),
    )
    detail = detail or {}
    tag_041, tag_546, original_title = builder.get_kormarc_tags(item, detail)
    h_code = builder.extract_lang_h(tag_041) if tag_041 else None
    a_code = builder.lang3_from_tag041(tag_041) if tag_041 else None
    is_translation = bool(tag_041 and "$h" in tag_041 and not str(tag_041).startswith("📕"))

    return {
        "engine": "lang_field",
        "tag_041": tag_041,
        "tag_546": tag_546,
        "original_title": original_title or "",
        "h_code": h_code,
        "a_code": a_code,
        "h_language_name": ISDS_LANGUAGE_CODES.get(h_code or "", ""),
        "a_language_name": ISDS_LANGUAGE_CODES.get(a_code or "", ""),
        "is_translation": is_translation,
        "pipeline_note": "Rule → GPT-General → Author-Hint (문학/비문학 분기)",
        "debug_log": log[-40:],
    }


def build_trans_analysis_snapshot(
    *,
    book: Dict[str, Any],
    writers: Optional[List[Dict[str, Any]]] = None,
    translators: Optional[List[Dict[str, Any]]] = None,
    writer_education: Optional[List[Dict[str, Any]]] = None,
    translator_name: Optional[str] = None,
    translator_education: Optional[Dict[str, Any]] = None,
    inferred_from_major: Optional[str] = None,
    career_hint_weights: Optional[Dict[str, float]] = None,
    trans_final: Optional[Dict[str, Any]] = None,
    catalog_book_count: int = 0,
) -> Dict[str, Any]:
    """trans.py 2차 심층 판정 결과를 Meta-Judge 입력용 dict로 정리."""
    final = trans_final or {}
    conclusion_ko = (final.get("conclusion") or "").strip()
    return {
        "engine": "trans",
        "book": book,
        "writers": writers or [],
        "translators": translators or [],
        "writer_education": writer_education or [],
        "primary_translator": translator_name,
        "translator_education": translator_education,
        "inferred_from_major": inferred_from_major,
        "inferred_from_major_isds": korean_lang_to_isds(inferred_from_major),
        "career_hint_weights": dict(career_hint_weights or {}),
        "career_collapsed": final.get("raw_career_collapse") or final.get("career_runner_up"),
        "second_pass": {
            "conclusion_korean": conclusion_ko,
            "conclusion_isds_guess": korean_lang_to_isds(conclusion_ko),
            "tier": final.get("tier"),
            "reason": final.get("reason"),
        },
        "catalog_book_count": catalog_book_count,
    }


def build_meta_judge_payload(
    lang_field_result: Dict[str, Any],
    trans_result: Dict[str, Any],
) -> Dict[str, Any]:
    """두 엔진 결과를 Meta-Judge LLM 프롬프트용 단일 dict로 병합."""
    lf = lang_field_result or {}
    tr = trans_result or {}
    return {
        "task": "original_source_language_isds_resolution",
        "priority_rule": (
            "충돌 시 단순 제목/본문 힌트보다 역자 전공·번역 커리어(2차)를 우선. "
            "1차 lang_field의 $h(원서) 코드와 2차 trans 커리어 판정을 교차 검증."
        ),
        "first_pass_lang_field": {
            "gpt_and_rules_original_lang_h": lf.get("h_code"),
            "gpt_and_rules_main_lang_a": lf.get("a_code"),
            "h_language_label": lf.get("h_language_name"),
            "a_language_label": lf.get("a_language_name"),
            "original_title": lf.get("original_title"),
            "tag_041": lf.get("tag_041"),
            "tag_546": lf.get("tag_546"),
            "is_translation_detected": lf.get("is_translation"),
        },
        "second_pass_trans": {
            "translator": tr.get("primary_translator"),
            "major_inferred_language": tr.get("inferred_from_major"),
            "major_inferred_isds": tr.get("inferred_from_major_isds"),
            "translator_education_extraction": tr.get("translator_education"),
            "career_hint_weights": tr.get("career_hint_weights"),
            "career_collapsed_scores": tr.get("career_collapsed"),
            "final_conclusion_korean": (tr.get("second_pass") or {}).get("conclusion_korean"),
            "final_conclusion_isds_guess": (tr.get("second_pass") or {}).get(
                "conclusion_isds_guess"
            ),
            "final_tier": (tr.get("second_pass") or {}).get("tier"),
            "final_reason": (tr.get("second_pass") or {}).get("reason"),
            "catalog_books_analyzed": tr.get("catalog_book_count"),
        },
        "writers_reference": tr.get("writer_education") or [],
    }


def meta_judge_payload_to_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _meta_judge_system_prompt() -> str:
    return (
        "너는 두 분석 엔진의 결과를 종합하여 최종 원서 언어(ISDS 3자리 코드)를 확정하는 수석 사서다. "
        "입력 JSON의 first_pass_lang_field(1차: lang_field, GPT·규칙·카테고리)와 "
        "second_pass_trans(2차: trans, 역자 전공·커리어 가중치)를 비교 분석하라. "
        "충돌이 발생할 경우 단순 텍스트 정황(제목·본문)보다 역자의 전공이나 번역 커리어 데이터를 우선하여 "
        "논리적으로 단계별 추론(Thinking)을 진행하라. "
        "허용 ISDS 코드: kor, eng, jpn, chi, rus, ara, fre, ger, ita, spa, por, tur. "
        "불가 시 und. "
        '반드시 JSON만 출력: {"thinking_process": "...", "final_isds_code": "xxx", "reason": "..."}'
    )


def call_meta_judge_llm(
    payload: Dict[str, Any],
    api_key: str,
    model: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """
    Meta-Judge OpenAI 호출. 성공 시 thinking_process, final_isds_code, reason 반환.
    """
    if not api_key or not payload:
        return None

    body = {
        "model": model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": _meta_judge_system_prompt()},
            {
                "role": "user",
                "content": (
                    "다음은 동일 번역서에 대한 1차·2차 언어 판정 결과 JSON입니다. "
                    "종합 판정을 수행하세요.\n\n"
                    + meta_judge_payload_to_json(payload)
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["choices"][0]["message"]["content"]
        obj = json.loads(raw)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
    ):
        return None

    thinking = (obj.get("thinking_process") or "").strip()
    code = (obj.get("final_isds_code") or "").strip().lower()
    reason = (obj.get("reason") or "").strip()

    if code not in ISDS_LANGUAGE_CODES:
        m = re.search(
            r"\b(kor|eng|jpn|chi|rus|ara|fre|ger|ita|spa|por|tur|und)\b",
            code or thinking,
            re.I,
        )
        code = m.group(1).lower() if m else "und"

    return {
        "thinking_process": thinking or "(추론 과정 없음)",
        "final_isds_code": code,
        "reason": reason or "종합 판정",
    }


def validate_meta_judge_response(result: Optional[Dict[str, str]]) -> bool:
    if not result:
        return False
    return all(result.get(k) for k in META_JUDGE_SCHEMA_KEYS)


def make_openai_client(api_key: str) -> Any:
    """OpenAI 클라이언트 생성. openai 패키지 없으면 None."""
    if not api_key:
        return None
    try:
        from openai import OpenAI

        return OpenAI(api_key=api_key)
    except ImportError:
        return None
