# student_bot.py
import os
import re
import json
import base64
import logging
from typing import List, Set, Dict, Optional, Union
from datetime import datetime, timedelta, date, time

from dotenv import load_dotenv
from telegram import (
    Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
)
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
    ZoneInfo = None

load_dotenv()

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

def _load_gcp_credentials) -> Credentials:
    path = os.getenv("GOOGLE_CREDENTIALS_FILE")
    if path and os.path.exists(path):
        return Credentials.from_service_account_file(path, scopes=SCOPES)

    b64 = os.getenv("GOOGLE_CREDENTIALS_JSON_B64")
    if b64:
        info = json.loads(base64.b64decode(b64.strip().strip('"').strip("'")).decode("utf-8"))
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    raise RuntimeError("No Google credentials provided.")

def setup_sheets():
    creds = _load_gcp_credentials()
    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
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
    for rnum, row in enumerate(rows[1:], start=2):
        raw_rid = _safe_cell(row, id_idx, "")
        rid_norm = _id_str_norm(raw_rid)
        if rid_norm == sid_norm:
            subjects_csv = str(_safe_cell(row, subjects_idx, "") or "")
            subjects = [s.strip() for s in subjects_csv.split(",") if s.strip()]
            niveau = str(_safe_cell(row, niveau_idx, "") or "")
            name = str(_safe_cell(row, name_idx, "") or "")
            subs_val = str(_safe_cell(row, subs_idx, "") or "").strip().upper()
            return {"name": name, "subjects": subjects, "niveau": niveau, "subscription": (subs_val == "TRUE")}
    return None

def _key_for(niveau: str, subject: str) -> str:
    normalized_subject = re.sub(r'\s+', '_', subject.strip())
    return f"{niveau}_{normalized_subject}"

# ===================== Reminders job (10d + 3d) =====================

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
    if id_idx == -1 or end_date_idx == -1 or subscription_idx == -1:
        return

    today = date.today()

    for sheet_row_num, row in enumerate(rows[1:], start=2):
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
                # Flip subscription to FALSE and notify
                try:
                    col = _col_letter(subscription_idx)
                    sheets.values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=f"{STUDENT_TABLE_NAME}!{col}{sheet_row_num}",
                        valueInputOption="RAW",
                        body={"values": [["FALSE"]]}
                    ).execute()
                except Exception:
                    pass
                try:
                    await context.bot.send_message(
                        chat_id=student_id,
                        text="⏳ انتهى اشتراكك. يرجى التجديد لمواصلة الوصول."
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
                    text=(f"⏳ سينتهي اشتراكك في {end_dt.isoformat()}.\n"
                          f"متبقّي {days_left} يوم/أيام. يرجى التجديد قريبًا.")
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
                msg = ("⏳ ينتهي اشتراكك اليوم."
                       if days_left == 0 else
                       f"⏳ سينتهي اشتراكك في {end_dt.isoformat()}. متبقّي {days_left} يوم/أيام.")
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
                chat_id=int(telegram_id),
                text=f"رابط الدعوة لمجموعة {key}:\n{invite_link.invite_link}"
            )
        except Exception as e:
            logger.error(f"[invite_student_to_subject_groups] Could not send invite for {key} to {telegram_id}: {e}")

# ===== Helper: invite existing subscribed students when a mapping is (re)assigned ====

