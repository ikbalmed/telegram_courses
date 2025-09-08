# admin_bot.py
import os
import re
import uuid
import json
import base64
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot
from telegram.ext import (
    Application, ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Invite helper from the student bot (used after adding a student)
from student_bot import invite_student_to_subject_groups

load_dotenv()
logger = logging.getLogger("admin_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ========================= Config =========================
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
STUDENTS_SHEET = os.getenv("STUDENT_TABLE_NAME", "Students")
SUBJECTS_CHANNELS_SHEET = os.getenv("SUBJECTS_CHANNEL_TABLE_NAME", "Subjects_Channels")
STUDENTS_RANGE = f"{STUDENTS_SHEET}!A2:L"  # Phone..Name..Subjects..Speciality..Payment..ID..Register..End..Sub..10d..3d..Niveau

STUDENT_BOT_TOKEN = os.getenv("STUDENT_BOT_TOKEN")  # used to DM Zoom links & invites

# ========================= Admins =========================
def _parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: set[int] = set()
    for tok in re.split(r"[,\s]+", raw.strip().strip("'").strip('"')):
        if tok and tok.strip().lstrip("-").isdigit():
            ids.add(int(tok))
    return ids

ADMIN_IDS: set[int] = _parse_admin_ids()
ADMIN_FILTER = filters.User(user_id=list(ADMIN_IDS)) if ADMIN_IDS else filters.User(user_id=[])

async def _deny(update: Update):
    try:
        if update.callback_query:
            await update.callback_query.answer("المشرفون فقط.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("المشرفون فقط.")
    except Exception:
        pass

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in ADMIN_IDS:
            await _deny(update)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper

# ========================= Google Sheets helpers =========================
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

    raise RuntimeError("No Google credentials provided. Set GOOGLE_CREDENTIALS_FILE or GOOGLE_CREDENTIALS_JSON_B64 or GOOGLE_CREDENTIALS_JSON.")

def setup_sheets():
    creds = _load_gcp_credentials()
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets(), service

def get_sheet_id_by_title(service, title: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sh in meta.get('sheets', []):
        if sh.get('properties', {}).get('title') == title:
            return sh.get('properties', {}).get('sheetId')
    raise ValueError(f"Sheet '{title}' not found.")

def read_students_values():
    sheets, _ = setup_sheets()
    result = sheets.values().get(spreadsheetId=SPREADSHEET_ID, range=STUDENTS_RANGE).execute()
    return result.get('values', [])

def _norm(s: object) -> str:
    return re.sub(r'[^a-z0-9]', '', str(s).lower())

def _header_index_alias(headers: List[str], aliases: List[str],
                        contains_any: Optional[List[str]] = None,
                        contains_all: Optional[List[str]] = None) -> int:
    hdr_norm = [_norm(h) for h in headers]
    for al in aliases:
        try:
            return headers.index(al)
        except ValueError:
            pass
    if contains_all:
        toks = [_norm(t) for t in contains_all]
        for i, h in enumerate(hdr_norm):
            if all(t in h for t in toks):
                return i
    if contains_any:
        toks = [_norm(t) for t in contains_any]
        for i, h in enumerate(hdr_norm):
            if any(t in h for t in toks):
                return i
    return -1

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

def _safe_cell(row: List[object], idx: int, default: object=""):
    return row[idx] if idx != -1 and len(row) > idx else default

def _chat_id(value: str | int) -> int | str:
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return int(s) if s.lstrip("-").isdigit() else s

# ---------- Subjects_Channels ensure ----------
def ensure_subject_channels_rows(niveau: str, subjects_csv: str):
    if not subjects_csv:
        return
    subjects = [s.strip() for s in subjects_csv.split(',') if s.strip()]
    sheets, _ = setup_sheets()
    existing = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{SUBJECTS_CHANNELS_SHEET}!A2:A'
    ).execute().get('values', [])
    existing_keys = set(v[0] for v in existing if v)
    to_append = []
    for subj in subjects:
        normalized = re.sub(r'\s+', '_', subj)
        key = f"{niveau}_{normalized}"
        if key not in existing_keys:
            to_append.append([key, ""])
    if to_append:
        sheets.values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f'{SUBJECTS_CHANNELS_SHEET}!A:B',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': to_append}
        ).execute()

# ========================= Student CRUD helpers =========================
def check_phone_exists(phone_number):
    values = read_students_values()
    matches = []
    for r_idx, row in enumerate(values):
        if len(row) >= 1 and str(row[0]).strip() == str(phone_number).strip():
            matches.append({'row_number': r_idx + 2, 'data': row})
    return (len(matches) > 0), matches

def check_telegram_id_exists(telegram_id):
    values = read_students_values()
    matches = []
    for r_idx, row in enumerate(values):
        if len(row) > 5 and _id_str_norm(row[5]) == _id_str_norm(telegram_id):
            matches.append({'row_number': r_idx + 2, 'data': row})
    return (len(matches) > 0), matches

def delete_student(row_number):
    sheets, service = setup_sheets()
    sheet_id = get_sheet_id_by_title(service, STUDENTS_SHEET)
    request = {
        'requests': [{
            'deleteDimension': {
                'range': {'sheetId': sheet_id, 'dimension': 'ROWS',
                          'startIndex': row_number - 1, 'endIndex': row_number}
            }
        }]
    }
    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=request).execute()

def add_student(phone, name, subjects, speciality, payment, student_id,
                register_date, end_date, subscription_status,
                ten_days_reminder_sent, three_days_reminder_sent, niveau):
    sheets, _ = setup_sheets()
    values = [[
        phone, name, subjects, speciality, payment, student_id,
        register_date, end_date, subscription_status,
        ten_days_reminder_sent, three_days_reminder_sent, niveau
    ]]
    sheets.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=STUDENTS_RANGE,
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': values}
    ).execute()
    return student_id

