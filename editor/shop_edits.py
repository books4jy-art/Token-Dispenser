"""Shop-only edits, turned into the editor's own edits once the save is loaded.

The editor (worker.py / extras.py, copied from the Battle-Cats-Editor-Site-KR repo)
sets values. The shop sells amounts ("+5 platinum tickets", "+200 of every catfruit"),
which depend on what the save already has, so these keys are expanded here after the
save is downloaded:

  "add":          {"platinum_tickets": 5, ...}  -> current + amount (worker caps it)
  "atleast":      {"catfood": 100000}           -> raised to this amount (never lowered)
  "fruit_add":    200   -> +200 of every catfruit (not seeds); "fruitseed_add" also seeds
  "stone_add":    200   -> +200 of every behemoth stone (not gems); "stonegem_add" also gems
  "catseyes_set": 9999  -> every catseye type set to this; "catseyes_add" adds
  "battle_add":   998   -> + to every battle item
  "drink_add":    999   -> + to every Catamin (drink)
  "endless_all":  True  -> every battle item endless
  "helpers_top":  3     -> Gamatoto helpers: 3 of the highest rarity
"""
from __future__ import annotations

from typing import Any

BEHEMOTH_GROUP = 9  # same as worker.BEHEMOTH_GROUP
GAME_DATA_KEYS = ("fruit_add", "fruitseed_add", "stone_add", "stonegem_add", "helpers_top")


def needs_game_data(edits: dict[str, Any]) -> bool:
    return any(edits.get(k) for k in GAME_DATA_KEYS)


def expand(core: Any, save: Any, edits: dict[str, Any]) -> dict[str, Any]:
    edits = dict(edits)

    for key, amount in (edits.pop("add", None) or {}).items():
        edits[key] = int(getattr(save, key)) + int(amount)
    for key, amount in (edits.pop("atleast", None) or {}).items():
        edits[key] = max(int(getattr(save, key)), int(amount))

    fruit_add, stone_add = edits.pop("fruit_add", None), edits.pop("stone_add", None)
    fruitseed_add, stonegem_add = edits.pop("fruitseed_add", None), edits.pop("stonegem_add", None)
    if fruit_add or stone_add or fruitseed_add or stonegem_add:
        matatabi = core.Matatabi(save).matatabi
        if not matatabi:
            raise RuntimeError("게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요")
        items = dict(edits.get("items_fruit") or {})
        for i, fr in enumerate(matatabi):
            if i >= len(save.catfruit):
                break
            behemoth = fr.group == BEHEMOTH_GROUP
            if fruitseed_add and not behemoth:
                items[i] = int(save.catfruit[i]) + int(fruitseed_add)
            elif fruit_add and not behemoth and not fr.seed:
                items[i] = int(save.catfruit[i]) + int(fruit_add)
            elif stonegem_add and behemoth:
                items[i] = int(save.catfruit[i]) + int(stonegem_add)
            elif stone_add and behemoth and fr.sort < 300:
                items[i] = int(save.catfruit[i]) + int(stone_add)
        if not items:
            raise RuntimeError("이 세이브에 해당하는 아이템이 없어요")
        edits["items_fruit"] = items

    catseyes = edits.pop("catseyes_set", None)
    if catseyes:
        edits["items_eye"] = {i: int(catseyes) for i in range(len(save.catseyes))}
    catseyes_add = edits.pop("catseyes_add", None)
    if catseyes_add:
        edits["items_eye"] = {i: int(v) + int(catseyes_add) for i, v in enumerate(save.catseyes)}
    battle_add = edits.pop("battle_add", None)
    if battle_add:
        edits["items_battle"] = {i: int(it.amount) + int(battle_add) for i, it in enumerate(save.battle_items.items)}
    drink_add = edits.pop("drink_add", None)
    if drink_add:
        edits["items_drink"] = {i: int(v) + int(drink_add) for i, v in enumerate(save.catamins)}

    if edits.pop("endless_all", None):
        edits["endless"] = {"minutes": "inf", "ids": list(range(len(save.battle_items.items)))}

    helpers = edits.pop("helpers_top", None)
    if helpers:
        rarities = core.core_data.get_gamatoto_members_name(save).get_all_rarity_names() or []
        if not rarities:
            raise RuntimeError("게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요")
        gama = dict(edits.get("gamatoto") or {})
        gama["helpers"] = [0] * (len(rarities) - 1) + [int(helpers)]
        edits["gamatoto"] = gama
    return edits


def cat_names(core: Any, cc: Any) -> dict[str, Any]:
    """Every character with all its form names, for matching what buyers type."""
    save = core.SaveFile(cc=cc, load=False, gv=core.GameVersion(999999))  # newest game data
    unit_buy = core.UnitBuy(save).unit_buy
    if not unit_buy:
        raise RuntimeError("게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요")
    cats = []
    for cat_id in range(len(unit_buy)):
        names = [n for n in (core.Cat.get_names(cat_id, save) or []) if n and n.strip()]
        cats.append([cat_id, names])
    return {"ok": True, "cc": cc.get_code(), "cats": cats}
