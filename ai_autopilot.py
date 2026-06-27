"""ИИ-автопилот для Kwork.

Бот сам:
  1) открывает заявку (или берёт текст с уже открытой формы new_offer),
  2) читает её описание/бюджет,
  3) через Groq (llama-3.3-70b) САМ придумывает текст предложения, цену и срок,
  4) заполняет форму new_offer и (если не dry_run) нажимает «Предложить».

Ключ Groq берётся из переменной окружения GROQ_API_KEY.
"""
import os
import re
import sys
import json
import requests
from playwright.sync_api import sync_playwright

CDP = os.environ.get("CDP_URL", "http://localhost:29229")
WEB_BASE = "https://kwork.ru"
BASIC_DURATIONS = [1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30, 60]
# По умолчанию — Groq напрямую; на заблокированном сервере задаём GROQ_BASE
# (например, Cloudflare Worker-прокси) через переменную окружения.
GROQ_BASE = os.environ.get("GROQ_BASE", "https://api.groq.com").rstrip("/")
GROQ_URL = f"{GROQ_BASE}/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Заказы на конструкторы сайтов / CMS — НЕ наш профиль (делаем только самописное).
SKIP_KEYWORDS = [
    "tilda", "тильда", "wix", "викс", "wordpress", "вордпресс", "вордпрес",
    "joomla", "джумла", "bitrix", "битрикс", "конструктор",
    "insales", "shopify", "webflow", "вебфлоу", "taplink", "таплинк",
    "elementor",
]

# Разработка игр и приложений (Android/iOS/ПК) — НЕ наш профиль.
# Матчим по границе слова, чтобы не ловить «тигр», «фигура» и т.п.
SKIP_KEYWORDS_WB = [
    "игра", "игры", "игру", "игрой", "игровой", "игровое", "геймдев", "gamedev",
    "game", "games", "unity", "unreal", "godot", "роблокс", "roblox",
    "майнкрафт", "minecraft",
    "андроид", "android", "ios", "айфон", "iphone", "айос",
    "google play", "app store", "apk", "flutter", "react native",
    "kotlin", "swift",
    "мобильное приложение", "мобильного приложения", "моб приложение",
    "десктопное приложение", "приложение на пк", "приложение для пк",
    "приложение для windows", "приложение под windows", "приложение на windows",
]


