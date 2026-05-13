import streamlit as st
import requests
import re

def get_translator_by_isbn(isbn, ttbkey):
    """
    ISBN을 입력받아 알라딘 API에서 번역자 명단, 원본 저자 문자열, 에러 메시지를 반환합니다.
    """
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
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        # 🚨 1. 알라딘 API 자체 에러 체크 (TTB 키 미등록 등)
        if "errorCode" in data:
            return None, None, f"알라딘 API 에러 [{data.get('errorCode')}]: {data.get('errorMessage')}"
            
        items = data.get("item", [])
        if not items:
            return None, None, "검색 결과가 없습니다. 알라딘에 등록되지 않은 ISBN일 수 있습니다."
            
        item = items[0]
        translators = []
        author_str = item.get("author", "저자 정보 없음")
        
        # 🔍 2. 1순위: subInfo의 구조화된 데이터 활용
        sub_info = item.get("subInfo", {})
        authors_list = sub_info.get("authors", [])
        
        if authors_list:
            for auth in authors_list:
                # 알라딘 API 버전에 따라 필드명이 다를 수 있어 꼼꼼히 체크
                role = auth.get("authorTypeDesc", "") or auth.get("authorTypeName", "")
                if "옮긴이" in role or "역자" in role or "역" in role or "옮김" in role:
                    name = auth.get("authorName", "").strip()
                    if name:
                        translators.append(name)
        
        # 🔍 3. 2순위: 괄호 파싱이 실패할 경우를 대비한 강력한 Fallback
        if not translators and author_str != "저자 정보 없음":
            # 예: "마이클 샌델 지음, 이창신 옮김" 쉼표 단위로 쪼갬
            parts = author_str.split(',')
            for part in parts:
                if "옮긴이" in part or "역자" in part or "옮김" in part:
                    # '옮긴이', '지은이' 등 역할 단어와 특수기호 싹 지우고 이름만 추출
                    name = re.sub(r"\(.*?\)|옮긴이|역자|옮김|지은이|지음", "", part).strip()
                    if name:
                        translators.append(name)
                        
        return translators, author_str, None

    except Exception as e:
        return None, None, f"통신 오류 발생: {e}"

# ==========================================
# 🎨 스트림릿 UI 
# ==========================================

st.title("📚 알라딘 번역자 검색기 (Pro)")
st.write("ISBN을 입력하면 알라딘 API에서 번역자 정보를 찾아옵니다.")

# ⚠️ 주의: 아래 변수에 본인의 TTB 키를 직접 입력하세요!
TTB_KEY = "여기에_본인의_알라딘_TTB키를_넣으세요"

isbn_input = st.text_input("ISBN 입력", placeholder="예: 9791160261479")

if st.button("번역자 찾기"):
    if not isbn_input:
        st.warning("ISBN을 입력해주세요!")
    elif TTB_KEY == "여기에_본인의_알라딘_TTB키를_넣으세요":
        st.error("코드 안에 TTB_KEY를 본인의 발급 키로 변경해주세요!")
    else:
        with st.spinner("알라딘 API에서 데이터를 분석 중입니다..."):
            translators, raw_author, error_msg = get_translator_by_isbn(isbn_input, TTB_KEY)
            
        # 1. API 에러가 났을 때
        if error_msg:
            st.error(error_msg)
