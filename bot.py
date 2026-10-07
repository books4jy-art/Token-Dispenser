"""자동 구매 봇 — a Korean point shop (자판기) bot for Discord.

Users top up points by bank transfer: `/충전` files a request, and when the bank's
deposit notification reaches the /deposit webhook (forwarded from the admin's
phone), the matching request is approved and the points are added automatically.
Points buy products from the shop: stock items (codes/accounts, sent by DM),
roles, or services an admin fulfils by hand.

The real money stays in the bank account. The bot keeps a 금고 (vault) ledger of
how much was paid in and how much admins have taken out (`/금고`).

Run:  python bot.py   (settings come from .env — see .env.example)
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import time

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import bank
import catalog
import fulfil
from db import Database, ShopError, names_match

# ---------------------------------------------------------------- config ----
TOKEN = os.environ.get("DISCORD_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "shop.db")
# The server whose bank account the deposit webhook belongs to.
DEPOSIT_GUILD_ID = int(os.environ.get("DEPOSIT_GUILD_ID", "0") or 0)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("PORT", os.environ.get("WEBHOOK_PORT", "8080")))
# The https address phones reach the webhook at (set by deploy/https.sh), e.g. https://myshop.duckdns.org
WEBHOOK_PUBLIC_URL = os.environ.get("WEBHOOK_PUBLIC_URL", "").rstrip("/")
# A charge request waits this long for its deposit before it expires.
CHARGE_EXPIRE_MINUTES = int(os.environ.get("CHARGE_EXPIRE_MINUTES", "60"))
# Register commands to this server instantly (global registration can take a while).
DEV_GUILD_ID = int(os.environ.get("DEV_GUILD_ID", "0") or 0)

KST = datetime.timezone(datetime.timedelta(hours=9))
MAX_CHARGE = 10_000_000

COLOR_OK = 0x57F287
COLOR_INFO = 0x5865F2
COLOR_WARN = 0xFEE75C
COLOR_ERR = 0xED4245

KIND_LABEL = {
    "stock": "자동 전송", "role": "역할 지급", "manual": "관리자 처리", "lifetime": "평생 무료 키",
}
STATUS_LABEL = {"done": "완료", "pending": "처리 대기", "refunded": "환불됨"}

log = logging.getLogger("shopbot")
db = Database(DB_PATH)
cat_names = fulfil.CatNames()


# --------------------------------------------------------------- helpers ----
def setting_int(guild_id: int, key: str, default: int) -> int:
    try:
        return int(db.get_setting(guild_id, key, str(default)))
    except (TypeError, ValueError):
        return default


def points_for(guild_id: int, won: int) -> int:
    """Points given for a top-up of `won`: 1원 = 1P plus the server's bonus %."""
    return won + won * setting_int(guild_id, "charge_bonus", 0) // 100