# ========================= Conversation states =========================
(
    PHONE, NAME, TELEGRAM_ID, SUBJECTS, SPECIALITY, PAYMENT,
    SUBSCRIPTION_PERIOD, EDIT_COLUMN, EDIT_VALUE, RENEW_SUBSCRIPTION_PERIOD,
    ZOOM_SUBJECT, ZOOM_URL, CONFIRM_ADD,
    ZOOM_NIVEAU, ZOOM_CONFIRM
) = range(15)

EDIT_STUDENT = 'edit_student'
DELETE_STUDENT = 'delete_student'
ADD_NEW_STUDENT_SAME_NUMBER = 'add_new_student_same_number'

# ========================= /add_student flow =========================
@admin_only
async def start_add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /add_student 1AS
    niveau = None
    if context.args and len(context.args) >= 1:
        niveau = context.args[0].strip().upper()
    if not niveau:
        await update.message.reply_text("الاستخدام: /add_student <المستوى>\nمثال: /add_student 1AS")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['niveau'] = niveau
    context.user_data['new_flow'] = True
    context.user_data['adding_same_phone'] = False
    await update.message.reply_text("يرجى إدخال اسم الطالب:")
    return NAME

@admin_only
async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text.strip()
    if context.user_data.get('adding_same_phone'):
        await update.message.reply_text("يرجى إدخال معرّف تيليجرام للطالب:")
        return TELEGRAM_ID
    await update.message.reply_text("يرجى إدخال رقم هاتف الطالب:")
    return PHONE