async def _broadcast_invites_to_existing_students(
    niveau: str,
    subject_canonical: str,
    group_chat_id: Union[int, str],
    bot: Bot
) -> tuple[int, int]:
    """
    Finds subscribed students with the given niveau & subject; sends them an invite
    link to the provided group. Returns (sent_count, failed_count). Silent (no chat message).
    """
    try:
        sheets = setup_sheets()
        res = sheets.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{STUDENT_TABLE_NAME}!A:Z",
            valueRenderOption="UNFORMATTED_VALUE"
        ).execute()
        rows = res.get("values", []) or []
        if len(rows) < 2:
            return (0, 0)

        headers = rows[0]
        id_idx       = _header_index_alias(headers, ["ID"], contains_any=["id"])
        subjects_idx = _header_index_alias(headers, ["Student Subjects", "Subjects"], contains_any=["subject"])
        niveau_idx   = _header_index_alias(headers, ["Niveau", "Level"], contains_any=["niveau","level"])
        subs_idx     = _header_index_alias(headers, ["Subscription"], contains_any=["subscript"])

        if min(id_idx, subjects_idx, niveau_idx, subs_idx) == -1:
            return (0, 0)

        want_level = niveau.strip().lower()
        want_subject = subject_canonical.strip().lower()

        sent = failed = 0
        # create invite once to reduce API calls (can expire; but fine for immediate send)
        try:
            link_obj = await bot.create_chat_invite_link(chat_id=_chat_id(group_chat_id), creates_join_request=True)
            invite_url = link_obj.invite_link
        except Exception as e:
            logger.warning("Failed to create invite link for broadcast: %s", e)
            return (0, 0)

        for row in rows[1:]:
            if str(_safe_cell(row, subs_idx, "")).strip().upper() != "TRUE":
                continue
            if str(_safe_cell(row, niveau_idx, "")).strip().lower() != want_level:
                continue
            subjects_csv = str(_safe_cell(row, subjects_idx, "") or "")
            subj_list = [s.strip().lower() for s in subjects_csv.split(",") if s.strip()]
            if want_subject not in subj_list:
                continue

            rid = _id_str_norm(_safe_cell(row, id_idx, ""))
            if not rid:
                continue
            try:
                await bot.send_message(
                    chat_id=_chat_id(rid),
                    text=f"تم ربط مجموعة جديدة بـ {niveau}_{subject_canonical}.\nرابط الدعوة:\n{invite_url}"
                )
                sent += 1
            except Exception:
                failed += 1

        logger.info("Broadcast invites after /set for %s_%s: sent=%d failed=%d", niveau, subject_canonical, sent, failed)
        return (sent, failed)
    except Exception as e:
        logger.debug("Broadcast failed: %s", e)
        return (0, 0)

# ===================== Commands =====================

def _is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"معرّفك هو: {update.effective_user.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "الأوامر المتاحة:\n"
        "/subjects - عرض موادك مع روابط الدعوة\n"
        "/subscription - حالة الاشتراك\n"
    )
    await update.message.reply_text(help_text)

async def view_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    logger.debug(f"[/subjects] Requested by user_id={uid}")
    student_id = str(uid)

    info = _get_student_subjects_and_niveau(student_id)
    if not info:
        await update.message.reply_text("تعذّر جلب موادك. أعد المحاولة أو تواصل مع المشرف.")
        return

    # Subscription check first
    if not bool(info.get("subscription", False)):
        await update.message.reply_text("ليس لديك اشتراك فعّال حاليًا.")
        return

    subjects: List[str] = info["subjects"]  # type: ignore
    niveau: str = str(info.get("niveau") or "").strip()

    if not subjects:
        await update.message.reply_text("لا توجد مواد مسجّلة في حسابك.")
        return
    if not niveau:
        await update.message.reply_text("مستواك الدراسي غير مسجّل. يرجى التواصل مع المشرف.")
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
                lines.append(f"- {subj}: (تعذّر إنشاء رابط الدعوة: {e})")
        else:
            lines.append(f"- {subj}: (لا توجد مجموعة حالياً)")

    header = "موادك (روابط للانضمام):" if had_any_link else "موادك:"
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

        subscription_info = f"حالة الاشتراك للطالب { _safe_cell(student_data, name_idx, '') }:\n"
        subscription_info += f"طريقة الدفع: { _safe_cell(student_data, pay_idx, '') }\n"

        start_date = _safe_cell(student_data, reg_idx, "غير متوفّر")
        end_date   = _safe_cell(student_data, end_idx, "غير متوفّر")

        subscription_info += f"تاريخ البداية: {start_date}\n"
        subscription_info += f"تاريخ الانتهاء: {end_date}\n"

        try:
            if start_date != "غير متوفّر" and end_date != "غير متوفّر":
                start = datetime.strptime(str(start_date), '%Y-%m-%d').date()
                end = datetime.strptime(str(end_date), '%Y-%m-%d').date()
                today = datetime.now().date()
                if today < start:
                    subscription_info += "اشتراكك لم يبدأ بعد.\n"
                elif today > end:
                    subscription_info += "انتهى اشتراكك.\n"
                else:
                    days_left = (end - today).days
                    subscription_info += f"المدّة المتبقية: {days_left} يوم/أيام.\n"
        except Exception:
            subscription_info += "(تعذّر تفسير التواريخ)\n"

        await update.message.reply_text(subscription_info)
    else:
        await update.message.reply_text("تعذّر جلب حالة الاشتراك. أعد المحاولة أو تواصل مع الدعم.")

