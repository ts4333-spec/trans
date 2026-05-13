import streamlit as st
import requests
import re

def get_translator_by_isbn(isbn, ttbkey):
    url = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
    isbn = isbn.replace("-", "").strip()
    item_id_type = "ISBN13" if len(isbn) == 13 else "ISBN"

    params = {
        "ttbkey": ttbkey,
        "itemIdType": item_id_type,
        "ItemId": isbn,
        "output": "js",
        "Version": "20131101",
        "OptResult": "authors"
    }
    
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        
        if "errorCode" in data:
            return None, None, f"알라딘 API 에러: {data.get('errorMessage')}"
            
        items = data.get("item", [])
        if not items:
            return None, None, "검색 결과가 없습니다. (ISBN을 다시 확인해주세요)"
            
        item = items[0]
        translators = []
        author_str = item.get("author", "저자 정보 없음")
        
        # 1순위: 구조화된 배열에서 추출
        authors_list = item.get("subInfo", {}).get("authors", [])
        if authors_list:
            for auth in authors_list:
                role = auth.get("authorTypeDesc", "") or auth.get("authorTypeName", "")
                if any(k in role for k in ["옮긴이", "역자", "역", "옮김"]):
                    name = auth.get("authorName", "").strip()
                    if name:
                        translators.append(name)
                        
        # 2순위: 문자열에서 추출
        if not translators and author_str != "저자 정보 없음":
            parts = author_str.split(',')
            for part in parts:
                if any(k in part for k in ["옮긴이", "역자", "옮김"]):
                    name = re.sub(r"\(.*?\)|옮긴이|역자|옮김|지은이|지음", "", part).strip()
                    if name:
                        translators.append(name)
                        
        return translators, author_str, None
    except Exception as e:
        return None, None, f"파이썬 통신 에러: {e}"

# ==========================================
# 🎨 스트림릿 UI (폼 적용)
# ==========================================

st.title("📚 알라딘 번역자 검색기 (안정화 버전)")

# ⚠️ 주의: 아래 변수에 본인의 TTB 키를 직접 입력하세요!
TTB_KEY = "여기에_본인의_알라딘_TTB키를_넣으세요"

# 🔥 핵심 해결책: st.form으로 입력과 버튼을 묶어서 데이터 증발 방지
with st.form("search_form"):
    isbn_input = st.text_input("ISBN 입력", placeholder="예: 9788925588735")
    # 일반 button 대신 form_submit_button 사용
    submitted = st.form_submit_button("번역자 찾기 🚀")

# 버튼이 눌렸을 때만 아래 로직 실행
if submitted:
    # 버튼이 제대로 눌렸는지 화면에 즉시 표시
    st.info("✅ 버튼 클릭이 인식되었습니다. 알라딘에서 데이터를 가져옵니다...") 
    
    if not isbn_input:
        st.warning("ISBN을 입력해주세요!")
    elif TTB_KEY == "여기에_본인의_알라딘_TTB키를_넣으세요":
        st.error("코드 안의 TTB_KEY를 본인의 발급 키로 변경해주세요!")
    else:
        with st.spinner("API 통신 중..."):
            translators, raw_author, error_msg = get_translator_by_isbn(isbn_input, TTB_KEY)
            
        if error_msg:
            st.error(error_msg)
        elif translators:
            st.success(f"🎉 찾은 번역자: **{', '.join(translators)}**")
            with st.expander("원본 저자 정보 보기"):
                st.code(raw_author)
        else:
            st.warning("해당 ISBN에서 번역자 정보를 찾을 수 없습니다.")
            st.info("알라딘에서 내려준 원본 데이터:")
            st.code(raw_author)