def is_admin(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    role_id = setting_int(member.guild.id, "admin_role", 0)
    return any(r.id == role_id for r in member.roles)


def is_money_admin(member: discord.abc.User) -> bool:
    """Who may approve top-ups and change balances, prices and settings: every admin
    (서버 관리 permission or the bot's admin role)."""
    return is_admin(member)


MONEY_ONLY = "관리자만 할 수 있어요."


def embed(title: str, description: str = "", color: int = COLOR_INFO) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = discord.utils.utcnow()
    return e


async def reply(
    interaction: discord.Interaction, e: discord.Embed, view: discord.ui.View | None = None
) -> None:
    kwargs = {"embed": e, "ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def error(interaction: discord.Interaction, message: str) -> None:
    await reply(interaction, embed("❌ 오류", message, COLOR_ERR))


async def send_log(
    guild: discord.Guild | None, e: discord.Embed, view: discord.ui.View | None = None
) -> discord.Message | None:
    if guild is None:
        return None
    channel = guild.get_channel(setting_int(guild.id, "log_channel", 0))
    if not isinstance(channel, discord.abc.Messageable):
        return None
    try:
        if view is not None:
            return await channel.send(embed=e, view=view)
        return await channel.send(embed=e)
    except discord.HTTPException:
        log.warning("로그 채널에 메시지를 보내지 못했어요 (guild %s)", guild.id)
        return None


async def dm(user_id: int, e: discord.Embed) -> bool:
    try:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
        await user.send(embed=e)
        return True
    except discord.HTTPException:
        return False


async def set_lifetime_role(guild: discord.Guild, user_id: int, give: bool) -> None:
    """Give or take the optional lifetime member role (설정 평생역할)."""
    role = guild.get_role(setting_int(guild.id, "lifetime_role", 0))
    if role is None:
        return
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if give:
            await member.add_roles(role, reason="평생 무료 회원")
        else:
            await member.remove_roles(role, reason="평생 무료 회원 해제")
    except discord.HTTPException:
        log.warning("평생 회원 역할을 바꾸지 못했어요 (user %s)", user_id)


async def after_refund(guild: discord.Guild, order) -> None:
    """A refunded lifetime key is revoked in the DB; also take the role back."""
    key = db.get_key(order["delivered"]) if order["delivered"].startswith("LIFE-") else None
    if key is not None and key["redeemed_by"]:
        await set_lifetime_role(guild, key["redeemed_by"], give=False)


def kst_day_start() -> int:
    now = datetime.datetime.now(KST)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def fmt_time(ts: int) -> str:
    return f"<t:{ts}:f>"


# ------------------------------------------------------------ user flows ----
async def show_balance(interaction: discord.Interaction, user: discord.abc.User) -> None:
    row = db.get_user(interaction.guild_id, user.id)
    e = embed(f"💰 {user.display_name}님의 잔액", f"## {row['balance']:,}원")
    e.add_field(name="누적 충전", value=f"{row['total_charged']:,}원")
    e.add_field(name="누적 사용", value=f"{row['total_spent']:,}원")
    if db.is_lifetime(interaction.guild_id, user.id):
        e.add_field(name="👑 평생 무료 회원", value="모든 상품을 0원으로 구매할 수 있어요.", inline=False)
    await reply(interaction, e)


async def claim_daily(interaction: discord.Interaction) -> None:
    amount = setting_int(interaction.guild_id, "daily_points", 0)
    if amount <= 0:
        return await error(interaction, "이 서버에서는 출석 체크를 사용하지 않아요.")
    balance = db.claim_daily(interaction.guild_id, interaction.user.id, amount, kst_day_start())
    if balance is None:
        return await error(interaction, "오늘은 이미 출석했어요. 내일(자정 이후) 다시 와 주세요!")
    await reply(
        interaction,
        embed("✅ 출석 완료", f"**{amount:,}원**을 받았어요!\n현재 잔액: **{balance:,}원**", COLOR_OK),
    )


async def show_history(interaction: discord.Interaction) -> None:
    orders = db.recent_orders(interaction.guild_id, interaction.user.id)
    if not orders:
        return await reply(interaction, embed("🧾 구매 내역", "아직 구매한 상품이 없어요."))
    lines = [
        f"`#{o['id']}` **{o['product_name']}**"
        + (f" × {o['quantity']}" if o["quantity"] > 1 else "")
        + f" — {o['price']:,}원 · {STATUS_LABEL.get(o['status'], o['status'])} · {fmt_time(o['created_at'])}"
        for o in orders
    ]
    await reply(interaction, embed("🧾 최근 구매 내역", "\n".join(lines)))


def kst_hour() -> int:
    return datetime.datetime.now(KST).hour


def price_label(p, price: int | None = None) -> str:
    """'3,000원', '200원 / 200개', or crossed out for lifetime members."""
    unit = f" / {p.unit}" if p.unit else ""
    if price is not None and price != p.price:
        return f"~~{p.price:,}원{unit}~~ 0원"
    return f"{p.price:,}원{unit}"


def product_note(p) -> str:
    notes = [KIND_LABEL[p.kind]]
    if p.kind == "stock":
        notes.append(f"재고 {p.stock}개")
    if p.sale_hours():
        notes.append(f"판매 {p.sale_hours()}" + ("" if p.on_sale(kst_hour()) else " (지금은 판매 시간 아님)"))
    return " · ".join(notes)


def categories(products: list) -> list[str]:
    seen: list[str] = []
    for p in products:
        if p.category not in seen:
            seen.append(p.category)
    return seen


async def open_shop(interaction: discord.Interaction) -> None:
    products = db.list_products(interaction.guild_id)
    if not products:
        return await error(interaction, "아직 판매 중인 상품이 없어요.")
    cats = categories(products)
    if len(cats) == 1:
        return await show_category(interaction, cats[0], edit=False)
    balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
    e = embed("🛒 상점", f"보유 잔액: **{balance:,}원**\n아래 메뉴에서 분류를 골라 주세요.")
    if db.is_lifetime(interaction.guild_id, interaction.user.id):
        e.description += "\n👑 **평생 무료 회원**이라 모든 상품이 **0원**이에요!"
    for cat in cats[:25]:
        items = [p for p in products if p.category == cat]
        cheapest = min(p.price for p in items)
        e.add_field(name=f"📂 {cat}", value=f"상품 {len(items)}개 · {cheapest:,}원부터", inline=True)
    await reply(interaction, e, CategoryView(cats))


async def show_category(interaction: discord.Interaction, category: str, edit: bool = True) -> None:
    products = [p for p in db.list_products(interaction.guild_id) if p.category == category][:25]
    if not products:
        return await error(interaction, "이 분류에 판매 중인 상품이 없어요.")
    balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
    e = embed(f"🛒 {category}", f"보유 잔액: **{balance:,}원**\n아래 메뉴에서 구매할 상품을 골라 주세요.")
    for p in products:
        price = db.price_for(interaction.guild_id, interaction.user.id, p)
        e.add_field(
            name=f"{p.name} — {price_label(p, price)}",
            value=(f"{p.description}\n" if p.description else "") + f"`{product_note(p)}`",
            inline=False,
        )
    view = ShopView(products, back=len(categories(db.list_products(interaction.guild_id))) > 1)
    if edit:
        await interaction.response.edit_message(embed=e, view=view)
    else:
        await reply(interaction, e, view)


async def open_charge(interaction: discord.Interaction) -> None:
    if not db.get_setting(interaction.guild_id, "bank_info"):
        return await error(interaction, "관리자가 아직 입금 계좌를 설정하지 않았어요.")
    await interaction.response.send_modal(ChargeModal())


# ------------------------------------------------------------------- shop ---
class CategoryView(discord.ui.View):
    def __init__(self, cats: list[str]) -> None:
        super().__init__(timeout=300)
        select = discord.ui.Select(
            placeholder="분류 선택",
            options=[discord.SelectOption(label=c[:100], value=c[:100], emoji="📂") for c in cats[:25]],
        )
        select.callback = self.on_select
        self.add_item(select)
        self.select = select

    async def on_select(self, interaction: discord.Interaction) -> None:
        await show_category(interaction, self.select.values[0])


class ShopView(discord.ui.View):
    def __init__(self, products: list, back: bool = False) -> None:
        super().__init__(timeout=300)
        options = [
            discord.SelectOption(
                label=p.name[:100],
                description=(
                    price_label(p).replace("~~", "")
                    + (" · 품절" if p.kind == "stock" and p.stock == 0 else "")
                    + (f" · {p.sale_hours()}" if p.sale_hours() else "")
                )[:100],
                value=str(p.id),
            )
            for p in products[:25]
        ]
        select = discord.ui.Select(placeholder="구매할 상품 선택", options=options)
        select.callback = self.on_select
        self.add_item(select)
        self.select = select
        if back:
            button = discord.ui.Button(label="분류로 돌아가기", emoji="↩️", style=discord.ButtonStyle.secondary)
            button.callback = self.on_back
            self.add_item(button)

    async def on_back(self, interaction: discord.Interaction) -> None:
        products = db.list_products(interaction.guild_id)
        cats = categories(products)
        balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
        e = embed("🛒 상점", f"보유 잔액: **{balance:,}원**\n아래 메뉴에서 분류를 골라 주세요.")
        for cat in cats[:25]:
            items = [p for p in products if p.category == cat]
            e.add_field(name=f"📂 {cat}", value=f"상품 {len(items)}개 · {min(p.price for p in items):,}원부터")
        await interaction.response.edit_message(embed=e, view=CategoryView(cats))

    async def on_select(self, interaction: discord.Interaction) -> None:
        product = db.get_product(interaction.guild_id, int(self.select.values[0]))
        if product is None or not product.active:
            return await error(interaction, "판매 중인 상품이 아니에요.")
        if not product.on_sale(kst_hour()):
            return await error(interaction, f"지금은 판매 시간이 아니에요. (판매 시간: {product.sale_hours()})")
        if (product.unit and product.kind != "stock") or product.form is not None:
            # Check the balance covers at least one unit before asking for codes and details.
            balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
            unit_price = db.price_for(interaction.guild_id, interaction.user.id, product)
            if balance < unit_price:
                return await confirm_purchase(interaction, product, 1, "")
            return await interaction.response.send_modal(OrderModal(product))
        await confirm_purchase(interaction, product, 1, "")


class OrderModal(discord.ui.Modal):
    """Asks how many units, and the game codes / details the admin needs."""

    def __init__(self, product) -> None:
        super().__init__(title=product.name[:45])
        self.product = product
        self.qty = self.code = self.pin = self.detail = None
        self.mode = fulfil.quantity_mode(product.auto)
        # Character and treasure items count the names / chapters typed instead.
        if product.unit and product.kind != "stock" and self.mode not in ("cats", "chapters"):
            self.qty = discord.ui.TextInput(
                label=f"수량 ({product.unit} 단위, 최대 {product.max_qty})"[:45],
                placeholder=f"예: 1 → {product.unit}, 2 → {product.unit} × 2", default="1", max_length=4,
            )
            self.add_item(self.qty)
        if product.form is not None:
            self.code = discord.ui.TextInput(label="기종변경(이어하기) 코드", max_length=20)
            self.pin = discord.ui.TextInput(label="인증번호", max_length=10)
            self.add_item(self.code)
            self.add_item(self.pin)
        # Automatic items only take details they can act on (character names, chapters).
        if product.form is not None and (self.mode is None or self.mode in ("cats", "chapters")):
            self.detail = discord.ui.TextInput(
                label=(product.form or "요청사항 (선택)")[:45], style=discord.TextStyle.paragraph,
                required=bool(product.form), max_length=500,
            )
            self.add_item(self.detail)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        quantity = 1
        if self.qty is not None:
            try:
                quantity = int(str(self.qty.value).strip())
            except ValueError:
                return await error(interaction, "수량은 숫자로 입력해 주세요.")
            if not 1 <= quantity <= self.product.max_qty:
                return await error(interaction, f"수량은 1~{self.product.max_qty} 사이로 입력해 주세요.")
        request, job = "", None
        detail = str(self.detail.value).strip() if self.detail is not None else ""
        if self.code is not None and self.mode is not None:
            # Automatic: the codes go only into the job (never into the admin log).
            picks: list[int] = []
            if self.mode in ("cats", "chapters"):
                if self.mode == "cats":
                    picks, problem = cat_names.resolve(detail)
                    shown = ", ".join(f"{cat_names.name(i)} (#{i})" for i in picks)
                else:
                    picks, problem = fulfil.parse_chapters(detail)
                    shown = fulfil.chapter_names(picks)
                if problem:
                    return await error(interaction, problem)
                if len(picks) > self.product.max_qty:
                    return await error(interaction, f"한 번에 최대 {self.product.max_qty}{self.product.unit[1:] or '개'}까지 주문할 수 있어요.")
                quantity = len(picks)
                request = f"{self.product.form}: {shown}"
            elif detail:
                request = f"요청사항: {detail}"
            job = {"auto": self.product.auto, "quantity": quantity, "picks": picks,
                   "code": str(self.code.value).strip(), "pin": str(self.pin.value).strip()}
        elif self.code is not None:
            lines = [f"기종변경 코드: {str(self.code.value).strip()}", f"인증번호: {str(self.pin.value).strip()}"]
            if detail:
                lines.append(f"{self.product.form or '요청사항'}: {detail}")
            request = "\n".join(lines)
        await confirm_purchase(interaction, self.product, quantity, request, job)


async def confirm_purchase(interaction: discord.Interaction, product, quantity: int, request: str,
                           job: dict | None = None) -> None:
    balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
    unit_price = db.price_for(interaction.guild_id, interaction.user.id, product)
    price = unit_price * quantity
    what = f"**{product.name}**" + (f" × {quantity} ({product.unit} 단위)" if product.unit else "")
    e = embed(
        "🛍️ 구매 확인",
        f"{what}을(를) **{price:,}원**에 구매할까요?"
        + (" (👑 평생 무료 회원 혜택)" if unit_price != product.price else "")
        + f"\n\n보유 잔액: {balance:,}원 → 구매 후: {balance - price:,}원",
        COLOR_WARN,
    )
    if balance < price:
        e = embed(
            "💳 잔액이 부족해요",
            f"{what}은(는) **{price:,}원**이에요.\n\n보유 잔액: {balance:,}원 · "
            f"**{price - balance:,}원**이 더 필요해요.\n충전한 뒤 다시 구매해 주세요.",
            COLOR_ERR,
        )
        return await interaction.response.send_message(embed=e, view=ChargeButtonView(), ephemeral=True)
    if job is not None:
        if request:
            e.add_field(name="주문 내용", value=request[:1000], inline=False)
        e.add_field(
            name="🤖 자동 처리",
            value="구매하면 봇이 바로 세이브를 수정해요 (1~3분).\n입력한 이어하기 코드는 사용되고, "
                  "**새 이어하기 코드와 인증번호를 DM으로** 보내 드려요. 서버 멤버의 DM을 허용해 주세요.",
            inline=False,
        )
    elif request:
        e.add_field(name="입력한 정보", value="코드와 요청 내용은 관리자에게만 전달돼요.", inline=False)
    await interaction.response.send_message(
        embed=e, view=ConfirmBuyView(product.id, quantity, request, job), ephemeral=True
    )


class ChargeButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(label="충전하기", emoji="💳", style=discord.ButtonStyle.success)
    async def charge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_charge(interaction)


class ConfirmBuyView(discord.ui.View):
    def __init__(self, product_id: int, quantity: int = 1, request: str = "", job: dict | None = None) -> None:
        super().__init__(timeout=120)
        self.product_id, self.quantity, self.request, self.job = product_id, quantity, request, job

    @discord.ui.button(label="구매하기", style=discord.ButtonStyle.success, emoji="✅")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)
        await purchase(interaction, self.product_id, self.quantity, self.request, self.job)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embed("구매를 취소했어요.", color=COLOR_INFO), view=None
        )


async def purchase(interaction: discord.Interaction, product_id: int, quantity: int = 1, request: str = "",
                   job: dict | None = None) -> None:
    guild = interaction.guild
    try:
        result = db.purchase(guild.id, interaction.user.id, product_id, quantity, request, kst_hour(),
                             json.dumps(job) if job else "")
    except ShopError as exc:
        return await error(interaction, str(exc))
    product = result.product
    qty_text = f" × {result.quantity} ({product.unit} 단위)" if product.unit else ""

    if product.kind == "role":
        role = guild.get_role(product.role_id or 0)
        try:
            if role is None:
                raise discord.HTTPException
            await interaction.user.add_roles(role, reason=f"상점 구매 #{result.order_id}")
        except discord.HTTPException:
            db.refund_order(guild.id, result.order_id)
            return await error(
                interaction,
                "역할을 지급하지 못해 금액을 잔액으로 돌려드렸어요. 관리자에게 문의해 주세요.\n"
                "(봇의 역할이 지급할 역할보다 위에 있어야 해요)",
            )

    e = embed("🎉 구매 완료", f"**{product.name}**{qty_text} 구매가 완료됐어요!", COLOR_OK)
    e.add_field(name="주문 번호", value=f"#{result.order_id}")
    e.add_field(name="사용 금액", value=f"{result.price:,}원")
    e.add_field(name="남은 잔액", value=f"{result.balance:,}원")
    if product.kind == "stock":
        e.add_field(name="📦 상품 내용", value=f"||{result.delivered[:1000]}||", inline=False)
        dm_embed = embed(f"📦 {product.name} 구매 상품", f"```\n{result.delivered[:3900]}\n```", COLOR_OK)
        dm_embed.set_footer(text=f"{guild.name} · 주문 #{result.order_id}")
        if not await dm(interaction.user.id, dm_embed):
            e.set_footer(text="DM을 보낼 수 없어서 여기에만 표시했어요. 꼭 따로 저장해 주세요!")
    elif product.kind == "role":
        e.add_field(name="지급된 역할", value=f"<@&{product.role_id}>", inline=False)
    elif product.kind == "lifetime":
        e.add_field(name="🔑 평생 무료 키", value=f"`{result.delivered}`", inline=False)
        e.add_field(
            name="사용 방법",
            value="`/키등록`(또는 자판기의 🔑 버튼)에 이 키를 넣으면 평생 무료 회원이 돼요.\n"
                  "다른 사람에게 선물해도 돼요. 키는 한 번만 사용할 수 있어요.",
            inline=False,
        )
        dm_embed = embed(
            "🔑 평생 무료 키",
            f"```\n{result.delivered}\n```\n`/키등록`으로 등록하면 모든 상품을 0원으로 구매할 수 있어요.",
            COLOR_OK,
        )
        dm_embed.set_footer(text=f"{guild.name} · 주문 #{result.order_id}")
        await dm(interaction.user.id, dm_embed)
    elif job:
        e.add_field(
            name="🤖 자동 처리 중",
            value="세이브를 수정하고 있어요. 보통 1~3분 걸려요.\n끝나면 **새 이어하기 코드와 인증번호를 DM으로** 보내 드려요.\n"
                  "그 전까지 게임에서 이어하기를 하지 마세요.",
            inline=False,
        )
    else:
        e.add_field(
            name="안내", value="관리자가 확인 후 처리해 드릴 거예요. 잠시만 기다려 주세요!", inline=False
        )
    await reply(interaction, e)

    log_e = embed("🛒 구매", color=COLOR_INFO)
    log_e.add_field(name="구매자", value=interaction.user.mention)
    log_e.add_field(name="상품", value=f"#{product.id} {product.name}{qty_text}")
    log_e.add_field(
        name="가격", value=f"{result.price:,}원" + (" (평생 회원)" if result.price != product.price else "")
    )
    if request:
        log_e.add_field(name="요청 내용", value=f"```\n{request[:1000]}\n```", inline=False)
    log_e.set_footer(text=f"주문 #{result.order_id}")
    view = None
    if product.kind == "manual":
        log_e.title = "🤖 자동 처리 중인 주문" if job else "🛎️ 처리가 필요한 주문"
        log_e.color = COLOR_INFO if job else COLOR_WARN
        view = discord.ui.View(timeout=None)
        view.add_item(OrderButton("done", result.order_id))
        view.add_item(OrderButton("refund", result.order_id))
    msg = await send_log(guild, log_e, view)
    if msg is not None:
        db.set_order_log(result.order_id, msg.channel.id, msg.id)
    if job:
        asyncio.create_task(fulfil_order(result.order_id))


