"""
Telegram Giveaway Voting Bot — FINAL (error-hardened)
======================================================
FEATURES
--------
Users:
  • /start -> "Do you want to participate in the giveaway?" + Yes button
  • Yes -> force-join check -> name moderation -> channel post + Vote button
  • Success message: View My Post + Join Channel + "ask friends to vote"
  • Name moderation: 200+ blocked words (drugs/porn/abuse/slurs/weapons/
    roman-urdu abuses) with word-boundary matching (no false positives)
  • Bad name -> change-name prompt OR appeal with admin username shown
  • Appeal goes to admins with Approve/Reject buttons

Voting:
  • Only channel members can vote (voting channel + force-join channels)
  • One user = one vote (DB primary key, duplicate impossible)
  • Live vote counter updates on the channel post
  • No self-voting

Admin (all inline buttons, panel via /admin):
  • Set Voting Channel (forward post / @username / -100 ID) — private+public
  • Force Join: ON/OFF, add/remove channels — private+public, auto invite links
  • New Voting Round, Open/Close Participation, Open/Close Voting
  • Stats (with user_ids), Top 10, End Vote (winner), Cancel Round
  • Add/Remove votes on any participant (live counter update)
  • Broadcast to all participants
  • Pending name reviews with Approve/Reject

SAFETY
------
  • safe_edit() — never crashes on "message not modified"
  • short() — UTF-16 aware truncation (Telegram 64-unit button limit)
  • Defensive admin_panel_kb — one bad query can't kill the panel
  • Global error handler — logs full traceback + DMs it to every admin
  • Startup DB verification — detects corrupt/locked databases

ENTRY COMMANDS: /start (users), /admin (admins)
SETUP: 1) fill CONFIG below  2) make bot admin in all channels
       3) pip install -r requirements.txt  4) python bot.py
"""

import asyncio
import logging
import re
import time
import traceback

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

# ==================== CONFIG ====================
BOT_TOKEN = "8973288899:AAFvr6VXb9t1-lcaJWv-0_RViNs9zFt2Zow"   # from @BotFather
ADMIN_IDS = [8913693655]                        # your numeric Telegram user ID(s)
BOT_USERNAME = "PrinceXgiveawaybot"             # without @
ADMIN_USERNAME = "@PrinceXDaemon"         # shown on name-review appeals
DB_NAME = "voting_bot.db"
# ===============================================

# ==================== NAME MODERATION ====================
BANNED_WORDS = [
    # drugs
    "drug", "drugs", "cocaine", "heroin", "meth", "methamphetamine",
    "weed", "cannabis", "marijuana", "ganja", "hashish", "opium",
    "mdma", "ecstasy", "lsd", "narcotic", "narcotics",
    "pilldealer", "drugdealer", "cartel",
    # porn / sexual
    "porn", "porno", "pornhub", "xnxx", "xvideos", "redtube",
    "onlyfans", "nude", "nudes", "sex", "sexy", "horny", "xxx",
    "boobs", "tits", "dick", "cock", "pussy", "slut", "whore",
    "fuck", "escort", "camgirl", "milf", "hentai", "nsfw", "18+",
    "sugar", "naked", "blowjob", "anal", "orgy", "threesome",
    "nipples", "vagina", "penis",
    # English profanity / leetspeak
    "bitch", "bastard", "asshole", "motherfucker", "fucker",
    "cunt", "dickhead", "shithead", "dumbass", "jackass",
    "retard", "faggot", "fag", "dyke", "twat", "wanker",
    "cocksucker", "ballsack", "bellend", "bollocks", "prick",
    "piss", "scumbag", "skank", "knob", "jerkoff", "cum",
    "fuk", "fukk", "fking", "fck", "f*ck", "fvck", "phuck",
    "mofo", "son of a bitch", "sonofabitch",
    # racial slurs
    "nigga", "nigger", "nig", "negro", "chink", "gook", "spic",
    "kike", "paki",
    # weapons / violence
    "gun", "guns", "rifle", "ak47", "ak-47", "pistol", "revolver",
    "sniper", "shotgun", "bomb", "bomber", "grenade", "missile",
    "weapon", "weapons", "ammo", "ammunition", "shooter", "shooting",
    "killer", "kill", "murder", "murderer", "terror", "terrorist",
    "terrorism", "isis", "taliban", "alqaeda", "extremist", "suicide",
    # abuse phrases
    "mother fuck", "mother fucker", "fuck you", "fuck u", "fuck off",
    "fuck your mom", "fuck your mother", "fuck ur mom", "fuk you",
    "shut the fuck up", "stfu", "gtfo", "kiss my ass", "eat shit",
    "dipshit", "dickface", "pussyboy", "bitchboy",
    # roman-urdu / hindi abuses
    "randi", "randii", "kanjar", "kanjri", "bhenchod", "madarchod",
    "chutiya", "chut", "bhosdi", "bhosdike", "lund", "loda", "lavda",
    "gaand", "gandu", "gaandu", "harami", "haramkhor", "kamina",
    "chakka", "hijra", "kutta", "kutti", "kutte",
    "bhadwa", "bhadvi", "bhadwe", "chodu", "chodon", "bhosda",
    "bhosdi ke", "chut ke", "chut ke baal", "laude", "laude ke",
    "landi", "gaand mara", "muth", "mutha", "muthal", "matherchod",
    "bhen ke lode", "bhen ke laude", "teri maa", "teri maa ki",
    "teri behen", "teri gaand", "randi khana", "randi ka", "randi ke",
    "suar", "suar ki", "suar ka", "harami ke", "haramkhor ki",
    "chutmar", "chutmarika", "bosi da", "bosi de",
    # bad girl/boy names
    "thot", "hoe", "hooker", "prostitute", "pimp", "stripper",
    "rapist", "hitman", "badmash", "dagga", "charsi", "chars",
]

