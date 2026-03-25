import os
import asyncio
import sqlite3
import random
import logging
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")
GAME_COST = 3

WAITING_NICK = 1
(
    ADMIN_SEARCH_INPUT,
    ADMIN_GIVE_USER,
    ADMIN_GIVE_AMOUNT,
    ADMIN_TAKE_USER,
    ADMIN_TAKE_AMOUNT,
    ADMIN_HISTORY_USER,
    ADMIN_BROADCAST,
    ADMIN_ADD_ADMIN,
    ADMIN_PROMO_CODE,
    ADMIN_PROMO_TYPE,
    ADMIN_PROMO_AMOUNT,
    ADMIN_PROMO_MAX,
    ADMIN_GIVE_STARS_USER,
    ADMIN_GIVE_STARS_AMOUNT,
    ADMIN_TAKE_STARS_USER,
    ADMIN_TAKE_STARS_AMOUNT,
    ADMIN_DEACT_PROMO,
    ADMIN_BATTLE_CHANNEL,
) = range(10, 28)

NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]


# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY,
            tokens  INTEGER DEFAULT 0,
            stars   INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'tokens',
            type        TEXT    NOT NULL,
            description TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id  INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS promo_codes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            code            TEXT UNIQUE NOT NULL,
            reward_type     TEXT NOT NULL DEFAULT 'tokens',
            reward_amount   INTEGER NOT NULL,
            max_activations INTEGER NOT NULL,
            activations     INTEGER DEFAULT 0,
            created_by      INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            active          INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS promo_uses (
            promo_id INTEGER,
            user_id  INTEGER,
            used_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (promo_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS battle_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS battle_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER UNIQUE,
            nickname      TEXT NOT NULL,
            votes         INTEGER DEFAULT 0,
            registered_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS battle_rounds (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            round_num     INTEGER,
            nick1_user_id INTEGER,
            nick1         TEXT,
            nick1_votes   INTEGER DEFAULT 0,
            nick2_user_id INTEGER,
            nick2         TEXT,
            nick2_votes   INTEGER DEFAULT 0,
            channel_id    INTEGER,
            message_id    INTEGER,
            posted_at     TEXT DEFAULT (datetime('now')),
            finished      INTEGER DEFAULT 0
        );
    """)
    # Migrations for older DBs
    for stmt in [
        "ALTER TABLE balances ADD COLUMN stars INTEGER DEFAULT 0",
        "ALTER TABLE transactions ADD COLUMN currency TEXT NOT NULL DEFAULT 'tokens'",
    ]:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:
            pass
    conn.commit()
    conn.close()


# ── User helpers ─────────────────────────────────────────────

def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO users (user_id, username, full_name) VALUES (?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username, full_name=excluded.full_name, last_seen=datetime('now')""",
        (user_id, username, full_name),
    )
    conn.commit()
    conn.close()


def get_balance(user_id: int) -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT tokens, stars FROM balances WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (0, 0)


def get_tokens(user_id: int) -> int:
    return get_balance(user_id)[0]


def get_stars(user_id: int) -> int:
    return get_balance(user_id)[1]


def _add_currency(user_id: int, amount: int, currency: str, tx_type: str, description: str) -> int:
    col = "tokens" if currency == "tokens" else "stars"
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO balances (user_id, tokens, stars) VALUES (?,0,0)", (user_id,))
    conn.execute(f"UPDATE balances SET {col}=MAX(0,{col}+?) WHERE user_id=?", (amount, user_id))
    conn.execute(
        "INSERT INTO transactions (user_id,amount,currency,type,description) VALUES (?,?,?,?,?)",
        (user_id, amount, currency, tx_type, description),
    )
    conn.commit()
    new_val = conn.execute(f"SELECT {col} FROM balances WHERE user_id=?", (user_id,)).fetchone()[0]
    conn.close()
    return new_val


def add_tokens(user_id: int, amount: int, tx_type: str, description: str) -> int:
    return _add_currency(user_id, amount, "tokens", tx_type, description)


def add_stars(user_id: int, amount: int, tx_type: str, description: str) -> int:
    return _add_currency(user_id, amount, "stars", tx_type, description)


def get_user_by_username(username: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT u.user_id, u.username, u.full_name, u.last_seen,
                  COALESCE(b.tokens,0), COALESCE(b.stars,0)
           FROM users u LEFT JOIN balances b ON u.user_id=b.user_id
           WHERE LOWER(u.username)=LOWER(?)""",
        (username.lstrip("@"),),
    ).fetchone()
    conn.close()
    return _row_to_user(row)


def get_user_by_id(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT u.user_id, u.username, u.full_name, u.last_seen,
                  COALESCE(b.tokens,0), COALESCE(b.stars,0)
           FROM users u LEFT JOIN balances b ON u.user_id=b.user_id
           WHERE u.user_id=?""",
        (user_id,),
    ).fetchone()
    conn.close()
    return _row_to_user(row)


def _row_to_user(row) -> dict | None:
    if not row:
        return None
    return {"user_id": row[0], "username": row[1], "full_name": row[2],
            "last_seen": row[3], "tokens": row[4], "stars": row[5]}


def resolve_user(text: str) -> dict | None:
    text = text.strip()
    clean = text.lstrip("@")
    if clean.lstrip("-").isdigit():
        return get_user_by_id(int(clean))
    return get_user_by_username(text)


def get_user_transactions(user_id: int, limit: int = 15) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT amount,currency,type,description,created_at FROM transactions "
        "WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    conn.close()
    return rows


def get_recent_transactions(limit: int = 15) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT t.amount,t.currency,t.type,t.created_at,u.full_name,u.username
           FROM transactions t LEFT JOIN users u ON t.user_id=u.user_id
           ORDER BY t.id DESC LIMIT ?""", (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    s = {
        "total_users":  conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "total_tokens": conn.execute("SELECT COALESCE(SUM(tokens),0) FROM balances").fetchone()[0],
        "total_stars":  conn.execute("SELECT COALESCE(SUM(stars),0) FROM balances").fetchone()[0],
        "total_tx":     conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "active_today": conn.execute("SELECT COUNT(*) FROM users WHERE last_seen>=datetime('now','-1 day')").fetchone()[0],
        "total_admins": conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] + 1,
        "total_promos": conn.execute("SELECT COUNT(*) FROM promo_codes WHERE active=1").fetchone()[0],
    }
    conn.close()
    return s


def get_top_players(limit: int = 10) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT b.tokens,b.stars,u.full_name,u.username
           FROM balances b LEFT JOIN users u ON b.user_id=u.user_id
           WHERE b.tokens>0 OR b.stars>0 ORDER BY b.tokens DESC LIMIT ?""", (limit,)
    ).fetchall()
    conn.close()
    return rows


# ── Admin helpers ─────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_CHAT_ID:
        return True
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def is_superadmin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


