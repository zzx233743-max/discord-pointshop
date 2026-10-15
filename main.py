# -*- coding: utf-8 -*-
"""
週末チャレンジ戦・栄誉戦 エントリー受付 Bot（赛事报名专用 bot）
- 管理コマンド「/报名设置」で受付期間（開始・締め切り、北京时间）を設定
- 管理コマンド「/报名面板」で常驻エントリー受付メッセージ（规则文案 + 永続化ボタン）を送信
- プレイヤーはボタンから Discord 標準フォーム（单弹窗 5 字段）でメンバー情報を提出
- 締め切り到達で自動集計：簡版をチャンネル B、完整版をチャンネル C へ送信後クリア（一次性）
- 频道 ID は .env の CHANNEL_A_ID / CHANNEL_B_ID / CHANNEL_C_ID で設定
- 詳細な機能は event_reg.py を参照
"""

import discord
from discord.ext import commands
import os
import sys
import atexit
import logging
import traceback
from dotenv import load_dotenv
load_dotenv()

# 军团大会报名系统（独立模块）
try:
    from event_reg import setup_event_reg
    _EVENT_REG_AVAILABLE = True
except Exception as _reg_err:
    print(f"⚠️ event_reg のロードに失敗しました: {_reg_err}")
    _EVENT_REG_AVAILABLE = False

# ============== 多重起動防止（プロセスロック） ==============
# ロックファイルは DISCORD_TOKEN のハッシュをファイル名に含める：
#   同じ token 同士 → 全機で相互排他（誤っての二重起動を防止）
#   異なる token 同士 → 互いに干渉しない（同一 PC で複数 bot を並行稼働できる）
import hashlib as _hashlib
_TOKEN_TAG = _hashlib.md5(os.getenv("DISCORD_TOKEN", "").encode()).hexdigest()[:8]
LOCK_FILE = os.path.join(os.path.expanduser("~"), f'.bot_{_TOKEN_TAG}.lock')

def _pid_alive(pid: int) -> bool:
    """跨平台进程存活检查（Windows 上 os.kill(pid, 0) 会误杀目标进程，不可用）"""
    if os.name == 'nt':
        import ctypes
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def acquire_lock():
    """他のBotインスタンスが起動していないか確認"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = f.read().strip()
            # プロセスが生存しているか確認
            if old_pid and _pid_alive(int(old_pid)):
                # プロセスが生きている = 既に起動中
                print(f"❌ 既にBotが起動しています (PID: {old_pid})。多重起動を防止しました。")
                sys.exit(1)
            # 古いプロセスは既に終了している → ロックファイルを上書き
        except (ValueError, IOError):
            pass
    # ロックファイルに現在のPIDを書き込む
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    atexit.register(release_lock)

def release_lock():
    """終了時にロックファイルを削除"""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except IOError:
        pass

acquire_lock()

# Windows: fix encoding for console (GBK) and handle pythonw.exe (stdout=None)
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.log')
if sys.stdout is None:
    # Running under pythonw.exe - redirect to log file
    sys.stdout = open(_log_path, 'a', encoding='utf-8', buffering=1)
    sys.stderr = sys.stdout
elif sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ============== Bot 本体 ==============
# 纯交互型 bot（斜杠命令 + 按钮 + Modal），不需要任何特权 Intent
intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

_EVENT_REG_LOADED = False  # on_ready 在重连时会再次触发，用标记保证只加载一次


@bot.event
async def on_ready():
    global _EVENT_REG_LOADED
    print(f"✅ {bot.user} がオンラインになりました")
    print(f"   {len(bot.guilds)} サーバーで稼働中")

    # 军团大会报名系统読み込み（初回のみ）
    # ※コマンド同期より前に Cog を登録しないと、报名命令が Discord に同期されない
    if _EVENT_REG_AVAILABLE and not _EVENT_REG_LOADED:
        try:
            await setup_event_reg(bot)
            _EVENT_REG_LOADED = True
        except Exception as e:
            print(f"   ⚠️ 軍団大会応募システムの読み込みに失敗: {e}")
            traceback.print_exc()

    try:
        synced = await bot.tree.sync()
        print(f"   スラッシュコマンド {len(synced)} 件を同期しました")
    except Exception as e:
        print(f"   コマンド同期失敗: {e}")

    # 永続化 View の登録は setup_event_reg 内で実施済み（bot 再起動後もパネルのボタンが反応する）


@bot.event
async def on_disconnect():
    print("⚠️ Discord から切断されました。再接続を試みます…")


@bot.event
async def on_resumed():
    print("✅ Discord に再接続しました")


@bot.event
async def on_error(event_method, *args, **kwargs):
    print(f"❌ エラー発生 ({event_method}):")
    traceback.print_exc()


# ============== 起動 ==============
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("❌ 環境変数 DISCORD_TOKEN を設定してください")
        print("   方法1: .env ファイルに DISCORD_TOKEN=あなたのトークン")
        print("   方法2: export DISCORD_TOKEN=あなたのトークン")
        exit(1)

    # クラッシュしても自動再起動
    while True:
        try:
            bot.run(TOKEN)
        except KeyboardInterrupt:
            print("bot を終了します")
            break
        except Exception as e:
            print(f"❌ クラッシュ発生: {e}")
            traceback.print_exc()
            print("10秒後に再起動します…")
            import time
            time.sleep(10)
