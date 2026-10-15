# -*- coding: utf-8 -*-
"""
週末チャレンジ戦・栄誉戦 エントリー受付モジュール（赛事报名模块）

功能（按开发需求文档 + 用户确认的调整实现）：
- /报名设置（管理员）：设置报名窗口 开始时间~截止时间（北京时间，可随时改）
- /报名面板（管理员）：发送常驻规则文案 + 永続化ボタン「📝 提出フォームを開く」；改设置后面板自动更新
- 时间锁：仅在设置的窗口内可提交，期外点击按钮仅本人可见提示（显示当前窗口，换算日本时间）
- 单弹窗 5 字段：席番号（数字）/ 軍団名 / 軍団名読み方 / 軍団（数字）ID / 出戦メンバーID（多行，1行1ID、第1行队长）
- 提交后：ephemeral「✅ 提出完了」+ 频道A公屏播报「18番 華の秘密結社 提出済」
- 重复提交=覆盖自己的旧报名；席番号与他人冲突=提示冲突对象但不拒绝
- 到截止时间自动结算：按席番号升序，频道B发简版（席 名 読み）、频道C发完整版（含成员ID），成功后清空表（一次性，非循环）
- 数据持久化 SQLite（data/event_reg.db），重启不丢；SettleLog 幂等防重发；宕机后重启自动补结算

频道配置走 .env：CHANNEL_A_ID / CHANNEL_B_ID / CHANNEL_C_ID
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "event_reg.db")

# 内部统一按北京时间（UTC+8）判定；玩家侧展示换算日本时间（UTC+9）
TZ = timezone(timedelta(hours=8))
TZ_JST = timezone(timedelta(hours=9))

# 频道配置（.env 读取）：A=公开播报频道，B=版主制图频道（简版），C=运营存档频道（完整版）
CHANNEL_A_ID = int(os.getenv("CHANNEL_A_ID", "0") or 0)
CHANNEL_B_ID = int(os.getenv("CHANNEL_B_ID", "0") or 0)
CHANNEL_C_ID = int(os.getenv("CHANNEL_C_ID", "0") or 0)

engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# ==================== 文案（需求文档原文为底，期限部分随设置动态生成） ====================

# 常驻入口消息模板（3.1 节；仅 ⏰ 受付期間 段落动态化，其余保持文档原文）
_ENTRY_HEAD = """# 🏆 【週末チャレンジ戦・栄誉戦】参戦エントリー

各軍団のリーダーの皆様、いつもコミュニティを盛り上げていただきありがとうございます！
今週の「チャレンジ戦・栄誉戦」のエントリー受付を開始いたします。参戦をご希望の軍団は、本メッセージ下部のボタンよりメンバー情報の提出をお願いいたします。

━━━━━━━━━━━━━━━━━━━━
📜 【大会概要・基本ルール】

🔹 大会形式：5 vs 5 軍団対抗戦
🔹 試合日程：毎週日曜日開催
🔹 参加条件：軍団に所属し、5名の出場メンバーが揃っていること。
🔹 禁止事項：不正利用、または対戦相手への煽り行為は厳禁です。
（※その他の詳細なルールについては、必ず大会ルールチャンネルをご確認ください。）

━━━━━━━━━━━━━━━━━━━━
⏰ 【提出期限について】
"""

_ENTRY_TAIL = """
━━━━━━━━━━━━━━━━━━━━
📝 【提出方法】

1️⃣ 下の『📝 提出フォームを開く』ボタンをタップしてください。
2️⃣ 表示される入力画面（ポップアップ）に以下の情報を入力してください。
・席番号
・軍団名
・軍団名読み方（カナ表記）
・軍団（数字）ID
・出戦メンバーID（5名様分 / 1行ずつ改行して入力）
3️⃣ 「送信」を押せば完了です！提出完了後、このチャンネルに自動で提出済みのメッセージが表示されます。

⚠️ 【重要】参加同意事項
本フォームより提出を完了した時点で、大会の全てのルールに同意し、「チャレンジ戦・栄誉戦」への参戦を承認・確約したものとみなされます。

