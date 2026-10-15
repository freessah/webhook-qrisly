import re
import asyncio
import aiosqlite
import aiohttp
import json
import os
import uuid
import qrcode
from io import BytesIO
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TimedOut, NetworkError, RetryAfter
from database import DB_NAME, get_user

# Waktu Indonesia Barat (WIB)
WIB = timezone(timedelta(hours=7))

# Channel Leaderboard
LB_CHANNEL_ID = "-1004373322957"

CONFIG_FILE = "config.json"
OWNER_ID = 7277769315  # Ganti dengan User ID Telegram milik Owner/Admin Utama

# Konfigurasi QRISly Payment Gateway
QRISLY_API_KEY = os.getenv("QRISLY_API_KEY", "gmlbZfjn9ee8a44e79bbb19f2VIUXjJH").strip()
QRISLY_QRIS_ID = os.getenv("QRISLY_QRIS_ID", "217").strip()
QRISLY_BASE_URL = os.getenv("QRISLY_BASE_URL", "https://api.collaborator.komerce.id/user").strip().rstrip("/")
QRISLY_WEBHOOK_URL = os.getenv("QRISLY_WEBHOOK_URL", "https://api.collaborator.komerce.id/user/api/v1/webhook/payment-notification").strip()

def load_config():
    """Membaca config dari file config.json, default fee_persen=7.0, cashback_percent=2.0"""
    default_config = {"fee_persen": 7.0, "cashback_percent": 2.0}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                return {
                    "fee_persen": data.get("fee_persen", 7.0),
                    "cashback_percent": data.get("cashback_percent", 2.0)
                }
        except Exception as e:
            print(f"Gagal membaca config, menggunakan default: {e}")
            return default_config
    else:
        save_config_data(default_config)
        return default_config

def save_config_data(data_dict: dict):
    """Menyimpan dictionary config ke file config.json"""
    try:
        data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        
        data.update(data_dict)
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Gagal menyimpan config: {e}")

def save_config(fee_val: float):
    """Helper untuk update fee_persen saja"""
    cfg = load_config()
    cfg["fee_persen"] = fee_val
    save_config_data(cfg)

# Inisialisasi variabel global fee_persen dari config.json
cfg_loaded = load_config()
fee_persen = cfg_loaded["fee_persen"]

roll_lock = asyncio.Lock()
title_lock = asyncio.Lock()

active_roll = {
    "user_id": None,
    "user_name": None,
    "message_id": None,
    "chat_id": None,
    "created_at": None,
    "side": None,
    "needed_amount": 0,
    "task": None
}

# Temporary pending state untuk konfirmasi Geseran, Angpao, Deposit, dll.
PENDING_CONFIRMATIONS = {}

async def check_private_chat(update: Update) -> bool:
    """Memeriksa apakah command dijalankan melalui chat pribadi (Private Chat)."""
    if update.effective_chat and update.effective_chat.type != 'private':
        await update.message.reply_text("❌ Fitur ini hanya dapat digunakan melalui chat pribadi (Private Chat) dengan bot!")
        return False
    return True

async def ensure_db_tables_and_columns():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("PRAGMA table_info(users)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "pindah_used" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN pindah_used INTEGER DEFAULT 0")
            if "ws_streak" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN ws_streak INTEGER DEFAULT 0")
            if "ws_claimed" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN ws_claimed INTEGER DEFAULT 0")
            if "lb_wins" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN lb_wins INTEGER DEFAULT 0")
            if "cb_last_claimed_date" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN cb_last_claimed_date TEXT")
            if "cb_last_claimed_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN cb_last_claimed_at TEXT")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_picks_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                created_at TEXT,
                side TEXT,
                amount REAL,
                result TEXT
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lb_state (
                id INTEGER PRIMARY KEY,
                message_id INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                title TEXT,
                expires_at TEXT,
                type TEXT,
                UNIQUE(user_id, chat_id)
            )
        """)

        async with db.execute("PRAGMA table_info(user_titles)") as cursor:
            title_columns = [row[1] for row in await cursor.fetchall()]
        if "user_id" not in title_columns:
            await db.execute("ALTER TABLE user_titles ADD COLUMN user_id INTEGER")
        if "chat_id" not in title_columns:
            await db.execute("ALTER TABLE user_titles ADD COLUMN chat_id INTEGER")
        if "title" not in title_columns:
            await db.execute("ALTER TABLE user_titles ADD COLUMN title TEXT")
        if "expires_at" not in title_columns:
            await db.execute("ALTER TABLE user_titles ADD COLUMN expires_at TEXT")
        if "type" not in title_columns:
            await db.execute("ALTER TABLE user_titles ADD COLUMN type TEXT DEFAULT 'req'")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS deposit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                created_at TEXT,
                date_str TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS title_transactions (
                tx_id TEXT PRIMARY KEY,
                buyer_id INTEGER,
                owner_id INTEGER,
                amount REAL,
                title TEXT,
                duration_days INTEGER,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_admins (
                user_id INTEGER,
                chat_id INTEGER,
                granted_at TEXT,
                active INTEGER DEFAULT 1,
                PRIMARY KEY(user_id, chat_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_state (
                key TEXT PRIMARY KEY,
                last_date TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id TEXT PRIMARY KEY,
                user_id INTEGER,
                type TEXT,
                amount_coins REAL,
                amount_idr REAL,
                status TEXT,
                gateway_tx_id TEXT,
                created_at TEXT,
                expires_at TEXT,
                completed_at TEXT
            )
        """)

        await db.commit()

# ==================== VALIDASI & HELPER TITLE ====================
def validate_and_format_title(raw_title: str):
    cleaned = raw_title.strip()
    if not cleaned:
        return None, "Gelar tidak boleh kosong."
    if not re.match(r"^[a-zA-Z0-9 ]+$", cleaned):
        return None, "Gelar hanya boleh mengandung huruf, angka, dan spasi (tanpa karakter khusus)."
    if len(cleaned) > 11:
        return None, "Isi gelar maksimal 11 karakter."
    
    formatted = f"[ {cleaned.upper()} ]"
    if len(formatted) > 15:
        return None, "Panjang total gelar (termasuk kurung dan spasi) maksimal 15 karakter."
    return formatted, None

def parse_duration(duration_str: str):
    if not duration_str:
        return 1, None
    match = re.match(r"^(\d+)([dD]?)$", duration_str.strip().lower())
    if not match:
        return None, "Format durasi salah. Gunakan contoh: 3d, 7d, atau kosongkan untuk 1 hari."
    days = int(match.group(1))
    if days <= 0:
        return None, "Durasi minimal adalah 1 hari."
    if days > 30:
        return None, "Durasi maksimal adalah 30 hari."
    return days, None

def calculate_ws_bonus(streak: int) -> float:
    if streak < 3:
        return 0.0
    if streak > 20:
        streak = 20
    return 1.0 + (streak - 3) * 0.50

def get_roll_confirmation_keyboard(user_id: int):
    keyboard = [
        [
            InlineKeyboardButton("✅ Konfirmasi", callback_data=f"confirm_roll_{user_id}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"cancel_roll_{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def format_picks_list():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT COALESCE(u.nama, 'Pemain'), p.amount 
            FROM player_picks p 
            LEFT JOIN users u ON p.user_id = u.user_id 
            WHERE p.side = 'K'
        """) as cursor:
            picks_k = await cursor.fetchall()
            
        async with db.execute("""
            SELECT COALESCE(u.nama, 'Pemain'), p.amount 
            FROM player_picks p 
            LEFT JOIN users u ON p.user_id = u.user_id 
            WHERE p.side = 'B'
        """) as cursor:
            picks_b = await cursor.fetchall()

    total_k = sum(p[1] for p in picks_k)
    total_b = sum(p[1] for p in picks_b)

    display_total_k = f"{total_k:.2f}"
    display_total_b = f"{total_b:.2f}"

    msg = f"KECIL : BL ( {display_total_k} )\n"
    if picks_k:
        for name, amount in picks_k:
            amt_str = f"{amount:.2f}"
            msg += f"{name} {amt_str}\n"
    else:
        msg += "-\n"

    msg += f"\nBESAR: BL ( {display_total_b} )\n"
    if picks_b:
        for name, amount in picks_b:
            amt_str = f"{amount:.2f}"
            msg += f"{name} {amt_str}\n"
    else:
        msg += "-\n"

    return msg

async def format_last_win_text():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT match_number FROM game_room WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            current_match = row[0] if row else 1

        start_game = max(1, ((current_match - 2) // 20) * 20 + 1)
        end_game = start_game + 19

        async with db.execute("""
            SELECT game_number, side, dice_value, bl_amount 
            FROM game_history 
            WHERE game_number BETWEEN ? AND ? 
            ORDER BY game_number ASC
        """, (start_game, end_game)) as cursor:
            history = await cursor.fetchall()

    msg = "LAST WIN:\n\n"
    if history:
        for g_num, side, dice_val, bl in history:
            side_str = side.upper()
            bl_str = f"{bl:.2f}" if bl is not None else "0.00"
            msg += f"GAME {g_num} : {side_str} {dice_val} {bl_str}\n"
    else:
        msg += "Belum ada riwayat game.\n"
    return msg

# ==================== FITUR ROLL GAME (SISTEM BO3) ====================
async def execute_roll_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    global active_roll
    await ensure_db_tables_and_columns()
    
    async with roll_lock:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT match_number, last_win_msg_id FROM game_room WHERE id = 1") as cursor:
                room_data = await cursor.fetchone()
                current_match = room_data[0] if room_data else 1
                old_last_win_id = room_data[1] if room_data else None

            async with db.execute("SELECT SUM(amount) FROM player_picks WHERE side = 'K'") as cursor:
                row_k = await cursor.fetchone()
                total_k = row_k[0] or 0

            async with db.execute("SELECT SUM(amount) FROM player_picks WHERE side = 'B'") as cursor:
                row_b = await cursor.fetchone()
                total_b = row_b[0] or 0

        if total_k == 0 or total_k != total_b:
            return

        if active_roll["message_id"]:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=active_roll["message_id"],
                    text="🎲 Mengocok dadu Sistem BO3..."
                )
            except Exception:
                pass
        
        active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}

        score_k = 0
        score_b = 0
        dice_values = []

        # MATCH 1 (M1)
        await context.bot.send_message(chat_id=chat_id, text="M1 123")
        await asyncio.sleep(0.5)
        dice_msg1 = await context.bot.send_dice(chat_id=chat_id, emoji='🎲')
        val1 = dice_msg1.dice.value
        await asyncio.sleep(3.5)
        res1_side = 'K' if val1 <= 3 else 'B'
        if res1_side == 'K': score_k += 1
        else: score_b += 1
        dice_values.append(str(val1))

        # MATCH 2 (M2)
        await context.bot.send_message(chat_id=chat_id, text=f"M2 123")
        await asyncio.sleep(0.5)
        dice_msg2 = await context.bot.send_dice(chat_id=chat_id, emoji='🎲')
        val2 = dice_msg2.dice.value
        await asyncio.sleep(3.5)
        res2_side = 'K' if val2 <= 3 else 'B'
        if res2_side == 'K': score_k += 1
        else: score_b += 1
        dice_values.append(str(val2))

        # MATCH 3 (M3) Jika Skor 1-1
        if score_k == 1 and score_b == 1:
            await context.bot.send_message(chat_id=chat_id, text="M3 123")
            await asyncio.sleep(0.5)
            dice_msg3 = await context.bot.send_dice(chat_id=chat_id, emoji='🎲')
            val3 = dice_msg3.dice.value
            await asyncio.sleep(3.5)
            res3_side = 'K' if val3 <= 3 else 'B'
            if res3_side == 'K': score_k += 1
            else: score_b += 1
            dice_values.append(str(val3))

        winning_side_code = 'K' if score_k > score_b else 'B'
        winning_side_name = "KECIL" if winning_side_code == 'K' else "BESAR"
        winning_side_short = "K" if winning_side_code == 'K' else "B"
        bl_total = total_k
        now_wib_str = datetime.now(WIB).strftime("%d/%m %H:%M WIB")

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, side, amount FROM player_picks") as cursor:
                all_picks = await cursor.fetchall()

            cfg = load_config()
            current_fee = cfg.get("fee_persen", 7.0)

            for u_id, side, amt in all_picks:
                side_name_disp = "Kecil" if side == 'K' else "Besar"
                
                await db.execute("UPDATE users SET to_progress = to_progress + ? WHERE user_id = ?", (amt, u_id))

                if side == winning_side_code:
                    profit = amt
                    fee = profit * (current_fee / 100)
                    payout = (amt * 2) - fee
                    
                    await db.execute("UPDATE users SET saldo = saldo + ? WHERE user_id = ?", (payout, u_id))
                    
                    if amt >= 3.0:
                        await db.execute("UPDATE users SET ws_streak = ws_streak + 1, ws_claimed = 0 WHERE user_id = ?", (u_id,))
                    
                    if amt >= 10.0:
                        await db.execute("UPDATE users SET lb_wins = lb_wins + 1 WHERE user_id = ?", (u_id,))

                    await db.execute("""
                        INSERT INTO user_picks_history (user_id, created_at, side, amount, result)
                        VALUES (?, ?, ?, ?, 'W')
                    """, (u_id, now_wib_str, side_name_disp, amt))
                else:
                    if amt >= 3.0:
                        await db.execute("UPDATE users SET ws_streak = 0, ws_claimed = 0 WHERE user_id = ?", (u_id,))
                    
                    await db.execute("""
                        INSERT INTO user_picks_history (user_id, created_at, side, amount, result)
                        VALUES (?, ?, ?, ?, 'L')
                    """, (u_id, now_wib_str, side_name_disp, amt))

            final_score_str = f"{score_k}-{score_b}" if winning_side_code == 'K' else f"{score_b}-{score_k}"
            await db.execute(
                "INSERT OR REPLACE INTO game_history (game_number, side, dice_value, bl_amount) VALUES (?, ?, ?, ?)",
                (current_match, winning_side_short, final_score_str, bl_total)
            )

            await db.execute("DELETE FROM player_picks")
            
            async with db.execute("SELECT user_id, saldo FROM users") as cursor:
                all_users = await cursor.fetchall()
                for u_id, u_saldo in all_users:
                    if u_saldo <= 0:
                        await db.execute("UPDATE users SET to_target = 0, to_progress = 0 WHERE user_id = ?", (u_id,))

            next_match = current_match + 1
            await db.execute("UPDATE users SET pindah_used = 0")
            
            async with db.execute("SELECT COUNT(*) FROM game_room WHERE id = 1") as r_cursor:
                room_exist = await r_cursor.fetchone()
                if room_exist and room_exist[0] > 0:
                    await db.execute("UPDATE game_room SET match_number = ?, players_k = 0, players_b = 0 WHERE id = 1", (next_match,))
                else:
                    await db.execute("INSERT INTO game_room (id, match_number, players_k, players_b) VALUES (1, ?, 0, 0)", (next_match,))

            await db.commit()

        dice_str = " - ".join(dice_values)
        win_score = max(score_k, score_b)
        lose_score = min(score_k, score_b)

        result_msg = (
            f"🎯 Hasil BO3: {winning_side_name.capitalize()} ({winning_side_short}) {win_score}-{lose_score}\n"
            f"🎲 Dadu: {dice_str}\n"
            f"🏆 {winning_side_name.capitalize()} menang {win_score}-{lose_score}!"
        )
        await context.bot.send_message(chat_id=chat_id, text=result_msg)
        await asyncio.sleep(1)

        if old_last_win_id:
            try:
                await context.bot.unpin_chat_message(chat_id=chat_id, message_id=old_last_win_id)
            except Exception:
                pass

        last_win_text = await format_last_win_text()
        new_last_win_msg = await context.bot.send_message(chat_id=chat_id, text=last_win_text)

        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=new_last_win_msg.message_id, disable_notification=True)
        except Exception:
            pass

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE game_room SET last_win_msg_id = ? WHERE id = 1", (new_last_win_msg.message_id,))
            await db.commit()

        picks_list_text = await format_picks_list()
        await context.bot.send_message(chat_id=chat_id, text=picks_list_text)

async def check_and_trigger_auto_roll(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    global active_roll
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT SUM(amount) FROM player_picks WHERE side = 'K'") as cursor:
            row_k = await cursor.fetchone()
            total_k = row_k[0] or 0

        async with db.execute("SELECT SUM(amount) FROM player_picks WHERE side = 'B'") as cursor:
            row_b = await cursor.fetchone()
            total_b = row_b[0] or 0

    if total_k > 0 and total_k == total_b:
        if active_roll["message_id"]:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=active_roll["message_id"],
                    text="🔄 Pasangan berubah ~ konfirmasi /roll dibatalkan."
                )
            except Exception:
                pass
            if active_roll["task"]:
                active_roll["task"].cancel()
            active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}

        asyncio.create_task(execute_roll_game(chat_id, context))
    else:
        if active_roll["message_id"] and active_roll["chat_id"] == chat_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=active_roll["message_id"],
                    text="🔄 Pasangan berubah ~ konfirmasi /roll dibatalkan."
                )
            except Exception:
                pass
            if active_roll["task"]:
                active_roll["task"].cancel()
            active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}


# ==================== FITUR ANGPAO & APGB (WITH CONFIRMATION) ====================
ACTIVE_ANGPAO = {}

async def process_angpao_request(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, is_apgb: bool = False):
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name

    _, _, _, _, to_target, to_progress = await get_user(user_id, user_name)
    sisa_to = max(0.0, to_target - to_progress)
    if sisa_to > 0:
        await update.message.reply_text(f"❌ Sisa TO kamu masih ada ({sisa_to:.2f}). Selesaikan TO terlebih dahulu sebelum membuat angpao!")
        return

    parts = raw_text.split()
    min_args = 4 if is_apgb else 3
    
    if len(parts) < min_args:
        if is_apgb:
            await update.message.reply_text(
                "❌ Format salah.\n"
                "apgb nominal jumlahorang mindep\n"
                "Contoh: apgb 10 5 50 (10 koin/orang, 5 orang, min dep 50)"
            )
        else:
            await update.message.reply_text(
                "❌ Format salah.\n"
                "angpao nominal jumlahorang\n"
                "Contoh: angpao 10 5 (10 koin/orang, 5 orang)"
            )
        return

    try:
        nominal = float(parts[1])
        jumlah_orang = int(parts[2])
        min_dep = float(parts[3]) if is_apgb else 0.0
    except ValueError:
        await update.message.reply_text("❌ Nominal, jumlah orang, dan minimal deposit harus berupa angka!")
        return

    if nominal < 1 or jumlah_orang < 1:
        await update.message.reply_text("❌ Nominal dan jumlah orang minimal 1!")
        return

    total_biaya = nominal * jumlah_orang
    _, saldo, _, _, _, _ = await get_user(user_id, user_name)

    if saldo < total_biaya:
        disp_saldo = f"{saldo:.2f}"
        disp_biaya = f"{total_biaya:.2f}"
        await update.message.reply_text(f"❌ Saldo tidak cukup!\nButuh: {disp_biaya} | Saldo kamu: {disp_saldo}")
        return

    confirm_id = f"conf_angpao_{user_id}_{int(datetime.now().timestamp())}"
    PENDING_CONFIRMATIONS[confirm_id] = {
        "user_id": user_id,
        "nominal": nominal,
        "jumlah_orang": jumlah_orang,
        "min_dep": min_dep,
        "is_apgb": is_apgb,
        "total_biaya": total_biaya
    }

    type_str = "Angpao Depositor (APGB)" if is_apgb else "Angpao"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ya, Buat", callback_data=f"confirm_angpao_{confirm_id}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"cancel_angpao_{confirm_id}")
        ]
    ])

    await update.message.reply_text(
        f"❓ **Konfirmasi Pembuatan {type_str}**\n\n"
        f"• Nominal: {nominal:.2f} koin/orang\n"
        f"• Kuota: {jumlah_orang} orang\n"
        f"• Total potongan saldo: **{total_biaya:.2f} koin**\n"
        f"{f'• Min Dep: {min_dep:.2f} koin' if is_apgb else ''}\n\n"
        f"Lanjutkan pembuatan angpao ini?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def handle_angpao_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id

    if data.startswith("cancel_angpao_"):
        confirm_id = data.replace("cancel_angpao_", "")
        PENDING_CONFIRMATIONS.pop(confirm_id, None)
        await query.edit_message_text("❌ Pembuatan Angpao dibatalkan.")
        return

    if data.startswith("confirm_angpao_"):
        confirm_id = data.replace("confirm_angpao_", "")
        conf = PENDING_CONFIRMATIONS.get(confirm_id)

        if not conf:
            await query.answer("❌ Sesi konfirmasi telah kedaluwarsa!", show_alert=True)
            return

        if user_id != conf["user_id"]:
            await query.answer("❌ Ini bukan konfirmasi milikmu!", show_alert=True)
            return

        total_biaya = conf["total_biaya"]
        user_name = user.full_name
        _, saldo, _, _, _, _ = await get_user(user_id, user_name)

        if saldo < total_biaya:
            await query.answer("❌ Saldo kamu sudah tidak mencukupi!", show_alert=True)
            PENDING_CONFIRMATIONS.pop(confirm_id, None)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ?", (total_biaya, user_id))
            async with db.execute("SELECT saldo FROM users WHERE user_id = ?", (user_id,)) as cursor:
                curr_s = await cursor.fetchone()
                if curr_s and curr_s[0] <= 0:
                    await db.execute("UPDATE users SET to_target = 0, to_progress = 0 WHERE user_id = ?", (user_id,))
            await db.commit()

        angpao_id = f"angpao_{user_id}_{int(datetime.now().timestamp())}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧧 Ambil Angpao", callback_data=f"claim_{angpao_id}")]
        ])

        title_text = "🎁 ANGPAO DEPOSITOR (APGB)" if conf["is_apgb"] else "🎁 ANGPAO"
        desc_msg = (
            f"{title_text}\n\n"
            f"Dari: @{user.username or user_name}\n"
            f"Nominal: {conf['nominal']:.2f} koin/orang\n"
            f"Kuota: 0 / {conf['jumlah_orang']} orang\n"
        )
        if conf["is_apgb"]:
            desc_msg += f"Syarat Min Dep: {conf['min_dep']:.2f} koin\n"
        desc_msg += "\nDaftar pengambil:\n- Belum ada"

        try:
            await query.message.delete()
        except Exception:
            pass

        sent_msg = await context.bot.send_message(chat_id=chat_id, text=desc_msg, reply_markup=keyboard)

        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except Exception:
            pass

        ACTIVE_ANGPAO[angpao_id] = {
            "nominal": conf["nominal"],
            "total_slot": conf["jumlah_orang"],
            "sisa_slot": conf["jumlah_orang"],
            "pembuat_id": user_id,
            "pembuat_name": user_name,
            "pembuat_username": user.username,
            "min_dep": conf["min_dep"],
            "is_apgb": conf["is_apgb"],
            "klaim_list": [],
            "message_id": sent_msg.message_id,
            "chat_id": chat_id
        }
        PENDING_CONFIRMATIONS.pop(confirm_id, None)

async def handle_angpao_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id
    user_name = user.full_name

    if not data.startswith("claim_"):
        return

    angpao_id = data.replace("claim_", "")
    if angpao_id not in ACTIVE_ANGPAO:
        await query.answer("❌ Angpao ini sudah habis atau kedaluwarsa!", show_alert=True)
        return

    angpao = ACTIVE_ANGPAO[angpao_id]

    if user_id == angpao["pembuat_id"]:
        await query.answer("❌ Pembuat angpao tidak bisa mengambil angpao sendiri!", show_alert=True)
        return

    if user_id in [c["id"] for c in angpao["klaim_list"]]:
        await query.answer("❌ Kamu sudah mengambil angpao ini!", show_alert=True)
        return

    if angpao["is_apgb"] and angpao["min_dep"] > 0:
        _, _, _, _, to_target, _ = await get_user(user_id, user_name)
        if to_target < angpao["min_dep"]:
            await query.answer(f"❌ Syarat APGB tidak terpenuhi! Minimal deposit {angpao['min_dep']:.2f} koin.", show_alert=True)
            return

    if angpao["sisa_slot"] <= 0:
        await query.answer("❌ Kuota angpao sudah habis!", show_alert=True)
        return

    angpao["sisa_slot"] -= 1
    angpao["klaim_list"].append({
        "id": user_id,
        "name": user_name,
        "username": user.username
    })

    nominal = angpao["nominal"]
    await get_user(user_id, user_name)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET saldo = saldo + ? WHERE user_id = ?", (nominal, user_id))
        await db.commit()

    await query.answer(f"✅ Berhasil mendapatkan {nominal:.2f} koin!")

    terambil = angpao["total_slot"] - angpao["sisa_slot"]
    title_text = "🎁 ANGPAO DEPOSITOR (APGB)" if angpao["is_apgb"] else "🎁 ANGPAO"
    pembuat_disp = f"@{angpao['pembuat_username']}" if angpao['pembuat_username'] else angpao['pembuat_name']

    list_str = ""
    for idx, c in enumerate(angpao["klaim_list"], 1):
        c_disp = f"@{c['username']}" if c['username'] else c['name']
        list_str += f"{idx}. {c_disp} (+{nominal:.2f})\n"

    updated_msg = (
        f"{title_text}\n\n"
        f"Dari: {pembuat_disp}\n"
        f"Nominal: {nominal:.2f} koin/orang\n"
        f"Kuota: {terambil} / {angpao['total_slot']} orang\n"
    )
    if angpao["is_apgb"]:
        updated_msg += f"Syarat Min Dep: {angpao['min_dep']:.2f} koin\n"
    updated_msg += f"\nDaftar pengambil:\n{list_str}"

    if angpao["sisa_slot"] > 0:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧧 Ambil Angpao", callback_data=f"claim_{angpao_id}")]
        ])
        try:
            await context.bot.edit_message_text(
                chat_id=angpao["chat_id"],
                message_id=angpao["message_id"],
                text=updated_msg,
                reply_markup=keyboard
            )
        except Exception:
            pass
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=angpao["chat_id"],
                message_id=angpao["message_id"],
                text=updated_msg + "\n🎉 ANGPAO HABIS TERAMBIL!"
            )
            await context.bot.unpin_chat_message(chat_id=angpao["chat_id"], message_id=angpao["message_id"])
        except Exception:
            pass
        del ACTIVE_ANGPAO[angpao_id]


# ==================== FITUR GESERAN (WITH CONFIRMATION) ====================
ACTIVE_GESERAN = {}

async def process_geseran_request(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name

    _, _, _, _, to_target, to_progress = await get_user(user_id, user_name)
    sisa_to = max(0.0, to_target - to_progress)
    if sisa_to > 0:
        await update.message.reply_text(f"❌ Sisa TO kamu masih ada ({sisa_to:.2f}). Selesaikan TO terlebih dahulu sebelum membuat geseran!")
        return

    parts = raw_text.split()
    if len(parts) < 3:
        await update.message.reply_text(
            "❌ Format salah.\n"
            "geseran #kode nominal jumlahorang\n"
            "Contoh: geseran #promo 1 10 (1 koin/orang, 10 orang)\n"
            "Kode diawali # · min 1 koin/orang · maks 100 orang.\n"
            "Klaim: cukup ketik kodenya di grup."
        )
        return

    kode = parts[1]
    try:
        nominal = float(parts[2])
        jumlah_orang = int(parts[3])
    except ValueError:
        await update.message.reply_text("❌ Nominal dan jumlah orang harus berupa angka!")
        return

    if not kode.startswith("#"):
        await update.message.reply_text("❌ Kode harus diawali dengan tanda pagar (#). Contoh: #promo")
        return

    if nominal < 1:
        await update.message.reply_text("❌ Minimal 1 koin/orang.")
        return

    if jumlah_orang < 1 or jumlah_orang > 100:
        await update.message.reply_text("❌ Maksimal 100 orang.")
        return

    if kode in ACTIVE_GESERAN:
        await update.message.reply_text(f"❌ Kode {kode} sudah aktif!")
        return

    total_biaya = nominal * jumlah_orang
    _, saldo, _, _, _, _ = await get_user(user_id, user_name)

    if saldo < total_biaya:
        disp_saldo = f"{saldo:.2f}"
        disp_biaya = f"{total_biaya:.2f}"
        await update.message.reply_text(f"❌ Saldo tidak cukup untuk membuat geseran!\nButuh: {disp_biaya} | Saldo kamu: {disp_saldo}")
        return

    confirm_id = f"conf_geseran_{user_id}_{int(datetime.now().timestamp())}"
    PENDING_CONFIRMATIONS[confirm_id] = {
        "user_id": user_id,
        "kode": kode,
        "nominal": nominal,
        "jumlah_orang": jumlah_orang,
        "total_biaya": total_biaya
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ya, Buat", callback_data=f"confirm_geseran_{confirm_id}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"cancel_geseran_{confirm_id}")
        ]
    ])

    await update.message.reply_text(
        f"❓ **Konfirmasi Pembuatan Geseran**\n\n"
        f"• Kode: `{kode}`\n"
        f"• Nominal: {nominal:.2f} koin/orang\n"
        f"• Kuota: {jumlah_orang} orang\n"
        f"• Total potongan saldo: **{total_biaya:.2f} koin**\n\n"
        f"Lanjutkan pembuatan geseran ini?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def handle_geseran_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id

    if data.startswith("cancel_geseran_"):
        confirm_id = data.replace("cancel_geseran_", "")
        PENDING_CONFIRMATIONS.pop(confirm_id, None)
        await query.edit_message_text("❌ Pembuatan Geseran dibatalkan.")
        return

    if data.startswith("confirm_geseran_"):
        confirm_id = data.replace("confirm_geseran_", "")
        conf = PENDING_CONFIRMATIONS.get(confirm_id)

        if not conf:
            await query.answer("❌ Sesi konfirmasi telah kedaluwarsa!", show_alert=True)
            return

        if user_id != conf["user_id"]:
            await query.answer("❌ Ini bukan konfirmasi milikmu!", show_alert=True)
            return

        total_biaya = conf["total_biaya"]
        kode = conf["kode"]
        nominal = conf["nominal"]
        jumlah_orang = conf["jumlah_orang"]
        user_name = user.full_name

        _, saldo, _, _, _, _ = await get_user(user_id, user_name)

        if saldo < total_biaya:
            await query.answer("❌ Saldo kamu sudah tidak mencukupi!", show_alert=True)
            PENDING_CONFIRMATIONS.pop(confirm_id, None)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ?", (total_biaya, user_id))
            async with db.execute("SELECT saldo FROM users WHERE user_id = ?", (user_id,)) as cursor:
                curr_s = await cursor.fetchone()
                if curr_s and curr_s[0] <= 0:
                    await db.execute("UPDATE users SET to_target = 0, to_progress = 0 WHERE user_id = ?", (user_id,))
            await db.commit()

        async def expire_geseran():
            await asyncio.sleep(1800)
            if kode in ACTIVE_GESERAN:
                g_data = ACTIVE_GESERAN[kode]
                sisa_slot = g_data["sisa_slot"]
                if sisa_slot > 0:
                    refund_amt = sisa_slot * g_data["nominal"]
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute("UPDATE users SET saldo = saldo + ? WHERE user_id = ?", (refund_amt, g_data["pembuat_id"]))
                        await db.commit()
                    disp_ref = f"{refund_amt:.2f}"
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏰ Geseran dengan kode `{kode}` kedaluwarsa (30 menit).\nSisa {sisa_slot} slot dikembalikan ke pembuat ({disp_ref} koin).",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                del ACTIVE_GESERAN[kode]

        task = asyncio.create_task(expire_geseran())

        ACTIVE_GESERAN[kode] = {
            "nominal": nominal,
            "sisa_slot": jumlah_orang,
            "total_slot": jumlah_orang,
            "pembuat_id": user_id,
            "pembuat_name": user_name,
            "pembuat_username": user.username,
            "klaim_list": [],
            "task": task
        }

        disp_nom = f"{nominal:.2f}"
        disp_total = f"{total_biaya:.2f}"

        msg = (
            f"🎟 Geseran dibuat!\n\n"
            f"Dari: @{user.username or user_name}\n"
            f"Kode: `{kode}`\n"
            f"Nominal: {disp_nom} koin/orang\n"
            f"Kuota: {jumlah_orang} orang yang bisa mengambil sesuai kuota yang diberikan\n"
            f"Total keluar: {disp_total} koin total koin yang di keluarkan\n"
            f"Aktif: 30 menit (sisa kuota balik ke pembuat)\n\n"
            f"👉 Ketik `{kode}` untuk klaim — siapa cepat dia dapat! (ketik manual ya)"
        )
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        PENDING_CONFIRMATIONS.pop(confirm_id, None)

async def check_and_claim_geseran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text.strip()
    text_lower = raw_text.lower()
    
    if text_lower == "geseran" or text_lower.startswith("geseran "):
        await process_geseran_request(update, context, raw_text)
        return

    if text_lower.startswith("#"):
        clean_code = text_lower
        if clean_code in ACTIVE_GESERAN:
            user = update.effective_user
            user_id = user.id
            user_name = user.full_name
            chat_id = update.effective_chat.id
            data = ACTIVE_GESERAN[clean_code]

            if user_id in [item["id"] for item in data["klaim_list"]]:
                return

            if data["sisa_slot"] > 0:
                data["sisa_slot"] -= 1
                data["klaim_list"].append({
                    "id": user_id,
                    "name": user_name,
                    "username": user.username
                })

                nominal = data["nominal"]
                await get_user(user_id, user_name)

                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("UPDATE users SET saldo = saldo + ? WHERE user_id = ?", (nominal, user_id))
                    await db.commit()

                disp_nom = f"{nominal:.2f}"
                user_display = f"@{user.username}" if user.username else user_name

                success_claim_msg = f"✅ {user_display} dapat {disp_nom} koin dari geseran `{clean_code}`"
                await update.message.reply_text(success_claim_msg, parse_mode="Markdown")

                if data["sisa_slot"] == 0:
                    if data["task"]:
                        data["task"].cancel()
                    
                    pembuat_display = f"@{data['pembuat_username']}" if data['pembuat_username'] else data['pembuat_name']
                    
                    list_str = ""
                    for idx, claimant in enumerate(data["klaim_list"], 1):
                        c_name = f"@{claimant['username']}" if claimant['username'] else claimant['name']
                        list_str += f"{idx}. {c_name}\n"

                    habis_msg = (
                        f"🎁 GESERAN HABIS! 🎉\n\n"
                        f"Kode `{clean_code}` sudah ludes — semua {data['total_slot']} kuota terambil. 🙌\n"
                        f"Dari: {pembuat_display}\n\n"
                        f"Yang dapat:\n"
                        f"{list_str}\n"
                        f"Pantengin geseran berikutnya, jangan sampai kelewatan! ✨"
                    )
                    del ACTIVE_GESERAN[clean_code]
                    await context.bot.send_message(chat_id=chat_id, text=habis_msg, parse_mode="Markdown")


# ==================== FITUR WS DANA ====================
async def process_ws_dana_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name

    _, saldo, _, _, to_target, to_progress = await get_user(user_id, user_name)
    sisa_to = max(0.0, to_target - to_progress)

    if sisa_to > 0:
        await update.message.reply_text(f"❌ Tidak dapat melakukan penarikan/WS DANA! Masih ada sisa TO sebesar {sisa_to:.2f}.")
        return

    if saldo < 11:
        await update.message.reply_text(f"❌ Saldo tidak mencukupi untuk penarikan! Minimal WS DANA adalah 11 koin. Saldo kamu: {saldo:.2f}")
        return

    ws_msg = (
        f"🌐 **WEBSOCKET DANA / PENARIKAN**\n\n"
        f"👤 Pemohon: {user_name} (`{user_id}`)\n"
        f"💰 Saldo Tersedia: {saldo:.2f} koin\n"
        f"📌 Minimal Tarik: 11 koin (1 koin = Rp 1.000)\n\n"
        f"Silakan hubungi Admin/Bot Kasir untuk pemrosesan pencairan Dana lebih lanjut."
    )
    await update.message.reply_text(ws_msg, parse_mode="Markdown")


# ==================== FITUR WIN STREAK (WS) ====================
async def process_win_streak_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    user_id = user.id
    user_disp = f"@{user.username}" if user.username else user.full_name

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT ws_streak, ws_claimed FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            streak = row[0] if row else 0
            claimed = row[1] if row else 0

    bonus_amount = calculate_ws_bonus(streak)
    
    msg = (
        f"📊 STATUS WIN STREAK\n"
        f"User: {user_disp}\n\n"
        f"Streak saat ini: {streak}x\n"
        f"Bonus saat ini: {bonus_amount:.2f} koin\n"
        f"Min. Bet: 3 koin\n"
        f"Max Streak: 20x (Bonus 9.50 koin)\n\n"
    )

    reply_markup = None
    if streak < 3:
        needed = 3 - streak
        msg += f"Bonus belum aktif (minimal WS 3x).\nMenang beruntun lagi: {needed}x."
    else:
        if claimed == 0:
            msg += f"🎉 Bonus WS kamu aktif! Klik tombol di bawah ini untuk klaim bonus."
            keyboard = [[InlineKeyboardButton("CLAIM WS💥", callback_data=f"claim_ws_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            msg += "✅ Bonus WS untuk streak saat ini sudah diklaim! Tingkatkan streak lagi untuk bonus berikutnya."

    await update.message.reply_text(msg, reply_markup=reply_markup)

async def handle_claim_ws_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    user_id = user.id

    if not data.startswith("claim_ws_"):
        return

    target_id = int(data.split("_")[-1])
    if user_id != target_id:
        await query.answer("❌ Ini bukan bonus Win Streak kamu!", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT ws_streak, ws_claimed FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            streak = row[0] if row else 0
            claimed = row[1] if row else 0

        if streak < 3:
            await query.answer("❌ Minimal Win Streak 3x untuk klaim bonus!", show_alert=True)
            return

        if claimed == 1:
            await query.answer("❌ Kamu sudah mengklaim bonus untuk streak saat ini!", show_alert=True)
            return

        bonus = calculate_ws_bonus(streak)
        await db.execute("UPDATE users SET saldo = saldo + ?, ws_claimed = 1 WHERE user_id = ?", (bonus, user_id))
        await db.commit()

    await query.answer(f"🎉 Sukses klaim bonus WS sebesar {bonus:.2f} koin!", show_alert=True)
    try:
        await query.edit_message_text(f"✅ Bonus Win Streak {streak}x sebesar {bonus:.2f} koin telah diklaim ke saldo kamu! 💥")
    except Exception:
        pass


# ==================== FITUR LEADERBOARD (LB) ====================
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    user_id = user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT lb_wins FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            user_wins = row[0] if row else 0

        async with db.execute("SELECT user_id, nama, lb_wins FROM users WHERE lb_wins >= 10 ORDER BY lb_wins DESC") as cursor:
            all_lb = await cursor.fetchall()

    if user_wins < 10:
        msg = (
            "📊 Posisi Leaderboard KB DaduGo\n\n"
            f"🎯 Total menang kamu: {user_wins}x\n"
            "⚠️ Kamu belum masuk Leaderboard. Minimal harus menang 10x (taruhan ≥ 10.00 koin)!\n\n"
            "🎁 Hadiah: #1 20 • #2 15 • #3 10 koin\n\n"
            "👀 Top 1-10 lengkap di @lbdadustar"
        )
    else:
        rank = 0
        total_players = len(all_lb)
        for idx, (u_id, _, _) in enumerate(all_lb, 1):
            if u_id == user_id:
                rank = idx
                break

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        rank_icon = medals.get(rank, f"#{rank}")

        msg = (
            "📊 Posisi Leaderboard KB DaduGo\n\n"
            f"Peringkat kamu: {rank_icon} #{rank} dari {total_players} pemain\n"
            f"🎯 Total menang: {user_wins}x\n"
            f"🎁 Hadiah: #1 20 • #2 15 • #3 10 koin\n\n"
            "👀 Top 1-10 lengkap di @lbdadustar"
        )

    await update.message.reply_text(msg)

async def generate_lb_data():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, nama, lb_wins FROM users WHERE lb_wins >= 10 ORDER BY lb_wins DESC LIMIT 10") as cursor:
            top_players = await cursor.fetchall()

    medals = ["🥇", "🥈", "🥉"]
    list_str = ""
    for idx, (u_id, name, wins) in enumerate(top_players, 1):
        disp_name = name if name.startswith("@") else f"@{name}"
        prefix = medals[idx-1] if idx <= 3 else f"#{idx}"
        list_str += f"{prefix} {disp_name} — {wins}x menang\n"

    if not list_str:
        list_str = "Belum ada pemain yang memenuhi syarat min 10x menang.\n"

    return top_players, list_str

async def update_leaderboard_channel(context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    now_wib = datetime.now(WIB)
    
    if now_wib.hour < 9:
        start_date = now_wib - timedelta(days=1)
        end_date = now_wib
    else:
        start_date = now_wib
        end_date = now_wib + timedelta(days=1)

    date_str = start_date.strftime("%d/%m/%Y")
    p_start = start_date.strftime("%d/%m/%Y 09:00")
    p_end = end_date.strftime("%d/%m/%Y 09:00")

    target_09 = now_wib.replace(hour=9, minute=0, second=0, microsecond=0)
    if now_wib >= target_09:
        target_09 += timedelta(days=1)
    
    time_diff = target_09 - now_wib
    sisa_jam = int(time_diff.total_seconds() // 3600)
    sisa_menit = int((time_diff.total_seconds() % 3600) // 60)

    _, list_str = await generate_lb_data()
    update_time = now_wib.strftime("%H:%M WIB")

    msg_lb = (
        f"🏆 Leaderboard KB DaduStar — {date_str}\n\n"
        f"{list_str}\n"
        f"💰 Hadiah: #1 20 • #2 15 • #3 10 koin\n"
        f"📊 Syarat: min 10x menang (taruhan ≥ 10.00 koin)\n"
        f"🗓️ Periode: {p_start} - {p_end} WIB\n"
        f"⏰ Reward jam 09:00 WIB · Sisa {sisa_jam}j {sisa_menit}m\n\n"
        f"🔄 Diperbarui: {update_time}"
    )

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT message_id FROM lb_state WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            lb_msg_id = row[0] if row else None

    if lb_msg_id:
        try:
            await context.bot.edit_message_text(chat_id=LB_CHANNEL_ID, message_id=lb_msg_id, text=msg_lb)
            return
        except Exception:
            pass

    try:
        sent = await context.bot.send_message(chat_id=LB_CHANNEL_ID, text=msg_lb)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR REPLACE INTO lb_state (id, message_id) VALUES (1, ?)", (sent.message_id,))
            await db.commit()
    except Exception as e:
        print(f"Gagal kirim update LB ke channel: {e}")

async def process_daily_reward(context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    now_wib = datetime.now(WIB)
    yesterday_wib = now_wib - timedelta(days=1)

    top_players, list_str = await generate_lb_data()

    rewards = [20.0, 15.0, 10.0]
    reward_text = ""

    async with aiosqlite.connect(DB_NAME) as db:
        for idx, (u_id, name, _) in enumerate(top_players[:3]):
            reward = rewards[idx]
            disp_name = name if name.startswith("@") else f"@{name}"
            reward_text += f"#{idx+1} {disp_name} +{reward:.2f} koin\n"
            
            await db.execute("""
                UPDATE users 
                SET saldo = saldo + ?, to_target = to_target + ? 
                WHERE user_id = ?
            """, (reward, reward, u_id))

        await db.execute("UPDATE users SET lb_wins = 0")
        await db.execute("DELETE FROM lb_state WHERE id = 1")
        await db.commit()

    if reward_text:
        msg_reward = (
            f"🏆 Leaderboard KB DaduStar — {yesterday_wib.strftime('%d/%m/%Y')}\n\n"
            f"{list_str}\n"
            f"📌 Masuk LB: min 10x menang (taruhan ≥ 10.00 koin).\n\n"
            f"🎁 Reward berhasil dibagikan:\n"
            f"{reward_text}\n"
            f"♻️ Koin hadiah ber-TO 1× (dimainkan dulu sebelum bisa ditarik)."
        )
        try:
            await context.bot.send_message(chat_id=LB_CHANNEL_ID, text=msg_reward)
        except Exception as e:
            print(f"Gagal kirim reward LB ke channel: {e}")

async def schedule_daily_lb(context: ContextTypes.DEFAULT_TYPE):
    while True:
        now = datetime.now(WIB)
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        await asyncio.sleep(sleep_seconds)
        await process_daily_reward(context)

# ==================== HELPER CUSTOM TITLE TELEGRAM ====================
def _plain_admin_title(formatted_title: str) -> str:
    title = formatted_title.strip()
    if len(title) > 16:
        raise ValueError("Gelar Telegram maksimal 16 karakter termasuk tanda kurung dan spasi.")
    return title

async def _telegram_retry(operation, *, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        try:
            return await operation()
        except RetryAfter as exc:
            last_error = exc
            await asyncio.sleep(min(float(exc.retry_after) + 0.5, 10))
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_error

async def apply_custom_admin_title(bot, chat_id: int, user_id: int, formatted_title: str):
    exact_title = _plain_admin_title(formatted_title)
    member = await _telegram_retry(lambda: bot.get_chat_member(chat_id=chat_id, user_id=user_id))
    if member.status == "creator":
        raise RuntimeError("Pemilik grup (creator) tidak bisa diberi custom title melalui Bot API.")

    bot_member = await _telegram_retry(lambda: bot.get_chat_member(chat_id=chat_id, user_id=bot.id))
    if not getattr(bot_member, "can_promote_members", False):
        raise RuntimeError("Bot tidak memiliki izin Promote Members.")

    promoted = await _telegram_retry(lambda: bot.promote_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        can_manage_chat=False,
        can_change_info=False,
        can_post_messages=False,
        can_edit_messages=False,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    ))
    if not promoted:
        raise RuntimeError("Telegram gagal mengatur hak admin pengguna.")

    await _telegram_retry(lambda: bot.set_chat_administrator_custom_title(
        chat_id=chat_id, user_id=user_id, custom_title=exact_title
    ))

    verified = await _telegram_retry(lambda: bot.get_chat_member(chat_id=chat_id, user_id=user_id))
    actual_title = getattr(verified, "custom_title", None)
    if actual_title != exact_title:
        raise RuntimeError(f"Verifikasi title gagal. Diminta: {exact_title}; terbaca: {actual_title!r}.")

# ==================== FITUR /BUYTITLE ====================
async def buy_title_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id
    user_name = user.full_name
    args = context.args

    if not args:
        pesan = (
            "🏷️ Beli Gelar — 10 koin/hari (maks 30 hari)\n"
            "/buytitle keren → [ KEREN ] · /buytitle keren 3d → 3 hari\n"
            "Maks 15 char (isi ≤ 11), huruf/angka/spasi.\n"
            "Kuota gelar per grup: 30 orang (dipakai bareng dengan reqtitle)."
        )
        await update.message.reply_text(pesan)
        return

    raw_title = args[0]
    duration_arg = args[1] if len(args) > 1 else "1d"
    formatted_title, err = validate_and_format_title(raw_title)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return
    days, err_dur = parse_duration(duration_arg)
    if err_dur:
        await update.message.reply_text(f"❌ {err_dur}")
        return

    total_price = float(days * 10)
    _, saldo, _, _, _, _ = await get_user(user_id, user_name)
    if saldo < total_price:
        await update.message.reply_text(
            f"❌ Saldo tidak mencukupi!\nButuh: {total_price:.0f} koin | Saldo kamu: {saldo:.2f} koin"
        )
        return

    now_wib = datetime.now(WIB)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM user_titles WHERE chat_id = ? AND expires_at > ?",
            (chat_id, now_wib.isoformat())
        ) as cursor:
            row_count = await cursor.fetchone()
        async with db.execute(
            "SELECT expires_at FROM user_titles WHERE user_id = ? AND chat_id = ? AND expires_at > ?",
            (user_id, chat_id, now_wib.isoformat())
        ) as cursor:
            existing_title = await cursor.fetchone()

    if not existing_title and row_count and row_count[0] >= 30:
        await update.message.reply_text("❌ Kuota gelar grup sudah penuh (30/30).\nSilakan coba kembali ketika tersedia slot.")
        return

    confirm_id = uuid.uuid4().hex[:16]
    PENDING_CONFIRMATIONS[f"buytitle_{confirm_id}"] = {
        "user_id": user_id, "chat_id": chat_id, "user_name": user_name,
        "title": formatted_title, "days": days, "price": total_price,
        "created_at": now_wib.isoformat()
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Konfirmasi", callback_data=f"confirm_buytitle_{confirm_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"cancel_buytitle_{confirm_id}")
    ]])
    await update.message.reply_text(
        f"🏷️ Konfirmasi beli gelar {formatted_title} Durasi {days} Hari • harga {total_price:.0f} coin lanjut?",
        reply_markup=keyboard
    )

async def handle_buytitle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    prefix = "confirm_buytitle_" if data.startswith("confirm_buytitle_") else "cancel_buytitle_"
    confirm_id = data[len(prefix):]
    key = f"buytitle_{confirm_id}"
    pending = PENDING_CONFIRMATIONS.get(key)

    if not pending:
        await query.answer("Konfirmasi sudah kedaluwarsa atau sudah diproses.", show_alert=True)
        return
    if user_id != pending["user_id"]:
        await query.answer("Tombol ini bukan milikmu.", show_alert=True)
        return

    if data.startswith("cancel_buytitle_"):
        PENDING_CONFIRMATIONS.pop(key, None)
        await query.edit_message_text("❌ Pembelian gelar dibatalkan.")
        return

    await query.answer()
    await ensure_db_tables_and_columns()
    chat_id = pending["chat_id"]
    title = pending["title"]
    days = pending["days"]
    price = pending["price"]
    user_name = pending["user_name"]
    now_wib = datetime.now(WIB)

    async with title_lock:
        _, saldo, _, _, _, _ = await get_user(user_id, user_name)
        if saldo < price:
            PENDING_CONFIRMATIONS.pop(key, None)
            await query.edit_message_text(f"❌ Saldo tidak mencukupi. Harga {price:.0f} koin, saldo kamu {saldo:.2f} koin.")
            return

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM user_titles WHERE chat_id = ? AND expires_at > ?", (chat_id, now_wib.isoformat())) as cursor:
                row = await cursor.fetchone()
            async with db.execute("SELECT expires_at FROM user_titles WHERE user_id = ? AND chat_id = ? AND expires_at > ?", (user_id, chat_id, now_wib.isoformat())) as cursor:
                existing = await cursor.fetchone()
            if not existing and row and row[0] >= 30:
                PENDING_CONFIRMATIONS.pop(key, None)
                await query.edit_message_text("❌ Kuota gelar grup sudah penuh (30/30).")
                return

            new_expiry = now_wib + timedelta(days=days)
            if existing:
                try:
                    old_expiry = datetime.fromisoformat(existing[0])
                    if old_expiry > now_wib:
                        new_expiry = old_expiry + timedelta(days=days)
                except (TypeError, ValueError):
                    pass

            tx_id = f"BUY_{user_id}_{int(now_wib.timestamp())}_{uuid.uuid4().hex[:6]}"
            await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ? AND saldo >= ?", (price, user_id, price))
            if user_id != OWNER_ID:
                await db.execute("UPDATE users SET saldo = saldo + ? WHERE user_id = ?", (price, OWNER_ID))
            await db.execute("""INSERT OR IGNORE INTO title_transactions
                (tx_id, buyer_id, owner_id, amount, title, duration_days, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tx_id, user_id, OWNER_ID, price, title, days, now_wib.strftime("%Y-%m-%d %H:%M:%S")))
            await db.execute("""INSERT INTO user_titles (user_id, chat_id, title, expires_at, type)
                VALUES (?, ?, ?, ?, 'buy')
                ON CONFLICT(user_id, chat_id) DO UPDATE SET title=excluded.title, expires_at=excluded.expires_at, type='buy'""",
                (user_id, chat_id, title, new_expiry.isoformat()))
            await db.commit()

    PENDING_CONFIRMATIONS.pop(key, None)
    try:
        await apply_custom_admin_title(context.bot, chat_id, user_id, title)
    except Exception as e:
        await query.edit_message_text(
            f"Pembelian gelar {title} berhasil dicatat, tetapi custom title Telegram belum bisa dipasang. "
            f"Pastikan bot punya izin Promote Members dan pengguna bukan creator grup. Detail: {e}"
        )
        return
    await query.edit_message_text(f"Selamat title {title} Sudah aktif silahkan di cek.")

buytitle_command = buy_title_command

# ==================== FITUR /REQTITLE & ADMIN OTOMATIS ====================
async def reqtitle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id
    user_name = user.full_name

    args = context.args
    if not args:
        pesan = (
            "🏷️ Gelar Depositor — gratis, syaratnya setor 50 koin hari ini.\n"
            "reqtitle ganteng → [ GANTENG ]\n"
            "Maks 15 karakter (isi ≤ 11), huruf/angka/spasi.\n"
            "Dicek ulang tiap hari: kalau besok tidak setor segitu lagi, status adminnya dicabut.\n"
            "Kuota gelar per grup: 30 orang (dipakai bareng dengan /buytitle)."
        )
        await update.message.reply_text(pesan)
        return

    raw_title = args[0]
    formatted_title, err = validate_and_format_title(raw_title)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    now_wib = datetime.now(WIB)
    today_date_str = now_wib.strftime("%Y-%m-%d")

    async with title_lock:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("""
                SELECT SUM(amount) FROM deposit_history 
                WHERE user_id = ? AND date_str = ?
            """, (user_id, today_date_str)) as cursor:
                row_dep = await cursor.fetchone()
                total_deposit_today = row_dep[0] if row_dep and row_dep[0] else 0.0

            if total_deposit_today < 50.0:
                shortage = 50.0 - total_deposit_today
                reject_msg = (
                    f"❌ Setoran hari ini belum cukup.\n"
                    f"Deposit hari ini: {total_deposit_today:.0f} koin — syaratnya 50 koin (kurang {shortage:.0f}).\n"
                    f"Silakan penuhi kekurangan deposit sebelum mengajukan gelar depositor."
                )
                await update.message.reply_text(reject_msg)
                return

            async with db.execute("SELECT COUNT(*) FROM user_titles WHERE chat_id = ? AND expires_at > ?", (chat_id, now_wib.isoformat())) as cursor:
                row_count = await cursor.fetchone()
                active_count = row_count[0] if row_count else 0

            async with db.execute("SELECT expires_at FROM user_titles WHERE user_id = ? AND chat_id = ? AND expires_at > ?", (user_id, chat_id, now_wib.isoformat())) as cursor:
                existing_title = await cursor.fetchone()

            if not existing_title and active_count >= 30:
                await update.message.reply_text("❌ Kuota gelar grup sudah penuh (30/30).\nSilakan coba kembali ketika tersedia slot.")
                return

        try:
            bot_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=context.bot.id)
            if not bot_member.can_promote_members:
                await update.message.reply_text("❌ Bot tidak memiliki izin untuk mempromosikan anggota menjadi admin di grup ini.")
                return
        except Exception as e:
            await update.message.reply_text(f"❌ Gagal memeriksa izin bot: {e}")
            return

        try:
            await apply_custom_admin_title(context.bot, chat_id, user_id, formatted_title)
        except Exception as e:
            await update.message.reply_text(f"❌ Terjadi kesalahan saat promosi admin: {e}")
            return

        expires_at_dt = now_wib + timedelta(days=1)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                INSERT INTO user_titles (user_id, chat_id, title, expires_at, type)
                VALUES (?, ?, ?, ?, 'req')
                ON CONFLICT(user_id, chat_id) DO UPDATE SET title=excluded.title, expires_at=excluded.expires_at, type='req'
            """, (user_id, chat_id, formatted_title, expires_at_dt.isoformat()))

            await db.execute("""
                INSERT OR REPLACE INTO auto_admins (user_id, chat_id, granted_at, active)
                VALUES (?, ?, ?, 1)
            """, (user_id, chat_id, now_wib.strftime("%Y-%m-%d %H:%M:%S")))
            await db.commit()

        success_reply = (
            f"🏅 Gelar {formatted_title} aktif!\n"
            f"Deposit hari ini {total_deposit_today:.0f} koin.\n\n"
            f"⚠️ Tiap hari jam 23.56 WIB dicek ulang. Kalau hari itu kamu tidak setor 50 koin, status adminnya dicabut."
        )
        await update.message.reply_text(success_reply)

# ==================== PEMERIKSAAN OTOMATIS 23.56 WIB ====================
async def daily_admin_revocation_job(context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    now_wib = datetime.now(WIB)
    today_date_str = now_wib.strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT last_date FROM scheduler_state WHERE key = 'revocation_job'") as cursor:
            row = await cursor.fetchone()
            if row and row[0] == today_date_str:
                return

        async with db.execute("SELECT user_id, chat_id FROM auto_admins WHERE active = 1") as cursor:
            auto_admin_list = await cursor.fetchall()

        for user_id, chat_id in auto_admin_list:
            async with db.execute("""
                SELECT SUM(amount) FROM deposit_history 
                WHERE user_id = ? AND date_str = ?
            """, (user_id, today_date_str)) as cursor:
                dep_row = await cursor.fetchone()
                total_dep = dep_row[0] if dep_row and dep_row[0] else 0.0

            if total_dep >= 50.0:
                continue

            try:
                member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if member.status in ['creator']:
                    continue

                await context.bot.promote_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    can_manage_chat=False,
                    can_delete_messages=False,
                    can_invite_users=False,
                    can_restrict_members=False
                )

                async with aiosqlite.connect(DB_NAME) as db_inner:
                    await db_inner.execute("UPDATE auto_admins SET active = 0 WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
                    await db_inner.commit()
            except Exception as e:
                print(f"Gagal mencabut admin otomatis untuk user {user_id} di chat {chat_id}: {e}")

        await db.execute("""
            INSERT OR REPLACE INTO scheduler_state (key, last_date)
            VALUES ('revocation_job', ?)
        """, (today_date_str,))
        await db.commit()

async def scheduler_loop(context: ContextTypes.DEFAULT_TYPE):
    while True:
        try:
            now = datetime.now(WIB)
            target = now.replace(hour=23, minute=56, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
            await daily_admin_revocation_job(context)
        except Exception as e:
            print(f"Error pada scheduler loop: {e}")
            await asyncio.sleep(60)

async def record_successful_deposit(user_id: int, amount: float):
    now_wib = datetime.now(WIB)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            INSERT INTO deposit_history (user_id, amount, created_at, date_str)
            VALUES (?, ?, ?, ?)
        """, (user_id, amount, now_wib.strftime("%Y-%m-%d %H:%M:%S"), now_wib.strftime("%Y-%m-%d"))) as cursor:
            await db.commit()

async def setfee_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global fee_persen
    user = update.effective_user
    
    if user.id != OWNER_ID:
        await update.message.reply_text("❌ Fitur ini hanya bisa digunakan oleh Owner!")
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(f"❌ Format salah.\nContoh: `/setfee 12`\nFee saat ini: {fee_persen}%", parse_mode="Markdown")
        return

    try:
        val = float(parts[1])
        if val < 0 or val > 100:
            await update.message.reply_text("❌ Fee harus berada di antara 0 - 100%!")
            return
        
        fee_persen = val
        save_config(fee_persen)
        
        fee_disp = int(fee_persen) if fee_persen.is_integer() else fee_persen
        await update.message.reply_text(f"Fee diubah jadi {fee_disp}%")
    except ValueError:
        await update.message.reply_text("❌ Angka fee tidak valid!")

async def process_saldo_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name
    _, saldo, _, _, _, _ = await get_user(user_id, user_name)

    msg = (
        f"💰 Saldo: {saldo:.2f} koin\n"
        f"1 koin = 1.000 · min duel 1 · transfer 1 · tarik 11\n"
        f"/deposit — top up"
    )
    await update.message.reply_text(msg)

async def process_to_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name
    _, saldo, _, _, to_target, to_progress = await get_user(user_id, user_name)
    
    if saldo <= 0:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET to_target = 0, to_progress = 0 WHERE user_id = ?", (user_id,))
            await db.commit()
        to_target = 0.0
        to_progress = 0.0

    sisa_to = max(0.0, to_target - to_progress)
    saldo_bebas = max(0.0, saldo - sisa_to)

    msg = (
        f"📊 TO\n"
        f"Progress: {to_progress:.2f}/{to_target:.2f}\n"
        f"Sisa TO: {sisa_to:.2f}\n\n"
        f"Saldo: {saldo:.2f}\n"
        f"Saldo bebas: {saldo_bebas:.2f}"
    )
    await update.message.reply_text(msg)

async def process_kur_request(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    now_wib_str = datetime.now(WIB).strftime("%d/%m %H:%M WIB")
    
    parts = raw_text.split()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT side, amount FROM player_picks WHERE user_id = ?", (user_id,)) as cursor:
            pick_row = await cursor.fetchone()

    if len(parts) == 1:
        if not pick_row:
            await update.message.reply_text("❌ Kamu tidak memiliki taruhan aktif di meja untuk di-kur.")
            return
        
        current_side, total_pick_amount = pick_row
        side_disp = "Kecil" if current_side == 'K' else "Besar"
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                UPDATE users 
                SET saldo = saldo + ? 
                WHERE user_id = ?
            """, (total_pick_amount, user_id))
            
            await db.execute("DELETE FROM player_picks WHERE user_id = ?", (user_id,))
            
            if current_side == 'K':
                await db.execute("UPDATE game_room SET players_k = MAX(0, players_k - 1) WHERE id = 1")
            else:
                await db.execute("UPDATE game_room SET players_b = MAX(0, players_b - 1) WHERE id = 1")
                
            await db.execute("""
                INSERT INTO user_picks_history (user_id, created_at, side, amount, result)
                VALUES (?, ?, ?, ?, 'reff')
            """, (user_id, now_wib_str, side_disp, total_pick_amount))

            await db.commit()

        disp_kur = f"{total_pick_amount:.2f}"
        await update.message.reply_text(f"✅ /kur Berhasil. 💸Semua taruhan sebesar **{disp_kur}** ditarik kembali ke saldo.", parse_mode="Markdown")
        
        picks_list_text = await format_picks_list()
        await context.bot.send_message(chat_id=chat_id, text=picks_list_text)
        await check_and_trigger_auto_roll(chat_id, context)
    else:
        try:
            kur_amount = float(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Format nominal KUR tidak valid!")
            return
        
        if kur_amount <= 0:
            await update.message.reply_text("❌ Nominal KUR harus lebih dari 0!")
            return

        if not pick_row:
            await update.message.reply_text("❌ Kamu tidak memiliki taruhan aktif di meja.")
            return

        current_side, current_pick_amount = pick_row
        side_disp = "Kecil" if current_side == 'K' else "Besar"

        if kur_amount > current_pick_amount:
            disp_max = f"{current_pick_amount:.2f}"
            await update.message.reply_text(f"❌ Nominal KUR melebihi total taruhan kamu di meja! Maksimal taruhan kamu saat ini adalah {disp_max}.")
            return

        async with aiosqlite.connect(DB_NAME) as db:
            if kur_amount == current_pick_amount:
                await db.execute("DELETE FROM player_picks WHERE user_id = ?", (user_id,))
                if current_side == 'K':
                    await db.execute("UPDATE game_room SET players_k = MAX(0, players_k - 1) WHERE id = 1")
                else:
                    await db.execute("UPDATE game_room SET players_b = MAX(0, players_b - 1) WHERE id = 1")
            else:
                await db.execute("""
                    UPDATE player_picks 
                    SET amount = amount - ? 
                    WHERE user_id = ?
                """, (kur_amount, user_id))

            await db.execute("""
                UPDATE users 
                SET saldo = saldo + ? 
                WHERE user_id = ?
            """, (kur_amount, user_id))
            
            await db.execute("""
                INSERT INTO user_picks_history (user_id, created_at, side, amount, result)
                VALUES (?, ?, ?, ?, 'reff')
            """, (user_id, now_wib_str, side_disp, kur_amount))

            await db.commit()

        disp_kur = f"{kur_amount:.2f}"
        await update.message.reply_text(f"✅ /kur Berhasil. 💸Taruhan ditarik sebagian sebesar **{disp_kur}**.", parse_mode="Markdown")
        
        picks_list_text = await format_picks_list()
        await context.bot.send_message(chat_id=chat_id, text=picks_list_text)
        await check_and_trigger_auto_roll(chat_id, context)

async def process_tf_request(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    user = update.effective_user
    sender_id = user.id
    sender_name = user.full_name

    _, _, _, _, to_target, to_progress = await get_user(sender_id, sender_name)
    sisa_to = max(0.0, to_target - to_progress)
    if sisa_to > 0:
        await update.message.reply_text(f"❌ Sisa TO kamu masih ada ({sisa_to:.2f}). Selesaikan TO terlebih dahulu sebelum melakukan transfer!")
        return

    parts = raw_text.split()
    if len(parts) < 3:
        if update.message.reply_to_message and len(parts) == 2:
            try:
                amount = float(parts[1])
            except ValueError:
                await update.message.reply_text(
                    "❌ Format: /transfer jumlah (reply user) atau /transfer username/ID jumlah\n"
                    "Contoh: /transfer 123456789 1 · min 1 koin"
                )
                return
            
            target_user = update.message.reply_to_message.from_user
            target_id = target_user.id
            target_name = target_user.full_name or "User"
        else:
            await update.message.reply_text(
                "❌ Format: /transfer jumlah (reply user) atau /transfer username/ID jumlah\n"
                "Contoh: /transfer 123456789 1 · min 1 koin"
            )
            return
    else:
        target_identifier = parts[1]
        try:
            amount = float(parts[2])
        except ValueError:
            await update.message.reply_text("❌ Nominal transfer tidak valid!")
            return
        
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, nama FROM users WHERE user_id = ? OR nama LIKE ?", (target_identifier, f"%{target_identifier}%")) as cursor:
                target_row = await cursor.fetchone()
                if not target_row:
                    await update.message.reply_text("❌ Target transfer tidak ditemukan di database.")
                    return
                target_id, target_name = target_row

    if amount <= 0:
        await update.message.reply_text("❌ Nominal transfer harus lebih besar dari 0!")
        return

    if sender_id == target_id:
        await update.message.reply_text("❌ Tidak bisa transfer saldo ke diri sendiri!")
        return

    _, sender_saldo, _, _, _, _ = await get_user(sender_id, sender_name)
    if sender_saldo < amount:
        disp_saldo = f"{sender_saldo:.2f}"
        await update.message.reply_text(
            f"❌ Saldo tidak mencukupi!\n\n"
            f"Saldo Anda: {disp_saldo} koin"
        )
        return

    await get_user(target_id, target_name)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ?", (amount, sender_id))
        await db.execute("UPDATE users SET saldo = saldo + ? WHERE user_id = ?", (amount, target_id))
        
        async with db.execute("SELECT saldo FROM users WHERE user_id = ?", (sender_id,)) as cursor:
            curr_s = await cursor.fetchone()
            if curr_s and curr_s[0] <= 0:
                await db.execute("UPDATE users SET to_target = 0, to_progress = 0 WHERE user_id = ?", (sender_id,))

        await db.commit()

    _, updated_sender_saldo, _, _, _, _ = await get_user(sender_id, sender_name)
    disp_amt = f"{amount:.2f}"
    disp_sisa = f"{updated_sender_saldo:.2f}"
    target_username_display = f"@{target_name}" if not target_name.startswith("@") else target_name

    success_msg = (
        f"✅ Transfer Berhasil\n\n"
        f"Transfer: -{disp_amt} koin ke {target_username_display}\n"
        f"💸 Fee 0% | Diterima: {disp_amt} koin\n"
        f"Saldo: {disp_sisa} koin sisa saldo"
    )
    await update.message.reply_text(success_msg)

async def process_pindah_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    user_id = user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT side, amount FROM player_picks WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await update.message.reply_text("❌ Kamu belum memiliki taruhan.")
                return
            current_side, amount = row

        async with db.execute("SELECT pindah_used FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user_row = await cursor.fetchone()
            pindah_used = user_row[0] if user_row else 0

        if pindah_used == 1:
            await update.message.reply_text("♻️ Jatah pindah game ini sudah dipakai (maks 1x).")
            return

        new_side = 'B' if current_side == 'K' else 'K'
        old_side_name = "Kecil" if current_side == 'K' else "Besar"
        new_side_name = "Besar" if current_side == 'K' else "Kecil"

        if current_side == 'K':
            await db.execute("""
                UPDATE game_room 
                SET players_k = MAX(0, players_k - 1), players_b = players_b + 1 
                WHERE id = 1
            """)
        else:
            await db.execute("""
                UPDATE game_room 
                SET players_k = players_k + 1, players_b = MAX(0, players_b - 1) 
                WHERE id = 1
            """)

        await db.execute("UPDATE player_picks SET side = ? WHERE user_id = ?", (new_side, user_id))
        await db.execute("UPDATE users SET pindah_used = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

    display_amount = f"{amount:.2f}"
    await update.message.reply_text(f"✅ Pindah berhasil: {old_side_name} → {new_side_name} ({display_amount}) ♻️Jatah pindah game ini sudah dipakai (maks 1x).")
    
    picks_list_text = await format_picks_list()
    await context.bot.send_message(chat_id=update.effective_chat.id, text=picks_list_text)
    await check_and_trigger_auto_roll(update.effective_chat.id, context)

async def process_roll_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_roll
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name

    if active_roll["message_id"] and active_roll["user_id"] != user_id:
        await update.message.reply_text(f"⏳ Konfirmasi /roll masih aktif oleh {active_roll['user_name']}, tunggu sebentar.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT SUM(amount) FROM player_picks WHERE side = 'K'") as cursor:
            row_k = await cursor.fetchone()
            total_k = row_k[0] or 0.0

        async with db.execute("SELECT SUM(amount) FROM player_picks WHERE side = 'B'") as cursor:
            row_b = await cursor.fetchone()
            total_b = row_b[0] or 0.0

    if total_k > 0 and total_k == total_b:
        await execute_roll_game(chat_id, context)
        return

    if total_k == 0 and total_b == 0:
        await update.message.reply_text("❌ Belum ada taruhan di meja.")
        return

    diff = abs(total_k - total_b)
    if total_k > total_b:
        lighter_side = 'B'
        lighter_name = "Besar (B)"
    else:
        lighter_side = 'K'
        lighter_name = "Kecil (K)"

    _, saldo, _, _, _, _ = await get_user(user_id, user_name)

    if saldo <= 0 or saldo < diff:
        display_needed = f"{diff:.2f}"
        await update.message.reply_text(f"😒 Saldo kamu kurang dari {display_needed} silahkan isi saldo ke pribadi bot terima kasih.")
        return

    diff_str = f"{diff:.2f}"
    msg_text = (
        f"🎲 Konfirmasi /roll oleh: {user_name}\n\n"
        f"⚖ Meja belum seimbang (Kurang {diff_str} di sisi {lighter_name}).\n"
        f"Aksi ini akan memotong saldo kamu sebesar {diff_str} untuk dipasang ke {lighter_name} agar meja seimbang.\n\n"
        f"👉 Konfirmasi lanjut atau batal."
    )
    keyboard = get_roll_confirmation_keyboard(user_id)
    sent_msg = await update.message.reply_text(msg_text, reply_markup=keyboard)

    async def expire_roll():
        global active_roll
        await asyncio.sleep(15)
        if active_roll["message_id"] == sent_msg.message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_msg.message_id,
                    text="❌ Konfirmasi /roll kedaluwarsa."
                )
            except Exception:
                pass
            active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}

    task = asyncio.create_task(expire_roll())

    active_roll = {
        "user_id": user_id,
        "user_name": user_name,
        "message_id": sent_msg.message_id,
        "chat_id": chat_id,
        "created_at": datetime.now(),
        "side": lighter_side,
        "needed_amount": diff,
        "task": task
    }

async def handle_roll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_roll
    await ensure_db_tables_and_columns()
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    user_id = user.id

    if not data.startswith("confirm_roll_") and not data.startswith("cancel_roll_"):
        return

    target_user_id = int(data.split("_")[-1])

    if user_id != target_user_id:
        await query.answer("❌ Ini bukan konfirmasi roll milikmu!", show_alert=True)
        return

    if active_roll["message_id"] != query.message.message_id:
        await query.edit_message_text("❌ Sesi /roll ini sudah kedaluwarsa atau dibatalkan.")
        return

    if active_roll["task"]:
        active_roll["task"].cancel()

    chat_id = query.message.chat_id

    if data.startswith("cancel_roll_"):
        active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}
        await query.edit_message_text("❌ Konfirmasi /roll dibatalkan.")
        return

    needed = active_roll["needed_amount"]
    lighter_side = active_roll["side"]
    user_name = active_roll["user_name"]

    _, saldo, _, _, _, _ = await get_user(user_id, user_name)
    if saldo < needed:
        active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}
        display_needed = f"{needed:.2f}"
        await query.edit_message_text(f"😒 Saldo kamu kurang dari {display_needed} silahkan isi saldo ke pribadi bot terima kasih.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT side, amount FROM player_picks WHERE user_id = ?", (user_id,)) as cursor:
            existing_pick = await cursor.fetchone()

        if existing_pick:
            existing_side, existing_amount = existing_pick
            if existing_side != lighter_side:
                if existing_side == 'K':
                    await db.execute("UPDATE game_room SET players_k = MAX(0, players_k - 1), players_b = players_b + 1 WHERE id = 1")
                else:
                    await db.execute("UPDATE game_room SET players_k = players_k + 1, players_b = MAX(0, players_b - 1) WHERE id = 1")
                await db.execute("UPDATE player_picks SET side = ?, amount = amount + ? WHERE user_id = ?", (lighter_side, needed, user_id))
            else:
                await db.execute("UPDATE player_picks SET amount = amount + ? WHERE user_id = ?", (needed, user_id))
        else:
            await db.execute("INSERT INTO player_picks (user_id, side, amount) VALUES (?, ?, ?)", (user_id, lighter_side, needed))
            if lighter_side == 'B':
                await db.execute("UPDATE game_room SET players_b = players_b + 1 WHERE id = 1")
            else:
                await db.execute("UPDATE game_room SET players_k = players_k + 1 WHERE id = 1")

        await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ?", (needed, user_id))
        await db.commit()

    active_roll = {"user_id": None, "user_name": None, "message_id": None, "chat_id": None, "created_at": None, "side": None, "needed_amount": 0, "task": None}
    
    try:
        await query.message.delete()
    except Exception:
        pass

    picks_list_text = await format_picks_list()
    await context.bot.send_message(chat_id=chat_id, text=picks_list_text)
    await execute_roll_game(chat_id, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Halo! Bot game siap digunakan.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Daftar perintah: /saldo, /to, /transfer, /geseran, /angpao, /apgb, /kur, /ws, /lb, /history, /setfee, /cashback, /claimcb, /buytitle, /reqtitle, /deposit, /tarik, /wd")

# ==================== FITUR DEPOSIT DENGAN QRISLY (ENDPOINT PERBAIKAN) ====================

async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_private_chat(update):
        return
    await ensure_db_tables_and_columns()

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format salah.\nGunakan: `/deposit 10` atau `deposit 10`", parse_mode="Markdown")
        return

    try:
        coins = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Nominal koin harus berupa bilangan bulat positif!")
        return

    if coins <= 0:
        await update.message.reply_text("❌ Nominal deposit minimal 1 koin!")
        return

    rupiah = coins * 1000
    user = update.effective_user
    user_id = user.id

    now_wib = datetime.now(WIB)
    expiry_wib = now_wib + timedelta(minutes=15)
    expiry_str = expiry_wib.strftime("%H:%M WIB")

    confirm_token = uuid.uuid4().hex[:12]
    PENDING_CONFIRMATIONS[f"depo_{confirm_token}"] = {
        "user_id": user_id,
        "coins": coins,
        "rupiah": rupiah,
        "expires_at": expiry_wib.isoformat()
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Konfirmasi", callback_data=f"conf_depo_yes_{confirm_token}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"conf_depo_no_{confirm_token}")
        ]
    ])

    msg_text = (
        f"💳 **Konfirmasi Deposit**\n\n"
        f"Detail Deposit:\n"
        f"- Jumlah: {coins} koin\n"
        f"- Nilai: {rupiah:,} rupiah\n\n"
        f"Konversi:\n"
        f"- 1 koin = 1.000 rupiah\n"
        f"- {coins} koin = {rupiah:,} rupiah\n\n"
        f"Ketentuan:\n"
        f"- QRIS akan berlaku selama 15 menit (sampai {expiry_str})\n"
        f"- Transfer harus sesuai nominal tagihan\n"
        f"- Saldo akan ditambahkan otomatis setelah pembayaran berhasil\n\n"
        f"Konfirmasi untuk melanjutkan?"
    )
    await update.message.reply_text(msg_text, reply_markup=keyboard, parse_mode="Markdown")

async def handle_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id

    if data.startswith("conf_depo_no_"):
        token = data.replace("conf_depo_no_", "")
        key = f"depo_{token}"
        PENDING_CONFIRMATIONS.pop(key, None)
        await query.answer("Deposit dibatalkan.")
        await query.edit_message_text(
            "❌ **Deposit Dibatalkan**\n\nKonfirmasi deposit telah dibatalkan.\nTidak ada saldo yang berubah dan tidak ada QRIS yang dibuat.",
            parse_mode="Markdown"
        )
        return

    if data.startswith("conf_depo_yes_"):
        token = data.replace("conf_depo_yes_", "")
        key = f"depo_{token}"
        conf = PENDING_CONFIRMATIONS.get(key)

        if not conf:
            await query.answer("❌ Sesi konfirmasi kedaluwarsa atau tidak valid!", show_alert=True)
            return

        if user_id != conf["user_id"]:
            await query.answer("❌ Hanya pemilik permintaan yang boleh menggunakan tombol ini!", show_alert=True)
            return

        PENDING_CONFIRMATIONS.pop(key, None)
        await query.answer("Membuat tagihan QRIS dari QRISly...")

        coins = conf["coins"]
        rupiah = conf["rupiah"]
        tx_id = f"TXD-{uuid.uuid4().hex[:10].upper()}"
        now_wib = datetime.now(WIB)
        expiry_wib = now_wib + timedelta(minutes=15)
        expiry_str = expiry_wib.strftime("%H:%M WIB")

        if not QRISLY_API_KEY:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ API key QRISly belum dikonfigurasi. "
                    "Atur environment variable QRISLY_API_KEY lalu mulai ulang bot. "
                    "Tidak ada pembayaran yang dibuat."
                )
            )
            return

        headers = {
            "X-API-Key": QRISLY_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Payload request disesuaikan dengan QRIS ID dan Webhook URL
        payload = {
            "qris_id": int(QRISLY_QRIS_ID),
            "amount": int(rupiah),
            "output_type": "string",
            "unique_amount": True,
            "webhook_url": QRISLY_WEBHOOK_URL
        }

        qris_string = None
        gateway_tx_id = tx_id
        final_rupiah = rupiah
        error_debug_msg = None

        try:
            async with aiohttp.ClientSession() as session:
                # Endpoint API yang benar: https://api.collaborator.komerce.id/user/api/v1/qrisly/generate-qris
                async with session.post(
                    f"{QRISLY_BASE_URL}/api/v1/qrisly/generate-qris",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    raw_t = await resp.text()
                    if resp.status < 200 or resp.status >= 300:
                        error_debug_msg = f"HTTP {resp.status} dari QRISly: {raw_t[:250]}"
                    else:
                        try:
                            res_data = json.loads(raw_t)
                            if isinstance(res_data, dict):
                                data_obj = res_data.get("data") or {}
                                if not isinstance(data_obj, dict):
                                    data_obj = {}
                                success = res_data.get("success")
                                qris_string = (
                                    data_obj.get("qris_string") or data_obj.get("qris")
                                    or data_obj.get("qr_string") or data_obj.get("qr_content")
                                    or res_data.get("qris_string") or res_data.get("qris")
                                    or res_data.get("qr_string") or res_data.get("qr_content")
                                )
                                gateway_tx_id = str(data_obj.get("history_id") or data_obj.get("transaction_id") or tx_id)
                                final_rupiah = data_obj.get("final_amount") or data_obj.get("amount") or rupiah
                                if success is False or not qris_string:
                                    error_debug_msg = str(res_data.get("message") or res_data.get("msg") or "Respons API tidak berisi qris_string.")
                            else:
                                error_debug_msg = "Format respons QRISly tidak sesuai (bukan objek JSON)."
                        except json.JSONDecodeError:
                            error_debug_msg = f"Respons QRISly bukan JSON (HTTP {resp.status}): {raw_t[:180]}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"Gagal menghubungi API QRISly: {type(e).__name__}: {e}")
            error_debug_msg = f"Koneksi ke QRISly gagal ({type(e).__name__}). Periksa domain/API dan koneksi server."
        except Exception as e:
            print(f"Gagal memproses respons QRISly: {type(e).__name__}: {e}")
            error_debug_msg = f"Respons QRISly gagal diproses ({type(e).__name__})."

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                INSERT INTO transactions (tx_id, user_id, type, amount_coins, amount_idr, status, gateway_tx_id, created_at, expires_at)
                VALUES (?, ?, 'deposit', ?, ?, 'pending', ?, ?, ?)
            """, (tx_id, user_id, coins, final_rupiah, gateway_tx_id, now_wib.strftime("%Y-%m-%d %H:%M:%S"), expiry_wib.strftime("%Y-%m-%d %H:%M:%S")))
            await db.commit()

        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batalkan Deposit", callback_data=f"cancel_qris_{tx_id}")]
        ])

        qris_display_msg = (
            f"💳 **QRIS Deposit**\n\n"
            f"Transaction ID: `{tx_id}`\n"
            f"Nominal: {final_rupiah:,} rupiah ({coins} koin)\n\n"
            f"Cara deposit:\n"
            f"1. Scan QR Code di atas.\n"
            f"2. Transfer TEPAT sampai digit terakhir: {final_rupiah:,} rupiah.\n"
            f"3. Saldo akan otomatis ditambahkan setelah transfer berhasil.\n\n"
            f"Catatan:\n"
            f"- Nominal harus sama persis dengan tagihan QRIS.\n"
            f"- QRIS berlaku selama 15 menit (sampai {expiry_str}).\n"
            f"- Jika pembayaran berhasil tetapi saldo belum masuk, hubungi admin untuk pemeriksaan transaksi."
        )

        try:
            await query.message.delete()
        except Exception:
            pass

        if qris_string:
            qr = qrcode.QRCode(version=1, box_size=10, border=2)
            qr.add_data(qris_string)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            bio = BytesIO()
            img.save(bio, format="PNG")
            bio.seek(0)

            await context.bot.send_photo(
                chat_id=user_id,
                photo=bio,
                caption=qris_display_msg,
                reply_markup=cancel_kb,
                parse_mode="Markdown"
            )
        else:
            err_info = f"\n\n❌ *Gagal men-generate gambar QRIS dari API.*"
            if error_debug_msg:
                safe_error = str(error_debug_msg).replace("`", "'").replace("\n", " ")[:220]
                err_info += f"\n*Detail Error:* `{safe_error}`"

            await context.bot.send_message(
                chat_id=user_id,
                text=qris_display_msg + err_info,
                reply_markup=cancel_kb,
                parse_mode="Markdown"
            )

