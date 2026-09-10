---
name: sber_operational_director
description: ИИ-агент «Операционный директор» Сбербанка через MCP — сбор клиентских данных и ответы на бизнес-вопросы (ограничения, задолженность, справки, карты, профиль и т.п.).
version: 0.2.10
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [net, tool, route, widget, read_settings]
env_from_settings:
  - SBER_ACCESS_TOKEN
  - SBER_TLS_P12_PASSWORD
  - SBER_MCP_URL
when_to_use: Пользователь просит у «Операционного директора» Сбера бизнес-сведения — есть ли ограничения на счетах, задолженность, готовые справки, бизнес-карты, профиль организации, доверенности, операции, балансы, обороты, сводку по банку и т.п. (без технических кодов разделов).
timeout_sec: 90
ui_tab:
  tab_id: operational_director
  title: Операционный директор
  icon: "🏦"
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: markdown
        text: |
          ## Настройка
          Проверьте токен, пароль P12, клиентский .p12 и CA банка.
          Корневой CA для PROM встроен. Клиентский .p12/.pfx ставится из вложений чата.
          В таблице: **OK** — готово, **Нет** — нужно настроить.
      - type: action
        id: check_setup
        route: check_setup
        method: POST
        target: setup
        fields: []
        submit_label: Проверить настройку
        busy_label: Проверяю…
      - type: status
        target: setup
        idle: Нажмите «Проверить настройку»
        loading: Проверяю конфигурацию…
        error: Не удалось проверить настройку
        success: Статус обновлён
      - type: kv
        target: setup
        fields:
          - label: Готовность
            path: ready_label
          - label: Контур
            path: stand
          - label: MCP URL
            path: mcp_url
      - type: table
        target: setup
        path: checks
        columns:
          - label: Статус
            path: mark
          - label: Параметр
            path: title
          - label: Детали
            path: detail
          - label: Как настроить
            path: how
      - type: markdown
        target: setup
        path: hints_md
      - type: markdown
        text: |
          ## Запрос к MCP
          Сформулируйте **бизнес-вопрос** (что нужно узнать по клиенту).
          Ключ сессии можно оставить пустым. Поле кодов разделов — только для отладки.
      - type: form
        route: check_collect
        method: POST
        target: mcp
        fields:
          - name: text_input
            label: Вопрос / инструкция для MCP
            type: text
            placeholder: "Какие ограничения на счетах?"
            required: true
          - name: integration_name
            label: Коды разделов (опционально, отладка)
            type: text
            placeholder: ""
            required: false
          - name: legal_person_session_id
            label: Ключ сессии (пусто = новый uuid4)
            type: text
            placeholder: ""
            required: false
        submit_label: Отправить в MCP
      - type: status
        target: mcp
        idle: Введите вопрос и нажмите «Отправить в MCP»
        loading: Запрос к MCP…
        error: Ошибка запроса к MCP
        success: Ответ MCP получен
      - type: kv
        target: mcp
        fields:
          - label: Session ID
            path: legalPersonSessionId
          - label: Разделы
            path: integrations_label
          - label: Сбор завершён
            path: collected_label
          - label: Ошибка
            path: error
      - type: json
        target: mcp
        label: Ответ check_collect
      - type: markdown
        text: |
          ### Получить данные
          После `is_data_collected=true` — тот же Session ID и **те же разделы**, что в check_collect.
      - type: form
        route: get_data
        method: POST
        target: data
        fields:
          - name: legal_person_session_id
            label: Session ID из ответа выше
            type: text
            placeholder: "00000000-0000-4000-8000-000000000000"
            required: true
          - name: integration_name
            label: Те же коды разделов, что в check_collect (если задавали)
            type: text
            placeholder: ""
            required: false
        submit_label: Получить данные
      - type: status
        target: data
        idle: Укажите Session ID и нажмите «Получить данные»
        loading: Запрашиваю данные…
        error: Ошибка get_data
        success: Данные получены
      - type: json
        target: data
        label: Ответ get_data
---

# Агент «Операционный директор» Сбербанк (MCP)

Расширение для MCP-сервера `transactional-agent`: сбор клиентских данных и ответы ИИ-агента «Операционный директор» на **бизнес-вопросы**.

**Клиенту** — только смысл запроса (что узнать по организации). Технические коды разделов клиенту не показывать и не спрашивать: агент сам подбирает их по смыслу.

## Если скилл не настроен

Перед любым сбором данных (или сразу, если пользователь просит «проверить настройку»):

1. Вызовите `check_setup`.
2. Если `ready=false` — **обязательно напишите в чат** текст из поля `chat_instruction` (что нужно и где настроить). Не ограничивайтесь фразой «скилл не настроен».
3. Пока `ready=false`, **не** вызывайте `check_collect` / `get_data`.
4. Если пользователь прикрепил `.p12` / `.pfx` или CA — установите через `install_tls_certificate` / `install_ca_certificate`, снова `check_setup`, и только при `ready=true` продолжайте исходный запрос.

То же правило, если `check_collect` / `get_data` вернули `error` с `chat_instruction` / `message_for_user`: перескажите инструкцию в чат.

## Пользователь приложил сертификат: довести настройку до проверки

В контексте настройки этого скилла вложение сертификата — сигнал продолжить настройку,
даже если сообщение содержит только файл. Не завершайте ответ фразой «файл загружен»
или «сертификат установлен»: это промежуточные шаги.

1. Вызовите `check_setup`, затем установите приложенный `.p12` / `.pfx` через
   `install_tls_certificate`, а CA `.crt` / `.pem` / `.cer` — через `install_ca_certificate`.
   Используйте фактический путь вложения. Если для P12 нет пароля или Grant,
   объясните, как задать `SBER_TLS_P12_PASSWORD` в Settings и выдать Grant; не просите
   отправлять пароль или токен в чат. Не выдавайте загрузку файла за успешную установку.