# ------------------------------------------------------- automatic edits ----
async def edit_order_log(order, title: str, color: int, note: str, keep_buttons: bool) -> None:
    """Update the order's message in the log channel (or post a new one)."""
    e = embed(title, note, color)
    e.add_field(name="구매자", value=f"<@{order['user_id']}>")
    e.add_field(name="상품", value=f"{order['product_name']}" + (f" × {order['quantity']}" if order["quantity"] > 1 else ""))
    e.add_field(name="가격", value=f"{order['price']:,}원")
    e.set_footer(text=f"주문 #{order['id']}")
    view = None
    if keep_buttons:
        view = discord.ui.View(timeout=None)
        view.add_item(OrderButton("done", order["id"]))
        view.add_item(OrderButton("refund", order["id"]))
    channel = client.get_channel(order["log_channel_id"] or 0)
    if channel is not None and order["log_message_id"]:
        try:
            await channel.get_partial_message(order["log_message_id"]).edit(embed=e, view=view)
            return
        except discord.HTTPException:
            pass
    await send_log(client.get_guild(order["guild_id"]), e, view)


def codes_embed(title: str, codes: tuple[str, str], text: str, color: int) -> discord.Embed:
    e = embed(title, text, color)
    e.add_field(name="이어하기 코드", value=f"```\n{codes[0]}\n```")
    e.add_field(name="인증번호", value=f"```\n{codes[1]}\n```")
    e.add_field(
        name="받는 방법",
        value="게임 첫 화면 → 기종변경 → 데이터 인계(이어하기)에서 위 코드를 입력하세요.",
        inline=False,
    )
    return e


def bullet(items: list[str], limit: int = 900) -> str:
    return ("\n".join(f"• {x}" for x in items) or "-")[:limit]


async def fulfil_order(order_id: int) -> None:
    """Edit the buyer's save with the editor code and deliver the new transfer codes."""
    order = db.get_order(order_id)
    if order is None or order["status"] != "pending" or order["job_state"] != "queued" or not order["job"]:
        return
    job = json.loads(order["job"])
    db.set_job_state(order_id, "running")
    try:
        edits = fulfil.build_edits(job["auto"], int(job["quantity"]), job.get("picks") or [])
        result = await fulfil.run_worker({
            "mode": "codes", "transfer_code": job["code"], "confirmation_code": job["pin"],
            "cc": fulfil.GAME_CC, "edits": edits,
        })
    except Exception as exc:  # noqa: BLE001 - never leave an order stuck in "running"
        log.exception("자동 처리 오류 (주문 #%s)", order_id)
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "crashed": True}
    backup = fulfil.save_backup(order_id, result.get("original_b64"))
    codes = (result.get("transfer_code"), result.get("confirmation_code"))
    have_codes = bool(codes[0] and codes[1])
    done, failed = result.get("done") or [], result.get("failed") or []
    order = db.get_order(order_id)
    user_id, guild = order["user_id"], client.get_guild(order["guild_id"])
    guild_name = guild.name if guild else "서버"
    backup_note = f"\n원본 세이브 백업: `{backup}`" if backup else ""

    if have_codes and done and not failed and not result.get("error"):
        # Everything worked.
        db.complete_order(order["guild_id"], order_id)
        db.set_job_state(order_id, "done", wipe=True)
        sent = await dm(user_id, codes_embed(
            f"✅ {order['product_name']} 처리 완료", codes,
            f"**{guild_name}** 주문 #{order_id}이(가) 완료됐어요!\n{bullet(done)}", COLOR_OK))
        note = f"자동으로 처리했어요.\n{bullet(done)}"
        if not sent:
            note += f"\n\n⚠️ 구매자에게 DM을 보내지 못했어요. 이 코드를 직접 전달해 주세요:\n`{codes[0]}` / `{codes[1]}`"
        await edit_order_log(order, "✅ 자동 처리 완료", COLOR_OK, note, keep_buttons=False)
        return

    if have_codes and not done:
        # The save came back untouched (e.g. a game version the editor can't read yet): refund.
        db.refund_order(order["guild_id"], order_id)
        db.set_job_state(order_id, "failed", wipe=True)
        reason = result.get("error") or bullet(failed)
        sent = await dm(user_id, codes_embed(
            "↩️ 처리하지 못해 환불했어요", codes,
            f"**{guild_name}** 주문 #{order_id}을(를) 처리하지 못해서 {order['price']:,}원을 잔액으로 돌려드렸어요.\n"
            f"사유: {reason}\n세이브는 바뀌지 않았어요. 이어하기 코드가 새로 바뀌었으니 아래 코드를 입력하세요.",
            COLOR_ERR))
        note = f"편집하지 못해 자동 환불했어요.\n사유: {reason}{backup_note}"
        if not sent:
            note += f"\n\n⚠️ 구매자에게 DM을 보내지 못했어요. 새 코드를 직접 전달해 주세요:\n`{codes[0]}` / `{codes[1]}`"
        await edit_order_log(order, "↩️ 자동 환불", COLOR_ERR, note, keep_buttons=False)
        return

    if have_codes:
        # Some parts worked, some didn't: deliver the codes and let an admin decide.
        db.set_job_state(order_id, "failed", wipe=True)
        sent = await dm(user_id, codes_embed(
            "⚠️ 일부만 처리됐어요", codes,
            f"**{guild_name}** 주문 #{order_id}\n완료:\n{bullet(done)}\n실패:\n{bullet(failed + ([result['error']] if result.get('error') else []))}\n"
            "관리자가 확인한 뒤 나머지를 처리하거나 환불해 드릴 거예요.",
            COLOR_WARN))
        note = (f"완료:\n{bullet(done)}\n실패:\n{bullet(failed + ([result['error']] if result.get('error') else []))}"
                f"{backup_note}\n\n남은 부분을 처리했다면 `처리 완료`, 아니면 `환불`을 눌러 주세요.")
        if not sent:
            note += f"\n\n⚠️ 구매자에게 DM을 보내지 못했어요. 새 코드를 직접 전달해 주세요:\n`{codes[0]}` / `{codes[1]}`"
        await edit_order_log(order, "⚠️ 자동 처리 일부 실패", COLOR_WARN, note, keep_buttons=True)
        return

    if not result.get("original_b64") and not result.get("timeout") and not result.get("crashed"):
        # The save was never downloaded (wrong code, server down): the codes still work. Refund.
        db.refund_order(order["guild_id"], order_id)
        db.set_job_state(order_id, "failed", wipe=True)
        reason = result.get("error") or "알 수 없는 오류"
        await dm(user_id, embed(
            "↩️ 처리하지 못해 환불했어요",
            f"**{guild_name}** 주문 #{order_id}: {reason}\n{order['price']:,}원을 잔액으로 돌려드렸어요. "
            "입력한 이어하기 코드는 그대로 쓸 수 있어요. 코드를 확인하고 다시 구매해 주세요.",
            COLOR_ERR))
        await edit_order_log(order, "↩️ 자동 환불", COLOR_ERR, f"세이브를 받지 못해 자동 환불했어요.\n사유: {reason}",
                             keep_buttons=False)
        return

    # The save was downloaded (code used up) but no new codes came back: an admin must step in.
    db.set_job_state(order_id, "failed", wipe=True)
    reason = result.get("error") or "알 수 없는 오류"
    folder = f"\n작업 폴더: `{result['job_dir']}`" if result.get("job_dir") else ""
    await dm(user_id, embed(
        "⚠️ 처리 중 문제가 생겼어요",
        f"**{guild_name}** 주문 #{order_id}을(를) 처리하다 문제가 생겼어요. 관리자가 직접 확인해서 "
        "세이브를 돌려드리거나 환불해 드릴게요. 잠시만 기다려 주세요.",
        COLOR_WARN))
    await edit_order_log(order, "🚨 자동 처리 실패: 관리자 확인 필요", COLOR_ERR,
                         f"사유: {reason}{backup_note}{folder}\n이어하기 코드가 이미 사용됐을 수 있어요. "
                         "백업 세이브로 복원해 주거나 환불해 주세요.", keep_buttons=True)


async def resume_jobs() -> None:
    """After a restart: run orders that never started; flag ones cut off mid-edit."""
    for order in db.jobs_in_state("running"):
        db.set_job_state(order["id"], "interrupted", wipe=True)
        await edit_order_log(order, "🚨 자동 처리 중단: 관리자 확인 필요", COLOR_ERR,
                             "봇이 다시 시작되면서 편집이 중간에 멈췄어요. 이어하기 코드가 이미 사용됐을 수 있어요.\n"
                             "구매자에게 확인한 뒤 처리하거나 환불해 주세요.", keep_buttons=True)
    for order in db.jobs_in_state("queued"):
        asyncio.create_task(fulfil_order(order["id"]))


