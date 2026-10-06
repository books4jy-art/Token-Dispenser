"""The default shop items, loaded into a server with `/상품 기본목록`.

Each item is a service an admin carries out on the buyer's save, so every item asks
for the game's 기종변경 code and 인증번호 (form ""), and some also ask for details
(form "<label>"). Items with a `unit` are priced per unit and the buyer picks how
many units (up to `max_qty`). Prices are in won. Change anything here, or later in
Discord with `/상품 수정`.
"""

CODES = ""  # order form: game codes only


def _item(category, name, price, unit="", max_qty=1, form=CODES, **extra):
    return {"category": category, "name": name, "price": price, "unit": unit,
            "max_qty": max_qty, "form": form, "kind": "manual", **extra}


CATALOG = [
    # 통조림
    _item("통조림", "통조림 45,000개", 200),
    _item("통조림", "통조림 50만개", 500, unit="50만개", max_qty=20, sale_start=4, sale_end=12,
          description="04:00~12:00에만 판매해요."),
    # 티켓
    _item("티켓", "레어 티켓 299개", 800),
    _item("티켓", "냥코 티켓 2,999개", 800),
    _item("티켓", "플래티넘 티켓", 100, unit="1개", max_qty=99),
    _item("티켓", "레전드 티켓", 200, unit="1개", max_qty=99),
    # 업그레이드 재화
    _item("업그레이드 재화", "NP 9,999개", 500),
    _item("업그레이드 재화", "열매 (모든 종류)", 200, unit="200개", max_qty=50),
    _item("업그레이드 재화", "수석 (모든 종류)", 200, unit="200개", max_qty=50),
    _item("업그레이드 재화", "XP", 300, unit="1억", max_qty=99),
    _item("업그레이드 재화", "모든 갯츠아이 9,999개", 2000),
    # 스테이지
    _item("스테이지", "세계편 올클리어", 1000),
    _item("스테이지", "미래편 올클리어", 1000),
    _item("스테이지", "우주편 올클리어", 1000),
    _item("스테이지", "보물작", 1500, unit="1장", max_qty=9, form="보물작할 장 (예: 세계편 1장, 미래편 2장)"),
    # 캐릭터
    _item("캐릭터", "모든 캐릭터 획득", 3000),
    _item("캐릭터", "모든 캐릭터 강화", 3000),
    _item("캐릭터", "모든 캐릭터 본능", 3000),
    _item("캐릭터", "원하는 캐릭터 추가", 800, unit="1마리", max_qty=50, form="추가할 캐릭터 이름 (여러 마리면 쉼표로)"),
    _item("캐릭터", "원하는 캐릭터 삭제", 800, unit="1마리", max_qty=50, form="삭제할 캐릭터 이름 (여러 마리면 쉼표로)"),
    _item("캐릭터", "원하는 캐릭터 강화", 500, unit="1마리", max_qty=50, form="강화할 캐릭터 이름과 원하는 레벨"),
    _item("캐릭터", "원하는 캐릭터 진화", 500, unit="1마리", max_qty=50, form="진화할 캐릭터 이름 (여러 마리면 쉼표로)"),
    _item("캐릭터", "원하는 캐릭터 본능", 800, unit="1마리", max_qty=50, form="본능을 열 캐릭터 이름 (여러 마리면 쉼표로)"),
    # 전투 아이템
    _item("전투 아이템", "모든 배틀 아이템 9,999개", 2000),
    _item("전투 아이템", "모든 배틀 아이템 무제한", 2000),
    _item("전투 아이템", "리더십", 100, unit="100개", max_qty=99),
    # 가마토토
    _item("가마토토", "가마토토 레벨 130 (MAX)", 500),
    _item("가마토토", "가마토토 대원", 200, unit="1마리", max_qty=10),
    # 골드회원
    _item("골드회원", "골드회원 30일", 300),
]