# ===================== /set conversation (robust in groups) =====================

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
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("يرجى تشغيل /set داخل المجموعة المستهدفة.")
        return ConversationHandler.END

    if not await _is_admin_for_set(update, context):
        await update.effective_message.reply_text("المشرفون فقط.")
        return ConversationHandler.END

    # Inline buttons ensure bot receives the callback even with privacy mode ON
    nivs = ["1AS", "2AS", "3AS", "4AM", "3AM", "2AM", "1AM"]
    rows = [
        [InlineKeyboardButton(n, callback_data=f"setniv:{n}") for n in nivs[:3]],
        [InlineKeyboardButton(n, callback_data=f"setniv:{n}") for n in nivs[3:6]],
        [InlineKeyboardButton(nivs[6], callback_data=f"setniv:{nivs[6]}")]
    ]
    await update.effective_message.reply_text(
        "ما هو المستوى (Niveau) لهذه المجموعة؟ اختر من الأزرار:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return SET_NIVEAU

async def set_channel_get_niveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    niveau = None
    if update.callback_query:
        await update.callback_query.answer()
        data = update.callback_query.data or ""
        if data.startswith("setniv:"):
            niveau = data.split(":", 1)[1].strip().upper()
            await update.callback_query.edit_message_text(f"المستوى المحدد: {niveau}")
    elif update.message:
        niveau = (update.message.text or "").strip().upper()

    if not niveau:
        await (update.effective_message or update.callback_query.message).reply_text("يرجى اختيار مستوى صحيح.")
        return SET_NIVEAU

    context.user_data['set_niveau'] = niveau
    # ForceReply so the bot gets the next message even with privacy mode ON
    await (update.effective_message or update.callback_query.message).reply_text(
        f"جيّد. ما هي المادة للمستوى {niveau}؟ (مثل: Math, English)",
        reply_markup=ForceReply(selective=True)
    )
    return SET_SUBJECT

async def set_channel_get_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    subject = str(update.message.text).strip() if update.message else ""
    if not subject:
        await update.effective_message.reply_text("يرجى إدخال مادة صحيحة، مثل Math.")
        return SET_SUBJECT

    niveau = context.user_data.get('set_niveau', '')
    key_canonical = _key_for(niveau, subject)  # e.g., 2AS_Math
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
                'target_row_index': target_row_index,
                'chat_id_to_store': chat_id_to_store,
            }
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("تعيين على أي حال", callback_data="set_confirm_yes"),
                 InlineKeyboardButton("إلغاء", callback_data="set_confirm_no")]
            ])
            await update.effective_message.reply_text(
                f"⚠️ هذه المجموعة معيّنة بالفعل إلى <b>{conflict_key}</b>.\n"
                f"هل تريد تعيينها إلى <b>{key_canonical}</b> رغم التداخل؟",
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
            await update.effective_message.reply_text(
                f"✅ تم إنشاء وربط <b>{key_canonical}</b> بهذه المجموعة.",
                parse_mode="HTML"
            )
        else:
            sheets.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!B{target_row_index}",
                valueInputOption="RAW",
                body={"values": [[chat_id_to_store]]},
            ).execute()
            await update.effective_message.reply_text(
                f"✅ تم تحديث الربط لـ <b>{key_canonical}</b> بهذه المجموعة.",
                parse_mode="HTML"
            )

        # Silently notify existing subscribed students (no message in the group)
        try:
            await _broadcast_invites_to_existing_students(
                niveau=niveau,
                subject_canonical=key_canonical.split("_", 1)[1],
                group_chat_id=chat.id,
                bot=context.bot
            )
        except Exception as e:
            logger.debug(f"[/set] broadcast invite failed: {e}")

    except Exception as e:
        logger.exception("[/set] Exception while setting channel:")
        await update.effective_message.reply_text(f"❌ تعذّر ضبط المعرّف: {e}")

    context.user_data.pop('set_niveau', None)
    return ConversationHandler.END

