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

import datetime
import hashlib
import hmac
import json
import logging
import os
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
from db import Database, ShopError

# ---------------------------------------------------------------- config ----
TOKEN = os.environ.get("DISCORD_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "shop.db")
# The server whose bank account the deposit webhook belongs to.
DEPOSIT_GUILD_ID = int(os.environ.get("DEPOSIT_GUILD_ID", "0") or 0)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("PORT", os.environ.get("WEBHOOK_PORT", "8080")))
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

KIND_LABEL = {"stock": "자동 전송", "role": "역할 지급", "manual": "관리자 처리"}
STATUS_LABEL = {"done": "완료", "pending": "처리 대기", "refunded": "환불됨"}

log = logging.getLogger("shopbot")
db = Database(DB_PATH)


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


def kst_day_start() -> int:
    now = datetime.datetime.now(KST)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def fmt_time(ts: int) -> str:
    return f"<t:{ts}:f>"


# ------------------------------------------------------------ user flows ----
async def show_balance(interaction: discord.Interaction, user: discord.abc.User) -> None:
    row = db.get_user(interaction.guild_id, user.id)
    e = embed(f"💰 {user.display_name}님의 포인트", f"## {row['balance']:,} P")
    e.add_field(name="누적 충전", value=f"{row['total_charged']:,} P")
    e.add_field(name="누적 사용", value=f"{row['total_spent']:,} P")
    await reply(interaction, e)


async def claim_daily(interaction: discord.Interaction) -> None:
    amount = setting_int(interaction.guild_id, "daily_points", 100)
    if amount <= 0:
        return await error(interaction, "이 서버에서는 출석 체크를 사용하지 않아요.")
    balance = db.claim_daily(interaction.guild_id, interaction.user.id, amount, kst_day_start())
    if balance is None:
        return await error(interaction, "오늘은 이미 출석했어요. 내일(자정 이후) 다시 와 주세요!")
    await reply(
        interaction,
        embed("✅ 출석 완료", f"**{amount:,} P**를 받았어요!\n현재 잔액: **{balance:,} P**", COLOR_OK),
    )


async def show_history(interaction: discord.Interaction) -> None:
    orders = db.recent_orders(interaction.guild_id, interaction.user.id)
    if not orders:
        return await reply(interaction, embed("🧾 구매 내역", "아직 구매한 상품이 없어요."))
    lines = [
        f"`#{o['id']}` **{o['product_name']}** — {o['price']:,} P · "
        f"{STATUS_LABEL.get(o['status'], o['status'])} · {fmt_time(o['created_at'])}"
        for o in orders
    ]
    await reply(interaction, embed("🧾 최근 구매 내역", "\n".join(lines)))


async def open_shop(interaction: discord.Interaction) -> None:
    products = db.list_products(interaction.guild_id)
    if not products:
        return await error(interaction, "아직 판매 중인 상품이 없어요.")
    balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
    e = embed("🛒 상점", f"보유 포인트: **{balance:,} P**\n아래 메뉴에서 구매할 상품을 골라 주세요.")
    for p in products[:25]:
        stock = f" · 재고 {p.stock}개" if p.kind == "stock" else ""
        e.add_field(
            name=f"#{p.id} {p.name} — {p.price:,} P",
            value=f"{p.description or '설명 없음'}\n`{KIND_LABEL[p.kind]}{stock}`",
            inline=False,
        )
    await reply(interaction, e, ShopView(products))


async def open_charge(interaction: discord.Interaction) -> None:
    if not db.get_setting(interaction.guild_id, "bank_info"):
        return await error(interaction, "관리자가 아직 입금 계좌를 설정하지 않았어요.")
    await interaction.response.send_modal(ChargeModal())


