# student_bot.py
import os
import re
import json
import base64
import logging
import unicodedata
import difflib
from typing import List, Set, Dict, Optional, Union, Tuple
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

# Admins allowed to use admin-only commands inside this bot (/set, /zoom)
def _parse_admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: Set[int] = set()
    for tok in re.split(r"[,\s]+", raw.strip().strip("'").strip('"')):
        if tok and tok.strip().lstrip("-").isdigit():
            ids.add(int(tok))
    return ids

ADMIN_IDS: Set[int] = _parse_admin_ids()

logger.info("Student bot starting with:")
logger.info(f"  SPREADSHEET_ID={SPREADSHEET_ID}")
logger.info(f"  STUDENT_TABLE_NAME={STUDENT_TABLE_NAME}")
logger.info(f"  SUBJECTS_CHANNEL_TABLE_NAME={SUBJECTS_CHANNEL_TABLE_NAME}")
logger.info(f"  DEBUG={'ON' if _log_level == logging.DEBUG else 'OFF'}")
if ADMIN_IDS:
    logger.info(f"  ADMIN_IDS (count={len(ADMIN_IDS)}): {sorted(ADMIN_IDS)}")

# Conversation states
# /set admin flow
SET_NIVEAU, SET_SUBJECT, SET_CONFIRM = range(3)

# /zoom admin flow
ZOOM_NIVEAU, ZOOM_SUBJECT, ZOOM_URL, ZOOM_CONFIRM = range(3, 7)

# /register student flow
REG_NIVEAU, REG_NAME, REG_PHONE, REG_SUBJECTS, REG_SPECIALITY, REG_PAYMENT, REG_PERIOD, REG_CONFIRM = range(7, 15)

# Allowed niveaux
ALLOWED_NIVEAUX = {"3AS", "2AS", "1AS", "4AM", "3AM", "2AM", "1AM"}

# ===================== Google Sheets helpers =====================

def _load_gcp_credentials() -> Credentials:
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

def ensure_subject_channels_rows(niveau: str, subjects_csv: str):
    """Ensure rows '<niveau>_<subject>' exist in Subjects_Channels (B blank if unknown)."""
    if not subjects_csv:
        return
    subjects = [s.strip() for s in subjects_csv.split(',') if s.strip()]
    sheets = setup_sheets()
    existing = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{SUBJECTS_CHANNEL_TABLE_NAME}!A2:A'
    ).execute().get('values', [])
    existing_keys = set(v[0].strip() for v in existing if v)

    to_append = []
    for subj in subjects:
        normalized = re.sub(r'\s+', '_', subj.strip())
        key = f"{niveau}_{normalized}"
        if key not in existing_keys:
            to_append.append([key, ""])
    if to_append:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f'{SUBJECTS_CHANNEL_TABLE_NAME}!A:B',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': to_append}
        ).execute()

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

def add_student_row(phone: str, name: str, subjects_csv: str, speciality: str,
                    payment: str, student_id: str, register_date: str,
                    end_date: str, subscription_status: str,
                    ten_days_sent: str, three_days_sent: str, niveau: str) -> None:
    sheets = setup_sheets()
    values = [[
        phone, name, subjects_csv, speciality, payment, student_id,
        register_date, end_date, subscription_status,
        ten_days_sent, three_days_sent, niveau
    ]]
    sheets.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENT_TABLE_NAME}!A2:L",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': values}
    ).execute()

# ===================== Fuzzy subject matching =====================

def _strip_diacritics(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def _subj_norm(s: str) -> str:
    s = _strip_diacritics(s).lower().strip()
    s = re.sub(r'[^a-z0-9\s_]', '', s)
    s = s.replace('-', ' ')
    s = re.sub(r'\s+', '_', s)
    return s

# Basic FR->EN & common typos map -> canonical lowercase tokens
_SUBJECT_SYNONYMS: Dict[str, str] = {
    # English
    "english": "english", "eng": "english", "anglais": "english", "englich": "english",
    "englsh": "english", "ang": "english",
    # Math
    "math": "math", "maths": "math", "mathematiques": "math", "mathematique": "math",
    "mathematiquesappliquees": "math", "mathématiques": "math", "mathématique": "math",
    # Physics
    "physics": "physics", "physic": "physics", "physique": "physics",
    # Chemistry
    "chemistry": "chemistry", "chimie": "chemistry", "chemestry": "chemistry",
    # Arabic
    "arabic": "arabic", "arabe": "arabic",
    # French
    "french": "french", "francais": "french", "français": "french",
    # Biology / SVT
    "svt": "biology", "biologie": "biology", "biology": "biology",
    # History/Geography
    "history": "history", "histoire": "history",
    "geography": "geography", "geographie": "geography", "géographie": "geography",
    # CS
    "informatique": "computer_science", "cs": "computer_science", "computer": "computer_science",
    "computer_science": "computer_science", "info": "computer_science",
    # Spanish/German
    "espagnol": "spanish", "spanish": "spanish",
    "allemand": "german", "german": "german",
}

def _canonicalize_user_subject_token(token: str) -> str:
    n = _subj_norm(token)
    if n in _SUBJECT_SYNONYMS:
        return _SUBJECT_SYNONYMS[n]
    # heuristic: try singular/plural trims
    n2 = n.rstrip('s')
    if n2 in _SUBJECT_SYNONYMS:
        return _SUBJECT_SYNONYMS[n2]
    return n  # fallback normalized token

def _load_available_subjects_for_niveau(niveau: str) -> Dict[str, str]:
    """
    Returns mapping { normalized_subject_token: original_canonical_from_sheet }
    for keys starting with '<niveau>_' in Subjects_Channels.
    """
    mapping = {}
    sc = fetch_subject_channel_links()
    prefix = f"{niveau}_".lower()
    for key in sc.keys():
        if not key.startswith(prefix):
            continue
        subj_part = key[len(prefix):]  # e.g., 'Math' or 'Computer_Science'
        # original casing from sheet is unknown here because we have only lower() keys;
        # reconstruct Title-like from stored key: use the key as-is after prefix (lower),
        # but we’ll keep underscores and title-case tokens.
        original = "_".join(w.capitalize() for w in subj_part.split('_'))
        mapping[_subj_norm(subj_part)] = original
    return mapping

def _match_user_subjects_to_canonical(niveau: str, user_subjects_csv: str) -> Tuple[List[str], List[str]]:
    """
    Try to match user-input subjects to canonical subjects for the niveau.
    Returns (matched_canonicals, unknown_inputs)
    """
    wanted = [s.strip() for s in user_subjects_csv.split(",") if s.strip()]
    if not wanted:
        return [], []

    available = _load_available_subjects_for_niveau(niveau)  # norm -> Canonical
    available_norms = list(available.keys())

    matched: List[str] = []
    unknown: List[str] = []

    for w in wanted:
        tok = _canonicalize_user_subject_token(w)  # normalized / synonyms handled
        # direct hit
        if tok in available:
            matched.append(available[tok])
            continue
        # difflib against available norms
        if available_norms:
            cand = difflib.get_close_matches(tok, available_norms, n=1, cutoff=0.6)
            if cand:
                matched.append(available[cand[0]])
                continue
        # still nothing -> keep normalized title-cased version (will be ensured in sheet)
        # e.g., 'english' -> 'English'
        fallback = "_".join(p.capitalize() for p in tok.split('_') if p)
        if fallback:
            matched.append(fallback)
        else:
            unknown.append(w)

    return matched, unknown

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
    niveau_idx       = _header_index_alias(headers, ["Niveau", "Level"], contains_any=["niveau", "level"])
    subjects_idx     = _header_index_alias(headers, ["Student Subjects", "Subjects"], contains_any=["subject"])
    if id_idx == -1 or end_date_idx == -1 or subscription_idx == -1:
        return

    today = date.today()

    # Preload group map for potential kicking later
    subject_map = fetch_subject_channel_links()

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
            try:
                end_dt = datetime.strptime(str(end_date_val).strip(), "%Y-%m-%d").date()
            except Exception:
                continue

        sub_status = str(_safe_cell(row, subscription_idx, "")).strip().upper()
        days_left = (end_dt - today).days

        ten_sent   = _to_bool(_safe_cell(row, ten_day_idx, False)) if ten_day_idx   != -1 else False
        three_sent = _to_bool(_safe_cell(row, three_day_idx, False)) if three_day_idx != -1 else False

        # If expired, flip to FALSE and (optionally) kick from channels
        if today > end_dt:
            if sub_status == "TRUE":
                # Flip subscription to FALSE
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
                # Kick from joined subject channels if any (best-effort)
                try:
                    niveau = str(_safe_cell(row, niveau_idx, "") or "").strip()
                    subjects_csv = str(_safe_cell(row, subjects_idx, "") or "").strip()
                    subs = [s.strip() for s in subjects_csv.split(",") if s.strip()]
                    for ssub in subs:
                        key = _key_for(niveau, ssub).lower()
                        gid = subject_map.get(key)
                        if gid:
                            try:
                                await context.bot.ban_chat_member(chat_id=_chat_id(gid), user_id=int(student_id))
                                await context.bot.unban_chat_member(chat_id=_chat_id(gid), user_id=int(student_id), only_if_banned=True)
                            except Exception:
                                pass
                except Exception:
                    pass
                # Notify
                try:
                    await context.bot.send_message(
                        chat_id=student_id,
                        text="لقد انتهى اشتراكك. يرجى التجديد لمتابعة الوصول."
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
                    text=(f"سينتهي اشتراكك في {end_dt.isoformat()}. "
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
                msg = ("ينتهي اشتراكك اليوم."
                       if days_left == 0 else
                       f"سينتهي اشتراكك في {end_dt.isoformat()}. متبقّي {days_left} يوم/أيام.")
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

# ===================== Admin checks/helpers =====================

def _is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)

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
                text=f"رابط الدعوة للمجموعة ({key}): {invite_link.invite_link}"
            )
        except Exception as e:
            logger.error(f"[invite_student_to_subject_groups] Could not send invite for {key} to {telegram_id}: {e}")

# ===================== Student commands =====================

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"معرّفك هو: {update.effective_user.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "الأوامر المتاحة:\n"
        "/register - تسجيل طالب جديد\n"
        "/subjects - عرض المواد وروابط الدعوة\n"
        "/subscription - حالة الاشتراك\n"
        "/myid - عرض معرّف تيليجرام\n"
        "/help - المساعدة\n"
    )
    if _is_admin(update.effective_user.id):
        help_text += "\nأوامر المشرف:\n/set - ربط مجموعة بمستوى/مادة\n/zoom - إرسال رابط Zoom للطلاب"
    await update.message.reply_text(help_text)