async def handle_cancel_qris_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id

    if data.startswith("cancel_qris_"):
        tx_id = data.replace("cancel_qris_", "")
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, amount_coins, amount_idr, status FROM transactions WHERE tx_id = ?", (tx_id,)) as cursor:
                row = await cursor.fetchone()
            
            if not row:
                await query.answer("❌ Transaksi tidak ditemukan!", show_alert=True)
                return

            db_user_id, coins, rupiah, status = row
            if user_id != db_user_id:
                await query.answer("❌ Hanya pemilik transaksi yang dapat membatalkan!", show_alert=True)
                return

            if status == 'success':
                await query.answer("❌ Pembayaran sudah berhasil diproses, tidak dapat dibatalkan!", show_alert=True)
                return

            await db.execute("UPDATE transactions SET status = 'cancelled' WHERE tx_id = ?", (tx_id,))
            await db.commit()

        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ **Deposit Dibatalkan**\n\n"
                 f"Transaction ID: `{tx_id}`\n"
                 f"Nominal: {rupiah:,} rupiah ({coins} koin)\n"
                 f"Status: 🚫 DIBATALKAN\n\n"
                 f"Deposit Anda telah dibatalkan.\n"
                 f"Sudah terlanjur transfer sebelum membatalkan? Uangnya tidak hangus — pembayaran yang sudah masuk tetap dikreditkan otomatis jika pembayaran tersebut terverifikasi sah. Jangan transfer ulang ke QR ini.\n\n"
                 f"Anda bisa membuat deposit baru dengan command:\n`/deposit jumlah`",
            parse_mode="Markdown"
        )

# ==================== FITUR TARIK ====================

async def tarik_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_private_chat(update):
        return
    await ensure_db_tables_and_columns()

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format salah.\nGunakan: `/tarik 11`", parse_mode="Markdown")
        return

    raw_arg = args[0]
    if not re.match(r"^\d+$", raw_arg):
        await update.message.reply_text("❌ Fitur tarik hanya menerima nominal bilangan bulat positif (contoh: `/tarik 11`). Angka desimal atau format tidak valid ditolak.", parse_mode="Markdown")
        return

    amount = int(raw_arg)
    if amount < 11:
        await update.message.reply_text("❌ Minimal penarikan /tarik adalah 11 koin.")
        return

    user = update.effective_user
    user_id = user.id
    user_name = user.full_name

    _, saldo, _, _, to_target, to_progress = await get_user(user_id, user_name)
    sisa_to = max(0.0, to_target - to_progress)
    if sisa_to > 0:
        await update.message.reply_text(f"❌ Sisa TO kamu masih ada ({sisa_to:.2f}). Selesaikan TO terlebih dahulu sebelum melakukan penarikan!")
        return

    if saldo < amount:
        await update.message.reply_text(f"❌ Saldo tidak mencukupi!\nSaldo kamu: {saldo:.2f} koin | Diminta: {amount} koin")
        return

    tx_id = f"TXT-{uuid.uuid4().hex[:10].upper()}"
    now_wib = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ? AND saldo >= ?", (amount, user_id, amount))
        await db.execute("""
            INSERT INTO transactions (tx_id, user_id, type, amount_coins, amount_idr, status, created_at)
            VALUES (?, ?, 'tarik', ?, ?, 'pending', ?)
        """, (tx_id, user_id, amount, amount * 1000, now_wib))
        await db.commit()

    await update.message.reply_text(
        f"✅ **Permintaan Penarikan Diproses**\n\n"
        f"Transaction ID: `{tx_id}`\n"
        f"Jumlah: {amount} koin ({amount * 1000:,} rupiah)\n"
        f"Status: ⏳ PENDING (Menunggu pemrosesan pencairan)\n\n"
        f"Saldo Anda telah ditahan secara aman.",
        parse_mode="Markdown"
    )

# ==================== FITUR WD ====================

async def wd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_private_chat(update):
        return
    await ensure_db_tables_and_columns()

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format salah.\nGunakan: `/wd 11.2`", parse_mode="Markdown")
        return

    raw_arg = args[0]
    if not re.match(r"^\d+(\.\d+)?$", raw_arg):
        await update.message.reply_text("❌ Format nominal WD tidak valid. Gunakan angka desimal dengan titik (contoh: `/wd 11.2`).", parse_mode="Markdown")
        return

    try:
        amount = float(raw_arg)
    except ValueError:
        await update.message.reply_text("❌ Nominal WD tidak valid.")
        return

    if amount <= 11:
        await update.message.reply_text("❌ Nominal WD harus lebih besar dari 11 koin (contoh: `/wd 11.2`).")
        return

    user = update.effective_user
    user_id = user.id
    user_name = user.full_name

    _, saldo, _, _, to_target, to_progress = await get_user(user_id, user_name)
    sisa_to = max(0.0, to_target - to_progress)
    if sisa_to > 0:
        await update.message.reply_text(f"❌ Sisa TO kamu masih ada ({sisa_to:.2f}). Selesaikan TO terlebih dahulu sebelum melakukan WD!")
        return

    if saldo < amount:
        await update.message.reply_text(f"❌ Saldo tidak mencukupi!\nSaldo kamu: {saldo:.2f} koin | Diminta: {amount:.2f} koin")
        return

    tx_id = f"TXW-{uuid.uuid4().hex[:10].upper()}"
    now_wib = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET saldo = saldo - ? WHERE user_id = ? AND saldo >= ?", (amount, user_id, amount))
        await db.execute("""
            INSERT INTO transactions (tx_id, user_id, type, amount_coins, amount_idr, status, created_at)
            VALUES (?, ?, 'wd', ?, ?, 'pending', ?)
        """, (tx_id, user_id, amount, amount * 1000, now_wib))
        await db.commit()

    await update.message.reply_text(
        f"✅ **Permintaan WD Diproses**\n\n"
        f"Transaction ID: `{tx_id}`\n"
        f"Jumlah: {amount:.2f} koin\n"
        f"Status: ⏳ PENDING (Menunggu verifikasi payout)\n\n"
        f"Saldo Anda telah ditahan secara aman.",
        parse_mode="Markdown"
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data.startswith("conf_depo_"):
        await handle_deposit_callback(update, context)
    elif query.data.startswith("cancel_qris_"):
        await handle_cancel_qris_callback(update, context)
    elif query.data.startswith("confirm_buytitle_") or query.data.startswith("cancel_buytitle_"):
        await handle_buytitle_confirm_callback(update, context)
    elif query.data.startswith("confirm_roll_") or query.data.startswith("cancel_roll_"):
        await handle_roll_callback(update, context)
    elif query.data.startswith("confirm_angpao_") or query.data.startswith("cancel_angpao_"):
        await handle_angpao_confirm_callback(update, context)
    elif query.data.startswith("confirm_geseran_") or query.data.startswith("cancel_geseran_"):
        await handle_geseran_confirm_callback(update, context)
    elif query.data.startswith("claim_ws_"):
        await handle_claim_ws_callback(update, context)
    elif query.data.startswith("claim_"):
        await handle_angpao_callback(update, context)
    else:
        await query.answer()

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    user_id = user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT created_at, side, amount, result 
            FROM user_picks_history 
            WHERE user_id = ? 
            ORDER BY id DESC 
            LIMIT 10
        """, (user_id,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("🕘 Belum ada riwayat taruhan untuk akun kamu.")
        return

    msg = "🕘 10 pick terakhir kamu:\n\n"
    for created_at, side, amount, result in rows:
        if result == 'W':
            res_str = "✅ W"
        elif result == 'L':
            res_str = "❌ L"
        else:
            res_str = "↩️ reff"

        msg += f"• {created_at} · {side} · {amount:.2f} · {res_str}\n"

    await update.message.reply_text(msg)

async def cashback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    user_id = user.id

    now_wib = datetime.now(WIB)
    today_str = now_wib.strftime("%Y-%m-%d")

    cfg = load_config()
    cashback_percent = cfg.get("cashback_percent", 2.0)

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT cb_last_claimed_date, cb_last_claimed_at FROM users WHERE user_id = ?", 
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            last_claimed_date = row[0] if row else None
            last_claimed_at = row[1] if row else None

    if last_claimed_date == today_str:
        async with aiosqlite.connect(DB_NAME) as db:
            if last_claimed_at:
                query_history = """
                    SELECT side, amount, result 
                    FROM user_picks_history 
                    WHERE user_id = ? AND created_at > ?
                """
                params = (user_id, last_claimed_at)
            else:
                query_history = """
                    SELECT side, amount, result 
                    FROM user_picks_history 
                    WHERE user_id = ?
                """
                params = (user_id,)

            async with db.execute(query_history, params) as cursor:
                history_after_claim = await cursor.fetchall()

        total_bet_setelah = 0.0
        total_win_setelah = 0.0

        for _, amt, res in history_after_claim:
            if res in ['W', 'L']:
                total_bet_setelah += amt
            if res == 'W':
                total_win_setelah += (amt * 2)

        rugi_besok = total_bet_setelah - total_win_setelah

        msg = (
            "💸 Cashback kamu\n"
            "Balik modal dari taruhan yang kalah.\n"
            "✅ Hari ini sudah kamu ambil. Besok lagi jam 18.00–23.00 WIB."
        )

        if rugi_besok >= 50.0:
            koin_besok = rugi_besok * (cashback_percent / 100.0)
            msg += f"\n⏭️ Buat besok: {koin_besok:.2f} koin."

        await update.message.reply_text(msg)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT side, amount, result 
            FROM user_picks_history 
            WHERE user_id = ? AND created_at LIKE ?
        """, (user_id, f"%{now_wib.strftime('%d/%m')}%")) as cursor:
            today_history = await cursor.fetchall()

    total_bet = 0.0
    total_win = 0.0

    for _, amt, res in today_history:
        if res in ['W', 'L']:
            total_bet += amt
        if res == 'W':
            total_win += (amt * 2)

    total_rugi = total_bet - total_win

    if total_rugi < 50.0:
        msg = (
            "💸 Cashback kamu\n"
            "Balik modal dari taruhan yang kalah.\n"
            "Belum ada yang bisa diambil hari ini. Main dulu ya."
        )
    else:
        jumlah_cb = total_rugi * (cashback_percent / 100.0)
        msg = (
            f"💸 Cashback kamu: {jumlah_cb:.2f} koin.\n"
            f"Ketik claimcb jam 18.00-23.00 WIB untuk ambil."
        )

    await update.message.reply_text(msg)