@admin_only
async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('adding_same_phone'):
        await update.message.reply_text("يرجى إدخال معرّف تيليجرام للطالب:")
        return TELEGRAM_ID

    phone = update.message.text.strip()
    context.user_data['phone'] = phone

    exists, students = check_phone_exists(phone)
    if exists:
        for student_info in students:
            row_number = student_info['row_number']
            data = student_info['data']
            keyboard = [
                [InlineKeyboardButton("تعديل الطالب", callback_data=f"{EDIT_STUDENT}_student_{row_number}")],
                [InlineKeyboardButton("حذف الطالب", callback_data=f"{DELETE_STUDENT}_student_{row_number}")]
            ]
            await update.message.reply_text(
                "تم العثور على طالب:\n"
                f"الهاتف: {data[0]}\nالاسم: {data[1]}\nالمواد: {data[2]}\n"
                f"التخصص: {data[3]}\nالدفع: {data[4]}\n"
                f"تاريخ التسجيل: {data[6] if len(data)>6 else 'غير متاح'}\n"
                f"تاريخ الانتهاء: {data[7] if len(data)>7 else 'غير متاح'}\n"
                f"الاشتراك: {data[8] if len(data)>8 else 'غير متاح'}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        add_new_keyboard = [[InlineKeyboardButton("إضافة طالب جديد بنفس الرقم", callback_data=ADD_NEW_STUDENT_SAME_NUMBER)]]
        await update.message.reply_text("الخيارات:", reply_markup=InlineKeyboardMarkup(add_new_keyboard))
        return ConversationHandler.END

    await update.message.reply_text("يرجى إدخال معرّف تيليجرام للطالب:")
    return TELEGRAM_ID

@admin_only
async def handle_telegram_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id_input = update.message.text.strip()
    if telegram_id_input.lower() == 'skip':
        telegram_id_input = str(uuid.uuid4())[:8]
    context.user_data['telegram_id'] = telegram_id_input

    exists, students = check_telegram_id_exists(telegram_id_input)
    if exists:
        for student_info in students:
            row_number = student_info['row_number']
            data = student_info['data']
            keyboard = [
                [InlineKeyboardButton("تعديل الطالب", callback_data=f"{EDIT_STUDENT}_student_{row_number}")],
                [InlineKeyboardButton("حذف الطالب", callback_data=f"{DELETE_STUDENT}_student_{row_number}")]
            ]
            await update.message.reply_text(
                "يوجد طالب بهذا معرّف تيليجرام:\n"
                f"الهاتف: {data[0]}\nالاسم: {data[1]}\nالمواد: {data[2]}\n"
                f"التخصص: {data[3]}\nالدفع: {data[4]}\n"
                f"تاريخ التسجيل: {data[6] if len(data)>6 else 'غير متاح'}\n"
                f"تاريخ الانتهاء: {data[7] if len(data)>7 else 'غير متاح'}\n"
                f"الاشتراك: {data[8] if len(data)>8 else 'غير متاح'}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return ConversationHandler.END

    await update.message.reply_text("يرجى إدخال مواد الطالب (مفصولة بفواصل):")
    return SUBJECTS

@admin_only
async def handle_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['subjects'] = update.message.text.strip()
    await update.message.reply_text("يرجى إدخال التخصص:")
    return SPECIALITY

@admin_only
async def handle_speciality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['speciality'] = update.message.text.strip()
    await update.message.reply_text("يرجى إدخال طريقة الدفع:")
    return PAYMENT

@admin_only
async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['payment'] = update.message.text.strip()
    await update.message.reply_text("يرجى إدخال مدة الاشتراك بالأشهر (مثال: 1، 3، 6، 12) أو تاريخ انتهاء محدد (DD/MM/YYYY):")
    return SUBSCRIPTION_PERIOD

@admin_only
async def handle_subscription_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
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
            await update.message.reply_text("مدة غير صالحة. أدخل عدد الأشهر كعدد صحيح أو تاريخ بصيغة DD/MM/YYYY.")
            return SUBSCRIPTION_PERIOD

    context.user_data['pending_student'] = {
        'phone': context.user_data.get('phone', ''),
        'name': context.user_data.get('name', ''),
        'subjects': context.user_data.get('subjects', ''),
        'speciality': context.user_data.get('speciality', ''),
        'payment': context.user_data.get('payment', ''),
        'telegram_id': context.user_data.get('telegram_id', str(uuid.uuid4())[:8]),
        'register_date': register_date,
        'end_date': end_date,
        'subscription_status': "TRUE",
        'ten_days_reminder_sent': "FALSE",
        'three_days_reminder_sent': "FALSE",
        'niveau': context.user_data.get('niveau', '')
    }

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("نعم", callback_data="confirm_add_yes"),
                                InlineKeyboardButton("لا", callback_data="confirm_add_no")]])
    await update.message.reply_text(
        f"هل أنت متأكد أنك تريد إضافة هذا الطالب بالمستوى: {context.user_data.get('niveau','')}",
        reply_markup=kb
    )
    return CONFIRM_ADD