async def view_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    logger.debug(f"[/subjects] Requested by user_id={uid}")
    student_id = str(uid)

    info = _get_student_subjects_and_niveau(student_id)
    if not info:
        await update.message.reply_text("تعذّر جلب موادك. حاول لاحقًا أو تواصل مع الدعم.")
        return

    # Subscription check first
    if not bool(info.get("subscription", False)):
        await update.message.reply_text("لا تملك اشتراكًا حاليًا.")
        return

    subjects: List[str] = info["subjects"]  # type: ignore
    niveau: str = str(info.get("niveau") or "").strip()

    if not subjects:
        await update.message.reply_text("لا توجد مواد مسجّلة حاليًا.")
        return
    if not niveau:
        await update.message.reply_text("مستواك غير مسجّل لدينا. يرجى التواصل مع المشرف.")
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
            lines.append(f"- {subj}: (لا توجد مجموعة بعد)")

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
        subscription_info += f"الدفع: { _safe_cell(student_data, pay_idx, '') }\n"

        start_date = _safe_cell(student_data, reg_idx, "غير متاح")
        end_date   = _safe_cell(student_data, end_idx, "غير متاح")

        subscription_info += f"تاريخ البداية: {start_date}\n"
        subscription_info += f"تاريخ الانتهاء: {end_date}\n"

        try:
            if start_date != "غير متاح" and end_date != "غير متاح":
                start = datetime.strptime(str(start_date), '%Y-%m-%d').date()
                end = datetime.strptime(str(end_date), '%Y-%m-%d').date()
                today = datetime.now().date()
                if today < start:
                    subscription_info += "الاشتراك لم يبدأ بعد\n"
                elif today > end:
                    subscription_info += "انتهى الاشتراك\n"
                else:
                    days_left = (end - today).days
                    subscription_info += f"المدّة المتبقية: {days_left} يوم/أيام\n"
        except Exception:
            subscription_info += "(تعذّر تحليل التواريخ)\n"

        await update.message.reply_text(subscription_info)
    else:
        await update.message.reply_text("تعذّر جلب حالة اشتراكك. حاول لاحقًا أو تواصل مع الدعم.")

