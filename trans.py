import streamlit as st
import requests
import re

def get_translator_by_isbn(isbn, ttbkey):
    """
    ISBN을 입력받아 알라딘 API에서 번역자(옮긴이) 명단을 리스트로 반환합니다.
    """
    url = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
    params = {
        "ttbkey": ttbkey,
        "itemIdType": "ISBN",
        "ItemId": isbn,
        "output": "js",
        "Version": "20131101",
        "OptResult": "authors" # 저자/역자 상세 정보 포함 필수
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        items = data.get("item", [])
        if not items:
            return []
            
        item = items[0]
        translators = []
        
        # 1순위: subInfo의 구조화된 데이터 활용
        sub_info = item.get("subInfo", {})
        authors_list = sub_info.get("authors", [])
        
        if authors_list:
            for auth in authors_list:
                role = auth.get("authorTypeName", "")
                if "옮긴이" in role or "역자" in role or "역" == role:
                    name = auth.get("authorName", "").strip()
                    if name:
                        translators.append(name)
        
        # 2순위: 구조화된 데이터가 없을 경우 전체 author 문자열에서 파싱 (Fallback)
        if not translators:
            author_str = item.get("author", "")
            matches = re.findall(r"([^,]+)\s*\((?:옮긴이|역자|역)\)", author_str)
            translators = [m.strip() for m in matches]
            
        return translators

    except Exception as e:
        # 오류 메시지도 스트림릿 화면에 띄우도록 변경
        st.error(f"API 호출 오류: {e}") 
        return []

# ==========================================
# 🎨 여기서부터 스트림릿 UI 화면 구성입니다
# ==========================================

st.title("📚 알라딘 번역자 검색기")
st.write("ISBN을 입력하면 알라딘 API에서 번역자 정보를 찾아옵니다.")

# ⚠️ 주의: 아래 변수에 본인의 TTB 키를 직접 입력하세요!
TTB_KEY = "여기에_본인의_알라딘_TTB키를_넣으세요"

# 사용자로부터 ISBN 입력받기
isbn_input = st.text_input("ISBN 입력", placeholder="예: 9791160261479")

# 검색 버튼 만들기
if st.button("번역자 찾기"):
    if not isbn_input:
        st.warning("ISBN을 입력해주세요!")
    elif TTB_KEY == "여기에_본인의_알라딘_TTB키를_넣으세요":
        st.error("코드 안에 TTB_KEY를 본인의 발급 키로 변경해주세요!")
    else:
        # 로딩 스피너 보여주기
        with st.spinner("알라딘 API에서 데이터를 가져오는 중..."):
            result = get_translator_by_isbn(isbn_input, TTB_KEY)
            
        # 결과 화면에 출력하기
        if result:
            st.success(f"찾은 번역자: **{', '.join(result)}**")
        else:
            st.info("해당 ISBN의 번역자 정보를 찾을 수 없거나, 번역서가 아닙니다.")
