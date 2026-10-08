"""Automatic order fulfilment: turns a shop order into an editor job and runs it.

The editor code lives in editor/ (copied from the Battle-Cats-Editor-Site-KR repo).
It runs in its own process per order, like on the site: the bot sends the job as JSON
and reads the result. Nothing talks to the website.

Each automatic product has an `auto` key from AUTO below. Character and treasure items
take their quantity from what the buyer typed (names / chapters) instead of a number.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
WORKER = HERE / "editor" / "worker.py"
DATA_DIR = os.path.abspath(os.environ.get("BCSFE_DATA_DIR", str(HERE / "bcsfe-data")))
BACKUP_DIR = os.path.abspath(os.environ.get("SAVE_BACKUP_DIR", str(HERE / "save-backups")))
GAME_CC = os.environ.get("GAME_CC", "kr")  # game version the shop edits (kr, jp, en, tw)
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "600"))
BACKUP_DAYS = 14

STORY = {"world": [0, 1, 2], "future": [3, 4, 5], "cosmos": [6, 7, 8]}
STORY_NAMES = {"세계": 0, "미래": 3, "우주": 6}
ERA_NAMES = {"world": "세계편", "future": "미래편", "cosmos": "우주편"}

Builder = Callable[[int, list[int]], dict[str, Any]]


def _story(era: str) -> Builder:
    """Picked chapters of one era (picks are 0, 1, 2): cleared, treasures at the best level."""
    return lambda q, x: {"clear_story": True, "story_chapters": [STORY[era][i] for i in x], "treasure_level": 3}


EVOLVE = {"true": "evolve", "fourth": "evolve"}  # 3rd and 4th forms, where the character has them

# auto key -> (edits builder, how the quantity is chosen, unlimited)
#   how: "number" | "cats" | "chapters" (보물작, any era) | "era:<world|future|cosmos>" | "fixed"
#   unlimited: the editor runs with the Unlimited editor's limits (storage maxima, not the game's).
AUTO: dict[str, tuple[Builder, str, bool]] = {
    # ---- 재화
    "catfood_45000": (lambda q, x: {"atleast": {"catfood": 45_000}}, "fixed", False),
    "catfood_100k": (lambda q, x: {"atleast": {"catfood": 100_000}}, "fixed", True),
    "catfood_1m": (lambda q, x: {"atleast": {"catfood": 1_000_000}}, "fixed", True),
    "catfood_2100m": (lambda q, x: {"atleast": {"catfood": 2_100_000_000}}, "fixed", True),
    "xp_add": (lambda q, x: {"add": {"xp": 100_000_000 * q}}, "number", True),
    "np_add": (lambda q, x: {"add": {"np": 1000 * q}}, "number", True),
    "catseyes_add": (lambda q, x: {"catseyes_add": 998 * q}, "number", True),
    "fruitseed_add": (lambda q, x: {"fruitseed_add": 998 * q}, "number", True),
    "stonegem_add": (lambda q, x: {"stonegem_add": 998 * q}, "number", True),
    "leadership_add": (lambda q, x: {"add": {"leadership": 999 * q}}, "number", True),
    "orbs_998": (lambda q, x: {"orbs": {"all": True, "count": 998}}, "fixed", False),
    "battle_add": (lambda q, x: {"battle_add": 998 * q}, "number", True),
    "drink_add": (lambda q, x: {"drink_add": 999 * q}, "number", True),
    "skills_max": (lambda q, x: {"skills": {"base": "max", "plus": "max", "all": True, "ids": []}}, "fixed", False),
    # ---- 캐릭터
    "all_cats_unlock": (lambda q, x: {"unlock_cats": True}, "fixed", False),
    "all_cats_upgrade": (lambda q, x: {"upgrade": {"target": "all", "base": "max", "plus": "max"},
                                       "rank_caps": True}, "fixed", False),
    "all_cats_evolve": (lambda q, x: {"forms": {**EVOLVE, "target": "all", "ids": []}}, "fixed", False),
    "cat_add": (lambda q, x: {"add_cats": x}, "cats", False),
    "cat_remove": (lambda q, x: {"remove_cats": x}, "cats", False),
    "cat_upgrade": (lambda q, x: {"upgrade": {"target": "picked", "base": "max", "plus": "max", "ids": x},
                                  "rank_caps": True}, "cats", False),
    "cat_evolve": (lambda q, x: {"forms": {**EVOLVE, "target": "picked", "ids": x}}, "cats", False),
    # ---- 스테이지 (장마다 클리어 + 보물)
    "story_world_ch": (_story("world"), "era:world", False),
    "story_future_ch": (_story("future"), "era:future", False),
    "story_cosmos_ch": (_story("cosmos"), "era:cosmos", False),
    # ---- 티켓
    "normal_tickets_add": (lambda q, x: {"add": {"normal_tickets": 3000 * q}}, "number", True),
    "rare_tickets_add": (lambda q, x: {"add": {"rare_tickets": 300 * q}}, "number", True),
    "platinum_9": (lambda q, x: {"add": {"platinum_tickets": 9}}, "fixed", True),
    "platinum_100": (lambda q, x: {"add": {"platinum_tickets": 100}}, "fixed", True),
    "legend_4": (lambda q, x: {"add": {"legend_tickets": 4}}, "fixed", True),
    "legend_50": (lambda q, x: {"add": {"legend_tickets": 50}}, "fixed", True),

    # Older items: no longer in the catalog, kept so orders already queued still run.
    "rare_tickets_299": (lambda q, x: {"rare_tickets": 299}, "fixed", False),
    "normal_tickets_2999": (lambda q, x: {"normal_tickets": 2999}, "fixed", False),
    "platinum_tickets": (lambda q, x: {"add": {"platinum_tickets": q}}, "number", False),
    "legend_tickets": (lambda q, x: {"add": {"legend_tickets": q}}, "number", False),
    "np_9999": (lambda q, x: {"np": 9999}, "fixed", False),
    "fruit": (lambda q, x: {"fruit_add": 200 * q}, "number", False),
    "stones": (lambda q, x: {"stone_add": 200 * q}, "number", False),
    "xp": (lambda q, x: {"add": {"xp": 100_000_000 * q}}, "number", False),
    "catseyes_9999": (lambda q, x: {"catseyes_set": 9999}, "fixed", False),
    "story_world": (lambda q, x: {"clear_story": True, "story_chapters": STORY["world"]}, "fixed", False),
    "story_future": (lambda q, x: {"clear_story": True, "story_chapters": STORY["future"]}, "fixed", False),
    "story_cosmos": (lambda q, x: {"clear_story": True, "story_chapters": STORY["cosmos"]}, "fixed", False),
    "treasure": (lambda q, x: {"story_chapters": x, "treasure_level": 3}, "chapters", False),
    "all_cats_talents": (lambda q, x: {"forms": {"talents": "max", "target": "all", "ids": []}}, "fixed", False),
    "cat_talents": (lambda q, x: {"forms": {"talents": "max", "target": "picked", "ids": x}}, "cats", False),
    "battle_9999": (lambda q, x: {"max_battle_items": True}, "fixed", False),
    "battle_endless": (lambda q, x: {"endless_all": True}, "fixed", False),
    "leadership": (lambda q, x: {"add": {"leadership": 100 * q}}, "number", False),
    "gamatoto_max": (lambda q, x: {"gamatoto": {"level": "max"}}, "fixed", False),
    "gamatoto_helpers": (lambda q, x: {"helpers_top": q}, "number", False),
    "gold_pass": (lambda q, x: {"progress": {"gold_pass": "give"}}, "fixed", False),
}


def quantity_mode(auto: str | None) -> str | None:
    return AUTO[auto][1] if auto in AUTO else None


def picks_names(mode: str | None) -> bool:
    """Whether the buyer types what they want (names / chapters) and the count comes from that."""
    return mode is not None and (mode in ("cats", "chapters") or mode.startswith("era:"))


def is_unlimited(auto: str | None) -> bool:
    return bool(auto in AUTO and AUTO[auto][2])


def build_edits(auto: str, quantity: int, picks: list[int]) -> dict[str, Any]:
    return AUTO[auto][0](quantity, picks)


# ------------------------------------------------------------ characters ----
class CatNames:
    """Character names (every form) from the game data, cached on disk."""

    def __init__(self) -> None:
        self.path = Path(DATA_DIR) / f"shop_cat_names_{GAME_CC}.json"
        self.cats: list[tuple[int, list[str]]] = []
        self.loaded_at = 0.0
        self._load_file()

    def _load_file(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.cats = [(int(i), names) for i, names in data["cats"]]
            self.loaded_at = self.path.stat().st_mtime
        except (OSError, ValueError, KeyError):
            pass

    @property
    def ready(self) -> bool:
        return bool(self.cats)

    async def refresh(self) -> bool:
        result = await run_worker({"mode": "cat_names", "cc": GAME_CC})
        if not result.get("ok"):
            return False
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path.write_text(json.dumps({"cats": result["cats"]}, ensure_ascii=False), encoding="utf-8")
        self._load_file()
        return True

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()

    def name(self, cat_id: int) -> str:
        for i, names in self.cats:
            if i == cat_id:
                return names[0] if names else f"#{cat_id}"
        return f"#{cat_id}"

    def resolve(self, text: str) -> tuple[list[int], str | None]:
        """'탱크 고양이, 고양이 무트, 25' -> ([1, 25, ...], None) or ([], error message)."""
        tokens = [t.strip() for t in re.split(r"[,，\n/]+", text) if t.strip()]
        if not tokens:
            return [], "캐릭터 이름을 입력해 주세요."
        named = [(i, [n for n in names if n and not re.fullmatch(r"\d+_\d+", n)]) for i, names in self.cats]
        ids: list[int] = []
        for token in tokens:
            if token.isdigit():
                ids.append(int(token))
                continue
            if not self.ready:
                return [], "캐릭터 목록을 아직 불러오는 중이에요. 잠시 뒤 다시 시도하거나 캐릭터 ID(숫자)로 입력해 주세요."
            want = self._norm(token)
            exact = {i for i, names in named if any(self._norm(n) == want for n in names)}
            found = exact or {i for i, names in named if any(want in self._norm(n) for n in names)}
            if len(found) == 1:
                ids.append(found.pop())
            elif not found:
                return [], f"'{token}' 캐릭터를 찾지 못했어요. 게임에 나오는 이름 그대로 입력해 주세요."
            else:
                options = ", ".join(sorted({self.name(i) for i in found})[:6])
                return [], f"'{token}'에 해당하는 캐릭터가 여러 개예요: {options}… 더 정확히 입력해 주세요."
        unique = list(dict.fromkeys(ids))
        return unique, None


def parse_chapters(text: str) -> tuple[list[int], str | None]:
    """'세계편 1장, 미래편 3장' -> ([0, 5], None)."""
    found: list[int] = []
    for era, chapter in re.findall(r"(세계|미래|우주)\s*편?\s*제?\s*([123])\s*장?", text):
        index = STORY_NAMES[era] + int(chapter) - 1
        if index not in found:
            found.append(index)
    if not found:
        return [], "보물작할 장을 '세계편 1장, 미래편 2장'처럼 입력해 주세요."
    return found, None


def parse_era_chapters(era: str, text: str) -> tuple[list[int], str | None]:
    """For one era: '1장, 3장' / '1,2,3' / '전부' -> ([0, 2], None) (chapter 1 = 0)."""
    if re.search(r"전부|전체|모두|올", text):
        return [0, 1, 2], None
    found: list[int] = []
    for chapter in re.findall(r"[123]", re.sub(r"[456789]\d*|\d{2,}", " ", text)):
        if int(chapter) - 1 not in found:
            found.append(int(chapter) - 1)
    if not found:
        return [], "클리어할 장을 '1장, 2장' 또는 '1,2,3'처럼 입력해 주세요 (1~3장)."
    return sorted(found), None


def era_chapter_names(era: str, picks: list[int]) -> str:
    return ", ".join(f"{ERA_NAMES[era]} {i + 1}장" for i in picks)


def chapter_names(indexes: list[int]) -> str:
    eras = ["세계편", "미래편", "우주편"]
    return ", ".join(f"{eras[i // 3]} {i % 3 + 1}장" for i in indexes)


# -------------------------------------------------------------- running ----
_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:  # one edit at a time: the server has little memory
        _semaphore = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT_JOBS", "1")))
    return _semaphore


async def run_worker(job: dict[str, Any]) -> dict[str, Any]:
    """Run editor/worker.py for one job and return its JSON result."""
    async with _sem():
        os.makedirs(DATA_DIR, exist_ok=True)
        job_dir = tempfile.mkdtemp(prefix="job-", dir=DATA_DIR)
        payload = json.dumps({**job, "data_dir": DATA_DIR, "job_dir": job_dir}).encode()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(WORKER), cwd=str(WORKER.parent),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), JOB_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            # Keep the job folder: it may hold the downloaded original save.
            return {"ok": False, "timeout": True, "job_dir": job_dir,
                    "error": "편집 시간이 너무 오래 걸려서 멈췄어요."}
        try:
            result = json.loads(out.decode() or "{}")
        except ValueError:
            result = {"ok": False, "error": "편집기가 올바른 결과를 돌려주지 않았어요."}
        if not result.get("ok") and not result.get("original_b64"):
            tail = err.decode(errors="replace")[-1500:]
            result.setdefault("log", tail)
        shutil.rmtree(job_dir, ignore_errors=True)
        return result


def save_backup(order_id: int, original_b64: str | None) -> str | None:
    """Keep the save as it was before the edit, so an admin can restore it if needed."""
    if not original_b64:
        return None
    import base64

    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"order-{order_id}-{int(time.time())}.bin")
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(original_b64))
    os.chmod(path, 0o600)
    return path


def prune_backups() -> None:
    cutoff = time.time() - BACKUP_DAYS * 86400
    try:
        for name in os.listdir(BACKUP_DIR):
            path = os.path.join(BACKUP_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass
