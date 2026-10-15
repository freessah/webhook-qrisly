import asyncio
import logging
from datetime import time, timezone, timedelta
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from database import init_db
from handlers import (
    start_command, 
    help_command, 
    handle_callback_query, 
    handle_text_message,
    history_command, 
    leaderboard_command, 
    cashback_command, 
    reqtitle_command, 
    pindah_command, 
    kur_command, 
    saldo_command,
    setfee_command,
    deposit_command,
    tarik_command,
    wd_command,
    update_leaderboard_channel,
    process_daily_reward,
    auto_check_pending_deposits
)

TOKEN = "8804318321:AAGwaRnAsniK8wGNMeq5X5frq-9NVEApuSU"
WIB = timezone(timedelta(hours=7))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

async def main():
    await init_db()

    app = Application.builder().token(TOKEN).build()

    # Perintah /slash
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("lb", leaderboard_command))
    app.add_handler(CommandHandler("1b", leaderboard_command))
    app.add_handler(CommandHandler("claimcb", cashback_command))
    app.add_handler(CommandHandler("cashback", cashback_command))
    app.add_handler(CommandHandler("reqtitle", reqtitle_command))
    app.add_handler(CommandHandler("pindah", pindah_command))
    app.add_handler(CommandHandler("kur", kur_command))
    app.add_handler(CommandHandler("reff", kur_command))
    app.add_handler(CommandHandler("saldo", saldo_command))
    app.add_handler(CommandHandler("setfee", setfee_command))

    # Pendaftaran Perintah Baru: deposit, tarik, wd
    app.add_handler(CommandHandler("deposit", deposit_command))
    app.add_handler(CommandHandler("depo", deposit_command))
    app.add_handler(CommandHandler("tarik", tarik_command))
    app.add_handler(CommandHandler("wd", wd_command))

    # MessageHandler membaca pesan biasa
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

    # --- SETUP JOB QUEUE ---
    
    # 1. Kirim LB langsung 1 detik setelah bot start (first=1), lalu ulangi tiap 30 menit (1800 detik)
    app.job_queue.run_repeating(
        update_leaderboard_channel,
        interval=1800,
        first=1
    )

    # 2. Kirim & Bagi Reward Otomatis Setiap Jam 09:00 WIB
    app.job_queue.run_daily(
        process_daily_reward,
        time=time(hour=9, minute=0, second=0, tzinfo=WIB)
    )

    # 3. Pengecekan Deposit Otomatis setiap 10 detik
    app.job_queue.run_repeating(
        auto_check_pending_deposits,
        interval=10,
        first=5
    )

    print("Bot berhasil berjalan!")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())