#!/usr/bin/env python3
"""Офлайн-репетиция черновика sberpay-agent-guide.

Проходит шаги черновика по вымышленным данным из examples/offline-rehearsal.json:
готовность клиента и магазина, счёт этого магазина, «да» владельца на этот счёт,
одна передача платежа, отдельные итоги банка и магазина. Решение каждого случая
сверяется с его полем expect.

Скрипт ничего не отправляет и ничего не запускает: ответы клиента SberPay в файле
записаны заранее, протокол клиента и банка здесь не изображается. Число передач
платежа — счёт самой имитации: 0 — остановились до шага 4, 1 — передали один раз и
дальше только разбираем итоги. Больше одной скрипт не делает по построению, поэтому
число не проверяет ни клиент, ни банк: как они ответят на повтор, здесь неизвестно.

Данные читаются строго: true/false числом не считаются, 129000.0 — не целое число
копеек, нечитаемый ответ банка — неизвестный исход.

Запуск:  python3 -I -X utf8 scripts/rehearse.py [файл.json]
Без файла берётся комплектный examples/offline-rehearsal.json рядом со скриптом;
запускать можно из любой папки. Код выхода: 0 — все случаи совпали, 1 — есть
расхождение, 2 — файл не читается или у него неверная форма.
"""
import copy
import json
import sys
from collections import Counter
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "examples" / "offline-rehearsal.json"
LIMIT = "Лимит меняется в кабинете SberPay."
REBIND = "Карту заново привязываешь ты в своём клиенте."
RECHECK = "Сверим счёт ещё раз; сумму и магазин я не подгоняю."
DECLINES = {
    "LIMIT_PER_PURCHASE": ("превышен лимит на одну покупку", LIMIT),
    "LIMIT_PER_DAY": ("исчерпан дневной лимит", LIMIT),
    "ACQUIRING_DECLINED": ("банк отклонил списание", "Можно заплатить самому обычным способом."),
    "TOKEN_REVOKED": ("доступ к карте отозван или истёк", REBIND),
    "TOKEN_EXPIRED": ("доступ к карте отозван или истёк", REBIND),
    "INVOICE_MISMATCH": ("банк не сопоставил счёт", RECHECK),
    "AMOUNT_MISMATCH": ("банк не сопоставил сумму", RECHECK),
}
UNRESOLVED = ("unconfirmed", "bank_ok_store_pending", "conflict")  # исход счёта не выяснен


def rub(kopecks):
    whole, part = divmod(abs(kopecks), 100)
    return ("−" if kopecks < 0 else "") + "{:,}".format(whole).replace(",", " ") + ",{:02d} ₽".format(part)


def obj(value):
    """Ответ или запись читается, только если это объект; иначе её как будто нет."""
    return value if isinstance(value, dict) else {}


def is_int(value):
    """Целое число; true/false и 129000.0 сюда не относятся."""
    return type(value) is int


def is_text(value):
    return isinstance(value, str) and bool(value.strip())


def same(left, right):
    """Равенство без подгонки типов: True не равно 1, 129000.0 не равно 129000."""
    return type(left) is type(right) and left == right


def basket(items):
    """Корзина как набор строк (название, количество, цена): порядок не важен, повторы
    считаются. Если список или строка записаны неверно, возвращает None."""
    if not isinstance(items, list) or not items:
        return None
    rows = Counter()
    for item in items:
        item = obj(item)
        name, qty, price = item.get("name"), item.get("qty"), item.get("price")
        if not (is_text(name) and is_int(qty) and qty > 0 and is_int(price)):
            return None
        rows[(name, qty, price)] += 1
    return rows


def same_order(left, right):
    """«Да» относится к этому счёту: номер, магазин, сумма, валюта и состав те же."""
    mine = basket(left.get("items"))
    return (mine is not None and mine == basket(right.get("items"))
            and all(same(left.get(field), right.get(field)) for field in ("id", "store", "amount", "currency")))


def merged(base, change):
    """Случай меняет в base только своё: объекты сливаются, списки и значения заменяются."""
    result = copy.deepcopy(base)
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def items_text(items):
    return ", ".join("{} × {}".format(item.get("name"), item.get("qty")) for item in items or [])


def invoice_problems(request, invoice):
    """Шаг 2: у счёта есть номер, его выставил этот магазин, рубли, состав как просили,
    сумма — целые копейки и равна сумме позиций."""
    wanted = basket(request.get("items"))
    if not is_text(request.get("store")) or wanted is None:
        return ["просьба записана неверно (нужны магазин и позиции с целыми количеством и ценой)"]
    problems = []
    if not is_text(invoice.get("id")):
        problems.append("у счёта нет номера")
    if invoice.get("store") != request["store"]:
        problems.append("счёт выставил {}, а покупка в {}".format(invoice.get("store"), request["store"]))
    if invoice.get("currency") != "RUB" or request.get("currency") != "RUB":
        problems.append("счёт в {}, просьба в {}: черновик передаёт оплату только в рублях (RUB)".format(
            invoice.get("currency") or "неизвестной валюте", request.get("currency") or "неизвестной валюте"))
        return problems  # суммы в другой валюте не сверяются и рублями не печатаются
    amount, got = invoice.get("amount"), basket(invoice.get("items"))
    asked = sum(qty * price * count for (_, qty, price), count in wanted.items())
    if not is_int(amount) or amount <= 0:
        problems.append("сумма счёта не целое число копеек")
    elif amount != asked:
        problems.append("в счёте {}, а просили на {}".format(rub(amount), rub(asked)))
    elif got is None:
        problems.append("позиции счёта записаны неверно")
    elif got != wanted:
        problems.append("состав счёта не тот, что просили")
    return problems


