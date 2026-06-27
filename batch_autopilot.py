"""Батч-прогон ИИ-автопилота по заявкам Kwork.

Логика (запускается раз в час по cron):
  1) читает РЕАЛЬНЫЙ остаток коннектов с биржи («Осталось N из 30»);
  2) если коннектов 0 — «отдыхает» (выходит) до следующего часа;
  3) иначе берёт все НОВЫЕ (ещё не просмотренные) заявки с биржи и по каждой:
       - жёсткий фильтр конструкторов/CMS (is_skippable) — пропуск без трат,
       - ИИ (Groq) решает: skip (непрофиль) или предложение+цена+срок,
       - если профиль — заполняет форму и отправляет «Предложить» (live);
  4) откликается, пока есть свободные коннекты и есть новые заявки;
  5) как только коннекты кончились или новых заявок нет — выходит до след. часа.

Запуск:
  python batch_autopilot.py            # dry_run (без отправки)
  python batch_autopilot.py --submit   # live: реальная отправка по остатку коннектов
  python batch_autopilot.py --submit 5 # live, но не больше 5 отправок за прогон
"""
import os
import sys
import json
import time

from playwright.sync_api import sync_playwright

import ai_autopilot as A

LIST_URL = "https://kwork.ru/projects?c=11"

# Файл состояния: что уже отвечено/просмотрено (чтобы не дёргать одни и те же заявки).
STATE_FILE = os.environ.get("KWORK_STATE_FILE", "/root/kwork/connects_state.json")
# Сколько новых заявок максимум перебирать за один прогон (потолок, не лимит отправок).
SCAN_CAP = int(os.environ.get("KWORK_SCAN_CAP", "80"))


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("answered", [])   # id заявок, по которым отклик отправлен
    st.setdefault("seen", [])       # id заявок, уже просмотренных (фильтр/ИИ/отклик)
    return st


def save_state(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError as e:
        print("state save error:", e)


def collect_new_ids(page, seen: set, cap: int) -> list:
    """Собирает НОВЫЕ id заявок с биржи (которых нет в seen)."""
    ids = []
    picked = set()
    for pg in range(1, 8):
        url = LIST_URL + (f"&page={pg}" if pg > 1 else "")
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(1200)
        rows = page.evaluate("""() => {
            const out=[];
            document.querySelectorAll('a[href*=\"/projects/\"]').forEach(a=>{
                const m=a.getAttribute('href').match(/\\/projects\\/(\\d+)/);
                if(m) out.push(m[1]);
            });
            return out;
        }""")
        for r in rows:
            pid = int(r)
            if pid in seen or pid in picked:
                continue
            picked.add(pid)
            ids.append(pid)
        if len(ids) >= cap:
            break
    return ids[:cap]


def process_one(page, pid: int, dry_run: bool, ctx=None) -> dict:
    res = {"id": pid, "status": "", "detail": ""}
    try:
        project = A.read_project(page, pid)
        res["title"] = project.get("title", "")
        kw = A.is_skippable(project)
        if kw:
            res["status"] = "skip_filter"
            res["detail"] = kw
            return res
        offer = A.ai_compose(project)
        if offer.get("skip"):
            res["status"] = "skip_ai"
            res["detail"] = offer.get("skip_reason", "не наш профиль")
            return res
        res["price"] = offer.get("price")
        res["duration_days"] = offer.get("duration_days")
        out = A.fill_and_submit(page, pid, offer, dry_run, ctx=ctx)
        if isinstance(out, dict):
            res["connects"] = out.get("connects")
            ok = out.get("ok")
        else:
            ok = out
        res["status"] = "filled_dry" if dry_run else ("submitted" if ok else "submit_unknown")
    except Exception as e:
        res["status"] = "error"
        res["detail"] = str(e)[:200]
    return res


def main(dry_run: bool, max_send: int):
    results = []
    rested_reason = None
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(A.CDP)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        state = load_state()
        seen = set(state["seen"])
        answered = set(state["answered"])

        # Реальный остаток коннектов с биржи («Осталось N из 30») — главный источник правды.
        remaining = A.read_connects_on_exchange(page)
        if remaining is None:
            print("CONNECTS: не удалось прочитать остаток — на всякий случай отдыхаю.")
            return results
        print(f"CONNECTS: свободно на аккаунте = {remaining} (из 30)")
        if not dry_run and remaining <= 0:
            print("\nREST: свободных коннектов нет — сплю до следующего часа.")
            return results

        # Сколько откликов можем отправить за прогон: по остатку коннектов (+ необязательный потолок).
        budget = remaining if dry_run is False else max_send
        if max_send and max_send < budget:
            budget = max_send

        pool = collect_new_ids(page, seen, SCAN_CAP)
        print(f"NEW POOL ({len(pool)}) [бюджет откликов={budget}]:", pool[:40])
        if not pool:
            print("\nREST: новых заявок нет — сплю до следующего часа.")
            return results

        sent = 0
        for pid in pool:
            if not dry_run and remaining <= 0:
                rested_reason = "коннекты закончились"
                break
            if budget and sent >= budget:
                rested_reason = "достигнут бюджет прогона"
                break
            print(f"\n=== project {pid} | свободно коннектов={remaining} | отправлено={sent} ===")
            r = process_one(page, pid, dry_run, ctx=ctx)
            print("RESULT:", json.dumps(r, ensure_ascii=False))
            results.append(r)

            # Помечаем просмотренной всё, кроме ошибок (ошибки перепроверим в след. час).
            if r.get("status") != "error":
                seen.add(pid)

            if not dry_run and r.get("status") == "submitted":
                sent += 1
                answered.add(pid)
                # Синхронизируем остаток с реальным показом после отправки, если он есть.
                rc = r.get("connects")
                remaining = rc if isinstance(rc, int) else remaining - 1
                state["seen"] = sorted(seen)
                state["answered"] = sorted(answered)
                save_state(state)
                print(f"CONNECTS: отправлено за прогон={sent} | свободно осталось={remaining}")
            time.sleep(6)  # анти-бан пауза

        state["seen"] = sorted(seen)
        state["answered"] = sorted(answered)
        save_state(state)

    print("\n===== SUMMARY =====")
    for r in results:
        line = f"{r['id']:>9} | {r['status']:<14} | {r.get('title','')[:40]} | {r.get('detail','')}"
        if r.get("price") is not None:
            line += f" | {r.get('price')}₽/{r.get('duration_days')}д"
        print(line)
    n_sub = sum(1 for r in results if r.get("status") == "submitted")
    n_fdry = sum(1 for r in results if r.get("status") == "filled_dry")
    n_sf = sum(1 for r in results if r.get("status") == "skip_filter")
    n_sa = sum(1 for r in results if r.get("status") == "skip_ai")
    n_err = sum(1 for r in results if r.get("status") == "error")
    print(f"\nИТОГО: отправлено={n_sub}, dry={n_fdry}, skip_filter={n_sf}, "
          f"skip_ai={n_sa}, ошибок={n_err}")
    if not dry_run:
        tail = f"КОННЕКТЫ: отправлено за прогон={n_sub} | свободно осталось≈{remaining}"
        if rested_reason:
            tail += f"  | ОТДЫХАЮ ({rested_reason})"
        print(tail)
    return results


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    dry = "--submit" not in args
    nums = [int(a) for a in args if a.isdigit()]
    # В live без числа — потолок не задан (ограничивает только остаток коннектов).
    n = nums[0] if nums else (10 if dry else 0)
    main(dry, n)