def _make_pattern(word):
    if re.fullmatch(r"[a-z0-9]+", word):
        return re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE), word
    return re.compile(re.escape(word), re.IGNORECASE), word

_BANNED_PATTERNS = [_make_pattern(w) for w in BANNED_WORDS]

def name_is_violating(full_name):
    return [orig for pat, orig in _BANNED_PATTERNS if pat.search(full_name)]
# =======================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voting-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================== SMALL HELPERS ====================
def short(t, n=40):
    """UTF-16 aware truncation (Telegram button limit: 64 units)."""
    t = str(t or "")
    out, used = [], 0
    for ch in t:
        units = 2 if ord(ch) > 0xFFFF else 1
        if used + units > n:
            return "".join(out) + "…"
        out.append(ch)
        used += units
    return t

async def safe_edit(message, text, kb=None, **kw):
    try:
        return await message.edit_text(text, reply_markup=kb, **kw)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning(f"edit_text: {e}")
        return None
    except Exception as e:
        log.warning(f"edit_text: {e}")
        return None

def is_admin(uid):
    return uid in ADMIN_IDS


# ==================== DATABASE ====================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                status TEXT DEFAULT 'active',
                participate_open INTEGER DEFAULT 1,
                vote_open INTEGER DEFAULT 1,
                created_at REAL);
            CREATE TABLE IF NOT EXISTS participants (
                round_id INTEGER,
                user_id INTEGER,
                name TEXT,
                username TEXT,
                post_msg_id INTEGER,
                votes INTEGER DEFAULT 0,
                joined_at REAL,
                PRIMARY KEY (round_id, user_id));
            CREATE TABLE IF NOT EXISTS votes (
                round_id INTEGER,
                voter_id INTEGER,
                target_id INTEGER,
                voted_at REAL,
                PRIMARY KEY (round_id, voter_id));
            CREATE TABLE IF NOT EXISTS name_reviews (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                status TEXT DEFAULT 'pending',
                requested_at REAL);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT);
            CREATE TABLE IF NOT EXISTS force_channels (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                invite_link TEXT);""")
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cur.fetchall()}
    required = {"rounds", "participants", "votes", "name_reviews",
                "settings", "force_channels"}
    missing = required - tables
    if missing:
        raise RuntimeError(
            f"DB corrupt/locked, missing tables: {missing}. "
            f"Stop all bot instances, delete {DB_NAME}, restart.")

async def get_setting(key, default=None):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

async def set_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
            (key, str(value)))
        await db.commit()

async def list_force_channels():
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT * FROM force_channels")
        return await cur.fetchall()

async def remove_force_channel(chat_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM force_channels WHERE chat_id=?", (chat_id,))
        await db.commit()

async def get_active_round():
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, title, participate_open, vote_open FROM rounds "
            "WHERE status='active' ORDER BY id DESC LIMIT 1")
        return await cur.fetchone()

async def set_round(rid, field, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE rounds SET {field}=? WHERE id=?", (value, rid))
        await db.commit()

async def get_review_status(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT status FROM name_reviews WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def request_review(user_id, name):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO name_reviews (user_id,name,status,requested_at) "
            "VALUES (?,?,'pending',?)", (user_id, name, time.time()))
        await db.commit()

async def add_participant(rid, uid, name, username, msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO participants (round_id,user_id,name,username,post_msg_id,votes,joined_at) "
                "VALUES (?,?,?,?,?,0,?)", (rid, uid, name, username, msg_id, time.time()))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def add_vote(rid, voter_id, target_id):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO votes VALUES (?,?,?,?)",
                             (rid, voter_id, target_id, time.time()))
            await db.execute(
                "UPDATE participants SET votes=votes+1 WHERE round_id=? AND user_id=?",
                (rid, target_id))
            cur = await db.execute(
                "SELECT votes, post_msg_id FROM participants "
                "WHERE round_id=? AND user_id=?", (rid, target_id))
            votes, msg_id = await cur.fetchone()
            await db.commit()
            return True, votes, msg_id
        except aiosqlite.IntegrityError:
            return False, 0, None

async def adjust_votes(rid, target_id, delta):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT votes, post_msg_id FROM participants "
            "WHERE round_id=? AND user_id=?", (rid, target_id))
        row = await cur.fetchone()
        if not row:
            return None
        new_votes = max(0, row[0] + delta)
        await db.execute(
            "UPDATE participants SET votes=? WHERE round_id=? AND user_id=?",
            (new_votes, rid, target_id))
        await db.commit()
        return new_votes, row[1]

async def get_stats(rid):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(votes),0) FROM participants WHERE round_id=?",
            (rid,))
        p_count, v_sum = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM votes WHERE round_id=?", (rid,))
        voters = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT name, votes, user_id FROM participants WHERE round_id=? "
            "ORDER BY votes DESC LIMIT 10", (rid,))
        top = await cur.fetchall()
        return p_count, v_sum, voters, top


# ==================== CHANNEL HELPERS ====================
BOT_ID = None

async def is_member_of(chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.CREATOR)
    except Exception:
        return False

async def check_membership(user_id):
    """Force-join check ONLY (voting channel NOT required to participate).
    Returns list of (title, link) for force channels not joined."""
    missing = []
    if (await get_setting("force_join", "0")) == "1":
        for cid, title, uname, link in await list_force_channels():
            if not await is_member_of(cid, user_id):
                if not link:
                    link = f"https://t.me/{uname}" if uname else "https://t.me/"
                missing.append((title, link))
    return missing

async def channel_view_url(msg_id):
    uname = await get_setting("vote_channel_username")
    cid = await get_setting("vote_channel_id")
    if uname:
        return f"https://t.me/{uname}/{msg_id}"
    if cid:
        return f"https://t.me/c/{str(cid).replace('-100', '', 1)}/{msg_id}"
    return "https://t.me/"

async def resolve_channel(chat_id):
    """Validate access + bot admin rights, build invite link.
    Returns ((chat, invite), None) on success or (None, error) ."""
    global BOT_ID
    if BOT_ID is None:
        BOT_ID = (await bot.get_me()).id
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        return None, "Cannot access that chat. Is the bot ADDED AS ADMIN in the channel?"
    try:
        member = await bot.get_chat_member(chat_id, BOT_ID)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return None, "Bot is not an admin in that channel. Make it an admin first."
    except Exception:
        return None, "Could not verify bot rights in that channel."
    invite = None
    try:
        invite = (await bot.create_chat_invite_link(chat_id)).invite_link
    except Exception:
        if chat.username:
            invite = f"https://t.me/{chat.username}"
    return (chat, invite), None


# ==================== FSM ====================
class AdminFlow(StatesGroup):
    set_vote_channel = State()
    add_force_channel = State()
    new_vote_title = State()
    broadcast = State()
    adjust_user = State()
    adjust_amount = State()


# ==================== KEYBOARDS ====================
def user_menu_kb(admin_user):
    rows = [[InlineKeyboardButton(text="✅ Yes, I want to participate",
                                  callback_data="participate_yes")]]
    if admin_user:
        rows.append([InlineKeyboardButton(text="🛠 Admin Panel",
                                          callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def name_violation_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="My name is fine - Request Admin Review",
                              callback_data="name_review")],
        [InlineKeyboardButton(text="I changed my name - Try Again",
                              callback_data="participate_yes")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_back")]])

def join_channels_kb(missing):
    rows = [[InlineKeyboardButton(text=f"📢 Join: {short(title, 28)}", url=link)]
            for title, link in missing]
    rows.append([InlineKeyboardButton(text="🔄 I Joined - Try Again",
                                      callback_data="participate_yes")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def vote_kb(rid, target_id, votes):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗳️ Vote ({votes})",
                              callback_data=f"vote_{rid}_{target_id}")],
        [InlineKeyboardButton(text="🤖 Participate via Bot",
                              url=f"https://t.me/{BOT_USERNAME}")]])

def admin_review_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"nrapprove_{user_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"nrreject_{user_id}")]])

def cancel_only_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel")]])

def confirm_kb(action):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"cf_{action}"),
        InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")]])

def retry_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Retry", callback_data="admin_panel")]])

async def admin_panel_kb():
    r = fj = vch = None
    fj_on, fj_count, pending = False, 0, 0
    try:
        r = await get_active_round()
    except Exception as e:
        log.error(f"panel: rounds check: {e}")
    try:
        fj_on = (await get_setting("force_join", "0")) == "1"
        vch = await get_setting("vote_channel_title")
    except Exception as e:
        log.error(f"panel: settings: {e}")
    try:
        fj_count = len(await list_force_channels())
    except Exception as e:
        log.error(f"panel: force channels: {e}")
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM name_reviews WHERE status='pending'")
            pending = (await cur.fetchone())[0]
    except Exception as e:
        log.error(f"panel: reviews count: {e}")

    rows = [[InlineKeyboardButton(
        text=f"🗳️ Voting Channel: {short(vch) if vch else 'NOT SET'}",
        callback_data="ap_setchannel")]]
    rows.append([InlineKeyboardButton(
        text=f"🔐 Force Join: {'ON' if fj_on else 'OFF'} ({fj_count} ch)",
        callback_data="ap_forcejoin"),
        InlineKeyboardButton(text="➕ Add FJ Channel", callback_data="ap_fj_add")])
    rows.append([InlineKeyboardButton(text="➖ Remove FJ Channel",
                                      callback_data="ap_fj_remove")])
    rows.append([InlineKeyboardButton(text="🎁 New Voting Round",
                                      callback_data="ap_newvote")])
    if r:
        rows.append([InlineKeyboardButton(
            text=f"{'🔒' if r[2] else '🔓'} Participation: {'OPEN' if r[2] else 'CLOSED'}",
            callback_data="ap_toggle_part"),
            InlineKeyboardButton(
            text=f"{'🔒' if r[3] else '🔓'} Voting: {'OPEN' if r[3] else 'CLOSED'}",
            callback_data="ap_toggle_vote")])
        rows.append([InlineKeyboardButton(text="📊 Stats", callback_data="ap_stats"),
                     InlineKeyboardButton(text="🏆 Top 10", callback_data="ap_top")])
        rows.append([InlineKeyboardButton(text="➕ Add Votes", callback_data="ap_addvotes"),
                     InlineKeyboardButton(text="➖ Remove Votes",
                                          callback_data="ap_remvotes")])
        rows.append([InlineKeyboardButton(text="⏹ End Vote & Winner",
                                          callback_data="ap_end"),
                     InlineKeyboardButton(text="❌ Cancel Round",
                                          callback_data="ap_cancel")])
        rows.append([InlineKeyboardButton(text="📢 Broadcast to Participants",
                                          callback_data="ap_broadcast")])
    rows.append([InlineKeyboardButton(
        text=f"📝 Name Reviews ({pending} pending)", callback_data="ap_reviews")])
    rows.append([InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== USER FLOW ====================
@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Do you want to participate in the giveaway?",
                   reply_markup=user_menu_kb(is_admin(m.from_user.id)))

@dp.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("You are not an admin.")
    await state.clear()
    try:
        kb = await admin_panel_kb()
    except Exception:
        log.exception("admin panel build failed")
        kb = retry_kb()
    await m.answer("🛠 <b>Admin Panel</b>", reply_markup=kb)

@dp.callback_query(F.data == "menu_back")
async def cb_back(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cq.message, "Do you want to participate in the giveaway?",
                    kb=user_menu_kb(is_admin(cq.from_user.id)))
    await cq.answer()

@dp.callback_query(F.data == "participate_yes")
async def cb_yes(cq: CallbackQuery):
    user = cq.from_user
    r = await get_active_round()
    if not r:
        await cq.answer("No giveaway voting is active right now.", show_alert=True)
        return
    rid, title, p_open, _ = r
    if not p_open:
        await cq.answer("Participation is currently CLOSED.", show_alert=True)
        return

    missing = await check_membership(user.id)
    if missing:
        await safe_edit(cq.message,
            "🔐 <b>Join Required!</b>\n\nJoin the channel(s) below first, "
            "then press <b>Try Again</b>:", kb=join_channels_kb(missing))
        await cq.answer("Join the channels first!")
        return

    found = name_is_violating(user.full_name)
    if found:
        review = await get_review_status(user.id)
        if review != "approved":
            if review is None:
                await request_review(user.id, user.full_name)
            words = ", ".join(found)
            await safe_edit(cq.message,
                "⚠️ <b>Name Review Required</b>\n\n"
                "Your Telegram name contains words that violate our rules "
                f"(<b>{words}</b>).\n\n"
                "Change your name first (Settings → Edit Profile), then press "
                "<b>Try Again</b>.\n\n"
                "If you believe this is a mistake, request an admin review — "
                "you can only participate after an admin approves your name.\n\n"
                f"👮 Admin: @{ADMIN_USERNAME}",
                kb=name_violation_kb())
            await cq.answer("Name review required.", show_alert=True)
            return

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT 1 FROM participants WHERE round_id=? AND user_id=?",
            (rid, user.id))
        if await cur.fetchone():
            await cq.answer("You have already participated!", show_alert=True)
            return

    vch = await get_setting("vote_channel_id")
    if not vch:
        await cq.answer("Voting channel not set yet. Ask the admin.", show_alert=True)
        return
    vch = int(vch)
    tag = f"@{user.username}" if user.username else user.full_name
    text = (f"🎉 <b>{tag}</b> is participating in the giveaway!\n"
            f"🎁 Prize: {title}\n\nVote for them below! 👇")
    try:
        sent = await bot.send_message(vch, text, reply_markup=vote_kb(rid, user.id, 0))
    except Exception as e:
        log.error(f"channel post failed: {e}")
        await cq.answer("Error: is the bot an admin in the voting channel?",
                        show_alert=True)
        return

    await add_participant(rid, user.id, user.full_name, user.username or "",
                          sent.message_id)
    url = await channel_view_url(sent.message_id)
    join_link = await get_setting("vote_channel_link") or url
    ch_title = await get_setting("vote_channel_title", "our channel")
    await safe_edit(cq.message,
        "✅ <b>You are in!</b> Your name and vote button are now posted on the channel.\n\n"
        "Ask your friends to open the channel and vote for you! 🗳️",
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔎 View My Post & Votes", url=url)],
            [InlineKeyboardButton(text=f"📢 Join {short(ch_title)}", url=join_link)],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="menu_back")]]))
    await cq.answer("Done!")


# ==================== NAME REVIEW ====================
@dp.callback_query(F.data == "name_review")
async def cb_review(cq: CallbackQuery):
    user = cq.from_user
    review = await get_review_status(user.id)
    if review == "approved":
        await cq.answer("Already approved! Press Try Again.", show_alert=True)
        return
    if review == "pending":
        await cq.answer("Your request is already pending. Please wait.", show_alert=True)
        return
    await request_review(user.id, user.full_name)
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid,
                f"📝 <b>Name Review Request</b>\n\n"
                f"User: {user.full_name} (ID: <code>{user.id}</code>)\n"
                f"Username: @{user.username or 'none'}\n\n"
                f"Insists the name is fine and wants to participate.",
                reply_markup=admin_review_kb(user.id))
        except Exception:
            pass
    await safe_edit(cq.message,
        "📨 Your request was sent to the admins. You can participate once an "
        "admin approves your name.")
    await cq.answer("Request sent.")

@dp.callback_query(F.data.startswith("nrapprove_"))
async def cb_approve(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True)
        return
    uid = int(cq.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE name_reviews SET status='approved' WHERE user_id=?",
                         (uid,))
        await db.commit()
    await safe_edit(cq.message, cq.message.text + "\n\n✅ <b>APPROVED</b>")
    try:
        await bot.send_message(uid, "🎉 An admin <b>approved</b> your name review. "
                                     "Open the bot and participate now!")
    except Exception:
        pass
    await cq.answer("Approved.")

@dp.callback_query(F.data.startswith("nrreject_"))
async def cb_reject(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True)
        return
    uid = int(cq.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE name_reviews SET status='rejected' WHERE user_id=?",
                         (uid,))
        await db.commit()
    await safe_edit(cq.message, cq.message.text + "\n\n❌ <b>REJECTED</b>")
    try:
        await bot.send_message(uid, "❌ Your name review was <b>rejected</b>. "
                                    "Change your Telegram name and try again.")
    except Exception:
        pass
    await cq.answer("Rejected.")


# ==================== VOTING ====================
@dp.callback_query(F.data.startswith("vote_"))
async def cb_vote(cq: CallbackQuery):
    _, rid, target_id = cq.data.split("_")
    rid, target_id = int(rid), int(target_id)
    user = cq.from_user
    r = await get_active_round()
    if not r or r[0] != rid:
        await cq.answer("This voting round has ended.", show_alert=True)
        return
    if not r[3]:
        await cq.answer("Voting is currently closed.", show_alert=True)
        return

    missing = await check_membership(user.id)
    vch = await get_setting("vote_channel_id")
    if vch and not await is_member_of(int(vch), user.id):
        title = await get_setting("vote_channel_title", "Voting Channel")
        link = await get_setting("vote_channel_link")
        if not link:
            un = await get_setting("vote_channel_username")
            link = f"https://t.me/{un}" if un else "https://t.me/"
        missing.append((title, link))
    if missing:
        try:
            await bot.send_message(
                user.id,
                "🔐 <b>Join Required to Vote!</b>\n\n"
                "Join the channel(s) below, then vote again on the post:",
                reply_markup=join_channels_kb(missing))
        except Exception:
            pass
        await cq.answer("Join the channels first (bot sent you the links)!",
                        show_alert=True)
        return
    if user.id == target_id:
        await cq.answer("You cannot vote for yourself!", show_alert=True)
        return

    ok, votes, msg_id = await add_vote(rid, user.id, target_id)
    if not ok:
        await cq.answer("You have already voted! One user = one vote only.",
                        show_alert=True)
        return
    if msg_id and vch:
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(vch), message_id=msg_id,
                reply_markup=vote_kb(rid, target_id, votes))
        except Exception:
            pass
    await cq.answer(f"🗳️ Vote counted! Total votes: {votes}")


# ==================== ADMIN PANEL ====================
@dp.callback_query(F.data == "admin_panel")
async def cb_panel(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True)
        return
    await state.clear()
    try:
        kb = await admin_panel_kb()
    except Exception:
        log.exception("admin panel build crashed")
        kb = retry_kb()
    await safe_edit(cq.message, "🛠 <b>Admin Panel</b>", kb=kb)
    await cq.answer()

@dp.callback_query(F.data == "ap_setchannel")
async def ap_setchannel(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.set_vote_channel)
    await safe_edit(cq.message,
        "🗳️ <b>Set Voting Channel</b>\n\nForward any message FROM the channel, "
        "or send its <b>@username</b> / <code>-100xxxxxxxxxx</code> ID.\n\n"
        "⚠️ Bot must be an <b>admin</b> there (private & public both work).",
        kb=cancel_only_kb())
    await cq.answer()

async def _handle_channel_input(m: Message, save_to):
    """Shared logic for set-vote-channel and add-force-channel."""
    txt = (m.text or "").strip()
    chat_id = None
    if m.forward_from_chat is not None:
        chat_id = m.forward_from_chat.id
    elif txt.startswith("@"):
        try:
            chat_id = (await bot.get_chat(txt)).id
        except Exception:
            return False, "Could not find that channel. Is the bot a member?"
    elif txt.lstrip("-").isdigit():
        chat_id = int(txt)
    else:
        return False, "Please forward a channel post or send @username / -100 ID."
    resolved, err = await resolve_channel(chat_id)
    if err:
        return False, err
    chat, invite = resolved
    await save_to(chat, invite)
    return True, chat.title or str(chat_id)

@dp.message(AdminFlow.set_vote_channel)
async def svc_input(m: Message, state: FSMContext):
    async def save(chat, invite):
        await set_setting("vote_channel_id", chat.id)
        await set_setting("vote_channel_title", chat.title or "")
        await set_setting("vote_channel_username", chat.username or "")
        await set_setting("vote_channel_link", invite or "")
    ok, info = await _handle_channel_input(m, save)
    await state.clear()
    if ok:
        await m.answer(f"✅ Voting channel set to <b>{info}</b>!",
                       reply_markup=await admin_panel_kb())
    else:
        await m.answer(f"❌ {info}", reply_markup=await admin_panel_kb())

@dp.callback_query(F.data == "ap_forcejoin")
async def ap_fj_toggle(cq: CallbackQuery):
    cur = (await get_setting("force_join", "0")) == "1"
    await set_setting("force_join", "0" if cur else "1")
    await safe_edit(cq.message, "🛠 <b>Admin Panel</b>", kb=await admin_panel_kb())
    await cq.answer(f"Force Join {'ON' if not cur else 'OFF'}")

@dp.callback_query(F.data == "ap_fj_add")
async def ap_fj_add(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.add_force_channel)
    await safe_edit(cq.message,
        "➕ <b>Add Force-Join Channel</b>\n\nForward a message FROM the channel "
        "or send @username / -100 ID.\n⚠️ Bot must be admin there.",
        kb=cancel_only_kb())
    await cq.answer()

@dp.message(AdminFlow.add_force_channel)
async def fjc_input(m: Message, state: FSMContext):
    async def save(chat, invite):
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR REPLACE INTO force_channels VALUES (?,?,?,?)",
                (chat.id, chat.title or "", chat.username or "", invite or ""))
            await db.commit()
    ok, info = await _handle_channel_input(m, save)
    await state.clear()
    if ok:
        await m.answer(f"✅ Added <b>{info}</b> to force-join list.",
                       reply_markup=await admin_panel_kb())
    else:
        await m.answer(f"❌ {info}", reply_markup=await admin_panel_kb())

@dp.callback_query(F.data == "ap_fj_remove")
async def ap_fj_remove(cq: CallbackQuery):
    chans = await list_force_channels()
    if not chans:
        await cq.answer("No force-join channels added yet.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=f"🗑 {short(title)}",
                                  callback_data=f"fjdel_{cid}")]
            for cid, title, _, _ in chans]
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")])
    await safe_edit(cq.message, "➖ <b>Remove Force-Join Channel</b>:",
                    kb=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

@dp.callback_query(F.data.startswith("fjdel_"))
async def fj_del(cq: CallbackQuery):
    await remove_force_channel(int(cq.data.split("_")[1]))
    await safe_edit(cq.message, "🛠 <b>Admin Panel</b>", kb=await admin_panel_kb())
    await cq.answer("Removed.")

@dp.callback_query(F.data == "ap_newvote")
async def ap_newvote(cq: CallbackQuery, state: FSMContext):
    if not await get_setting("vote_channel_id"):
        await cq.answer("Set the voting channel first!", show_alert=True)
        return
    if await get_active_round():
        await cq.answer("A round is already active. End/Cancel it first.",
                        show_alert=True)
        return
    await state.set_state(AdminFlow.new_vote_title)
    await safe_edit(cq.message, "🎁 <b>New Voting Round</b>\n\nWhat is the prize/title?",
                    kb=cancel_only_kb())
    await cq.answer()

@dp.message(AdminFlow.new_vote_title)
async def nvt(m: Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("INSERT INTO rounds (title, created_at) VALUES (?,?)",
                               (m.text, time.time()))
        await db.commit()
        rid = cur.lastrowid
    vch = int(await get_setting("vote_channel_id"))
    await bot.send_message(
        vch,
        f"🎁 <b>{m.text}</b>\n\nTo participate, open the bot:\n@{BOT_USERNAME}")
    await m.answer(f"✅ Round #{rid} started! Participation & Voting are ON.",
                   reply_markup=await admin_panel_kb())

@dp.callback_query(F.data == "ap_toggle_part")
async def ap_tp(cq: CallbackQuery):
    r = await get_active_round()
    if r:
        await set_round(r[0], "participate_open", 0 if r[2] else 1)
    await safe_edit(cq.message, "🛠 <b>Admin Panel</b>", kb=await admin_panel_kb())
    await cq.answer("Toggled.")

@dp.callback_query(F.data == "ap_toggle_vote")
async def ap_tv(cq: CallbackQuery):
    r = await get_active_round()
    if r:
        await set_round(r[0], "vote_open", 0 if r[3] else 1)
    await safe_edit(cq.message, "🛠 <b>Admin Panel</b>", kb=await admin_panel_kb())
    await cq.answer("Toggled.")

@dp.callback_query(F.data == "ap_stats")
async def ap_stats(cq: CallbackQuery):
    r = await get_active_round()
    if not r:
        await cq.answer("No active round.", show_alert=True)
        return
    p, v_sum, voters, top = await get_stats(r[0])
    lines = [f"📊 <b>Round #{r[0]} – {r[1]}</b>\n",
             f"👥 Participants: {p}",
             f"🗳️ Votes cast: {voters}",
             f"🔢 Vote points: {v_sum}\n", "<b>Top 10:</b>"]
    for i, (name, votes, uid) in enumerate(top, 1):
        lines.append(f"{i}. {name} — {votes} votes (id: <code>{uid}</code>)")
    await safe_edit(cq.message, "\n".join(lines), kb=await admin_panel_kb())
    await cq.answer()

@dp.callback_query(F.data == "ap_top")
async def ap_top(cq: CallbackQuery):
    r = await get_active_round()
    if not r:
        await cq.answer("No active round.", show_alert=True)
        return
    _, _, _, top = await get_stats(r[0])
    if not top:
        await cq.answer("No participants yet.", show_alert=True)
        return
    lines = [f"🏆 <b>Top Participants – {r[1]}</b>\n"]
    for i, (name, votes, _) in enumerate(top, 1):
        lines.append(f"{i}. {name} — {votes} 🗳️")
    await safe_edit(cq.message, "\n".join(lines), kb=await admin_panel_kb())
    await cq.answer()

@dp.callback_query(F.data == "ap_end")
async def ap_end(cq: CallbackQuery):
    if not await get_active_round():
        await cq.answer("No active round.", show_alert=True)
        return
    await safe_edit(cq.message, "⚠️ End this round and announce the winner?",
                    kb=confirm_kb("end"))
    await cq.answer()

@dp.callback_query(F.data == "ap_cancel")
async def ap_cancel(cq: CallbackQuery):
    if not await get_active_round():
        await cq.answer("No active round.", show_alert=True)
        return
    await safe_edit(cq.message, "⚠️ Cancel this round?", kb=confirm_kb("cancel"))
    await cq.answer()

@dp.callback_query(F.data.startswith("cf_"))
async def cf_action(cq: CallbackQuery):
    action = cq.data.split("_")[1]
    r = await get_active_round()
    if not r:
        await cq.answer("No active round.", show_alert=True)
        return
    rid, title = r[0], r[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE rounds SET status=? WHERE id=?",
                         ("ended" if action == "end" else "cancelled", rid))
        await db.commit()
    vch = int(await get_setting("vote_channel_id"))
    if action == "end":
        p, _, voters, top = await get_stats(rid)
        if top:
            winner, wv, _ = top[0]
            await bot.send_message(
                vch,
                f"🏁 <b>{title} – VOTING ENDED!</b>\n\n"
                f"🎉 WINNER: <b>{winner}</b> with {wv} votes!\n\n"
                f"👥 Participants: {p}\n🗳️ Total votes: {voters}\n\n"
                f"Congratulations! 🎊")
            await safe_edit(cq.message,
                            f"✅ Winner announced: <b>{winner}</b> ({wv} votes)",
                            kb=await admin_panel_kb())
        else:
            await bot.send_message(vch, f"🏁 <b>{title}</b> ended – no participants. 😔")
            await safe_edit(cq.message, "Round ended with no participants.",
                            kb=await admin_panel_kb())
    else:
        await safe_edit(cq.message, "❌ Round cancelled.", kb=await admin_panel_kb())
    await cq.answer()

@dp.callback_query(F.data == "ap_broadcast")
async def ap_broadcast(cq: CallbackQuery, state: FSMContext):
    if not await get_active_round():
        await cq.answer("No active round.", show_alert=True)
        return
    await state.set_state(AdminFlow.broadcast)
    await safe_edit(cq.message,
        "📢 <b>Broadcast</b>\n\nSend me any message now — it will be delivered "
        "to ALL participants of the active round.", kb=cancel_only_kb())
    await cq.answer()

@dp.message(AdminFlow.broadcast)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    r = await get_active_round()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id FROM participants WHERE round_id=?",
                               (r[0],))
        users = [x[0] for x in await cur.fetchall()]
    ok, fail = 0, 0
    for uid in users:
        try:
            await m.copy_to(uid)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await m.answer(f"📢 Broadcast finished: {ok} delivered, {fail} failed.",
                   reply_markup=await admin_panel_kb())

@dp.callback_query(F.data.in_({"ap_addvotes", "ap_remvotes"}))
async def ap_adjust(cq: CallbackQuery, state: FSMContext):
    if not await get_active_round():
        await cq.answer("No active round.", show_alert=True)
        return
    mode = "add" if cq.data == "ap_addvotes" else "remove"
    await state.update_data(adj_mode=mode)
    await state.set_state(AdminFlow.adjust_user)
    await safe_edit(cq.message,
        f"{'➕' if mode == 'add' else '➖'} <b>{mode.capitalize()} Votes</b>\n\n"
        f"Send the participant's <code>user_id</code> (see Stats):",
        kb=cancel_only_kb())
    await cq.answer()

@dp.message(AdminFlow.adjust_user)
async def adj_user(m: Message, state: FSMContext):
    if not (m.text or "").strip().lstrip("-").isdigit():
        await m.answer("❌ Send a numeric user_id.", reply_markup=cancel_only_kb())
        return
    await state.update_data(adj_uid=int(m.text.strip()))
    await state.set_state(AdminFlow.adjust_amount)
    await m.answer("Now send the <b>amount</b> of votes:",
                   reply_markup=cancel_only_kb())

@dp.message(AdminFlow.adjust_amount)
async def adj_amount(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        await m.answer("❌ Send a number.", reply_markup=cancel_only_kb())
        return
    data = await state.get_data()
    await state.clear()
    uid, amount = data["adj_uid"], int(m.text.strip())
    delta = amount if data["adj_mode"] == "add" else -amount
    r = await get_active_round()
    res = await adjust_votes(r[0], uid, delta)
    if not res:
        await m.answer(f"❌ User <code>{uid}</code> is not a participant.",
                       reply_markup=await admin_panel_kb())
        return
    new_votes, msg_id = res
    vch = await get_setting("vote_channel_id")
    if msg_id and vch:
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(vch), message_id=msg_id,
                reply_markup=vote_kb(r[0], uid, new_votes))
        except Exception:
            pass
    await m.answer(f"✅ User <code>{uid}</code> now has <b>{new_votes}</b> votes.",
                   reply_markup=await admin_panel_kb())

@dp.callback_query(F.data == "ap_reviews")
async def ap_reviews(cq: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, name FROM name_reviews WHERE status='pending'")
        rows = await cur.fetchall()
    if not rows:
        await cq.answer("No pending name reviews.", show_alert=True)
        return
    for uid, name in rows:
        await cq.message.answer(f"📝 Review: <b>{name}</b> (ID: <code>{uid}</code>)",
                                reply_markup=admin_review_kb(uid))
    await cq.answer()


# ==================== GLOBAL ERROR HANDLER ====================
@dp.error()
async def global_error(event):
    exc = getattr(event, "exception", None)
    try:
        tb = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))[-1800:] if exc else "unknown"
    except Exception:
        tb = str(exc)
    log.error(f"Unhandled error: {exc}\n{tb}")
    try:
        obj = event.update.callback_query or event.update.message
        if isinstance(obj, CallbackQuery):
            await obj.answer("⚠️ Something went wrong. The admin has been notified.",
                             show_alert=True)
        elif obj:
            await obj.answer("⚠️ Something went wrong. The admin has been notified.")
    except Exception:
        pass
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid,
                f"🐛 <b>Bot Error</b>\n\n<code>{tb}</code>\n\n"
                f"Checklist: ONE bot instance only • DB file writable • "
                f"bot is admin in the channel • channel set in Admin Panel.")
        except Exception:
            pass
    return True


# ==================== RUN ====================
async def main():
    try:
        await db_init()
    except Exception as e:
        log.error(f"DATABASE INIT FAILED: {e}")
        log.error("-> Stop ALL bot processes, delete voting_bot.db, restart.")
        return
    log.info("Voting bot started.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