# ---------------------------------------------------------------- charge ----
class ChargeModal(discord.ui.Modal, title="잔액 충전 신청"):
    amount = discord.ui.TextInput(label="충전 금액 (원)", placeholder="예: 10000", max_length=9)
    depositor = discord.ui.TextInput(
        label="입금자명", placeholder="실제로 입금할 때 표시되는 이름", max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        try:
            won = int(str(self.amount.value).replace(",", "").replace("원", "").strip())
        except ValueError:
            return await error(interaction, "충전 금액은 숫자로만 입력해 주세요.")
        minimum = setting_int(guild_id, "min_charge", 1000)
        if won < minimum or won > MAX_CHARGE:
            return await error(
                interaction, f"충전 금액은 {minimum:,}원 이상 {MAX_CHARGE:,}원 이하로 입력해 주세요."
            )
        name = str(self.depositor.value).strip()
        points = points_for(guild_id, won)
        try:
            charge_id, balance = db.create_charge(
                guild_id, interaction.user.id, won, name, points, CHARGE_EXPIRE_MINUTES * 60
            )
        except ShopError as exc:
            return await error(interaction, str(exc))

        if balance is not None:  # the deposit had already arrived
            await reply(
                interaction,
                embed("✅ 충전 완료", f"입금이 확인되어 **{points:,}원**이 충전됐어요!\n"
                      f"현재 잔액: **{balance:,}원**", COLOR_OK),
            )
            charge = db.get_charge(charge_id)
            return await send_log(interaction.guild, charge_embed(charge, "자동 승인 (입금 먼저 확인됨)"))

        e = embed(
            "🏦 입금 안내",
            "아래 계좌로 **정확한 금액**을 **신청한 입금자명**으로 보내 주세요.\n"
            "입금이 확인되면 잔액이 충전되고 DM으로 알려 드려요.",
            COLOR_INFO,
        )
        e.add_field(name="입금 계좌", value=db.get_setting(guild_id, "bank_info"), inline=False)
        e.add_field(name="입금 금액", value=f"**{won:,}원**")
        e.add_field(name="입금자명", value=f"**{name}**")
        e.add_field(name="충전될 금액", value=f"{points:,}원")
        e.set_footer(text=f"신청 #{charge_id} · {CHARGE_EXPIRE_MINUTES}분 안에 입금해 주세요")
        await reply(interaction, e)

        view = discord.ui.View(timeout=None)
        view.add_item(ChargeButton("ok", charge_id))
        view.add_item(ChargeButton("no", charge_id))
        msg = await send_log(interaction.guild, charge_embed(db.get_charge(charge_id)), view)
        if msg is not None:
            db.set_charge_log(charge_id, msg.channel.id, msg.id)


def charge_embed(charge, status: str | None = None) -> discord.Embed:
    titles = {
        "pending": ("💳 충전 신청 (입금 대기)", COLOR_WARN),
        "approved": ("✅ 충전 승인", COLOR_OK),
        "rejected": ("⛔ 충전 거절", COLOR_ERR),
        "expired": ("⌛ 충전 신청 만료", 0x99AAB5),
        "cancelled": ("🚫 충전 취소 (입금 없음)", COLOR_ERR),
    }
    title, color = titles[charge["status"]]
    e = embed(title, status or "", color)
    e.add_field(name="신청자", value=f"<@{charge['user_id']}>")
    e.add_field(name="금액", value=f"{charge['amount']:,}원")
    e.add_field(name="입금자명", value=charge["depositor"])
    if charge["status"] == "approved":
        e.add_field(name="충전 금액", value=f"{charge['points']:,}원")
    e.set_footer(text=f"신청 #{charge['id']}")
    return e


async def update_charge_log(charge_id: int, status: str | None = None) -> None:
    charge = db.get_charge(charge_id)
    if charge is None or not charge["log_message_id"]:
        return
    channel = client.get_channel(charge["log_channel_id"])
    if channel is None:
        return
    try:
        msg = channel.get_partial_message(charge["log_message_id"])
        await msg.edit(embed=charge_embed(charge, status), view=None)
    except discord.HTTPException:
        pass


class ChargeButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"charge:(?P<action>ok|no):(?P<id>\d+)"
):
    """Approve / reject buttons on a charge request in the log channel.
    For when a deposit could not be matched automatically."""

    def __init__(self, action: str, charge_id: int) -> None:
        self.action, self.charge_id = action, charge_id
        super().__init__(
            discord.ui.Button(
                label="입금 확인 (승인)" if action == "ok" else "거절",
                style=discord.ButtonStyle.success if action == "ok" else discord.ButtonStyle.danger,
                custom_id=f"charge:{action}:{charge_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user):
            return await error(interaction, "관리자만 처리할 수 있어요.")
        if self.action == "ok":
            # Approving adds real balance, so the admin must confirm the deposit in the bank app.
            if not is_money_admin(interaction.user):
                return await error(interaction, "충전 승인은 " + MONEY_ONLY)
            charge = db.get_charge(self.charge_id)
            if charge is None or charge["status"] != "pending":
                return await error(interaction, "이미 처리된 충전 신청이에요.")
            return await interaction.response.send_modal(ApproveModal(self.charge_id))
        await resolve_charge_action(interaction, self.charge_id, approve=False)


class ApproveModal(discord.ui.Modal, title="입금 확인"):
    """Asks for the deposit as the bank app shows it; it must match the charge request."""

    amount = discord.ui.TextInput(label="은행 앱에서 확인한 입금액 (원)", placeholder="예: 10000", max_length=12)
    depositor = discord.ui.TextInput(label="은행 앱에 표시된 입금자명", max_length=20)

    def __init__(self, charge_id: int) -> None:
        super().__init__()
        self.charge_id = charge_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        charge = db.get_charge(self.charge_id)
        if charge is None or charge["status"] != "pending":
            return await error(interaction, "이미 처리된 충전 신청이에요.")
        if not is_money_admin(interaction.user):
            return await error(interaction, "이 충전 신청을 승인할 권한이 없어요.")
        try:
            won = int(str(self.amount.value).replace(",", "").replace("원", "").strip())
        except ValueError:
            return await error(interaction, "입금액은 숫자로 입력해 주세요.")
        if won != charge["amount"] or not names_match(charge["depositor"], str(self.depositor.value)):
            return await error(
                interaction,
                f"신청 내용과 달라서 승인하지 않았어요.\n신청: **{charge['depositor']}** · **{charge['amount']:,}원**\n"
                f"입력: **{str(self.depositor.value).strip()}** · **{won:,}원**\n"
                "은행 앱에서 이 입금이 실제로 들어왔는지 다시 확인해 주세요.",
            )
        await resolve_charge_action(interaction, self.charge_id, approve=True)


async def resolve_charge_action(interaction: discord.Interaction, charge_id: int, approve: bool) -> None:
    guild_id = interaction.guild_id
    charge = db.get_charge(charge_id)
    points = points_for(guild_id, charge["amount"]) if charge else 0
    try:
        charge, balance = db.resolve_charge(guild_id, charge_id, approve, interaction.user.id, points)
    except ShopError as exc:
        return await error(interaction, str(exc))
    charge = db.get_charge(charge_id)
    note = f"{interaction.user.mention}님이 " + ("은행 앱에서 입금을 확인하고 승인했어요." if approve else "거절했어요.")
    if interaction.message is not None:
        await interaction.response.edit_message(embed=charge_embed(charge, note), view=None)
    else:
        await reply(interaction, charge_embed(charge, note))
    if approve:
        await dm(charge["user_id"], embed(
            "✅ 충전 완료",
            f"**{interaction.guild.name}**에서 **{points:,}원**이 충전됐어요!\n현재 잔액: **{balance:,}원**",
            COLOR_OK,
        ))
    else:
        await dm(charge["user_id"], embed(
            "⛔ 충전 거절",
            f"**{interaction.guild.name}**의 충전 신청 #{charge['id']}이(가) 거절됐어요. "
            "입금했는데 거절됐다면 관리자에게 문의해 주세요.",
            COLOR_ERR,
        ))


class OrderButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"order:(?P<action>done|refund):(?P<id>\d+)"
):
    """Complete / refund buttons on a manual order in the log channel."""

    def __init__(self, action: str, order_id: int) -> None:
        self.action, self.order_id = action, order_id
        super().__init__(
            discord.ui.Button(
                label="처리 완료" if action == "done" else "환불",
                style=discord.ButtonStyle.success if action == "done" else discord.ButtonStyle.danger,
                custom_id=f"order:{action}:{order_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user):
            return await error(interaction, "관리자만 처리할 수 있어요.")
        if self.action == "refund" and not is_money_admin(interaction.user):
            return await error(interaction, "환불은 " + MONEY_ONLY)
        try:
            if self.action == "done":
                order = db.complete_order(interaction.guild_id, self.order_id)
                text, color = "✅ 주문 처리 완료", COLOR_OK
                user_msg = f"주문 #{order['id']} **{order['product_name']}** 처리가 완료됐어요!"
            else:
                order = db.refund_order(interaction.guild_id, self.order_id)
                await after_refund(interaction.guild, order)
                text, color = "↩️ 주문 환불", COLOR_ERR
                user_msg = (f"주문 #{order['id']} **{order['product_name']}**이(가) 환불되어 "
                            f"{order['price']:,}원을 돌려드렸어요.")
        except ShopError as exc:
            return await error(interaction, str(exc))
        e = interaction.message.embeds[0] if interaction.message.embeds else embed(text)
        e.title, e.color = text, color
        e.description = f"{interaction.user.mention}님이 처리했어요."
        # The buyer's game codes don't need to stay in the log once the order is handled.
        for i, field in reversed(list(enumerate(e.fields))):
            if field.name == "요청 내용":
                e.remove_field(i)
        await interaction.response.edit_message(embed=e, view=None)
        await dm(order["user_id"], embed(text, user_msg, color))


# -------------------------------------------------------------- lifetime ----
async def redeem(interaction: discord.Interaction, key: str) -> None:
    try:
        db.redeem_key(interaction.guild_id, interaction.user.id, key)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await set_lifetime_role(interaction.guild, interaction.user.id, give=True)
    await reply(interaction, embed(
        "👑 평생 무료 회원 등록 완료",
        "이제 상점의 모든 상품을 **0원**로 구매할 수 있어요!\n(평생 무료 키 상품은 제외)",
        COLOR_OK,
    ))
    await send_log(interaction.guild, embed(
        "👑 평생 무료 키 등록", f"{interaction.user.mention}님이 키 `{key.strip().upper()}`를 등록했어요.",
        COLOR_OK,
    ))


class RedeemModal(discord.ui.Modal, title="평생 무료 키 등록"):
    key = discord.ui.TextInput(label="키", placeholder="LIFE-XXXX-XXXX-XXXX", max_length=40)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await redeem(interaction, str(self.key.value))


# ----------------------------------------------------------------- panel ----
class OldDailyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"panel:daily"):
    """The check-in button on panels posted before it was removed."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="출석 체크", custom_id="panel:daily"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        if setting_int(interaction.guild_id, "daily_points", 0) > 0:
            return await claim_daily(interaction)
        await error(interaction, "출석 체크는 더 이상 사용하지 않아요. 잔액은 계좌 입금으로만 충전돼요.")


class PanelView(discord.ui.View):
    """The 자판기 panel posted by /자판기설치. Buttons keep working after restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="상품 구매", emoji="🛒", style=discord.ButtonStyle.primary, custom_id="panel:shop")
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_shop(interaction)

    @discord.ui.button(label="충전", emoji="💳", style=discord.ButtonStyle.success, custom_id="panel:charge")
    async def charge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_charge(interaction)

    @discord.ui.button(label="내 정보", emoji="💰", style=discord.ButtonStyle.secondary, custom_id="panel:balance")
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_balance(interaction, interaction.user)

    @discord.ui.button(label="구매 내역", emoji="🧾", style=discord.ButtonStyle.secondary, custom_id="panel:history")
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await show_history(interaction)


    @discord.ui.button(label="키 등록", emoji="🔑", style=discord.ButtonStyle.secondary, custom_id="panel:redeem")
    async def redeem_key(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RedeemModal())


# --------------------------------------------------------- bank webhook -----
async def handle_deposit(guild_id: int, amount: int, name: str, raw: str, key: str) -> dict:
    guild = client.get_guild(guild_id)
    result = db.record_deposit(
        guild_id, amount, name, raw, key,
        lambda won: points_for(guild_id, won), CHARGE_EXPIRE_MINUTES * 60,
    )
    if result is None:
        return {"ok": True, "duplicate": True}
    deposit_id, charge, balance = result

    if charge is not None:
        charge = db.get_charge(charge["id"])
        await update_charge_log(charge["id"], f"입금 #{deposit_id} 확인 → 자동 승인")
        if not charge["log_message_id"]:
            await send_log(guild, charge_embed(charge, f"입금 #{deposit_id} 확인 → 자동 승인"))
        await dm(charge["user_id"], embed(
            "✅ 충전 완료",
            f"입금 **{amount:,}원**이 확인되어 **{charge['points']:,}원**이 충전됐어요!\n"
            f"현재 잔액: **{balance:,}원**",
            COLOR_OK,
        ))
        return {"ok": True, "matched": True, "charge_id": charge["id"]}

    e = embed(
        "❓ 확인되지 않은 입금",
        f"**{name}**님이 **{amount:,}원**을 입금했지만 일치하는 충전 신청이 없어요.\n"
        f"같은 이름·금액으로 `/충전`을 신청하면 자동으로 연결되고, "
        f"직접 처리하려면 `/입금 연결 입금번호:{deposit_id}`을 사용하세요.",
        COLOR_WARN,
    )
    e.set_footer(text=f"입금 #{deposit_id}")
    await send_log(guild, e)
    return {"ok": True, "matched": False, "deposit_id": deposit_id}


async def deposit_webhook(request: web.Request) -> web.Response:
    token = request.headers.get("X-Webhook-Token") or request.query.get("token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(token, WEBHOOK_SECRET):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if not DEPOSIT_GUILD_ID:
        return web.json_response({"ok": False, "error": "DEPOSIT_GUILD_ID not set"}, status=500)

    raw = await request.text()
    data: dict = {}
    if "json" in (request.content_type or ""):
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
    elif "form" in (request.content_type or ""):
        data = dict(await request.post())
    text = str(data.get("text") or data.get("message") or data.get("body") or ("" if data else raw))

    # MacroDroid's "test macro" sends its placeholders unfilled: treat that as a connection test.
    if not text.strip() or "[notification" in text or "연결 테스트" in text:
        await send_log(client.get_guild(DEPOSIT_GUILD_ID), embed(
            "📱 입금 알림 폰 연결 확인",
            "은행 앱 알림을 보내는 폰이 봇에 연결됐어요. 실제 입금이 들어오면 자동으로 충전돼요.",
            COLOR_OK,
        ))
        return web.json_response({"ok": True, "test": True})

    amount, name = data.get("amount"), data.get("name") or data.get("depositor")
    if amount is not None and name:
        try:
            amount = int(str(amount).replace(",", ""))
        except ValueError:
            return web.json_response({"ok": False, "error": "bad amount"}, status=400)
        name = str(name)
    else:
        parsed = bank.parse_deposit(text)
        if parsed is None:
            # Not a deposit (withdrawal, ad…) or a format we can't read. Answer 200 so
            # the phone app doesn't keep retrying; show readable-looking ones to admins.
            if re.search(r"입금|보냈|받았|\d원", text):
                await send_log(client.get_guild(DEPOSIT_GUILD_ID), embed(
                    "⚠️ 읽지 못한 은행 알림",
                    f"```\n{text[:1500]}\n```\n입금 알림이 맞다면 충전 신청의 `입금 확인 (승인)`으로 처리해 주세요. "
                    "이 형식을 자동으로 읽게 하려면 이 내용을 개발자에게 알려 주세요.",
                    COLOR_WARN,
                ))
            return web.json_response({"ok": True, "ignored": True})
        amount, name = parsed.amount, parsed.name
    if amount <= 0 or amount > MAX_CHARGE:
        return web.json_response({"ok": False, "error": "bad amount"}, status=400)

    key = str(data.get("id") or "") or hashlib.sha256((text or raw).encode()).hexdigest()
    result = await handle_deposit(DEPOSIT_GUILD_ID, amount, name, text or raw, key)
    return web.json_response(result)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ------------------------------------------------------------------- bot ----
class ShopBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        self.add_view(PanelView())
        self.add_dynamic_items(ChargeButton, OrderButton, OldDailyButton)
        for group in (settings_group, points_group, product_group, stock_group,
                      order_group, deposit_group, vault_group, lifetime_group):
            self.tree.add_command(group)
        try:
            if DEV_GUILD_ID:
                guild = discord.Object(DEV_GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.Forbidden:
            # Don't crash-loop: keep running so the webhook and buttons still work.
            log.error(
                "명령어를 등록하지 못했어요 (Missing Access). 봇을 서버에 초대할 때 "
                "'bot'과 'applications.commands'를 모두 체크했는지, DEV_GUILD_ID가 "
                "봇이 들어간 서버의 ID인지 확인한 뒤 봇을 재시작하세요."
            )

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_post("/deposit", deposit_webhook)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, WEBHOOK_HOST, WEBHOOK_PORT).start()
        log.info("입금 웹훅 대기 중: http://%s:%s/deposit", WEBHOOK_HOST, WEBHOOK_PORT)
        if not WEBHOOK_SECRET:
            log.warning("WEBHOOK_SECRET이 비어 있어서 입금 웹훅이 꺼져 있어요.")
        expire_charges.start()

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        log.info("%s 로그인 완료 (서버 %d개)", self.user, len(self.guilds))
        await self.change_presence(activity=discord.Game("/상점 · /충전"))
        if not getattr(self, "_started", False):  # on_ready also fires after reconnects
            self._started = True
            await resume_jobs()
            asyncio.create_task(refresh_cat_names())


_names_tried = 0.0


async def refresh_cat_names() -> None:
    """Character names for the '원하는 캐릭터' items, from the game data (once a day)."""
    global _names_tried
    if cat_names.ready and time.time() - cat_names.loaded_at < 86400:
        return
    if time.time() - _names_tried < 3600:  # at most one try an hour
        return
    _names_tried = time.time()
    if await cat_names.refresh():
        log.info("캐릭터 이름 %d개를 불러왔어요", len(cat_names.cats))
    else:
        log.warning("캐릭터 이름을 불러오지 못했어요 (게임 데이터 다운로드 실패)")


client = ShopBot()
tree = client.tree


@tasks.loop(minutes=5)
async def expire_charges() -> None:
    for charge in db.expire_charges(int(time.time()) - CHARGE_EXPIRE_MINUTES * 60):
        await update_charge_log(charge["id"], "입금이 확인되지 않아 만료됐어요.")
    fulfil.prune_backups()
    if client.is_ready():
        asyncio.create_task(refresh_cat_names())


@tree.error
async def on_app_command_error(interaction: discord.Interaction, exc: app_commands.AppCommandError):
    if isinstance(exc, NotMoneyAdmin):
        return await error(interaction, MONEY_ONLY)
    if isinstance(exc, app_commands.MissingPermissions):
        return await error(interaction, "이 명령어를 사용할 권한이 없어요.")
    if isinstance(exc, app_commands.NoPrivateMessage):
        return await error(interaction, "서버에서만 사용할 수 있어요.")
    log.exception("명령어 오류", exc_info=exc)
    await error(interaction, "처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")


def admin_only():
    def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            raise app_commands.MissingPermissions(["manage_guild"])
        return True

    return app_commands.check(predicate)


class NotMoneyAdmin(app_commands.CheckFailure):
    pass


def money_only():
    """For commands that create or move money, or change prices and settings."""
    def predicate(interaction: discord.Interaction) -> bool:
        if not is_money_admin(interaction.user):
            raise NotMoneyAdmin()
        return True

    return app_commands.check(predicate)


async def product_autocomplete(interaction: discord.Interaction, current: str):
    products = db.list_products(interaction.guild_id, include_inactive=True)
    return [
        app_commands.Choice(name=f"#{p.id} {p.name} ({p.price:,}원)"[:100], value=p.id)
        for p in products
        if current.lower() in p.name.lower() or current == str(p.id)
    ][:25]


# --------------------------------------------------------- user commands ----
@tree.command(name="잔액", description="내 잔액을 확인해요")
@app_commands.guild_only()
@app_commands.describe(유저="확인할 유저 (비우면 나)")
async def cmd_balance(interaction: discord.Interaction, 유저: discord.Member | None = None):
    if 유저 is not None and 유저 != interaction.user and not is_admin(interaction.user):
        return await error(interaction, "다른 유저의 잔액은 관리자만 볼 수 있어요.")
    await show_balance(interaction, 유저 or interaction.user)


@tree.command(name="출석", description="하루 한 번 출석 체크로 보상을 받아요")
@app_commands.guild_only()
async def cmd_daily(interaction: discord.Interaction):
    await claim_daily(interaction)


@tree.command(name="충전", description="계좌 입금으로 잔액을 충전해요")
@app_commands.guild_only()
async def cmd_charge(interaction: discord.Interaction):
    await open_charge(interaction)


@tree.command(name="상점", description="잔액으로 상품을 구매해요")
@app_commands.guild_only()
async def cmd_shop(interaction: discord.Interaction):
    await open_shop(interaction)


@tree.command(name="구매내역", description="최근 구매 내역을 확인해요")
@app_commands.guild_only()
async def cmd_history(interaction: discord.Interaction):
    await show_history(interaction)


@tree.command(name="키등록", description="평생 무료 키를 등록해요")
@app_commands.guild_only()
@app_commands.describe(키="LIFE-XXXX-XXXX-XXXX")
async def cmd_redeem(interaction: discord.Interaction, 키: str):
    await redeem(interaction, 키)


@tree.command(name="랭킹", description="잔액 순위를 확인해요")
@app_commands.guild_only()
async def cmd_rank(interaction: discord.Interaction):
    rows = db.leaderboard(interaction.guild_id)
    if not rows:
        return await reply(interaction, embed("🏆 잔액 랭킹", "아직 잔액이 있는 사람이 없어요."))
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — **{r['balance']:,}원**"
        for i, r in enumerate(rows)
    ]
    await reply(interaction, embed("🏆 잔액 랭킹", "\n".join(lines)))


@tree.command(name="자판기설치", description="[관리자] 이 채널에 자판기 패널을 올려요")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only()
async def cmd_panel(interaction: discord.Interaction):
    e = embed(
        f"🏪 {interaction.guild.name} 자판기",
        "아래 버튼으로 잔액을 충전하고 상품을 구매할 수 있어요.\n\n"
        "💳 **충전** — 계좌에 입금이 확인되면 잔액 충전\n"
        "🛒 **상품 구매** — 잔액으로 상품 구매 (24시간 자동 판매)\n"
        "💰 **내 정보** — 잔액·누적 충전 확인\n"
        "🔑 **키 등록** — 평생 무료 키를 등록하면 모든 상품이 0원",
    )
    await interaction.channel.send(embed=e, view=PanelView())
    await reply(interaction, embed("✅ 자판기 패널을 설치했어요.", color=COLOR_OK))


# -------------------------------------------------------- admin commands ----
def admin_group(name: str, description: str) -> app_commands.Group:
    return app_commands.Group(
        name=name, description=description, guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )


settings_group = admin_group("설정", "[관리자] 봇 설정")
points_group = admin_group("잔액관리", "[관리자] 유저 잔액 지급·차감")
product_group = admin_group("상품", "[관리자] 상품 관리")
stock_group = admin_group("재고", "[관리자] 재고 관리")
order_group = admin_group("주문", "[관리자] 주문 처리")
deposit_group = admin_group("입금", "[관리자] 계좌 입금 확인")
vault_group = admin_group("금고", "[관리자] 충전으로 들어온 실제 돈 관리")
lifetime_group = admin_group("평생", "[관리자] 평생 무료 회원·키 관리")


@settings_group.command(name="보기", description="현재 설정을 확인해요")
@admin_only()
async def set_show(interaction: discord.Interaction):
    g = interaction.guild_id
    log_ch = setting_int(g, "log_channel", 0)
    role = setting_int(g, "admin_role", 0)
    e = embed("⚙️ 현재 설정")
    e.add_field(name="로그 채널", value=f"<#{log_ch}>" if log_ch else "없음")
    e.add_field(name="관리자 역할", value=f"<@&{role}>" if role else "없음 (서버 관리 권한만)")
    e.add_field(name="출석 보상", value=f"{setting_int(g, 'daily_points', 0):,}원")
    e.add_field(name="최소 충전", value=f"{setting_int(g, 'min_charge', 1000):,}원")
    e.add_field(name="충전 보너스", value=f"{setting_int(g, 'charge_bonus', 0)}%")
    life_role = setting_int(g, "lifetime_role", 0)
    e.add_field(name="평생 회원 역할", value=f"<@&{life_role}>" if life_role else "없음")
    e.add_field(name="입금 계좌", value=db.get_setting(g, "bank_info") or "없음", inline=False)
    e.add_field(
        name="자동 입금 확인",
        value="켜짐" if WEBHOOK_SECRET and DEPOSIT_GUILD_ID == g else "꺼짐 (.env 확인)",
    )
    await reply(interaction, e)


@settings_group.command(name="로그채널", description="충전·구매 기록과 승인 버튼이 올라갈 채널")
@money_only()
async def set_log(interaction: discord.Interaction, 채널: discord.TextChannel):
    db.set_setting(interaction.guild_id, "log_channel", str(채널.id))
    await reply(interaction, embed("✅ 설정 완료", f"로그 채널: {채널.mention}", COLOR_OK))


@settings_group.command(name="계좌", description="충전할 때 보여 줄 입금 계좌")
@app_commands.describe(계좌정보="예: 카카오뱅크 3333-01-2345678 (예금주 홍길동)")
@money_only()
async def set_bank(interaction: discord.Interaction, 계좌정보: str):
    db.set_setting(interaction.guild_id, "bank_info", 계좌정보)
    await reply(interaction, embed("✅ 설정 완료", f"입금 계좌: {계좌정보}", COLOR_OK))


@settings_group.command(name="관리자역할", description="봇 관리 권한을 줄 역할")
@money_only()
async def set_admin_role(interaction: discord.Interaction, 역할: discord.Role):
    db.set_setting(interaction.guild_id, "admin_role", str(역할.id))
    await reply(interaction, embed("✅ 설정 완료", f"관리자 역할: {역할.mention}", COLOR_OK))


@settings_group.command(name="평생역할", description="평생 무료 회원에게 자동으로 줄 역할")
@money_only()
async def set_lifetime_role_cmd(interaction: discord.Interaction, 역할: discord.Role):
    db.set_setting(interaction.guild_id, "lifetime_role", str(역할.id))
    await reply(interaction, embed("✅ 설정 완료", f"평생 회원 역할: {역할.mention}", COLOR_OK))


@settings_group.command(name="출석보상", description="출석 체크 보상 (0 = 끔). 입금 없이 잔액이 생기니 주의하세요")
@money_only()
async def set_daily(interaction: discord.Interaction, 금액: app_commands.Range[int, 0, 1_000_000]):
    db.set_setting(interaction.guild_id, "daily_points", str(금액))
    await reply(interaction, embed("✅ 설정 완료", f"출석 보상: {금액:,}원", COLOR_OK))


@settings_group.command(name="최소충전", description="한 번에 충전할 수 있는 최소 금액")
@money_only()
async def set_min(interaction: discord.Interaction, 금액: app_commands.Range[int, 1, MAX_CHARGE]):
    db.set_setting(interaction.guild_id, "min_charge", str(금액))
    await reply(interaction, embed("✅ 설정 완료", f"최소 충전: {금액:,}원", COLOR_OK))


@settings_group.command(name="충전보너스", description="충전 시 추가로 얹어 주는 비율 (%)")
@money_only()
async def set_bonus(interaction: discord.Interaction, 퍼센트: app_commands.Range[int, 0, 100]):
    db.set_setting(interaction.guild_id, "charge_bonus", str(퍼센트))
    await reply(
        interaction,
        embed("✅ 설정 완료", f"충전 보너스: {퍼센트}% (10,000원 → {points_for(interaction.guild_id, 10000):,}원)", COLOR_OK),
    )


@points_group.command(name="지급", description="입금 없이 유저 잔액을 늘려요 (이벤트·테스트용, 금고에는 안 들어가요)")
@money_only()
async def pts_give(interaction: discord.Interaction, 유저: discord.Member,
                   금액: app_commands.Range[int, 1, 100_000_000], 사유: str = "관리자 지급"):
    balance = db.add_points(interaction.guild_id, 유저.id, 금액, f"{사유} (by {interaction.user.id})")
    await reply(interaction, embed("✅ 지급 완료", f"{유저.mention}에게 {금액:,}원 지급 → 잔액 {balance:,}원", COLOR_OK))
    await send_log(interaction.guild, embed(
        "➕ 잔액 지급", f"{interaction.user.mention} → {유저.mention}: **{금액:,}원**\n사유: {사유}"))


@points_group.command(name="차감", description="유저 잔액을 줄여요")
@money_only()
async def pts_take(interaction: discord.Interaction, 유저: discord.Member,
                   금액: app_commands.Range[int, 1, 100_000_000], 사유: str = "관리자 차감"):
    try:
        balance = db.add_points(interaction.guild_id, 유저.id, -금액, f"{사유} (by {interaction.user.id})")
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed("✅ 차감 완료", f"{유저.mention}에게서 {금액:,}원 차감 → 잔액 {balance:,}원", COLOR_OK))
    await send_log(interaction.guild, embed(
        "➖ 잔액 차감", f"{interaction.user.mention} → {유저.mention}: **{금액:,}원**\n사유: {사유}"))


