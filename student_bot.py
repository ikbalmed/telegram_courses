import os
import re
import logging
from typing import List, Set, Dict, Optional, Union
from datetime import datetime, timedelta, date, time

from dotenv import load_dotenv
from telegram import Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    Defaults,
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # On 3.8, install backports.zoneinfo and import from there

load_dotenv()

# ========================= Logging =========================

logger = logging.getLogger("student_bot")
_log_level = logging.DEBUG if str(os.getenv("DEBUG", "0")).strip() in {"1", "true", "True"} else logging.INFO
logging.basicConfig(level=_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger.setLevel(_log_level)

# ========================= Config =========================

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")
STUDENT_TABLE_NAME = os.getenv("STUDENT_TABLE_NAME", "Students")
SUBJECTS_CHANNEL_TABLE_NAME = os.getenv("SUBJECTS_CHANNEL_TABLE_NAME", "Subjects_Channels")

ADMIN_IDS: Set[int] = {
    int(tok) for tok in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "").strip().strip("'").strip('"'))
    if tok.strip().lstrip("-").isdigit()
}

logger.info("Student bot starting with:")
logger.info(f"  SPREADSHEET_ID={SPREADSHEET_ID}")
logger.info(f"  STUDENT_TABLE_NAME={STUDENT_TABLE_NAME}")
logger.info(f"  SUBJECTS_CHANNEL_TABLE_NAME={SUBJECTS_CHANNEL_TABLE_NAME}")
logger.info(f"  DEBUG={'ON' if _log_level == logging.DEBUG else 'OFF'}")
if ADMIN_IDS:
    logger.info(f"  ADMIN_IDS (count={len(ADMIN_IDS)}): {sorted(ADMIN_IDS)}")

# Conversation states for /set flow
SET_NIVEAU, SET_SUBJECT, SET_CONFIRM = range(3)

# ===================== Google Sheets helpers =====================

def setup_sheets():
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    return service.spreadsheets()

def fetch_subject_channel_links() -> Dict[str, str]:
    """Return { '<niveau>_<subject>'.lower(): <telegram_group_id or ''> } from Subjects_Channels."""
    sheets = setup_sheets()
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{SUBJECTS_CHANNEL_TABLE_NAME}!A:B'
    ).execute()
    values = result.get('values', []) or []
    subject_channel_map: Dict[str, str] = {}
    for row in values[1:]:  # skip header
        if row and len(row) >= 2 and row[0]:
            subject_channel_map[str(row[0]).strip().lower()] = str(row[1]).strip() if len(row) > 1 else ""
    logger.debug(f"[fetch_subject_channel_links] Loaded {len(subject_channel_map)} keys.")
    return subject_channel_map

def _safe_cell(row: List[object], idx: int, default: object="") -> object:
    return row[idx] if idx != -1 and len(row) > idx else default

def _col_letter(idx_zero_based: int) -> str:
    s, n = "", idx_zero_based + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

def _norm(s: object) -> str:
    """normalize a header or token: lowercase, drop non-alphanumerics"""
    return re.sub(r'[^a-z0-9]', '', str(s).lower())

def _header_index(headers: List[str], target_name: str) -> int:
    norm = { _norm(h): i for i, h in enumerate(headers) }
    return norm.get(_norm(target_name), -1)

def _header_index_alias(
    headers: List[str],
    aliases: List[str],
    contains_any: Optional[List[str]] = None,
    contains_all: Optional[List[str]] = None
) -> int:
    """
    Find a column by aliases first; otherwise by 'contains' tokens (all or any).
    Tokens are normalized via _norm and matched as substrings.
    """
    hdr_norm = [ _norm(h) for h in headers ]
    # exact alias
    for al in aliases:
        try_idx = _header_index(headers, al)
        if try_idx != -1:
            return try_idx
    # contains all tokens
    if contains_all:
        toks = [ _norm(t) for t in contains_all ]
        for i, h in enumerate(hdr_norm):
            if all(t in h for t in toks):
                return i
    # contains any token
    if contains_any:
        toks = [ _norm(t) for t in contains_any ]
        for i, h in enumerate(hdr_norm):
            if any(t in h for t in toks):
                return i
    return -1

def _chat_id(value: Union[str, int]) -> Union[int, str]:
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return int(s) if s.lstrip("-").isdigit() else s

def _id_str_norm(value: object) -> str:
    """Normalize IDs e.g., 123456789.0 -> '123456789'."""
    try:
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value)) if value.is_integer() else str(value)
        s = str(value).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return str(value)