async def claimcb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_db_tables_and_columns()
    user = update.effective_user
    user_id = user.id

    now_wib = datetime.now(WIB)
    today_str = now_wib.strftime("%Y-%m-%d")
    current_hour = now_wib.hour

    if not (18 <= current_hour < 23):
        await update.message.reply_text(
            "⏰ Klaim cashback hanya bisa dilakukan jam 18.00–23.00 WIB!"
        )
        return

    cfg = load_config()
    cashback_percent = cfg.get("cashback_percent", 2.0)

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT cb_last_claimed_date FROM users WHERE user_id = ?", 
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            last_claimed_date = row[0] if row else None

    if last_claimed_date == today_str:
        await update.message.reply_text(
            "💸 Cashback kamu\n"
            "Balik modal dari taruhan yang kalah.\n"
            "✅ Hari ini sudah kamu ambil. Besok lagi jam 18.00–23.00 WIB."
        )
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT side, amount, result 
            FROM user_picks_history 
            WHERE user_id = ? AND created_at LIKE ?
        """, (user_id, f"%{now_wib.strftime('%d/%m')}%")) as cursor:
            today_history = await cursor.fetchall()

    total_bet = 0.0
    total_win = 0.0

    for _, amt, res in today_history:
        if res in ['W', 'L']:
            total_bet += amt
        if res == 'W':
            total_win += (amt * 2)

    total_rugi = total_bet - total_win

    if total_rugi < 50.0:
        await update.message.reply_text(
            "❌ Kamu belum memenuhi syarat kekalahan minimal untuk klaim cashback hari ini."
        )
        return

    jumlah_cb = total_rugi * (cashback_percent / 100.0)
    now_ts_str = now_wib.strftime("%d/%m %H:%M WIB")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users 
            SET saldo = saldo + ?, 
                to_target = to_target + ?,
                cb_last_claimed_date = ?,
                cb_last_claimed_at = ?
            WHERE user_id = ?
        """, (jumlah_cb, jumlah_cb, today_str, now_ts_str, user_id))
        await db.commit()

    success_msg = (
        f"✅ Cashback dari taruhan kalah masuk: +{jumlah_cb:.2f} koin.\n"
        f"Koin ini harus dimainkan dulu (TO), belum bisa langsung ditarik.\n"
        f"Besok bisa ambil lagi jam 18.00–23.00 WIB."
    )
    await update.message.reply_text(success_msg)

