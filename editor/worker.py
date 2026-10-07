"""Runs one BCSFE job in an isolated process.

Copied from the Battle-Cats-Editor-Site-KR repo (bcsfe-web/worker.py). Changes for the
shop bot: shop_edits.expand() turns shop amounts into edits after the save is loaded,
and a "cat_names" mode lists every character's form names.

The web server (or Discord bot) starts this script once per request, sends the job as JSON on stdin and
reads the result as JSON from stdout. Running each job in its own process keeps
BCSFE's global state (config, cached game data, country code) from leaking
between users, and means one crashed or hung edit can't take the bot down.

Job format (stdin):
{
  "mode": "codes" | "file",
  "transfer_code": "...", "confirmation_code": "...",   # mode == codes
  "file_b64": "...",                                     # mode == file
  "cc": "en" | "jp" | "kr" | "tw" | null,
  "edits": {"catfood": 45000, "xp": 99999999, "unlock_cats": true, ...},
  "new_account": false,
  "data_dir": "/path/to/shared/bcsfe-data",
  "job_dir": "/path/to/private/tmp/dir"
}
"""
from __future__ import annotations

import base64
import datetime
import gzip
import contextlib
import json
import os
import random
import sys
import traceback
from typing import Any, Callable

# BCSFE prints to stdout (progress messages, colour codes). Keep the real
# stdout for our JSON result and send everything else to stderr.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

from importlib import resources  # noqa: E402

import bcsfe  # noqa: E402
import extras  # noqa: E402
import shop_edits  # noqa: E402
from bcsfe import core  # noqa: E402

# Numeric fields: (save attribute, max-value key, managed item type or None).
# Managed items are how the game server tracks premium currency; BCSFE reports
# changes to them so the server's records match the save (ban prevention).
NUMERIC_FIELDS: dict[str, tuple[str, str, Any]] = {
    "catfood": ("catfood", "catfood", core.ManagedItemType.CATFOOD),
    "xp": ("xp", "xp", None),
    "np": ("np", "np", None),
    "leadership": ("leadership", "leadership", None),
    "normal_tickets": ("normal_tickets", "normal_tickets", None),
    "rare_tickets": ("rare_tickets", "rare_tickets", core.ManagedItemType.RARE_TICKET),
    "platinum_tickets": (
        "platinum_tickets",
        "platinum_tickets",
        core.ManagedItemType.PLATINUM_TICKET,
    ),
    "legend_tickets": (
        "legend_tickets",
        "legend_tickets",
        core.ManagedItemType.LEGEND_TICKET,
    ),
    "platinum_shards": ("platinum_shards", "platinum_tickets", None),
    "hundred_million_ticket": ("hundred_million_ticket", "hundred_million_tickets", None),
}

LABELS = {
    "catfood": "고양이 통조림",
    "xp": "경험치",
    "np": "NP",
    "leadership": "통솔력",
    "normal_tickets": "냥코 티켓",
    "rare_tickets": "레어 티켓",
    "platinum_tickets": "플래티넘 티켓",
    "legend_tickets": "레전드 티켓",
    "platinum_shards": "플래티넘의 조각",
    "hundred_million_ticket": "1억 다운로드 기념 티켓",
}


def accept_backup_game_data_repo() -> None:
    """Let BCSFE fall back to its backup game-data repo without asking.

    When the main repo is unreachable BCSFE asks on the terminal whether to
    switch to its GitLab mirror. A web request can't answer, so cat edits would
    just fail; answer yes instead. Every other question still raises EOFError.
    """
    from bcsfe.core.server import game_data_getter

    ask = game_data_getter.dialog_creator.yes_no_key
    if getattr(ask, "_accepts_backup_repo", False):
        return

    def yes_no_key(key: str, *args: Any, **kwargs: Any) -> bool:
        if key == "use_alternative_repo":
            return True
        return ask(key, *args, **kwargs)

    yes_no_key._accepts_backup_repo = True  # type: ignore[attr-defined]
    game_data_getter.dialog_creator.yes_no_key = yes_no_key


# Main story chapters, in BCSFE's get_real_chapters() order.
STORY_CHAPTERS = ['세계편 제1장', '세계편 제2장', '세계편 제3장', '미래편 제1장', '미래편 제2장', '미래편 제3장', '우주편 제1장', '우주편 제2장', '우주편 제3장']
# Chapters the game needs cleared first (from BCSFE's clear_previous_chapters).
STORY_REQUIRES = {1: [0], 2: [0, 1], 3: [0], 4: [0, 3], 5: [0, 3, 4], 6: [0, 3], 7: [0, 3, 6], 8: [0, 3, 6, 7]}
TREASURE_LEVELS = ['없음', '조잡한', '평범한', '최고급']
STORY_STAGES = 48


# Legend and event maps: save attribute, BCSFE map code, map id base, EventChapters type.
# The same arguments the BCSFE CLI passes to edits.map.edit_chapters for each menu.
MAP_GROUPS: dict[str, tuple[str, str, int, int | None]] = {
    "legend": ("event_stages", "N", 0, 0),
    "uncanny": ("uncanny", "NA", 13000, None),
    "zero": ("zero_legends", "ND", 34000, None),
    "event": ("event_stages", "S", 1000, 1),
    "collab": ("event_stages", "C", 2000, 2),
    "gauntlet": ("gauntlets", "A", 24000, None),
    "collab_gauntlet": ("collab_gauntlets", "CA", 27000, None),
    "behemoth": ("behemoth_culling", "Q", 31000, None),
    "enigma": ("enigma_clears", "H", 25000, None),
    "tower": ("tower.chapters", "V", 7000, None),
    "legend_quest": ("legend_quest", "D", 16000, None),
    "catamin_stage": ("catamin_stages.chapters", "B", 14000, None),
    "catclaw": ("dojo_chapters", "G", 37000, None),
}
NO_R_PREFIX = {'catclaw'}  # map names without the R prefix
MAP_GROUP_NAMES = {'legend': '레전드 스토리', 'uncanny': '신 레전드 스토리', 'zero': '레전드 스토리 0', 'event': '이벤트 스테이지', 'collab': '콜라보 스테이지', 'gauntlet': '건틀렛', 'collab_gauntlet': '콜라보 건틀렛', 'behemoth': '거대 수석 사냥', 'enigma': '수수께끼 스테이지', 'tower': '탑', 'legend_quest': '레전드 퀘스트', 'catamin_stage': '고양이 드링크 스테이지', 'catclaw': '냥코 도장 랭킹전'}