def update_sheet_cell(sheets, spreadsheet_id: str, sheet_name: str, col_idx: int, row_index: int, value: object):
    range_name = f'{sheet_name}!{_col_letter(col_idx)}{row_index}'
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption='RAW',
        body={'values': [[value]]}
    ).execute()

def _to_bool(v: object) -> bool:
    if v is True or v == 1:
        return True
    if v is False or v == 0 or v is None:
        return False
    return str(v).strip().upper() == "TRUE"

# ===================== Student data helpers =====================

def _get_student_subjects_and_niveau(student_id: str) -> Optional[Dict[str, object]]:
    """
    Reads Students sheet by headers and returns:
    { 'name': str, 'subjects': List[str], 'niveau': str, 'subscription': bool }
    Supports headers: "Student Name", "Student Subjects", "Niveau", "Subscription", "ID".
    """
    logger.debug(f"[_get_student_subjects_and_niveau] Start for student_id={student_id!r}")
    sheets = setup_sheets()
    res = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENT_TABLE_NAME}!A:Z",
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()
    rows = res.get("values", []) or []
    if len(rows) < 2:
        logger.debug("[_get_student_subjects_and_niveau] No rows or only header present.")
        return None

    headers = rows[0]
    logger.debug(f"[_get_student_subjects_and_niveau] Headers: {headers}")

    # Robust indices
    id_idx = _header_index_alias(headers, ["ID"], contains_any=["id"])
    name_idx = _header_index_alias(headers, ["Student Name", "Name"], contains_any=["name"])
    subjects_idx = _header_index_alias(headers, ["Student Subjects", "Subjects"], contains_any=["subject"])
    niveau_idx = _header_index_alias(headers, ["Niveau", "Level"], contains_any=["niveau", "level"])
    subs_idx = _header_index_alias(headers, ["Subscription"], contains_any=["subscript"])

    logger.debug(f"[_get_student_subjects_and_niveau] Indices -> ID:{id_idx}, Name:{name_idx}, "
                 f"Subjects:{subjects_idx}, Niveau:{niveau_idx}, Subscription:{subs_idx}")

    if id_idx == -1 or subjects_idx == -1:
        logger.debug("[_get_student_subjects_and_niveau] Missing required columns (ID or Subjects).")
        return None

    sid_norm = _id_str_norm(student_id)
    logger.debug(f"[_get_student_subjects_and_niveau] Normalized target ID: {sid_norm!r}")

    out: Optional[Dict[str, object]] = None
    for rnum, row in enumerate(rows[1:], start=2):
        raw_rid = _safe_cell(row, id_idx, "")
        rid_norm = _id_str_norm(raw_rid)
        logger.debug(f"[_get_student_subjects_and_niveau] Row {rnum}: raw ID={raw_rid!r} -> norm={rid_norm!r}")
        if rid_norm == sid_norm:
            subjects_csv = str(_safe_cell(row, subjects_idx, "") or "")
            subjects = [s.strip() for s in subjects_csv.split(",") if s.strip()]
            niveau = str(_safe_cell(row, niveau_idx, "") or "")
            name = str(_safe_cell(row, name_idx, "") or "")
            subs_val = str(_safe_cell(row, subs_idx, "") or "").strip().upper()
            out = {
                "name": name,
                "subjects": subjects,
                "niveau": niveau,
                "subscription": (subs_val == "TRUE")
            }
            logger.debug(f"[_get_student_subjects_and_niveau] MATCH at row {rnum}: {out}")
            break

    if out is None:
        logger.debug(f"[_get_student_subjects_and_niveau] No match found for ID={sid_norm!r} in {STUDENT_TABLE_NAME}.")
    return out

def _key_for(niveau: str, subject: str) -> str:
    normalized_subject = re.sub(r'\s+', '_', subject.strip())
    return f"{niveau}_{normalized_subject}"

# ===================== Reminders job (10d + 3d) =====================

