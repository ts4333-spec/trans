"""
알라딘 TTB 베이스라인: ISBN → 역자 이름 추출 → ItemSearch 10권(무필터)
→ weighted_hint_counts → 원서 언어 힌트 요약.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Mapping, MutableMapping, Tuple

import requests
import streamlit as st

ITEM_LOOKUP = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
ITEM_SEARCH = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
API_VERSION = "20131101"
OPT_LOOKUP = "authors,categoryIdList,fulldescription,Story,toc"

PUBLISHER_WEIGHT = 8.0
CATALOG_MAX = 10

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


def item_search_translator_catalog(
    translator_display_name: str, ttbkey: str, max_results: int = CATALOG_MAX
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


def extract_translator_names(item: dict) -> List[str]:
    """subInfo.authors 또는 author 문자열에서 역자 이름만 추출."""
    names: List[str] = []
    for auth in (item.get("subInfo") or {}).get("authors") or []:
        if not isinstance(auth, dict):
            continue
        role = (auth.get("authorTypeDesc") or auth.get("authorTypeName") or "") + ""
        if not _role_is_translator(role):
            continue
        n = (auth.get("authorName") or "").strip()
        if n:
            names.append(n)
    raw = item.get("author") or ""
    if not names and raw:
        for part in raw.split(","):
            if any(k in part for k in ("옮긴이", "역자", "옮김", "역")):
                n = re.sub(
                    r"\(.*?\)|옮긴이|역자|옮김|지은이|지음|역",
                    "",
                    part,
                    flags=re.I,
                ).strip()
                if n:
                    names.append(n)
    return list(dict.fromkeys(names))


def extract_writer_names_from_item(item: dict) -> List[str]:
    names: List[str] = []
    for auth in (item.get("subInfo") or {}).get("authors") or []:
        if not isinstance(auth, dict):
            continue
        role = (auth.get("authorTypeDesc") or auth.get("authorTypeName") or "") + ""
        if any(k in role for k in ("지은이", "지음", "글")):
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
        rows.append({"title": b.get("title"), "weight": pub_w, **sig})
        for label, score in sig["hint_weights"].items():
            counts[label] = counts.get(label, 0.0) + float(score) * pub_w
    return counts, rows


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


def determine_final_language(career_hints: Mapping[str, float]) -> Dict[str, Any]:
    """검색 10권 힌트 가중치만으로 원서 언어 후보를 고름."""
    collapsed = _collapse_career_hints(career_hints)
    if not collapsed:
        return {
            "conclusion": "판별 불가",
            "tier": 1,
            "reason": "힌트 합산 결과가 비었습니다.",
            "breakdown": collapsed,
        }
    best_lang, best_score = max(collapsed.items(), key=lambda kv: kv[1])
    if best_score <= 0:
        return {
            "conclusion": "판별 불가",
            "tier": 1,
            "reason": "유효 가중치가 없습니다.",
            "breakdown": collapsed,
        }
    return {
        "conclusion": best_lang,
        "tier": 1,
        "reason": "검색 10권 메타·원제 문자 힌트 가중 합산",
        "breakdown": collapsed,
    }


# --- Streamlit ----------------------------------------------------------------

st.set_page_config(page_title="알라딘 번역가 (베이스라인)", layout="wide")
st.title("알라딘 번역가 · 10권 검색 · 언어 힌트")

with st.form("main"):
    ttb_key = st.text_input("TTB 키", type="password")
    isbn = st.text_input("ISBN (13 또는 10)")
    submitted = st.form_submit_button("실행")

if submitted:
    if not ttb_key or not isbn:
        st.error("TTB 키와 ISBN을 입력하세요.")
    else:
        isbn_clean = isbn.replace("-", "").strip()
        try:
            with st.spinner("ItemLookUp…"):
                data = item_lookup(isbn_clean, ttb_key)
            if data.get("errorCode"):
                st.error(f"API 오류: {data.get('errorMessage')}")
            else:
                items = data.get("item") or []
                if not items:
                    st.warning("도서를 찾을 수 없습니다.")
                else:
                    book = items[0]
                    target_pub = (book.get("publisher") or "").strip()
                    tnames = extract_translator_names(book)
                    writers = extract_writer_names_from_item(book)

                    st.subheader("현재 도서")
                    st.write(
                        {
                            "title": book.get("title"),
                            "publisher": target_pub,
                            "categoryName": book.get("categoryName"),
                            "isbn13": book.get("isbn13") or isbn_clean,
                        }
                    )

                    if not tnames:
                        st.warning("역자 이름을 찾지 못했습니다.")
                    else:
                        st.write("**역자 후보:**", ", ".join(tnames))
                        st.caption(f"지은이(참고): {', '.join(writers) if writers else '—'}")

                        for tr_name in tnames:
                            st.divider()
                            st.success(f"역자: **{tr_name}**")

                            with st.spinner(f"ItemSearch 10권 (Query={tr_name})…"):
                                j = item_search_translator_catalog(tr_name, ttb_key, CATALOG_MAX)
                            raw_list: List[dict] = j.get("item") or []

                            st.markdown(f"**검색 결과:** {len(raw_list)}권 (MaxResults={CATALOG_MAX}, 무필터)")
                            counts, detail_rows = weighted_hint_counts(raw_list, target_pub)

                            st.markdown("**언어·원제 힌트 (출판사 동일 시 가중)**")
                            st.json(dict(sorted(counts.items(), key=lambda x: -x[1])))

                            with st.expander("10권 요약"):
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
                                        for r in detail_rows
                                    ],
                                    use_container_width=True,
                                )

                            final = determine_final_language(counts)
                            st.markdown("---")
                            st.subheader("원서 언어 후보")
                            st.metric("결론", final["conclusion"])
                            st.caption(final["reason"])
                            with st.expander("상세"):
                                st.json(final)

        except requests.RequestException as e:
            st.error(f"HTTP 오류: {e}")
        except Exception as e:
            st.error(f"오류: {e}")