2. После установки снова вызовите `check_setup`. Если `ready=false`, сообщите
   конкретные недостающие настройки из `chat_instruction`. Сохраните исходный запрос
   и продолжите его после сообщения пользователя об исправлении настроек.
3. При `ready=true` в этом же сценарии проверьте реальный запрос к банку.
   Если есть исходный бизнес-вопрос, выполните его. Если пользователь только
   настраивает подключение, используйте минимальный запрос чтения: `text_input="Покажи
   профиль организации"`, `integration_name=["client_profile"]`.
4. Выполните `check_collect`, затем `get_data` по асинхронному flow ниже, сохраняя
   один UUID сессии и те же разделы. Для проверочного запроса ограничьте ожидание
   пятью минутами и максимум десятью вызовами сбора/получения суммарно. При ошибке
   остановитесь и объясните её; при истечении ожидания сообщите, что результат ещё
   не получен, сохранив Session ID для продолжения. Не запускайте новую сессию при опросе.
5. Подтвердите работоспособность только после успешного получения непустого ответа
   без ошибки от `get_data`. `ready=true` подтверждает лишь локальную настройку,
   а принятие `check_collect` — начало обработки. При проверке подключения достаточно
   сообщить результат проверки без вывода полного профиля организации.

## Возможности

| Инструмент агента | MCP tool | Назначение |
|---|---|---|
| `check_setup` | — | Самопроверка; при `ready=false` вернуть пользователю `chat_instruction` |
| `install_tls_certificate` | — | Установить клиентский .p12/.pfx из вложения чата |
| `install_ca_certificate` | — | Установить CA-цепочку банка (.crt/.pem) из вложения чата |
| `check_collect` | `summarise.agent_check_collect` | Асинхронный запуск сбора; для подробного ответа — `integration_name` по смыслу вопроса; возвращает `data` / `is_data_collected` |
| `get_data` | `summarise.agent_get_data` | Ответ по сессии; те же `integration_name`, что в check_collect — иначе кратко; `data=null` — генерация ещё идёт |

### Как мапить бизнес-вопрос → `integration_name` (только для агента)

| Смысл запроса клиента | Код |
|---|---|
| Ограничения / блокировки / ФССП на счетах | `fskk` |
| Задолженность / УДКЗ | `udkz` |
| Справки и документы к выдаче | `documents` |
| Отмена / отзыв документов | `documentCancel` |
| Бизнес-карты | `business_card` |
| Профиль клиента / организации | `client_profile` |
| Задачи / поручения в банке | `tasklist` |
| Доверенности / полномочия | `authority` |
| Обороты / движения по счетам | `account_turns` |

Краткий ответ — без `integration_name`. Подробный — передайте нужные коды в `check_collect` и те же в `get_data`.

## Типовой flow (сервер асинхронный!)

Сбор и генерация идут в фоне на стороне банка; ожидание результата — повторные вызовы, не долгий блокирующий запрос.

1. Агент генерирует `legal_person_session_id` (uuid4) сам — **у пользователя не спрашивать**. Вызов `check_collect` + `text_input` с формулировкой клиента. Для **подробного** ответа сам добавьте `integration_name` по таблице выше; без кодов ответ будет кратким.
2. Если `is_data_collected=false` — пауза 10–30 сек и повторный `check_collect` с **тем же** ключом и теми же кодами, пока не станет `true`
3. `get_data` с тем же ключом и **теми же** `integration_name`
4. Если `data` по разделу `null` — пауза и повторный `get_data` с теми же аргументами
5. В ответе клиенту — бизнес-вывод, без перечисления кодов разделов

## Настройка через чат

1. В **Settings** задайте и выдайте grant:
   - `SBER_ACCESS_TOKEN` — токен со scope **`MCP_TRANSACT_AGENT`**
   - `SBER_TLS_P12_PASSWORD` — пароль к P12
2. **Прикрепите** в чат:
   - клиентский `.p12` / `.pfx`
   - Для PROM корневой `SberCA Root Ext` уже включён в скилл — прикладывать его не нужно.
   - Для IFT или другой CA-цепочки прикрепите соответствующий `.crt` / `.pem`.
3. Агент вызывает:
   - `install_tls_certificate(source_path=...)`
   - `install_ca_certificate(source_path=...)` — только при установке отдельной CA-цепочки.
4. Агент автоматически проверяет настройку и выполняет проверочный запрос по сценарию выше;
   если ранее были запрошены бизнес-сведения, продолжает этот запрос.

### MCP endpoints

| Контур | URL |
|---|---|
| Пром (по умолчанию) | `https://fintech.sberbank.ru:9443/fintech/api/transactional-agent/mcp` |
| Тест (IFT) | `https://iftfintech.testsbi.sberbank.ru:9443/fintech/api/transactional-agent/mcp` |

Если нужен IFT — задайте `SBER_MCP_URL` = `ift` или полный URL.

Встроенный `certs/SberCA_Root_Ext.crt` используется только для PROM, если в state нет
пользовательской CA. Установленная CA имеет приоритет; если она повреждена, проверка
настройки сообщит об ошибке, без автоматического переключения на встроенную.
Встроенный файл — один корневой сертификат, а не полная цепочка промежуточных CA.

## Примеры запросов агенту

- «Проверь, настроен ли «Операционный директор»»
- «Есть ли ограничения или блокировки по счетам?»
- «Какая задолженность у организации?»
- «Какие справки готовы к выдаче?»
- «Покажи профиль организации и действующие доверенности»
- «Что по бизнес-картам и оборотам за период?»