def clear_map_group(save: core.SaveFile, key: str, crowns: int) -> int:
    """Clear every map of a legend/event group that exists in this game version.

    Mirrors the BCSFE CLI's "clear progress" option: only maps that have names in
    the game data (and exist in the save) are touched, each up to its own crown
    count (capped at `crowns` when non-zero). Existing clear counts are kept.
    """
    from bcsfe.cli.edits import map as map_edits

    attr, code, base, map_type = MAP_GROUPS[key]
    chapters = save
    for part in attr.split("."):
        chapters = getattr(chapters, part)
    if key == "uncanny":
        chapters = chapters.chapters
    map_option = core.MapOption.from_save(save)
    names = core.MapNames(save, code, base_index=base, output=False, no_r_prefix=key in NO_R_PREFIX).map_names
    if map_option is None or not names:
        raise RuntimeError('게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요' if map_option is None else '이 게임 버전의 게임 데이터에 맵이 없어요')

    total_maps = map_edits.get_total_maps(chapters)
    cleared = 0
    for map_id in sorted(names):
        if map_id >= total_maps:
            continue
        stars = map_edits.get_total_stars(map_option, base, chapters, map_id, map_type)
        if crowns:
            stars = min(stars, crowns)
        for star in range(stars):
            for stage in range(map_edits.get_total_stages(chapters, map_id, star, map_type)):
                if map_type is None:
                    chapters.clear_stage(map_id, star, stage, ensure_cleared_only=True)
                else:
                    chapters.clear_stage(map_type, map_id, star, stage, ensure_cleared_only=True)
        cleared += 1
    if not cleared:
        raise RuntimeError('이 게임 버전의 게임 데이터에 맵이 없어요')
    return cleared


# Rarity names in this site's language (as the game shows them).
RARITY_NAMES = ['기본 캐릭터', 'EX', '레어', '슈퍼 레어', '울트라 슈퍼 레어', '레전드 레어']


def cat_catalog(cc: core.CountryCode) -> dict[str, Any]:
    """Every character in the latest game data for a region: [id, name, rarity, obtainable]."""
    save = core.SaveFile(cc=cc, load=False, gv=core.GameVersion(999999))  # newest game data
    unit_buy = core.UnitBuy(save).unit_buy
    if not unit_buy:
        raise RuntimeError('게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요')
    obtainable = {c.cat_id for c in (core.NyankoPictureBook(save).get_obtainable_cats() or [])}
    cats = []
    for cat_id, data in enumerate(unit_buy):
        names = core.Cat.get_names(cat_id, save) or []
        cats.append([cat_id, names[0] if names else "", data.rarity, cat_id in obtainable])
    return {"ok": True, "cc": cc.get_code(), "cats": cats, "rarities": core.Cats.get_rarity_names(save)}


SPECIAL_CAT_TEXT = {'add_limited_cats': '한정 캐릭터 {n}개 추가', 'add_dummy_cats': '더미 데이터 캐릭터 {n}개 추가'}
NAME_LANG = {"en": "en", "jp": "ja", "kr": "ko", "tw": "tw"}
# Cats only one version ever released, from the Battle Cats wiki's "<Country> Exclusive Content"
# categories (.github/scripts/wiki_limited.py). A cat on another country's list is limited for this
# one even when this country's game data has a translated name for it.
WIKI_EXCLUSIVE: dict[str, list[int]] = {"jp": [29, 45, 54, 62, 77, 89, 90, 101, 102, 133, 141, 142, 155, 156, 157, 162, 163, 164, 165, 166, 167, 178, 192, 193, 202, 204, 205, 206, 207, 208, 231, 232, 233, 234, 235, 236, 249, 250, 251, 252, 253, 254, 255, 256, 262, 263, 264, 265, 266, 281, 309, 321, 347, 348, 349, 350, 356, 398, 422, 423, 424, 425, 430, 434, 453, 454, 479, 480, 503, 504, 524, 540, 541, 542, 567, 608, 626, 627, 628, 637, 638, 639, 640, 701, 702, 703, 704, 727, 762], "en": [432, 433, 497, 498, 499, 500, 740, 741, 742, 743, 744, 745], "tw": [285, 320, 353, 465, 492], "kr": [139, 182]}


def special_cats(save: core.SaveFile) -> dict[str, list[int]]:
    """Characters this save's country never released, from every country's own-language game data:
    limited edition = named in another country's data but not in this country's (country-limited
    collabs and exclusives), plus characters with a name here that this country can't obtain any more
    (old limited collabs); dummy data = no real name in any country's data (unused placeholder slots).
    Each country gets its own GameDataGetter: BCSFE's shared one must keep serving this save."""
    import re

    from bcsfe.core.server.game_data_getter import GameDataGetter

    def real(text: str) -> bool:
        name = re.split(r"[|,]", text or "")[0].strip()
        return bool(name and re.search(r"[^\W\d_]", name) and not re.fullmatch(r"[\d_\-\s]+|仮\d*", name))

    total = len(save.cats.cats)
    named: dict[str, set[int]] = {}
    for code, lang in NAME_LANG.items():
        getter = GameDataGetter(core.CountryCode.from_code(code), core.GameVersion(999999), do_print=False)
        named[code] = set()
        for cat_id in range(total):
            data = getter.download("resLocal", f"Unit_Explanation{cat_id + 1}_{lang}.csv")
            text = data.to_str() if data is not None else ""
            if real(text.splitlines()[0] if text else ""):
                named[code].add(cat_id)
    obtainable = save.cats.get_cats_obtainable(save)
    if obtainable is None or not any(named.values()):
        raise RuntimeError("couldn't download game data — try again later")
    obtainable_ids = {c.id for c in obtainable}
    anywhere = set().union(*named.values())
    code = save.cc.get_code()
    own = named.get(code, set())
    elsewhere = {i for c, ids in WIKI_EXCLUSIVE.items() if c != code for i in ids} - set(WIKI_EXCLUSIVE.get(code, []))
    return {"add_limited_cats": [i for i in range(total) if (i not in own and i in anywhere)
                                 or (i in own and i not in obtainable_ids) or i in elsewhere],
            "add_dummy_cats": [i for i in range(total) if i not in anywhere and i not in elsewhere]}


def edit_cats(save: core.SaveFile, edits: dict[str, Any], attempt: Callable[[str, Callable[[], None]], None],
              done: list[str]) -> None:
    """Add/remove individual characters and add whole rarities (obtainable only)."""
    add_ids = [i for i in edits.get("add_cats") or [] if i not in (edits.get("remove_cats") or [])]
    remove_ids = edits.get("remove_cats") or []

    def by_ids(ids: list[int]) -> tuple[list[Any], list[int]]:
        cats = [save.cats.get_cat_by_id(i) for i in ids]
        return [c for c in cats if c is not None], [i for i, c in zip(ids, cats) if c is None]

    def step(label: str, fn: Callable[[], str]) -> None:
        out: dict[str, str] = {}
        before = len(done)
        attempt(label, lambda: out.update(label=fn()))
        if len(done) > before and out.get("label"):
            done[-1] = out["label"]

    for rarity in edits.get("add_rarities") or []:
        def add_rarity(rarity: int = rarity) -> str:
            obtainable = save.cats.get_cats_obtainable(save)
            if obtainable is None:
                raise RuntimeError('게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요')
            ids = {c.id for c in obtainable}
            cats = [c for c in save.cats.get_cats_rarity(save, rarity) if c.id in ids and c.id not in remove_ids]
            for cat in cats:
                cat.unlock(save)
            name = RARITY_NAMES[rarity] if rarity < len(RARITY_NAMES) else str(rarity)
            return '획득 가능한 {name} 전부 추가: {n}개'.format(name=name, n=len(cats))
        step('등급별로 추가', add_rarity)

    if add_ids:
        def add() -> str:
            cats, missing = by_ids(add_ids)
            if not cats:
                raise RuntimeError('이 세이브에 해당하는 ID가 없어요')
            for cat in cats:
                cat.unlock(save)
            label = '캐릭터 {n}개 추가'.format(n=len(cats))
            return label + (' (이 세이브에 없는 ID: {ids})'.format(ids=", ".join(map(str, missing))) if missing else "")
        step('추가', add)

    if remove_ids:
        def remove() -> str:
            cats, missing = by_ids(remove_ids)
            if not cats:
                raise RuntimeError('이 세이브에 해당하는 ID가 없어요')
            for cat in cats:  # like the BCSFE CLI with its default reset_cat_data setting
                cat.remove(reset=True, save_file=save)
            label = '캐릭터 {n}개 삭제'.format(n=len(cats))
            return label + (' (이 세이브에 없는 ID: {ids})'.format(ids=", ".join(map(str, missing))) if missing else "")
        step('삭제', remove)