async def reqtitle_command_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reqtitle_command(update, context)

async def buytitle_command_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await buy_title_command(update, context)

async def pindah_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_pindah_request(update, context)

async def kur_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_kur_request(update, context, update.message.text)

async def tf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_tf_request(update, context, update.message.text)

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_saldo_request(update, context)

async def geseran_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_geseran_request(update, context, update.message.text)

async def angpao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_angpao_request(update, context, update.message.text, is_apgb=False)

async def apgb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_angpao_request(update, context, update.message.text, is_apgb=True)

async def ws_dana_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_ws_dana_request(update, context)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text.strip().lower()
    user = update.effective_user
    user_id = user.id
    user_name = user.full_name
    chat_id = update.effective_chat.id

    if raw_text.startswith('setfee ') or raw_text.startswith('/setfee ') or raw_text in ['setfee', '/setfee']:
        await setfee_command(update, context)
        return

    if raw_text in ['cashback', '/cashback']:
        await cashback_command(update, context)
        return

    if raw_text in ['claimcb', '/claimcb']:
        await claimcb_command(update, context)
        return

    if raw_text in ['saldo', '/saldo']:
        await process_saldo_request(update, context)
        return

    if raw_text in ['lb', '/lb', 'leaderboard', '/leaderboard']:
        await leaderboard_command(update, context)
        return

    if raw_text in ['ws', '/ws']:
        await process_win_streak_status(update, context)
        return

    if raw_text in ['ws dana', '/wsdana']:
        await process_ws_dana_request(update, context)
        return

    if raw_text.startswith('tarik') or raw_text.startswith('/tarik'):
        parts = raw_text.split()
        context.args = parts[1:]
        await tarik_command(update, context)
        return

    if raw_text.startswith('wd') or raw_text.startswith('/wd'):
        parts = raw_text.split()
        context.args = parts[1:]
        await wd_command(update, context)
        return

    if raw_text.startswith('deposit') or raw_text.startswith('/deposit') or raw_text.startswith('depo') or raw_text.startswith('/depo'):
        parts = raw_text.split()
        if len(parts) >= 2:
            context.args = parts[1:]
            await deposit_command(update, context)
            return

    if raw_text in ['history', '/history', 'lastwin', '/lastwin']:
        await history_command(update, context)
        return

    if raw_text in ['buytitle', '/buytitle'] or raw_text.startswith('buytitle ') or raw_text.startswith('/buytitle '):
        context.args = raw_text.split()[1:]
        await buy_title_command(update, context)
        return

    if raw_text in ['reqtitle', '/reqtitle'] or raw_text.startswith('reqtitle ') or raw_text.startswith('/reqtitle '):
        context.args = raw_text.split()[1:]
        await reqtitle_command(update, context)
        return

    if raw_text in ['to', '/to']:
        await process_to_request(update, context)
        return

    if raw_text.startswith('tf ') or raw_text.startswith('/tf '):
        await process_tf_request(update, context, raw_text)
        return

    if raw_text.startswith('geseran ') or raw_text.startswith('/geseran '):
        await process_geseran_request(update, context, raw_text)
        return

    if raw_text.startswith('angpao ') or raw_text.startswith('/angpao '):
        await process_angpao_request(update, context, raw_text, is_apgb=False)
        return

    if raw_text.startswith('apgb ') or raw_text.startswith('/apgb '):
        await process_angpao_request(update, context, raw_text, is_apgb=True)
        return

    await check_and_claim_geseran(update, context)

    match = re.match(r'^(k|b|kecil|besar)\s*(\d+(?:\.\d+)?|all)$', raw_text)
    if match:
        side_input = match.group(1)
        amount_input = match.group(2)
        
        side_code = 'K' if side_input in ['k', 'kecil'] else 'B'
        _, saldo, _, _, to_target, to_progress = await get_user(user_id, user_name)

        if amount_input == 'all':
            amount = saldo
        else:
            try:
                amount = float(amount_input)
            except ValueError:
                return

        if amount <= 0 or amount > saldo:
            await update.message.reply_text("❌ Saldo tidak mencukupi!")
            return

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT side, amount FROM player_picks WHERE user_id = ?", (user_id,)) as cursor:
                existing_pick = await cursor.fetchone()

            if existing_pick:
                existing_side, existing_amount = existing_pick
                if existing_side != side_code:
                    await update.message.reply_text("❌ Kamu sudah memasang di sisi lain! Gunakan `pindah` jika ingin berpindah sisi.")
                    return
                
                new_amount = existing_amount + amount
                await db.execute("UPDATE users SET saldo = saldo - ?, nama = ? WHERE user_id = ?", (amount, user_name, user_id))
                await db.execute("UPDATE player_picks SET amount = ? WHERE user_id = ?", (new_amount, user_id))
            else:
                await db.execute("UPDATE users SET saldo = saldo - ?, nama = ? WHERE user_id = ?", (amount, user_name, user_id))
                await db.execute("INSERT INTO player_picks (user_id, side, amount) VALUES (?, ?, ?)", (user_id, side_code, amount))
                
                if side_code == 'B':
                    await db.execute("UPDATE game_room SET players_b = players_b + 1 WHERE id = 1")
                else:
                    await db.execute("UPDATE game_room SET players_k = players_k + 1 WHERE id = 1")

            await db.commit()

        display_amount = f"{amount:.2f}"
        await update.message.reply_text(f"✅Bet diterima: {side_input} {display_amount}")
        
        picks_list_text = await format_picks_list()
        await context.bot.send_message(chat_id=chat_id, text=picks_list_text)

        await check_and_trigger_auto_roll(chat_id, context)
        return

    if raw_text.startswith('kur') or raw_text.startswith('/kur'):
        await process_kur_request(update, context, raw_text)
        return

    if raw_text in ['pindah', '/pindah']:
        await process_pindah_request(update, context)
        return

    if raw_text in ['r', 'roll', '/r', '/roll']:
        await process_roll_request(update, context)
        return