def rehearse(purchase):
    """Шаги 1–5 для одного заказа: (решение, фраза покупателю, путь, число передач)."""
    request, invoice = obj(purchase.get("request")), obj(purchase.get("invoice"))
    path = []

    # 1. Клиент и магазин готовы.
    if obj(purchase.get("auth_status")).get("canPay") is not True:
        path.append("стоп: готовность не подтверждена")
        return ("stop_not_ready", "Клиент SberPay пока не подтвердил, что можно платить. "
                "Проверь привязку карты в своём клиенте.", path, 0)
    path.append("клиент готов")
    check = obj(purchase.get("merchants_check"))
    merchant = check.get("merchant")
    listed = isinstance(merchant, dict)
    paused = listed and merchant.get("status") == "PAUSED"
    # supported: true не хватает: без записи merchant домена в реестре нет, а PAUSED — приём остановлен.
    if paused or not listed or check.get("supported") is not True:
        path.append("стоп: магазин на паузе" if paused else
                    "стоп: магазина нет в реестре" if not listed else "стоп: реестр не подтвердил магазин")
        return ("stop_store", "Магазин сейчас не принимает оплату агентом." if paused
                else "Реестр SberPay не подтвердил этот магазин; черновик передаёт оплату только магазинам "
                "из реестра.", path, 0)
    path.append("магазин в реестре")

    # 2. Счёт этого магазина.
    problems = invoice_problems(request, invoice)
    if problems:
        path.append("стоп: " + "; ".join(problems))
        return ("stop_mismatch", "Счёт не подходит для оплаты: {}. Оплату не передаю, жду твоего решения."
                .format("; ".join(problems)), path, 0)
    path.append("счёт {} сходится: {}".format(invoice["id"], rub(invoice["amount"])))

    # 3. Явное «да» владельца (approved: true) именно на этот счёт.
    consent = obj(purchase.get("consent"))
    if consent.get("approved") is not True or not same_order(consent, invoice):
        path.append("стоп: нет «да» на этот счёт")
        return ("ask_consent", "Счёт {} от {}: {}, {}. Оплатить этот счёт?".format(
            invoice["id"], invoice["store"], items_text(invoice["items"]), rub(invoice["amount"])), path, 0)
    path.append("«да» на этот счёт")

    # 4. Одна передача платежа клиенту. Это счёт самой имитации: других мест, где он растёт, нет.
    handoffs = 1
    path.append("передача платежа клиенту: 1")

    # 5. Банк и заказ — отдельно. Чистые итоги — только SUCCESS с кодом 0 и DECLINED с кодом 1; всё
    # остальное, в том числе нечитаемые bank_result или response, — неизвестный исход. Передача уже
    # учтена, факт магазина и выписки разбирается следом сам по себе.
    result = obj(purchase.get("bank_result"))
    response = obj(result.get("response"))
    if same(result.get("exit_code"), 0) and response.get("status") == "SUCCESS":
        bank = "success"
        path.append("банк: SUCCESS")
    elif same(result.get("exit_code"), 1) and response.get("status") == "DECLINED":
        bank = "declined"
        path.append("банк: DECLINED {}".format(response.get("reasonCode")))
    else:
        bank = "unknown"
        path.append("банк: исход неизвестен")

    store_paid = charge_seen = False
    new_invoices = []
    events = purchase.get("after")
    for event in events if isinstance(events, list) else []:
        event = obj(event)
        kind = event.get("kind")
        if kind == "retry_request":
            path.append("просьба повторить: второй передачи нет")
        elif kind == "new_invoice":
            new_invoices.append(str(event.get("invoice") or "без номера"))
        elif kind == "store_order":
            paid = (same(event.get("invoice"), invoice["id"]) and same(event.get("amount"), invoice["amount"])
                    and event.get("paid") is True)
            store_paid = store_paid or paid
            path.append("магазин: счёт оплачен" if paid else "магазин: оплата этого счёта не отмечена")
        elif kind == "bank_app":
            # Магазин и сумма сами по себе не привязывают строку выписки к счёту: нужен его номер.
            seen = (same(event.get("invoice"), invoice["id"]) and same(event.get("store"), invoice["store"])
                    and same(event.get("amount"), invoice["amount"]) and event.get("charge_seen") is True)
            charge_seen = charge_seen or seen
            path.append("в выписке списание с номером этого счёта" if seen
                        else "в выписке нет списания с номером этого счёта")
        else:
            path.append("запись о событии не распознана и пропущена")

    if bank == "declined":
        code = response.get("reasonCode") if isinstance(response.get("reasonCode"), str) else None
        message = response.get("userMessage")
        if code == "ALREADY_PAID":
            decision, say = "already_paid", ("Этот счёт уже оплачен раньше, второй раз не платим. "
                                             "Заказ проверь у магазина.")
        elif store_paid or charge_seen:
            decision, say = "conflict", ("Банк ответил отказом, а магазин или выписка показывают оплату этого "
                                         "счёта: нужна сверка, новой оплаты не будет.")
        else:
            reason, action = DECLINES.get(code, ((message if is_text(message) else "банк отказал").rstrip("."),
                                                 "Подробности — в банковском приложении."))
            decision, say = "declined", "Оплата не прошла: {}. {}".format(reason, action)
    elif store_paid:
        decision, say = "paid", "Заказ оплачен."
    elif bank == "success" or charge_seen:
        decision, say = "bank_ok_store_pending", "Оплата прошла, магазин ещё не подтвердил заказ."
    else:
        decision, say = "unconfirmed", ("Пока не удалось подтвердить оплату. Посмотри списание в банковском "
                                        "приложении и заказ у магазина; второй раз не платим.")

    # Новый счёт магазина сам по себе не повод для передачи; пока исход прежнего счёта не выяснен, тем более.
    if new_invoices:
        later = ", ".join(new_invoices)
        if decision in UNRESOLVED:
            path.append("новый счёт {}: исход счёта {} не выяснен, второй передачи нет".format(later, invoice["id"]))
            say += " Новый счёт {} не оплачиваю, пока не выяснен исход счёта {}.".format(later, invoice["id"])
        else:
            path.append("новый счёт {}: это уже другая покупка, ей нужны свой показ и своё «да»".format(later))
    return decision, say, path, handoffs


