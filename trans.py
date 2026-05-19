"""
알라딘 TTB: ISBN → 역자 식별(ItemLookUp) → 역자명 ItemSearch(최대 50권, OptResult=authors)
→ is_translator_role + (선택) 대분류 카테고리 일치 → 원제·커리어 힌트 → 최종 원서 언어 판정.
"""
from __future__ import annotations

import html as html_stdlib
import json
import os
import re
import urllib.error
import urllib.parse
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
ALADIN_WAUTHOR_OVERVIEW = "https://www.aladin.co.kr/author/wauthor_overview.aspx"
ALADIN_WSEARCH = "https://www.aladin.co.kr/search/wsearchresult.aspx"
ALADIN_WSEARCH_UPPER = "https://www.aladin.co.kr/Search/wSearchResult.aspx"
ALADIN_WPRODUCT = "https://www.aladin.co.kr/shop/wproduct.aspx"
WSEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ITEM_ID_HTML_RE = re.compile(r"itemid=(\d+)", re.I)  # ItemId / itemid 대소문자 무시
WPRODUCT_ITEM_ID_RE = re.compile(
    r"(?:/shop/)?wproduct\.aspx\?ItemId=(\d+)",
    re.I,
)
ITEM_ID_JSON_RE = re.compile(r'["\']ItemId["\']\s*:\s*["\']?(\d{6,})', re.I)
BIO_PROFILE_KEYWORDS = ("대학교", "학과", "졸업", "역서", "번역", "전공", "출신", "소개")
API_VERSION = "20131101"
OPT_LOOKUP = "authors,categoryIdList,fulldescription,Story,toc"

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


