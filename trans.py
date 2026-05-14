"""
알라딘 TTB: ISBN으로 번역가 식별 → Author 검색(최대 50권) → 카테고리 교차 필터·출판사 가중 →
원제/언어 휴리스틱, 저자(지은이) 병행 조회, 역자 소개는 Regex → (선택) LLM JSON 구조화.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st

ITEM_LOOKUP = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ITEM_SEARCH = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
API_VERSION = "20131101"
# 부가정보: authors(역할/ID), 카테고리 트리, 책소개 등 — 응답에 biography류 키가 있으면 파싱
OPT_LOOKUP = "authors,categoryIdList,fulldescription,Story,toc"

# 출판사 일치 시 통계 가중 (동명이인·언어 추론용)
PUBLISHER_WEIGHT = 8.0

# --- 알라딘 호출 ---


def _get_json(url: str, params: dict, timeout: int = 15) -> dict:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def item_lookup(isbn_clean: str, ttbkey: str, opt_result: str = OPT_LOOKUP) -> dict:
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


def item_search_author(author_name: str, ttbkey: str, max_results: int = 50) -> dict:
    params = {
        "ttbkey": ttbkey.strip(),
        "QueryType": "Author",
        "Query": author_name.strip(),
        "MaxResults": str(max_results),
        "start": "1",
        "SearchTarget": "Book",
        "output": "js",
        "Version": API_VERSION,
    }
    return _get_json(ITEM_SEARCH, params)


# --- 번역가 / 저자 ID ---


def aladin_author_page_url(author_id: Optional[int]) -> Optional[str]:
    if author_id is None:
        return None
    return f"https://www.aladin.co.kr/author/wauthor_overview.aspx?AuthorSearch=@{author_id}"


def _author_dict_extra_link(auth: dict) -> Optional[str]:
    """API가 주는 임의 키 중 저자 페이지 URL 스캔."""
    for k, v in auth.items():
        if not isinstance(v, str) or "aladin.co.kr" not in v:
            continue
        if "wauthor" in v or "author" in v.lower():
            return v.split()[0] if v else None
    return None


def extract_translators_from_item(item: dict) -> List[dict]:
    """역자 목록: 이름, authorId, API 제공 링크, 원시 author 엔트리."""
    out: List[dict] = []
    raw_author = item.get("author") or ""
    authors_list = (item.get("subInfo") or {}).get("authors") or []

    for auth in authors_list:
        if not isinstance(auth, dict):
            continue
        role = (auth.get("authorTypeDesc") or auth.get("authorTypeName") or "") + ""
        if not any(k in role for k in ["옮긴이", "역자", "역", "옮김"]):
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
    """지은이(원저자) 이름 — 병행 조회용."""
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
    """
    ItemLookUp 부가정보에서 역자 소개 후보 텍스트 수집.
    API 스펙에 따라 키가 다를 수 있어 authors 항목 전체 + 설명 필드를 훑음.
    """
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
        # 나머지 문자열 필드 중 긴 텍스트
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


# --- 동명이인: 카테고리 교차 필터 ---


def category_segments(cat: Optional[str]) -> List[str]:
    if not cat:
        return []
    s = cat.replace("국내도서", "").replace("외국도서", "").replace("eBook", "")
    return [p.strip() for p in s.split(">") if p.strip()]


def category_overlap_ok(target_cat: str, book_cat: str, min_depth_match: int = 1) -> bool:
    """대분류~중분류 수준에서 겹치면 동일 역자 후보로 간주."""
    ta = category_segments(target_cat)
    ba = category_segments(book_cat)
    if not ta or not ba:
        return True
    # 1-depth: 두 번째 세그먼트(예: 컴퓨터/모바일) 일치 우선
    for i in range(min(min_depth_match + 1, len(ta), len(ba))):
        if i < len(ta) and i < len(ba) and ta[i] == ba[i]:
            return True
    # 토큰 부분일치 (IT, 과학, 컴퓨터 등)
    tail_t = " ".join(ta[:3]).lower()
    tail_b = " ".join(ba[:3]).lower()
    for token in ("컴퓨터", "모바일", "IT", "과학", "자연", "프로그래", "머신", "데이터", "수학", "물리"):
        if token.lower() in tail_t and token.lower() in tail_b:
            return True
    return False


# --- 언어 / 원제 휴리스틱 ---


COUNTRY_LANG_HINTS = [
    (r"영국|영어|영문|미국|American|English", "영어권"),
    (r"일본|日|ジャパン", "일본"),
    (r"중국|中文|汉语", "중국"),
    (r"프랑스|프랑스어|French", "프랑스"),
    (r"독일|German|Deutsch", "독일"),
    (r"이탈리아|Italian", "이탈리아"),
    (r"스페인|Spanish|Español", "스페인"),
    (r"러시아|Russian", "러시아"),
    (r"한국|국내", "한국"),
]


def infer_signals_from_book(book: dict) -> Dict[str, Any]:
    title = (book.get("title") or "") + " " + (book.get("description") or "")
    sub = book.get("subInfo") or {}
    ot = sub.get("originalTitle") or sub.get("subTitle") or ""
    blob = f"{title} {ot}"
    hints: List[str] = []
    for pat, label in COUNTRY_LANG_HINTS:
        if re.search(pat, blob, re.I):
            hints.append(label)
    # 라틴 문자 비율이 높은 원제 → 영어 추정 보조
    if ot and re.search(r"[A-Za-z]{4,}", ot) and not re.search(r"[가-힣]", ot):
        hints.append("원제_라틴_문자(영어 가능)")
    return {
        "originalTitle": ot or None,
        "hints": list(dict.fromkeys(hints)),
        "categoryName": book.get("categoryName"),
        "publisher": book.get("publisher"),
    }


def weighted_hint_counts(
    books: List[dict], target_publisher: str
) -> Tuple[Dict[str, float], List[dict]]:
    counts: Dict[str, float] = {}
    rows = []
    tp = (target_publisher or "").strip()
    for b in books:
        sig = infer_signals_from_book(b)
        w = PUBLISHER_WEIGHT if tp and (b.get("publisher") or "").strip() == tp else 1.0
        row = {"title": b.get("title"), "weight": w, **sig}
        rows.append(row)
        for h in sig["hints"]:
            counts[h] = counts.get(h, 0.0) + w
    return counts, rows


# --- 학력: Regex → LLM ---


def extract_univ_major_regex(text: str) -> Optional[Dict[str, str]]:
    if not text or not text.strip():
        return None
    # 예: OO대학교 컴퓨터공학과, OO대 전자공학과
    m = re.search(
        r"([가-힣A-Za-z·\s]{2,30}(?:대학교|대학|대))\s*([가-힣A-Za-z·\s]{2,20}(?:학과|전공|학부))",
        text,
    )
    if m:
        return {"university": m.group(1).strip(), "major": m.group(2).strip()}
    m2 = re.search(
        r"([가-힣A-Za-z·\s]{2,30}(?:대학교|대학|대))\s*에서\s*([가-힣A-Za-z·\s]{2,20}(?:학과|전공|학부))",
        text,
    )
    if m2:
        return {"university": m2.group(1).strip(), "major": m2.group(2).strip()}
    return None


def extract_univ_major_llm(text: str, api_key: str) -> Optional[Dict[str, str]]:
    if not api_key or not text.strip():
        return None
    try:
        import urllib.request

        body = {
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "messages": [
                {
                    "role": "system",
                    "content": "You extract only university name and major from Korean biography text. Reply JSON: {\"university\": string|null, \"major\": string|null}",
                },
                {
                    "role": "user",
                    "content": text[:8000],
                },
            ],
            "response_format": {"type": "json_object"},
        }
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
        u, mj = obj.get("university"), obj.get("major")
        if u or mj:
            return {
                "university": (u or "").strip() or "",
                "major": (mj or "").strip() or "",
            }
    except Exception:
        return None
    return None


def run_parallel_lookups(ttbkey: str, translator_name: str, writer_names: List[str]):
    """번역가 저자검색 50 + (있으면) 지은이 저자검색 50 병행."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(item_search_author, translator_name, ttbkey, 50)
        fb = (
            ex.submit(item_search_author, writer_names[0], ttbkey, 50)
            if writer_names
            else None
        )
        author50 = fa.result()
        writer50 = fb.result() if fb else None
    return author50, writer50


