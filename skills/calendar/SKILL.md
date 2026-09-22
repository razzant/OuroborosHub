---
name: calendar
description: Единый календарь Уробороса — свой календарь «Личное» плюс Яндекс Календарь (CalDAV); события, свободные окна, напоминания и виджет День/Неделя с перетаскиванием.
version: 0.2.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [tool, route, widget, net, read_settings]
env_from_settings: [YANDEX_CALDAV_USER, YANDEX_CALDAV_APP_PASSWORD]
timeout_sec: 30
when_to_use: >
  Владелец говорит «что у меня сегодня / завтра / на неделе», «поставь / создай / забей встречу, обед,
  тренировку на 16», «найди окно на час», «перенеси … на 17», «отмени …», «напомни о …», «брифинг на день».
model_experience:
  what_model_sees: >
    7 tools cal_*: cal_status, cal_events, cal_create, cal_move, cal_delete, cal_free, cal_brief.
    Все возвращают JSON {status, ...}; времена ISO 8601 с offset; now_local, today, календари Личное+Яндекс
    и статус подключения Яндекса (с next_step) — в cal_status. calendar='yandex'|'local'|'all' у cal_create.
  token_effect: 7 небольших схем; ответы ограничены 50 событиями и 6 окнами.
ui_tab:
  tab_id: agenda
  title: Календарь
  icon: "📅"
  render:
    kind: declarative
    start: auto
    schema_version: 1
    span: 2
    components:
      - type: poll
        route: agenda
        method: GET
        target: result
        interval_ms: 30000
        max_ticks: 100
        auto_start: true
      - type: callout
        target: result
        path: notice
        tone: info
      - type: group
        layout: cluster
        components:
          - type: metric
            label: Событий сегодня
            target: result
            path: metrics.today_count
          - type: metric
            label: Свободно часов сегодня
            target: result
            path: metrics.free_hours
            precision: 1
          - type: metric
            label: На неделе
            target: result
            path: metrics.week_count
          - type: metric
            label: Пересечений
            target: result
            path: metrics.conflicts
            tone: warning
      - type: tabs
        target: result
        tabs:
          - label: Сегодня
            components:
              - type: calendar
                target: result
                path: items_today
          - label: Неделя
            components:
              - type: kanban
                target: result
                path: cards
                on_move:
                  route: move_card
                  method: POST
                columns:
                  - { id: d0, label: Пн }
                  - { id: d1, label: Вт }
                  - { id: d2, label: Ср }
                  - { id: d3, label: Чт }
                  - { id: d4, label: Пт }
                  - { id: d5, label: Сб }
                  - { id: d6, label: Вс }
          - label: Список недели
            components:
              - type: calendar
                target: result
                path: items_week
          - label: Свободные окна
            components:
              - type: table
                target: result
                path: free_slots
                columns:
                  - { label: Начало, path: start }
                  - { label: Конец, path: end }
                  - { label: Минут, path: minutes, presentation: number }
      - type: form
        route: create
        method: POST
        target: create_result
        submit_label: Создать
        columns: 4
        fields:
          - { name: title, label: Название, type: text, required: true, span: 2 }
          - { name: date, label: Дата, type: text, placeholder: "2026-09-22 (пусто = сегодня)" }
          - { name: time, label: Время, type: text, placeholder: "16:00" }
          - name: calendar
            label: Календарь
            type: select
            default: local
            options:
              - { value: local, label: Личное }
              - { value: yandex, label: Яндекс }
              - { value: all, label: Все }
          - { name: duration_min, label: Длительность, type: number, default: 60, min: 5, max: 1440, step: 5 }
          - { name: remind_min, label: Напомнить за (мин), type: number, default: 0, min: 0, max: 1440, step: 5 }
      - type: kv
        target: create_result
        fields:
          - { label: Результат, path: message }
---

# Календарь Уробороса (v0.2: Личное + Яндекс)

Два источника в одной базе: локальный календарь «Личное» (SQLite в state dir, без сети)
и Яндекс Календарь по CalDAV (`caldav.yandex.ru`, пароль приложения). Google — следующая версия.

## Подключение Яндекса (делает владелец)

1. `id.yandex.ru/security/app-passwords` → создать пароль приложения типа «Календарь»
   (показывается один раз; может заработать через 2–3 часа).
2. Ouroboros → Settings → Secrets: `YANDEX_CALDAV_USER` = полный e-mail (`login@yandex.ru`),
   `YANDEX_CALDAV_APP_PASSWORD` = пароль приложения. Выдать скиллу грант на оба ключа.
3. В чате: «подключи яндекс» → агент вызывает `cal_status(sync=true)` и показывает календари.
   Пока Яндекс не подключён, `cal_status` отдаёт `next_step` — повтори его владельцу.

## Как агенту работать с календарём

1. Любой сценарий начинай с `cal_status`: там `now_local`, `today` и таймзона —
   считай «сегодня», «завтра», «в четверг», «через час» от `now_local`, а не от UTC.
2. Времена передавай в ISO 8601. Без offset — трактуется как местное время владельца.
3. Записи (`cal_create`, `cal_move`, `cal_delete`) делай только по явной команде
   владельца или после его выбора в карточке `escalate`; из автоматических задач — никогда.
   Перед созданием посмотри пересечения в ответе (`overlaps`) и назови их.
4. «Найди окно» → `cal_free` → предложи 2–4 варианта через `escalate` с рекомендуемым →
   после выбора `cal_create`.
5. «Напомни о …» → `remind_before_min` в `cal_create`, а затем поставь одноразовое
   `schedule_followup(run_at = start − N минут, objective = «Напомни владельцу: <название> в <время>»)`.
6. После записи коротко подтверди: что, когда, в каком календаре. Событие сразу
   появится в виджете «Календарь» на странице Widgets (вкладки Сегодня / Неделя).
7. «Брифинг» → `cal_brief` и верни текст как есть. Чтобы включить утренний брифинг,
   поставь повторяющееся `schedule_followup(cron="30 7 * * 1-5", timezone=<tz владельца>,
   objective="Утренний брифинг: вызови cal_brief(date='today') и отправь текст владельцу без записей")`.

## Виджет

Страница Widgets → карточка «Календарь»: метрики дня, вкладки Сегодня / Неделя (доска
по дням недели, карточку можно перетащить на другой день — это перенос события) /
Список недели / Свободные окна, форма быстрого создания. Данные обновляются сами
каждые 30 секунд и после каждого действия.

## Правила размещения

- Без указания календаря событие идёт в «Личное».
- «в яндекс», «в яндекс-календарь» → `calendar="yandex"` (основной календарь Яндекса).
- «во все календари», «везде» → `calendar="all"` (одно событие копией во всех; в повестке показывается один раз с «также в: …»).
- Название календаря Яндекса тоже работает: `calendar="Работа"`.

## Ограничения версии

Один аккаунт Яндекса. Повторяющиеся события Яндекса показываются одним мастером с пометкой ↻
(без разворота серии). Участники и приглашения не поддерживаются. Перенос через доску сохраняет
время и меняет только день. Синхронизация с Яндексом — при обращении, не чаще раза в 2 минуты.