@admin_only
async def confirm_add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_add_no":
        context.user_data.pop('pending_student', None)
        await query.edit_message_text("تم إلغاء العملية.")
        return ConversationHandler.END

    pending = context.user_data.get('pending_student')
    if not pending:
        await query.edit_message_text("لا توجد عملية إضافة معلّقة.")
        return ConversationHandler.END

    # Ensure Subjects_Channels keys exist for this niveau+subjects
    ensure_subject_channels_rows(pending['niveau'], pending['subjects'])

    # Append student to Students
    add_student(
        pending['phone'], pending['name'], pending['subjects'], pending['speciality'],
        pending['payment'], pending['telegram_id'], pending['register_date'],
        pending['end_date'], pending['subscription_status'],
        pending['ten_days_reminder_sent'], pending['three_days_reminder_sent'], pending['niveau']
    )

    # Auto-send subject group invites if mapped
    keys = [f"{pending['niveau']}_{re.sub(r'\\s+', '_', s.strip())}".lower()
            for s in pending['subjects'].split(',') if s.strip()]
    if keys and STUDENT_BOT_TOKEN:
        student_bot = Bot(STUDENT_BOT_TOKEN)
        try:
            await invite_student_to_subject_groups(student_bot, pending['telegram_id'], keys)
        except Exception as e:
            await query.message.reply_text(f"تمت إضافة الطالب، لكن فشل إرسال الدعوات: {e}")

    context.user_data.pop('pending_student', None)
    context.user_data['adding_same_phone'] = False
    await query.edit_message_text("تمت إضافة الطالب بنجاح!")
    return ConversationHandler.END