def level_up_cats(save: core.SaveFile, spec: dict[str, Any], skip_ids: list[int]) -> str:
    """Set base+plus levels like the BCSFE CLI's upgrade: the base level is reached by
    levelling up one step at a time (capped at the character's limit, Catseyes counted),
    the plus level is capped at the character's max plus level."""
    base, plus = spec.get("base"), spec.get("plus")
    missing: list[int] = []
    if spec.get("target") == "all":
        cats = save.cats.get_unlocked_cats()
    else:
        cats = []
        for cat_id in spec.get("ids") or []:
            cat = save.cats.get_cat_by_id(cat_id)
            (cats.append(cat) if cat is not None else missing.append(cat_id))
    cats = [c for c in cats if c.id not in skip_ids]
    if not cats:
        raise RuntimeError('레벨업할 보유 캐릭터가 없어요')

    capped = False
    for cat in cats:
        power_up = core.PowerUpHelper(cat, save)
        if base is not None:
            power_up.reset_upgrade()
            if base == "max":
                power_up.max_upgrade()
            else:
                power_up.upgrade_by(max(int(base) - 1, 0))
                capped |= cat.upgrade.get_base() < int(base)
        if plus is not None:
            max_plus = power_up.get_max_possible_plus()
            cat.upgrade.plus = max_plus if plus == "max" else min(int(plus), max_plus)
            capped |= plus != "max" and int(plus) > max_plus
        if not cat.unlocked:
            cat.unlock(save)

    level = ("" if base is None else str(base)) + ("" if plus is None else f"+{plus}")
    level = level.replace("max", '최대')
    n = len(cats)
    label = '캐릭터 {n}개를 {level}(으)로 레벨업'.format(n=n, s="" if n == 1 else "s", level=level)
    if capped:
        label += ' (캐릭터별 최대치까지)'
    if missing:
        label += ' (이 세이브에 없는 ID: {ids})'.format(ids=", ".join(map(str, missing)))
    return label


def claim_rank_rewards(save: core.SaveFile) -> int:
    """Mark the user rank rewards the account's rank has reached as claimed (they raise level caps)."""
    gifts = core.core_data.get_rank_gifts(save).rank_gift
    if gifts is None:
        raise RuntimeError("게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요")
    rank, rewards, n = save.calculate_user_rank(), save.user_rank_rewards.rewards, 0
    for gift in gifts:
        if gift.index < len(rewards) and gift.threshold <= rank and not rewards[gift.index].claimed:
            rewards[gift.index].claimed, n = True, n + 1
    return n


TRAIT_NAMES = {0: '빨간 적', 1: '떠있는 적', 2: '검은 적', 3: '메탈 적', 4: '천사', 5: '에이리언', 6: '좀비', 7: '고대종', 11: '악마'}  # full names where the game data abbreviates them


def orb_effect_name(text: str) -> str:
    """'Attack Up %@: %@' / '데미지 업 %@【%@】' -> 'Attack Up' / '데미지 업'."""
    for token in ("【%@】", ": %@", ":%@", "%@"):
        text = text.replace(token, "")
    return text.strip()


def orb_catalog(cc: core.CountryCode) -> dict[str, Any]:
    """Talent orb types in the newest game data: grades, traits, effects and each orb's ids."""
    save = core.SaveFile(cc=cc, load=False, gv=core.GameVersion(999999))
    info = core.OrbInfoList.create(save)
    if info is None:
        raise RuntimeError('게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요')
    grades, traits, effects, orbs = {}, {}, {}, []
    for orb in info.orb_info_list:
        raw = orb.raw_orb_info
        grades[raw.rank_id] = orb.rank
        if raw.target_id is not None:
            traits[raw.target_id] = TRAIT_NAMES.get(raw.target_id) or orb.target
        effects[raw.effect_id] = orb_effect_name(orb.effect)
        orbs.append([raw.orb_id, raw.rank_id, raw.target_id, raw.effect_id])
    as_list = lambda d: [[k, d[k]] for k in sorted(d)]
    return {"ok": True, "cc": cc.get_code(), "grades": as_list(grades), "traits": as_list(traits),
            "effects": as_list(effects), "orbs": orbs, "max": core.core_data.max_value_manager.talent_orbs}


def set_talent_orbs(save: core.SaveFile, spec: dict[str, Any]) -> str:
    """Set the count of every orb type matching the filters (empty filter = any)."""
    info = core.OrbInfoList.create(save)
    if info is None:
        raise RuntimeError('게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요')
    grades, traits, effects = (set(spec.get(k) or []) for k in ("grades", "traits", "effects"))
    count = max(0, min(int(spec["count"]), core.core_data.max_value_manager.talent_orbs))
    chosen = []
    for orb in info.orb_info_list:
        raw = orb.raw_orb_info
        trait = -1 if raw.target_id is None else raw.target_id   # -1 = orbs with no trait
        if spec.get("all") or ((not grades or raw.rank_id in grades) and (not traits or trait in traits)
                               and (not effects or raw.effect_id in effects)):
            chosen.append(raw.orb_id)
    if not chosen:
        raise RuntimeError('고른 카테고리에 맞는 구슬이 없어요')
    for orb_id in chosen:
        save.talent_orbs.set_orb(orb_id, count)
    n = len(chosen)
    return '본능 구슬 {n}종류를 {count}개로 설정'.format(n=n, s="" if n == 1 else "s", count=count)


ITEM_GROUP_NAMES = {'fruit': '개다래 열매 · 씨앗', 'stone': '수석 · 결정', 'eye': '캣츠아이', 'battle': '배틀 아이템', 'drink': '고양이 드링크', 'chest': '보물 상자', 'material': '성 재료', 'medal': '미궁 훈장'}
BEHEMOTH_GROUP = 9  # matatabi group of behemoth stones/gems (the "crystals")