def load(source):
    """Читает файл репетиции. Любая неверная оболочка — ValueError с понятной причиной, не трассировка."""
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))  # sig: файл из Блокнота с BOM тоже читается
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError("файл не открывается или это не JSON ({})".format(error))
    if not isinstance(data, dict) or not isinstance(data.get("base"), dict):
        raise ValueError("на верхнем уровне нужен объект с полем base (объект: заказ из примера)")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("поле cases должно быть непустым списком случаев")
    for number, case in enumerate(cases, 1):
        expect = case.get("expect") if isinstance(case, dict) else None
        change_ok = isinstance(case, dict) and (case.get("change") is None or isinstance(case["change"], dict))
        expect_ok = isinstance(expect, dict) and is_text(expect.get("decision")) and is_int(expect.get("handoffs"))
        says = expect.get("says") if expect_ok else None
        if not (change_ok and expect_ok and (says is None or isinstance(says, list) and all(map(is_text, says)))):
            raise ValueError("случай №{}: нужен объект с полем expect (decision — текст, handoffs — целое число, "
                             "если есть, says — список фраз) и, если есть, change — объектом".format(number))
    return data["base"], cases


def main(argv):
    for stream in (sys.stdout, sys.stderr):  # терминал не в UTF-8: вместо исключения — знак подстановки
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(argv) > 2:
        print("Использование: rehearse.py [файл.json]", file=sys.stderr)
        return 2
    source = Path(argv[1]) if len(argv) > 1 else DEFAULT
    try:
        base, cases = load(source)
    except ValueError as error:
        print("Не удалось прочитать {}: {}".format(source, error), file=sys.stderr)
        return 2
    print("Офлайн-репетиция sberpay-agent-guide: данные вымышлены, реального платежа нет.")
    failed = 0
    for case in cases:
        expect = case["expect"]
        try:
            decision, say, path, handoffs = rehearse(merged(base, case.get("change") or {}))
        except Exception as error:  # непредвиденная форма данных: случай не засчитывается, трассировки нет
            decision, say, path, handoffs = "error", "случай не разбирается: {!r}".format(error), [], None
        says = expect.get("says") or []
        ok = decision == expect["decision"] and same(handoffs, expect["handoffs"]) and all(s in say for s in says)
        failed += not ok
        print("\n{} {} — {}".format("ok " if ok else "НЕТ", case.get("id"), case.get("title")))
        print("    " + " → ".join(path))
        print("    передач платежа в имитации: {}; покупателю: «{}»".format(handoffs, say))
        if not ok:
            print("    ожидалось: {}, передач {}{}".format(expect["decision"], expect["handoffs"],
                                                           ", во фразе: " + " | ".join(says) if says else ""))
    print("\nСовпало {} из {}. Передача платежа в имитации одна или ни одной: так устроен скрипт, "
          "поведение клиента и банка при повторе он не проверяет.".format(len(cases) - failed, len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