# ===================== /set conversation (admin-only) =====================

async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("يرجى تشغيل /set داخل المجموعة المستهدفة.")
        return ConversationHandler.END

    if not await _is_admin_for_set(update, context):
        await update.effective_message.reply_text("المشرفون فقط.")
        return ConversationHandler.END

    await update.effective_message.reply_text("ما هو المستوى (Niveau) لهذه المجموعة؟ (مثال: 1AS، 2AS، 3AS)")
    return SET_NIVEAU

async def set_channel_get_niveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    niveau = str(update.message.text).strip().upper()
    if not niveau:
        await update.message.reply_text("يرجى تزويد مستوى صحيح، مثل 3AS.")
        return SET_NIVEAU

    context.user_data['set_niveau'] = niveau
    await update.message.reply_text(f"جيّد. ما هي المادة لهذا المستوى {niveau}؟ (مثال: Math, English)")
    return SET_SUBJECT

async def set_channel_get_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    subject = str(update.message.text).strip()
    if not subject:
        await update.message.reply_text("يرجى إدخال مادة صحيحة، مثل Math.")
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
            await update.message.reply_text(
                f"⚠️ هذه المجموعة معيّنة بالفعل إلى <b>{conflict_key}</b>.\n"
                f"هل تريد تعيينها إلى <b>{key_canonical}</b> رغم ذلك؟",
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
            await update.message.reply_text(
                f"✅ تم تحديث الربط لـ <b>{key_canonical}</b> بهذه المجموعة.",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.exception("[/set] Exception while setting channel:")
        await update.message.reply_text(f"❌ تعذّر ضبط المعرّف: {e}")

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

    if data == "set_confirm_no":
        context.user_data.pop('pending_set', None)
        context.user_data.pop('set_niveau', None)
        await query.edit_message_text("تم الإلغاء.")
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

# ===================== /zoom (admin-only) =====================

async def zoom_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("المشرفون فقط.")
        return ConversationHandler.END
    context.user_data.pop('zoom', None)
    await update.message.reply_text("لأي مستوى تريد إرسال رابط Zoom؟ (مثل 1AS، 2AS، 3AS، 4AM، 3AM، 2AM، 1AM)")
    return ZOOM_NIVEAU

async def zoom_get_niveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("المشرفون فقط.")
        return ConversationHandler.END
    niveau = update.message.text.strip().upper()
    if not niveau:
        await update.message.reply_text("يرجى إدخال مستوى صالح، مثل 3AS.")
        return ZOOM_NIVEAU
    context.user_data['zoom'] = {'niveau': niveau}
    await update.message.reply_text(f"ما هي المادة للمستوى {niveau}؟ (مثل: Math, English)")
    return ZOOM_SUBJECT

async def zoom_get_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("المشرفون فقط.")
        return ConversationHandler.END
    subject = update.message.text.strip()
    if not subject:
        await update.message.reply_text("يرجى إدخال مادة صالحة، مثل Math.")
        return ZOOM_SUBJECT
    context.user_data['zoom']['subject'] = subject
    await update.message.reply_text("يرجى لصق رابط اجتماع Zoom:")
    return ZOOM_URL

def _find_zoom_recipients(niveau: str, subject: str) -> List[Dict[str, str]]:
    sheets = setup_sheets()
    res = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENT_TABLE_NAME}!A:Z",
        valueRenderOption="UNFORMATTED_VALUE"
    ).execute()
    rows = res.get("values", []) or []
    if len(rows) < 2:
        return []

    headers = rows[0]
    def idx_exact(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError:
            return -1

    id_idx       = _header_index_alias(headers, ["ID"], contains_any=["id"])
    name_idx     = _header_index_alias(headers, ["Student Name", "Name"], contains_any=["name"])
    subjects_idx = _header_index_alias(headers, ["Student Subjects", "Subjects"], contains_any=["subject"])
    niveau_idx   = _header_index_alias(headers, ["Niveau", "Level"], contains_any=["niveau","level"])
    subs_idx     = idx_exact("Subscription")

    if min(id_idx, subjects_idx, niveau_idx, subs_idx) == -1:
        return []

    want_level = niveau.strip().lower()
    want_subject = subject.strip().lower()

    recipients: List[Dict[str, str]] = []
    for row in rows[1:]:
        sub_status = str(_safe_cell(row, subs_idx, "")).strip().upper()
        if sub_status != "TRUE":
            continue

        row_level = str(_safe_cell(row, niveau_idx, "")).strip().lower()
        if row_level != want_level:
            continue

        subjects_csv = str(_safe_cell(row, subjects_idx, "") or "")
        subj_list = [s.strip().lower() for s in subjects_csv.split(",") if s.strip()]
        if want_subject not in subj_list:
            continue

        rid = _id_str_norm(_safe_cell(row, id_idx, ""))
        name = str(_safe_cell(row, name_idx, "")) if name_idx != -1 else ""
        if rid:
            recipients.append({"id": rid, "name": name})

    return recipients

async def zoom_get_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("المشرفون فقط.")
        return ConversationHandler.END
    url = update.message.text.strip()
    if not re.match(r"^https?://", url, re.I):
        await update.message.reply_text("الرابط غير صالح. يرجى لصق رابط يبدأ بـ http(s)://")
        return ZOOM_URL

    z = context.user_data.get('zoom', {})
    z['url'] = url
    context.user_data['zoom'] = z

    recipients = _find_zoom_recipients(z.get("niveau",""), z.get("subject",""))
    z['recipients'] = recipients
    context.user_data['zoom'] = z

    count = len(recipients)
    sample = ", ".join([f"{r['name']} ({r['id']})" for r in recipients[:5]]) or "—"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("نعم", callback_data="zoom_send_yes"),
         InlineKeyboardButton("إلغاء", callback_data="zoom_send_no")]
    ])
    await update.message.reply_text(
        f"هل تريد إرسال رابط Zoom إلى {count} طالب/طلاب؟\n"
        f"المستوى: {z.get('niveau')}, المادة: {z.get('subject')}\n"
        f"معاينة: {sample}\n\n{url}",
        reply_markup=kb
    )
    return ZOOM_CONFIRM

