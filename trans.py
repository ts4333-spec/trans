import streamlit as st
import re

def extract_translator_from_text(author_str: str) -> list:
    """
    알라딘에서 내려주는 지저분한 저자 문자열에서 번역가 이름만 깔끔하게 추출합니다.
    """
    translators = []
    
    if not author_str:
        return translators

    # 예: "앤디 위어 (지은이), 강동혁 (옮긴이)" -> 쉼표(,) 기준으로 분리
    parts = author_str.split(',')
    
    for part in parts:
        # 분리된 조각 안에 번역가를 뜻하는 단어가 있는지 확인
        if any(keyword in part for keyword in ["옮긴이", "역자", "옮김", "역"]):
            # 괄호 안의 내용이나 불필요한 단어들을 정규식으로 싹 지움
            name = re.sub(r"\(.*?\)|옮긴이|역자|옮김|지은이|지음|역", "", part).strip()
            if name:
                translators.append(name)
                
    return translators

# ==========================================
# 🎨 스트림릿 UI (API 통신 없음, 오프라인 모드)
# ==========================================

st.set_page_config(page_title="번역가 추출 테스트", page_icon="✂️")

st.title("✂️ 번역가 이름만 쏙! 추출기")
st.write("알라딘 API 차단과 상관없이, **텍스트 추출 로직**만 단독으로 테스트하는 화면입니다.")

# 테스트용 예시 데이터 제공
st.info("💡 **테스트해볼 수 있는 텍스트 예시**\n"
        "- 앤디 위어 (지은이), 강동혁 (옮긴이)\n"
        "- 마이클 샌델 지음, 이창신 옮김\n"
        "- 무라카미 하루키 (지은이), 양억관, 김난주 (옮긴이)")

# 사용자 입력 받기
sample_text = st.text_input("알라딘 저자 텍스트 입력:", placeholder="예: 앤디 위어 (지은이), 강동혁 (옮긴이)")

if st.button("번역가 추출하기 🚀"):
    if not sample_text:
        st.warning("텍스트를 입력해 주세요.")
    else:
        # API 통신 없이 순수하게 파이썬 함수만 실행
        result = extract_translator_from_text(sample_text)
        
        if result:
            st.success(f"🎉 추출 성공! 찾은 번역가: **{', '.join(result)}**")
        else:
            st.error("입력하신 텍스트에서는 번역가를 찾을 수 없습니다.")