def short_labels(names: list[str]) -> list[str]:
    """Drop the words every name in a group shares ("Purple Catfruit Seed" -> "Purple")."""
    if len(names) < 2:
        return names
    prefix = os.path.commonprefix(names)
    suffix = os.path.commonprefix([n[::-1] for n in names])[::-1]
    # only strip whole words / bracketed parts, never half a word
    if not prefix.endswith((" ", "[", "【")):
        cut = max(prefix.rfind(" "), prefix.rfind("["), prefix.rfind("【"))
        prefix = prefix[:cut + 1] if cut >= 0 else ""
    if not suffix.startswith((" ", "]", "】")):
        cuts = [i for i in (suffix.find(" "), suffix.find("]"), suffix.find("】")) if i >= 0]
        suffix = suffix[min(cuts):] if cuts else ""
    out = [n[len(prefix):len(n) - len(suffix)].strip(" []【】") for n in names]
    return out if all(out) else names


def item_catalog(cc: core.CountryCode) -> dict[str, Any]:
    """Catfruit/seeds, behemoth stones/gems and catseye types, grouped, with short and full names."""
    save = core.SaveFile(cc=cc, load=False, gv=core.GameVersion(999999))
    matatabi = core.Matatabi(save)
    names = matatabi.get_names()
    eyes = core.core_data.get_gatya_item_buy(save).get_by_category(5)
    if names is None or not matatabi.matatabi or eyes is None:
        raise RuntimeError('아이템 목록을 불러오지 못했어요. 잠시 뒤 다시 시도하세요.')
    parts: dict[str, list[tuple[int, int, str]]] = {"seeds": [], "fruit": [], "stones": [], "gems": []}
    for i, (fr, name) in enumerate(zip(matatabi.matatabi, names)):
        if fr.group == BEHEMOTH_GROUP:
            key = "stones" if fr.sort < 300 else "gems"   # gems sort 300+, stones (incl. epic) below
        else:
            key = "seeds" if fr.seed else "fruit"
        parts[key].append((fr.sort, i, name or f"#{i}"))

    def group(label: str, rows: list[tuple[int, int, str]], merge: bool = False) -> dict[str, Any]:
        """[first index, short name, full name, [all indexes]]; merge=True folds same-named slots
        into one button and drops placeholder names."""
        rows.sort()
        merged: dict[str, list[int]] = {}
        for _, index, name in rows:
            if merge and (name.strip() in ("", "＠", "@", "Unknown Item") or name.startswith("#")):
                continue
            merged.setdefault(name if merge else f"{index}\0{name}", []).append(index)
        full = [k.split("\0")[-1] for k in merged]
        return {"label": label, "items": [[ids[0], s, f, ids] for ids, s, f in zip(merged.values(), short_labels(full), full)]}

    item_names = core.core_data.get_gatya_item_names(save)
    eye_rows = [(i, i, item_names.get_name(it.id) or f"#{i}") for i, it in enumerate(eyes)]
    buy = core.core_data.get_gatya_item_buy(save)

    def gatya_rows(category: int) -> list[tuple[int, int, str]]:
        items = buy.get_by_category(category) or []
        return [(i, i, item_names.get_name(it.id) or f"#{i}") for i, it in enumerate(items)]

    battle_names = save.battle_items.get_names(save) or []
    battle_rows = [(i, i, n or f"#{i}") for i, n in enumerate(battle_names)]
    maxes = core.core_data.max_value_manager
    return {"ok": True, "cc": cc.get_code(),
            "fruit": [group('씨앗', parts["seeds"]), group('열매', parts["fruit"])],
            "stone": [group('수석', parts["stones"]), group('결정', parts["gems"])],
            "battle": [group('배틀 아이템', battle_rows)],
            "drink": [group('고양이 드링크', gatya_rows(6))],
            "material": [group(ITEM_GROUP_NAMES["material"], gatya_rows(7)[:8]),        # normal materials
                         group(ITEM_GROUP_NAMES["material"] + " Z", gatya_rows(7)[8:])],  # the Z versions
            "medal": [group(ITEM_GROUP_NAMES["medal"], gatya_rows(11))],
            "chest": [group('보물 상자', gatya_rows(12), merge=True)],
            "eye": [group('캣츠아이', eye_rows)],
            "max": {"fruit": maxes.catfruit_new, "stone": maxes.catfruit_new, "eye": maxes.catseyes,
                    "battle": maxes.battle_items, "drink": maxes.catamins, "chest": maxes.treasure_chests,
                    "material": maxes.base_materials, "medal": maxes.labyrinth_medals}}


def set_items(save: core.SaveFile, group: str, values: dict[int, int]) -> str:
    """Set amounts in save.catfruit (fruit, seeds, stones, gems) or save.catseyes by index."""
    maxes = core.core_data.max_value_manager
    if group == "eye":
        target, cap = save.catseyes, maxes.catseyes
    elif group == "drink":
        target, cap = save.catamins, maxes.catamins
    elif group == "chest":
        target, cap = save.treasure_chests, maxes.treasure_chests
    elif group == "battle":
        target, cap = save.battle_items.items, maxes.battle_items
    elif group == "material":
        target, cap = save.ototo.base_materials.materials, maxes.base_materials
    elif group == "medal":
        target, cap = save.labyrinth_medals, maxes.labyrinth_medals
    else:
        target = save.catfruit
        cap = maxes.catfruit_new if save.game_version >= 110400 else maxes.catfruit_old
    set_count, missing = 0, []
    # Keys arrive as strings after the JSON hop from app.py to this worker.
    for index, value in sorted((int(k), int(v)) for k, v in values.items()):
        if index < len(target):
            amount = max(0, min(int(value), cap))
            if group in ("battle", "material"):
                target[index].amount = amount
            else:
                target[index] = amount
            set_count += 1
        else:
            missing.append(f"#{index}")
    if not set_count:
        raise RuntimeError('이 세이브에 해당하는 아이템이 없어요')
    label = f"{ITEM_GROUP_NAMES[group]}: {set_count}{'칸' if group == 'chest' else '종류'} 설정"
    return label + (' (이 세이브에 없는 아이템: {names})'.format(names=", ".join(missing)) if missing else "")


def clear_story_chapter(chapter: Any) -> None:
    """Clear all 48 stages without lowering existing clear counts."""
    for stage in chapter.stages[:STORY_STAGES]:
        stage.clear_times = max(stage.clear_times, 1)
    chapter.progress = max(chapter.progress, STORY_STAGES)


def migrate_data(data_dir: str) -> None:
    """Copy BCSFE's bundled files (locales, themes, max values) into data_dir."""
    core.set_data_dir_path(core.Path(data_dir))
    version_path = core.Path.get_data_folder().add("version.txt")
    if version_path.exists() and version_path.read().to_str().strip() == bcsfe.__version__:
        return
    src = resources.files(bcsfe.__app_name__).joinpath("files")
    bcsfe.copy_to_data_dir(src, src)
    version_path.write(core.Data(bcsfe.__version__))


