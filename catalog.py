"""The default shop items, loaded into a server with `/상품 기본목록`.

`auto` links an item to fulfil.AUTO: the bot edits the save itself right after the
purchase. Items without `auto` are done by an admin by hand. Running `/상품 기본목록`
again updates these settings on items that already exist (prices are kept).

Every item asks for the game's 기종변경 code and 인증번호 (form ""), and some also ask
for details (form "<label>"). Items with a `unit` are priced per unit and the buyer picks how
many units (up to `max_qty`). Prices are in won. Change anything here, or later in
Discord with `/상품 수정`.
"""

CODES = ""  # order form: game codes only


def _item(category, name, price, unit="", max_qty=1, form=CODES, **extra):
    return {"category": category, "name": name, "price": price, "unit": unit,
            "max_qty": max_qty, "form": form, "kind": "manual", **extra}


CATALOG = [
    # 통조림
    _item("통조림", "통조림 45,000개", 200, auto="catfood_45000"),
    # The game caps cat food at 45,000, so the editor can't do this one: an admin handles it.
    _item("통조림", "통조림 50만개", 500, unit="50만개", max_qty=20, sale_start=4, sale_end=12,
          description="04:00~12:00에만 판매해요."),
    # 티켓
    _item("티켓", "레어 티켓 299개", 800, auto="rare_tickets_299"),
    _item("티켓", "냥코 티켓 2,999개", 800, auto="normal_tickets_2999"),
    _item("티켓", "플래티넘 티켓", 100, unit="1개", max_qty=9, auto="platinum_tickets",
          description="게임 최대 보유량은 9개예요."),
    _item("티켓", "레전드 티켓", 200, unit="1개", max_qty=4, auto="legend_tickets",
          description="게임 최대 보유량은 4개예요."),
    # 업그레이드 재화
    _item("업그레이드 재화", "NP 9,999개", 500, auto="np_9999"),
    _item("업그레이드 재화", "열매 (모든 종류)", 200, unit="200개", max_qty=5, auto="fruit",
          description="종류마다 최대 998개까지 보유할 수 있어요."),
    _item("업그레이드 재화", "수석 (모든 종류)", 200, unit="200개", max_qty=5, auto="stones",
          description="종류마다 최대 998개까지 보유할 수 있어요."),
    _item("업그레이드 재화", "XP", 300, unit="1억", max_qty=1, auto="xp",
          description="게임 최대 XP는 99,999,999예요."),
    _item("업그레이드 재화", "모든 갯츠아이 9,999개", 2000, auto="catseyes_9999"),
    # 스테이지
    _item("스테이지", "세계편 올클리어", 1000, auto="story_world"),
    _item("스테이지", "미래편 올클리어", 1000, auto="story_future"),
    _item("스테이지", "우주편 올클리어", 1000, auto="story_cosmos"),
    _item("스테이지", "보물작", 1500, unit="1장", max_qty=9, form="보물작할 장 (예: 세계편 1장, 미래편 2장)",
          auto="treasure"),
    # 캐릭터
    _item("캐릭터", "모든 캐릭터 획득", 3000, auto="all_cats_unlock"),
    _item("캐릭터", "모든 캐릭터 강화", 3000, auto="all_cats_upgrade"),
    _item("캐릭터", "모든 캐릭터 본능", 3000, auto="all_cats_talents"),
    _item("캐릭터", "원하는 캐릭터 추가", 800, unit="1마리", max_qty=50, form="추가할 캐릭터 이름 (여러 마리면 쉼표로)",
          auto="cat_add"),
    _item("캐릭터", "원하는 캐릭터 삭제", 800, unit="1마리", max_qty=50, form="삭제할 캐릭터 이름 (여러 마리면 쉼표로)",
          auto="cat_remove"),
    _item("캐릭터", "원하는 캐릭터 강화", 500, unit="1마리", max_qty=50, form="강화할 캐릭터 이름 (최대 레벨로 강화)",
          auto="cat_upgrade"),
    _item("캐릭터", "원하는 캐릭터 진화", 500, unit="1마리", max_qty=50, form="진화할 캐릭터 이름 (여러 마리면 쉼표로)",
          auto="cat_evolve"),
    _item("캐릭터", "원하는 캐릭터 본능", 800, unit="1마리", max_qty=50, form="본능을 열 캐릭터 이름 (여러 마리면 쉼표로)",
          auto="cat_talents"),
    # 전투 아이템
    _item("전투 아이템", "모든 배틀 아이템 9,999개", 2000, auto="battle_9999"),
    _item("전투 아이템", "모든 배틀 아이템 무제한", 2000, auto="battle_endless"),
    _item("전투 아이템", "리더십", 100, unit="100개", max_qty=99, auto="leadership"),
    # 가마토토
    _item("가마토토", "가마토토 레벨 130 (MAX)", 500, auto="gamatoto_max"),
    _item("가마토토", "가마토토 대원", 200, unit="1마리", max_qty=10, auto="gamatoto_helpers",
          description="최고 등급 대원으로 팀을 새로 짜요."),
    # 골드회원
    _item("골드회원", "골드회원 30일", 300, auto="gold_pass"),
]
