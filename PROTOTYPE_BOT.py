"""
Kwork-бот — отклики на заявки биржи (рабочий прототип).

Поток: REST читает заявки -> фильтр/дедуп -> отправка отклика веб-формой.
Отправка: Вариант A (Playwright, рекомендуется) или Вариант B (HTTP-replay).

Селекторы и структура payload сняты с живой формы new_offer (см. KWORK_API_ANALYSIS.md §8):
  description: div.trumbowyg-editor.js-stopwords-check (Trumbowyg, contenteditable)
  price:       input#offer-custom-price (type=tel)
  duration:    .v-select.duration-select (vue-select, выбор по числу дней)
  submit:      button.kw-button--green  (текст «Предложить»)

ВНИМАНИЕ: автоответы могут нарушать ToS Kwork (риск блокировки). Каждый отклик
списывает 1 коннект. Соблюдайте задержки и лимиты. Перед боевым прогоном
протестируйте на одной заявке (DRY_RUN=True не отправляет).

Запуск:
  pip install requests playwright && playwright install chromium
  1) однократно: python PROTOTYPE_BOT.py login     # ручной вход, сохраняет cookie
  2) затем:      python PROTOTYPE_BOT.py run        # цикл откликов (Playwright)
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests  # pip install requests

# ───────────────────────── config ─────────────────────────

API_BASE = "https://api.kwork.ru"
WEB_BASE = "https://kwork.ru"
STATE_FILE = Path("state.json")
STORAGE_STATE = "storage_state.json"   # cookie сессии kwork.ru (создаётся командой login)

# Допустимые сроки (дни) для custom-оффера — из durations.basic живой формы:
BASIC_DURATIONS = [1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30, 60]


@dataclass
class Config:
    login: str = ""           # email/телефон (нужен только для REST-чтения)
    password: str = ""
    # фильтры заявок (REST /projects)
    price_from: int = 1200
    price_to: int = 18000
    query: str = ""           # ключевые слова, "" = всё
    categories: str = ""      # id рубрик через запятую
    # параметры отклика
    offer_price: int = 6000       # цена, которую платит покупатель (1200..18000)
    offer_duration_days: int = 3  # ЧИСЛО ДНЕЙ (должно быть из BASIC_DURATIONS)
    offer_name: str = "Готов выполнить ваш проект"
    # тайминги/лимиты (анти-бан)
    min_delay_s: int = 40
    max_delay_s: int = 120
    max_offers_per_run: int = 5
    dry_run: bool = True          # True = заполнить, но НЕ нажимать «Предложить»


# ───────────────────────── device id (uad) ─────────────────────────

UAD_FILE = Path("uad.txt")


def get_uad() -> str:
    """uad как в мобильном API: стабильный device id. Генерируем один раз и кэшируем."""
    if UAD_FILE.exists():
        return UAD_FILE.read_text().strip()
    raw = f"{random.random()}-{time.time()}-kwork-bot-device"
    uad = hashlib.sha1(raw.encode()).hexdigest()
    UAD_FILE.write_text(uad)
    return uad


# ───────────────────────── REST: чтение заявок ─────────────────────────

class KworkApi:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.token: str | None = None
        self.uad = get_uad()

    def sign_in(self) -> None:
        r = self.s.post(f"{API_BASE}/signIn", data={
            "login": self.cfg.login,
            "password": self.cfg.password,
            "uad": self.uad,
            "device": "android",
        })
        r.raise_for_status()
        data = r.json()
        self.token = (data.get("response") or {}).get("token") or data.get("token")
        if not self.token:
            raise RuntimeError(f"signIn без токена: {data}")

    def get_projects(self, page: int = 1) -> list[dict]:
        r = self.s.post(f"{API_BASE}/projects", data={
            "token": self.token,
            "uad": self.uad,
            "categories": self.cfg.categories,
            "price_from": self.cfg.price_from,
            "price_to": self.cfg.price_to,
            "query": self.cfg.query,
            "page": page,
        }, headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        data = r.json()
        return (data.get("response") or {}).get("projects") or data.get("projects") or []


# ───────────────────────── фильтр + дедуп ─────────────────────────

def load_answered() -> set[int]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()).get("answered", []))
    return set()


def save_answered(answered: set[int]) -> None:
    STATE_FILE.write_text(json.dumps({"answered": sorted(answered)}))


def pick_projects(projects: list[dict], cfg: Config, answered: set[int]) -> list[dict]:
    out = []
    for p in projects:
        pid = int(p.get("id"))
        if pid in answered:
            continue
        out.append(p)        # сюда — своя доп.логика отбора (по тексту/заказчику)
    return out[: cfg.max_offers_per_run]


# ───────────────────────── генерация текста ─────────────────────────

def build_offer_text(project: dict, cfg: Config) -> str:
    title = project.get("name") or project.get("title") or "вашему проекту"
    text = (
        f"Здравствуйте! Внимательно изучил задачу «{title}». "
        f"Готов выполнить качественно и в срок, имею релевантный опыт и портфолио. "
        f"Мой подход: уточняю требования, согласую этапы и сроки, веду прозрачную коммуникацию "
        f"и даю правки до результата, который вас устроит. "
        f"Сроки и стоимость указаны в предложении — детали готов обсудить в чате."
    )
    # ВАЖНО: уникализируйте текст под каждую заявку (анти-бан). Минимум 150 символов.
    return text


def nearest_duration(days: int) -> int:
    return min(BASIC_DURATIONS, key=lambda d: abs(d - days))


# ───────────────────────── Вариант A: Playwright (рекомендуется) ─────────────────────────

def submit_offer_playwright(project_id: int, text: str, price: int,
                            duration_days: int, cfg: Config) -> bool:
    """Открывает форму с сохранённой сессией, заполняет и (если не dry_run) отправляет."""
    from playwright.sync_api import sync_playwright

    duration_days = nearest_duration(duration_days)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # headless=False надёжнее против антибота
        ctx = browser.new_context(storage_state=STORAGE_STATE)
        page = ctx.new_page()
        page.goto(f"{WEB_BASE}/new_offer?project={project_id}", wait_until="networkidle")

        # 1) Описание — Trumbowyg contenteditable
        desc = page.locator("div.trumbowyg-editor.js-stopwords-check").first
        desc.click()
        desc.fill("")                  # очистить плейсхолдер-состояние
        desc.type(text, delay=15)      # печатаем как человек -> срабатывают input-события

        # 2) Стоимость
        page.fill("input#offer-custom-price", str(price))
        page.locator("input#offer-custom-price").press("Tab")

        # 3) Срок — vue-select: открыть и выбрать пункт по числу дней
        page.click(".v-select.duration-select")
        page.wait_for_timeout(400)
        # пункты выпадашки vue-select: .vs__dropdown-option; текст вида "3 дня"/"5 дней"
        option = page.locator(".vs__dropdown-option", has_text=str(duration_days)).first
        option.click()

        page.wait_for_timeout(500)

        if cfg.dry_run:
            print(f"[{project_id}] DRY_RUN: форма заполнена, сабмит НЕ выполнен")
            ctx.close(); browser.close()
            return False

        # 4) Отправка
        page.click("button.kw-button--green")  # «Предложить»
        page.wait_for_timeout(3500)
        ok = page.get_by_text("отправлено").count() > 0
        ctx.close(); browser.close()
        return ok


def login_and_save_state() -> None:
    """Однократный ручной логин (капча/SMS вручную), затем сохранение cookie в STORAGE_STATE."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{WEB_BASE}/login")
        print("Войдите вручную в открытом браузере (логин/пароль/капча/SMS),")
        print("дождитесь загрузки кабинета, затем нажмите Enter здесь...")
        input()
        ctx.storage_state(path=STORAGE_STATE)
        print(f"Сессия сохранена в {STORAGE_STATE}")
        browser.close()


