"""
알라딘 TTB: ISBN → 역자 식별(ItemLookUp) → 역자명 ItemSearch(최대 50권, OptResult=authors)
→ is_translator_role + (선택) 대분류 카테고리 일치 → 원제·커리어 힌트 → 최종 원서 언어 판정.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# 설정 · 상수
# ---------------------------------------------------------------------------

ITEM_LOOKUP = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ITEM_SEARCH = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
ALADIN_WSEARCH = "https://www.aladin.co.kr/search/wsearchresult.aspx"
WSEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
API_VERSION = "20131101"
OPT_LOOKUP = "authors,categoryIdList,fulldescription,Story,toc"

PUBLISHER_WEIGHT = 8.0

# ItemLookUp 역자 추출용 (역할 표기 다양성)
TRANSLATOR_ROLE_STRICT = ("옮긴이", "역자", "옮김", "번역")


def _role_is_translator(role: str) -> bool:
    r = (role or "").strip()
    if not r:
        return False
    if any(m in r for m in TRANSLATOR_ROLE_STRICT):
        return True
    if "역" in r and not any(x in r for x in ("지은이", "지음", "감수", "교정", "편집")):
        return True
    return False


# 역자 커리어 필터: 사용자 지정 역할 키워드만 사용
_TRANSLATOR_MARKERS_FOR_CATALOG = ("옮긴이", "역자", "역", "옮김")


def _author_name_equals_target(target: str, author_name: str) -> bool:
    return target.strip() == (author_name or "").strip()


def _fallback_translator_role_from_raw_author(raw_author: str, target_name: str) -> bool:
    """
    ItemSearch 등에서 subInfo.authors가 비었을 때, book['author'] 한 줄에서
    타겟 이름과 역자 표기가 같은 쉼표 구간에 묶였는지 Regex로 판별.
    """
    t = target_name.strip()
    blob = (raw_author or "").strip()
    if not t or not blob:
        return False
    esc = re.escape(t)

    for chunk in re.split(r"\s*,\s*", blob):
        ch = chunk.strip()
        if t not in ch:
            continue

        # 이 구간이 "이름 (지은이|그림|…)"만 담당하면 역자로 보지 않음
        if re.fullmatch(
            rf"{esc}\s*\(\s*(?:지은이|지음|그림|편집|감수|일러스트|사진|촬영)\s*\)",
            ch,
            re.UNICODE,
        ):
            continue

        # 이름 직후 괄호 안에 역자 키워드
        if re.search(
            rf"{esc}\s*\([^)]*(?:옮긴이|역자|옮김|번역)[^)]*\)",
            ch,
            re.UNICODE,
        ):
            return True
        # 이름 (역) — '역' 단독 역할
        if re.search(rf"{esc}\s*\(\s*역\s*\)", ch, re.UNICODE):
            return True
        # 괄호 없이 "이름 옮김" 등
        if re.search(
            rf"{esc}\s+(?:옮긴이|역자|옮김|번역)(?=\s*(?:,|$))",
            ch,
            re.UNICODE,
        ):
            return True
        # 유연 패턴: 이름 + 선택 '(' + … 역자 키워드 … + 선택 ')'
        # '역'은 앞뒤가 비한글일 때만 (역사, 지은이 등 오탐 완화)
        if re.search(
            rf"{esc}\s*\(?[^)]*"
            r"(?:옮긴이|역자|옮김|번역|(?<![가-힣])역(?![가-힣]))"
            r"[^)]*\)?",
            ch,
            re.UNICODE,
        ):
            return True
    return False


def is_translator_role(book: dict, target_name: str) -> bool:
    """
    subInfo.authors가 있으면 구조화된 역할로 판별.
    authors가 비었거나 없으면 book['author'] 원시 문자열 Regex 폴백.
    """
    sub = book.get("subInfo") or {}
    authors = sub.get("authors")
    if isinstance(authors, list) and len(authors) > 0:
        for auth in authors:
            if not isinstance(auth, dict):
                continue
            if not _author_name_equals_target(target_name, (auth.get("authorName") or "")):
                continue
            desc = (auth.get("authorTypeDesc") or "") + ""
            name_t = (auth.get("authorTypeName") or "") + ""
            role_blob = f"{desc} {name_t}"
            if any(marker in role_blob for marker in _TRANSLATOR_MARKERS_FOR_CATALOG):
                return True
        return False
    return _fallback_translator_role_from_raw_author(book.get("author") or "", target_name)


# ---------------------------------------------------------------------------
# 알라딘 API
# ---------------------------------------------------------------------------


def _get_json(url: str, params: dict, timeout: int = 15) -> dict:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def item_lookup(
    isbn_clean: str,
    ttbkey: str,
    opt_result: str = OPT_LOOKUP,
    item_id_type: Optional[str] = None,
) -> dict:
    if item_id_type:
        itype = item_id_type
    else:
        itype = "ISBN13" if len(isbn_clean) == 13 else "ISBN"
    params = {
        "ttbkey": ttbkey.strip(),
        "itemIdType": itype,
        "ItemId": isbn_clean,
        "output": "js",
        "Version": API_VERSION,
        "OptResult": opt_result,
    }
    return _get_json(ITEM_LOOKUP, params)


def get_item_ids_by_author_id(author_id: int, name: str = "") -> List[str]:
    """
    알라딘 웹 검색(AuthorSearch=이름@AuthorId) HTML에서 ItemId 목록 추출.
    TTB API는 AuthorId 직접 조회를 지원하지 않아 크롤링으로 ID를 수집한다.
    """
    nm = (name or "").strip()
    author_search = f"{nm}@{author_id}" if nm else f"@{author_id}"
    headers = {"User-Agent": WSEARCH_USER_AGENT}
    resp = requests.get(
        ALADIN_WSEARCH,
        params={"AuthorSearch": author_search},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    ids = re.findall(r"wproduct\.aspx\?ItemId=(\d+)", resp.text)
    seen: set[str] = set()
    out: List[str] = []
    for iid in ids:
        if iid not in seen:
            seen.add(iid)
            out.append(iid)
    return out


def item_search_translator_catalog(
    translator_display_name: str, ttbkey: str, max_results: int = 50
) -> dict:
    params = {
        "ttbkey": ttbkey.strip(),
        "QueryType": "Author",
        "Query": translator_display_name.strip(),
        "MaxResults": str(max_results),
        "start": "1",
        "SearchTarget": "Book",
        "output": "js",
        "Version": API_VERSION,
        "OptResult": "authors",
    }
    return _get_json(ITEM_SEARCH, params)


def item_lookup_minimal(isbn13: str, ttbkey: str) -> dict:
    params = {
        "ttbkey": ttbkey.strip(),
        "itemIdType": "ISBN13",
        "ItemId": isbn13.replace("-", "").strip(),
        "output": "js",
        "Version": API_VERSION,
        "OptResult": "authors",
    }
    return _get_json(ITEM_LOOKUP, params)


# ---------------------------------------------------------------------------
# 역자 식별 · 저자 페이지 URL
# ---------------------------------------------------------------------------


def aladin_author_page_url(author_id: Optional[int]) -> Optional[str]:
    if author_id is None:
        return None
    return f"https://www.aladin.co.kr/author/wauthor_overview.aspx?AuthorSearch=@{author_id}"


def _author_dict_extra_link(auth: dict) -> Optional[str]:
    for k, v in auth.items():
        if not isinstance(v, str) or "aladin.co.kr" not in v:
            continue
        if "wauthor" in v or "author" in v.lower():
            return v.split()[0] if v else None
    return None


def extract_translators_from_item(item: dict) -> List[dict]:
    out: List[dict] = []
    raw_author = item.get("author") or ""
    authors_list = (item.get("subInfo") or {}).get("authors") or []

    for auth in authors_list:
        if not isinstance(auth, dict):
            continue
        role = (auth.get("authorTypeDesc") or auth.get("authorTypeName") or "") + ""
        if not _role_is_translator(role):
            continue
        name = (auth.get("authorName") or "").strip()
        if not name:
            continue
        aid = auth.get("authorId")
        try:
            aid_int = int(aid) if aid is not None else None
        except (TypeError, ValueError):
            aid_int = None
        link = _author_dict_extra_link(auth) or aladin_author_page_url(aid_int)
        out.append(
            {
                "name": name,
                "authorId": aid_int,
                "authorPageUrl": link,
                "role": role,
                "_raw": auth,
            }
        )

    if not out and raw_author:
        for part in raw_author.split(","):
            if any(k in part for k in ["옮긴이", "역자", "옮김", "역"]):
                name = re.sub(
                    r"\(.*?\)|옮긴이|역자|옮김|지은이|지음|역",
                    "",
                    part,
                    flags=re.I,
                ).strip()
                if name:
                    out.append(
                        {
                            "name": name,
                            "authorId": None,
                            "authorPageUrl": None,
                            "role": "문자열파싱",
                            "_raw": None,
                        }
                    )
    return out


def extract_writer_names_from_item(item: dict) -> List[str]:
    names: List[str] = []
    for auth in (item.get("subInfo") or {}).get("authors") or []:
        if not isinstance(auth, dict):
            continue
        role = (auth.get("authorTypeDesc") or auth.get("authorTypeName") or "") + ""
        if any(k in role for k in ["지은이", "지음", "글"]):
            n = (auth.get("authorName") or "").strip()
            if n:
                names.append(n)
    if not names and item.get("author"):
        for part in item["author"].split(","):
            if "지은이" in part or "지음" in part:
                n = re.sub(r"\(.*?\)|지은이|지음|글", "", part).strip()
                if n:
                    names.append(n)
    return list(dict.fromkeys(names))


def collect_biography_text(item: dict, translator_name: str) -> str:
    chunks: List[str] = []
    sub = item.get("subInfo") or {}
    for auth in sub.get("authors") or []:
        if not isinstance(auth, dict):
            continue
        if translator_name and (auth.get("authorName") or "").strip() != translator_name.strip():
            continue
        for key in (
            "authorBio",
            "biography",
            "authorIntro",
            "intro",
            "description",
            "authorDescription",
            "profile",
        ):
            val = auth.get(key)
            if isinstance(val, str) and val.strip():
                chunks.append(val.strip())
        for k, v in auth.items():
            if k in ("authorName", "authorId", "authorTypeDesc", "authorTypeName"):
                continue
            if isinstance(v, str) and len(v) > 40:
                chunks.append(v.strip())

    for key in ("fulldescription", "fullDescription", "Story", "story", "toc", "Toc"):
        v = item.get(key) or sub.get(key)
        if isinstance(v, str) and len(v) > 80:
            chunks.append(v[:5000])

    return "\n\n".join(dict.fromkeys(chunks))


# ---------------------------------------------------------------------------
# 동명이인: 카테고리 (대분류만 느슨하게)
# ---------------------------------------------------------------------------


def category_segments(cat: Optional[str]) -> List[str]:
    if not cat:
        return []
    s = cat.replace("국내도서", "").replace("외국도서", "").replace("eBook", "")
    return [p.strip() for p in s.split(">") if p.strip()]


def category_overlap_loose(target_cat: str, book_cat: str) -> bool:
    """대분류(첫 세그먼트)만 같으면 True. 한쪽이 비어 있으면 필터를 가리지 않음."""
    ta = category_segments(target_cat)
    ba = category_segments(book_cat)
    if not ta or not ba:
        return True
    return ta[0] == ba[0]


# ---------------------------------------------------------------------------
# authors 보강 (검색 응답에 역할 정보가 없을 때)
# ---------------------------------------------------------------------------


def enrich_catalog_with_authors_lookup(
    books: List[dict], ttbkey: str, target_name: str, max_lookups: int = 25
) -> List[dict]:
    out: List[dict] = []
    lookups = 0
    for b in books:
        if is_translator_role(b, target_name):
            out.append(b)
            continue
        if lookups >= max_lookups:
            continue
        isbn = (b.get("isbn13") or b.get("isbn") or "").replace("-", "").strip()
        if len(isbn) != 13:
            continue
        try:
            data = item_lookup_minimal(isbn, ttbkey)
            lookups += 1
        except (requests.RequestException, KeyError, ValueError):
            continue
        items = data.get("item") or []
        if not items:
            continue
        merged = {**b, "subInfo": {**(b.get("subInfo") or {}), **(items[0].get("subInfo") or {})}}
        if is_translator_role(merged, target_name):
            out.append(merged)
    return out


# ---------------------------------------------------------------------------
# 원제 문자 체계 · 메타 휴리스틱
# ---------------------------------------------------------------------------

RE_KANA = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
RE_HAN = re.compile(r"[\u4E00-\u9FFF]")
RE_LATIN = re.compile(r"[A-Za-z]")


COUNTRY_LANG_HINTS: List[Tuple[str, str]] = [
    (r"영국|영어|영문|미국|American|English", "메타_영어권"),
    (r"일본|日|ジャパン", "메타_일본"),
    (r"중국|中文|汉语", "메타_중국"),
    (r"프랑스|프랑스어|French", "메타_프랑스"),
    (r"독일|German|Deutsch", "메타_독일"),
    (r"이탈리아|Italian", "메타_이탈리아"),
    (r"스페인|Spanish|Español", "메타_스페인"),
    (r"러시아|Russian|俄", "메타_러시아"),
    (r"한국|국내", "메타_한국"),
]


def _script_weights_on_text(text: str) -> Dict[str, float]:
    w: Dict[str, float] = {}
    if not text:
        return w
    if RE_KANA.search(text):
        w["원제_가나(일본어)"] = w.get("원제_가나(일본어)", 0.0) + 2.0
    if RE_HAN.search(text):
        w["원제_한자(중국어)"] = w.get("원제_한자(중국어)", 0.0) + 1.0
        w["원제_한자(일본어)"] = w.get("원제_한자(일본어)", 0.0) + 1.0
    if RE_LATIN.search(text):
        w["원제_라틴(영미·유럽권)"] = w.get("원제_라틴(영미·유럽권)", 0.0) + 1.5
    return w


def infer_signals_from_book(book: dict) -> Dict[str, Any]:
    title = (book.get("title") or "") + " " + (book.get("description") or "")
    sub = book.get("subInfo") or {}
    ot = (sub.get("originalTitle") or sub.get("subTitle") or "").strip()
    blob = f"{title} {ot}"

    hint_weights: MutableMapping[str, float] = defaultdict(float)
    for pat, label in COUNTRY_LANG_HINTS:
        if re.search(pat, blob, re.I):
            hint_weights[label] += 1.0

    script_src = ot if ot else title
    for k, v in _script_weights_on_text(script_src).items():
        hint_weights[k] += v

    if ot and RE_LATIN.search(ot) and not re.search(r"[가-힣]", ot):
        hint_weights["원제_라틴_보조(영어 가능)"] += 0.5

    return {
        "originalTitle": ot or None,
        "hint_weights": dict(hint_weights),
        "categoryName": book.get("categoryName"),
        "publisher": book.get("publisher"),
    }


def weighted_hint_counts(
    books: List[dict], target_publisher: str
) -> Tuple[Dict[str, float], List[dict]]:
    counts: Dict[str, float] = {}
    rows: List[dict] = []
    tp = (target_publisher or "").strip()
    for b in books:
        sig = infer_signals_from_book(b)
        pub_w = PUBLISHER_WEIGHT if tp and (b.get("publisher") or "").strip() == tp else 1.0
        row = {
            "title": b.get("title"),
            "weight": pub_w,
            **sig,
        }
        rows.append(row)
        for label, score in sig["hint_weights"].items():
            counts[label] = counts.get(label, 0.0) + float(score) * pub_w
    return counts, rows


# ---------------------------------------------------------------------------
# 전공 → 언어 (Regex 보조 + LLM)
# ---------------------------------------------------------------------------

_MAJOR_LANG_RULES: List[Tuple[str, str]] = [
    (r"노어|러시아|슬라브", "러시아어"),
    (r"영미|영어|미국문학|영문", "영어"),
    (r"불어|프랑스", "프랑스어"),
    (r"독어|독일", "독일어"),
    (r"스페인|스페인어|히스패닉", "스페인어"),
    (r"이탈리아|이태리", "이탈리아어"),
    (r"일본|일어", "일본어"),
    (r"중국|중문|한문|중어", "중국어"),
    (r"아랍|터키|페르시아|이란", "아랍어권·중동어권"),
    (r"노르웨이|스웨덴|덴마크|북유럽", "북유럽어권"),
    (r"라틴아메리카|포르투갈|브라질", "포르투갈어"),
    (r"한국어|국어국문", "한국어"),
]


def infer_language_from_major_text(major: Optional[str]) -> Optional[str]:
    if not major:
        return None
    m = major.strip()
    for pat, lang in _MAJOR_LANG_RULES:
        if re.search(pat, m, re.I):
            return lang
    return None


def extract_univ_major_regex(text: str) -> Optional[Dict[str, Optional[str]]]:
    if not text or not text.strip():
        return None
    m = re.search(
        r"([가-힣A-Za-z·\s]{2,30}(?:대학교|대학|대))\s*([가-힣A-Za-z·\s]{2,20}(?:학과|전공|학부))",
        text,
    )
    if not m:
        m = re.search(
            r"([가-힣A-Za-z·\s]{2,30}(?:대학교|대학|대))\s*에서\s*([가-힣A-Za-z·\s]{2,20}(?:학과|전공|학부))",
            text,
        )
    if not m:
        return None
    uni, maj = m.group(1).strip(), m.group(2).strip()
    inferred = infer_language_from_major_text(maj)
    return {
        "university": uni,
        "major": maj,
        "inferred_language": inferred,
    }


def extract_univ_major_llm(
    text: str, api_key: str
) -> Optional[Dict[str, Optional[str]]]:
    if not api_key or not text.strip():
        return None
    system = (
        "You read Korean biographies of translators. "
        "Extract university, major, AND infer the primary source language they most likely "
        "translate from, based on the major name (e.g. 노어노문학과 → 러시아어, 영어영문학과 → 영어). "
        "Use concise Korean language names for inferred_language (e.g. 러시아어, 영어, 일본어, 독일어, 중국어). "
        "If impossible, use null. "
        'Reply JSON only: {"university": string|null, "major": string|null, "inferred_language": string|null}'
    )
    body = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:8000]},
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["choices"][0]["message"]["content"]
        obj = json.loads(raw)
        u = obj.get("university")
        mj = obj.get("major")
        inf = obj.get("inferred_language")
        return {
            "university": (u or "").strip() or None,
            "major": (mj or "").strip() or None,
            "inferred_language": (inf or "").strip() or None,
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# 최종 원서 언어 판정
# ---------------------------------------------------------------------------


def _collapse_career_hints(hints: Mapping[str, float]) -> Dict[str, float]:
    collapsed: Dict[str, float] = defaultdict(float)
    for label, score in hints.items():
        if score <= 0:
            continue
        if label.startswith("메타_"):
            w = score * 0.6
            if "일본" in label:
                collapsed["일본어"] += w
            elif "중국" in label:
                collapsed["중국어"] += w
            elif "영어" in label:
                collapsed["영어"] += w
            elif "프랑스" in label:
                collapsed["프랑스어"] += w
            elif "독일" in label:
                collapsed["독일어"] += w
            elif "스페인" in label:
                collapsed["스페인어"] += w
            elif "이탈리아" in label:
                collapsed["이탈리아어"] += w
            elif "러시아" in label:
                collapsed["러시아어"] += w
            elif "한국" in label:
                collapsed["한국어"] += w
            continue
        if "가나" in label:
            collapsed["일본어"] += score
        elif "한자(중국" in label:
            collapsed["중국어"] += score
        elif "한자(일본" in label:
            collapsed["일본어"] += score
        elif "라틴" in label or "영미" in label or "영어" in label:
            collapsed["영어"] += score
        elif "프랑스" in label:
            collapsed["프랑스어"] += score
        elif "독일" in label:
            collapsed["독일어"] += score
        elif "스페인" in label:
            collapsed["스페인어"] += score
        elif "이탈리아" in label:
            collapsed["이탈리아어"] += score
        elif "러시아" in label:
            collapsed["러시아어"] += score
        elif "한국" in label:
            collapsed["한국어"] += score
        elif "중동" in label or "아랍" in label:
            collapsed["아랍어권"] += score
        elif "북유럽" in label:
            collapsed["북유럽어권"] += score
        elif "포르투갈" in label:
            collapsed["포르투갈어"] += score
    return dict(collapsed)


def determine_final_language(
    inferred_from_major: Optional[str],
    career_hints: Mapping[str, float],
) -> Dict[str, Any]:
    tier = 3
    reason = "커리어·전공 단서 부족"
    conclusion = "판별 불가"
    major_s = (inferred_from_major or "").strip()
    if major_s:
        return {
            "conclusion": major_s,
            "tier": 1,
            "reason": "전공(소개) 기반 추론 언어",
            "career_runner_up": None,
            "raw_career_collapse": _collapse_career_hints(career_hints),
        }

    collapsed = _collapse_career_hints(career_hints)
    if collapsed:
        best_lang, best_score = max(collapsed.items(), key=lambda kv: kv[1])
        if best_score > 0:
            conclusion = best_lang
            tier = 2
            reason = "필터링된 커리어 도서 메타·원제 문자 힌트 가중 합산"
            return {
                "conclusion": conclusion,
                "tier": tier,
                "reason": reason,
                "career_runner_up": collapsed,
                "raw_career_collapse": collapsed,
            }

    return {
        "conclusion": conclusion,
        "tier": tier,
        "reason": reason,
        "career_runner_up": None,
        "raw_career_collapse": collapsed,
    }


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="알라딘 번역가 분석", layout="wide")
st.title("알라딘 번역가 · 커리어 · 원서 언어 추론")

with st.sidebar:
    st.markdown("**옵션**")
    use_category_filter = st.checkbox(
        "대분류 카테고리 필터(동명이인, 느슨)", value=True
    )
    enrich_missing_authors = st.checkbox(
        "authors 누락 시 ISBN LookUp으로 보강(느림, 호출↑)", value=False
    )
    openai_key = st.text_input(
        "OpenAI API 키(선택, 전공·언어 LLM)",
        type="password",
        value=os.environ.get("OPENAI_API_KEY", ""),
    )

with st.form("main"):
    ttb_key = st.text_input("TTB 키", type="password")
    isbn = st.text_input("ISBN (13 또는 10)")
    submitted = st.form_submit_button("분석 실행")

if submitted:
    if not ttb_key or not isbn:
        st.error("TTB 키와 ISBN을 입력하세요.")
    else:
        isbn_clean = isbn.replace("-", "").strip()
        try:
            with st.spinner("상품 조회(ItemLookUp)…"):
                data = item_lookup(isbn_clean, ttb_key)
            if data.get("errorCode"):
                st.error(f"API 오류: {data.get('errorMessage')}")
            else:
                items = data.get("item") or []
                if not items:
                    st.warning("도서를 찾을 수 없습니다.")
                else:
                    item = items[0]
                    target_cat = item.get("categoryName") or ""
                    target_pub = (item.get("publisher") or "").strip()
                    translators = extract_translators_from_item(item)
                    writers = extract_writer_names_from_item(item)

                    st.subheader("현재 도서")
                    st.write(
                        {
                            "title": item.get("title"),
                            "publisher": target_pub,
                            "categoryName": target_cat,
                            "isbn13": item.get("isbn13") or isbn_clean,
                        }
                    )

                    if not translators:
                        st.warning("번역가 정보가 없습니다.")
                    else:
                        for tr in translators:
                            st.divider()
                            st.success(f"번역가: **{tr['name']}**")
                            c1, c2 = st.columns(2)
                            with c1:
                                st.markdown("**역자 Author ID · 알라딘 역자 페이지**")
                                st.write(
                                    {
                                        "translatorAuthorId": tr.get("authorId"),
                                        "translatorAuthorPageUrl": tr.get("authorPageUrl")
                                        or aladin_author_page_url(tr.get("authorId")),
                                    }
                                )
                            with c2:
                                st.markdown("**지은이(원저자) — 참고**")
                                st.write(writers or "(없음)")

                            bio_text = collect_biography_text(item, tr["name"])
                            st.markdown("**역자 소개 후보 (ItemLookUp)**")
                            inferred_major_lang: Optional[str] = None
                            education_block: Optional[Dict[str, Optional[str]]] = None

                            if bio_text:
                                st.text_area(
                                    "biography_raw",
                                    bio_text[:4000],
                                    height=160,
                                    key=f"bio_{tr['name']}",
                                )
                                reg = extract_univ_major_regex(bio_text)
                                if reg:
                                    st.info(f"1단계 Regex: `{reg}`")
                                    education_block = reg
                                    inferred_major_lang = reg.get("inferred_language")
                                if openai_key.strip():
                                    with st.spinner("LLM: 전공 + 원서 언어 추론…"):
                                        llm = extract_univ_major_llm(bio_text, openai_key.strip())
                                    if llm:
                                        st.info(f"LLM: `{llm}`")
                                        education_block = llm
                                        inf = llm.get("inferred_language")
                                        if inf:
                                            inferred_major_lang = inf
                                elif not reg:
                                    st.caption("Regex 실패 시 OpenAI 키로 LLM 단계를 쓸 수 있습니다.")
                            else:
                                st.caption("역자 소개 텍스트가 비어 있을 수 있습니다.")

                            raw_list: List[dict] = []
                            author_id = tr.get("authorId")
                            if author_id is not None:
                                st.caption(
                                    f"AuthorId **{author_id}** 기반 도서 수집 "
                                    f"(웹 검색 → ItemLookUp, 상위 30권)"
                                )
                                with st.spinner(
                                    f"알라딘 웹 검색에서 AuthorId={author_id} 도서 ID 수집…"
                                ):
                                    item_ids = get_item_ids_by_author_id(
                                        int(author_id), tr["name"]
                                    )
                                st.caption(
                                    f"웹 검색에서 **{len(item_ids)}**개 ItemId 추출 "
                                    f"(API 조회는 상위 30개)"
                                )
                                lookup_ids = item_ids[:30]
                                for idx, iid in enumerate(lookup_ids, start=1):
                                    with st.spinner(
                                        f"ItemLookUp ({idx}/{len(lookup_ids)}) "
                                        f"ItemId={iid}…"
                                    ):
                                        try:
                                            lk = item_lookup(
                                                iid,
                                                ttb_key,
                                                opt_result="authors",
                                                item_id_type="ItemId",
                                            )
                                        except requests.RequestException:
                                            continue
                                        for it in lk.get("item") or []:
                                            if isinstance(it, dict):
                                                raw_list.append(it)
                                st.markdown(
                                    f"**역자 검색 원본**: {len(raw_list)}권 "
                                    f"(AuthorId=`{author_id}`, AuthorSearch="
                                    f"`{tr['name']}@{author_id}`)"
                                )
                            else:
                                st.caption(
                                    "AuthorId 없음 → 역자명 ItemSearch 폴백 (동명이인 혼입 가능)"
                                )
                                with st.spinner(
                                    "역자명 ItemSearch (OptResult=authors) 최대 50권…"
                                ):
                                    author_json = item_search_translator_catalog(
                                        tr["name"], ttb_key, 50
                                    )
                                raw_list = author_json.get("item") or []
                                st.markdown(
                                    f"**역자 검색 원본**: {len(raw_list)}권 "
                                    f"(Query=`{tr['name']}`, 이름 기반 폴백)"
                                )

                            n_role_raw = sum(
                                1 for b in raw_list if is_translator_role(b, tr["name"])
                            )
                            work_list: List[dict] = list(raw_list)
                            if enrich_missing_authors and n_role_raw < max(1, len(raw_list)) * 0.3:
                                with st.spinner("ISBN LookUp으로 authors 보강(제한)…"):
                                    work_list = enrich_catalog_with_authors_lookup(
                                        raw_list, ttb_key, tr["name"], max_lookups=25
                                    )
                                st.caption(
                                    f"보강 후 `is_translator_role` 통과 후보: **{len(work_list)}**권 "
                                    f"(원본 대비 역자 명시 적을 때만 보강)"
                                )

                            filtered = [
                                b
                                for b in work_list
                                if is_translator_role(b, tr["name"])
                                and (
                                    not use_category_filter
                                    or category_overlap_loose(
                                        target_cat, b.get("categoryName") or ""
                                    )
                                )
                            ]
                            n_role_work = sum(
                                1 for b in work_list if is_translator_role(b, tr["name"])
                            )
                            st.caption(
                                f"`is_translator_role` 통과: **{n_role_work}**권 / 작업 목록 "
                                f"**{len(work_list)}**권 → 최종 필터 후 **{len(filtered)}**권"
                            )

                            counts, detail_rows = weighted_hint_counts(filtered, target_pub)
                            st.markdown("**커리어 언어·원제 힌트 (출판사 가중 반영)**")
                            st.json(dict(sorted(counts.items(), key=lambda x: -x[1])))

                            with st.expander("필터 통과 도서 요약"):
                                st.dataframe(
                                    [
                                        {
                                            "title": r["title"],
                                            "publisher": r.get("publisher"),
                                            "weight": r["weight"],
                                            "hints": json.dumps(
                                                r.get("hint_weights") or {},
                                                ensure_ascii=False,
                                            ),
                                            "originalTitle": r.get("originalTitle"),
                                        }
                                        for r in detail_rows[:50]
                                    ],
                                    use_container_width=True,
                                )

                            final = determine_final_language(inferred_major_lang, counts)

                            st.markdown("---")
                            st.subheader("최종 원서 언어 판정")
                            if final["tier"] <= 2:
                                st.success(
                                    f"**원서 언어 추정:** {final['conclusion']} "
                                    f"({final['tier']}순위 근거 — {final['reason']})"
                                )
                            else:
                                st.warning(
                                    f"**원서 언어:** {final['conclusion']} — {final['reason']}"
                                )
                            cols = st.columns([2, 1, 1])
                            with cols[0]:
                                st.metric(
                                    label="결론 (원서 언어)",
                                    value=final["conclusion"],
                                )
                            with cols[1]:
                                st.metric(label="근거 단계", value=f"{final['tier']}순위")
                            with cols[2]:
                                st.caption(final["reason"])

                            if final.get("career_runner_up"):
                                st.success(
                                    f"2순위 세부 축: `{final['career_runner_up']}`"
                                )

                            with st.expander("판정 상세 JSON"):
                                st.json(
                                    {
                                        "education_extraction": education_block,
                                        "inferred_from_major": inferred_major_lang,
                                        "career_hint_weights": counts,
                                        "final": final,
                                    }
                                )

                            with st.expander("API 디버그: 현재 도서 역자 authors"):
                                st.json(
                                    [
                                        a
                                        for a in (item.get("subInfo") or {}).get("authors") or []
                                        if isinstance(a, dict)
                                        and tr["name"] in (a.get("authorName") or "")
                                    ]
                                )

        except requests.RequestException as e:
            st.error(f"HTTP 오류: {e}")
        except Exception as e:
            st.error(f"오류: {e}")
