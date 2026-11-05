"""Учёт смен — облачная версия.

Один запуск = один проход по накопившимся апдейтам Telegram + сохранение состояния.
Запускается по расписанию GitHub Actions, состояние лежит в state.enc (шифрование Fernet).
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
BOT_TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = os.environ.get("STATE_FILE", "state.enc")
KEY = Fernet(os.environ["STATE_KEY"])
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "540"))
client = httpx.Client(timeout=60)

MENU = [
    ("🕒 Начать смену", "shift:start"),
    ("🛑 Закончить смену", "shift:end"),
    ("👤 Сотрудники", "emp:list"),
    ("💰 Выдать аванс", "adv:add"),
    ("📊 Отчёт за месяц", "rep:month"),
    ("📋 Все смены", "rep:shifts"),
    ("❓ Помощь", "help"),
]


# ---------------------------------------------------------------- состояние
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "rb") as f:
            return json.loads(KEY.decrypt(f.read()).decode("utf-8"))
    return {"offset": 0, "seq": 0, "steps": {}, "employees": [], "shifts": [], "advances": []}


def save_state(st):
    with open(STATE_FILE, "wb") as f:
        f.write(KEY.encrypt(json.dumps(st, ensure_ascii=False).encode("utf-8")))


def api(method, **params):
    try:
        r = client.post(f"{API}/{method}", json=params)
        return r.json()
    except Exception:
        return {"ok": False}


def send(chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return api("sendMessage", **payload)


def answer(cb_id, text=""):
    api("answerCallbackQuery", callback_query_id=cb_id, text=text)


# ---------------------------------------------------------------- хелперы
def now():
    return datetime.now(TZ)


def money(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def kb(rows):
    return [[{"text": t, "callback_data": d}] for t, d in rows]


def menu_kb():
    return kb(MENU)


def back_kb():
    return kb([("⬅️ Назад", "menu")])


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def set_step(st, chat_id, step, data=None):
    st["steps"][str(chat_id)] = {"step": step, "data": data or {}}


def get_step(st, chat_id):
    return st["steps"].get(str(chat_id))


def clear_step(st, chat_id):
    st["steps"].pop(str(chat_id), None)


def employees(st, active_only=True):
    return [e for e in st["employees"] if not active_only or e.get("active", 1)]


def emp_by_id(st, eid):
    for e in st["employees"]:
        if e["id"] == eid:
            return e
    return None


def next_id(st, key):
    st["seq"] = st.get("seq", 0) + 1
    return st["seq"]


def open_shift(st, eid):
    for s in st["shifts"]:
        if s["employee_id"] == eid and not s.get("end_ts"):
            return s
    return None


def month_bounds():
    n = now()
    start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def earnings_for(emp, hours):
    if emp.get("mode") == "month":
        return emp.get("salary", 0)
    return round(hours * emp.get("rate", 0), 2)


def parse_time(text):
    m = re.search(r"(\d{1,2})\s*[:.\-]?\s*(\d{2})?", text.strip())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return None
    return h, mm


def money_parse(text):
    raw = text.replace(" ", "").replace(",", ".").replace("₽", "").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def greeting():
    return ("👋 <b>Учёт смен</b>\nСчитаю смены, часы, зарплату и авансы.\nВыбери действие:")


# ---------------------------------------------------------------- отчёты
def report_emp(st, emp):
    start, end = month_bounds()
    shifts = [s for s in st["shifts"] if s["employee_id"] == emp["id"] and start.isoformat() <= s["start_ts"] < end.isoformat()]
    closed = [s for s in shifts if s.get("hours") is not None]
    hours = round(sum(s["hours"] for s in closed), 2)
    gross = earnings_for(emp, hours)
    advs = [a for a in st["advances"] if a["employee_id"] == emp["id"] and start.strftime("%Y-%m-%d") <= a["date"] < end.strftime("%Y-%m-%d")]
    adv_total = round(sum(a["amount"] for a in advs), 2)
    rate = f"оклад {money(emp.get('salary', 0))} ₽/мес" if emp.get("mode") == "month" else f"{money(emp.get('rate', 0))} ₽/час"
    lines = [
        f"📊 <b>{esc(emp['name'])}</b> — {now().strftime('%m.%Y')}",
        f"Смен: {len(closed)}",
        f"Отработано: <b>{hours} ч</b>",
        f"Ставка: {rate}",
        f"Начислено: <b>{money(gross)} ₽</b>",
        f"Авансов: {len(advs)} на {money(adv_total)} ₽",
        f"К выплате: <b>{money(gross - adv_total)} ₽</b>",
    ]
    if open_shift(st, emp["id"]):
        lines.append("⏳ Есть открытая смена — она не учтена.")
    return "\n".join(lines)


def report_all(st):
    start, end = month_bounds()
    lines = [f"📊 <b>Отчёт за {now().strftime('%m.%Y')}</b>"]
    tg = ta = 0.0
    for emp in st["employees"]:
        shifts = [s for s in st["shifts"] if s["employee_id"] == emp["id"] and start.isoformat() <= s["start_ts"] < end.isoformat()]
        closed = [s for s in shifts if s.get("hours") is not None]
        hours = round(sum(s["hours"] for s in closed), 2)
        gross = earnings_for(emp, hours)
        advs = [a for a in st["advances"] if a["employee_id"] == emp["id"] and start.strftime("%Y-%m-%d") <= a["date"] < end.strftime("%Y-%m-%d")]
        adv_total = round(sum(a["amount"] for a in advs), 2)
        tg += gross
        ta += adv_total
        lines.append(
            f"• <b>{esc(emp['name'])}</b>: {hours} ч, начислено {money(gross)} ₽, "
            f"аванс {money(adv_total)} ₽, к выплате {money(gross - adv_total)} ₽"
        )
    lines.append(f"— всего к выплате: <b>{money(tg - ta)} ₽</b>")
    return "\n".join(lines)


def shifts_list(st):
    start, end = month_bounds()
    rows = [s for s in st["shifts"] if start.isoformat() <= s["start_ts"] < end.isoformat()]
    if not rows:
        return "В этом месяце смен нет."
    emp_names = {e["id"]: e["name"] for e in st["employees"]}
    lines = ["📋 <b>Смены за месяц</b>"]
    for s in sorted(rows, key=lambda x: x["start_ts"]):
        st_ = datetime.fromisoformat(s["start_ts"]).astimezone(TZ)
        en = datetime.fromisoformat(s["end_ts"]).astimezone(TZ) if s.get("end_ts") else None
        h = f"{s['hours']} ч" if s.get("hours") is not None else "не закрыта"
        lines.append(
            f"• {esc(emp_names.get(s['employee_id'], '?'))} — {st_.strftime('%d.%m %H:%M')} → "
            f"{(en.strftime('%H:%M') if en else '...')} ({h})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- хендлеры
def handle_text(st, chat_id, text):
    step = get_step(st, chat_id)
    if not step:
        send(chat_id, greeting(), menu_kb())
        return
    s = step["step"]
    d = step["data"]

    if s == "emp_name":
        set_step(st, chat_id, "emp_rate", {"name": text.strip()})
        send(chat_id, f"Ставка сотрудника <b>{esc(text.strip())}</b>.\nВведи часовую ставку в рублях (например, 350). Если месячный оклад — введи 0.", back_kb())
        return

    if s == "emp_rate":
        rate = money_parse(text)
        if rate is None:
            send(chat_id, "Нужно число. Например: 350")
            return
        if rate <= 0:
            set_step(st, chat_id, "emp_salary", d)
            send(chat_id, "Введи месячный оклад в рублях (например, 45000):", back_kb())
            return
        st["employees"].append({"id": next_id(st, "emp"), "name": d["name"], "mode": "hour", "rate": rate, "salary": 0, "active": 1})
        clear_step(st, chat_id)
        send(chat_id, f"✅ Сотрудник <b>{esc(d['name'])}</b> добавлен: {money(rate)} ₽/час.", menu_kb())
        return

    if s == "emp_salary":
        salary = money_parse(text)
        if salary is None:
            send(chat_id, "Нужно число. Например: 45000")
            return
        st["employees"].append({"id": next_id(st, "emp"), "name": d["name"], "mode": "month", "rate": 0, "salary": salary, "active": 1})
        clear_step(st, chat_id)
        send(chat_id, f"✅ Сотрудник <b>{esc(d['name'])}</b> добавлен: оклад {money(salary)} ₽/мес.", menu_kb())
        return

    if s == "shift_start":
        t = parse_time(text)
        if t is None:
            send(chat_id, "Не понял время. Формат: <code>09:30</code>")
            return
        emp = emp_by_id(st, d["emp_id"])
        start = now().replace(hour=t[0], minute=t[1], second=0, microsecond=0)
        if start > now():
            start -= timedelta(days=1)
        st["shifts"].append({"id": next_id(st, "shift"), "employee_id": d["emp_id"], "start_ts": start.isoformat(timespec="seconds"), "end_ts": None, "hours": None, "note": ""})
        clear_step(st, chat_id)
        send(chat_id, f"✅ Смена начата: <b>{esc(emp['name'])}</b> в {start.strftime('%H:%M')}.", menu_kb())
        return

    if s == "shift_end":
        t = parse_time(text)
        if t is None:
            send(chat_id, "Не понял время. Формат: <code>18:30</code>")
            return
        shift = next((s_ for s_ in st["shifts"] if s_["id"] == d["shift_id"]), None)
        if not shift:
            clear_step(st, chat_id)
            send(chat_id, "Смена не найдена.", menu_kb())
            return
        end = now().replace(hour=t[0], minute=t[1], second=0, microsecond=0)
        start_dt = datetime.fromisoformat(shift["start_ts"])
        hours = round((end - start_dt).total_seconds() / 3600, 2)
        shift["end_ts"] = end.isoformat(timespec="seconds")
        shift["hours"] = hours
        clear_step(st, chat_id)
        send(chat_id, f"✅ Смена закрыта. Отработано: <b>{hours} ч</b>.", menu_kb())
        return

    if s == "adv_amount":
        amount = money_parse(text)
        if amount is None or amount <= 0:
            send(chat_id, "Нужно число больше нуля. Например: 5000")
            return
        set_step(st, chat_id, "adv_note", {"emp_id": d["emp_id"], "amount": amount})
        send(chat_id, f"Аванс {money(amount)} ₽. Добавь комментарий (можно «-»):", back_kb())
        return

    if s == "adv_note":
        note = "" if text.strip() == "-" else text.strip()
        st["advances"].append({"id": next_id(st, "adv"), "employee_id": d["emp_id"], "amount": d["amount"], "note": note, "date": now().strftime("%Y-%m-%d")})
        emp = emp_by_id(st, d["emp_id"])
        clear_step(st, chat_id)
        send(chat_id, f"✅ Аванс <b>{money(d['amount'])} ₽</b> записан: {esc(emp['name'])}.", menu_kb())
        return

    send(chat_id, "Не понял. Начни с кнопок 👇", menu_kb())


def handle_callback(st, cb):
    cid = cb.get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    answer(cid)

    if data == "menu":
        clear_step(st, chat_id)
        send(chat_id, greeting(), menu_kb())
        return

    if data == "help":
        send(chat_id, "• Смена: <b>Начать</b> → сотрудник → время старта.\n• Закрытие: <b>Закончить</b> → сотрудник → время закрытия.\n• Аванс: <b>Выдать аванс</b> → сотрудник → сумма → комментарий.\n• Отчёт: <b>Отчёт за месяц</b> → сотрудник или все сразу.", menu_kb())
        return

    if data == "emp:list":
        lines = []
        for e in employees(st):
            lines.append(f"• {esc(e['name'])} — " + (f"оклад {money(e['salary'])} ₽/мес" if e.get("mode") == "month" else f"{money(e['rate'])} ₽/час"))
        text = "<b>👤 Сотрудники</b>\n" + ("\n".join(lines) if lines else "Список пуст.")
        send(chat_id, text, kb([("➕ Добавить сотрудника", "emp:add"), ("⬅️ Назад", "menu")]))
        return

    if data == "emp:add":
        set_step(st, chat_id, "emp_name")
        send(chat_id, "Введи имя сотрудника:", back_kb())
        return

    if data == "shift:start":
        emps = employees(st)
        if not emps:
            send(chat_id, "Сначала добавь сотрудника: <b>👤 Сотрудники</b>.", menu_kb())
            return
        set_step(st, chat_id, "shift_pick")
        send(chat_id, "🕒 <b>Начать смену</b>\nКто выходит на смену?", kb([(e["name"], f"sh:{e['id']}") for e in emps] + [("⬅️ Назад", "menu")]))
        return

    if data.startswith("sh:"):
        eid = int(data.split(":")[1])
        emp = emp_by_id(st, eid)
        if not emp:
            send(chat_id, "Сотрудник не найден.", menu_kb())
            return
        op = open_shift(st, eid)
        if op:
            when = datetime.fromisoformat(op["start_ts"]).astimezone(TZ).strftime("%d.%m %H:%M")
            send(chat_id, f"У {esc(emp['name'])} уже есть открытая смена с {when}. Сначала закрой её.", menu_kb())
            return
        set_step(st, chat_id, "shift_start", {"emp_id": eid})
        send(chat_id, f"Время начала смены для <b>{esc(emp['name'])}</b>.\nНапиши время в формате <code>ЧЧ:ММ</code> или нажми «Начать сейчас».", kb([("🕐 Начать сейчас", "shnow"), ("⬅️ Назад", "menu")]))
        return

    if data == "shnow":
        step = get_step(st, chat_id)
        if not step or step["step"] != "shift_start":
            send(chat_id, "Начни смену заново.", menu_kb())
            return
        emp = emp_by_id(st, step["data"]["emp_id"])
        st["shifts"].append({"id": next_id(st, "shift"), "employee_id": emp["id"], "start_ts": now().isoformat(timespec="seconds"), "end_ts": None, "hours": None, "note": ""})
        clear_step(st, chat_id)
        send(chat_id, f"✅ Смена начата: <b>{esc(emp['name'])}</b> в {now().strftime('%H:%M')}.", menu_kb())
        return

    if data == "shift:end":
        rows = [s for s in st["shifts"] if not s.get("end_ts")]
        if not rows:
            send(chat_id, "Открытых смен нет.", menu_kb())
            return
        names = {e["id"]: e["name"] for e in st["employees"]}
        send(chat_id, "🛑 <b>Закончить смену</b>\nУ кого закрываем?", kb([(names.get(s["employee_id"], "?"), f"end:{s['id']}") for s in rows] + [("⬅️ Назад", "menu")]))
        return

    if data.startswith("end:"):
        sid = int(data.split(":")[1])
        shift = next((s for s in st["shifts"] if s["id"] == sid), None)
        if not shift:
            send(chat_id, "Смена не найдена.", menu_kb())
            return
        emp = emp_by_id(st, shift["employee_id"])
        set_step(st, chat_id, "shift_end", {"shift_id": sid})
        start_dt = datetime.fromisoformat(shift["start_ts"]).astimezone(TZ)
        send(chat_id, f"Смена <b>{esc(emp['name'] if emp else '?')}</b> начата в {start_dt.strftime('%d.%m %H:%M')}.\nНапиши время окончания <code>ЧЧ:ММ</code> или нажми «Закрыть сейчас».", kb([("🕐 Закрыть сейчас", "endnow"), ("⬅️ Назад", "menu")]))
        return

    if data == "endnow":
        step = get_step(st, chat_id)
        if not step or step["step"] != "shift_end":
            send(chat_id, "Закрой смену заново.", menu_kb())
            return
        shift = next((s for s in st["shifts"] if s["id"] == step["data"]["shift_id"]), None)
        if not shift:
            clear_step(st, chat_id)
            send(chat_id, "Смена не найдена.", menu_kb())
            return
        end = now()
        start_dt = datetime.fromisoformat(shift["start_ts"])
        hours = round((end - start_dt).total_seconds() / 3600, 2)
        shift["end_ts"] = end.isoformat(timespec="seconds")
        shift["hours"] = hours
        clear_step(st, chat_id)
        send(chat_id, f"✅ Смена закрыта. Отработано: <b>{hours} ч</b>.", menu_kb())
        return

    if data == "adv:add":
        emps = employees(st)
        if not emps:
            send(chat_id, "Сначала добавь сотрудника.", menu_kb())
            return
        set_step(st, chat_id, "adv_emp")
        send(chat_id, "💰 <b>Аванс</b>\nКому выдаём?", kb([(e["name"], f"adv:{e['id']}") for e in emps] + [("⬅️ Назад", "menu")]))
        return

    if data.startswith("adv:"):
        eid = int(data.split(":")[1])
        emp = emp_by_id(st, eid)
        if not emp:
            send(chat_id, "Сотрудник не найден.", menu_kb())
            return
        set_step(st, chat_id, "adv_amount", {"emp_id": eid})
        send(chat_id, f"Сумма аванса для <b>{esc(emp['name'])}</b> в рублях:", back_kb())
        return

    if data == "rep:month":
        emps = employees(st, active_only=False)
        if not emps:
            send(chat_id, "Пока нет сотрудников.", menu_kb())
            return
        send(chat_id, "📊 <b>Отчёт за месяц</b>\nКого показываем?", kb([("👥 Все сотрудники", "rep:all")] + [(e["name"], f"repe:{e['id']}") for e in emps] + [("⬅️ Назад", "menu")]))
        return

    if data.startswith("repe:"):
        eid = int(data.split(":")[1])
        emp = emp_by_id(st, eid)
        if not emp:
            send(chat_id, "Сотрудник не найден.", menu_kb())
            return
        send(chat_id, report_emp(st, emp), back_kb())
        return

    if data == "rep:all":
        send(chat_id, report_all(st), back_kb())
        return

    if data == "rep:shifts":
        send(chat_id, shifts_list(st), back_kb())
        return

    send(chat_id, greeting(), menu_kb())


def process_update(st, upd):
    if "callback_query" in upd:
        handle_callback(st, upd["callback_query"])
        return
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "private":
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start"):
        clear_step(st, chat_id)
        send(chat_id, greeting(), menu_kb())
        return
    if text.startswith("/help"):
        send(chat_id, "• Смена: <b>Начать</b> → сотрудник → время.\n• Аванс: <b>Выдать аванс</b> → сотрудник → сумма.\n• Отчёт: <b>Отчёт за месяц</b>.", menu_kb())
        return
    handle_text(st, chat_id, text)


def main():
    st = load_state()
    deadline = time.time() + RUN_SECONDS
    while time.time() < deadline:
        try:
            r = client.get(f"{API}/getUpdates", params={"offset": st["offset"], "timeout": 25, "allowed_updates": ["message", "callback_query"]})
            data = r.json()
        except Exception:
            time.sleep(5)
            continue
        if not data.get("ok"):
            time.sleep(5)
            continue
        for upd in data.get("result", []):
            st["offset"] = upd["update_id"] + 1
            try:
                process_update(st, upd)
            except Exception as e:
                print("update error:", e, flush=True)
            save_state(st)
        if not data.get("result"):
            time.sleep(1)
    save_state(st)


if __name__ == "__main__":
    main()
