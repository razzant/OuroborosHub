#!/usr/bin/env python3
"""Офлайн-репетиция черновика sberpay-agent-guide.

Проходит шаги черновика по вымышленным данным из examples/offline-rehearsal.json:
готовность клиента и магазина, счёт этого магазина, «да» владельца на этот счёт,
одна передача платежа, отдельные итоги банка и магазина. Решение каждого случая
сверяется с его полем expect.

Скрипт ничего не отправляет и ничего не запускает: ответы клиента SberPay в файле
записаны заранее, протокол клиента и банка здесь не изображается.

Запуск из папки навыка:  python3 -I scripts/rehearse.py [файл.json]
Код выхода: 0 — все случаи совпали, 1 — есть расхождение, 2 — файл не читается.
"""
import copy
import json
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "examples" / "offline-rehearsal.json"
ORDER = ("id", "store", "items", "amount", "currency")
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


def rub(kopecks):
    whole, part = divmod(kopecks, 100)
    return "{:,}".format(whole).replace(",", " ") + ",{:02d} ₽".format(part)


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
    """Шаг 2: счёт этого магазина, только рубли, его позиции и сумма в копейках."""
    problems = []
    if invoice.get("store") != request["store"]:
        problems.append("счёт выставил {}, а покупка в {}".format(invoice.get("store"), request["store"]))
    if invoice.get("currency") != "RUB" or request.get("currency") != "RUB":
        problems.append("счёт в {}, просьба в {}: черновик передаёт оплату только в рублях (RUB)".format(
            invoice.get("currency"), request.get("currency")))
        return problems  # суммы в другой валюте не сверяются и рублями не печатаются
    amount = invoice.get("amount")
    asked = sum(item["price"] * item["qty"] for item in request["items"])
    if type(amount) is not int or amount <= 0:
        problems.append("сумма счёта не целое число копеек")
    elif amount != asked:
        problems.append("в счёте {}, а просили на {}".format(rub(amount), rub(asked)))
    elif invoice.get("items") != request["items"]:
        problems.append("состав счёта не тот, что просили")
    return problems


def rehearse(purchase):
    """Шаги 1–5 для одного заказа: (решение, фраза покупателю, путь, число передач)."""
    request, invoice = purchase["request"], purchase.get("invoice") or {}
    path = []

    # 1. Клиент и магазин готовы.
    if (purchase.get("auth_status") or {}).get("canPay") is not True:
        path.append("стоп: готовность не подтверждена")
        return ("stop_not_ready", "Клиент SberPay пока не подтвердил, что можно платить. "
                "Проверь привязку карты в своём клиенте.", path, 0)
    path.append("клиент готов")
    check = purchase.get("merchants_check") or {}
    merchant = check.get("merchant")
    listed = isinstance(merchant, dict)
    paused = listed and merchant.get("status") == "PAUSED"
    # Без merchant домена в реестре нет, даже при supported: true; противоречивый ответ — стоп.
    if check.get("supported") is not True or not listed or paused:
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
    consent = purchase.get("consent") or {}
    if consent.get("approved") is not True or any(consent.get(field) != invoice.get(field) for field in ORDER):
        path.append("стоп: нет «да» на этот счёт")
        return ("ask_consent", "Счёт {} от {}: {}, {}. Оплатить этот счёт?".format(
            invoice["id"], invoice["store"], items_text(invoice["items"]), rub(invoice["amount"])), path, 0)
    path.append("«да» на этот счёт")

    # 4. Одна передача платежа клиенту: других мест, где она считается, нет.
    handoffs = 1
    path.append("передача платежа клиенту: 1")

    # 5. Банк и заказ — отдельно. Всё, кроме чистых SUCCESS и DECLINED, — неизвестный исход.
    result = purchase.get("bank_result") or {}
    response = result.get("response") or {}
    if result.get("exit_code") == 0 and response.get("status") == "SUCCESS":
        bank = "success"
        path.append("банк: SUCCESS")
    elif result.get("exit_code") == 1 and response.get("status") == "DECLINED":
        bank = "declined"
        path.append("банк: DECLINED {}".format(response.get("reasonCode")))
    else:
        bank = "unknown"
        path.append("банк: исход неизвестен")

    store_paid = charge_seen = False
    for event in purchase.get("after") or []:
        kind = event.get("kind")
        if kind == "retry_request":
            path.append("просьба повторить: второй передачи нет")
        elif kind == "store_order":
            paid = (event.get("invoice") == invoice["id"] and event.get("amount") == invoice["amount"]
                    and event.get("paid") is True)
            store_paid = store_paid or paid
            path.append("магазин: счёт оплачен" if paid else "магазин: оплата этого счёта не отмечена")
        elif kind == "bank_app":
            # Магазин и сумма сами по себе не привязывают строку выписки к счёту: нужен его номер.
            seen = (event.get("invoice") == invoice["id"] and event.get("store") == invoice["store"]
                    and event.get("amount") == invoice["amount"] and event.get("charge_seen") is True)
            charge_seen = charge_seen or seen
            path.append("в выписке списание с номером этого счёта" if seen
                        else "в выписке нет списания с номером этого счёта")

    if bank == "declined":
        code = response.get("reasonCode")
        if code == "ALREADY_PAID":
            return ("already_paid", "Этот счёт уже оплачен раньше, второй раз не платим. "
                    "Заказ проверь у магазина.", path, handoffs)
        if store_paid or charge_seen:
            return ("conflict", "Банк ответил отказом, а магазин или выписка показывают оплату этого счёта: "
                    "нужна сверка, новой оплаты не будет.", path, handoffs)
        reason, action = DECLINES.get(code, ((response.get("userMessage") or "банк отказал").rstrip("."),
                                             "Подробности — в банковском приложении."))
        return "declined", "Оплата не прошла: {}. {}".format(reason, action), path, handoffs
    if store_paid:
        return "paid", "Заказ оплачен.", path, handoffs
    if bank == "success" or charge_seen:
        return "bank_ok_store_pending", "Оплата прошла, магазин ещё не подтвердил заказ.", path, handoffs
    return ("unconfirmed", "Пока не удалось подтвердить оплату. Посмотри списание в банковском приложении "
            "и заказ у магазина; второй раз не платим.", path, handoffs)


def main(argv):
    source = Path(argv[1]) if len(argv) > 1 else DEFAULT
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        base, cases = data["base"], data["cases"]
        if not isinstance(base, dict) or not isinstance(cases, list) or not cases:
            raise ValueError("нужны объект base и непустой список cases")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Не удалось прочитать {}: {}".format(source, error), file=sys.stderr)
        return 2
    print("Офлайн-репетиция sberpay-agent-guide: данные вымышлены, реального платежа нет.")
    failed = most = 0
    for case in cases:
        expect = case.get("expect") or {}
        try:
            decision, say, path, handoffs = rehearse(merged(base, case.get("change") or {}))
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            decision, say, path, handoffs = "error", "случай не разбирается: {!r}".format(error), [], None
        ok = decision == expect.get("decision") and handoffs == expect.get("handoffs")
        failed += not ok
        most = max(most, handoffs or 0)
        print("\n{} {} — {}".format("ok " if ok else "НЕТ", case.get("id"), case.get("title")))
        print("    " + " → ".join(path))
        print("    передач платежа: {}; покупателю: «{}»".format(handoffs, say))
        if not ok:
            print("    ожидалось: {}, передач {}".format(expect.get("decision"), expect.get("handoffs")))
    print("\nСовпало {} из {}; передач платежа по одному счёту не больше {}.".format(
        len(cases) - failed, len(cases), most))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