def _book_has_author_id(book: dict, author_id: int) -> bool:
    for auth in (book.get("subInfo") or {}).get("authors") or []:
        if not isinstance(auth, dict):
            continue
        try:
            if int(auth.get("authorId")) == int(author_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


def include_in_career_analysis(
    book: dict,
    target_name: str,
    author_id: Optional[int],
    target_cat: str,
    use_category_filter: bool,
) -> bool:
    """
    커리어 원서 언어 분석에 넣을지 판단.
    웹 AuthorSearch=이름@ID 로 수집한 도서는 역할 메타 누락 시에도 신뢰한다.
    """
    if use_category_filter and not category_overlap_loose(
        target_cat, book.get("categoryName") or ""
    ):
        return False
    if book.get("_webAuthorSearchTrusted"):
        return True
    if author_id is not None:
        if not _book_has_author_id(book, author_id):
            return False
        if book.get("_authorIdVerified"):
            return True
        return (
            is_translator_role(book, target_name)
            or _fallback_translator_role_from_raw_author(
                book.get("author") or "", target_name
            )
        )
    return is_translator_role(book, target_name)


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


def _aladin_web_headers() -> Dict[str, str]:
    return {
        "User-Agent": WSEARCH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.aladin.co.kr/",
        "Connection": "keep-alive",
    }


_aladin_http_session: Optional[requests.Session] = None


def _get_aladin_http_session() -> requests.Session:
    global _aladin_http_session
    if _aladin_http_session is None:
        sess = requests.Session()
        sess.headers.update(_aladin_web_headers())
        try:
            sess.get("https://www.aladin.co.kr/", timeout=15)
        except requests.RequestException:
            pass
        _aladin_http_session = sess
    return _aladin_http_session


def parse_manual_item_ids(text: str) -> List[str]:
    """사용자가 붙여넣은 ItemId 목록(숫자·URL·쉼표 구분) 파싱."""
    if not (text or "").strip():
        return []
    found = re.findall(r"(?:ItemId=)(\d{6,})|(?:^|[,\s])(\d{6,})(?:[,\s]|$)", text, re.I)
    out: List[str] = []
    seen: set[str] = set()
    for g1, g2 in found:
        iid = g1 or g2
        if iid and iid not in seen:
            seen.add(iid)
            out.append(iid)
    if not out:
        for iid in re.findall(r"\d{6,}", text):
            if iid not in seen:
                seen.add(iid)
                out.append(iid)
    return out


def _extract_item_ids_from_html(html: str) -> List[str]:
    """검색/저자 페이지 HTML에서 도서 ItemId 추출 (wproduct·JSON·일반 패턴)."""
    if not html:
        return []
    seen: set[str] = set()
    out: List[str] = []
    batches = (
        WPRODUCT_ITEM_ID_RE.findall(html),
        ITEM_ID_JSON_RE.findall(html),
        ITEM_ID_HTML_RE.findall(html),
    )
    for sources in batches:
        for iid in sources:
            if iid not in seen:
                seen.add(iid)
                out.append(iid)
    return out


def _fetch_html_item_ids(url: str, params: dict) -> Tuple[List[str], int]:
    sess = _get_aladin_http_session()
    resp = sess.get(url, params=params, timeout=25)
    resp.raise_for_status()
    text = resp.text or ""
    return _extract_item_ids_from_html(text), len(text)


def _crawl_wsearch_author_item_ids(
    search_query: str, max_pages: int = 5
) -> Tuple[List[str], int]:
    """
    wsearchresult AuthorSearch=이름@ID — 분야 제한 없음 + 국내도서, 페이지 순회.
    """
    merged: List[str] = []
    seen: set[str] = set()
    total_len = 0
    base_variants = (
        {"AuthorSearch": search_query},
        {"AuthorSearch": search_query, "BranchType": "1"},
    )
    search_urls = (ALADIN_WSEARCH, ALADIN_WSEARCH_UPPER)
    for base in base_variants:
        for page in range(1, max_pages + 1):
            params = {**base, "page": str(page), "ViewRowsCount": "50"}
            page_ids: List[str] = []
            for search_url in search_urls:
                try:
                    ids, resp_len = _fetch_html_item_ids(search_url, params)
                except requests.RequestException:
                    continue
                total_len += resp_len
                page_ids.extend(ids)
            new_ids = [i for i in dict.fromkeys(page_ids) if i not in seen]
            if not new_ids:
                break
            for iid in new_ids:
                seen.add(iid)
                merged.append(iid)
    return merged, total_len


def get_item_ids_by_author_id(
    author_id: int,
    name: str = "",
    extra_item_ids: Optional[List[str]] = None,
) -> List[str]:
    """
    알라딘 통합 검색(AuthorSearch=이름@ID) 및 저자 개요에서 ItemId 수집.
    """
    search_query = f"{name}@{author_id}" if name else f"@{author_id}"
    item_ids: List[str] = list(extra_item_ids or [])
    len_search, len_overview = 0, 0

    try:
        ids_search, len_search = _crawl_wsearch_author_item_ids(search_query)
        item_ids.extend(ids_search)
    except Exception:
        pass

    overview_params = {"AuthorSearch": f"@{author_id}"}
    try:
        ids_overview, len_overview = _fetch_html_item_ids(
            ALADIN_WAUTHOR_OVERVIEW, overview_params
        )
        item_ids.extend(ids_overview)
    except Exception:
        pass

    final_ids = list(dict.fromkeys(item_ids))

    if not final_ids:
        st.warning(
            f"⚠️ 도서 ID 추출 실패. 검색응답({len_search}자), 개요응답({len_overview}자). "
            "알라딘 봇 차단(CAPTCHA)이거나 HTML 구조 변경일 수 있습니다."
        )
    elif len(final_ids) <= 3 and len_search > 5000:
        st.caption(
            f"웹 HTML에서 도서 링크가 **{len(final_ids)}**개만 보입니다. "
            "브라딘 목록과 다르면 봇 차단으로 일부만 받았을 수 있습니다."
        )

    return final_ids


def _catalog_dedup_key(book: dict) -> str:
    isbn = (book.get("isbn13") or book.get("isbn") or "").replace("-", "").strip()
    if isbn:
        return isbn
    iid = book.get("itemId")
    if iid is not None:
        return str(iid).strip()
    return ""


def _append_catalog_book(
    book: dict,
    seen_keys: set[str],
    out: List[dict],
    web_author_search_trusted: bool = False,
    author_id_verified: bool = False,
) -> bool:
    if not isinstance(book, dict):
        return False
    key = _catalog_dedup_key(book)
    if not key or key in seen_keys:
        return False
    seen_keys.add(key)
    entry = dict(book)
    if web_author_search_trusted:
        entry["_webAuthorSearchTrusted"] = True
    if author_id_verified:
        entry["_authorIdVerified"] = True
    out.append(entry)
    return True


def _fetch_book_detail_with_authors(book: dict, ttbkey: str) -> Optional[dict]:
    """ItemId 또는 ISBN13으로 ItemLookUp(OptResult=authors) 후 상세 1건 반환."""
    iid = book.get("itemId")
    if iid is not None:
        try:
            data = item_lookup(
                str(iid).strip(),
                ttbkey,
                opt_result="authors",
                item_id_type="ItemId",
            )
            items = data.get("item") or []
            if items:
                return items[0]
        except requests.RequestException:
            pass
    isbn = (book.get("isbn13") or book.get("isbn") or "").replace("-", "").strip()
    if len(isbn) == 13:
        try:
            data = item_lookup_minimal(isbn, ttbkey)
            items = data.get("item") or []
            if items:
                return items[0]
        except requests.RequestException:
            pass
    return None


def _merge_api_item_lists(*lists: List[dict]) -> List[dict]:
    by_id: Dict[str, dict] = {}
    for lst in lists:
        for b in lst:
            if not isinstance(b, dict):
                continue
            iid = b.get("itemId")
            if iid is not None:
                by_id[str(iid)] = b
            else:
                key = _catalog_dedup_key(b)
                if key:
                    by_id[f"isbn:{key}"] = b
    return list(by_id.values())


def collect_translator_catalog_via_web_author_search(
    author_id: int,
    translator_name: str,
    ttbkey: str,
    api_list: List[dict],
    max_books: int = 30,
    extra_web_item_ids: Optional[List[str]] = None,
) -> Tuple[List[dict], Dict[str, int]]:
    """
    웹 wsearchresult(AuthorSearch=이름@ID) ItemId 목록을 기준으로 커리어 도서 수집.
    API ItemSearch 결과와 매칭하고, 없으면 ItemLookUp으로 보완한다.
    """
    stats = {
        "web_ids": 0,
        "from_api_map": 0,
        "from_lookup": 0,
        "from_api_author_id": 0,
        "from_api_verified": 0,
    }
    out: List[dict] = []
    seen: set[str] = set()

    web_ids = get_item_ids_by_author_id(
        author_id,
        translator_name,
        extra_item_ids=extra_web_item_ids,
    )
    stats["web_ids"] = len(web_ids)

    api_by_item_id: Dict[str, dict] = {}
    for b in api_list:
        if isinstance(b, dict) and b.get("itemId") is not None:
            api_by_item_id[str(b.get("itemId"))] = b

    for iid in web_ids[:max_books]:
        if iid in api_by_item_id:
            if _append_catalog_book(
                api_by_item_id[iid], seen, out, web_author_search_trusted=True
            ):
                stats["from_api_map"] += 1
            continue
        try:
            lk = item_lookup(
                iid,
                ttbkey,
                opt_result="authors",
                item_id_type="ItemId",
            )
        except requests.RequestException:
            continue
        for it in lk.get("item") or []:
            if _append_catalog_book(it, seen, out, web_author_search_trusted=True):
                stats["from_lookup"] += 1

    aid = int(author_id)
    for b in api_list:
        if not isinstance(b, dict):
            continue
        if _book_has_author_id(b, aid):
            if _append_catalog_book(b, seen, out, author_id_verified=True):
                stats["from_api_author_id"] += 1

    lookups = 0
    max_verify = 50
    for b in api_list:
        if len(out) >= max_books or lookups >= max_verify:
            break
        if not isinstance(b, dict):
            continue
        key = _catalog_dedup_key(b)
        if key and key in seen:
            continue
        if _book_has_author_id(b, aid):
            continue
        detail = _fetch_book_detail_with_authors(b, ttbkey)
        lookups += 1
        if not detail or not _book_has_author_id(detail, aid):
            continue
        if _append_catalog_book(detail, seen, out, author_id_verified=True):
            stats["from_api_verified"] += 1

    return out, stats


def _html_to_bio_text_lines(raw_html: str) -> List[str]:
    """script/style 제거 후 HTML 태그를 벗긴 순수 텍스트 줄 목록."""
    if not raw_html:
        return []
    s = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", raw_html, flags=re.I)
    s = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "\n", s)
    s = html_stdlib.unescape(s)
    lines: List[str] = []
    for line in s.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 8:
            lines.append(line)
    return lines