@product_group.command(name="추가", description="새 상품을 등록해요")
@app_commands.describe(
    이름="상품 이름", 가격="가격 (원, 단위가 있으면 1단위 가격)", 종류="판매 방식", 설명="상품 설명",
    역할="종류가 '역할 지급'일 때 줄 역할", 분류="상점에서 묶어 보여 줄 분류 (예: 티켓)",
    단위="단위별 판매일 때 1단위 (예: 200개, 1마리). 비우면 고정 상품",
    최대수량="단위별 판매일 때 한 번에 살 수 있는 최대 단위 수",
    판매시작="이 시각(0~23시)부터 판매", 판매종료="이 시각(0~23시)까지 판매",
    주문양식="구매할 때 받을 정보", 추가입력="주문양식이 '코드 + 추가 입력'일 때 받을 내용 (예: 캐릭터 이름)",
)
@app_commands.choices(주문양식=[
    app_commands.Choice(name="없음", value="none"),
    app_commands.Choice(name="기종변경 코드 + 인증번호", value="codes"),
    app_commands.Choice(name="코드 + 추가 입력", value="detail"),
])
@app_commands.choices(종류=[
    app_commands.Choice(name="자동 전송 (재고를 DM으로 보냄)", value="stock"),
    app_commands.Choice(name="역할 지급", value="role"),
    app_commands.Choice(name="관리자 처리 (서비스 신청형)", value="manual"),
    app_commands.Choice(name="평생 무료 키 (구매하면 키 발급)", value="lifetime"),
])
@money_only()
async def prod_add(interaction: discord.Interaction, 이름: app_commands.Range[str, 1, 80],
                   가격: app_commands.Range[int, 0, 100_000_000], 종류: app_commands.Choice[str],
                   설명: app_commands.Range[str, 0, 500] = "", 역할: discord.Role | None = None,
                   분류: app_commands.Range[str, 1, 50] = "기타", 단위: app_commands.Range[str, 0, 20] = "",
                   최대수량: app_commands.Range[int, 1, 9999] = 99,
                   판매시작: app_commands.Range[int, 0, 23] | None = None,
                   판매종료: app_commands.Range[int, 0, 23] | None = None,
                   주문양식: app_commands.Choice[str] | None = None,
                   추가입력: app_commands.Range[str, 1, 45] | None = None):
    if 종류.value == "role" and 역할 is None:
        return await error(interaction, "역할 지급 상품은 `역할`을 꼭 정해 주세요.")
    if (판매시작 is None) != (판매종료 is None):
        return await error(interaction, "판매시작과 판매종료는 함께 정해 주세요.")
    form = {"codes": "", "detail": 추가입력 or "요청 내용"}.get(주문양식.value if 주문양식 else "none")
    pid = db.add_product(
        interaction.guild_id, 이름, 가격, 설명, 종류.value, 역할.id if 역할 else None,
        category=분류, unit=단위.strip(), max_qty=최대수량 if 단위.strip() else 1,
        sale_start=판매시작, sale_end=판매종료, form=form,
    )
    hint = {
        "stock": f"\n`/재고 추가 상품:{pid}`로 재고를 넣어 주세요.",
        "lifetime": "\n구매할 때마다 새 키가 자동으로 만들어져요. `/설정 평생역할`로 회원 역할도 정할 수 있어요.",
    }.get(종류.value, "")
    await reply(interaction, embed("✅ 상품 등록", f"#{pid} **{이름}** — {가격:,}원 ({종류.name}){hint}", COLOR_OK))


