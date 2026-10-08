"""The default shop items, loaded into a server with `/상품 기본목록`.

`auto` links an item to fulfil.AUTO: the bot edits the save itself right after the
purchase. Items without `auto` (memberships) are handled by an admin.

`/상품 기본목록` replaces the shop with this list: items with the same name are updated
(prices too), and every other 수동/자동 처리 item is taken off sale. Change prices later
in Discord with `/상품 수정`, but running `/상품 기본목록` again resets them to these.

Items with a `unit` are priced per unit and the buyer picks how many (up to `max_qty`).
Items past the game's own limits run with the Unlimited editor (fulfil.AUTO) and carry a
`risk`: "high" (well past the limit: 밴 위험 높음) or "some" (can go past it: 밴 위험 있음).
Prices are in won.
"""

CODES = ""  # order form: game codes only


def _item(category, name, price, unit="", max_qty=1, form=CODES, risk="", **extra):
    return {"category": category, "name": name, "price": price, "unit": unit,
            "max_qty": max_qty, "form": form, "kind": "manual", "risk": risk, **extra}


CAT = "💎 재화"
CHARS = "🐾 캐릭터"
STAGE = "🌎 스테이지"
TICKET = "🎟️ 티켓"
PLAT = "👑 플래티넘"
LEGEND = "👑 레전드"
MEMBER = "🐶 멤버십"

CHAPTERS = "클리어할 장 (예: 1장, 3장 / 1,2,3)"
PERKS = {
    "VIP": "매주 원하는 커스텀 2회 무료 · 달꽃 특보 지급",
    "VVIP": "매주 원하는 커스텀 5회 무료 · 달꽃 반모 지급",
    "MASTER": "매주 원하는 커스텀 10회 무료 · 달꽃 특보 + 반모 지급 · 달꽃이 많이 사랑해줌",
}