def scrape_author_bio_from_overview(author_id: int) -> str:
    """
    wauthor_overview 프로필 HTML에서 학력·번역 경력 키워드가 포함된 소개 블록 추출.
    """
    try:
        resp = requests.get(
            ALADIN_WAUTHOR_OVERVIEW,
            params={"AuthorSearch": f"@{author_id}"},
            headers=_aladin_web_headers(),
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return ""

    lines = _html_to_bio_text_lines(resp.text or "")
    hits = [ln for ln in lines if any(kw in ln for kw in BIO_PROFILE_KEYWORDS)]
    return "\n\n".join(dict.fromkeys(hits))


def item_search_translator_catalog(
    translator_display_name: str,
    ttbkey: str,
    max_results: int = 50,
    author_id: Optional[int] = None,
) -> dict:
    """
    역자 연관 도서 검색. author_id가 있으면 Query를 '이름@ID'로 보내 동명이인 혼입을 줄인다.
    (웹의 SearchWord=이름 과 달리, AuthorSearch=이름@ID 에 가깝게 동작하도록 시도)
    """
    q = translator_display_name.strip()
    if author_id is not None:
        q = f"{q}@{int(author_id)}" if q else f"@{int(author_id)}"
    params = {
        "ttbkey": ttbkey.strip(),
        "QueryType": "Author",
        "Query": q,
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


def _product_page_item_id(item: dict, isbn_fallback: str = "") -> Optional[str]:
    """도서 상세 웹페이지용 ItemId (API itemId 우선, 없으면 ISBN13)."""
    for key in ("itemId", "item_id"):
        v = item.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    isbn = (item.get("isbn13") or item.get("isbn") or isbn_fallback or "").replace(
        "-", ""
    ).strip()
    return isbn or None


def fetch_product_page_html(item_id: str) -> str:
    headers = {"User-Agent": WSEARCH_USER_AGENT}
    resp = requests.get(
        ALADIN_WPRODUCT,
        params={"ItemId": item_id},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.text


def resolve_author_id_from_product_html(
    html: str, translator_name: str
) -> Optional[int]:
    """
    상품 페이지 HTML에서 역자 저자 링크(<a> 앵커 텍스트)가 이름과 정확히 일치할 때만
    AuthorSearch 고유 ID를 반환한다. wauthor_overview / wsearchresult URL 모두 지원.
    """
    t = (translator_name or "").strip()
    if not t or not html:
        return None

    anchor_pat = re.compile(
        r'<a[^>]+href=["\']?(?:https?://(?:www\.)?aladin\.co\.kr)?'
        r'/(?:author/wauthor_overview|search/wsearchresult)\.aspx\?AuthorSearch='
        r'(?:[^"\'&>]*?@)?(\d+)[^"\'<>]*?["\']?[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    for m in anchor_pat.finditer(html):
        try:
            aid = int(m.group(1))
        except (TypeError, ValueError):
            continue
        text = re.sub(r"<[^>]+>", "", m.group(2))
        text = re.sub(r"\s+", " ", text).strip()
        if _author_name_equals_target(t, text):
            return aid
    return None


def scrape_author_id_from_product_page(
    item_id: str, translator_name: str
) -> Optional[int]:
    try:
        html = fetch_product_page_html(item_id)
    except requests.RequestException:
        return None
    return resolve_author_id_from_product_html(html, translator_name)


def enrich_translators_author_id_from_web(
    translators: List[dict],
    item: dict,
    isbn_fallback: str = "",
) -> List[dict]:
    """
    authorId가 비어 있는 역자에 대해 도서 상세 페이지 HTML에서 ID를 보완한다.
    """
    pid = _product_page_item_id(item, isbn_fallback)
    if not pid:
        return translators
    needs = [tr for tr in translators if tr.get("authorId") is None]
    if not needs:
        return translators

    html: Optional[str] = None
    for tr in needs:
        if html is None:
            try:
                html = fetch_product_page_html(pid)
            except requests.RequestException:
                return translators
        aid = resolve_author_id_from_product_html(html, tr["name"])
        if aid is None:
            continue
        tr["authorId"] = aid
        tr["authorPageUrl"] = aladin_author_page_url(aid)
        role = (tr.get("role") or "").strip()
        if role == "문자열파싱":
            tr["role"] = "문자열파싱(웹크롤링ID보완)"
        elif "웹크롤링ID보완" not in role:
            tr["role"] = f"{role}(웹크롤링ID보완)" if role else "웹크롤링ID보완"
    return translators


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


def weighted_hint_counts(books: List[dict]) -> Tuple[Dict[str, float], List[dict]]:
    counts: Dict[str, float] = {}
    rows: List[dict] = []
    for b in books:
        sig = infer_signals_from_book(b)
        row = {
            "title": b.get("title"),
            **sig,
        }
        rows.append(row)
        for label, score in sig["hint_weights"].items():
            counts[label] = counts.get(label, 0.0) + float(score)
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
    manual_item_ids_text = st.text_area(
        "수동 ItemId (웹 크롤 실패 시)",
        placeholder="345526780, 63778393, 103046432 …",
        help=(
            "브라우저에서 wsearchresult.aspx?AuthorSearch=이름@AuthorId "
            "페이지의 도서 URL에서 ItemId 숫자만 복사해 붙여넣으세요."
        ),
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
                    if any(tr.get("authorId") is None for tr in translators):
                        with st.spinner(
                            "API에 AuthorId 없음 → 도서 페이지에서 역자 ID 보완(웹 크롤링)…"
                        ):
                            translators = enrich_translators_author_id_from_web(
                                translators, item, isbn_clean
                            )
                        found = sum(1 for tr in translators if tr.get("authorId"))
                        st.caption(
                            f"웹 페이지 AuthorId 보완: **{found}**/{len(translators)}명 "
                            f"(상품 ItemId=`{_product_page_item_id(item, isbn_clean)}`)"
                        )
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
                            st.markdown("**역자 Author ID · 알라딘 역자 페이지**")
                            st.write(
                                {
                                    "translatorAuthorId": tr.get("authorId"),
                                    "translatorAuthorPageUrl": tr.get("authorPageUrl")
                                    or aladin_author_page_url(tr.get("authorId")),
                                }
                            )

                            bio_text = collect_biography_text(item, tr["name"])
                            if not (bio_text or "").strip() and tr.get("authorId"):
                                with st.spinner(
                                    "API 소개글 없음 → 웹 프로필에서 역자 소개글 수집…"
                                ):
                                    scraped_bio = scrape_author_bio_from_overview(
                                        int(tr["authorId"])
                                    )
                                if (scraped_bio or "").strip():
                                    bio_text = scraped_bio.strip()
                                    st.caption(
                                        "wauthor_overview 프로필에서 소개글 보완 "
                                        "(Regex·LLM 입력용)"
                                    )
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
                            seen_isbn: set[str] = set()

                            search_author_id = (
                                int(author_id) if author_id is not None else None
                            )
                            query_hint = (
                                f"{tr['name']}@{search_author_id}"
                                if search_author_id is not None
                                else tr["name"]
                            )
                            with st.spinner(
                                f"역자 ItemSearch API (Query=`{query_hint}`, 최대 50권)…"
                            ):
                                author_json = item_search_translator_catalog(
                                    tr["name"],
                                    ttb_key,
                                    50,
                                    author_id=search_author_id,
                                )
                            if author_json.get("errorCode"):
                                st.error(
                                    f"역자 검색 API 오류: {author_json.get('errorMessage')}"
                                )
                            api_list_name_id = author_json.get("item") or []
                            api_list_at_id: List[dict] = []
                            api_list_name_only: List[dict] = []
                            if search_author_id is not None:
                                with st.spinner(
                                    f"ItemSearch Query=`@{search_author_id}` (TTB)…"
                                ):
                                    at_id_json = item_search_translator_catalog(
                                        "",
                                        ttb_key,
                                        50,
                                        author_id=search_author_id,
                                    )
                                api_list_at_id = at_id_json.get("item") or []
                                with st.spinner(
                                    f"ItemSearch 이름만(`{tr['name']}`) 후보 확보…"
                                ):
                                    name_json = item_search_translator_catalog(
                                        tr["name"], ttb_key, 50
                                    )
                                api_list_name_only = name_json.get("item") or []
                            api_list = _merge_api_item_lists(
                                api_list_at_id,
                                api_list_name_id,
                                api_list_name_only,
                            )
                            manual_ids = parse_manual_item_ids(manual_item_ids_text)
                            if manual_ids:
                                st.success(
                                    f"수동 ItemId **{len(manual_ids)}**개를 "
                                    "웹 수집 목록에 합칩니다."
                                )
                            if api_list_at_id:
                                st.caption(
                                    f"TTB Query=`@{search_author_id}` → **{len(api_list_at_id)}**권 "
                                    "(가능하면 LookUp 없이 AuthorId 매칭)"
                                )
                            elif api_list_name_only and not api_list_name_id:
                                st.caption(
                                    "TTB `이름@ID` 0권 → 이름 검색 "
                                    f"**{len(api_list_name_only)}**권 LookUp 검증."
                                )
                            if not api_list:
                                st.warning(
                                    f"역자명 `{tr['name']}` ItemSearch 후보 0권. "
                                    "웹 AuthorSearch만 사용합니다."
                                )

                            if author_id is not None:
                                aid_int = int(author_id)
                                with st.spinner(
                                    f"웹 AuthorSearch + API LookUp 검증 "
                                    f"(최대 50회, AuthorId={aid_int})…"
                                ):
                                    raw_list, cat_stats = (
                                        collect_translator_catalog_via_web_author_search(
                                            aid_int,
                                            tr["name"],
                                            ttb_key,
                                            api_list,
                                            max_books=50,
                                            extra_web_item_ids=manual_ids,
                                        )
                                    )
                                st.caption(
                                    f"웹 ItemId **{cat_stats['web_ids']}**개 → 수집 **{len(raw_list)}**권 "
                                    f"(동명이인 제외: AuthorId **{aid_int}** 일치만) · "
                                    f"API매칭 {cat_stats['from_api_map']}, "
                                    f"LookUp {cat_stats['from_lookup']}, "
                                    f"API AuthorId {cat_stats['from_api_author_id']}, "
                                    f"API검증 {cat_stats['from_api_verified']}"
                                )
                                if cat_stats["web_ids"] == 0:
                                    _web_author_url = (
                                        "https://www.aladin.co.kr/search/wsearchresult.aspx?"
                                        f"AuthorSearch={urllib.parse.quote(query_hint)}"
                                        "&BranchType=1"
                                    )
                                    st.warning(
                                        "웹 검색에서 ItemId를 못 가져왔습니다. "
                                        "봇 차단(CAPTCHA)이거나 HTML 구조 변경일 수 있습니다. "
                                        f"브라우저에서 [이 링크]({_web_author_url})가 열리는지 확인하세요."
                                    )
                                elif len(raw_list) <= 3:
                                    st.warning(
                                        f"수집 **{len(raw_list)}**권뿐입니다. 자동 크롤이 막히면 "
                                        "사이드바 **「수동 ItemId」**에 브라우저 wsearchresult "
                                        "페이지의 ItemId를 붙여넣은 뒤 다시 실행하세요. "
                                        f"(예: 이지연@878593 검색 결과 **10권** URL에서 복사)"
                                    )
                                elif len(raw_list) == 0:
                                    st.warning(
                                        "도서 메타를 가져오지 못했습니다. "
                                        "TTB 키·수동 ItemId 입력을 확인하세요."
                                    )
                                st.markdown(
                                    f"**역자 검색 원본**: {len(raw_list)}권 "
                                    f"(웹 AuthorSearch=`{query_hint}` + API 보완)"
                                )
                            else:
                                st.warning(
                                    "AuthorId 없음 → 이름 검색 결과를 그대로 씁니다. "
                                    "웹에서 `SearchWord=이름` 검색할 때와 같이 **동명이인이 섞일 수 있습니다.**"
                                )
                                for b in api_list:
                                    _append_catalog_book(b, seen_isbn, raw_list)
                                st.markdown(
                                    f"**역자 검색 원본**: {len(raw_list)}권 "
                                    f"(Query=`{tr['name']}`, API ItemSearch)"
                                )

                            aid_for_filter = (
                                int(author_id) if author_id is not None else None
                            )

                            def _counts_for_analysis(books: List[dict]) -> int:
                                return sum(
                                    1
                                    for b in books
                                    if include_in_career_analysis(
                                        b,
                                        tr["name"],
                                        aid_for_filter,
                                        target_cat,
                                        use_category_filter,
                                    )
                                )

                            n_role_raw = _counts_for_analysis(raw_list)
                            work_list: List[dict] = list(raw_list)
                            if (
                                enrich_missing_authors
                                and n_role_raw < max(1, len(raw_list)) * 0.3
                                and not any(
                                    b.get("_webAuthorSearchTrusted") for b in raw_list
                                )
                            ):
                                with st.spinner("ISBN LookUp으로 authors 보강(제한)…"):
                                    work_list = enrich_catalog_with_authors_lookup(
                                        raw_list, ttb_key, tr["name"], max_lookups=25
                                    )
                                st.caption(
                                    f"보강 후 분석 대상 후보: **{_counts_for_analysis(work_list)}**권 "
                                    f"(원본 대비 역자 메타 적을 때만 보강)"
                                )

                            filtered = [
                                b
                                for b in work_list
                                if include_in_career_analysis(
                                    b,
                                    tr["name"],
                                    aid_for_filter,
                                    target_cat,
                                    use_category_filter,
                                )
                            ]
                            n_web_trusted = sum(
                                1 for b in work_list if b.get("_webAuthorSearchTrusted")
                            )
                            if use_category_filter and len(filtered) == 0 and work_list:
                                st.warning(
                                    "카테고리 필터를 적용하면 분석 대상이 0권이 되어, "
                                    "필터를 임시 해제하고 분석을 진행합니다. "
                                    "(동명이인 데이터가 섞여 있을 수 있습니다.)"
                                )
                                filtered = [
                                    b
                                    for b in work_list
                                    if include_in_career_analysis(
                                        b,
                                        tr["name"],
                                        aid_for_filter,
                                        target_cat,
                                        False,
                                    )
                                ]

                            n_role_work = _counts_for_analysis(work_list)
                            st.caption(
                                f"분석 포함: **{n_role_work}**권 / 작업 목록 **{len(work_list)}**권 "
                                f"(웹 AuthorSearch 신뢰 **{n_web_trusted}**권) → "
                                f"최종 **{len(filtered)}**권"
                            )

                            counts, detail_rows = weighted_hint_counts(filtered)
                            st.markdown("**커리어 언어·원제 힌트**")
                            st.json(dict(sorted(counts.items(), key=lambda x: -x[1])))

                            with st.expander("필터 통과 도서 요약"):
                                st.dataframe(
                                    [
                                        {
                                            "title": r["title"],
                                            "publisher": r.get("publisher"),
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