async def check_subscriptions_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    Sends:
      - 10-day reminder when 2..10 days remain (and 10DaysReminder is FALSE)
      - 3-day reminder when 0..3 days remain (and 3DaysReminder is FALSE)
      - Expiry notice when already expired (and flips Subscription to FALSE)
    """
    sheets = setup_sheets()
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENT_TABLE_NAME}!A:Z",
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()

    rows = result.get("values", []) or []
    if len(rows) < 2:
        return

    headers = rows[0]
    data_rows = rows[1:]

    # Use exact names per your sheet
    def idx_exact(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError:
            return -1

    id_idx           = idx_exact("ID")
    # Allow End_Date / End Date
    end_date_idx     = _header_index_alias(headers, ["End_Date", "End Date"], contains_all=["end", "date"])
    subscription_idx = idx_exact("Subscription")
    ten_day_idx      = idx_exact("10DaysReminder")
    three_day_idx    = idx_exact("3DaysReminder")

    logger.debug(f"[reminders] idx -> ID:{id_idx}, End_Date:{end_date_idx}, "
                 f"Subscription:{subscription_idx}, 10DaysReminder:{ten_day_idx}, 3DaysReminder:{three_day_idx}")

    if id_idx == -1 or end_date_idx == -1 or subscription_idx == -1:
        logger.error("[reminders] Missing required columns: need 'ID', 'End_Date', 'Subscription'.")
        return

    today = date.today()

    for sheet_row_num, row in enumerate(data_rows, start=2):
        raw_id = _safe_cell(row, id_idx, "")
        if raw_id in ("", None):
            continue

        student_id = _chat_id(raw_id)
        end_date_val = _safe_cell(row, end_date_idx, "")
        if not end_date_val:
            continue

        if isinstance(end_date_val, (int, float)):
            end_dt = date(1899, 12, 30) + timedelta(days=int(end_date_val))
        else:
            end_dt = datetime.strptime(str(end_date_val).strip(), "%Y-%m-%d").date()

        sub_status = str(_safe_cell(row, subscription_idx, "")).strip().upper()
        days_left = (end_dt - today).days

        ten_sent   = _to_bool(_safe_cell(row, ten_day_idx, False)) if ten_day_idx   != -1 else False
        three_sent = _to_bool(_safe_cell(row, three_day_idx, False)) if three_day_idx != -1 else False

        if today > end_dt:
            if sub_status == "TRUE":
                try:
                    sub_col = _col_letter(subscription_idx)
                    sheets.values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"{STUDENT_TABLE_NAME}!{sub_col}{sheet_row_num}",
                        valueInputOption="RAW",
                        body={"values": [["FALSE"]]}
                    ).execute()
                except Exception:
                    pass
                try:
                    await context.bot.send_message(
                        chat_id=student_id,
                        text="Your subscription has ended. Please renew to continue access."
                    )
                except Exception:
                    pass
            continue

        if sub_status != "TRUE":
            continue

        if 2 <= days_left <= 10 and not ten_sent and ten_day_idx != -1:
            try:
                await context.bot.send_message(
                    chat_id=student_id,
                    text=(f"Your subscription will expire on {end_dt.isoformat()}. "
                          f"You have {days_left} day(s) left. Please renew soon!")
                )
                col = _col_letter(ten_day_idx)
                sheets.values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"{STUDENT_TABLE_NAME}!{col}{sheet_row_num}",
                    valueInputOption="RAW",
                    body={"values": [["TRUE"]]}
                ).execute()
            except Exception:
                pass

        if 0 <= days_left <= 3 and not three_sent and three_day_idx != -1:
            try:
                msg = ("Your subscription expires today."
                       if days_left == 0 else
                       f"Your subscription will expire on {end_dt.isoformat()}. You have {days_left} day(s) left.")
                await context.bot.send_message(chat_id=student_id, text=msg)
                col = _col_letter(three_day_idx)
                sheets.values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"{STUDENT_TABLE_NAME}!{col}{sheet_row_num}",
                    valueInputOption="RAW",
                    body={"values": [["TRUE"]]}
                ).execute()
            except Exception:
                pass

# ===================== Admin-bot helper =====================

async def invite_student_to_subject_groups(bot: Bot, telegram_id: str, subject_keys_lower: List[str]) -> None:
    """
    subject_keys_lower: ['3as_math', '3as_english'] — MUST be lowercase to match sheet keys.
    For each key with a Telegram Group ID present, create an invite link and DM it to the student.
    """
    if not subject_keys_lower:
        return
    subject_map = fetch_subject_channel_links()  # { key_lower: group_id }
    for key in subject_keys_lower:
        group_id = subject_map.get(key)
        if not group_id:
            continue
        try:
            chat_id = _chat_id(group_id)
            invite_link = await bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True
            )
            await bot.send_message(
                chat_id=int(telegram_id),
                text=f"Here is your invite link for {key}: {invite_link.invite_link}"
            )
        except Exception as e:
            logger.error(f"[invite_student_to_subject_groups] Could not send invite for {key} to {telegram_id}: {e}")

# ===================== Commands =====================

def _is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your ID is: {update.effective_user.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Available commands:\n"
        "/subjects - View your subjects with invite links\n"
        "/subscription - Check subscription status\n"
    )
    await update.message.reply_text(help_text)

async def view_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shows user's subjects with invite links next to each, based on Subjects_Channels sheet.
    Keys are '<Niveau>_<Subject>'. If a key has no group ID, show '(no channel yet)'.
    """
    uid = update.effective_user.id if update.effective_user else None
    logger.debug(f"[/subjects] Requested by user_id={uid}")
    student_id = str(uid)

    info = _get_student_subjects_and_niveau(student_id)
    if not info:
        logger.debug(f"[/subjects] _get_student_subjects_and_niveau returned None for {student_id}")
        await update.message.reply_text("Could not retrieve your subjects. Please try again or contact support.")
        return

    subjects: List[str] = info["subjects"]  # type: ignore
    niveau: str = str(info.get("niveau") or "").strip()
    logger.debug(f"[/subjects] Found info: niveau={niveau!r}, subjects={subjects}")

    if not subjects:
        await update.message.reply_text("You currently have no subjects on file.")
        return
    if not niveau:
        await update.message.reply_text("Your level (Niveau) is missing in the system. Please contact the admin.")
        return

    subject_map = fetch_subject_channel_links()
    lines: List[str] = []
    had_any_link = False

    for subj in subjects:
        key = _key_for(niveau, subj)
        key_lower = key.lower()
        group_id = subject_map.get(key_lower)
        logger.debug(f"[/subjects] Key={key_lower}, group_id={group_id!r}")
        if group_id:
            try:
                invite_link_obj = await context.bot.create_chat_invite_link(
                    chat_id=_chat_id(group_id),
                    creates_join_request=True
                )
                lines.append(f"- {subj}: {invite_link_obj.invite_link}")
                had_any_link = True
            except Exception as e:
                logger.error(f"[/subjects] Failed to create invite for {key_lower}: {e}")
                lines.append(f"- {subj}: (couldn’t create invite link: {e})")
        else:
            lines.append(f"- {subj}: (no channel yet)")

    header = "Your subjects (click to join):" if had_any_link else "Your subjects:"
    await update.message.reply_text("\n".join([header] + lines))

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = setup_sheets()
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{STUDENT_TABLE_NAME}!A:Z',
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()
    values = result.get('values', []) or []

    student_id = str(update.effective_user.id)
    student_data = None
    for row_index, row in enumerate(values[1:]):  # Skip header row
        headers = values[0]
        id_idx = _header_index_alias(headers, ["ID"], contains_any=["id"])
        if id_idx == -1:
            break
        if row and len(row) > id_idx and _id_str_norm(row[id_idx]) == _id_str_norm(student_id):
            student_data = row
            break

    if student_data:
        headers = values[0]
        name_idx      = _header_index_alias(headers, ["Student Name", "Name"], contains_any=["name"])
        pay_idx       = _header_index_alias(headers, ["Payment Method", "Payment"], contains_any=["payment"])
        reg_idx       = _header_index_alias(headers, ["Register_Date", "Register Date"], contains_all=["register","date"])
        end_idx       = _header_index_alias(headers, ["End_Date", "End Date"], contains_all=["end","date"])

        subscription_info = f"Subscription Status for { _safe_cell(student_data, name_idx, '') }:\n"
        subscription_info += f"Payment: { _safe_cell(student_data, pay_idx, '') }\n"

        start_date = _safe_cell(student_data, reg_idx, "Not available")
        end_date   = _safe_cell(student_data, end_idx, "Not available")

        subscription_info += f"Start Date: {start_date}\n"
        subscription_info += f"End Date: {end_date}\n"

        try:
            if start_date != "Not available" and end_date != "Not available":
                start = datetime.strptime(str(start_date), '%Y-%m-%d').date()
                end = datetime.strptime(str(end_date), '%Y-%m-%d').date()
                today = datetime.now().date()
                if today < start:
                    subscription_info += "Your subscription hasn't started yet\n"
                elif today > end:
                    subscription_info += "Your subscription has expired\n"
                else:
                    days_left = (end - today).days
                    subscription_info += f"Time remaining: {days_left} day(s)\n"
        except Exception:
            subscription_info += "(Could not parse dates)\n"

        await update.message.reply_text(subscription_info)
    else:
        await update.message.reply_text("Could not retrieve your subscription status. Please try again or contact support.")