def add_admin(user_id: int, added_by: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO admins (user_id,added_by) VALUES (?,?)", (user_id, added_by))
    conn.commit()
    conn.close()


def remove_admin(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_admin_list() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT a.user_id,u.full_name,u.username,a.added_at
           FROM admins a LEFT JOIN users u ON a.user_id=u.user_id ORDER BY a.added_at"""
    ).fetchall()
    conn.close()
    return rows


# ── Promo helpers ─────────────────────────────────────────────

def create_promo(code: str, reward_type: str, reward_amount: int, max_act: int, created_by: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO promo_codes (code,reward_type,reward_amount,max_activations,created_by) VALUES (?,?,?,?,?)",
            (code.upper(), reward_type, reward_amount, max_act, created_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def activate_promo(code: str, user_id: int) -> tuple[str, int, str]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id,reward_amount,reward_type,max_activations,activations,active FROM promo_codes WHERE UPPER(code)=UPPER(?)",
        (code,),
    ).fetchone()
    if not row:
        conn.close()
        return "not_found", 0, ""
    pid, amount, rtype, max_act, act, active = row
    if not active or act >= max_act:
        conn.close()
        return "expired", 0, ""
    if conn.execute("SELECT 1 FROM promo_uses WHERE promo_id=? AND user_id=?", (pid, user_id)).fetchone():
        conn.close()
        return "used", 0, ""
    conn.execute("UPDATE promo_codes SET activations=activations+1 WHERE id=?", (pid,))
    conn.execute("INSERT INTO promo_uses (promo_id,user_id) VALUES (?,?)", (pid, user_id))
    conn.commit()
    conn.close()
    return "ok", amount, rtype


def get_all_promos() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT code,reward_type,reward_amount,activations,max_activations,active,created_at FROM promo_codes ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def deactivate_promo_by_code(code: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE promo_codes SET active=0 WHERE UPPER(code)=UPPER(?)", (code,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ── Battle helpers ────────────────────────────────────────────

def get_battle_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM battle_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_battle_setting(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO battle_settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


def is_battle_active() -> bool:
    return get_battle_setting("active", "0") == "1"


def get_battle_channel() -> int | None:
    val = get_battle_setting("channel_id", "")
    return int(val) if val else None


def get_next_round_num() -> int:
    n = int(get_battle_setting("round_counter", "0")) + 1
    set_battle_setting("round_counter", str(n))
    return n


def add_to_battle_queue(user_id: int, nickname: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO battle_queue (user_id,nickname) VALUES (?,?)", (user_id, nickname))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_battle_queue() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id,user_id,nickname,votes FROM battle_queue ORDER BY id").fetchall()
    conn.close()
    return rows


def pop_two_from_queue() -> tuple | None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id,user_id,nickname,votes FROM battle_queue ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        conn.close()
        return None
    conn.execute("DELETE FROM battle_queue WHERE id IN (?,?)", (rows[0][0], rows[1][0]))
    conn.commit()
    conn.close()
    return rows[0], rows[1]


def clear_battle_queue() -> int:
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM battle_queue").fetchone()[0]
    conn.execute("DELETE FROM battle_queue")
    conn.commit()
    conn.close()
    return count


def is_user_in_battle(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    in_q = conn.execute("SELECT 1 FROM battle_queue WHERE user_id=?", (user_id,)).fetchone()
    in_r = conn.execute(
        "SELECT 1 FROM battle_rounds WHERE (nick1_user_id=? OR nick2_user_id=?) AND finished=0",
        (user_id, user_id),
    ).fetchone()
    conn.close()
    return in_q is not None or in_r is not None


def create_round(round_num: int, p1: tuple, p2: tuple, channel_id: int, message_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO battle_rounds
           (round_num,nick1_user_id,nick1,nick1_votes,nick2_user_id,nick2,nick2_votes,channel_id,message_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (round_num, p1[1], p1[2], p1[3], p2[1], p2[2], p2[3], channel_id, message_id),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get_active_round_for_user(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT id,round_num,nick1_user_id,nick1,nick1_votes,nick2_user_id,nick2,nick2_votes,channel_id,message_id
           FROM battle_rounds WHERE (nick1_user_id=? OR nick2_user_id=?) AND finished=0""",
        (user_id, user_id),
    ).fetchone()
    conn.close()
    return _row_to_round(row)


def get_round_by_id(round_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT id,round_num,nick1_user_id,nick1,nick1_votes,nick2_user_id,nick2,nick2_votes,channel_id,message_id,finished
           FROM battle_rounds WHERE id=?""",
        (round_id,),
    ).fetchone()
    conn.close()
    return _row_to_round(row)


def _row_to_round(row) -> dict | None:
    if not row:
        return None
    d = {"id": row[0], "round_num": row[1], "nick1_user_id": row[2], "nick1": row[3], "nick1_votes": row[4],
         "nick2_user_id": row[5], "nick2": row[6], "nick2_votes": row[7], "channel_id": row[8], "message_id": row[9]}
    if len(row) > 10:
        d["finished"] = row[10]
    return d


def update_round_votes(round_id: int, user_id: int, additional: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT nick1_user_id FROM battle_rounds WHERE id=?", (round_id,)).fetchone()
    col = "nick1_votes" if row and row[0] == user_id else "nick2_votes"
    conn.execute(f"UPDATE battle_rounds SET {col}={col}+? WHERE id=?", (additional, round_id))
    conn.commit()
    conn.close()
    return get_round_by_id(round_id)


def update_queue_votes(user_id: int, additional: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE battle_queue SET votes=votes+? WHERE user_id=?", (additional, user_id))
    conn.commit()
    conn.close()


def finish_round(round_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE battle_rounds SET finished=1 WHERE id=?", (round_id,))
    conn.commit()
    conn.close()


def get_active_rounds_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM battle_rounds WHERE finished=0").fetchone()[0]
    conn.close()
    return n


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def plural_votes(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "голос"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "голоса"
    return "голосов"


def votes_str(n: int) -> str:
    return f"+{n} {plural_votes(n)}" if n > 0 else ""


def format_battle_message(round_num: int, nick1: str, v1: int, nick2: str, v2: int) -> str:
    vs1 = f"  <i>({votes_str(v1)})</i>" if v1 > 0 else ""
    vs2 = f"  <i>({votes_str(v2)})</i>" if v2 > 0 else ""
    return (
        f"⚔️ <b>РАУНД {round_num} — БИТВА НИКОВ</b> ⚔️\n\n"
        f"🔴  1. <b>{nick1}</b>{vs1}\n"
        f"🔵  2. <b>{nick2}</b>{vs2}\n\n"
        f"⏰ Итоги через <b>1 час</b>\n"
        f"🗳️ Поддержи своего фаворита реакцией!"
    )


def tx_type_label(tx_type: str, currency: str = "tokens") -> str:
    icon = "⭐" if currency == "stars" else "🪙"
    return {
        "win_dart":    f"🎯 Победа (дротик) {icon}",
        "win_dice":    f"🎲 Победа (кубик) {icon}",
        "win_casino":  f"🎰 Победа (казино) {icon}",
        "admin_give":  f"➕ Выдано администратором {icon}",
        "admin_take":  f"➖ Списано администратором {icon}",
        "game_spend":  f"💸 Оплата игры {icon}",
        "promo":       f"🎟️ Промокод {icon}",
    }.get(tx_type, tx_type)


def format_user_card(u: dict) -> str:
    uname = f"@{u['username']}" if u["username"] else "—"
    return (
        f"👤 <b>{u['full_name']}</b>\n"
        f"🔗 {uname}\n"
        f"🆔 <code>{u['user_id']}</code>\n"
        f"🪙 Токены: <b>{u['tokens']}</b>\n"
        f"⭐ Звёзды: <b>{u['stars']}</b>\n"
        f"🕐 Онлайн: <i>{u['last_seen'][:16]}</i>"
    )


async def send_invoice(update, context, title, description, payload, stars_count):
    query = update.callback_query
    await query.answer()
    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=title, description=description, payload=payload,
        provider_token="", currency="XTR",
        prices=[LabeledPrice(title, stars_count)],
    )


def game_keyboard(user_id: int, game: str) -> InlineKeyboardMarkup:
    user_stars = get_stars(user_id)
    labels = {"dart": "🎯 Дротик", "dice": "🎲 Кубик", "casino": "🎰 Казино"}
    label   = labels.get(game, game)
    back_cb = "star_games" if game in ("dart", "dice") else "back_main"
    rows = [[InlineKeyboardButton(f"{label} — {GAME_COST}⭐ (Telegram)", callback_data=f"pay_{game}")]]
    if user_stars >= GAME_COST:
        rows.append([InlineKeyboardButton(
            f"{label} — {GAME_COST}⭐ из баланса  (у вас {user_stars}⭐)",
            callback_data=f"bal_{game}"
        )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


async def _update_battle_message(rnd: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_battle_message(rnd["round_num"], rnd["nick1"], rnd["nick1_votes"], rnd["nick2"], rnd["nick2_votes"])
    try:
        await context.bot.edit_message_text(
            chat_id=rnd["channel_id"], message_id=rnd["message_id"],
            text=text, parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not edit battle message: {e}")


async def _post_battle_round(p1: tuple, p2: tuple, context: ContextTypes.DEFAULT_TYPE) -> None:
    channel_id = get_battle_channel() or ADMIN_CHAT_ID
    round_num  = get_next_round_num()
    text = format_battle_message(round_num, p1[2], p1[3], p2[2], p2[3])
    try:
        msg = await context.bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
        rid = create_round(round_num, p1, p2, channel_id, msg.message_id)
        context.job_queue.run_once(
            battle_results_job,
            timedelta(hours=1),
            data={"round_id": rid},
            name=f"battle_round_{rid}",
        )
        for uid in [p1[1], p2[1]]:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"⚔️ <b>Ваш бой начался!</b>\n\n"
                        f"Раунд {round_num} опубликован в канале.\n"
                        f"⏰ Итоги через <b>1 час</b>!"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to post battle round to {channel_id}: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"❌ <b>Ошибка публикации раунда {round_num}!</b>\n<i>{e}</i>",
            parse_mode="HTML",
        )


# ═══════════════════════════════════════════════════════════
#  GAME LOGIC
# ═══════════════════════════════════════════════════════════

async def play_dart(chat_id, user_id, context, update):
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text("🎯 <b>Дротик летит...</b>", parse_mode="HTML")
    d = await context.bot.send_dice(chat_id=chat_id, emoji="🎯")
    await asyncio.sleep(4)
    v = d.dice.value
    if v == 6:
        nb = add_tokens(user_id, 20, "win_dart", "Победа в дротике")
        _, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎯 <b>Яблочко!</b>\n\n🏆 <b>Победа!</b>\n💰 <b>+20 токенов</b>\n"
            f"💼 Баланс: <b>{nb} 🪙</b>  |  <b>{stars} ⭐</b>", parse_mode="HTML")
    else:
        t, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎯 {v}-е кольцо...\n\n😔 <b>Мимо!</b>\n"
            f"💼 Баланс: <b>{t} 🪙</b>  |  <b>{stars} ⭐</b> 🍀", parse_mode="HTML")


async def play_dice(chat_id, user_id, context, update):
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text("🎲 <b>Кубик брошен...</b>", parse_mode="HTML")
    d = await context.bot.send_dice(chat_id=chat_id, emoji="🎲")
    await asyncio.sleep(3)
    v = d.dice.value
    if v == 6:
        nb = add_tokens(user_id, 20, "win_dice", "Победа в кубике")
        _, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎲 Выпало <b>6</b>!\n\n🏆 <b>Победа!</b>\n💰 <b>+20 токенов</b>\n"
            f"💼 Баланс: <b>{nb} 🪙</b>  |  <b>{stars} ⭐</b>", parse_mode="HTML")
    else:
        t, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎲 Выпало <b>{v}</b>...\n\n😔 <b>Не повезло!</b>\n"
            f"💼 Баланс: <b>{t} 🪙</b>  |  <b>{stars} ⭐</b> 🍀", parse_mode="HTML")


async def play_casino(user_id, context, update):
    msg = update.callback_query.message if update.callback_query else update.message
    n1, n2, n3 = random.randint(1,7), random.randint(1,7), random.randint(1,7)
    disp = f"{NUM_EMOJI[n1-1]}  {NUM_EMOJI[n2-1]}  {NUM_EMOJI[n3-1]}"
    slot = f"┌──────────────────┐\n│   {disp}   │\n└──────────────────┘"
    if n1 == n2 == n3 == 7:
        nb = add_tokens(user_id, 25, "win_casino", "Джекпот 777")
        _, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎰 <b>Барабаны крутятся...</b>\n\n<code>{slot}</code>\n\n"
            f"💎 <b>ДЖЕКПОТ! 7-7-7!</b>\n🏆 <b>Победа!</b>\n💰 <b>+25 токенов</b>\n"
            f"💼 Баланс: <b>{nb} 🪙</b>  |  <b>{stars} ⭐</b>", parse_mode="HTML")
    elif n1 == n2 == n3:
        nb = add_tokens(user_id, 15, "win_casino", f"Три одинаковых ({n1})")
        _, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎰 <b>Барабаны крутятся...</b>\n\n<code>{slot}</code>\n\n"
            f"🎉 <b>Три одинаковых!</b>\n🏆 <b>Победа!</b>\n💰 <b>+15 токенов</b>\n"
            f"💼 Баланс: <b>{nb} 🪙</b>  |  <b>{stars} ⭐</b>", parse_mode="HTML")
    else:
        t, stars = get_balance(user_id)
        await msg.reply_text(
            f"🎰 <b>Барабаны крутятся...</b>\n\n<code>{slot}</code>\n\n"
            f"😔 <b>Не сегодня!</b>\n"
            f"💼 Баланс: <b>{t} 🪙</b>  |  <b>{stars} ⭐</b> 🍀", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  BATTLE RESULTS JOB
# ═══════════════════════════════════════════════════════════

async def battle_results_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    round_id = context.job.data["round_id"]
    rnd = get_round_by_id(round_id)
    if not rnd or rnd.get("finished"):
        return
    finish_round(round_id)

    n1, v1 = rnd["nick1"], rnd["nick1_votes"]
    n2, v2 = rnd["nick2"], rnd["nick2_votes"]

    if v1 == v2:
        text = (
            f"🤝 <b>ИТОГИ РАУНДА {rnd['round_num']}</b> 🤝\n\n"
            f"⚖️ <b>Ничья!</b> Оба участника набрали одинаковое количество голосов.\n\n"
            f"🔴 <b>{n1}</b>  —  {votes_str(v1) or '0 голосов'}\n"
            f"🔵 <b>{n2}</b>  —  {votes_str(v2) or '0 голосов'}"
        )
    else:
        if v1 >= v2:
            winner, wv, loser, lv = n1, v1, n2, v2
        else:
            winner, wv, loser, lv = n2, v2, n1, v1
        ws = f" ({votes_str(wv)})" if wv > 0 else ""
        ls = f" ({votes_str(lv)})" if lv > 0 else ""
        text = (
            f"🏆 <b>ИТОГИ РАУНДА {rnd['round_num']}</b> 🏆\n\n"
            f"🥇 Победитель: <b>{winner}</b>{ws}\n"
            f"🥈 Уступил: <b>{loser}</b>{ls}\n\n"
            f"🎉 Поздравляем победителя!"
        )
    try:
        await context.bot.send_message(chat_id=rnd["channel_id"], text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send results for round {round_id}: {e}")


# ═══════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════

MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🗡️ Битва ников",   callback_data="battle")],
    [InlineKeyboardButton("⭐ Игры на звёзды", callback_data="star_games")],
    [InlineKeyboardButton("🎰 Казино",         callback_data="casino")],
    [InlineKeyboardButton("💰 Мой баланс",     callback_data="balance")],
])

STAR_GAMES_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎯 Дротик",  callback_data="dart")],
    [InlineKeyboardButton("🎲 Кубик",   callback_data="dice_game")],
    [InlineKeyboardButton("◀️ Назад",  callback_data="back_main")],
])

BUY_VOTES_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("1⭐ — 2 голоса",     callback_data="pay_votes_1")],
    [InlineKeyboardButton("10⭐ — 25 голосов",  callback_data="pay_votes_10")],
    [InlineKeyboardButton("50⭐ — 115 голосов", callback_data="pay_votes_50")],
])

ADMIN_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📊 Статистика",      callback_data="adm_stats"),
        InlineKeyboardButton("👑 Топ игроков",     callback_data="adm_top"),
    ],
    [
        InlineKeyboardButton("🔍 Найти игрока",    callback_data="adm_search"),
        InlineKeyboardButton("📜 История",         callback_data="adm_history"),
    ],
    [
        InlineKeyboardButton("🪙 Выдать токены",   callback_data="adm_give"),
        InlineKeyboardButton("🪙 Забрать токены",  callback_data="adm_take"),
    ],
    [
        InlineKeyboardButton("⭐ Выдать звёзды",   callback_data="adm_give_stars"),
        InlineKeyboardButton("⭐ Забрать звёзды",  callback_data="adm_take_stars"),
    ],
    [
        InlineKeyboardButton("🎟️ Промокоды",      callback_data="adm_promos"),
        InlineKeyboardButton("🛡️ Администраторы", callback_data="adm_admins"),
    ],
    [InlineKeyboardButton("⚔️ Битва ников",        callback_data="adm_battle")],
    [InlineKeyboardButton("📢 Рассылка",           callback_data="adm_broadcast")],
])

ADMIN_CANCEL_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel")],
])

PROMO_TYPE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🪙 Токены",  callback_data="pt_tokens"),
        InlineKeyboardButton("⭐ Звёзды", callback_data="pt_stars"),
    ],
    [InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel")],
])


# ═══════════════════════════════════════════════════════════
#  /start  /promo
# ═══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user.id, user.username, user.full_name)
    await update.message.reply_text(
        "✨ <b>HarukaHelper</b> — твой игровой помощник!\n\n"
        "🗡️ <b>Битва ников</b> — сразись за звание сильнейшего\n"
        "⭐ <b>Игры на звёзды</b> — дротик и кубик\n"
        "🎰 <b>Казино</b> — испытай удачу на барабанах\n\n"
        "💫 Копи токены и звёзды — внутреннюю валюту!\n\n"
        "👇 <b>Выбирай и вперёд:</b>",
        reply_markup=MAIN_KEYBOARD, parse_mode="HTML",
    )


async def promo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user.id, user.username, user.full_name)
    if not context.args:
        await update.message.reply_text(
            "🎟️ <b>Промокод</b>\n\nИспользование: <code>/promo КОД</code>", parse_mode="HTML"
        )
        return
    status, amount, rtype = activate_promo(context.args[0], user.id)
    if status == "not_found":
        await update.message.reply_text("❌ <b>Промокод не найден.</b>", parse_mode="HTML")
    elif status == "expired":
        await update.message.reply_text("⏳ <b>Промокод исчерпан.</b>", parse_mode="HTML")
    elif status == "used":
        await update.message.reply_text("⚠️ <b>Вы уже активировали этот промокод.</b>", parse_mode="HTML")
    elif status == "ok":
        if rtype == "stars":
            nv = add_stars(user.id, amount, "promo", f"Промокод {context.args[0].upper()}")
            t, _ = get_balance(user.id)
            await update.message.reply_text(
                f"🎟️ <b>Промокод активирован!</b>\n\n⭐ <b>+{amount} звёзд</b>\n"
                f"💼 Баланс: <b>{t} 🪙</b>  |  <b>{nv} ⭐</b>", parse_mode="HTML")
        else:
            nv = add_tokens(user.id, amount, "promo", f"Промокод {context.args[0].upper()}")
            _, stars = get_balance(user.id)
            await update.message.reply_text(
                f"🎟️ <b>Промокод активирован!</b>\n\n🪙 <b>+{amount} токенов</b>\n"
                f"💼 Баланс: <b>{nv} 🪙</b>  |  <b>{stars} ⭐</b>", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  BALANCE
# ═══════════════════════════════════════════════════════════

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tokens, stars = get_balance(query.from_user.id)
    await query.message.reply_text(
        f"💰 <b>Мой баланс</b>\n\n"
        f"🪙 Токены: <b>{tokens}</b>\n"
        f"⭐ Звёзды: <b>{stars}</b>\n\n"
        f"<i>Хочешь обменять? 📩 Напиши в поддержку!</i>",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════
#  BATTLE
# ═══════════════════════════════════════════════════════════

async def battle_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_battle_active():
        await query.message.reply_text(
            "⚔️ <b>Битва ников</b>\n\n"
            "🔴 Битва сейчас неактивна.\n\n"
            "Следите за объявлениями — она скоро начнётся! 🔥",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    queue = get_battle_queue()
    await query.message.reply_text(
        "⚔️ <b>Битва ников</b>\n\n"
        "Докажи, что твой ник — самый крутой!\n\n"
        "<b>Как это работает:</b>\n"
        "• Отправь свой ник следующим сообщением\n"
        "• Когда наберётся пара — начнётся раунд\n"
        "• Через час объявляются итоги по голосам\n\n"
        f"⏳ В очереди: <b>{len(queue)}</b> / 2\n\n"
        "✏️ <i>Отправь свой ник прямо сейчас:</i>",
        parse_mode="HTML",
    )
    return WAITING_NICK


async def receive_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    nickname = update.message.text.strip()
    user = update.effective_user
    upsert_user(user.id, user.username, user.full_name)

    if not is_battle_active():
        await update.message.reply_text("🔴 Битва неактивна. Попробуйте позже.")
        return ConversationHandler.END

    if is_user_in_battle(user.id):
        await update.message.reply_text("⚠️ Вы уже участвуете в текущем раунде или стоите в очереди!")
        return ConversationHandler.END

    if not add_to_battle_queue(user.id, nickname):
        await update.message.reply_text("⚠️ Вы уже в очереди!")
        return ConversationHandler.END

    queue = get_battle_queue()
    pos = len(queue)

    if pos < 2:
        await update.message.reply_text(
            f"✅ <b>Ник принят!</b>\n\n"
            f"🏷️ Ваш ник: <b>{nickname}</b>\n"
            f"⏳ Ожидаем соперника... (<b>{pos}</b>/2)\n\n"
            f"Хотите увеличить шансы на победу?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Купить голоса", callback_data="buy_votes")]]),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"✅ <b>Ник принят!</b>  🏷️ <b>{nickname}</b>\n\n⚔️ Пара найдена! Раунд начинается...",
            parse_mode="HTML",
        )
        pair = pop_two_from_queue()
        if pair:
            await _post_battle_round(pair[0], pair[1], context)

    return ConversationHandler.END


async def cancel_battle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def buy_votes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "⭐ <b>Купить голоса</b>\n\nВыбери пакет:",
        reply_markup=BUY_VOTES_KEYBOARD, parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════
#  STAR GAMES
# ═══════════════════════════════════════════════════════════

async def show_star_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, stars = get_balance(query.from_user.id)
    await query.message.reply_text(
        f"⭐ <b>Игры на звёзды</b>\n\n"
        f"Каждая игра стоит <b>{GAME_COST}⭐</b>.\n"
        f"🎯 <b>Дротик</b> — яблочко → <b>+20 токенов</b>\n"
        f"🎲 <b>Кубик</b> — выпадет 6 → <b>+20 токенов</b>\n\n"
        f"💫 Ваш звёздный баланс: <b>{stars} ⭐</b>",
        reply_markup=STAR_GAMES_KEYBOARD, parse_mode="HTML",
    )


async def show_dart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, stars = get_balance(query.from_user.id)
    await query.message.reply_text(
        f"🎯 <b>Дротик</b>\n\n• Стоимость: <b>{GAME_COST}⭐</b>\n"
        f"• Яблочко → <b>+20 токенов</b>\n\n💫 Баланс: <b>{stars} ⭐</b>",
        reply_markup=game_keyboard(query.from_user.id, "dart"), parse_mode="HTML",
    )


async def show_dice_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, stars = get_balance(query.from_user.id)
    await query.message.reply_text(
        f"🎲 <b>Кубик</b>\n\n• Стоимость: <b>{GAME_COST}⭐</b>\n"
        f"• Выпало 6 → <b>+20 токенов</b>\n\n💫 Баланс: <b>{stars} ⭐</b>",
        reply_markup=game_keyboard(query.from_user.id, "dice"), parse_mode="HTML",
    )


async def show_casino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, stars = get_balance(query.from_user.id)
    await query.message.reply_text(
        f"🎰 <b>Казино</b>\n\n• Стоимость: <b>{GAME_COST}⭐</b>\n"
        f"• Три одинаковых → <b>+15 токенов</b>\n"
        f"• 7️⃣7️⃣7️⃣ → <b>+25 токенов</b> 💎\n\n💫 Баланс: <b>{stars} ⭐</b>",
        reply_markup=game_keyboard(query.from_user.id, "casino"), parse_mode="HTML",
    )


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("👇 <b>Главное меню:</b>", reply_markup=MAIN_KEYBOARD, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
#  PAYMENTS — Telegram Stars
# ═══════════════════════════════════════════════════════════

async def pay_dart(u, c):
    await send_invoice(u, c, "Бросок дротика", "Яблочко = +20 токенов!", "game_dart", GAME_COST)

async def pay_dice(u, c):
    await send_invoice(u, c, "Бросок кубика", "Выпадет 6 = +20 токенов!", "game_dice", GAME_COST)

async def pay_casino(u, c):
    await send_invoice(u, c, "Прокрут казино", "Три одинаковых или 777 = победа!", "game_casino", GAME_COST)

async def pay_votes_1(u, c):
    await send_invoice(u, c, "2 голоса", "2 голоса в Битве ников", "votes_2", 1)

async def pay_votes_10(u, c):
    await send_invoice(u, c, "25 голосов", "25 голосов в Битве ников", "votes_25", 10)

async def pay_votes_50(u, c):
    await send_invoice(u, c, "115 голосов", "115 голосов в Битве ников", "votes_115", 50)


# ═══════════════════════════════════════════════════════════
#  PAYMENTS — Balance Stars
# ═══════════════════════════════════════════════════════════

async def bal_dart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if get_stars(uid) < GAME_COST:
        await query.answer(f"❌ Нужно {GAME_COST}⭐, у вас {get_stars(uid)}⭐", show_alert=True)
        return
    await query.answer()
    add_stars(uid, -GAME_COST, "game_spend", "Оплата броска дротика")
    await play_dart(query.message.chat_id, uid, context, update)


async def bal_dice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if get_stars(uid) < GAME_COST:
        await query.answer(f"❌ Нужно {GAME_COST}⭐, у вас {get_stars(uid)}⭐", show_alert=True)
        return
    await query.answer()
    add_stars(uid, -GAME_COST, "game_spend", "Оплата броска кубика")
    await play_dice(query.message.chat_id, uid, context, update)


async def bal_casino(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if get_stars(uid) < GAME_COST:
        await query.answer(f"❌ Нужно {GAME_COST}⭐, у вас {get_stars(uid)}⭐", show_alert=True)
        return
    await query.answer()
    add_stars(uid, -GAME_COST, "game_spend", "Оплата прокрута казино")
    await play_casino(uid, context, update)


# ═══════════════════════════════════════════════════════════
#  PRE-CHECKOUT & SUCCESSFUL PAYMENT
# ═══════════════════════════════════════════════════════════

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payload = update.message.successful_payment.invoice_payload
    user    = update.effective_user
    chat_id = update.effective_chat.id
    upsert_user(user.id, user.username, user.full_name)

    # ── Votes ──────────────────────────────────────────────
    if payload in ("votes_2", "votes_25", "votes_115"):
        votes_map = {"votes_2": 2, "votes_25": 25, "votes_115": 115}
        votes = votes_map[payload]

        # Update battle round or queue
        rnd = get_active_round_for_user(user.id)
        if rnd:
            rnd = update_round_votes(rnd["id"], user.id, votes)
            await _update_battle_message(rnd, context)
            ctx_note = f"\n\n⚔️ Голоса учтены в текущем раунде!"
        else:
            update_queue_votes(user.id, votes)
            ctx_note = f"\n\n⏳ Голоса будут применены в вашем раунде."

        await update.message.reply_text(
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"🗳️ <b>+{votes} {plural_votes(votes)}</b> добавлено к вашему нику!{ctx_note}",
            parse_mode="HTML",
        )
        return

    if payload == "game_dart":
        await play_dart(chat_id, user.id, context, update)
    elif payload == "game_dice":
        await play_dice(chat_id, user.id, context, update)
    elif payload == "game_casino":
        await play_casino(user.id, context, update)


# ═══════════════════════════════════════════════════════════
#  ADMIN — entry
# ═══════════════════════════════════════════════════════════

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await update.message.reply_text(
        "👑 <b>Админ-панель HarukaHelper</b>\n\nВыберите действие:",
        reply_markup=ADMIN_KEYBOARD, parse_mode="HTML",
    )


async def adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    s = get_stats()
    await query.message.reply_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Игроков: <b>{s['total_users']}</b>\n"
        f"🟢 Активны сегодня: <b>{s['active_today']}</b>\n"
        f"🪙 Токенов в обороте: <b>{s['total_tokens']}</b>\n"
        f"⭐ Звёзд в обороте: <b>{s['total_stars']}</b>\n"
        f"📈 Транзакций: <b>{s['total_tx']}</b>\n"
        f"🛡️ Администраторов: <b>{s['total_admins']}</b>\n"
        f"🎟️ Активных промокодов: <b>{s['total_promos']}</b>",
        parse_mode="HTML",
    )


async def adm_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    top = get_top_players(10)
    if not top:
        await query.message.reply_text("👑 Нет игроков с балансом.")
        return
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = [
        f"{medals[i]} <b>{name or '—'}</b> (@{uname or '—'}) — <b>{tokens} 🪙</b> / <b>{stars} ⭐</b>"
        for i, (tokens, stars, name, uname) in enumerate(top)
    ]
    await query.message.reply_text("👑 <b>Топ игроков</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# ── Battle management ──────────────────────────────────────

async def adm_battle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    active = is_battle_active()
    channel_id = get_battle_channel()
    queue = get_battle_queue()
    arc = get_active_rounds_count()
    channel_str = str(channel_id) if channel_id else "❗ Не установлен"
    status_icon = "🟢 Активна" if active else "🔴 Остановлена"
    text = (
        f"⚔️ <b>Управление Битвой ников</b>\n\n"
        f"📡 Статус: <b>{status_icon}</b>\n"
        f"📢 Канал: <code>{channel_str}</code>\n"
        f"⏳ В очереди: <b>{len(queue)}</b> чел.\n"
        f"🥊 Активных раундов: <b>{arc}</b>"
    )
    kb = []
    if active:
        kb.append([InlineKeyboardButton("⏹️ Остановить битву", callback_data="adm_battle_stop")])
    else:
        kb.append([InlineKeyboardButton("▶️ Запустить битву",  callback_data="adm_battle_start")])
    kb.append([InlineKeyboardButton("📢 Сменить канал", callback_data="adm_battle_channel")])
    if queue:
        kb.append([InlineKeyboardButton(f"🗑️ Очистить очередь ({len(queue)})", callback_data="adm_battle_clear")])
    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def adm_battle_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    set_battle_setting("active", "1")
    await query.message.reply_text(
        "✅ <b>Битва ников запущена!</b>\n\n"
        "🟢 Игроки теперь могут регистрироваться через кнопку «Битва ников».",
        parse_mode="HTML",
    )


async def adm_battle_stop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    set_battle_setting("active", "0")
    cleared = clear_battle_queue()
    await query.message.reply_text(
        f"⏹️ <b>Битва ников остановлена.</b>\n\n"
        f"🗑️ Очередь очищена ({cleared} чел.).",
        parse_mode="HTML",
    )


async def adm_battle_clear_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cleared = clear_battle_queue()
    await query.message.reply_text(f"✅ Очередь очищена ({cleared} чел.).")


async def adm_battle_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "📢 <b>Канал для битвы</b>\n\n"
        "Введите ID канала (например <code>-1001234567890</code>)\n"
        "или @username (например <code>@mychannel</code>).\n\n"
        "<i>Бот должен быть добавлен в канал как администратор.</i>",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_BATTLE_CHANNEL


async def adm_battle_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        chat = await context.bot.get_chat(text)
        set_battle_setting("channel_id", str(chat.id))
        await update.message.reply_text(
            f"✅ <b>Канал установлен!</b>\n\n📢 <b>{chat.title or chat.username}</b>\n🆔 <code>{chat.id}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не удалось найти канал. Убедитесь, что бот добавлен как администратор.\n<i>{e}</i>",
            reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
        )
        return ADMIN_BATTLE_CHANNEL
    return ConversationHandler.END


# ── Search ─────────────────────────────────────────────────

async def adm_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "🔍 <b>Поиск игрока</b>\n\nВведите <b>@username</b> или <b>ID</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_SEARCH_INPUT


async def adm_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = resolve_user(update.message.text)
    if not user:
        await update.message.reply_text("❌ Не найден.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_SEARCH_INPUT
    txs = get_user_transactions(user["user_id"], 5)
    tx_lines = ""
    if txs:
        tx_lines = "\n\n📜 <b>Последние операции:</b>\n"
        for amount, currency, tx_type, _, created_at in txs:
            icon = "⭐" if currency == "stars" else "🪙"
            sign = "+" if amount >= 0 else ""
            tx_lines += f"  {sign}{amount}{icon} — {tx_type_label(tx_type, currency)} <i>({created_at[:16]})</i>\n"
    badge = " 🛡️ <i>Администратор</i>" if is_admin(user["user_id"]) else ""
    await update.message.reply_text(
        f"🔍 <b>Найден игрок</b>{badge}\n\n{format_user_card(user)}{tx_lines}", parse_mode="HTML"
    )
    return ConversationHandler.END


# ── Give / Take tokens ─────────────────────────────────────

async def adm_give_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data["give_currency"] = "tokens"
    await query.message.reply_text(
        "🪙 <b>Выдать токены</b>\n\nВведите <b>@username</b> или <b>ID</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_GIVE_USER


async def adm_give_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = resolve_user(update.message.text)
    if not user:
        await update.message.reply_text("❌ Не найден.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_GIVE_USER
    context.user_data["target_user"] = user
    cur = context.user_data.get("give_currency", "tokens")
    icon = "🪙" if cur == "tokens" else "⭐"
    cur_bal = user["tokens"] if cur == "tokens" else user["stars"]
    await update.message.reply_text(
        f"✅ <b>{user['full_name']}</b> (баланс: {cur_bal} {icon})\n\nВведите количество:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_GIVE_AMOUNT


async def adm_give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Число больше 0.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_GIVE_AMOUNT
    amount = int(text)
    target = context.user_data.pop("target_user")
    cur    = context.user_data.pop("give_currency", "tokens")
    icon   = "🪙" if cur == "tokens" else "⭐"
    nv = add_tokens(target["user_id"], amount, "admin_give", "Выдано администратором") if cur == "tokens" \
        else add_stars(target["user_id"], amount, "admin_give", "Выдано администратором")
    await update.message.reply_text(
        f"✅ <b>Готово!</b>\n\n👤 {target['full_name']}\n➕ <b>+{amount} {icon}</b>\n💼 Баланс: <b>{nv} {icon}</b>",
        parse_mode="HTML",
    )
    try:
        word = "токенов" if cur == "tokens" else "звёзд"
        await context.bot.send_message(
            chat_id=target["user_id"],
            text=f"🎁 <b>Администратор выдал вам {amount} {word}!</b>\n💼 Баланс: <b>{nv} {icon}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def adm_take_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data["take_currency"] = "tokens"
    await query.message.reply_text(
        "🪙 <b>Забрать токены</b>\n\nВведите <b>@username</b> или <b>ID</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_TAKE_USER


async def adm_take_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = resolve_user(update.message.text)
    if not user:
        await update.message.reply_text("❌ Не найден.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_TAKE_USER
    context.user_data["target_user"] = user
    cur = context.user_data.get("take_currency", "tokens")
    icon = "🪙" if cur == "tokens" else "⭐"
    cur_bal = user["tokens"] if cur == "tokens" else user["stars"]
    await update.message.reply_text(
        f"✅ <b>{user['full_name']}</b> (баланс: {cur_bal} {icon})\n\nВведите количество для списания:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_TAKE_AMOUNT


async def adm_take_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Число больше 0.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_TAKE_AMOUNT
    amount  = int(text)
    target  = context.user_data.pop("target_user")
    cur     = context.user_data.pop("take_currency", "tokens")
    icon    = "🪙" if cur == "tokens" else "⭐"
    cur_bal = target["tokens"] if cur == "tokens" else target["stars"]
    deduct  = min(amount, cur_bal)
    nv = add_tokens(target["user_id"], -deduct, "admin_take", "Списано администратором") if cur == "tokens" \
        else add_stars(target["user_id"], -deduct, "admin_take", "Списано администратором")
    await update.message.reply_text(
        f"✅ <b>Готово!</b>\n\n👤 {target['full_name']}\n➖ <b>{deduct} {icon}</b>\n💼 Баланс: <b>{nv} {icon}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── Give / Take stars ──────────────────────────────────────

async def adm_give_stars_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "⭐ <b>Выдать звёзды</b>\n\nВведите <b>@username</b> или <b>ID</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_GIVE_STARS_USER


async def adm_give_stars_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = resolve_user(update.message.text)
    if not user:
        await update.message.reply_text("❌ Не найден.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_GIVE_STARS_USER
    context.user_data["target_user"] = user
    await update.message.reply_text(
        f"✅ <b>{user['full_name']}</b> (звёзды: {user['stars']} ⭐)\n\nВведите количество звёзд:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_GIVE_STARS_AMOUNT


async def adm_give_stars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Число больше 0.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_GIVE_STARS_AMOUNT
    amount = int(text)
    target = context.user_data.pop("target_user")
    nv = add_stars(target["user_id"], amount, "admin_give", "Выдано администратором")
    await update.message.reply_text(
        f"✅ <b>Готово!</b>\n\n👤 {target['full_name']}\n➕ <b>+{amount} ⭐</b>\n⭐ Баланс: <b>{nv}</b>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=target["user_id"],
            text=f"🎁 <b>Администратор выдал вам {amount} звёзд!</b>\n⭐ Баланс: <b>{nv}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def adm_take_stars_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "⭐ <b>Забрать звёзды</b>\n\nВведите <b>@username</b> или <b>ID</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_TAKE_STARS_USER


async def adm_take_stars_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = resolve_user(update.message.text)
    if not user:
        await update.message.reply_text("❌ Не найден.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_TAKE_STARS_USER
    context.user_data["target_user"] = user
    await update.message.reply_text(
        f"✅ <b>{user['full_name']}</b> (звёзды: {user['stars']} ⭐)\n\nВведите количество для списания:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_TAKE_STARS_AMOUNT


async def adm_take_stars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Число больше 0.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_TAKE_STARS_AMOUNT
    amount  = int(text)
    target  = context.user_data.pop("target_user")
    deduct  = min(amount, target["stars"])
    nv = add_stars(target["user_id"], -deduct, "admin_take", "Списано администратором")
    await update.message.reply_text(
        f"✅ <b>Готово!</b>\n\n👤 {target['full_name']}\n➖ <b>{deduct} ⭐</b>\n⭐ Баланс: <b>{nv}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── History ────────────────────────────────────────────────

async def adm_history_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "📜 <b>История транзакций</b>\n\n"
        "Введите <b>@username</b> или <b>ID</b>,\n"
        "или напишите <b>all</b> для общей истории:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_HISTORY_USER


async def adm_history_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == "all":
        rows = get_recent_transactions(15)
        if not rows:
            await update.message.reply_text("📜 Транзакций нет.")
            return ConversationHandler.END
        lines = []
        for amount, currency, tx_type, created_at, full_name, username in rows:
            icon = "⭐" if currency == "stars" else "🪙"
            sign = "+" if amount >= 0 else ""
            who = f"@{username}" if username else full_name or "—"
            lines.append(f"• {sign}{amount}{icon} | {tx_type_label(tx_type, currency)}\n  <i>{who} — {created_at[:16]}</i>")
        await update.message.reply_text(
            "📜 <b>Последние 15 транзакций</b>\n\n" + "\n\n".join(lines), parse_mode="HTML"
        )
        return ConversationHandler.END
    user = resolve_user(text)
    if not user:
        await update.message.reply_text("❌ Не найден.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_HISTORY_USER
    txs = get_user_transactions(user["user_id"], 15)
    if not txs:
        await update.message.reply_text(f"📜 У <b>{user['full_name']}</b> нет транзакций.", parse_mode="HTML")
        return ConversationHandler.END
    lines = []
    for amount, currency, tx_type, _, created_at in txs:
        icon = "⭐" if currency == "stars" else "🪙"
        sign = "+" if amount >= 0 else ""
        lines.append(f"• {sign}{amount}{icon} — {tx_type_label(tx_type, currency)}\n  <i>{created_at[:16]}</i>")
    await update.message.reply_text(
        f"📜 <b>История — {user['full_name']}</b>\n"
        f"🪙 {user['tokens']}  ⭐ {user['stars']}\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── Admins ─────────────────────────────────────────────────

async def adm_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    admin_list = get_admin_list()
    lines = [f"👑 <b>Главный:</b> <code>{ADMIN_CHAT_ID}</code>"]
    for uid, name, uname, added_at in admin_list:
        lines.append(f"🛡️ <b>{name or uid}</b> (@{uname or '—'}) — <code>{uid}</code> | {added_at[:10]}")
    kb = []
    if is_superadmin(query.from_user.id):
        kb.append([InlineKeyboardButton("➕ Добавить администратора", callback_data="adm_add_admin")])
        for uid, name, _, _ in admin_list:
            kb.append([InlineKeyboardButton(f"❌ Удалить {name or uid}", callback_data=f"adm_del_admin_{uid}")])
    await query.message.reply_text(
        f"🛡️ <b>Администраторы</b>\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb) if kb else None, parse_mode="HTML",
    )


async def adm_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_superadmin(query.from_user.id):
        await query.answer("⛔ Только главный администратор.", show_alert=True)
        return ConversationHandler.END
    await query.message.reply_text(
        "🛡️ <b>Добавить администратора</b>\n\nВведите <b>@username</b> или <b>ID</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_ADD_ADMIN


async def adm_add_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = resolve_user(update.message.text)
    if not user:
        await update.message.reply_text("❌ Не найден в базе. Игрок должен запустить бота.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_ADD_ADMIN
    if is_admin(user["user_id"]):
        await update.message.reply_text(f"⚠️ <b>{user['full_name']}</b> уже администратор.", parse_mode="HTML")
        return ConversationHandler.END
    add_admin(user["user_id"], update.effective_user.id)
    await update.message.reply_text(
        f"✅ <b>{user['full_name']}</b> назначен администратором!\n🆔 <code>{user['user_id']}</code>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=user["user_id"],
            text="🛡️ <b>Вы назначены администратором HarukaHelper!</b>\n\nИспользуйте /admin для доступа к панели.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def adm_del_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_superadmin(query.from_user.id):
        await query.answer("⛔ Только главный.", show_alert=True)
        return
    uid = int(query.data.split("_")[-1])
    remove_admin(uid)
    await query.answer("✅ Удалён.")
    await query.message.reply_text(f"✅ <code>{uid}</code> удалён из администраторов.", parse_mode="HTML")


# ── Promo codes ────────────────────────────────────────────

async def adm_promos_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    promos = get_all_promos()
    if promos:
        lines = []
        for code, rtype, ramount, act, max_act, active, created_at in promos:
            icon   = "⭐" if rtype == "stars" else "🪙"
            status = "✅" if active and act < max_act else "❌"
            lines.append(f"{status} <code>{code}</code> — {ramount}{icon} | {act}/{max_act} | {created_at[:10]}")
        promos_text = "\n".join(lines)
    else:
        promos_text = "Промокодов нет."
    kb = [[InlineKeyboardButton("➕ Создать промокод", callback_data="adm_create_promo")]]
    if promos:
        kb.append([InlineKeyboardButton("🗑️ Деактивировать", callback_data="adm_deactivate_promo")])
    await query.message.reply_text(
        f"🎟️ <b>Промокоды</b>\n\n{promos_text}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
    )


async def adm_create_promo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "🎟️ <b>Создать промокод</b>\n\nВведите <b>название</b> (латиница, без пробелов):\n<i>Пример: HARUKA2025</i>",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_PROMO_CODE


async def adm_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = context.user_data.get("action")
    code = update.message.text.strip().upper().replace(" ", "")
    if action == "deactivate_promo":
        context.user_data.pop("action", None)
        ok = deactivate_promo_by_code(code)
        await update.message.reply_text(
            f"✅ Промокод <code>{code}</code> деактивирован." if ok else f"❌ Промокод <code>{code}</code> не найден.",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    if not code.isalnum():
        await update.message.reply_text("❌ Только буквы и цифры, без пробелов.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_PROMO_CODE
    context.user_data["promo_code"] = code
    await update.message.reply_text(
        f"✅ Код: <code>{code}</code>\n\n<b>Выберите тип награды:</b>",
        reply_markup=PROMO_TYPE_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_PROMO_TYPE


async def adm_promo_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rtype = "tokens" if query.data == "pt_tokens" else "stars"
    icon  = "🪙 токены" if rtype == "tokens" else "⭐ звёзды"
    context.user_data["promo_reward_type"] = rtype
    await query.message.reply_text(
        f"✅ Тип: <b>{icon}</b>\n\nВведите <b>количество</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_PROMO_AMOUNT


async def adm_promo_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Число больше 0.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_PROMO_AMOUNT
    context.user_data["promo_amount"] = int(text)
    await update.message.reply_text(
        f"✅ Количество: <b>{text}</b>\n\nВведите <b>максимальное число активаций</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_PROMO_MAX


async def adm_promo_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Число больше 0.", reply_markup=ADMIN_CANCEL_KEYBOARD)
        return ADMIN_PROMO_MAX
    code    = context.user_data.pop("promo_code")
    rtype   = context.user_data.pop("promo_reward_type")
    amount  = context.user_data.pop("promo_amount")
    max_act = int(text)
    icon    = "⭐" if rtype == "stars" else "🪙"
    ok = create_promo(code, rtype, amount, max_act, update.effective_user.id)
    if ok:
        await update.message.reply_text(
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎟️ <code>{code}</code>\n"
            f"💰 Награда: <b>{amount} {icon}</b>\n"
            f"🔢 Активаций: <b>{max_act}</b>\n\n"
            f"Команда для игроков: <code>/promo {code}</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"❌ Промокод <code>{code}</code> уже существует.", parse_mode="HTML")
    return ConversationHandler.END


async def adm_deactivate_promo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data["action"] = "deactivate_promo"
    await query.message.reply_text(
        "🗑️ <b>Деактивировать промокод</b>\n\nВведите <b>код</b>:",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_PROMO_CODE


# ── Broadcast ──────────────────────────────────────────────

async def adm_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "📢 <b>Рассылка</b>\n\nВведите текст сообщения.\n<i>Поддерживается HTML.</i>",
        reply_markup=ADMIN_CANCEL_KEYBOARD, parse_mode="HTML",
    )
    return ADMIN_BROADCAST


async def adm_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text_html
    conn = sqlite3.connect(DB_PATH)
    user_ids = [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    conn.close()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📢 <b>Рассылка завершена</b>\n✅ Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def adm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.reply_text("❌ Действие отменено.", reply_markup=ADMIN_KEYBOARD)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    init_db()
    app = ApplicationBuilder().token(token).build()

    battle_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(battle_info, pattern="^battle$")],
        states={WAITING_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_nickname)]},
        fallbacks=[CommandHandler("cancel", cancel_battle)],
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(adm_search_start,           pattern="^adm_search$"),
            CallbackQueryHandler(adm_give_start,             pattern="^adm_give$"),
            CallbackQueryHandler(adm_take_start,             pattern="^adm_take$"),
            CallbackQueryHandler(adm_give_stars_start,       pattern="^adm_give_stars$"),
            CallbackQueryHandler(adm_take_stars_start,       pattern="^adm_take_stars$"),
            CallbackQueryHandler(adm_history_start,          pattern="^adm_history$"),
            CallbackQueryHandler(adm_broadcast_start,        pattern="^adm_broadcast$"),
            CallbackQueryHandler(adm_add_admin_start,        pattern="^adm_add_admin$"),
            CallbackQueryHandler(adm_create_promo_start,     pattern="^adm_create_promo$"),
            CallbackQueryHandler(adm_deactivate_promo_start, pattern="^adm_deactivate_promo$"),
            CallbackQueryHandler(adm_battle_channel_start,   pattern="^adm_battle_channel$"),
        ],
        states={
            ADMIN_SEARCH_INPUT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_search_input)],
            ADMIN_GIVE_USER:         [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_give_user)],
            ADMIN_GIVE_AMOUNT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_give_amount)],
            ADMIN_TAKE_USER:         [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_take_user)],
            ADMIN_TAKE_AMOUNT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_take_amount)],
            ADMIN_GIVE_STARS_USER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_give_stars_user)],
            ADMIN_GIVE_STARS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_give_stars_amount)],
            ADMIN_TAKE_STARS_USER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_take_stars_user)],
            ADMIN_TAKE_STARS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_take_stars_amount)],
            ADMIN_HISTORY_USER:      [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_history_input)],
            ADMIN_BROADCAST:         [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_broadcast_send)],
            ADMIN_ADD_ADMIN:         [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_admin_input)],
            ADMIN_PROMO_CODE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_promo_code)],
            ADMIN_PROMO_TYPE:        [CallbackQueryHandler(adm_promo_type, pattern="^pt_(tokens|stars)$")],
            ADMIN_PROMO_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_promo_amount)],
            ADMIN_PROMO_MAX:         [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_promo_max)],
            ADMIN_BATTLE_CHANNEL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_battle_channel_input)],
        },
        fallbacks=[CallbackQueryHandler(adm_cancel, pattern="^adm_cancel$")],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("promo", promo_command))
    app.add_handler(battle_conv)
    app.add_handler(admin_conv)

    # Admin static callbacks
    app.add_handler(CallbackQueryHandler(adm_stats,          pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(adm_top,            pattern="^adm_top$"))
    app.add_handler(CallbackQueryHandler(adm_admins_menu,    pattern="^adm_admins$"))
    app.add_handler(CallbackQueryHandler(adm_del_admin,      pattern=r"^adm_del_admin_\d+$"))
    app.add_handler(CallbackQueryHandler(adm_promos_menu,    pattern="^adm_promos$"))
    app.add_handler(CallbackQueryHandler(adm_battle_menu,    pattern="^adm_battle$"))
    app.add_handler(CallbackQueryHandler(adm_battle_start_cb,pattern="^adm_battle_start$"))
    app.add_handler(CallbackQueryHandler(adm_battle_stop_cb, pattern="^adm_battle_stop$"))
    app.add_handler(CallbackQueryHandler(adm_battle_clear_cb,pattern="^adm_battle_clear$"))

    # User callbacks
    app.add_handler(CallbackQueryHandler(show_balance,    pattern="^balance$"))
    app.add_handler(CallbackQueryHandler(buy_votes_menu,  pattern="^buy_votes$"))
    app.add_handler(CallbackQueryHandler(show_star_games, pattern="^star_games$"))
    app.add_handler(CallbackQueryHandler(show_dart,       pattern="^dart$"))
    app.add_handler(CallbackQueryHandler(show_dice_game,  pattern="^dice_game$"))
    app.add_handler(CallbackQueryHandler(show_casino,     pattern="^casino$"))
    app.add_handler(CallbackQueryHandler(back_main,       pattern="^back_main$"))

    # Telegram Stars payments
    app.add_handler(CallbackQueryHandler(pay_dart,     pattern="^pay_dart$"))
    app.add_handler(CallbackQueryHandler(pay_dice,     pattern="^pay_dice$"))
    app.add_handler(CallbackQueryHandler(pay_casino,   pattern="^pay_casino$"))
    app.add_handler(CallbackQueryHandler(pay_votes_1,  pattern="^pay_votes_1$"))
    app.add_handler(CallbackQueryHandler(pay_votes_10, pattern="^pay_votes_10$"))
    app.add_handler(CallbackQueryHandler(pay_votes_50, pattern="^pay_votes_50$"))

    # Balance Stars payments
    app.add_handler(CallbackQueryHandler(bal_dart,   pattern="^bal_dart$"))
    app.add_handler(CallbackQueryHandler(bal_dice,   pattern="^bal_dice$"))
    app.add_handler(CallbackQueryHandler(bal_casino, pattern="^bal_casino$"))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    logger.info("Bot started. Polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