# ------------------------------------------------------------------- shop ---
class ShopView(discord.ui.View):
    def __init__(self, products: list) -> None:
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(
                label=f"{p.name}"[:100],
                description=(
                    f"{p.price:,} P · " + ("품절" if p.kind == "stock" and p.stock == 0
                                           else KIND_LABEL[p.kind])
                )[:100],
                value=str(p.id),
            )
            for p in products[:25]
        ]
        select = discord.ui.Select(placeholder="구매할 상품 선택", options=options)
        select.callback = self.on_select
        self.add_item(select)
        self.select = select

    async def on_select(self, interaction: discord.Interaction) -> None:
        product = db.get_product(interaction.guild_id, int(self.select.values[0]))
        if product is None or not product.active:
            return await error(interaction, "판매 중인 상품이 아니에요.")
        balance = db.get_user(interaction.guild_id, interaction.user.id)["balance"]
        e = embed(
            "🛍️ 구매 확인",
            f"**{product.name}**을(를) **{product.price:,} P**에 구매할까요?\n\n"
            f"보유 포인트: {balance:,} P → 구매 후: {balance - product.price:,} P",
            COLOR_WARN,
        )
        await interaction.response.send_message(
            embed=e, view=ConfirmBuyView(product.id), ephemeral=True
        )


class ConfirmBuyView(discord.ui.View):
    def __init__(self, product_id: int) -> None:
        super().__init__(timeout=60)
        self.product_id = product_id

    @discord.ui.button(label="구매하기", style=discord.ButtonStyle.success, emoji="✅")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)
        await purchase(interaction, self.product_id)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embed("구매를 취소했어요.", color=COLOR_INFO), view=None
        )


async def purchase(interaction: discord.Interaction, product_id: int) -> None:
    guild = interaction.guild
    try:
        result = db.purchase(guild.id, interaction.user.id, product_id)
    except ShopError as exc:
        return await error(interaction, str(exc))
    product = result.product

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
                "역할을 지급하지 못해 포인트를 돌려드렸어요. 관리자에게 문의해 주세요.\n"
                "(봇의 역할이 지급할 역할보다 위에 있어야 해요)",
            )

    e = embed("🎉 구매 완료", f"**{product.name}** 구매가 완료됐어요!", COLOR_OK)
    e.add_field(name="주문 번호", value=f"#{result.order_id}")
    e.add_field(name="사용 포인트", value=f"{product.price:,} P")
    e.add_field(name="남은 포인트", value=f"{result.balance:,} P")
    if product.kind == "stock":
        e.add_field(name="📦 상품 내용", value=f"||{result.delivered[:1000]}||", inline=False)
        dm_embed = embed(f"📦 {product.name} 구매 상품", f"```\n{result.delivered[:3900]}\n```", COLOR_OK)
        dm_embed.set_footer(text=f"{guild.name} · 주문 #{result.order_id}")
        if not await dm(interaction.user.id, dm_embed):
            e.set_footer(text="DM을 보낼 수 없어서 여기에만 표시했어요. 꼭 따로 저장해 주세요!")
    elif product.kind == "role":
        e.add_field(name="지급된 역할", value=f"<@&{product.role_id}>", inline=False)
    else:
        e.add_field(
            name="안내", value="관리자가 확인 후 처리해 드릴 거예요. 잠시만 기다려 주세요!", inline=False
        )
    await reply(interaction, e)

    log_e = embed("🛒 구매", color=COLOR_INFO)
    log_e.add_field(name="구매자", value=interaction.user.mention)
    log_e.add_field(name="상품", value=f"#{product.id} {product.name}")
    log_e.add_field(name="가격", value=f"{product.price:,} P")
    log_e.set_footer(text=f"주문 #{result.order_id}")
    view = None
    if product.kind == "manual":
        log_e.title = "🛎️ 처리가 필요한 주문"
        log_e.color = COLOR_WARN
        view = discord.ui.View(timeout=None)
        view.add_item(OrderButton("done", result.order_id))
        view.add_item(OrderButton("refund", result.order_id))
    await send_log(guild, log_e, view)


