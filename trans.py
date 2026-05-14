import requests
import re
from collections import Counter
import streamlit as st

# ==========================================
# [1단계: 수집] 도서 정보 및 역자 프로필 확보
# ==========================================
def step1_collection(ttb_key: str, isbn: str) -> dict:
    """
    [CoT 추론 과정]
    1. 사용자가 입력한 ISBN의 하이픈과 공백을 제거하여 정규화한다.
    2. 길이에 따라 ISBN(10자리) 또는 ISBN13으로 파라미터를 동적 할당한다.
    3. 가장 중요한 OptResult=authors 파라미터를 추가하여 도서 정보와 함께 역자 프로필(ID 포함)을 한 번에 가져온다.
    """
    isbn_clean = isbn.replace("-", "").strip()
    url = "http://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
    params = {
        "ttbkey": ttb_key.strip(),
        "itemIdType": "ISBN13" if len(isbn_clean) == 13 else "ISBN",
        "ItemId": isbn_clean,
        "output": "js",
        "Version": "20131101",
        "OptResult": "authors"
    }
    
    try:
        res = requests.get(url, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        if "errorCode" in data:
            raise ValueError(f"API 에러: {data.get('errorMessage')}")
        
        items = data.get("item", [])
        return items[0] if items else None
    except Exception as e:
        st.error(f"[수집 단계 실패] {str(e)}")
        return None


# ==========================================
# [2단계: 식별] 역자 AuthorID 및 이름 추출
# ==========================================
def step2_identification(book_item: dict) -> list:
    """
    [CoT 추론 과정]
    1. 응답 데이터의 subInfo.authors 배열을 순회하며 '역자' 역할을 찾는다.
    2. 역자를 찾으면 가장 확실한 식별자인 authorId와 authorName을 딕셔너리로 저장한다. (1순위)
    3. 만약 subInfo에서 역자를 찾지 못했다면, raw_author 문자열을 쉼표로 분리하여 정규식으로 역자 이름을 유추한다. (2순위 Fallback)
    """
    translators = []
    
    # 1순위: 구조화된 배열 데이터에서 안전하게 추출 (AuthorID 확보 목적)
    authors_list = book_item.get("subInfo", {}).get("authors", [])
    for auth in authors_list:
        role = auth.get("authorTypeDesc", "") or auth.get("authorTypeName", "")
        if any(keyword in role for keyword in ["옮긴이", "역자", "옮김"]):
            translators.append({
                "authorId": auth.get("authorId"), 
                "name": auth.get("authorName", "").strip(),
                "extracted_via": "structured_data"
            })
            
    # 2순위: 1순위 실패 시 정규식을 활용한 Fallback 로직
    if not translators:
        raw_author = book_item.get("author", "")
        for part in re.split(r'[,·/]', raw_author): # 쉼표, 가운뎃점, 슬래시 고려
            if any(k in part for k in ["옮긴이", "역자", "옮김"]):
                # 괄호 내용 및 불필요 키워드 제거
                clean_name = re.sub(r"\(.*?\)|옮긴이|역자|옮김|지은이|지음", "", part).strip()
                if clean_name:
                    translators.append({
                        "authorId": None, # 문자열 추출이므로 ID는 알 수 없음
                        "name": clean_name,
                        "extracted_via": "regex"
                    })
                    
    return translators


# ==========================================
# [3단계: 검증] 과거 도서 호출 및 동명이인 필터링
# ==========================================
def step3_verification(ttb_key: str, translator_name: str, target_category: str) -> list:
    """
    [CoT 추론 과정]
    1. 타겟 도서의 categoryName에서 대분류(예: 'IT 모바일', '경제경영')를 추출한다.
    2. ItemSearch API로 역자 이름 기반 최근 도서 50권을 조회한다.
    3. 검색된 도서 중, 타겟 도서의 대분류와 일치하는 도서만 남긴다. 
       -> 다른 분야를 번역한 동명이인의 데이터를 배제하기 위함 (Context-Aware Filtering).
    """
    # 타겟 도서의 대분류 추출 (예: "국내도서>IT 모바일>웹/컴퓨터" -> "IT 모바일")
    cat_parts = target_category.split('>')
    main_category = cat_parts[1].strip() if len(cat_parts) > 1 else target_category

    url = "http://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
    params = {
        "ttbkey": ttb_key.strip(),
        "Query": translator_name,
        "QueryType": "Author",
        "MaxResults": 50,
        "SearchTarget": "Book",
        "output": "js",
        "Version": "20131101"
    }
    
    try:
        res = requests.get(url, params=params, timeout=10)
        items = res.json().get("item", [])
        
        filtered_books = []
        for book in items:
            book_cat = book.get("categoryName", "")
            # 대분류 카테고리가 일치하는 경우만 필터링 (동명이인 방어)
            if main_category in book_cat:
                filtered_books.append(book)
                
        return filtered_books
    except Exception as e:
        st.warning(f"[검증 단계 에러] {str(e)}")
        return []


# ==========================================
# [4단계: 분석] 언어 힌트 통계 산출
# ==========================================
def step4_analysis(filtered_books: list) -> Counter:
    """
    [CoT 추론 과정]
    1. 필터링된 도서 목록을 순회하며 카테고리명과 도서명을 분석한다.
    2. 카테고리에 명시적인 국가 키워드(영미, 일본 등)가 있으면 해당 언어 카운트를 올린다.
    3. 비문학의 경우 카테고리에 국가가 없는 경우가 많으므로, 제목에 영문 알파벳이 포함되어 있는지 정규식으로 검사하여 '영어' 가중치를 올린다.
    4. 분석 결과를 collections.Counter 형태로 반환하여 통계를 낸다.
    """
    lang_counter = Counter()
    
    for book in filtered_books:
        cat = book.get("categoryName", "")
        title = book.get("title", "")
        
        # 1. 카테고리 기반 언어 유추
        if any(k in cat for k in ["영미", "미국", "영국"]):
            lang_counter["English"] += 2
        elif "일본" in cat:
            lang_counter["Japanese"] += 2
        elif "프랑스" in cat:
            lang_counter["French"] += 2
        elif "독일" in cat:
            lang_counter["German"] += 2
            
        # 2. 제목의 원제(알파벳) 기반 유추 (비문학 IT/경제 특화)
        if re.search(r'[a-zA-Z]', title):
            lang_counter["English"] += 1
            
    return lang_counter


# ==========================================
# [5단계: 확정] 가중치 기반 최종 판단
# ==========================================
def step5_confirmation(lang_stats: Counter, translator_info: dict, filtered_count: int) -> dict:
    """
    [CoT 추론 과정]
    1. 필터링된 데이터가 없으면(콜드 스타트) 판단 불가로 처리한다.
    2. 가장 빈도가 높은 언어를 추출한다.
    3. AuthorID 존재 여부, 필터링된 도서 수 등을 종합하여 결과의 '신뢰도(Confidence)'를 측정한다.
    """
    if not lang_stats or filtered_count == 0:
        return {"language": "Unknown", "confidence": "Low", "reason": "비교 가능한 과거 데이터 없음"}
        
    most_common_lang, score = lang_stats.most_common(1)[0]
    
    # 신뢰도 평가 로직
    confidence = "Low"
    if translator_info.get("authorId") and score >= 5:
        confidence = "High"
    elif score >= 3:
        confidence = "Medium"
        
    return {
        "language": most_common_lang,
        "confidence": confidence,
        "reason": f"관련 도서 {filtered_count}권 중 힌트 스코어 {score}점 획득",
        "stats": dict(lang_stats)
    }


# ==========================================
# [Streamlit UI] 메인 실행부
# ==========================================
st.title("📚 번역서 원천 언어 추적 엔진")

with st.form("pipeline_form"):
    ttb_key = st.text_input("알라딘 TTB 키", type="password")
    isbn = st.text_input("ISBN 입력")
    submit = st.form_submit_button("추적 시작")

if submit and ttb_key and isbn:
    with st.spinner("파이프라인 가동 중..."):
        # Step 1: 수집
        target_book = step1_collection(ttb_key, isbn)
        
        if target_book:
            st.write(f"**대상 도서:** {target_book.get('title')}")
            
            # Step 2: 식별
            translators = step2_identification(target_book)
            if not translators:
                st.warning("역자 정보를 식별할 수 없습니다.")
            else:
                target_translator = translators[0] # 첫 번째 역자 기준
                st.info(f"👤 식별된 역자: {target_translator['name']} (방식: {target_translator['extracted_via']})")
                
                # Step 3: 검증
                target_cat = target_book.get("categoryName", "")
                filtered_books = step3_verification(ttb_key, target_translator['name'], target_cat)
                
                # Step 4: 분석
                lang_stats = step4_analysis(filtered_books)
                
                # Step 5: 확정
                result = step5_confirmation(lang_stats, target_translator, len(filtered_books))
                
                st.success(f"🎯 **최종 예측 언어:** {result['language']}")
                st.write(f"- 신뢰도: {result['confidence']}")
                st.write(f"- 판단 근거: {result['reason']}")
                with st.expander("세부 통계 보기"):
                    st.json(result.get("stats", {}))