async def zoom_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.callback_query.edit_message_text("المشرفون فقط.")
        return ConversationHandler.END

    query = update.callback_query
    await query.answer()

    if query.data == "zoom_send_no":
        context.user_data.pop('zoom', None)
        await query.edit_message_text("تم إلغاء العملية.")
        return ConversationHandler.END

    z = context.user_data.get('zoom', {})
    recipients: List[Dict[str, str]] = z.get('recipients', [])
    url = z.get('url', '')
    niveau = z.get('niveau', '')
    subject = z.get('subject', '')

    if not recipients:
        await query.edit_message_text("لا يوجد طلاب مطابقون (تحقق من المستوى، المادة، أو حالة الاشتراك).")
        context.user_data.pop('zoom', None)
        return ConversationHandler.END

    ok, fail = 0, 0
    for r in recipients:
        chat_id = _chat_id(r['id'])
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📌 حصة Zoom لـ <b>{niveau} – {subject}</b>\n{url}",
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            ok += 1
        except Exception:
            fail += 1

    await query.edit_message_text(f"تم. أُرسل الرابط إلى {ok} طالب/طلاب. فشل الإرسال: {fail}.")
    context.user_data.pop('zoom', None)
    return ConversationHandler.END

# ===================== /register (student self-registration) =====================

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg'] = {}
    await update.message.reply_text(
        "مرحبًا! سنقوم بتسجيلك.\n"
        "أولًا، ما هو مستواك الدراسي (Niveau)؟\n"
        "اختر من: 3AS, 2AS, 1AS, 4AM, 3AM, 2AM, 1AM"
    )
    return REG_NIVEAU

