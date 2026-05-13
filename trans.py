import requests

def get_translator_by_isbn(isbn, ttbkey):
    """
    ISBN을 입력받아 알라딘 API에서 번역자(옮긴이) 명단을 리스트로 반환합니다.
    """
    url = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
    params = {
        "ttbkey": ttbkey,
        "itemIdType": "ISBN", # 또는 ISBN13
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
                # 역할이 '옮긴이' 또는 '역자'인 경우만 수집
                if "옮긴이" in role or "역자" in role or "역" == role:
                    name = auth.get("authorName", "").strip()
                    if name:
                        translators.append(name)
        
        # 2순위: 구조화된 데이터가 없을 경우 전체 author 문자열에서 파싱 (Fallback)
        if not translators:
            author_str = item.get("author", "")
            # 예: "마이클 샌델 (지은이), 이창신 (옮긴이)" 형태 분석
            import re
            # 괄호 안에 '옮긴이', '역자', '역' 등이 들어간 패턴 찾기
            matches = re.findall(r"([^,]+)\s*\((?:옮긴이|역자|역)\)", author_str)
            translators = [m.strip() for m in matches]
            
        return translators

    except Exception as e:
        print(f"API 호출 오류: {e}")
        return []

# 사용 예시
# TTB_KEY = "내_알라딘_TTB_키"
# isbn_input = "9791160261479"
# result = get_translator_by_isbn(isbn_input, TTB_KEY)
# print(f"번역자 명단: {result}")