# ========================= Edit/Delete callbacks =========================
@admin_only
async def start_edit_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    row_number = None
    if query.data.startswith(EDIT_STUDENT + '_student_'):
        row_number_str = query.data.replace(EDIT_STUDENT + '_student_', '', 1)
        if row_number_str.isdigit():
            row_number = int(row_number_str)
    if row_number:
        context.user_data['edit_row_number'] = row_number
        keyboard = [
            [InlineKeyboardButton("الهاتف", callback_data='edit_column_phone')],
            [InlineKeyboardButton("الاسم", callback_data='edit_column_name')],
            [InlineKeyboardButton("المواد", callback_data='edit_column_subjects')],
            [InlineKeyboardButton("التخصص", callback_data='edit_column_speciality')],
            [InlineKeyboardButton("الدفع", callback_data='edit_column_payment')]
        ]
        await query.edit_message_text("أي عمود تريد تعديله؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return EDIT_COLUMN
    else:
        await query.edit_message_text("تعذّر تعديل الطالب: لم يتم العثور على رقم الصف.")
        return ConversationHandler.END

@admin_only
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith(EDIT_STUDENT + '_student_'):
        await start_edit_student(update, context)
        return

    if query.data.startswith(DELETE_STUDENT + '_student_'):
        row_number_str = query.data.replace(DELETE_STUDENT + '_student_', '', 1)
        if row_number_str.isdigit():
            delete_student(int(row_number_str))
            await query.edit_message_text("تم حذف الطالب بنجاح!")
        else:
            await query.edit_message_text("تعذّر حذف الطالب: رقم الصف غير موجود.")

    if query.data == ADD_NEW_STUDENT_SAME_NUMBER:
        await start_add_new_student_same_number(update, context)
        return

@admin_only
async def start_add_new_student_same_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    phone = context.user_data.get('phone')
    if phone:
        context.user_data['adding_same_phone'] = True
        await query.edit_message_text(
            f"إضافة طالب جديد بنفس الرقم: {phone}.\nيرجى إدخال اسم الطالب:"
        )
        return NAME
    else:
        await query.edit_message_text("تعذّر استرجاع رقم الهاتف. يرجى البدء من جديد باستخدام /add_student.")
        return ConversationHandler.END

@admin_only
async def handle_edit_column(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    column_name = query.data.replace('edit_column_', '')
    valid_columns = {"phone": 0, "name": 1, "subjects": 2, "speciality": 3, "payment": 4}
    if column_name in valid_columns:
        context.user_data['edit_column_index'] = valid_columns[column_name]
        await query.edit_message_text("يرجى إدخال القيمة الجديدة:")
        return EDIT_VALUE
    else:
        await query.edit_message_text("اسم عمود غير صالح. الرجاء الاختيار من: Phone, Name, Subjects, Speciality, Payment.")
        return EDIT_COLUMN

@admin_only
async def handle_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_value = update.message.text.strip()
    row_number = context.user_data.get('edit_row_number')
    column_index = context.user_data.get('edit_column_index')

    if row_number is None or column_index is None:
        await update.message.reply_text("خطأ: تعذّر استرجاع معلومات التعديل.")
        return ConversationHandler.END

    sheets, _ = setup_sheets()
    col_letter = chr(ord('A') + column_index)  # A..E
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{STUDENTS_SHEET}!{col_letter}{row_number}',
        valueInputOption='RAW',
        body={'values': [[new_value]]}
    ).execute()

    await update.message.reply_text("تم تحديث بيانات الطالب بنجاح!")
    context.user_data.pop('edit_row_number', None)
    context.user_data.pop('edit_column_index', None)
    return ConversationHandler.END

# ========================= Zoom (send DM per student) =========================
@admin_only
async def zoom_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('zoom', None)
    await update.message.reply_text("لأي مستوى تريد إرسال رابط Zoom؟ (مثل 1AS، 2AS، 3AS)")
    return ZOOM_NIVEAU

@admin_only
async def zoom_get_niveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    niveau = update.message.text.strip().upper()
    if not niveau:
        await update.message.reply_text("يرجى إدخال مستوى صالح، مثل 3AS.")
        return ZOOM_NIVEAU
    context.user_data['zoom'] = {'niveau': niveau}
    await update.message.reply_text(f"ما هي المادة للمستوى {niveau}؟ (مثل: Math, Physic, English)")
    return ZOOM_SUBJECT

@admin_only
async def zoom_get_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()
    if not subject:
        await update.message.reply_text("يرجى إدخال مادة صالحة، مثل Math.")
        return ZOOM_SUBJECT
    context.user_data['zoom']['subject'] = subject
    await update.message.reply_text("يرجى لصق رابط اجتماع Zoom:")
    return ZOOM_URL

def _find_zoom_recipients(niveau: str, subject: str) -> List[Dict[str, str]]:
    sheets, _ = setup_sheets()
    res = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENTS_SHEET}!A:Z",
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

@admin_only
async def zoom_get_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not re.match(r"^https?://", url, re.I):
        await update.message.reply_text("يبدو أن الرابط غير صالح. يرجى لصق رابط يبدأ بـ http(s)://")
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