# ───────────────────────── Вариант B: HTTP-replay ─────────────────────────

def extract_page_context(session: requests.Session, project_id: int) -> dict:
    """Снимает csrftoken и wantId со страницы new_offer (нужно для HTTP-сабмита).
    session должна содержать cookie авторизации kwork.ru."""
    import re
    r = session.get(f"{WEB_BASE}/new_offer?project={project_id}")
    r.raise_for_status()
    html = r.text
    csrf = None
    m = re.search(r'csrftoken["\']?\s*[:=]\s*["\']([a-f0-9]{16,})["\']', html)
    if m:
        csrf = m.group(1)
    return {"csrftoken": csrf, "wantId": project_id}


def submit_offer_http(session: requests.Session, project_id: int, text: str,
                      price: int, duration_days: int, csrftoken: str,
                      offer_name: str) -> dict:
    """POST /api/offer/createoffer. session должна содержать cookie авторизации kwork.ru.
    Хрупкий вариант (зависит от csrf/полей) — может ломаться при обновлениях сайта."""
    duration_days = nearest_duration(duration_days)
    r = session.post(f"{WEB_BASE}/api/offer/createoffer", data={
        "wantId": project_id,
        "offerType": "custom",
        "description": text,
        "kwork_duration": duration_days,   # число дней
        "kwork_price": price,              # цена покупателя
        "kwork_name": offer_name,
        "csrftoken": csrftoken,
    }, headers={
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{WEB_BASE}/new_offer?project={project_id}",
    })
    r.raise_for_status()
    return r.json()  # {status, code, response, errors}


# ───────────────────────── main loop ─────────────────────────

def run(cfg: Config, mode: str = "playwright") -> None:
    api = KworkApi(cfg)
    api.sign_in()
    answered = load_answered()

    projects = api.get_projects(page=1)
    targets = pick_projects(projects, cfg, answered)
    print(f"Найдено заявок: {len(projects)}, к отклику: {len(targets)}")

    for p in targets:
        pid = int(p["id"])
        text = build_offer_text(p, cfg)
        if len(text) < 150:
            print(f"[{pid}] текст < 150 символов, пропуск"); continue

        if mode == "playwright":
            ok = submit_offer_playwright(pid, text, cfg.offer_price,
                                         cfg.offer_duration_days, cfg)
        else:
            ctx = extract_page_context(api.s, pid)
            if not ctx["csrftoken"]:
                print(f"[{pid}] нет csrftoken (нет веб-сессии в requests) — пропуск"); continue
            if cfg.dry_run:
                print(f"[{pid}] DRY_RUN HTTP: payload готов, не отправляю"); ok = False
            else:
                res = submit_offer_http(api.s, pid, text, cfg.offer_price,
                                        cfg.offer_duration_days, ctx["csrftoken"], cfg.offer_name)
                ok = res.get("status") != "error"
                print(f"[{pid}] ответ: {res}")

        if ok:
            answered.add(pid); save_answered(answered)
            print(f"[{pid}] отклик отправлен")
        else:
            print(f"[{pid}] не отправлено (dry_run или ошибка)")

        time.sleep(random.randint(cfg.min_delay_s, cfg.max_delay_s))  # анти-бан


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    cfg = Config(
        login="",          # заполнить для REST-чтения
        password="",
        offer_price=6000,
        offer_duration_days=3,
        dry_run=True,      # ПОМЕНЯЙТЕ на False для реальной отправки
    )
    if cmd == "login":
        login_and_save_state()
    elif cmd == "run":
        run(cfg, mode="playwright")
    elif cmd == "run-http":
        run(cfg, mode="http")
    else:
        print(__doc__)
