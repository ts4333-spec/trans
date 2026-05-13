import streamlit as st
import requests

st.title("알라딘 번역가 검색")

with st.form("api_form"):
    # 변수 하드코딩 오류를 막기 위해 UI에서 직접 키를 입력받습니다.
    ttb_key = st.text_input("알라딘 TTB 키", type="password")
    isbn = st.text_input("ISBN 입력 (예: 9788925588735)")
    submit = st.form_submit_button("번역가 검색")

if submit:
    if not ttb_key or not isbn:
        st.error("TTB 키와 ISBN을 모두 입력해주세요.")
    else:
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
            data = res.json()
            
            # API 에러 확인
            if "errorCode" in data:
                st.error(f"API 에러: {data.get('errorMessage')}")
            else:
                items = data.get("item", [])
                if not items:
                    st.warning("해당 ISBN의 도서 정보가 없습니다.")
                else:
                    item = items[0]
                    translators = []
                    
                    # subInfo에서 번역가 추출
                    authors_list = item.get("subInfo", {}).get("authors", [])
                    for auth in authors_list:
                        role = auth.get("authorTypeDesc", "") or auth.get("authorTypeName", "")
                        if "옮긴이" in role or "역자" in role or "역" in role or "옮김" in role:
                            name = auth.get("authorName", "").strip()
                            if name:
                                translators.append(name)
                                
                    if translators:
                        st.success(f"번역가: {', '.join(translators)}")
                    else:
                        st.warning("이 책에는 번역가 정보가 없습니다.")
                        
        except Exception as e:
            st.error(f"통신 에러: {str(e)}")