@admin_only
async def zoom_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "zoom_send_no":
        context.user_data.pop('zoom', None)
        await query.edit_message_text("تم إلغاء العملية.")
        return ConversationHandler.END

    if not STUDENT_BOT_TOKEN:
        await query.edit_message_text("لم يتم ضبط STUDENT_BOT_TOKEN؛ لا يمكن الإرسال عبر الرسائل الخاصة.")
        context.user_data.pop('zoom', None)
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

    student_bot = Bot(STUDENT_BOT_TOKEN)

    ok, fail = 0, 0
    for r in recipients:
        chat_id = _chat_id(r['id'])
        try:
            await student_bot.send_message(
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

# ========================= Cancel =========================
@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم إلغاء العملية.")
    return ConversationHandler.END

# ========================= Application factory (WEBHOOK-READY) =========================
async def main(student_app=None, updater_none: bool = False):
    token = os.getenv("ADMIN_BOT_TOKEN")
    builder = Application.builder().token(token)
    if updater_none:
        builder = builder.updater(None)  # disable Updater for webhook mode
    application = builder.build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('add_student', start_add_student, filters=ADMIN_FILTER),
            CommandHandler('zoom', zoom_start, filters=ADMIN_FILTER),
            CallbackQueryHandler(start_edit_student, pattern='^' + EDIT_STUDENT + '_student_'),
            CallbackQueryHandler(handle_callback, pattern='^' + DELETE_STUDENT + '_student_'),
            CallbackQueryHandler(start_add_new_student_same_number, pattern='^' + ADD_NEW_STUDENT_SAME_NUMBER + '$'),
        ],
        states={
            NAME:   [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_name)],
            PHONE:  [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_phone)],
            TELEGRAM_ID: [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_telegram_id)],
            SUBJECTS:    [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_subjects)],
            SPECIALITY:  [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_speciality)],
            PAYMENT:     [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_payment)],
            SUBSCRIPTION_PERIOD: [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_subscription_period)],
            CONFIRM_ADD: [CallbackQueryHandler(confirm_add_student, pattern=r'^confirm_add_(yes|no)$')],
            EDIT_COLUMN: [CallbackQueryHandler(handle_edit_column, pattern='^edit_column_')],
            EDIT_VALUE:  [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, handle_edit_value)],
            ZOOM_NIVEAU:  [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, zoom_get_niveau)],
            ZOOM_SUBJECT: [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, zoom_get_subject)],
            ZOOM_URL:     [MessageHandler(ADMIN_FILTER & filters.TEXT & ~filters.COMMAND, zoom_get_url)],
            ZOOM_CONFIRM: [CallbackQueryHandler(zoom_confirm, pattern=r'^zoom_send_(yes|no)$')],
        ],
        fallbacks=[CommandHandler('cancel', cancel, filters=ADMIN_FILTER)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)

    async def _not_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _deny(update)
    if ADMIN_IDS:
        application.add_handler(
            MessageHandler(filters.ChatType.PRIVATE & ~ADMIN_FILTER & (filters.TEXT | filters.COMMAND), _not_admin_message)
        )

    print(f"Admin bot started with {len(ADMIN_IDS)} admin(s).")
    return application

# --- Optional warm-up for Render cold starts (admin_bot) ----------------------
import asyncio

async def prewarm_clients():
    """
    Pre-initialize Google Sheets creds + client and touch both sheets so the
    first real webhook doesn’t pay the cold-start cost. Safe to call multiple times.
    """
    def _sync():
        try:
            creds = _load_gcp_credentials()
            # cache_discovery=False avoids extra disk I/O & warnings
            service = build('sheets', 'v4', credentials=creds, cache_discovery=False)

            # Tiny, fast reads just to establish TLS, mint tokens, etc.
            service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{STUDENTS_SHEET}!A1:A1"
            ).execute()
            service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{SUBJECTS_CHANNELS_SHEET}!A1:A1"
            ).execute()
        except Exception as e:
            logger.warning("[admin_bot prewarm_clients] Warm-up skipped/failed: %s", e)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync)