# ---------------------------------------------------------------- charge ----
class ChargeModal(discord.ui.Modal, title="포인트 충전 신청"):
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
                embed("✅ 충전 완료", f"입금이 확인되어 **{points:,} P**가 충전됐어요!\n"
                      f"현재 잔액: **{balance:,} P**", COLOR_OK),
            )
            charge = db.get_charge(charge_id)
            return await send_log(interaction.guild, charge_embed(charge, "자동 승인 (입금 먼저 확인됨)"))

        e = embed(
            "🏦 입금 안내",
            "아래 계좌로 **정확한 금액**을 **신청한 입금자명**으로 보내 주세요.\n"
            "입금이 확인되면 자동으로 포인트가 충전되고 DM으로 알려 드려요.",
            COLOR_INFO,
        )
        e.add_field(name="입금 계좌", value=db.get_setting(guild_id, "bank_info"), inline=False)
        e.add_field(name="입금 금액", value=f"**{won:,}원**")
        e.add_field(name="입금자명", value=f"**{name}**")
        e.add_field(name="충전될 포인트", value=f"{points:,} P")
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
    }
    title, color = titles[charge["status"]]
    e = embed(title, status or "", color)
    e.add_field(name="신청자", value=f"<@{charge['user_id']}>")
    e.add_field(name="금액", value=f"{charge['amount']:,}원")
    e.add_field(name="입금자명", value=charge["depositor"])
    if charge["status"] == "approved":
        e.add_field(name="지급 포인트", value=f"{charge['points']:,} P")
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
        guild_id = interaction.guild_id
        charge = db.get_charge(self.charge_id)
        approve = self.action == "ok"
        points = points_for(guild_id, charge["amount"]) if charge else 0
        try:
            charge, balance = db.resolve_charge(
                guild_id, self.charge_id, approve, interaction.user.id, points
            )
        except ShopError as exc:
            return await error(interaction, str(exc))
        charge = db.get_charge(self.charge_id)
        await interaction.response.edit_message(
            embed=charge_embed(charge, f"{interaction.user.mention}님이 처리했어요."), view=None
        )
        if approve:
            await dm(charge["user_id"], embed(
                "✅ 충전 완료",
                f"**{interaction.guild.name}**에서 **{points:,} P**가 충전됐어요!\n현재 잔액: **{balance:,} P**",
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
        try:
            if self.action == "done":
                order = db.complete_order(interaction.guild_id, self.order_id)
                text, color = "✅ 주문 처리 완료", COLOR_OK
                user_msg = f"주문 #{order['id']} **{order['product_name']}** 처리가 완료됐어요!"
            else:
                order = db.refund_order(interaction.guild_id, self.order_id)
                text, color = "↩️ 주문 환불", COLOR_ERR
                user_msg = (f"주문 #{order['id']} **{order['product_name']}**이(가) 환불되어 "
                            f"{order['price']:,} P를 돌려드렸어요.")
        except ShopError as exc:
            return await error(interaction, str(exc))
        e = interaction.message.embeds[0] if interaction.message.embeds else embed(text)
        e.title, e.color = text, color
        e.description = f"{interaction.user.mention}님이 처리했어요."
        await interaction.response.edit_message(embed=e, view=None)
        await dm(order["user_id"], embed(text, user_msg, color))


# ----------------------------------------------------------------- panel ----
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

    @discord.ui.button(label="출석 체크", emoji="📅", style=discord.ButtonStyle.secondary, custom_id="panel:daily")
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await claim_daily(interaction)


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
            f"입금 **{amount:,}원**이 확인되어 **{charge['points']:,} P**가 충전됐어요!\n"
            f"현재 잔액: **{balance:,} P**",
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
            if "입금" in text:
                await send_log(client.get_guild(DEPOSIT_GUILD_ID), embed(
                    "⚠️ 읽지 못한 입금 알림",
                    f"```\n{text[:1500]}\n```\n입금이 맞다면 `/포인트 지급`으로 직접 처리하고, "
                    "이 형식을 읽도록 DEPOSIT_REGEX를 설정해 주세요.",
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
        self.add_dynamic_items(ChargeButton, OrderButton)
        for group in (settings_group, points_group, product_group, stock_group,
                      order_group, deposit_group, vault_group):
            self.tree.add_command(group)
        if DEV_GUILD_ID:
            guild = discord.Object(DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

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


client = ShopBot()
tree = client.tree


@tasks.loop(minutes=5)
async def expire_charges() -> None:
    for charge in db.expire_charges(int(time.time()) - CHARGE_EXPIRE_MINUTES * 60):
        await update_charge_log(charge["id"], "입금이 확인되지 않아 만료됐어요.")


@tree.error
async def on_app_command_error(interaction: discord.Interaction, exc: app_commands.AppCommandError):
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


async def product_autocomplete(interaction: discord.Interaction, current: str):
    products = db.list_products(interaction.guild_id, include_inactive=True)
    return [
        app_commands.Choice(name=f"#{p.id} {p.name} ({p.price:,} P)"[:100], value=p.id)
        for p in products
        if current.lower() in p.name.lower() or current == str(p.id)
    ][:25]


# --------------------------------------------------------- user commands ----
@tree.command(name="잔액", description="포인트 잔액을 확인해요")
@app_commands.guild_only()
@app_commands.describe(유저="확인할 유저 (비우면 나)")
async def cmd_balance(interaction: discord.Interaction, 유저: discord.Member | None = None):
    if 유저 is not None and 유저 != interaction.user and not is_admin(interaction.user):
        return await error(interaction, "다른 유저의 잔액은 관리자만 볼 수 있어요.")
    await show_balance(interaction, 유저 or interaction.user)


@tree.command(name="출석", description="하루 한 번 출석 체크로 포인트를 받아요")
@app_commands.guild_only()
async def cmd_daily(interaction: discord.Interaction):
    await claim_daily(interaction)


@tree.command(name="충전", description="계좌 입금으로 포인트를 충전해요")
@app_commands.guild_only()
async def cmd_charge(interaction: discord.Interaction):
    await open_charge(interaction)


@tree.command(name="상점", description="포인트로 상품을 구매해요")
@app_commands.guild_only()
async def cmd_shop(interaction: discord.Interaction):
    await open_shop(interaction)


@tree.command(name="구매내역", description="최근 구매 내역을 확인해요")
@app_commands.guild_only()
async def cmd_history(interaction: discord.Interaction):
    await show_history(interaction)


@tree.command(name="랭킹", description="포인트 보유 순위를 확인해요")
@app_commands.guild_only()
async def cmd_rank(interaction: discord.Interaction):
    rows = db.leaderboard(interaction.guild_id)
    if not rows:
        return await reply(interaction, embed("🏆 포인트 랭킹", "아직 포인트를 가진 사람이 없어요."))
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — **{r['balance']:,} P**"
        for i, r in enumerate(rows)
    ]
    await reply(interaction, embed("🏆 포인트 랭킹", "\n".join(lines)))


@tree.command(name="자판기설치", description="[관리자] 이 채널에 자판기 패널을 올려요")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@admin_only()
async def cmd_panel(interaction: discord.Interaction):
    e = embed(
        f"🏪 {interaction.guild.name} 자판기",
        "아래 버튼으로 포인트를 충전하고 상품을 구매할 수 있어요.\n\n"
        "💳 **충전** — 계좌 입금 후 자동으로 포인트 충전\n"
        "🛒 **상품 구매** — 포인트로 상품 구매 (24시간 자동 판매)\n"
        "💰 **내 정보** — 잔액·누적 충전 확인\n"
        "📅 **출석 체크** — 하루 한 번 무료 포인트",
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
points_group = admin_group("포인트", "[관리자] 포인트 지급·차감")
product_group = admin_group("상품", "[관리자] 상품 관리")
stock_group = admin_group("재고", "[관리자] 재고 관리")
order_group = admin_group("주문", "[관리자] 주문 처리")
deposit_group = admin_group("입금", "[관리자] 계좌 입금 확인")
vault_group = admin_group("금고", "[관리자] 충전으로 들어온 실제 돈 관리")


@settings_group.command(name="보기", description="현재 설정을 확인해요")
@admin_only()
async def set_show(interaction: discord.Interaction):
    g = interaction.guild_id
    log_ch = setting_int(g, "log_channel", 0)
    role = setting_int(g, "admin_role", 0)
    e = embed("⚙️ 현재 설정")
    e.add_field(name="로그 채널", value=f"<#{log_ch}>" if log_ch else "없음")
    e.add_field(name="관리자 역할", value=f"<@&{role}>" if role else "없음 (서버 관리 권한만)")
    e.add_field(name="출석 포인트", value=f"{setting_int(g, 'daily_points', 100):,} P")
    e.add_field(name="최소 충전", value=f"{setting_int(g, 'min_charge', 1000):,}원")
    e.add_field(name="충전 보너스", value=f"{setting_int(g, 'charge_bonus', 0)}%")
    e.add_field(name="입금 계좌", value=db.get_setting(g, "bank_info") or "없음", inline=False)
    e.add_field(
        name="자동 입금 확인",
        value="켜짐" if WEBHOOK_SECRET and DEPOSIT_GUILD_ID == g else "꺼짐 (.env 확인)",
    )
    await reply(interaction, e)


@settings_group.command(name="로그채널", description="충전·구매 기록과 승인 버튼이 올라갈 채널")
@admin_only()
async def set_log(interaction: discord.Interaction, 채널: discord.TextChannel):
    db.set_setting(interaction.guild_id, "log_channel", str(채널.id))
    await reply(interaction, embed("✅ 설정 완료", f"로그 채널: {채널.mention}", COLOR_OK))


@settings_group.command(name="계좌", description="충전할 때 보여 줄 입금 계좌")
@app_commands.describe(계좌정보="예: 카카오뱅크 3333-01-2345678 (예금주 홍길동)")
@admin_only()
async def set_bank(interaction: discord.Interaction, 계좌정보: str):
    db.set_setting(interaction.guild_id, "bank_info", 계좌정보)
    await reply(interaction, embed("✅ 설정 완료", f"입금 계좌: {계좌정보}", COLOR_OK))


@settings_group.command(name="관리자역할", description="봇 관리 권한을 줄 역할")
@admin_only()
async def set_admin_role(interaction: discord.Interaction, 역할: discord.Role):
    db.set_setting(interaction.guild_id, "admin_role", str(역할.id))
    await reply(interaction, embed("✅ 설정 완료", f"관리자 역할: {역할.mention}", COLOR_OK))


@settings_group.command(name="출석포인트", description="출석 체크 보상 (0이면 출석 끔)")
@admin_only()
async def set_daily(interaction: discord.Interaction, 포인트: app_commands.Range[int, 0, 1_000_000]):
    db.set_setting(interaction.guild_id, "daily_points", str(포인트))
    await reply(interaction, embed("✅ 설정 완료", f"출석 포인트: {포인트:,} P", COLOR_OK))


@settings_group.command(name="최소충전", description="한 번에 충전할 수 있는 최소 금액")
@admin_only()
async def set_min(interaction: discord.Interaction, 금액: app_commands.Range[int, 1, MAX_CHARGE]):
    db.set_setting(interaction.guild_id, "min_charge", str(금액))
    await reply(interaction, embed("✅ 설정 완료", f"최소 충전: {금액:,}원", COLOR_OK))


@settings_group.command(name="충전보너스", description="충전 시 추가로 주는 포인트 비율 (%)")
@admin_only()
async def set_bonus(interaction: discord.Interaction, 퍼센트: app_commands.Range[int, 0, 100]):
    db.set_setting(interaction.guild_id, "charge_bonus", str(퍼센트))
    await reply(
        interaction,
        embed("✅ 설정 완료", f"충전 보너스: {퍼센트}% (10,000원 → {points_for(interaction.guild_id, 10000):,} P)", COLOR_OK),
    )


@points_group.command(name="지급", description="유저에게 포인트를 지급해요")
@admin_only()
async def pts_give(interaction: discord.Interaction, 유저: discord.Member,
                   포인트: app_commands.Range[int, 1, 100_000_000], 사유: str = "관리자 지급"):
    balance = db.add_points(interaction.guild_id, 유저.id, 포인트, f"{사유} (by {interaction.user.id})")
    await reply(interaction, embed("✅ 지급 완료", f"{유저.mention}에게 {포인트:,} P 지급 → 잔액 {balance:,} P", COLOR_OK))
    await send_log(interaction.guild, embed(
        "➕ 포인트 지급", f"{interaction.user.mention} → {유저.mention}: **{포인트:,} P**\n사유: {사유}"))


@points_group.command(name="차감", description="유저의 포인트를 차감해요")
@admin_only()
async def pts_take(interaction: discord.Interaction, 유저: discord.Member,
                   포인트: app_commands.Range[int, 1, 100_000_000], 사유: str = "관리자 차감"):
    try:
        balance = db.add_points(interaction.guild_id, 유저.id, -포인트, f"{사유} (by {interaction.user.id})")
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed("✅ 차감 완료", f"{유저.mention}에게서 {포인트:,} P 차감 → 잔액 {balance:,} P", COLOR_OK))
    await send_log(interaction.guild, embed(
        "➖ 포인트 차감", f"{interaction.user.mention} → {유저.mention}: **{포인트:,} P**\n사유: {사유}"))


@product_group.command(name="추가", description="새 상품을 등록해요")
@app_commands.describe(
    이름="상품 이름", 가격="가격 (포인트)", 종류="판매 방식", 설명="상품 설명",
    역할="종류가 '역할 지급'일 때 줄 역할",
)
@app_commands.choices(종류=[
    app_commands.Choice(name="자동 전송 (재고를 DM으로 보냄)", value="stock"),
    app_commands.Choice(name="역할 지급", value="role"),
    app_commands.Choice(name="관리자 처리 (서비스 신청형)", value="manual"),
])
@admin_only()
async def prod_add(interaction: discord.Interaction, 이름: app_commands.Range[str, 1, 80],
                   가격: app_commands.Range[int, 0, 100_000_000], 종류: app_commands.Choice[str],
                   설명: app_commands.Range[str, 0, 500] = "", 역할: discord.Role | None = None):
    if 종류.value == "role" and 역할 is None:
        return await error(interaction, "역할 지급 상품은 `역할`을 꼭 정해 주세요.")
    pid = db.add_product(interaction.guild_id, 이름, 가격, 설명, 종류.value, 역할.id if 역할 else None)
    hint = f"\n`/재고 추가 상품:{pid}`로 재고를 넣어 주세요." if 종류.value == "stock" else ""
    await reply(interaction, embed("✅ 상품 등록", f"#{pid} **{이름}** — {가격:,} P ({종류.name}){hint}", COLOR_OK))


@product_group.command(name="수정", description="상품 정보를 바꿔요")
@app_commands.autocomplete(상품=product_autocomplete)
@admin_only()
async def prod_edit(interaction: discord.Interaction, 상품: int, 이름: str | None = None,
                    가격: app_commands.Range[int, 0, 100_000_000] | None = None,
                    설명: str | None = None, 판매중: bool | None = None):
    ok = db.update_product(interaction.guild_id, 상품, name=이름, price=가격, description=설명,
                           active=None if 판매중 is None else int(판매중))
    if not ok:
        return await error(interaction, "바꿀 내용이 없거나 존재하지 않는 상품이에요.")
    await reply(interaction, embed("✅ 상품 수정 완료", f"상품 #{상품}", COLOR_OK))


@product_group.command(name="삭제", description="상품 판매를 중지해요 (구매 기록은 남아요)")
@app_commands.autocomplete(상품=product_autocomplete)
@admin_only()
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
    lines = [
        f"`#{p.id}` **{p.name}** — {p.price:,} P · {KIND_LABEL[p.kind]}"
        + (f" · 재고 {p.stock}" if p.kind == "stock" else "")
        + ("" if p.active else " · ~~판매 중지~~")
        for p in products
    ]
    await reply(interaction, embed("📦 상품 목록", "\n".join(lines)[:4000]))


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
@admin_only()
async def stock_add(interaction: discord.Interaction, 상품: int):
    product = db.get_product(interaction.guild_id, 상품)
    if product is None or product.kind != "stock":
        return await error(interaction, "자동 전송 상품만 재고를 넣을 수 있어요.")
    await interaction.response.send_modal(StockModal(상품))


@stock_group.command(name="비우기", description="상품의 남은 재고를 모두 지워요")
@app_commands.autocomplete(상품=product_autocomplete)
@admin_only()
async def stock_clear(interaction: discord.Interaction, 상품: int):
    try:
        n = db.clear_stock(interaction.guild_id, 상품)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed("✅ 재고 비우기", f"상품 #{상품}의 재고 {n}개를 지웠어요.", COLOR_OK))


@order_group.command(name="환불", description="주문을 환불하고 포인트를 돌려줘요")
@admin_only()
async def order_refund(interaction: discord.Interaction, 주문번호: int):
    try:
        order = db.refund_order(interaction.guild_id, 주문번호)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed("✅ 환불 완료", f"주문 #{주문번호}: <@{order['user_id']}>에게 {order['price']:,} P 반환", COLOR_OK))
    await dm(order["user_id"], embed(
        "↩️ 주문 환불", f"주문 #{주문번호} **{order['product_name']}**이(가) 환불되어 {order['price']:,} P를 돌려드렸어요.",
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
@admin_only()
async def dep_link(interaction: discord.Interaction, 입금번호: int, 유저: discord.Member):
    dep = next((d for d in db.unmatched_deposits(interaction.guild_id, 1000) if d["id"] == 입금번호), None)
    points = points_for(interaction.guild_id, dep["amount"]) if dep else 0
    try:
        dep, balance = db.link_deposit(interaction.guild_id, 입금번호, 유저.id, points, interaction.user.id)
    except ShopError as exc:
        return await error(interaction, str(exc))
    await reply(interaction, embed(
        "✅ 충전 완료", f"입금 #{입금번호} ({dep['depositor']}, {dep['amount']:,}원) → {유저.mention}에게 {points:,} P", COLOR_OK))
    await dm(유저.id, embed(
        "✅ 충전 완료", f"**{interaction.guild.name}**에서 **{points:,} P**가 충전됐어요!\n현재 잔액: **{balance:,} P**", COLOR_OK))
    await send_log(interaction.guild, embed(
        "🔗 입금 수동 연결", f"{interaction.user.mention}: 입금 #{입금번호} {dep['amount']:,}원 → {유저.mention} ({points:,} P)", COLOR_OK))


@deposit_group.command(name="테스트", description="입금 알림 문자가 제대로 읽히는지 확인해요 (실제 충전 안 됨)")
@admin_only()
async def dep_test(interaction: discord.Interaction, 알림문자: str):
    parsed = bank.parse_deposit(알림문자)
    if parsed is None:
        return await error(interaction, "입금으로 읽지 못했어요. 출금/광고 문자이거나 형식이 달라요 (DEPOSIT_REGEX 설정 필요).")
    await reply(interaction, embed("✅ 읽기 성공", f"입금자명: **{parsed.name}**\n금액: **{parsed.amount:,}원**", COLOR_OK))


def vault_embed(v: dict[str, int], title: str = "🏦 금고") -> discord.Embed:
    e = embed(title, f"## 출금 가능: {v['available']:,}원", COLOR_INFO)
    e.add_field(name="총 충전 금액", value=f"{v['received']:,}원 ({v['charges']}건)")
    e.add_field(name="출금한 금액", value=f"{v['withdrawn']:,}원")
    if v["unmatched_count"]:
        e.add_field(
            name="미확인 입금", value=f"{v['unmatched']:,}원 ({v['unmatched_count']}건)\n"
            "아직 포인트가 지급되지 않아 출금 가능 금액에서 빠져 있어요.", inline=False)
    e.set_footer(text="실제 돈은 입금 계좌에 있어요. 계좌에서 돈을 뺀 뒤 /금고 출금으로 기록하세요.")
    return e


@vault_group.command(name="잔액", description="충전으로 들어온 돈과 출금 가능 금액을 확인해요")
@admin_only()
async def vault_balance(interaction: discord.Interaction):
    await reply(interaction, vault_embed(db.vault(interaction.guild_id)))


@vault_group.command(name="출금", description="금고에서 돈을 꺼낸 것을 기록해요")
@app_commands.describe(금액="꺼낸 금액 (원)", 메모="예: 10월 정산, 서버 운영비")
@admin_only()
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