CATALOG = [
    # 💎 재화
    _item(CAT, "통조림 45,000개", 100, auto="catfood_45000",
          description="게임 최대치(45,000개)까지 채워요."),
    _item(CAT, "통조림 10만개", 500, auto="catfood_100k", risk="high"),
    _item(CAT, "통조림 100만개", 1000, auto="catfood_1m", risk="high"),
    _item(CAT, "통조림 21억개", 3000, auto="catfood_2100m", risk="high"),
    _item(CAT, "XP", 500, unit="1억", max_qty=20, auto="xp_add", risk="high"),
    _item(CAT, "NP", 500, unit="1,000개", max_qty=50, auto="np_add", risk="some"),
    _item(CAT, "캣츠아이 (모든 종류)", 1000, unit="998개", max_qty=10, auto="catseyes_add", risk="some"),
    _item(CAT, "개다래 열매 + 씨앗 (모든 종류)", 1000, unit="998개", max_qty=10, auto="fruitseed_add",
          risk="some"),
    _item(CAT, "수정 · 결정 (모든 종류)", 1500, unit="998개", max_qty=10, auto="stonegem_add", risk="some"),
    _item(CAT, "리더십", 1000, unit="999개", max_qty=30, auto="leadership_add", risk="some"),
    _item(CAT, "모든 본능 구슬 998개", 1000, auto="orbs_998"),
    _item(CAT, "모든 배틀 아이템", 1000, unit="998개", max_qty=10, auto="battle_add", risk="some"),
    _item(CAT, "모든 드링크", 1000, unit="999개", max_qty=10, auto="drink_add", risk="some"),
    _item(CAT, "특능 만렙 (통솔력 · 공부 능력)", 1000, auto="skills_max",
          description="모든 특수 능력을 최대 레벨로 올려요."),
    # 🐾 캐릭터
    _item(CHARS, "올냥 (모든 캐릭터 획득)", 3000, auto="all_cats_unlock"),
    _item(CHARS, "올강 (모든 캐릭터 강화)", 2000, auto="all_cats_upgrade",
          description="가진 캐릭터를 모두 최대 레벨로 강화해요."),
    _item(CHARS, "올진화 (모든 캐릭터 진화)", 2000, auto="all_cats_evolve",
          description="가진 캐릭터를 모두 3단·4단 진화가 있는 데까지 진화해요."),
    _item(CHARS, "원하는 캐릭터 추가", 500, unit="1마리", max_qty=50,
          form="추가할 캐릭터 이름 (여러 마리면 쉼표로)", auto="cat_add"),
    _item(CHARS, "원하는 캐릭터 제거", 500, unit="1마리", max_qty=50,
          form="제거할 캐릭터 이름 (여러 마리면 쉼표로)", auto="cat_remove"),
    _item(CHARS, "원하는 캐릭터 강화", 500, unit="1마리", max_qty=50,
          form="강화할 캐릭터 이름 (최대 레벨로 강화)", auto="cat_upgrade"),
    _item(CHARS, "원하는 캐릭터 진화", 500, unit="1마리", max_qty=50,
          form="진화할 캐릭터 이름 (여러 마리면 쉼표로)", auto="cat_evolve"),
    # 🌎 스테이지 (장마다, 보물 포함)
    _item(STAGE, "세계편 (보물 포함)", 1000, unit="1장", max_qty=3, form=CHAPTERS, auto="story_world_ch",
          description="고른 장을 클리어하고 보물을 모두 최고 등급으로 맞춰요."),
    _item(STAGE, "미래편 (보물 포함)", 1000, unit="1장", max_qty=3, form=CHAPTERS, auto="story_future_ch",
          description="고른 장을 클리어하고 보물을 모두 최고 등급으로 맞춰요."),
    _item(STAGE, "우주편 (보물 포함)", 1000, unit="1장", max_qty=3, form=CHAPTERS, auto="story_cosmos_ch",
          description="고른 장을 클리어하고 보물을 모두 최고 등급으로 맞춰요."),
    # 🎟️ 티켓
    _item(TICKET, "냥코 티켓", 500, unit="3,000장", max_qty=20, auto="normal_tickets_add", risk="high"),
    _item(TICKET, "레어 티켓", 500, unit="300장", max_qty=20, auto="rare_tickets_add", risk="high"),
    # 👑 플래티넘 / 레전드
    _item(PLAT, "플래티넘 티켓 9장", 500, auto="platinum_9", risk="some"),
    _item(PLAT, "플래티넘 티켓 100장", 3000, auto="platinum_100", risk="high"),
    _item(LEGEND, "레전드 티켓 4장", 500, auto="legend_4", risk="some"),
    _item(LEGEND, "레전드 티켓 50장", 3000, auto="legend_50", risk="high"),
    # 🐶 멤버십: an admin gives the perks by hand (no order form).
    *[
        _item(MEMBER, f"{tier} ({period})", price, form=None, description=PERKS[tier])
        for tier, monthly, forever in (("VIP", 10000, 30000), ("VVIP", 20000, 45000), ("MASTER", 30000, 60000))
        for period, price in (("매달", monthly), ("영구", forever))
    ],
]

# Shown on items with a risk (shop list, purchase confirmation).
RISK_LABEL = {"high": "⚠️ 밴 위험 높음", "some": "⚠️ 밴 위험 있음"}
RISK_TEXT = {
    "high": "게임 최대치를 훨씬 넘는 수량이에요. 게임 서버가 비정상 데이터로 보고 **계정 정지(밴)나 "
            "데이터 초기화**를 할 위험이 높아요. 문제가 생겨도 환불되지 않으니 신중하게 구매하세요.",
    "some": "보유량에 따라 게임 최대치를 넘을 수 있어서 **계정 정지(밴)나 데이터 초기화** 위험이 있어요. "
            "문제가 생겨도 환불되지 않으니 신중하게 구매하세요.",
}
