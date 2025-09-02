import os
import re
import json
import base64
import logging
import asyncio
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
    JobQueue,
)
from telegram.request import HTTPXRequest

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import google.auth.transport.requests as google_requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

load_dotenv()

# ========================= Logging =========================
logger = logging.getLogger("student_bot")
_log_level = logging.DEBUG if str(os.getenv("DEBUG", "0")).strip().lower() in {"1", "true", "yes"} else logging.INFO
logging.basicConfig(level=_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger.setLevel(_log_level)

# ========================= Config =========================
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
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
def _load_gcp_credentials() -> Credentials:
    """
    Load service account creds from (in order):
    1) GOOGLE_CREDENTIALS_FILE
    2) GOOGLE_CREDENTIALS_JSON_B64
    3) GOOGLE_CREDENTIALS_JSON
    """
    path = os.getenv("GOOGLE_CREDENTIALS_FILE")
    if path and os.path.exists(path):
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
        _log_sa(creds, f"file:{path}")
        _validate_gcp_credentials(creds)
        return creds

    b64 = os.getenv("GOOGLE_CREDENTIALS_JSON_B64")
    if b64:
        info = json.loads(base64.b64decode(b64.strip().strip('"').strip("'")).decode("utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _log_sa(creds, "env:JSON_B64")
        _validate_gcp_credentials(creds)
        return creds

    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _log_sa(creds, "env:JSON_RAW")
        _validate_gcp_credentials(creds)
        return creds

    raise RuntimeError("No Google credentials provided. Set GOOGLE_CREDENTIALS_FILE or GOOGLE_CREDENTIALS_JSON_B64 or GOOGLE_CREDENTIALS_JSON.")

def _log_sa(creds: Credentials, source: str):
    try:
        email = creds.service_account_email
        key_id = getattr(creds, "_service_account_info", {}).get("private_key_id", "")
        key_id_mask = (key_id[:8] + "…" + key_id[-6:]) if key_id else "unknown"
        logger.info(f"[creds] Loaded service account from {source} email={email} key_id={key_id_mask}")
    except Exception:
        pass

def _validate_gcp_credentials(creds: Credentials):
    try:
        req = google_requests.Request()
        creds.refresh(req)
        logger.info("[creds] Service account refresh OK.")
    except Exception as e:
        logger.error(
            "[creds] Refresh failed. Likely causes:\n"
            "- Wrong JSON type (must be 'service_account')\n"
            "- Rotated/stale key or corrupted private_key (lost \\n)\n"
            "- Env var not updated after key rotation\n"
            f"Details: {e}"
        )
        raise

def setup_sheets():
    creds = _load_gcp_credentials()
    service = build("sheets", "v4", credentials=creds)
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
    hdr_norm = [ _norm(h) for h in headers ]
    for al in aliases:
        try_idx = _header_index(headers, al)
        if try_idx != -1:
            return try_idx
    if contains_all:
        toks = [ _norm(t) for t in contains_all ]
        for i, h in enumerate(hdr_norm):
            if all(t in h for t in toks):
                return i
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

def _to_bool(v: object) -> bool:
    if v is True or v == 1:
        return True
    if v is False or v == 0 or v is None:
        return False
    return str(v).strip().upper() == "TRUE"

# ===================== Student data helpers =====================
def _get_student_subjects_and_niveau(student_id: str) -> Optional[Dict[str, object]]:
    """
    Returns:
    { 'name': str, 'subjects': List[str], 'niveau': str, 'subscription': bool }
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
        return None

    headers = rows[0]
    id_idx = _header_index_alias(headers, ["ID"], contains_any=["id"])
    name_idx = _header_index_alias(headers, ["Student Name", "Name"], contains_any=["name"])
    subjects_idx = _header_index_alias(headers, ["Student Subjects", "Subjects"], contains_any=["subject"])
    niveau_idx = _header_index_alias(headers, ["Niveau", "Level"], contains_any=["niveau", "level"])
    subs_idx = _header_index_alias(headers, ["Subscription"], contains_any=["subscript"])

    if id_idx == -1 or subjects_idx == -1:
        return None

    sid_norm = _id_str_norm(student_id)
    for row in rows[1:]:
        raw_rid = _safe_cell(row, id_idx, "")
        if _id_str_norm(raw_rid) == sid_norm:
            subjects_csv = str(_safe_cell(row, subjects_idx, "") or "")
            subjects = [s.strip() for s in subjects_csv.split(",") if s.strip()]
            niveau = str(_safe_cell(row, niveau_idx, "") or "")
            name = str(_safe_cell(row, name_idx, "") or "")
            subs_val = str(_safe_cell(row, subs_idx, "") or "").strip().upper()
            return {
                "name": name,
                "subjects": subjects,
                "niveau": niveau,
                "subscription": (subs_val == "TRUE")
            }
    return None

def _key_for(niveau: str, subject: str) -> str:
    normalized_subject = re.sub(r'\s+', '_', subject.strip())
    return f"{niveau}_{normalized_subject}"

# ===================== Kick helper =====================
async def kick_student_from_subject_groups(bot: Bot, telegram_id: Union[str, int], niveau: str, subjects: List[str]) -> None:
    try:
        student_id_int = int(_chat_id(telegram_id))
    except Exception:
        logger.error(f"[kick] Invalid telegram_id for kick: {telegram_id!r}")
        return

    if not subjects or not niveau:
        return

    mapping = fetch_subject_channel_links()
    for subj in subjects:
        key = _key_for(niveau, subj).lower()
        gid = mapping.get(key)
        if not gid:
            continue
        chat_id = _chat_id(gid)
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=student_id_int)
            await bot.unban_chat_member(chat_id=chat_id, user_id=student_id_int)
            logger.info(f"[kick] Kicked user {student_id_int} from chat {chat_id} (key={key})")
        except Exception as e:
            logger.error(f"[kick] Failed to kick {student_id_int} from {chat_id} (key={key}): {e}")

# ===================== Reminders job (10d + 3d + expiry kick) =====================
async def check_subscriptions_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
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

    def idx_exact(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError:
            return -1

    id_idx           = idx_exact("ID")
    end_date_idx     = _header_index_alias(headers, ["End_Date", "End Date"], contains_all=["end", "date"])
    subscription_idx = idx_exact("Subscription")
    ten_day_idx      = idx_exact("10DaysReminder")
    three_day_idx    = idx_exact("3DaysReminder")
    niveau_idx       = _header_index_alias(headers, ["Niveau", "Level"], contains_any=["niveau", "level"])
    subjects_idx     = _header_index_alias(headers, ["Student Subjects", "Subjects"], contains_any=["subject"])

    if id_idx == -1 or end_date_idx == -1 or subscription_idx == -1:
        logger.error("[reminders] Missing required columns: need 'ID', 'End_Date', 'Subscription'.")
        return

    today = date.today()

    for sheet_row_num, row in enumerate(data_rows, start=2):
        raw_id = _safe_cell(row, id_idx, "")
        if not raw_id:
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

        # Already expired: flip Subscription and kick
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
                except Exception as e:
                    logger.error(f"[reminders] Mark FALSE failed row {sheet_row_num}: {e}")

                try:
                    niveau = str(_safe_cell(row, niveau_idx, "") if niveau_idx != -1 else "").strip()
                    subjects_csv = str(_safe_cell(row, subjects_idx, "") if subjects_idx != -1 else "")
                    subjects = [s.strip() for s in subjects_csv.split(",") if s.strip()]
                    await kick_student_from_subject_groups(context.bot, student_id, niveau, subjects)
                except Exception as e:
                    logger.error(f"[reminders] Kick flow failed for user {student_id}: {e}")

                try:
                    await context.bot.send_message(
                        chat_id=student_id,
                        text="Your subscription has ended. Access to class groups has been revoked. Please renew to regain access."
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
            except Exception as e:
                logger.error(f"[reminders] 10-day reminder failed for {student_id}: {e}")

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
            except Exception as e:
                logger.error(f"[reminders] 3-day reminder failed for {student_id}: {e}")

# ===================== Admin-bot helper (invite) =====================
async def invite_student_to_subject_groups(bot: Bot, telegram_id: str, subject_keys_lower: List[str]) -> None:
    if not subject_keys_lower:
        return
    subject_map = fetch_subject_channel_links()
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
                chat_id=int(_chat_id(telegram_id)),
                text=f"Here is your invite link for {key}: {invite_link.invite_link}"
            )
        except Exception as e:
            logger.error(f"[invite] Could not send invite for {key} to {telegram_id}: {e}")

# ===================== Commands =====================
def _is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Your ID is: {update.effective_user.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Available commands:\n"
        "/subjects - View your subjects with invite links\n"
        "/subscription - Check subscription status\n"
        "/set - (in a group) Map this group to a {Niveau}_{Subject} key\n"
        "/myid - Show your Telegram ID"
    )
    await update.effective_message.reply_text(help_text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await help_command(update, context)

async def view_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    student_id = str(uid)

    info = _get_student_subjects_and_niveau(student_id)
    if not info:
        await update.effective_message.reply_text("Could not retrieve your subjects. Please try again or contact support.")
        return

    # Subscription check first
    if not bool(info.get("subscription", False)):
        await update.effective_message.reply_text("You don't have a current subscription")
        return

    subjects: List[str] = info["subjects"]  # type: ignore
    niveau: str = str(info.get("niveau") or "").strip()

    if not subjects:
        await update.effective_message.reply_text("You currently have no subjects on file.")
        return
    if not niveau:
        await update.effective_message.reply_text("Your level (Niveau) is missing in the system. Please contact the admin.")
        return

    subject_map = fetch_subject_channel_links()
    lines: List[str] = []
    had_any_link = False

    for subj in subjects:
        key = _key_for(niveau, subj).lower()
        group_id = subject_map.get(key)
        if group_id:
            try:
                invite_link_obj = await context.bot.create_chat_invite_link(
                    chat_id=_chat_id(group_id),
                    creates_join_request=True
                )
                lines.append(f"- {subj}: {invite_link_obj.invite_link}")
                had_any_link = True
            except Exception as e:
                logger.error(f"[/subjects] Failed to create invite for {key}: {e}")
                lines.append(f"- {subj}: (couldn’t create invite link)")
        else:
            lines.append(f"- {subj}: (no channel yet)")

    header = "Your subjects (click to join):" if had_any_link else "Your subjects:"
    await update.effective_message.reply_text("\n".join([header] + lines))

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
    for row in values[1:]:
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

        await update.effective_message.reply_text(subscription_info)
    else:
        await update.effective_message.reply_text("Could not retrieve your subscription status. Please try again or contact support.")

# ===================== /set conversation with conflict check =====================
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
    if not niveau:
        await update.message.reply_text("Please provide a valid Niveau, e.g., 3AS.")
        return SET_NIVEAU
    context.user_data['set_niveau'] = niveau
    await update.message.reply_text(f"Great. Which Subject should map to this group for {niveau}? (e.g., Math, English)")
    return SET_SUBJECT

async def set_channel_get_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    subject = str(update.message.text).strip()
    if not subject:
        await update.message.reply_text("Please provide a valid subject, e.g., Math.")
        return SET_SUBJECT

    niveau = context.user_data.get('set_niveau', '')
    key_canonical = _key_for(niveau, subject)
    key_lower = key_canonical.lower()

    try:
        sheets = setup_sheets()
        res = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A:B",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        values = res.get("values", []) or []

        if not values:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A1:B1",
                valueInputOption="RAW",
                body={"values": [["Subject", "Telegram Group ID"]]},
            ).execute()
            values = [["Subject", "Telegram Group ID"]]

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
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END

# ===================== Admin debug command =====================
async def debug_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not _is_admin(uid):
        await update.effective_message.reply_text("Admins only.")
        return

    target_id = context.args[0] if context.args else str(uid)
    info = _get_student_subjects_and_niveau(str(target_id))
    if info is None:
        await update.effective_message.reply_text(
            f"DEBUG: No match for ID={target_id}.\n"
            f"- Check sheet '{STUDENT_TABLE_NAME}' headers include: ID, Student Subjects, Niveau, Subscription\n"
            f"- Ensure the ID cell exactly matches {target_id}."
        )
        return

    await update.effective_message.reply_text(
        "DEBUG RESULT:\n"
        f"- ID: {target_id}\n"
        f"- Name: {info.get('name')}\n"
        f"- Niveau: {info.get('niveau')}\n"
        f"- Subjects: {', '.join(info.get('subjects', [])) or '(none)'}\n"
        f"- Subscription: {info.get('subscription')}"
    )

# ===================== Render/webhook friendly app factory =====================
async def _post_init(app: Application):
    # Schedule reminders after initialization so job_queue is ready
    app.job_queue.run_once(check_subscriptions_and_send_reminders, when=0)
    app.job_queue.run_daily(check_subscriptions_and_send_reminders, time(hour=9, minute=0))

def main():
    req = HTTPXRequest(
        connection_pool_size=50,
        connect_timeout=20.0,
        read_timeout=40.0,
        write_timeout=40.0,
        pool_timeout=20.0,
    )
    jq = JobQueue()

    builder = (
        Application.builder()
        .token(os.getenv("STUDENT_BOT_TOKEN"))
        .request(req)
        .job_queue(jq)
        .post_init(_post_init)
    )
    if ZoneInfo is not None:
        builder = builder.defaults(Defaults(tzinfo=ZoneInfo("Africa/Algiers")))
    application = builder.build()

    # Conversation for /set
    set_conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_channel_start, filters=(filters.ChatType.GROUPS & ~filters.SenderChat()))],
        states={
            SET_NIVEAU: [MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_niveau)],
            SET_SUBJECT: [
                MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_subject),
                CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$"),
            ],
            SET_CONFIRM: [CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", set_channel_cancel, filters=filters.ChatType.GROUPS)],
        allow_reentry=True,
    )

    # Handlers
    application.add_handler(set_conv)
    application.add_handler(CommandHandler("subjects", view_subjects))
    application.add_handler(CommandHandler("subscription", check_subscription))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("debug_subjects", debug_subjects))

    return application
