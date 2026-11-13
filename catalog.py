"""The default shop items, loaded into a server with `/상품 기본목록`.

`auto` links an item to fulfil.AUTO: the bot edits the save itself right after the
purchase. Items without `auto` (memberships) are handled by an admin.

`/상품 기본목록` replaces the shop with this list: items with the same name are updated
(prices too), and every other 수동/자동 처리 item is taken off sale. Change prices later
in Discord with `/상품 수정`, but running `/상품 기본목록` again resets them to these.

Items with a `unit` are priced per unit and the buyer picks how many (up to `max_qty`).
Items past the game's own limits run with the Unlimited editor (fulfil.AUTO) and carry a
`risk`: "high" (well past the limit: 밴 위험 높음) or "some" (can go past it: 밴 위험 있음).
Items without the Unlimited editor stop at the game's own limits (CAP_9999 etc.).
Prices are in won.
"""

CODES = ""  # order form: game codes only


def _item(category, name, price, unit="", max_qty=1, form=CODES, risk="", **extra):
    return {"category": category, "name": name, "price": price, "unit": unit,
            "max_qty": max_qty, "form": form, "kind": "manual", "risk": risk, **extra}


CAT = "💳 재화"
STAGE = "🗺️ 스테이지"
ZOMBIE = "🧟 좀비 스테이지"
CHARS = "🐱 캐릭터"
GAMA = "🔨 가마토토"
ETC = "👀 기타"
MEMBER = "🐶 멤버십"

TREASURE = "보물작할 장 (예: 세계편 1장, 미래편 2장)"
PLAYTIME = "원하는 플레이 시간 (시간 단위, 예: 1000)"
CAP_9999 = "게임 최대치(종류마다 9,999개)까지만 늘어나요."
PERKS = {
    "VIP": "매주 원하는 커스텀 2회 무료 · 달꽃 특보 지급",
    "VVIP": "매주 원하는 커스텀 5회 무료 · 달꽃 반모 지급",
    "MASTER": "매주 원하는 커스텀 10회 무료 · 달꽃 특보 + 반모 지급 · 달꽃이 많이 사랑해줌",
}


def _cats(name, price, verb, auto):
    return _item(CHARS, name, price, unit="1마리", max_qty=50, form=f"{verb} 캐릭터 이름 (여러 마리면 쉼표로)", auto=auto)


