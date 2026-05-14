import requests
import re
from collections import Counter
import streamlit as st

# ==========================================
# [1단계: 수집] 도서 정보 및 역자 프로필 확보
# ==========================================
def step1_collection(ttb_key: str, isbn: str) -> dict:
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
    translators = []
    
    # 1순위: 구조화된 배열 데이터에서 추출
    authors_list = book_item.get("subInfo", {}).get("authors", [])
    for auth in authors_list:
        role = auth.get("authorTypeDesc", "") or auth.get("authorTypeName", "")
        if any(keyword in role for keyword in ["옮긴이", "역자", "옮김"]):
            translators.append({
                "authorId": auth.get("authorId"), 
                "name": auth.get("authorName", "").strip(),
                "authorInfo": auth.get("authorInfo", ""), # 👈 소개글 텍스트 저장
                "extracted_via": "structured_data"
            })
            
    # 2순위: 1순위 실패 시 정규식을 활용한 Fallback 로직
    if not translators:
        raw_author = book_item.get("author", "")
        for part in re.split(r'[,·/]', raw_author):
            if any(k in part for k in ["옮긴이", "역자", "옮김"]):
                clean_name = re.sub(r"\(.*?\)|옮긴이|역자|옮김|지은이|지음", "", part).strip()
                if clean_name:
                    translators.append({
                        "authorId": None,
                        "name": clean_name,
                        "authorInfo": "",
                        "extracted_via": "regex"
                    })
    return translators

# ==========================================
# [2.5단계: 전공 분석] 소개글에서 언어 힌트 추출
# ==========================================
def step2_5_profile_analysis(author_info_text: str) -> dict:
    if not author_info_text:
        return {"univ": None, "major": None, "lang_hint": None}

    univ_pattern = r"([가-힣]+대학교|[가-힣]+대)"
    major_pattern = r"([가-힣]+학과|[가-힣]+전공|[가-힣]+문학)"
    
    univ_match = re.search(univ_pattern, author_info_text)
    major_match = re.search(major_pattern, author_info_text)
    
    university = univ_match.group() if univ_match else None
    major = major_match.group() if major_match else None
    
    lang_hint = None
    if major:
        if "독어" in major or "독문" in major: lang_hint = "German"
        elif "불어" in major or "불문" in major: lang_hint = "French"
        elif "일어" in major or "일문" in major: lang_hint = "Japanese"
        elif "영어" in major or "영문" in major: lang_hint = "English"

    return {"university": university, "major": major, "lang_hint": lang_hint}

# ==========================================
# [3단계: 검증] 과거 도서 호출 및 동명이인 필터링
# ==========================================
def step3_verification(ttb_key: str, translator_name: str, target_category: str) -> list:
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
            if main_category in book_cat:
                filtered_books.append(book)
        return filtered_books
    except Exception as e:
        st.warning(f"[검증 단계 에러] {str(e)}")
        return []

# ==========================================
# [4단계: 분석] 언어 힌트 통계 산출
# ==========================================
def step4_analysis(filtered_books: list, profile_lang_hint: str) -> Counter:
    lang_counter = Counter()
    
    # 전공 언어 힌트가 있으면 강력한 가산점 부여
    if profile_lang_hint:
        lang_counter[profile_lang_hint] += 5
        
    for book in filtered_books:
        cat = book.get("categoryName", "")
        title = book.get("title", "")
        
        if any(k in cat for k in ["영미", "미국", "영국"]): lang_counter["English"] += 2
        elif "일본" in cat: lang_counter["Japanese"] += 2
        elif "프랑스" in cat: lang_counter["French"] += 2
        elif "독일" in cat: lang_counter["German"] += 2
            
        if re.search(r'[a-zA-Z]', title):
            lang_counter["English"] += 1
            
    return lang_counter

# ==========================================
# [5단계: 확정] 가중치 기반 최종 판단
# ==========================================
def step5_confirmation(lang_stats: Counter, translator_info: dict, filtered_count: int) -> dict:
    if not lang_stats or filtered_count == 0:
        return {
            "language": "Unknown", 
            "confidence": "Low", 
            "reason": "비교 가능한 과거 데이터가 부족합니다.",
            "stats": {}
        }
        
    most_common_lang, score = lang_stats.most_common(1)[0]
    
    confidence = "Low"
    if translator_info.get("authorId") and score >= 5:
        confidence = "High"
    elif score >= 3:
        confidence = "Medium"
        
    return {
        "language": most_common_lang,
        "confidence": confidence,
        "reason": f"관련 도서 {filtered_count}권 분석 및 전공 데이터 기반 (스코어: {score}점)",
        "stats": dict(lang_stats)
    }

# ==========================================
# [Streamlit UI] 메인 실행부
# ==========================================
st.title("📚 번역서 원천 언어 추적 엔진")

with st.form("pipeline_form"):
    ttb_key = st.text_input("알라딘 TTB 키", type="password")
    isbn = st.text_input("ISBN 입력 (예: 9788925588735)")
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
                target_translator = translators[0]
                st.info(f"👤 식별된 역자: {target_translator['name']} (방식: {target_translator['extracted_via']})")
                
                # Step 2.5: 프로필 분석
                profile_info = step2_5_profile_analysis(target_translator.get("authorInfo", ""))
                if profile_info['major']:
                    st.success(f"🎓 학력 확인: {profile_info['university']} {profile_info['major']}")
                
                # Step 3: 검증
                target_cat = target_book.get("categoryName", "")
                filtered_books = step3_verification(ttb_key, target_translator['name'], target_cat)
                
                # Step 4: 분석
                lang_stats = step4_analysis(filtered_books, profile_info['lang_hint'])
                
                # Step 5: 확정
                result = step5_confirmation(lang_stats, target_translator, len(filtered_books))
                
                # 최종 결과 출력
                st.divider()
                st.subheader(f"🎯 최종 예측 언어: {result['language']}")
                st.write(f"- **신뢰도:** {result['confidence']}")
                st.write(f"- **판단 근거:** {result['reason']}")
                
                # 에러 안 나는 안전한 expander
                with st.expander("세부 통계 보기"):
                    st.json(result.get("stats", {}))