@product_group.command(name="수정", description="상품 정보를 바꿔요")
@app_commands.autocomplete(상품=product_autocomplete)
@money_only()
@app_commands.describe(
    판매시작="이 시각(0~23시)부터 판매, -1이면 시간 제한 없앰", 판매종료="이 시각(0~23시)까지 판매, -1이면 없앰",
    단위="1단위 (예: 200개). '없음'이면 고정 상품", 최대수량="한 번에 살 수 있는 최대 단위 수",
)
async def prod_edit(interaction: discord.Interaction, 상품: int, 이름: str | None = None,
                    가격: app_commands.Range[int, 0, 100_000_000] | None = None,
                    설명: str | None = None, 판매중: bool | None = None,
                    분류: app_commands.Range[str, 1, 50] | None = None,
                    단위: app_commands.Range[str, 1, 20] | None = None,
                    최대수량: app_commands.Range[int, 1, 9999] | None = None,
                    판매시작: app_commands.Range[int, -1, 23] | None = None,
                    판매종료: app_commands.Range[int, -1, 23] | None = None):
    ok = db.update_product(interaction.guild_id, 상품, name=이름, price=가격, description=설명,
                           active=None if 판매중 is None else int(판매중), category=분류,
                           unit=None if 단위 is None else ("" if 단위.strip() == "없음" else 단위.strip()),
                           max_qty=최대수량, sale_start=판매시작, sale_end=판매종료)
    if not ok:
        return await error(interaction, "바꿀 내용이 없거나 존재하지 않는 상품이에요.")
    await reply(interaction, embed("✅ 상품 수정 완료", f"상품 #{상품}", COLOR_OK))