def is_skippable(project: dict):
    """Возвращает ключевое слово-причину, если заявка вне профиля, иначе None.

    Профиль: только самописная веб/бэкенд-разработка, боты, парсеры, сервисы.
    НЕ берём: конструкторы/CMS, разработку игр (любые платформы, в т.ч. ПК),
    мобильные (Android/iOS) и десктопные приложения.
    """
    blob = f"{project.get('title','')} {project.get('text','')}".lower()
    for kw in SKIP_KEYWORDS:
        if kw in blob:
            return kw
    for kw in SKIP_KEYWORDS_WB:
        if re.search(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", blob):
            return kw
    return None


def nearest_duration(days: int) -> int:
    return min(BASIC_DURATIONS, key=lambda d: abs(d - days))


# Минимально допустимая цена оффера на форме Kwork.
PRICE_FLOOR = int(os.environ.get("KWORK_PRICE_FLOOR", "500"))
# Скидка от максимально допустимого бюджета (0.10 = −10%).
PRICE_DISCOUNT = float(os.environ.get("KWORK_PRICE_DISCOUNT", "0.10"))


def compute_price(project: dict, price_cap: int) -> int:
    """Цена = максимально допустимый бюджет заказчика − 10% (округлённо).

    Если максимум не распознан — берём имеющийся потолок (price_cap).
    Округляем до 10 руб, не опускаемся ниже PRICE_FLOOR и не выше price_cap.
    """
    base = project.get("budget_max") or price_cap
    price = int(round(base * (1.0 - PRICE_DISCOUNT) / 10.0) * 10)
    price = max(PRICE_FLOOR, min(price, price_cap))
    return price


def read_project(page, project_id: int) -> dict:
    """Читает заголовок/описание/бюджет заявки со страницы проекта."""
    page.goto(f"{WEB_BASE}/projects/{project_id}", wait_until="networkidle")
    page.wait_for_timeout(800)
    data = page.evaluate("""() => {
        const t = document.querySelector('h1');
        const body = document.body.innerText;
        const m = body.match(/Допустимый[^\\d]*(\\d[\\d\\s]*)/);
        const d = body.match(/бюджет[^\\d]*(\\d[\\d\\s]*)/i);
        const des = body.match(/желаем[^\\d]*(\\d[\\d\\s]*)/i);
        const off = body.match(/(\\d+)\\s*предложен/i);
        return {
            title: t ? t.innerText.trim() : '',
            text: body.slice(0, 4000),
            budget_max_raw: m ? m[1] : (d ? d[1] : ''),
            desired_raw: des ? des[1] : '',
            offers_count_raw: off ? off[1] : '',
        };
    }""")
    def _num(s):
        s = re.sub(r"\D", "", s or "")
        return int(s) if s else None
    data["budget_max"] = _num(data.get("budget_max_raw"))
    data["desired_price"] = _num(data.get("desired_raw"))
    data["offers_count"] = _num(data.get("offers_count_raw"))
    return data


def ai_compose(project: dict) -> dict:
    """Groq сам пишет предложение: текст (≥150 симв.), цену и срок."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise SystemExit("GROQ_API_KEY не задан в окружении")

    budget = project.get("budget_max") or 3000
    price_cap = min(budget, 100000)
    desired = project.get("desired_price")
    offers = project.get("offers_count")
    sys_prompt = (
        "Ты — опытный профессиональный фрилансер-разработчик на бирже Kwork. Ты делаешь "
        "ТОЛЬКО самописные проекты (чистый код: Python, JS/TS, PHP, бэкенд, боты, парсеры, "
        "самописные сайты и сервисы). Ты НЕ работаешь с конструкторами и CMS (Tilda, "
        "Wix, WordPress, Joomla, Bitrix, Webflow, InSales, Shopify и т.п.). Также ты "
        "НЕ берёшься за НЕпрофильное: чистый дизайн без кода, копирайт/тексты, SMM, "
        "помощь со стримами/трансляциями, консультации. "
        "Также ты НЕ берёшь РАЗРАБОТКУ ИГР (любые платформы, включая ПК — Unity, Unreal, "
        "Godot, Roblox, Minecraft и т.п.), МОБИЛЬНЫЕ приложения (Android/iOS, APK, "
        "Flutter, React Native, Kotlin, Swift) и ДЕСКТОПНЫЕ приложения под ПК/Windows. "
        "Если заявка про игру или приложение (мобильное/десктопное/ПК) — НЕ берись. "
        "Если заявка просит конструктор/CMS ЛИБО это не задача по самописной разработке, "
        "ЛИБО заявка бессмысленная/мусорная/шуточная/без понятного ТЗ (набор букв, "
        "непонятный текст, нет реальной задачи) — НЕ берись: верни СТРОГО JSON "
        '{"skip": true, "skip_reason": "..."} и больше ничего. '
        "Иначе напиши ПРОФЕССИОНАЛЬНОЕ персональное предложение на грамотном русском языке. "
        "КРИТИЧЕСКИ ВАЖНО — как писать текст (description): "
        "(A) НИКОГДА не обращайся к заказчику по нику/имени и не вставляй его логин в текст; "
        "ник на бирже часто мусорный (например 'gkugkugk') — обращений по имени быть НЕ должно. "
        "Начинай сразу с сути, можно с нейтрального 'Здравствуйте!' без имени. "
        "(B) НЕ повторяй дословно формулировку заявки и не перефразируй её как 'помогу сделать X'. "
        "Заказчик и так знает, что ему нужно. Вместо этого ДУМАЙ как инженер: предложи КОНКРЕТНОЕ "
        "техническое РЕШЕНИЕ — как именно ты это реализуешь, какой подход, какие шаги. "
        "(C) если в заявке есть неоднозначность — задай 1-2 уместных уточняющих вопроса по делу. "
        "Требования к тексту: "
        "(1) деловой, уверенный тон, без воды, эмодзи и восклицаний, без шаблонных фраз; "
        "(2) покажи понимание задачи через предложенное решение, а не пересказ запроса; "
        "(3) опиши КОНКРЕТНЫЙ план/подход и технологический стек под эту задачу "
        "(какие инструменты/язык/библиотеки используешь и почему); "
        "(4) что заказчик получит на выходе (результат, исходники, инструкция, поддержка); "
        "(5) грамотно, без орфографических ошибок, 350-650 символов. "
        "Цену НЕ придумывай — её задаёт система. Верни СТРОГО JSON с полями: "
        "description (текст предложения по правилам выше), "
        "duration_days (целое число дней, реалистичный срок), "
        "name (краткое профессиональное название заказа до 70 символов). "
        "Никакого текста вне JSON."
    )
    user_prompt = (
        f"Заголовок: {project.get('title','')}\n\n"
        f"Описание заявки:\n{project.get('text','')[:2500]}\n\n"
        f"Желаемая заказчиком цена: {desired if desired else 'не указана'} руб.\n"
        f"Максимально допустимый бюджет: {price_cap} руб.\n"
        f"Предложений от конкурентов уже: {offers if offers is not None else 'неизвестно'}"
    )
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    if data.get("skip"):
        return data
    data["duration_days"] = nearest_duration(int(data.get("duration_days", 3)))
    # Цену задаёт система: всегда «максимально допустимый бюджет − 10%».
    data["price"] = compute_price(project, price_cap)
    data["price_reason"] = "максимально допустимый бюджет минус 10%"
    if len(data.get("description", "")) < 150:
        data["description"] += (
            " Готов обсудить детали ТЗ, показать примеры из портфолио и приступить "
            "к работе сразу после согласования. Обеспечу качественный результат в срок."
        )
    return data


def read_connects(page):
    """Читает остаток коннектов из текста текущей страницы.

    На бирже kwork.ru/projects баланс показан как «Осталось N из M»
    (рядом с блоком «Коннекты»). Возвращает int (остаток) или None.
    """
    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:
        return None
    # Основной формат на бирже: «Осталось 13 из 30».
    m = re.search(r"осталось\s*(\d+)\s*из\s*(\d+)", body, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Запасные формы: «осталось 17 коннектов», «17 коннект».
    for pat in (r"осталось\s*(\d+)\s*коннект",
                r"(\d+)\s*коннект"):
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def read_connects_on_exchange(page):
    """Открывает биржу и читает реальный остаток коннектов (N из 30)."""
    try:
        page.goto(f"{WEB_BASE}/projects?c=11", wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(2500)
    except Exception:
        return None
    return read_connects(page)


def _select_duration(page, days: int):
    """Открывает выпадающий список срока и выбирает нужный день.

    Надёжно: читает реально отрисованные пункты, матчит по ведущему числу
    (\"30\" != \"3\"), при отсутствии — берёт ближайший доступный.
    """
    # Иногда первый клик не раскрывает список (форма ещё дорисовывается) —
    # пробуем несколько раз, прокрутив поле в видимую область.
    dd = page.locator(".v-select.duration-select").first
    opened = False
    for attempt in range(3):
        try:
            dd.scroll_into_view_if_needed(timeout=4000)
            dd.click(timeout=4000)
            page.wait_for_selector(".vs__dropdown-option", timeout=4000)
            opened = True
            break
        except Exception:
            page.wait_for_timeout(800)
    if not opened:
        page.wait_for_selector(".vs__dropdown-option", timeout=4000)
    page.wait_for_timeout(300)

    opts = page.locator(".vs__dropdown-option")
    texts = opts.all_inner_texts()
    # Парсим ведущее число каждого пункта: \"30 дней\" -> 30.
    parsed = []
    for i, t in enumerate(texts):
        m = re.match(r"\s*(\d+)", t or "")
        if m:
            parsed.append((i, int(m.group(1))))

    target_idx = None
    for i, n in parsed:
        if n == days:
            target_idx = i
            break
    if target_idx is None and parsed:
        # Ближайший среди реально доступных пунктов.
        i, _ = min(parsed, key=lambda p: abs(p[1] - days))
        target_idx = i

    if target_idx is None:
        # Фолбэк: старое поведение по подстроке.
        page.locator(".vs__dropdown-option", has_text=str(days)).first.click()
    else:
        opts.nth(target_idx).click()
    page.wait_for_timeout(400)


# HTTP-сабмит можно отключить, выставив KWORK_HTTP_SUBMIT=0 (тогда только браузер).
USE_HTTP_SUBMIT = os.environ.get("KWORK_HTTP_SUBMIT", "1") not in ("0", "false", "no")


def http_submit_offer(ctx, page, project_id: int, offer: dict) -> dict:
    """Быстрый путь: отправка отклика напрямую POST /api/offer/createoffer.

    Использует сессионные cookie из браузера + window.csrftoken со страницы.
    Поведение по проекту: пробуем HTTP; при любой ошибке/непонятном ответе
    вызывающий код откатывается на Playwright. Ничего не угадываем вслепую.
    """
    csrftoken = page.evaluate("() => window.csrftoken || null")
    if not csrftoken:
        return {"ok": False, "fallback": True, "detail": "нет window.csrftoken"}

    # Cookie именно домена kwork.ru (без поддоменов api.).
    jar = {}
    for c in ctx.cookies():
        dom = (c.get("domain") or "").lstrip(".")
        if dom.endswith("kwork.ru"):
            jar[c["name"]] = c["value"]
    if "csrf_user_token" not in jar and "i" not in jar:
        return {"ok": False, "fallback": True, "detail": "нет сессионных cookie"}

    ua = page.evaluate("() => navigator.userAgent")
    form = {
        "wantId": str(project_id),
        "offerType": "custom",
        "description": offer["description"],
        "kwork_duration": str(int(offer["duration_days"])),  # число дней, не ID
        "kwork_price": str(offer["price"]),
        "kwork_name": offer.get("name", "")[:70],
        "csrftoken": csrftoken,
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": ua,
        "Referer": f"{WEB_BASE}/new_offer?project={project_id}",
        "Origin": WEB_BASE,
        "Accept": "application/json, text/plain, */*",
    }
    try:
        r = requests.post(
            f"{WEB_BASE}/api/offer/createoffer",
            data=form, cookies=jar, headers=headers, timeout=30,
        )
    except Exception as e:
        return {"ok": False, "fallback": True, "detail": f"http exc: {e}"}

    body = (r.text or "")[:400]
    js = {}
    try:
        js = r.json()
    except Exception:
        pass
    # Kwork обычно отвечает {"success": true, ...} либо {"success": false, "error": ...}.
    if r.status_code == 200 and (js.get("success") is True or '"success":true' in body.replace(" ", "")):
        return {"ok": True, "fallback": False, "detail": "createoffer success", "via": "http"}
    return {"ok": False, "fallback": True,
            "detail": f"http {r.status_code}: {body}"[:300], "resp": js or body}


def fill_and_submit(page, project_id: int, offer: dict, dry_run: bool, ctx=None):
    page.goto(f"{WEB_BASE}/new_offer?project={project_id}", wait_until="networkidle")

    connects = read_connects(page)
    if connects is not None:
        print("CONNECTS before submit:", connects)

    # ── Быстрый путь: прямой HTTP-сабмит (createoffer). ──────────────────────
    # CSRF и cookie уже актуальны (страница открыта в залогиненном браузере).
    # При любой ошибке — молча откатываемся на надёжную браузерную отправку.
    if not dry_run and USE_HTTP_SUBMIT and ctx is not None:
        res = http_submit_offer(ctx, page, project_id, offer)
        if res.get("ok"):
            after = read_connects_on_exchange(page)
            print("HTTP submit OK | connects:", after)
            return {"ok": True, "dry_run": False, "via": "http", "connects": after}
        print("HTTP submit -> fallback на Playwright:", res.get("detail"))

    desc = page.locator("div.trumbowyg-editor.js-stopwords-check").first
    desc.click(); desc.fill(""); desc.type(offer["description"], delay=6)
    page.wait_for_timeout(300)

    page.fill("input#offer-custom-price", str(offer["price"]))
    page.locator("input#offer-custom-price").press("Tab")
    page.wait_for_timeout(300)

    try:
        # На форме два таких поля (десктоп + скрытый мобильный) — берём видимое.
        name_box = page.locator(
            '[contenteditable][placeholder="Введите название заказа"]:visible'
        ).first
        name_box.click(timeout=8000)
        name_box.fill(""); name_box.type(offer["name"], delay=6)
    except Exception as e:
        print("name field skip:", str(e)[:120])
    page.wait_for_timeout(300)

    _select_duration(page, int(offer["duration_days"]))
    page.wait_for_timeout(500)

    if dry_run:
        print("DRY_RUN: форма заполнена ИИ-текстом, сабмит НЕ выполнен")
        return {"ok": False, "dry_run": True, "connects": connects}
    page.click("button.kw-button--green")
    page.wait_for_timeout(4000)
    ok = page.get_by_text("отправлено").count() > 0
    # Остаток коннектов корректно показывается уже ПОСЛЕ отправки (баннер/баланс).
    after = read_connects(page)
    if after is not None:
        connects = after
        print("CONNECTS after submit:", connects)
    print("LIVE: submit clicked. url:", page.url, "| 'отправлено':", ok)
    return {"ok": ok, "dry_run": False, "connects": connects}


def main(project_id: int, dry_run: bool):
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        project = read_project(page, project_id)
        print("PROJECT:", json.dumps({k: project.get(k) for k in ("title", "budget_max", "desired_price", "offers_count")}, ensure_ascii=False))

        kw = is_skippable(project)
        if kw:
            print(f"SKIP: заявка про конструктор/CMS ('{kw}') — не наш профиль, пропускаю.")
            return

        offer = ai_compose(project)
        print("AI OFFER:", json.dumps(offer, ensure_ascii=False))
        if offer.get("skip"):
            print("SKIP (ИИ): ", offer.get("skip_reason", "не наш профиль"))
            return

        fill_and_submit(page, project_id, offer, dry_run, ctx=ctx)


if __name__ == "__main__":
    pid = int(sys.argv[1])
    dry = "--submit" not in sys.argv
    main(pid, dry)