CATALOG = [
    # 💳 재화
    _item(CAT, "🥫 통조림 45,000개", 500, auto="catfood_45000", description="게임 최대치(45,000개)까지 채워요."),
    _item(CAT, "🥫 통조림 20만개", 1000, auto="catfood_200k", risk="high"),
    _item(CAT, "🥫 통조림 50만개", 2000, auto="catfood_500k", risk="high"),
    _item(CAT, "🥫 통조림 100만개", 3000, auto="catfood_1m", risk="high"),
    _item(CAT, "⚡ XP", 500, unit="1억", max_qty=20, auto="xp_add", risk="high"),
    _item(CAT, "⚡ NP", 500, unit="1,000개", max_qty=50, auto="np_add", risk="some"),
    _item(CAT, "👀 캣츠아이 (모든 종류)", 1000, unit="999개", max_qty=10, auto="catseyes_999", description=CAP_9999),
    _item(CAT, "🌳 개다래 열매 + 씨앗 (모든 종류)", 1000, unit="999개", max_qty=10, auto="fruitseed_999",
          risk="some"),
    _item(CAT, "💎 수정 · 결정 (모든 종류)", 1500, unit="1,000개", max_qty=10, auto="stonegem_add", risk="high"),
    _item(CAT, "⚡ 리더십", 1000, unit="999개", max_qty=30, auto="leadership_add", risk="some"),
    _item(CAT, "📄 냥코 티켓", 500, unit="3,000장", max_qty=20, auto="normal_tickets_add", risk="high"),
    _item(CAT, "🎫 레어 티켓", 500, unit="300장", max_qty=20, auto="rare_tickets_add", risk="high"),
    _item(CAT, "🎟️ 플래티넘 티켓 9장", 500, auto="platinum_9", risk="some"),
    _item(CAT, "🎟️ 플래티넘 티켓 100장", 3000, auto="platinum_100", risk="high"),
    _item(CAT, "👑 레전드 티켓 4장", 500, auto="legend_4", risk="some"),
    _item(CAT, "👑 레전드 티켓 50장", 3000, auto="legend_50", risk="high"),
    _item(CAT, "🔮 모든 본능 구슬 999개", 1000, auto="orbs_999", risk="some"),
    _item(CAT, "⚔️ 모든 배틀 아이템", 1000, unit="999개", max_qty=10, auto="battle_999", description=CAP_9999),
    _item(CAT, "🍹 모든 드링크", 1000, unit="999개", max_qty=10, auto="drink_999", description=CAP_9999),
    _item(CAT, "👑 특능 만렙 (통솔력 · 공부 능력)", 1000, auto="skills_max",
          description="모든 특수 능력을 최대 레벨로 올려요."),
    # 🗺️ 스테이지
    _item(STAGE, "세계편 클리어", 1000, auto="story_world"),
    _item(STAGE, "미래편 클리어", 1000, auto="story_future"),
    _item(STAGE, "우주편 클리어", 1000, auto="story_cosmos"),
    _item(STAGE, "세계편 · 미래편 · 우주편 올 클리어", 2500, auto="story_all"),
    _item(STAGE, "보물", 500, unit="1장", max_qty=9, form=TREASURE, auto="treasure",
          description="고른 장의 보물을 모두 최고급으로 맞춰요."),
    _item(STAGE, "구레 올클 (4성)", 1500, auto="legend_all", description="레전드 스토리를 모든 크라운까지 클리어해요."),
    _item(STAGE, "신레 올클 (4성)", 1500, auto="uncanny_all", description="신 레전드 스토리를 모든 크라운까지 클리어해요."),
    _item(STAGE, "레제로 올클 (4성)", 1500, auto="zero_all", description="레전드 스토리 0을 모든 크라운까지 클리어해요."),
    # 🧟 좀비 스테이지
    _item(ZOMBIE, "좀비 세계편 클리어", 1500, auto="zombie_world"),
    _item(ZOMBIE, "좀비 미래편 클리어", 1500, auto="zombie_future"),
    _item(ZOMBIE, "좀비 우주편 클리어", 1500, auto="zombie_cosmos"),
    _item(ZOMBIE, "좀비 3개 올 클리어", 3000, auto="zombie_all"),
    # 🐱 캐릭터
    _cats("🐱 원하는 캐릭터", 700, "추가할", "cat_add"),
    _cats("🐷 원하는 캐릭터 레벨업", 700, "레벨업할", "cat_upgrade"),
    _cats("🐮 원하는 캐릭터 진화", 700, "진화할", "cat_true"),
    _cats("🐮 원하는 캐릭터 진화 (4진)", 1000, "4진할", "cat_fourth"),
    _cats("🦁 원하는 캐릭터 본능", 700, "본능을 열", "cat_talents"),
    _cats("🐵 원하는 캐릭터 삭제", 500, "삭제할", "cat_remove"),
    _item(CHARS, "🐯 모든 캐릭터", 3000, auto="all_cats_unlock"),
    _item(CHARS, "🐱 보유한 모든 캐릭터 레벨업", 3000, auto="all_cats_upgrade"),
    _item(CHARS, "🐥 보유한 모든 캐릭터 진화", 3000, auto="all_cats_true"),
    _item(CHARS, "🐥 보유한 모든 캐릭터 진화 (4진)", 4000, auto="all_cats_fourth",
          description="4진이 있는 캐릭터는 3진 · 4진까지 진화해요."),
    _item(CHARS, "🐸 보유한 모든 캐릭터 본능", 3000, auto="all_cats_talents"),
    # 🔨 가마토토
    _item(GAMA, "👨‍🏭 가마토토 조수 5마리", 500, auto="engineers_5",
          description="오토토 기술자를 5명 늘려요 (게임 최대치까지)."),
    _item(GAMA, "🏰 가마토토 성 재료", 500, unit="1,000개", max_qty=9, auto="material_1000", description=CAP_9999),
    _item(GAMA, "⚒️ 가마토토 만렙", 1000, auto="gamatoto_max"),
    _item(GAMA, "🔧 가마토토 대원 10마리", 1000, auto="helpers_10", description="최고 등급 대원으로 팀을 새로 짜요."),
    _item(GAMA, "🔫 가마토토 모든 대포 만렙", 1000, auto="cannons_max"),
    _item(GAMA, "🪖 가마토토 사당 만렙", 500, auto="shrine_max"),
    # 👀 기타
    _item(ETC, "💛 골드 회원", 500, auto="gold_pass", description="골드 회원 30일을 지급해요."),
    _item(ETC, "📺 플레이 타임 조작", 500, form=PLAYTIME, auto="playtime"),
    _item(ETC, "🥇 냥코 메달", 500, auto="medals_all", description="모든 냥코 메달을 획득해요."),
    # 주문제작: 0원 request; an admin quotes a price with /주문 견적 and the buyer pays it from a DM.
    _item(ETC, "🏆 주문제작 (견적 문의)", 0, form="원하는 내용 (가격표보다 많은 수량도 돼요)",
          description="가격표에 없는 것, 가격표보다 많은 수량도 주문할 수 있어요. 관리자가 견적을 보내 드리면 "
                      "DM의 결제하기 버튼으로 결제해 주세요."),
    # Done by an admin by hand (the codes go to the admin log).
    _item(ETC, "🖨️ 벤감소", 3000, description="관리자가 확인 후 처리해 드려요."),
    _item(ETC, "계정복사", 3000, description="관리자가 확인 후 처리해 드려요."),
    # 🐶 멤버십: the bot gives the role named after the tier (MEMBER_ROLES); an admin gives the
    # perks by hand. Monthly ones last 30 days (db.MEMBERSHIP_DAYS) and the role is then taken back.
    *[
        _item(MEMBER, f"{tier} ({period})", price, form=None, description=PERKS[tier],
              membership=f"{tier}:{tag}")
        for tier, monthly, forever in (("VIP", 10000, 30000), ("VVIP", 20000, 45000), ("MASTER", 30000, 60000))
        for period, tag, price in (("매달", "month", monthly), ("영구", "forever", forever))
    ],
]

# Membership tier -> (role name, role colour). The role is made if the server doesn't have one by that name.
MEMBER_ROLES = {"VIP": ("VIP", 0xF1C40F), "VVIP": ("VVIP", 0xE67E22), "MASTER": ("MASTER", 0x9B59B6)}

# Shown on items with a risk (shop list, purchase confirmation).
RISK_LABEL = {"high": "⚠️ 밴 위험 높음", "some": "⚠️ 밴 위험 있음"}
RISK_TEXT = {
    "high": "게임 최대치를 훨씬 넘는 수량이에요. 게임 서버가 비정상 데이터로 보고 **계정 정지(밴)나 "
            "데이터 초기화**를 할 위험이 높아요. 문제가 생겨도 환불되지 않으니 신중하게 구매하세요.",
    "some": "보유량에 따라 게임 최대치를 넘을 수 있어서 **계정 정지(밴)나 데이터 초기화** 위험이 있어요. "
            "문제가 생겨도 환불되지 않으니 신중하게 구매하세요.",
}