@product_group.command(name="삭제", description="상품 판매를 중지해요 (구매 기록은 남아요)")
@app_commands.autocomplete(상품=product_autocomplete)
@money_only()
async def prod_delete(interaction: discord.Interaction, 상품: int):
    if not db.update_product(interaction.guild_id, 상품, active=0):
        return await error(interaction, "존재하지 않는 상품이에요.")
    await reply(interaction, embed("✅ 판매 중지", f"상품 #{상품}을(를) 상점에서 내렸어요.", COLOR_OK))


@product_group.command(name="목록", description="모든 상품을 확인해요")
@admin_only()
async def prod_list(interaction: discord.Interaction):
    products = db.list_products(interaction.guild_id, include_inactive=True)
    if not products:
        return await reply(interaction, embed("📦 상품 목록", "등록된 상품이 없어요."))
    lines: list[str] = []
    for cat in categories(products):
        lines.append(f"**📂 {cat}**")
        lines += [
            f"`#{p.id}` {p.name} — {price_label(p)}"
            + (f" (최대 {p.max_qty})" if p.unit else "")
            + (f" · {p.sale_hours()}" if p.sale_hours() else "")
            + (f" · 재고 {p.stock}" if p.kind == "stock" else "")
            + ("" if p.active else " · ~~판매 중지~~")
            for p in products if p.category == cat
        ]
    await reply(interaction, embed("📦 상품 목록", "\n".join(lines)[:4000]))


@product_group.command(name="기본목록", description="냥코 서비스 상품 목록을 한 번에 등록해요 (이미 있는 이름은 건너뜀)")
@money_only()
async def prod_catalog(interaction: discord.Interaction):
    added, skipped = db.add_catalog(interaction.guild_id, catalog.CATALOG)
    await reply(interaction, embed(
        "✅ 기본 상품 등록",
        f"{added}개를 등록했어요." + (f" (이미 있는 {skipped}개는 건너뜀)" if skipped else "")
        + "\n`/상품 목록`으로 확인하고, `/상품 수정`으로 가격·수량을 바꿀 수 있어요.",
        COLOR_OK,
    ))


class StockModal(discord.ui.Modal, title="재고 추가"):
    items = discord.ui.TextInput(
        label="재고 (한 줄에 하나씩)", style=discord.TextStyle.paragraph,
        placeholder="CODE-AAAA-1111\nCODE-BBBB-2222", max_length=4000,
    )

    def __init__(self, product_id: int) -> None:
        super().__init__()
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items = [line.strip() for line in str(self.items.value).splitlines() if line.strip()]
        try:
            total = db.add_stock(interaction.guild_id, self.product_id, items)
        except ShopError as exc:
            return await error(interaction, str(exc))
        await reply(interaction, embed(
            "✅ 재고 추가", f"상품 #{self.product_id}에 {len(items)}개 추가 → 총 {total}개", COLOR_OK))


@stock_group.command(name="추가", description="자동 전송 상품에 재고를 넣어요")
@app_commands.autocomplete(상품=product_autocomplete)
@money_only()
async def stock_add(interaction: discord.Interaction, 상품: int):
    product = db.get_product(interaction.guild_id, 상품)
    if product is None or product.kind != "stock":
        return await error(interaction, "자동 전송 상품만 재고를 넣을 수 있어요.")
    await interaction.response.send_modal(StockModal(상품))


@stock_group.command(name="비우기", description="상품의 남은 재고를 모두 지워요")
@app_commands.autocomplete(상품=product_autocomplete)
@money_only()
async def stock_clear(interaction: discord.Interaction, 상품: int):
    try:
        n = db.clear_stock(interaction.guild_id, 상품)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed("✅ 재고 비우기", f"상품 #{상품}의 재고 {n}개를 지웠어요.", COLOR_OK))


@order_group.command(name="환불", description="주문을 환불하고 잔액으로 돌려줘요")
@money_only()
async def order_refund(interaction: discord.Interaction, 주문번호: int):
    try:
        order = db.refund_order(interaction.guild_id, 주문번호)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await after_refund(interaction.guild, order)
    await reply(interaction, embed("✅ 환불 완료", f"주문 #{주문번호}: <@{order['user_id']}>에게 {order['price']:,}원 반환", COLOR_OK))
    await dm(order["user_id"], embed(
        "↩️ 주문 환불", f"주문 #{주문번호} **{order['product_name']}**이(가) 환불되어 {order['price']:,}원을 돌려드렸어요.",
        COLOR_ERR))


@order_group.command(name="완료", description="관리자 처리 주문을 완료로 표시해요")
@admin_only()
async def order_done(interaction: discord.Interaction, 주문번호: int):
    try:
        order = db.complete_order(interaction.guild_id, 주문번호)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed("✅ 처리 완료", f"주문 #{주문번호}", COLOR_OK))
    await dm(order["user_id"], embed(
        "✅ 주문 처리 완료", f"주문 #{주문번호} **{order['product_name']}** 처리가 완료됐어요!", COLOR_OK))


@deposit_group.command(name="목록", description="충전 신청과 연결되지 않은 입금을 확인해요")
@admin_only()
async def dep_list(interaction: discord.Interaction):
    rows = db.unmatched_deposits(interaction.guild_id)
    if not rows:
        return await reply(interaction, embed("🏦 미확인 입금", "확인되지 않은 입금이 없어요."))
    lines = [f"`#{r['id']}` **{r['depositor']}** — {r['amount']:,}원 · {fmt_time(r['created_at'])}" for r in rows]
    await reply(interaction, embed("🏦 미확인 입금", "\n".join(lines) + "\n\n`/입금 연결`로 유저에게 충전할 수 있어요."))


@deposit_group.command(name="연결", description="확인되지 않은 입금을 유저에게 충전해요")
@money_only()
async def dep_link(interaction: discord.Interaction, 입금번호: int, 유저: discord.Member):
    dep = next((d for d in db.unmatched_deposits(interaction.guild_id, 1000) if d["id"] == 입금번호), None)
    points = points_for(interaction.guild_id, dep["amount"]) if dep else 0
    try:
        dep, balance = db.link_deposit(interaction.guild_id, 입금번호, 유저.id, points, interaction.user.id)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed(
        "✅ 충전 완료", f"입금 #{입금번호} ({dep['depositor']}, {dep['amount']:,}원) → {유저.mention}에게 {points:,}원", COLOR_OK))
    await dm(유저.id, embed(
        "✅ 충전 완료", f"**{interaction.guild.name}**에서 **{points:,}원**이 충전됐어요!\n현재 잔액: **{balance:,}원**", COLOR_OK))
    await send_log(interaction.guild, embed(
        "🔗 입금 수동 연결", f"{interaction.user.mention}: 입금 #{입금번호} {dep['amount']:,}원 → {유저.mention} ({points:,}원)", COLOR_OK))