@contextlib.contextmanager
def game_data_lock(data_dir: str):
    """Serialise game-data downloads so concurrent jobs don't corrupt the cache."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, ".game_data.lock"), "a+") as fh:
        if os.name == "nt":  # Windows
            import msvcrt
            import time

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def reads_back_exactly(original: bytes, cc: core.CountryCode) -> bool:
    """True if BCSFE writes the save back byte-for-byte as it came in.

    BCSFE's own tests require this for every save it supports. A mismatch means
    the save uses a newer format than this BCSFE version understands (new game
    versions sometimes add fields mid-save), so editing it could corrupt it.

    This parses a fresh copy: after a transfer-code download BCSFE stores the
    server's login details in the save, so that copy never matches the original.
    """
    try:
        return core.SaveFile(core.Data(original), cc).to_data().to_bytes() == original
    except Exception:
        traceback.print_exc()
        return False


def attach_backup(result: dict[str, Any], path: str) -> None:
    if os.path.exists(path):
        with open(path, "rb") as fh:
            result["original_b64"] = base64.b64encode(fh.read()).decode()


def snapshot(save: core.SaveFile) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, (attr, _, _) in NUMERIC_FIELDS.items():
        try:
            out[key] = int(getattr(save, attr))
        except Exception:
            pass
    try:
        out["cats_unlocked"] = len(save.cats.get_unlocked_cats())
    except Exception:
        pass
    try:
        out["talent_orbs"] = sum(int(o.value) for o in save.talent_orbs.orbs.values())
    except Exception:
        pass
    try:
        out["catseyes_total"] = sum(int(v) for v in save.catseyes)
        out["catfruit_total"] = sum(int(v) for v in save.catfruit)
    except Exception:
        pass
    try:
        chapters = save.story.get_real_chapters()
        out["story_cleared"] = sum(s.clear_times > 0 for c in chapters for s in c.stages[:STORY_STAGES])
        out["story_treasures"] = sum(s.treasure > 0 for c in chapters for s in c.stages[:STORY_STAGES])
    except Exception:
        pass
    return out


def apply_edits(save: core.SaveFile, edits: dict[str, Any], data_dir: str) -> tuple[list[str], list[str]]:
    done: list[str] = []
    failed: list[str] = []
    maxes = core.core_data.max_value_manager

    def attempt(label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            done.append(label)
        except EOFError:
            # BCSFE asked an interactive question (usually: game data repo unreachable)
            failed.append(f"{label}: 게임 데이터를 내려받지 못했어요 — 나중에 다시 시도하세요")
            traceback.print_exc()
        except Exception as e:  # keep going; report what failed
            failed.append(f"{label}: {e}")
            traceback.print_exc()

    for key, (attr, max_key, managed_type) in NUMERIC_FIELDS.items():
        if edits.get(key) is None:
            continue
        cap = int(getattr(maxes, max_key))
        if key == "platinum_shards":
            cap *= 10
        value = max(0, min(int(edits[key]), cap))

        def set_value(attr=attr, value=value, managed_type=managed_type):
            before = int(getattr(save, attr))
            setattr(save, attr, value)
            if managed_type is not None and value != before:
                core.BackupMetaData(save).add_managed_item(
                    core.ManagedItem.from_change(value - before, managed_type)
                )

        label = LABELS[key] + (f" (최대 {cap:,}(으)로 제한)" if value != int(edits[key]) else "")
        attempt(label, set_value)

    if edits.get("max_battle_items"):
        def f():
            for item in save.battle_items.items:
                item.amount = maxes.battle_items
        attempt("배틀 아이템 최대", f)

    if edits.get("max_catseyes"):
        def f():
            for i in range(len(save.catseyes)):
                save.catseyes[i] = maxes.catseyes
        attempt("캣츠아이 최대", f)

    if edits.get("max_catamins"):
        def f():
            for i in range(len(save.catamins)):
                save.catamins[i] = maxes.catamins
        attempt("고양이 드링크 최대", f)

    if edits.get("max_treasure_chests"):
        def f():
            for i in range(len(save.treasure_chests)):
                save.treasure_chests[i] = maxes.treasure_chests
        attempt("보물 상자 최대", f)

    chapter_ids = edits.get("story_chapters") or []
    names = ", ".join(STORY_CHAPTERS[i] for i in chapter_ids)

    if edits.get("clear_story") and chapter_ids:
        required = sorted({r for i in chapter_ids for r in STORY_REQUIRES.get(i, [])} - set(chapter_ids))
        label = '{names} 클리어'.format(names=names)
        if required:
            label += ' (필요한 챕터도 클리어: {names})'.format(names=", ".join(STORY_CHAPTERS[i] for i in required))

        def f():
            core.StoryChapters.clear_tutorial(save)
            chapters = save.story.get_real_chapters()
            for i in required + list(chapter_ids):
                clear_story_chapter(chapters[i])
        attempt(label, f)

    if edits.get("treasure_level") is not None and chapter_ids:
        level = int(edits["treasure_level"])

        def f():
            chapters = save.story.get_real_chapters()
            for i in chapter_ids:
                for stage in chapters[i].get_valid_treasure_stages():
                    stage.set_treasure(level)
        attempt('보물을 {level}(으)로 설정: {names}'.format(level=TREASURE_LEVELS[level], names=names), f)

    groups = [k for k in MAP_GROUPS if k in (edits.get("clear_maps") or [])]
    needs_game_data = groups or edits.get("unlock_cats") or edits.get("true_form_cats")
    if needs_game_data:
        accept_backup_game_data_repo()

    # Legend/event maps need game data (map names and crown counts).
    if groups:
        crowns = int(edits.get("map_crowns") or 0)
        crown_text = '크라운 {n}개까지'.format(n=crowns) if crowns else '모든 크라운'
        with game_data_lock(data_dir):
            for key in groups:
                result: dict[str, int] = {}

                def f(key=key, result=result):
                    result["n"] = clear_map_group(save, key, crowns)
                before = len(done)
                attempt(MAP_GROUP_NAMES[key], f)
                if len(done) > before:
                    done[-1] = '{group}: 맵 {n}개 클리어 ({crowns})'.format(group=MAP_GROUP_NAMES[key], n=result["n"], crowns=crown_text)

    # Unlock/true-form first, so levelling up "all owned" below includes every unlocked character.
    if edits.get("unlock_cats") or edits.get("true_form_cats"):
        with game_data_lock(data_dir):
            if edits.get("unlock_cats"):
                def f():
                    cats = save.cats.get_cats_obtainable(save)
                    if cats is None:
                        raise RuntimeError("획득 가능한 캐릭터를 찾기 위한 게임 데이터를 내려받지 못했어요")
                    for cat in cats:
                        cat.unlock(save)
                attempt("획득 가능한 모든 캐릭터 획득", f)

            if edits.get("true_form_cats"):
                def f():
                    cats = save.cats.get_unlocked_cats()
                    set_forms = core.core_data.config.get_bool(core.ConfigKey.SET_CAT_CURRENT_FORMS)
                    save.cats.true_form_cats(save, cats, False, set_forms)
                attempt("보유 캐릭터 제3형태 진화", f)

    # Limited edition and dummy data characters (never obtainable in any country).
    if edits.get("add_limited_cats") or edits.get("add_dummy_cats"):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            lists: dict[str, Any] = {}
            for key in ("add_limited_cats", "add_dummy_cats"):
                if not edits.get(key):
                    continue
                result: dict[str, int] = {}

                def unlock_special(key: str = key, result: dict[str, int] = result, lists: dict[str, Any] = lists) -> None:
                    ids = lists.setdefault("ids", special_cats(save))[key]
                    for cat_id in ids:
                        save.cats.cats[cat_id].unlock(save)
                    result["n"] = len(ids)
                before = len(done)
                attempt(SPECIAL_CAT_TEXT[key].format(n="").replace("  ", " "), unlock_special)
                if len(done) > before:
                    done[-1] = SPECIAL_CAT_TEXT[key].format(n=result["n"])

    # Individual characters and rarity groups (unlocking needs game data too).
    if edits.get("add_cats") or edits.get("remove_cats") or edits.get("add_rarities"):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            edit_cats(save, edits, attempt, done)

    # Catfruit & seeds, behemoth stones & gems, catseyes (indexes into the save's lists).
    for group in ("fruit", "stone", "eye", "battle", "drink", "chest", "material", "medal"):
        if edits.get(f"items_{group}"):
            item_out: dict[str, str] = {}
            before = len(done)
            attempt(ITEM_GROUP_NAMES[group],
                    lambda g=group, o=item_out: o.update(label=set_items(save, g, edits[f"items_{g}"])))
            if len(done) > before and item_out.get("label"):
                done[-1] = item_out["label"]

    # Talent orbs (orb names and categories come from the game data).
    if edits.get("orbs"):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            orb_out: dict[str, str] = {}
            before = len(done)
            attempt('본능 구슬', lambda: orb_out.update(label=set_talent_orbs(save, edits["orbs"])))
            if len(done) > before and orb_out.get("label"):
                done[-1] = orb_out["label"]

    # Level up after adding/removing, so "all owned" includes new characters.
    if edits.get("upgrade"):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            out: dict[str, str] = {}
            before = len(done)
            def level_up() -> None:
                out["label"] = level_up_cats(save, edits["upgrade"], edits.get("remove_cats") or [])
                # Account styles: claim the rank rewards the new levels reach (they raise the level caps), then level up again.
                for _ in range(8 if edits.get("rank_caps") else 0):
                    if not claim_rank_rewards(save):
                        break
                    out["label"] = level_up_cats(save, edits["upgrade"], edits.get("remove_cats") or [])
            attempt('레벨업', level_up)
            if len(done) > before and out.get("label"):
                done[-1] = out["label"]

    # Forms & talents, special skills, Ototo, Gamatoto, endless items, more stages,
    # progress and fixes (extras.py); most need game data.
    if any(edits.get(k) for k in ("forms", "skills", "ototo", "gamatoto", "endless", "more_stages", "progress", "fixes")):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            extras.apply(core, save, edits, attempt, done)

    return done, failed


# app.py drops CANCEL_FLAG in the job folder to cancel; the worker drops EDITING_MARK once
# edits start, after which a transfer-code edit can't be cancelled safely.
CANCEL_FLAG, EDITING_MARK = "cancel", "editing"
CANCEL_TEXT = {'early': '세이브를 내려받기 전에 편집을 취소했어요. 아무것도 바뀌지 않았고 이어하기 코드도 그대로 쓸 수 있어요.', 'file': '편집을 취소했어요. 아무것도 바뀌지 않았어요.', 'reuploaded': '편집을 취소했어요. 아무것도 바뀌지 않았어요. 세이브를 이미 내려받아서 이어하기 코드가 사용됐기 때문에 바뀌지 않은 세이브를 다시 업로드했어요. 아래 새 코드를 입력하세요.', 'upload_fail': '편집을 취소했지만 바뀌지 않은 세이브를 다시 업로드하지 못했어요. 이어하기 코드는 이미 사용됐으니 아래에서 원본 백업을 내려받아 꼭 보관하세요.', 'too_late': '취소 요청이 늦게 도착했어요. 편집이 이미 적용돼서 저장됐어요.'}


def cancel_requested(job: dict[str, Any]) -> bool:
    return os.path.exists(os.path.join(job["job_dir"], CANCEL_FLAG))


NEW_ACCOUNT_TEXT = {'country': '새 계정을 만들 게임의 국가(버전)를 고르세요.', 'data': '새 세이브를 준비하지 못했어요(이 버전의 게임 데이터를 내려받지 못했어요). 나중에 다시 시도하세요.', 'server': '게임 서버에서 새 계정을 만들지 못했어요. 나중에 다시 시도하세요.', 'upload': '새 계정은 만들었지만 세이브를 업로드하지 못했어요. 아래에서 세이브를 내려받거나 다시 시도하세요.', 'created': '새 계정을 만들었어요 (실험 기능)', 'mythic_next': '일부 스테이지(레전드, 이벤트, 연속 스테이지, 마계편, 좀비 습격)는 게임이 이 계정을 연 뒤에 생겨요. 이것들도 클리어하려면: 계정을 게임에서 한 번 열고 이어하기 코드를 만든 뒤(기종변경 → 데이터 인계 시작), 이어하기 코드 탭에서 스테이지 수정 → “모든 스테이지 완료”를 켜고 입력하세요.'}
PRESET_NAMES = {'starter': '루키', 'advanced': '베테랑', 'elite': '챔피언', 'ultimate': '신화'}
PRESET_TEXT = '계정 스타일: {name}'
STARTER_CATS = list(range(1, 9))  # the basic Cats after Cat itself: Tank, Axe, Gross, Cow, Bird, Fish, Lizard, Titan


def ready_map_groups(save: core.SaveFile) -> list[str]:
    """Legend/event groups whose stage tables exist in this save (a brand-new save has none until the game opens it)."""
    from bcsfe.cli.edits import map as map_edits

    ready = []
    for key, (attr, _code, _base, _type) in MAP_GROUPS.items():
        chapters = save
        try:
            for part in attr.split("."):
                chapters = getattr(chapters, part)
            if key == "uncanny":
                chapters = chapters.chapters
            if map_edits.get_total_maps(chapters) > 0:
                ready.append(key)
        except Exception:  # noqa: BLE001 - a missing table just means "not ready"
            continue
    return ready


def preset_edits(save: core.SaveFile, name: str) -> dict[str, Any]:
    """The edits behind an account style on the New account tab (random amounts are rolled here).
    Keys stay starter/advanced/elite/ultimate; the page shows Rookie, Veteran, Champion and Mythic."""
    maxes = core.core_data.max_value_manager
    roll = random.SystemRandom().randint
    cap = lambda key: int(getattr(maxes, key))  # noqa: E731
    fruit_cap = maxes.catfruit_new if save.game_version >= 110400 else maxes.catfruit_old
    battle, fruit = len(save.battle_items.items), len(save.catfruit)
    story = lambda n, treasure: {"clear_story": True, "story_chapters": list(range(n)), "treasure_level": treasure}  # noqa: E731
    if name == "starter":  # Rookie
        return {"catfood": roll(8_000, 15_000), "normal_tickets": roll(30, 60), "rare_tickets": roll(15, 35),
                "xp": roll(3_000_000, 12_000_000),
                "items_battle": {i: roll(cap("battle_items") // 5, cap("battle_items") * 2 // 5) for i in range(battle)},
                "add_cats": STARTER_CATS, **story(1, 2)}
    if name == "advanced":  # Veteran
        return {"catfood": roll(25_000, 35_000), "rare_tickets": roll(50, 100), "platinum_tickets": roll(2, 5),
                "legend_tickets": roll(1, 3), "xp": roll(25_000_000, 60_000_000), "np": roll(500, 1_500),
                "max_battle_items": True, "items_fruit": {i: roll(10, 30) for i in range(fruit)},
                "unlock_cats": True, "upgrade": {"target": "all", "base": 30}, "rank_caps": True,
                "progress": {"slots": 10}, **story(3, 3)}
    champion = {"catfood": cap("catfood"), "xp": cap("xp"), "leadership": cap("leadership"), "np": cap("np"),
                "rare_tickets": min(299, cap("rare_tickets")), "platinum_tickets": cap("platinum_tickets"),
                "legend_tickets": cap("legend_tickets"), "max_battle_items": True, "max_catseyes": True,
                "max_catamins": True, "items_fruit": {i: fruit_cap for i in range(fruit)},
                "unlock_cats": True, "upgrade": {"target": "all", "base": "max"}, "rank_caps": True,
                "forms": {"true": "evolve", "target": "all", "ids": []},
                "skills": {"base": "max", "plus": "max", "all": True, "ids": []},
                "ototo": {"engineers": "max"}, "progress": {"slots": "max"}, **story(6, 3)}
    if name == "elite":  # Champion
        return champion
    if name == "ultimate":  # Mythic
        return {**champion, "normal_tickets": cap("normal_tickets"), "rare_tickets": cap("rare_tickets"),
                "upgrade": {"target": "all", "base": "max", "plus": "max"},
                "forms": {"true": "evolve", "fourth": "evolve", "guide": "add", "target": "all", "ids": []},
                "orbs": {"all": True, "count": cap("talent_orbs")},
                "items_material": {i: cap("base_materials") for i in range(len(save.ototo.base_materials.materials))},
                "max_treasure_chests": True, "gamatoto": {"level": "max"},
                # Legend/event stages, talents, cannons, Aku and outbreaks only exist once the game has opened
                # the account, so they can't be set on a brand-new save (use Transfer code for them later).
                "progress": {"slots": "max", "medals": "add"}, **story(len(STORY_CHAPTERS), 3),
                **mythic_stages(save)}
    raise ValueError(name)


def mythic_stages(save: core.SaveFile) -> dict[str, Any]:
    """Every stage clear the save can take now; the rest wait for step 2 (Complete all stages)."""
    groups = ready_map_groups(save)
    stages: dict[str, Any] = {"tutorial": True}
    if "legend" in groups or "event" in groups:
        stages["aku_realm"] = True
    if save.aku.chapters and save.aku.chapters[0].chapters:
        stages["aku_chapters"] = True
    if save.outbreaks.chapters:
        stages["outbreaks"] = True
    edits: dict[str, Any] = {"more_stages": stages}
    if groups:
        edits.update(clear_maps=groups, map_crowns=0)
    return edits


# Freshly started game saves (made by the game itself, login details removed), one per country when
# available. A new account is a copy of one moved to a brand-new account code: an empty save built from
# scratch lacks dozens of things the game sets up on a new game, and the game then fails while loading
# ("Cannot access the network").
NEW_ACCOUNT_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "new_account_templates")


def template_save(cc: core.CountryCode) -> core.SaveFile | None:
    """A copy of the freshly started save for this country (or another country's, converted)."""
    code = cc.get_code()
    names = [f"{code}.bin.gz"] + sorted(n for n in os.listdir(NEW_ACCOUNT_TEMPLATES) if n.endswith(".bin.gz")) \
        if os.path.isdir(NEW_ACCOUNT_TEMPLATES) else []
    for name in names:
        path = os.path.join(NEW_ACCOUNT_TEMPLATES, name)
        if not os.path.exists(path):
            continue
        with gzip.open(path, "rb") as fh:
            save = core.SaveFile(core.Data(fh.read()), cc=core.CountryCode.from_code(name.split(".")[0]))
        if save.cc.get_code() != code:
            save.set_cc(cc)
        save.inquiry_code = ""
        save.password_refresh_token = ""
        now = datetime.datetime.now()
        save.energy_penalty_timestamp = now.timestamp()  # the server reads this as the account's creation time
        return save
    return None


def new_save(cc: core.CountryCode) -> core.SaveFile:
    """A save for a brand-new account: a copy of a freshly started game save (see template_save)."""
    save = template_save(cc)
    if save is None:
        raise RuntimeError("no new-account base copy in new_account_templates")
    return save


def run(job: dict[str, Any]) -> dict[str, Any]:
    data_dir = job["data_dir"]
    job_dir = job["job_dir"]
    os.makedirs(job_dir, exist_ok=True)

    migrate_data(data_dir)
    # Per-job config/log so nothing user-specific lands in the shared folder.
    core.set_log_path(core.Path(os.path.join(job_dir, "bcsfe.log")))
    core.set_transfer_backup_path(core.Path(os.path.join(job_dir, "original_SAVE_DATA")))
    core.core_data.init_data()

    cc = core.CountryCode.from_code(job["cc"]) if job.get("cc") else None
    result: dict[str, Any] = {"ok": False}

    if job["mode"] in ("catalog", "orbs", "items", "extras", "cat_names"):
        accept_backup_game_data_repo()
        with game_data_lock(data_dir):
            build = {"catalog": cat_catalog, "orbs": orb_catalog, "items": item_catalog,
                     "extras": lambda c: extras.catalog(core, c),
                     "cat_names": lambda c: shop_edits.cat_names(core, c)}[job["mode"]]
            return build(cc or core.CountryCode.from_code("en"))

    # ---- load the save ----------------------------------------------------
    if job["mode"] == "new":
        # Experimental: a brand-new account from an empty save.
        if cancel_requested(job):
            return {"ok": False, "cancelled": True, "error": CANCEL_TEXT["file"]}
        if cc is None:
            return {"ok": False, "error": NEW_ACCOUNT_TEXT["country"]}
        try:
            accept_backup_game_data_repo()
            with game_data_lock(data_dir):
                save = new_save(cc)
        except Exception:
            traceback.print_exc()
            return {"ok": False, "error": NEW_ACCOUNT_TEXT["data"]}
        if not core.ServerHandler(save, print=False).create_new_account():
            return {"ok": False, "error": NEW_ACCOUNT_TEXT["server"]}
        original = save.to_data().to_bytes()
        if job.get("preset") in PRESET_NAMES:  # the account style first; edits picked on the page override it
            job["edits"] = {**preset_edits(save, job["preset"]), **(job.get("edits") or {})}
    elif job["mode"] == "codes":
        backup = os.path.join(job_dir, "original_SAVE_DATA")
        if cancel_requested(job):
            return {"ok": False, "cancelled": True, "error": CANCEL_TEXT["early"]}
        if cc is None:
            return {"ok": False, "error": "국가를 선택해야 해요."}
        try:
            handler, req = core.ServerHandler.from_codes(
                job["transfer_code"].strip(),
                job["confirmation_code"].strip(),
                cc,
                core.GameVersion(120200),
                print=False,
                save_backup=True,
            )
        except Exception:
            # The save may already be downloaded (and the code used up) when
            # parsing fails, so always hand back the backup if there is one.
            traceback.print_exc()
            if not os.path.exists(backup):
                raise
            out = {"ok": False, "error": (
                "세이브를 내려받았지만 에디터가 읽지 못했어요 — 에디터가 아직 지원하지 않는 새 게임 버전일 수 "
                "있어요. 이어하기 코드는 이미 사용됐으니 아래에서 원본 백업을 내려받아 꼭 보관하세요."
            )}
            attach_backup(out, backup)
            return out
        if handler is None:
            if req is None:
                return {"ok": False, "error": "게임 서버에 연결할 수 없어요. 나중에 다시 시도하세요."}
            hint = " (일본판과 대만판 코드는 헷갈리기 쉬워요 — 국가를 확인하세요.)" if job["cc"] in ("jp", "tw") else ""
            return {"ok": False, "error": "이어하기 코드, 인증번호 또는 국가가 올바르지 않아요." + hint}
        save = handler.save_file
        attach_backup(result, backup)
        with open(backup, "rb") as fh:
            original = fh.read()
    else:
        raw = core.Data(base64.b64decode(job["file_b64"]))
        try:
            save = core.SaveFile(raw, cc)
        except core.CantDetectSaveCCError:
            return {"ok": False, "error": "세이브의 국가를 감지하지 못했어요. 게임 국가를 선택하고 다시 시도하세요."}
        except Exception as e:
            return {"ok": False, "error": f"세이브 파일을 읽을 수 없어요: {e}"}
        original = raw.to_bytes()

    result["country"] = save.cc.get_code()
    result["game_version"] = save.game_version.to_string()
    result["before"] = snapshot(save)

    # ---- make sure this BCSFE version fully understands the save ------------
    if not reads_back_exactly(original, save.cc):
        version = result["game_version"]
        print(f"save does not round-trip (game version {version})", file=sys.stderr)
        if job["mode"] == "new":
            return {"ok": False, "error": NEW_ACCOUNT_TEXT["data"]}
        if job["mode"] != "codes":
            return {"ok": False, "error": (
                f"이 세이브는 게임 버전 {version}이라 에디터가 아직 정확히 읽을 수 없어요. "
                "아무것도 바꾸지 않았어요."
            )}
        # The transfer code is already used up, so re-upload the untouched
        # original to give the player working codes again.
        save.to_data = lambda: core.Data(original)
        codes = core.ServerHandler(save, print=False).get_codes()
        result["after"] = result["before"]
        result["done"], result["failed"] = [], []
        if codes is None:
            result["error"] = (
                "세이브를 내려받았지만 에디터가 읽지 못했어요 — 에디터가 아직 지원하지 않는 새 게임 버전일 수 "
                "있어요. 이어하기 코드는 이미 사용됐으니 아래에서 원본 백업을 내려받아 꼭 보관하세요."
            )
            return result
        result["transfer_code"], result["confirmation_code"] = codes
        result["error"] = (
            f"이 세이브는 게임 버전 {version}이라 에디터가 아직 정확히 읽을 수 없어서 아무것도 "
            "편집하지 않았어요. 세이브는 바뀌지 않은 그대로 다시 업로드했어요 — 아래 새 코드를 "
            "입력하면 게임에서 되찾을 수 있어요."
        )
        result["ok"] = True
        return result

    # ---- cancelled before any edit? -----------------------------------------
    if cancel_requested(job):
        if job["mode"] != "codes":
            return {"ok": False, "cancelled": True, "error": CANCEL_TEXT["file"]}
        # The transfer code is used up, so upload the untouched save again for new codes.
        save.to_data = lambda: core.Data(original)
        codes = core.ServerHandler(save, print=False).get_codes()
        result["after"] = result["before"]
        result["done"], result["failed"], result["cancelled"] = [], [], True
        if codes is None:
            result["error"] = CANCEL_TEXT["upload_fail"]
            return result
        result["transfer_code"], result["confirmation_code"] = codes
        result["notice"] = CANCEL_TEXT["reuploaded"]
        result["ok"] = True
        return result
    open(os.path.join(job_dir, EDITING_MARK), "w").close()

    # ---- edit ---------------------------------------------------------------
    edits = job.get("edits") or {}
    try:
        if shop_edits.needs_game_data(edits):
            accept_backup_game_data_repo()
            with game_data_lock(data_dir):
                edits = shop_edits.expand(core, save, edits)
        else:
            edits = shop_edits.expand(core, save, edits)
    except Exception as e:  # report it like any other failed edit; the save still gets re-uploaded
        traceback.print_exc()
        result.setdefault("pre_failed", []).append(f"{type(e).__name__}: {e}")
        edits = {}
    done, failed = apply_edits(save, edits, data_dir)
    failed = result.pop("pre_failed", []) + failed
    result["done"] = done
    result["failed"] = failed
    result["after"] = snapshot(save)

    # ---- output -------------------------------------------------------------
    if job["mode"] in ("codes", "new"):
        if job.get("new_account"):
            if not core.ServerHandler(save, print=False).create_new_account():
                result["failed"].append("새 계정: 서버가 거부해서 기존 계정에 업로드했어요")
            else:
                result["done"].append("새 계정(문의코드)으로 이동")
        codes = core.ServerHandler(save, print=False).get_codes()
        if codes is None:
            if job["mode"] == "new":
                result["error"] = NEW_ACCOUNT_TEXT["upload"]
                result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()
                return result
            result["error"] = (
                "편집은 적용됐지만 업로드에 실패했어요. 아래에서 원본 세이브를 내려받으세요 — "
                "세이브 관리 도구로 복원하거나 다시 시도할 수 있어요."
            )
            result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()
            return result
        result["transfer_code"], result["confirmation_code"] = codes
    else:
        result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()

    if job["mode"] == "new":
        result["done"].insert(0, NEW_ACCOUNT_TEXT["created"])
        if job.get("preset") in PRESET_NAMES:
            result["done"].insert(1, PRESET_TEXT.format(name=PRESET_NAMES[job["preset"]]))
        if job.get("preset") == "ultimate" and not result.get("error") and len(ready_map_groups(save)) < len(MAP_GROUPS):
            result["notice"] = NEW_ACCOUNT_TEXT["mythic_next"]
        result["new_account"] = True
        result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()  # a copy to keep
    if cancel_requested(job):
        result["notice"] = CANCEL_TEXT["too_late"]
    result["ok"] = True
    return result


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        out = run(job)
    except Exception as e:
        traceback.print_exc()
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _REAL_STDOUT.write(json.dumps(out))
    _REAL_STDOUT.flush()


if __name__ == "__main__":
    main()