async def register_get_niveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    niveau = update.message.text.strip().upper()
    if niveau not in ALLOWED_NIVEAUX:
        await update.message.reply_text(
            "المستوى غير صحيح.\n"
            "المستويات المسموح بها: 3AS, 2AS, 1AS, 4AM, 3AM, 2AM, 1AM\n"
            "يرجى المحاولة مرة أخرى:"
        )
        return REG_NIVEAU
    context.user_data['reg']['niveau'] = niveau
    await update.message.reply_text("الاسم الكامل للطالب:")
    return REG_NAME

async def register_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("يرجى إدخال اسم صحيح.")
        return REG_NAME
    context.user_data['reg']['name'] = name
    await update.message.reply_text("رقم الهاتف:")
    return REG_PHONE

async def register_get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone:
        await update.message.reply_text("يرجى إدخال رقم هاتف صحيح.")
        return REG_PHONE
    context.user_data['reg']['phone'] = phone
    await update.message.reply_text("اكتب موادك مفصولة بفواصل (مثال: Math, English):")
    return REG_SUBJECTS

async def register_get_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subjects_csv = update.message.text.strip()
    if not subjects_csv:
        await update.message.reply_text("يرجى إدخال مادة واحدة على الأقل.")
        return REG_SUBJECTS

    niveau = context.user_data['reg']['niveau']
    matched, _unknown = _match_user_subjects_to_canonical(niveau, subjects_csv)
    if not matched:
        await update.message.reply_text(
            "تعذّر فهم المواد المُدخلة. يرجى كتابتها بشكل أوضح (مثال: Math, English)."
        )
        return REG_SUBJECTS

    context.user_data['reg']['subjects_canonical'] = matched
    await update.message.reply_text("التخصص:")
    return REG_SPECIALITY

async def register_get_speciality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    speciality = update.message.text.strip()
    context.user_data['reg']['speciality'] = speciality
    await update.message.reply_text("طريقة الدفع:")
    return REG_PAYMENT

async def register_get_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.text.strip()
    context.user_data['reg']['payment'] = payment
    await update.message.reply_text("مدّة الاشتراك بالأشهر (مثل 1، 3…) أو تاريخ انتهاء (DD/MM/YYYY):")
    return REG_PERIOD

async def register_get_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    reg = context.user_data.get('reg', {})
    register_date = datetime.now().strftime('%Y-%m-%d')

    try:
        months = int(txt)
        if months <= 0:
            raise ValueError
        end_date = (datetime.now() + timedelta(days=months * 30)).strftime('%Y-%m-%d')
    except ValueError:
        try:
            input_date = datetime.strptime(txt, '%d/%m/%Y').date()
            end_date = input_date.strftime('%Y-%m-%d')
        except ValueError:
            await update.message.reply_text("المدّة غير صالحة. أدخل عدد الأشهر كرقم صحيح أو تاريخ بصيغة DD/MM/YYYY.")
            return REG_PERIOD

    reg['register_date'] = register_date
    reg['end_date'] = end_date
    reg['student_id'] = str(update.effective_user.id)  # take from sender
    context.user_data['reg'] = reg

    # Summary & confirm
    subjects_view = ", ".join(reg.get('subjects_canonical', []))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("تأكيد", callback_data="reg_confirm_yes"),
         InlineKeyboardButton("إلغاء", callback_data="reg_confirm_no")]
    ])
    await update.message.reply_text(
        "يرجى تأكيد بيانات التسجيل:\n"
        f"- الاسم: {reg.get('name')}\n"
        f"- الهاتف: {reg.get('phone')}\n"
        f"- المستوى: {reg.get('niveau')}\n"
        f"- المواد: {subjects_view}\n"
        f"- التخصص: {reg.get('speciality')}\n"
        f"- الدفع: {reg.get('payment')}\n"
        f"- تاريخ البدء: {register_date}\n"
        f"- تاريخ الانتهاء: {end_date}\n",
        reply_markup=kb
    )
    return REG_CONFIRM

