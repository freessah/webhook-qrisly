import aiosqlite

DB_NAME = "bot_database.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Tabel Users
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                nama TEXT,
                saldo REAL DEFAULT 0,
                to_target REAL DEFAULT 0,
                to_progress REAL DEFAULT 0,
                role TEXT DEFAULT 'player',
                pindah_used INTEGER DEFAULT 0,
                win_streak INTEGER DEFAULT 0,
                ws_claim_status INTEGER DEFAULT 0
            )
        """)

        # Migrasi Otomatis untuk tabel users
        try:
            await db.execute("ALTER TABLE users ADD COLUMN to_target REAL DEFAULT 0")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE users ADD COLUMN to_progress REAL DEFAULT 0")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE users ADD COLUMN pindah_used INTEGER DEFAULT 0")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE users ADD COLUMN win_streak INTEGER DEFAULT 0")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE users ADD COLUMN ws_claim_status INTEGER DEFAULT 0")
        except Exception:
            pass
        
        # 2. Tabel Player Picks
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_picks (
                user_id INTEGER PRIMARY KEY,
                side TEXT,
                amount REAL
            )
        """)

        # 3. Tabel Game Room Status
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_room (
                id INTEGER PRIMARY KEY DEFAULT 1,
                match_number INTEGER DEFAULT 1,
                last_win_msg_id INTEGER DEFAULT NULL,
                players_k INTEGER DEFAULT 0,
                players_b INTEGER DEFAULT 0
            )
        """)

        try:
            await db.execute("ALTER TABLE game_room ADD COLUMN match_number INTEGER DEFAULT 1")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE game_room ADD COLUMN last_win_msg_id INTEGER DEFAULT NULL")
        except Exception:
            pass

        await db.execute("""
            INSERT OR IGNORE INTO game_room (id, match_number, last_win_msg_id) 
            VALUES (1, 1, NULL)
        """)

        # 4. Tabel Game History Global
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_history (
                game_number INTEGER PRIMARY KEY,
                side TEXT,
                dice_value INTEGER,
                bl_amount REAL
            )
        """)

        # 5. Tabel User History (Riwayat taruhan pribadi pemain)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                side TEXT,
                amount REAL,
                status TEXT,
                timestamp TEXT
            )
        """)

        await db.commit()

async def get_user(user_id: int, nama: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, saldo, role, nama, to_target, to_progress FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
            if not user:
                await db.execute("""
                    INSERT INTO users (user_id, nama, saldo, to_target, to_progress, win_streak, ws_claim_status, pindah_used) 
                    VALUES (?, ?, 0, 0, 0, 0, 0, 0)
                """, (user_id, nama))
                await db.commit()
                return user_id, 0.0, 'player', nama, 0.0, 0.0
            return user