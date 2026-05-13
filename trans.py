import streamlit as st
import requests
import re

st.title("알라딘 번역가 검색 (진짜 최종)")

with st.form("api_form"):
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
            
            if "errorCode" in data:
                st.error(f"API 에러: {data.get('errorMessage')}")
            else:
                items = data.get("item", [])
                if not items:
                    st.warning("해당 ISBN의 도서 정보가 없습니다.")
                else:
                    item = items[0]
                    translators = []
                    
                    # 💡 만약 실패할 경우를 대비해 원본 데이터를 백업
                    raw_author = item.get("author", "원본 데이터 없음")
                    
                    # 1순위: 깔끔하게 정리된 배열 데이터에서 추출
                    authors_list = item.get("subInfo", {}).get("authors", [])
                    for auth in authors_list:
                        role = auth.get("authorTypeDesc", "") or auth.get("authorTypeName", "")
                        if any(k in role for k in ["옮긴이", "역자", "역", "옮김"]):
                            name = auth.get("authorName", "").strip()
                            if name:
                                translators.append(name)
                                
                    # 2순위: 1순위에서 못 찾았을 경우 (제가 아까 빼먹은 부분!)
                    # 원본 문자열을 쉼표로 쪼개서 억지로라도 찾아냅니다.
                    if not translators and raw_author != "원본 데이터 없음":
                        for part in raw_author.split(','):
                            if any(k in part for k in ["옮긴이", "역자", "옮김", "역"]):
                                # 정규식으로 괄호와 불필요한 단어 싹둑
                                name = re.sub(r"\(.*?\)|옮긴이|역자|옮김|지은이|지음|역", "", part).strip()
                                if name:
                                    translators.append(name)
                                    
                    # 최종 결과 출력
                    if translators:
                        st.success(f"🎉 찾은 번역가: **{', '.join(translators)}**")
                    else:
                        st.warning("이 책에는 번역가 정보가 없습니다.")
                        st.info(f"알라딘이 던져준 원본 텍스트: {raw_author}")
                        
        except Exception as e:
            st.error(f"통신 에러: {str(e)}")