async def register_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "reg_confirm_no":
        context.user_data.pop('reg', None)
        await query.edit_message_text("تم إلغاء التسجيل.")
        return ConversationHandler.END

    reg = context.user_data.get('reg')
    if not reg:
        await query.edit_message_text("لا توجد بيانات للتسجيل.")
        return ConversationHandler.END

    # Prepare subjects CSV (canonical)
    subjects_canon = reg.get('subjects_canonical', [])
    subjects_csv = ", ".join(subjects_canon)

    # Ensure Subject rows exist, then add student row
    ensure_subject_channels_rows(reg['niveau'], subjects_csv)

    add_student_row(
        phone=reg.get('phone', ''),
        name=reg.get('name', ''),
        subjects_csv=subjects_csv,
        speciality=reg.get('speciality', ''),
        payment=reg.get('payment', ''),
        student_id=reg.get('student_id', ''),
        register_date=reg.get('register_date', ''),
        end_date=reg.get('end_date', ''),
        subscription_status="TRUE",
        ten_days_sent="FALSE",
        three_days_sent="FALSE",
        niveau=reg.get('niveau', '')
    )

    # Send invites (only for subjects that have a group id)
    keys = [f"{reg['niveau']}_{re.sub(r'\\s+', '_', s.strip())}".lower() for s in subjects_canon]
    try:
        await invite_student_to_subject_groups(context.bot, reg['student_id'], keys)
    except Exception as e:
        logger.warning(f"[register_confirm] Invite sending failed: {e}")

    context.user_data.pop('reg', None)
    await query.edit_message_text("✅ تم تسجيلك بنجاح! تم إرسال روابط الدعوة للمواد المتوفرة.")
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

    # ---- Admin conversations ----
    set_conv = ConversationHandler(
        entry_points=[CommandHandler("set", set_channel_start, filters=(filters.ChatType.GROUPS & ~filters.SenderChat()))],
        states={
            SET_NIVEAU:  [MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_niveau)],
            SET_SUBJECT: [MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, set_channel_get_subject),
                          CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$")],
            SET_CONFIRM: [CallbackQueryHandler(set_channel_confirm, pattern=r"^set_confirm_(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", set_channel_cancel, filters=filters.ChatType.GROUPS)],
        allow_reentry=True,
    )
    application.add_handler(set_conv)

    zoom_conv = ConversationHandler(
        entry_points=[CommandHandler("zoom", zoom_start)],
        states={
            ZOOM_NIVEAU:  [MessageHandler(filters.TEXT & ~filters.COMMAND, zoom_get_niveau)],
            ZOOM_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, zoom_get_subject)],
            ZOOM_URL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, zoom_get_url)],
            ZOOM_CONFIRM: [CallbackQueryHandler(zoom_confirm, pattern=r"^zoom_send_(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", set_channel_cancel)],
        allow_reentry=True,
    )
    application.add_handler(zoom_conv)

    # ---- Student registration conversation ----
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            REG_NIVEAU:    [MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_niveau)],
            REG_NAME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_name)],
            REG_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_phone)],
            REG_SUBJECTS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_subjects)],
            REG_SPECIALITY:[MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_speciality)],
            REG_PAYMENT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_payment)],
            REG_PERIOD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, register_get_period)],
            REG_CONFIRM:   [CallbackQueryHandler(register_confirm, pattern=r"^reg_confirm_(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", set_channel_cancel)],
        allow_reentry=True,
    )
    application.add_handler(register_conv)

    # ---- Public commands ----
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
            # Build client with cache disabled (avoids extra disk i/o and warnings)
            creds = _load_gcp_credentials()
            service = build('sheets', 'v4', credentials=creds, cache_discovery=False)

            # Small, fast call
            rng = f"{STUDENT_TABLE_NAME}!A1:A1"
            service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=rng
            ).execute()
        except Exception as e:
            logger.warning("[prewarm_clients] Warm-up skipped/failed: %s", e)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync)