async def set_channel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    pending = context.user_data.get('pending_set')
    if not pending:
        await query.edit_message_text("لا توجد عملية مُعلّقة.")
        return ConversationHandler.END

    try:
        sheets = setup_sheets()
        key_canonical = pending['key_canonical']
        target_row_index = pending['target_row_index']
        chat_id_to_store = pending['chat_id_to_store']

        if data == "set_confirm_no":
            context.user_data.pop('pending_set', None)
            context.user_data.pop('set_niveau', None)
            await query.edit_message_text("تم الإلغاء.")
            return ConversationHandler.END

        if target_row_index is None:
            sheets.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNEL_TABLE_NAME}!A:B",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [[key_canonical, chat_id_to_store]]},
            ).execute()
            await query.edit_message_text(
                f"✅ تم إنشاء وربط <b>{key_canonical}</b> بهذه المجموعة (رغم التداخل).",
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
                f"✅ تم تحديث الربط لـ <b>{key_canonical}</b> بهذه المجموعة (رغم التداخل).",
                parse_mode="HTML"
            )

        # Silently notify existing subscribed students (no group message)
        try:
            niveau = key_canonical.split("_", 1)[0]
            subj_canon = key_canonical.split("_", 1)[1]
            await _broadcast_invites_to_existing_students(
                niveau=niveau,
                subject_canonical=subj_canon,
                group_chat_id=query.message.chat.id,
                bot=context.bot
            )
        except Exception as e:
            logger.debug(f"[/set_confirm] broadcast invite failed: {e}")

    except Exception as e:
        logger.exception("[/set_confirm] Exception while confirming set:")
        await query.edit_message_text(f"❌ تعذّر ضبط المعرّف: {e}")
    finally:
        context.user_data.pop('pending_set', None)
        context.user_data.pop('set_niveau', None)

    return ConversationHandler.END

async def set_channel_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_set', None)
    context.user_data.pop('set_niveau', None)
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END

# ===================== App factory (WEBHOOK-READY) =====================

def main(updater_none: bool = False):
    token = os.getenv("STUDENT_BOT_TOKEN")
    builder = Application.builder().token(token)
    if updater_none:
        builder = builder.updater(None)  # disable Updater for webhook mode
    if ZoneInfo is not None:
        builder = builder.defaults(Defaults(tzinfo=ZoneInfo("Africa/Algiers")))
    application = builder.build()

    # Conversation for /set (handles callback for niveau + text for subject)
    set_conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_channel_start, filters=(filters.ChatType.GROUPS & ~filters.SenderChat()))],
        states={
            SET_NIVEAU: [
                CallbackQueryHandler(set_channel_get_niveau, pattern=r"^setniv:"),
                MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_niveau),
            ],
            SET_SUBJECT: [
                MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_subject),
                CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$"),
            ],
            SET_CONFIRM: [CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", set_channel_cancel, filters=filters.ChatType.GROUPS)],
        allow_reentry=True,
    )
    application.add_handler(set_conv)

    # Simple commands
    application.add_handler(CommandHandler("subjects", view_subjects))
    application.add_handler(CommandHandler("subscription", check_subscription))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("myid", myid))

    # Reminders (will run while the service is awake)
    application.job_queue.run_once(check_subscriptions_and_send_reminders, 0)
    application.job_queue.run_daily(check_subscriptions_and_send_reminders, time(hour=9, minute=0))

    return application

# --- Optional warm-up for Render cold starts ---------------------------------
import asyncio

async def prewarm_clients():
    """
    Lazily mint Google credentials and do a tiny Sheets read so the first
    real webhook doesn’t pay the cold-start cost. Safe to call multiple times.
    """
    def _sync():
        try:
            creds = _load_gcp_credentials()
            service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
            rng = f"{STUDENT_TABLE_NAME}!A1:A1"
            service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=rng
            ).execute()
        except Exception as e:
            logger.warning("[prewarm_clients] Warm-up skipped/failed: %s", e)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync)