# ===================== /set conversation with conflict check (HTML formatting) =====================

async def _is_admin_for_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if user and user.id in ADMIN_IDS:
        return True
    if chat and user and chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            return member.status in ("administrator", "creator")
        except Exception:
            return False
    return False

async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    uid = update.effective_user.id if update.effective_user else None
    logger.debug(f"[/set] start in chat_id={chat.id if chat else None}, by user_id={uid}")

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Please run /set inside the target group or supergroup.")
        return ConversationHandler.END

    if not await _is_admin_for_set(update, context):
        await msg.reply_text("Admins only. You must be a group admin or listed in ADMIN_IDS.")
        return ConversationHandler.END

    await msg.reply_text("Which Niveau do you want to set for this group? (e.g., 1AS, 2AS, 3AS)")
    return SET_NIVEAU

async def set_channel_get_niveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    niveau = str(update.message.text).strip().upper()
    logger.debug(f"[/set] received niveau={niveau!r}")
    if not niveau:
        await update.message.reply_text("Please provide a valid Niveau, e.g., 3AS.")
        return SET_NIVEAU

    context.user_data['set_niveau'] = niveau
    await update.message.reply_text(f"Great. Which Subject should map to this group for {niveau}? (e.g., Math, English)")
    return SET_SUBJECT

