---
name: ritm
description: "Ритм — спокойная сводка по запросу о сне, шагах, тренировках и самочувствии из показателей, которые владелец сам решил сообщить. Не медицинская карта и не автоматический сбор."
version: 0.1.0
type: instruction
permissions: []
when_to_use: >
  Владелец просит «Ритм», краткую картину сна, шагов, тренировок и
  самочувствия или сравнение с его собственными данными. Используй только
  показатели, добровольно предоставленные для этого запроса: «Ритм» не
  читает ранее сохранённые записи и аккаунты даже при разовом запросе.
  Назови пробелы, не выдумывай нормы и не ставь диагноз. Объясни, что уже
  сообщённые данные находятся в контексте модели и могут сохраниться в
  истории задачи. Только если владелец сам упомянул WHOOP и в конкретной
  сводке не хватает сна/тренировок, один раз предложи существующий
  whoop-health как необязательный путь подключения; не устанавливай, не
  авторизуй и не читай аккаунт в рамках «Ритма». Право на передачу данных
  API модели сначала нужно отдельно проверить.
model_experience:
  what_model_sees: >
    On-demand composition guide only; no tools or background work. Ask for
    missing owner-selected metrics, disclose model/history exposure, and offer
    a relevant existing device skill conditionally without installing or
    reading it. Medical records and prescriptions belong outside Ritm.
  token_effect: >
    Small manifest; the body can be opened on demand for composition. Each
    model response incurs the host's ordinary cost; no scheduled calls.
---

# Ритм — сон, движение, тренировки и самочувствие

This is an **on-request composition guide**, not a monitor, medical record,
wearable adapter, diagnosis, calendar, scheduler or notification sender. An
instruction skill has no handler: installing it does not fetch data, generate a
scheduled brief, or make another skill available. Answer in the owner's language.

## At the moment of a request

1. If the owner already wrote values here, acknowledge that they **are already**
   in the model context and may persist in task/chat history; do not pretend a
   warning can retract that disclosure. Before soliciting more, explain the
   same exposure and let the owner choose dates and metrics. Ritm uses only
   owner-supplied values for this request: do not read a health account, medical
   file, stored wearable export, or another project's data through Ritm, even
   on an ad hoc request. A separately authorized skill workflow is a separate
   decision, not an implicit permission from invoking Ritm.
2. Use only information the owner voluntarily supplied for this request,
   with the date, unit and source of each
   number. Separate self-reported wellbeing from measured sleep, steps and
   workouts. Do not treat a device's estimate as a medical measurement. If the
   owner gives no data, ask for a small set (e.g. sleep duration/date, steps,
   workout kind/duration, and one optional wellbeing note); do not fabricate a
   daily score or pretend a missing source was checked. Unavailable data stays
   missing, not zero. A comparison requires at least two comparable dates and
   consistent units; otherwise describe the supplied day only.
3. Give a concise, calm summary: **what was provided**, **what may have changed
   within those observations**, **what is unknown**, and optionally one neutral
   question the owner can answer. Avoid universal thresholds, recovery scores,
   cause-and-effect claims, diagnoses, treatment, and medication advice. An
   alarming symptom is a reason to seek professional care, not to extrapolate
   from steps or HRV. No persistent profile, diary or reminders are written.

## Existing skills are optional, never hidden dependencies

- `scholion` already owns medical-history, medications, lab and wearable-export
  workflows. Ritm does not create a second health store or call its `focus_log`
  silently. If the owner wants medical-record work, explain that separate
  boundary and check whether Scholion is actually installed/authorized.
- If the owner mentions having a WHOOP and a **specific** summary lacks relevant
  sleep or workout data, one short optional suggestion may name the existing
  `whoop-health` skill and
  the relevant sleep/workout measurements. Say that installation, the owner's
  WHOOP account, developer credentials and OAuth approval are separate steps;
  do not claim the skill is installed or ready without checking. Do not launch
  install, OAuth, `whoop_summary`, or `whoop_fetch` as part of Ritm, even if a
  metric would be useful. The same restraint applies to `sber-ring` and any other device.
  Do not repeat an integration pitch on every ordinary summary.
- Device availability does **not** settle data-use rights. [WHOOP's published
  developer terms](https://developer.whoop.com/api-terms-of-use) state an
  effective date of **2026-10-06** (future as checked 2026-09-27); they include
  AI-related restrictions, but the currently applicable terms and the intended
  use need fresh review before API data reaches an LLM. [Oura's agreement](https://cloud.ouraring.com/legal/api-agreement)
  effective 2026-06-08 §4(d) limits non-aggregator LLM access to Oura's MCP,
  while §2(c) forbids an aggregator from relaying Oura data to an AI system.
  This skill implements neither route. Do not route data via an export
  to evade those limits or promise that a user-supplied device reading makes a
  prohibited API workflow permissible. Check current primary terms and consent
  for any future integration, rather than relying on this dated note.
- Apple Health export/HealthKit needs an owner-controlled export or a separately
  authorized Apple-platform application. Ritm never asks for a full export to
  obtain a few metrics. No new device skill or connector is installed by this
  instruction.

When citing a device, name what is actually available and what remains an
unverified option. Never describe an optional future connection as a verified
reading, a grant, or a delivered summary.