# --- UI ---


st.set_page_config(page_title="알라딘 번역가 분석", layout="wide")
st.title("알라딘 번역가 · 커리어 · 동명이인 필터")

with st.sidebar:
    st.markdown("**옵션**")
    use_category_filter = st.checkbox("카테고리 교차 필터(동명이인)", value=True)
    openai_key = st.text_input(
        "OpenAI API 키(선택, 학력 LLM 단계)",
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
                                st.markdown("**Author ID / 알라딘 저자 페이지**")
                                st.write(
                                    {
                                        "authorId": tr.get("authorId"),
                                        "url": tr.get("authorPageUrl")
                                        or aladin_author_page_url(tr.get("authorId")),
                                    }
                                )
                            with c2:
                                st.markdown("**병행: 지은이(원저자)**")
                                st.write(writers or "(없음)")

                            bio_text = collect_biography_text(item, tr["name"])
                            st.markdown("**역자 소개 후보 텍스트 (ItemLookUp OptResult)**")
                            if bio_text:
                                st.text_area("biography_raw", bio_text[:4000], height=160, key=f"bio_{tr['name']}")
                                reg = extract_univ_major_regex(bio_text)
                                if reg:
                                    st.info(f"1단계 Regex: {reg}")
                                elif openai_key:
                                    with st.spinner("2단계 LLM…"):
                                        llm = extract_univ_major_llm(bio_text, openai_key.strip())
                                    if llm:
                                        st.info(f"2단계 LLM JSON: {llm}")
                                    else:
                                        st.caption("LLM에서 학력을 추출하지 못했습니다.")
                                else:
                                    st.caption("Regex 실패 시 OpenAI 키를 넣으면 LLM 단계가 동작합니다.")
                            else:
                                st.caption(
                                    "이 응답에는 역자 전용 biography 필드가 비어 있을 수 있습니다. "
                                    "authors 항목에 추가 키가 오는 경우가 있으며, 그때 자동 수집됩니다."
                                )

                            with st.spinner(
                                "병행 API: QueryType=Author 번역가 50권 + 지은이 50권…"
                            ):
                                author_json, writer_json = run_parallel_lookups(
                                    ttb_key, tr["name"], writers
                                )

                            raw_list = author_json.get("item") or []
                            st.markdown(f"**저자 검색 원본**: {len(raw_list)}권 (MaxResults=50)")

                            filtered = list(raw_list)
                            if use_category_filter and target_cat:
                                filtered = [
                                    b
                                    for b in raw_list
                                    if category_overlap_ok(
                                        target_cat, b.get("categoryName") or ""
                                    )
                                ]
                                st.caption(
                                    f"카테고리 필터 후 **{len(filtered)}**권 "
                                    f"(기준: `{target_cat[:60]}…`)"
                                )

                            counts, detail_rows = weighted_hint_counts(filtered, target_pub)
                            st.markdown("**언어·국가 힌트 (가중치: 동일 출판사 배수)**")
                            st.json(dict(sorted(counts.items(), key=lambda x: -x[1])))

                            with st.expander("필터 적용 도서 목록(요약)"):
                                st.dataframe(
                                    [
                                        {
                                            "title": r["title"],
                                            "publisher": r.get("publisher"),
                                            "weight": r["weight"],
                                            "hints": ", ".join(r.get("hints") or []),
                                            "originalTitle": r.get("originalTitle"),
                                        }
                                        for r in detail_rows[:50]
                                    ],
                                    use_container_width=True,
                                )

                            if writer_json and writer_json.get("item"):
                                with st.expander("지은이(원저자) 검색 50권 — 참고"):
                                    st.dataframe(
                                        [
                                            {
                                                "title": x.get("title"),
                                                "publisher": x.get("publisher"),
                                                "categoryName": x.get("categoryName"),
                                            }
                                            for x in (writer_json.get("item") or [])[:20]
                                        ],
                                        use_container_width=True,
                                    )

                            with st.expander("API 디버그: 역자 authors 원시 JSON"):
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