@deposit_group.command(name="취소", description="승인했지만 실제 입금이 없던 충전을 취소해요 (잔액·금고에서 빠져요)")
@app_commands.describe(신청번호="충전 신청 번호 (로그 채널의 '신청 #N')")
@money_only()
async def dep_cancel(interaction: discord.Interaction, 신청번호: int):
    try:
        charge, balance = db.cancel_charge(interaction.guild_id, 신청번호, interaction.user.id)
    except ShopError as exc:
        return await error(interaction, str(exc))
    note = f"<@{charge['user_id']}>의 충전 #{신청번호} ({charge['depositor']}, {charge['amount']:,}원)을 취소했어요.\n" \
           f"잔액에서 {charge['points']:,}원을 뺐어요 → 현재 잔액 **{balance:,}원**"
    if balance < 0:
        note += "\n(이미 사용한 금액이 있어서 잔액이 마이너스예요. 다시 충전하기 전까지 구매할 수 없어요.)"
    await reply(interaction, embed("✅ 충전 취소", note, COLOR_OK))
    await send_log(interaction.guild, embed("🚫 충전 취소", f"{interaction.user.mention}: {note}", COLOR_ERR))
    await update_charge_log(신청번호, f"{interaction.user.mention}님이 입금이 없어서 취소했어요.")


def phone_guide(url: str) -> str:
    """Setup steps for whoever owns the bank account's Android phone."""
    return f"""[토스뱅크 입금 알림 연결 방법] (안드로이드, 약 5분)

1. Play 스토어에서 "MacroDroid" 설치 후 실행 (무료)
2. "매크로 추가(Add Macro)" 누르기
3. 트리거(Triggers) ＋ → "알림(Notification)" → "알림 수신(Notification Received)"
   - 알림 접근 권한을 허용해 주세요
   - 앱 선택: "토스뱅크"만 체크
   - 텍스트: "모두(Any)" 선택 → 확인
4. 동작(Actions) ＋ → "웹 상호작용(Web Interactions)" 또는 "연결(Connectivity)" → "HTTP 요청(HTTP Request)"
   - 방식(Method): POST
   - URL: 아래 주소를 그대로 붙여넣기
   {url}
   - 본문(Body) 내용: [notification_title] [notification]
     (오른쪽 "..." 또는 매직 텍스트 버튼에서 '알림 제목', '알림 텍스트'를 넣어도 돼요)
   - 콘텐츠 유형(Content type): text/plain → 확인
5. 매크로 이름: "입금 알림 전달" → 저장(✓)
6. 매크로를 길게 눌러 "테스트(Test macro)" → 관리자에게 "연결 확인" 메시지가 가요
7. 설정 → 배터리 → MacroDroid를 "제한 없음(최적화 안 함)"으로 바꾸기

※ 토스뱅크 앱의 알림만 이 주소로 보내져요. 다른 앱 알림은 보내지 않아요.
※ 이 주소는 비밀번호와 같아요. 다른 사람에게 보여 주지 마세요."""


@deposit_group.command(name="폰설정", description="은행 계좌 주인에게 보낼 '입금 알림 연결 방법'을 만들어요 (주소 포함)")
@admin_only()
async def dep_phone(interaction: discord.Interaction):
    if not WEBHOOK_PUBLIC_URL or not WEBHOOK_SECRET:
        return await error(interaction, "아직 서버에 https 주소가 없어요. 서버에서 `sh deploy/https.sh` 를 먼저 실행해 주세요.")
    url = f"{WEBHOOK_PUBLIC_URL}/deposit?token={WEBHOOK_SECRET}"
    e = embed(
        "📱 입금 알림 연결 방법",
        "아래 내용을 복사해서 **계좌 주인에게만** 보내 주세요. (주소에 비밀번호가 들어 있어요)\n"
        f"```\n{phone_guide(url)}\n```",
        COLOR_INFO,
    )
    e.set_footer(text="설정이 끝나면 로그 채널에 '📱 입금 알림 폰 연결 확인'이 올라와요.")
    await reply(interaction, e)


@deposit_group.command(name="테스트", description="입금 알림 문자가 제대로 읽히는지 확인해요 (실제 충전 안 됨)")
@admin_only()
async def dep_test(interaction: discord.Interaction, 알림문자: str):
    parsed = bank.parse_deposit(알림문자)
    if parsed is None:
        return await error(interaction, "입금으로 읽지 못했어요. 출금/광고 문자이거나 형식이 달라요 (DEPOSIT_REGEX 설정 필요).")
    await reply(interaction, embed("✅ 읽기 성공", f"입금자명: **{parsed.name}**\n금액: **{parsed.amount:,}원**", COLOR_OK))


@lifetime_group.command(name="발급", description="평생 무료 키를 새로 만들어요 (이벤트·선물용)")
@app_commands.describe(개수="만들 키 개수")
@money_only()
async def life_issue(interaction: discord.Interaction, 개수: app_commands.Range[int, 1, 20] = 1):
    keys = [db.issue_key(interaction.guild_id, interaction.user.id) for _ in range(개수)]
    await reply(interaction, embed(
        "🔑 평생 무료 키 발급", "```\n" + "\n".join(keys) + "\n```\n받는 사람이 `/키등록`으로 등록하면 돼요.",
        COLOR_OK,
    ))
    await send_log(interaction.guild, embed(
        "🔑 평생 무료 키 발급", f"{interaction.user.mention}님이 키 {개수}개를 발급했어요.", COLOR_INFO))


@lifetime_group.command(name="지급", description="키 없이 바로 평생 무료 회원으로 만들어요")
@money_only()
async def life_grant(interaction: discord.Interaction, 유저: discord.Member):
    if not db.grant_lifetime(interaction.guild_id, 유저.id):
        return await error(interaction, "이미 평생 무료 회원이에요.")
    await set_lifetime_role(interaction.guild, 유저.id, give=True)
    await reply(interaction, embed("✅ 지급 완료", f"{유저.mention}님이 평생 무료 회원이 됐어요.", COLOR_OK))
    await dm(유저.id, embed(
        "👑 평생 무료 회원", f"**{interaction.guild.name}**에서 평생 무료 회원이 됐어요! 모든 상품을 0원으로 구매할 수 있어요.",
        COLOR_OK))
    await send_log(interaction.guild, embed(
        "👑 평생 무료 회원 지급", f"{interaction.user.mention} → {유저.mention}", COLOR_OK))


@lifetime_group.command(name="회수", description="평생 무료 회원을 해제해요 (등록한 키도 사용 중지)")
@admin_only()
async def life_revoke(interaction: discord.Interaction, 유저: discord.Member):
    if not db.revoke_lifetime(interaction.guild_id, 유저.id):
        return await error(interaction, "평생 무료 회원이 아니에요.")
    await set_lifetime_role(interaction.guild, 유저.id, give=False)
    await reply(interaction, embed("✅ 회수 완료", f"{유저.mention}님의 평생 무료 회원을 해제했어요.", COLOR_OK))
    await send_log(interaction.guild, embed(
        "⛔ 평생 무료 회원 회수", f"{interaction.user.mention} → {유저.mention}", COLOR_ERR))


@lifetime_group.command(name="목록", description="평생 무료 회원과 아직 안 쓴 키를 확인해요")
@admin_only()
async def life_list(interaction: discord.Interaction):
    members, unused = db.lifetime_summary(interaction.guild_id)
    e = embed("👑 평생 무료 회원")
    e.add_field(
        name=f"회원 ({len(members)}명)",
        value="\n".join(f"<@{m['user_id']}> · {fmt_time(m['granted_at'])}" for m in members[:20]) or "없음",
        inline=False,
    )
    e.add_field(
        name=f"사용 안 된 키 ({len(unused)}개)",
        value="\n".join(f"`{k['key']}` · {fmt_time(k['created_at'])}" for k in unused[:15]) or "없음",
        inline=False,
    )
    await reply(interaction, e)


def vault_embed(v: dict[str, int], title: str = "🏦 금고") -> discord.Embed:
    e = embed(title, f"## 출금 가능: {v['available']:,}원", COLOR_INFO)
    e.add_field(name="총 충전 금액", value=f"{v['received']:,}원 ({v['charges']}건)")
    e.add_field(name="출금한 금액", value=f"{v['withdrawn']:,}원")
    if v["unmatched_count"]:
        e.add_field(
            name="미확인 입금", value=f"{v['unmatched']:,}원 ({v['unmatched_count']}건)\n"
            "아직 잔액이 충전되지 않아 출금 가능 금액에서 빠져 있어요.", inline=False)
    e.set_footer(text="실제 돈은 입금 계좌에 있어요. 계좌에서 돈을 뺀 뒤 /금고 출금으로 기록하세요.")
    return e


@vault_group.command(name="잔액", description="충전으로 들어온 돈과 출금 가능 금액을 확인해요")
@admin_only()
async def vault_balance(interaction: discord.Interaction):
    await reply(interaction, vault_embed(db.vault(interaction.guild_id)))


@vault_group.command(name="출금", description="금고에서 돈을 꺼낸 것을 기록해요")
@app_commands.describe(금액="꺼낸 금액 (원)", 메모="예: 10월 정산, 서버 운영비")
@money_only()
async def vault_withdraw(interaction: discord.Interaction,
                         금액: app_commands.Range[int, 1, 1_000_000_000], 메모: str = ""):
    try:
        v = db.withdraw(interaction.guild_id, interaction.user.id, 금액, 메모)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, vault_embed(v, f"✅ {금액:,}원 출금 기록 완료"))
    await send_log(interaction.guild, embed(
        "💸 금고 출금", f"{interaction.user.mention}님이 **{금액:,}원**을 출금했어요.\n"
        f"메모: {메모 or '없음'}\n남은 금고: **{v['available']:,}원**", COLOR_WARN))


@vault_group.command(name="내역", description="최근 출금 기록을 확인해요")
@admin_only()
async def vault_history(interaction: discord.Interaction):
    rows = db.recent_withdrawals(interaction.guild_id)
    if not rows:
        return await reply(interaction, embed("💸 출금 내역", "아직 출금 기록이 없어요."))
    lines = [
        f"`#{r['id']}` <@{r['admin_id']}> — **{r['amount']:,}원** · {fmt_time(r['created_at'])}"
        + (f"\n　{r['note']}" if r["note"] else "")
        for r in rows
    ]
    await reply(interaction, embed("💸 최근 출금 내역", "\n".join(lines)))


# ------------------------------------------------------------------- main ---
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 없어요. .env 파일에 봇 토큰을 넣어 주세요.")
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
