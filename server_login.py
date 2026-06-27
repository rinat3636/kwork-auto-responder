"""Логин в Kwork на сервере через CDP-подключение к headless Chromium.

Подключается к уже запущенному Chromium (--remote-debugging-port=29229),
открывает форму входа, вводит сохранённые креды и проверяет, что вошли.
Сессия (куки) сохраняется в persistent user-data-dir браузера.
"""
import os
import sys
from playwright.sync_api import sync_playwright

CDP = os.environ.get("CDP_URL", "http://localhost:29229")
LOGIN = os.environ["KWORK_LOGIN"]
PASSWORD = os.environ["KWORK_PASSWORD"]


def logged_in(page) -> bool:
    page.goto("https://kwork.ru/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    # на залогиненной главной есть ссылка на профиль/баланс коннектов
    html = page.content().lower()
    return ("logout" in html) or ("выйти" in html) or ("/inbox" in html and "войти" not in page.content().lower())


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if logged_in(page):
            print("ALREADY LOGGED IN")
            return

        page.goto("https://kwork.ru/login", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        # Открыть модалку входа, если требуется
        try:
            page.get_by_text("Войти", exact=False).first.click(timeout=4000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Поля логина/пароля
        filled = False
        for sel in ['input[name="l_username"]', 'input[name="username"]', 'input[type="email"]', 'input[name="email"]']:
            try:
                page.fill(sel, LOGIN, timeout=3000)
                filled = True
                break
            except Exception:
                continue
        for sel in ['input[name="l_password"]', 'input[name="password"]', 'input[type="password"]']:
            try:
                page.fill(sel, PASSWORD, timeout=3000)
                break
            except Exception:
                continue
        print("fields filled:", filled)
        page.wait_for_timeout(500)

        # Кнопка отправки
        for sel in ['button:has-text("Войти")', 'button[type="submit"]', 'input[type="submit"]']:
            try:
                page.click(sel, timeout=3000)
                break
            except Exception:
                continue
        page.wait_for_timeout(5000)

        if logged_in(page):
            print("LOGIN OK")
        else:
            print("LOGIN FAILED — возможно капча/2FA. URL:", page.url)
            page.screenshot(path="/root/kwork/login_state.png")
            sys.exit(2)


if __name__ == "__main__":
    main()