👇 準備が整いましたら、以下のボタンから提出をお願いします！"""


# ==================== 数据库 ====================

class Base(DeclarativeBase):
    pass


class RegConfig(Base):
    """报名窗口配置（key/value）：open_time / close_time，格式 'YYYY-MM-DD HH:MM'（CST）"""
    __tablename__ = "reg_config"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64), default="")


class Registration(Base):
    """本期报名记录（到截止时间结算推送成功后清空）"""
    __tablename__ = "registrations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32), unique=True)  # 同一玩家只保留最新一条（重复提交=覆盖）
    seat_number: Mapped[int] = mapped_column(Integer)              # 席番号（升序排序用）
    clan_name: Mapped[str] = mapped_column(String(100))
    clan_kana: Mapped[str] = mapped_column(String(100))
    clan_id: Mapped[str] = mapped_column(String(50))
    members: Mapped[str] = mapped_column(Text)                     # 原始多行文本（1行1个ID，第1行=队长）
    timestamp: Mapped[str] = mapped_column(String(19))             # "YYYY-MM-DD HH:MM:SS"（CST）


class SettleLog(Base):
    """结算幂等标记（key=截止时间）：B/C 两个频道都发送成功后才落记录，进程重启不重发"""
    __tablename__ = "settle_log"
    settle_date: Mapped[str] = mapped_column(String(20), primary_key=True)  # "YYYY-MM-DD HH:MM"（CST）


class PanelMessage(Base):
    """常驻入口消息记录（同频道重发面板时删除旧消息；改设置后自动更新内容）"""
    __tablename__ = "reg_panel_messages"
    channel_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(32))


async def init_db():
    async with engine.begin() as conn:
        def _migrate(sync_conn):
            from sqlalchemy import inspect as sa_inspect
            insp = sa_inspect(sync_conn)
            if insp.has_table("registrations"):
                cols = {c["name"] for c in insp.get_columns("registrations")}
                if "seat_number" not in cols:
                    # v1 旧表结构（每日窗口模型）不兼容 → 废弃重建
                    sync_conn.exec_driver_sql("DROP TABLE registrations")
        await conn.run_sync(_migrate)
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")


# ==================== 配置与窗口 ====================

async def _set_config(key: str, value: str):
    async with SessionLocal() as s:
        async with s.begin():
            row = await s.get(RegConfig, key)
            if row:
                row.value = value
            else:
                s.add(RegConfig(key=key, value=value))


async def _get_window():
    """返回 (open_dt, close_dt)（均带 CST 时区）；未配置返回 (None, None)"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(RegConfig))).scalars().all()
    cfg = {r.key: r.value for r in rows}
    o, c = cfg.get("open_time"), cfg.get("close_time")
    if not o or not c:
        return None, None
    try:
        open_dt = datetime.strptime(o, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        close_dt = datetime.strptime(c, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return None, None
    return open_dt, close_dt


def _parse_dt(s: str):
    """解析 'YYYY-MM-DD HH:MM'（兼容 / 与全角空格、秒），失败返回 None"""
    s = s.strip().replace("/", "-").replace("　", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    return None


def _now() -> datetime:
    return datetime.now(TZ)


def _is_open(now: datetime, open_dt, close_dt) -> bool:
    return bool(open_dt and close_dt) and open_dt <= now < close_dt


_JP_WEEKDAYS = "月火水木金土日"


def _fmt_jp(dt: datetime) -> str:
    """CST 时间 → 日本时间显示：10月18日（日）19:00"""
    j = dt.astimezone(TZ_JST)
    return f"{j.month}月{j.day}日（{_JP_WEEKDAYS[j.weekday()]}）{j.hour:02d}:{j.minute:02d}"


def _build_entry_message(open_dt, close_dt) -> str:
    """常驻入口消息：文档原文 + 动态受付期間段落"""
    if open_dt and close_dt:
        period = (
            f"\n受付期間：**{_fmt_jp(open_dt)} ～ {_fmt_jp(close_dt)}** (日本時間)\n"
            "※ 締切後、システムが自動で集計を行い、リストを確定します。締切後の提出・変更は原則受け付けませんのでご注意ください。\n"
        )
    else:
        period = "\n受付期間は現在調整中です。開始まで今しばらくお待ちください。\n"
    return _ENTRY_HEAD + period + _ENTRY_TAIL


def _build_closed_message(open_dt, close_dt) -> str:
    """时间锁期外提示（3.2 节为底，期间部分动态化）"""
    if open_dt and close_dt:
        return (
            "⛔ 現在はエントリー期間外です。\n\n"
            f"【受付期間】\n{_fmt_jp(open_dt)} ～ {_fmt_jp(close_dt)} (日本時間)\n\n"
            "受付期間内に再度ボタンを押して提出をお願いいたします。"
        )
    return (
        "⛔ 現在はエントリー期間外です。\n\n"
        "（受付期間はまだ設定されていません。設定まで今しばらくお待ちください。）"
    )


# ==================== 工具函数 ====================

def _chunk_text(lines, limit=1900, sep="\n"):
    """把若干行/块拼成不超过 limit 的消息分片（Discord 单条 2000 字上限）"""
    chunks, cur = [], ""
    for line in lines:
        if not cur:
            cur = line
        elif len(cur) + len(sep) + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += sep + line
    if cur:
        chunks.append(cur)
    return chunks


def _build_simple_lines(rows):
    """频道 B 简版：席番号 軍団名 読み方（空格间隔，版主直接复制做图）"""
    return [f"{r.seat_number} {r.clan_name} {r.clan_kana}" for r in rows]


def _build_full_blocks(rows):
    """频道 C 完整版：每个军团一个文本区块，含全部成员 ID"""
    return [
        f"**[{r.seat_number}] {r.clan_name}** ({r.clan_kana})\n"
        f"軍団ID: {r.clan_id}\n"
        f"**【出戦メンバー】**\n{r.members}"
        for r in rows
    ]


async def _fetch_channel(bot, channel_id: int):
    if not channel_id:
        return None
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            print(f"⚠️ チャンネル {channel_id} の取得に失敗: {e}")
            return None
    return ch


async def _send_with_retry(channel, content: str, max_retry: int = 3) -> bool:
    """带重试的发送（429/网络波动时按 retry_after 或指数退避等待）"""
    for attempt in range(max_retry):
        try:
            await channel.send(content)
            return True
        except discord.HTTPException as e:
            wait = getattr(e, "retry_after", None) or (2 ** attempt)
            print(f"⚠️ 送信失敗（{attempt + 1}/{max_retry}）: {e} → {wait}s 後に再試行")
            await asyncio.sleep(min(wait, 30))
        except Exception as e:
            print(f"⚠️ 送信失敗（{attempt + 1}/{max_retry}）: {e}")
            await asyncio.sleep(2 ** attempt)
    return False


async def _public_broadcast(bot, fallback_channel, seat: int, clan_name: str):
    """频道 A 公屏播报：[席番号]番 [軍団名] 提出済（频道 A 不可用时回退到提交所在频道）"""
    text = f"{seat}番 {clan_name} 提出済"
    ch = await _fetch_channel(bot, CHANNEL_A_ID) if CHANNEL_A_ID else None
    if ch is None:
        ch = fallback_channel
    if ch is None:
        print("⚠️ 公開メッセージの送信先チャンネルが見つかりません")
        return
    try:
        await ch.send(text)
    except Exception as e:
        print(f"⚠️ 公開メッセージ送信失敗: {e}")


async def _refresh_panels(bot):
    """设置变更后，自动更新所有已记录的常驻入口消息内容"""
    open_dt, close_dt = await _get_window()
    text = _build_entry_message(open_dt, close_dt)
    async with SessionLocal() as s:
        panels = (await s.execute(select(PanelMessage))).scalars().all()
    for p in panels:
        try:
            ch = await _fetch_channel(bot, int(p.channel_id))
            if ch is None:
                continue
            msg = await ch.fetch_message(int(p.message_id))
            await msg.edit(content=text)
        except discord.NotFound:
            # 面板消息已被手动删除：清理数据库中的失效记录
            async with SessionLocal() as s:
                async with s.begin():
                    row = await s.get(PanelMessage, p.channel_id)
                    if row:
                        await s.delete(row)
            print(f"ℹ️ パネルメッセージが削除済みのため記録をクリーンアップ (channel={p.channel_id})")
        except Exception as e:
            print(f"⚠️ パネル自動更新に失敗 (channel={p.channel_id}): {e}")


# ==================== 弹窗表单（单弹窗 5 字段） ====================

class EntryModal(discord.ui.Modal, title="🏆 チャレンジ戦・栄誉戦メンバー提出"):
    seat = discord.ui.TextInput(
        label="席番号", placeholder="例：18", max_length=5, required=True)
    clan_name = discord.ui.TextInput(
        label="軍団名", placeholder="例：華の秘密結社", max_length=100, required=True)
    clan_kana = discord.ui.TextInput(
        label="軍団名読み方（カナ表記）", placeholder="例：ハナノヒミツケッシャ", max_length=100, required=True)
    clan_id = discord.ui.TextInput(
        label="軍団（数字）ID", placeholder="例：123456789", max_length=50, required=True)
    members = discord.ui.TextInput(
        label="出戦メンバーID（5名様まで）",
        style=discord.TextStyle.paragraph,
        placeholder="1行に1つのIDを入力（1行目はリーダー）",
        max_length=500, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        # 时间锁复检（打开弹窗后拖到截止才提交的情况）
        open_dt, close_dt = await _get_window()
        if not _is_open(_now(), open_dt, close_dt):
            await interaction.response.send_message(
                _build_closed_message(open_dt, close_dt), ephemeral=True)
            return

        # 席番号防呆校验（纯数字）
        seat_text = self.seat.value.strip()
        if not seat_text.isdigit():
            await interaction.response.send_message(
                "❌ 席番号は数字のみ入力してください。もう一度お試しください。", ephemeral=True)
            return
        seat = int(seat_text)

        uid = str(interaction.user.id)
        members_text = self.members.value.strip()
        conflict_names = []
        try:
            async with SessionLocal() as s:
                async with s.begin():
                    # 席番号冲突：只查询提示，不拒绝（用户拍板的规则）
                    rows = (await s.execute(
                        select(Registration).where(
                            Registration.seat_number == seat,
                            Registration.user_id != uid))
                    ).scalars().all()
                    conflict_names = [r.clan_name for r in rows]
                    # 重复提交 = 覆盖自己的旧报名（删旧插新同一事务）
                    await s.execute(delete(Registration).where(Registration.user_id == uid))
                    s.add(Registration(
                        user_id=uid,
                        seat_number=seat,
                        clan_name=self.clan_name.value.strip(),
                        clan_kana=self.clan_kana.value.strip(),
                        clan_id=self.clan_id.value.strip(),
                        members=members_text,
                        timestamp=_now().strftime("%Y-%m-%d %H:%M:%S"),
                    ))
        except IntegrityError as e:
            print(f"⚠️ 応募データ書き込み競合: {e}")
            await interaction.response.send_message(
                "⚠️ データの保存に失敗しました。もう一度お試しください。", ephemeral=True)
            return

        # 1) 向玩家反馈（仅本人可见）
        reply = "✅ 提出完了"
        if conflict_names:
            names = "、".join(f"「{n}」" for n in conflict_names)
            reply += (f"\n\n⚠️ 席番号 **{seat}** は {names} も提出しています。"
                      "提出は受け付けましたが、重複の可能性があるため運営にご確認ください。")
        await interaction.response.send_message(reply, ephemeral=True)

        # 2) 频道 A 公屏播报（不阻塞玩家反馈，失败只记日志）
        await _public_broadcast(
            interaction.client, interaction.channel, seat, self.clan_name.value.strip())


# ==================== 常驻入口（按钮） ====================

class EntryPanelView(discord.ui.View):
    """常驻入口消息的持久化按钮（custom_id 上线后勿改）"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📝 提出フォームを開く",
        style=discord.ButtonStyle.primary,
        custom_id="event_reg_open_btn")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 时间锁拦截（3.2 节：不可直接弹窗，先判时间）
        open_dt, close_dt = await _get_window()
        if not _is_open(_now(), open_dt, close_dt):
            await interaction.response.send_message(
                _build_closed_message(open_dt, close_dt), ephemeral=True)
            return
        await interaction.response.send_modal(EntryModal())


# ==================== 结算 ====================

async def _run_settlement(bot, now: datetime) -> bool:
    """到截止时间自动结算：B 简版 + C 完整版，双频道成功后清空并落幂等标记。

    返回 True = 本轮完成了一次结算。
    宕机补结算：重启时已过了截止时间且未结算过 → 只结算 timestamp <= 截止时间的记录，
    晚于截止的新一期记录保留不动。
    """
    open_dt, close_dt = await _get_window()
    if not close_dt or now < close_dt:
        return False
    settle_key = close_dt.strftime("%Y-%m-%d %H:%M")
    async with SessionLocal() as s:
        already = (await s.execute(
            select(SettleLog).where(SettleLog.settle_date == settle_key))
        ).scalar_one_or_none()
    if already:
        return False

    close_str = close_dt.strftime("%Y-%m-%d %H:%M:%S")
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Registration)
            .where(Registration.timestamp <= close_str)
            .order_by(Registration.seat_number.asc()))
        ).scalars().all()

    same_day = now.date() == close_dt.date()
    ch_b = await _fetch_channel(bot, CHANNEL_B_ID)
    ch_c = await _fetch_channel(bot, CHANNEL_C_ID)
    if ch_b is None or ch_c is None:
        print("⚠️ 集計：チャンネル B/C を取得できません。次の周期で再試行します。")
        return False

    b_ok = c_ok = True
    if not rows:
        # 无报名：只在截止当天发通知，补结算日不再打扰
        if same_day:
            b_ok = await _send_with_retry(ch_b, "今回のエントリーはありません。")
            c_ok = await _send_with_retry(ch_c, "今回のエントリーはありません。")
    else:
        for chunk in _chunk_text(_build_simple_lines(rows)):
            b_ok = (await _send_with_retry(ch_b, chunk)) and b_ok
        for chunk in _chunk_text(_build_full_blocks(rows), sep="\n\n"):
            c_ok = (await _send_with_retry(ch_c, chunk)) and c_ok

    if b_ok and c_ok:
        async with SessionLocal() as s:
            async with s.begin():
                s.add(SettleLog(settle_date=settle_key))
                await s.execute(delete(Registration).where(Registration.timestamp <= close_str))
        print(f"✅ 集計完了（締切 {settle_key}）: {len(rows)} 件を送信し、テーブルをクリアしました")
        return True
    print("⚠️ 集計：一部の送信に失敗しました。次の周期で再試行します（重複送信の可能性あり）。")
    return False


# ==================== Cog ====================

class RegCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.settle_loop.is_running():
            self.settle_loop.start()

    @tasks.loop(seconds=30)
    async def settle_loop(self):
        try:
            await _run_settlement(self.bot, _now())
        except Exception as e:
            print(f"⚠️ 集計ループでエラー: {e}")

    # ---------- 管理命令（中文命令名 + 日文描述，仅管理员可见可用） ----------

    @app_commands.command(name="报名设置", description="エントリー受付期間を設定します（例: 2026-10-16 10:00）")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        open_time="受付開始日時（YYYY-MM-DD HH:MM・北京時間）",
        close_time="締め切り日時（YYYY-MM-DD HH:MM・北京時間）")
    async def reg_config_cmd(self, interaction: discord.Interaction,
                             open_time: str = None, close_time: str = None):
        """设置报名窗口（开始~截止，北京时间）。两个参数都省略时查看当前设置。"""
        # 查看当前设置
        if open_time is None and close_time is None:
            o, c = await _get_window()
            if o and c:
                text = (f"📋 現在の設定\n\n受付開始: **{_fmt_jp(o)}**\n締め切り: **{_fmt_jp(c)}**"
                        "\n（日本時間表示）\n\n変更する場合は開始・締め切りの両方を入力してください。")
            else:
                text = ("📋 受付期間はまだ設定されていません。\n\n"
                        "開始・締め切りの両方を入力してください（例: 2026-10-16 10:00）。")
            await interaction.response.send_message(text, ephemeral=True)
            return

        if open_time is None or close_time is None:
            await interaction.response.send_message(
                "⚠️ 受付開始と締め切りは両方入力してください。", ephemeral=True)
            return

        o, c = _parse_dt(open_time), _parse_dt(close_time)
        if not o or not c:
            await interaction.response.send_message(
                "⚠️ 日時の形式が正しくありません。例: `2026-10-16 10:00`", ephemeral=True)
            return
        if o >= c:
            await interaction.response.send_message(
                "⚠️ 受付開始は締め切りより前の日時に設定してください。", ephemeral=True)
            return

        await _set_config("open_time", o.strftime("%Y-%m-%d %H:%M"))
        await _set_config("close_time", c.strftime("%Y-%m-%d %H:%M"))
        await _refresh_panels(interaction.client)

        reply = (f"✅ 受付期間を設定しました。\n\n受付開始: **{_fmt_jp(o)}**\n締め切り: **{_fmt_jp(c)}**"
                 "\n（日本時間表示。パネルメッセージは自動更新されます）")
        if _now() >= c:
            reply += "\n\n⚠️ 締め切りが過去の日時のため、まもなく集計が実行されます。"
        await interaction.response.send_message(reply, ephemeral=True)

    @app_commands.command(name="报名面板", description="週末チャレンジ戦のエントリー受付メッセージを送信します")
    @app_commands.default_permissions(administrator=True)
    async def reg_panel(self, interaction: discord.Interaction):
        """发送常驻入口消息（规则文案 + 提交按钮）。同频道重发时自动删除旧面板。"""
        channel = interaction.channel
        open_dt, close_dt = await _get_window()
        view = EntryPanelView()
        msg = await channel.send(_build_entry_message(open_dt, close_dt), view=view)

        async with SessionLocal() as s:
            async with s.begin():
                old = (await s.execute(
                    select(PanelMessage).where(PanelMessage.channel_id == str(channel.id)))
                ).scalar_one_or_none()
                if old:
                    if old.message_id != str(msg.id):
                        try:
                            old_msg = await channel.fetch_message(int(old.message_id))
                            await old_msg.delete()
                        except Exception:
                            pass
                    # 同频道重发：就地更新 message_id，避免 delete+insert 同 flush 冲突
                    old.message_id = str(msg.id)
                else:
                    s.add(PanelMessage(channel_id=str(channel.id), message_id=str(msg.id)))

        await interaction.response.send_message(
            "✅ エントリー受付メッセージを送信しました。", ephemeral=True)


async def setup_event_reg(bot):
    """报名系统装载入口：建库 → 挂 Cog → 注册持久化 View。必须在 tree.sync() 之前调用。"""
    await init_db()
    await bot.add_cog(RegCog(bot))
    bot.add_view(EntryPanelView())
    missing = [name for name, cid in
               [("CHANNEL_A_ID", CHANNEL_A_ID), ("CHANNEL_B_ID", CHANNEL_B_ID), ("CHANNEL_C_ID", CHANNEL_C_ID)]
               if not cid]
    if missing:
        print(f"⚠️ .env に {', '.join(missing)} が未設定です。関連機能が動作しません。")
    print("   週末チャレンジ戦エントリーシステムを読み込みました (data/event_reg.db)")