async def set_channel_get_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    subject = str(update.message.text).strip()
    logger.debug(f"[/set] received subject={subject!r} in chat_id={chat.id if chat else None}")
    if not subject:
        await update.message.reply_text("Please provide a valid subject, e.g., Math.")
        return SET_SUBJECT

    niveau = context.user_data.get('set_niveau', '')
    key_canonical = _key_for(niveau, subject)  # e.g., 2AS_Math
    key_lower = key_canonical.lower()
    logger.debug(f"[/set] computed key_canonical={key_canonical}, key_lower={key_lower}")

    try:
        sheets = setup_sheets()
        res = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A:B",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        values = res.get("values", []) or []
        logger.debug(f"[/set] Loaded {len(values)} rows from {SUBJECTS_CHANNEL_TABLE_NAME}")

        if not values:
            # initialize header if sheet was empty
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A1:B1",
                valueInputOption="RAW",
                body={"values": [["Subject", "Telegram Group ID"]]},
            ).execute()
            values = [["Subject", "Telegram Group ID"]]
            logger.debug("[/set] Initialized header row for Subjects_Channels.")

        # Find target row (same key) and conflicts (same chat id, different key)
        target_row_index = None
        conflict_key = None

        chat_id_to_store = str(chat.id)
        chat_id_norm = _id_str_norm(chat.id)

        for i, row in enumerate(values[1:], start=2):
            cell_key = (row[0].strip().lower() if row and len(row) > 0 and isinstance(row[0], str) else "")
            cell_gid_norm = _id_str_norm(row[1]) if row and len(row) > 1 else ""
            if cell_key == key_lower:
                target_row_index = i
            if cell_gid_norm and cell_gid_norm == chat_id_norm and cell_key != key_lower:
                conflict_key = row[0]
        logger.debug(f"[/set] target_row_index={target_row_index}, conflict_key={conflict_key!r}, chat_id_norm={chat_id_norm}")

        # Conflict → ask for confirmation (HTML)
        if conflict_key:
            context.user_data['pending_set'] = {
                'key_canonical': key_canonical,
                'key_lower': key_lower,
                'target_row_index': target_row_index,
                'chat_id_to_store': chat_id_to_store,
            }
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Set anyway", callback_data="set_confirm_yes"),
                 InlineKeyboardButton("Cancel", callback_data="set_confirm_no")]
            ])
            await update.message.reply_text(
                f"⚠️ This group is already assigned to <b>{conflict_key}</b>.\n"
                f"Do you want to set it anyway to <b>{key_canonical}</b>?",
                reply_markup=kb,
                parse_mode="HTML"
            )
            return SET_CONFIRM

        # No conflict → write immediately (HTML)
        if target_row_index is None:
            sheets.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A:B",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [[key_canonical, chat_id_to_store]]},
            ).execute()
            await update.message.reply_text(
                f"✅ Created and mapped <b>{key_canonical}</b> to this group.",
                parse_mode="HTML"
            )
        else:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!B{target_row_index}",
                valueInputOption="RAW",
                body={"values": [[chat_id_to_store]]},
            ).execute()
            await update.message.reply_text(
                f"✅ Updated mapping for <b>{key_canonical}</b> to this group.",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.exception("[/set] Exception while setting channel:")
        await update.message.reply_text(f"❌ Failed to set channel id: {e}")

    context.user_data.pop('set_niveau', None)
    return ConversationHandler.END

async def set_channel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'Set anyway' / 'Cancel' for channel reassignment conflicts."""
    query = update.callback_query
    await query.answer()
    data = query.data

    pending = context.user_data.get('pending_set')
    if not pending:
        await query.edit_message_text("No pending operation.")
        return ConversationHandler.END

    if data == "set_confirm_no":
        context.user_data.pop('pending_set', None)
        context.user_data.pop('set_niveau', None)
        await query.edit_message_text("Operation cancelled.")
        return ConversationHandler.END

    # Proceed with setting anyway (HTML)
    try:
        sheets = setup_sheets()
        key_canonical = pending['key_canonical']
        target_row_index = pending['target_row_index']
        chat_id_to_store = pending['chat_id_to_store']

        if target_row_index is None:
            sheets.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A:B",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [[key_canonical, chat_id_to_store]]},
            ).execute()
            await query.edit_message_text(
                f"✅ Created and mapped <b>{key_canonical}</b> to this group (despite existing assignment).",
                parse_mode="HTML"
            )
        else:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!B{target_row_index}",
                valueInputOption="RAW",
                body={"values": [[chat_id_to_store]]},
            ).execute()
            await query.edit_message_text(
                f"✅ Updated mapping for <b>{key_canonical}</b> to this group (despite existing assignment).",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.exception("[/set_confirm] Exception while confirming set:")
        await query.edit_message_text(f"❌ Failed to set channel id: {e}")

    finally:
        context.user_data.pop('pending_set', None)
        context.user_data.pop('set_niveau', None)

    return ConversationHandler.END

async def set_channel_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_set', None)
    context.user_data.pop('set_niveau', None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ===================== Admin debug command =====================

async def debug_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /debug_subjects [telegram_id] — show what we read & how we match."""
    uid = update.effective_user.id if update.effective_user else None
    if not _is_admin(uid):
        await update.message.reply_text("Admins only.")
        return

    target_id = context.args[0] if context.args else str(uid)
    info = _get_student_subjects_and_niveau(str(target_id))
    if info is None:
        await update.message.reply_text(
            f"DEBUG: No match for ID={target_id}.\n"
            f"- Check sheet '{STUDENT_TABLE_NAME}' headers include (or resemble): ID, Student Subjects, Niveau, Subscription\n"
            f"- Ensure the ID cell exactly matches {target_id} (we normalize 123.0 -> '123').\n"
            f"- DEBUG logs show row-by-row comparisons."
        )
        return

    await update.message.reply_text(
        "DEBUG RESULT:\n"
        f"- ID: {target_id}\n"
        f"- Name: {info.get('name')}\n"
        f"- Niveau: {info.get('niveau')}\n"
        f"- Subjects: {', '.join(info.get('subjects', [])) or '(none)'}\n"
        f"- Subscription: {info.get('subscription')}"
    )

# ===================== App factory =====================

def main():
    builder = Application.builder().token(os.getenv("STUDENT_BOT_TOKEN"))
    if ZoneInfo is not None:
        builder = builder.defaults(Defaults(tzinfo=ZoneInfo("Africa/Algiers")))
    application = builder.build()

    # Conversation for /set
    set_conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_channel_start, filters=(filters.ChatType.GROUPS & ~filters.SenderChat()))],
        states={
            SET_NIVEAU: [
                MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_niveau)
            ],
            SET_SUBJECT: [
                MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_subject),
                CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$"),
            ],
            SET_CONFIRM: [
                CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", set_channel_cancel, filters=filters.ChatType.GROUPS)],
        allow_reentry=True,
    )

    application.add_handler(set_conv)

    # Simple commands
    application.add_handler(CommandHandler("subjects", view_subjects))
    application.add_handler(CommandHandler("subscription", check_subscription))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("debug_subjects", debug_subjects))
    application.add_handler(CommandHandler("start", help_command))


    # Reminders
    application.job_queue.run_once(check_subscriptions_and_send_reminders, 0)
    application.job_queue.run_daily(
        check_subscriptions_and_send_reminders,
        time(hour=9, minute=0)
    )

    return application
