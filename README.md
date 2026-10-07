# SI-01 — встречающий Telegram-бот VPA

Первая версия по `docs/VPAI_v0.3_FINAL_handoff_for_Daniyar.md` и ответам Артура от 06.10.2026.
Python 3.11+, aiogram 3, Claude (`claude-opus-5-5`), SQLAlchemy (SQLite в разработке, Postgres на сервере).

> ⚠️ Разрабатываем на **тестовом** боте в группе VPA DEV. К настоящему VPA подключаем только после «основа работает».

## Быстрый старт (dev)

1. **BotFather** → `/newbot` → тестовый SI-01 → токен. Там же `/setprivacy` → Disable (чтобы бот видел реплаи админов в группе).
2. Создай группу **VPA DEV** (будет и админ-чатом на время разработки), добавь туда себя, Артура и тестового бота.
   Напиши в группе `/chatid` — бот ответит `chat_id` и твой `user_id`.
3. Настрой окружение:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
   cp .env.example .env   # BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_IDS, ANTHROPIC_API_KEY
   .venv/bin/python -m si01
   ```
4. Напиши боту в личку. `/reset` (только для ADMIN_IDS) — сбросить себя в начало, чтобы пройти путь заново.

Тесты (без сети и без токенов): `.venv/bin/python -m pytest`

## Как устроено

Слои из документа → файлы:

| Слой | Где |
|---|---|
| Telegram-слой (ничего не решает) | `si01/telegram.py` |
| System Prompt — характер и правила | `prompts/system.md` (версия в первой строке → `prompt_version`) |
| VPA_FACTS — источник истины | `config/facts.yaml` (`facts_version`) |
| State Machine — этап человека | `si01/states.py` |
| Session Context — текущий разговор, сброс через 48 ч | `si01/brain.py` (`touch_session`, `maybe_summarize`) |
| LLM Orchestrator | `si01/brain.py` |
| Action Layer — что модель может «сделать» | `si01/actions.py` |
| Шаги, которые делают люди и Tribute | `si01/flows.py`, `si01/tribute.py` |
| Eval Runner | `evals/scenarios.yaml`, `evals/run_evals.py` |

**Один ход:** сообщение → пользователь и его этап из БД → история текущей сессии → запрос к Claude
(system = правила + факты; в конце — служебное сообщение с этапом, статусами и тем, что можно дальше)
→ если модель вызвала действие, бэкенд проверяет этап и выполняет/отказывает → ответ → лог хода.

**Этапы:** `NEW → EXPLORING → VERIFICATION_PENDING → VERIFICATION_REVIEW → PAYMENT_PENDING → MEMBER`
(+ `REJECTED`, `MEMBER_CANCELLED`, `EXPIRED`). Кто какой переход может сделать — таблица `TRANSITIONS`:
модель максимум начинает проверку; одобрение — только админ; `MEMBER` — только вебхук Tribute.

**Действия модели:** `begin_verification`, `request_call_verification`, `resend_payment_link` (только после одобрения),
`notify_admins` (живой человек / апелляция / перенос оплаты / удаление данных / возвращение). Ссылку на оплату
модель в текст не пишет никогда — её присылает система кнопкой.

## Вступление

1. Человек пишет «хочу вступить» → модель вызывает `begin_verification` → просит кружок или голосовое.
2. Кружок/голосовое → копия в админ-чат с кнопками **Одобрить / Отклонить**. Сам файл у нас не сохраняется.
   Не может/не хочет — модель через `request_call_verification` отправляет в админ-чат «просит проверку созвоном».
3. **Одобрить** (только `ADMIN_IDS`) → человеку сразу уходит кнопка с оплатой Tribute (`TRIBUTE_PAYMENT_URL`).
4. Оплата → вебхук Tribute `new_subscription` → `MEMBER`. Доступ к комнатам выдаёт сам Tribute.
   `cancelled_subscription` → `MEMBER_CANCELLED` (доступ до `expires_at`), после — `EXPIRED`.

## Админ-чат

- Кнопки на карточках: ✅/❌ для проверки, ✔️ «Закрыть» для остальных запросов.
- **Реплай на карточку** — текст уйдёт человеку как «Сообщение от администрации VPA».
- `/user <id>`, `/approve <id>`, `/reject <id>`, `/chatid`.
- В личке с ботом (для ADMIN_IDS): `/reset`; реплай на ответ бота `/label TOO_SALESY комментарий` — разметка хода для разбора
  (метки из раздела 22 документа: GOOD, TOO_ROBOTIC, TOO_SALESY, QUESTIONNAIRE, INVENTED_FACT, LOST_CONTEXT, WRONG_STATE, TOO_LONG, TOO_FAMILIAR, OTHER).

## Tribute

В дашборде Tribute → Настройки → API-ключи: сгенерировать ключ (`TRIBUTE_API_KEY`) и указать URL вебхука
`https://<домен>/webhook/tribute`. Подпись проверяется (`trbt-signature`, HMAC-SHA256 тела), повторы доставки
отбрасываются. Если у Tribute несколько подписок — перечисли нужные в `TRIBUTE_SUBSCRIPTION_IDS`.
Если оплатил человек без одобренной проверки — в админ-чат уходит предупреждение.

## Evals

```bash
.venv/bin/python -m evals.run_evals            # 36 сценариев, ~18 критических; тратит токены
.venv/bin/python -m evals.run_evals 29 39 48   # выборочно
```
Детерминированные проверки (запрещённые действия/паттерны, этап) + LLM-судья по критерию из сценария.
Отчёт с диалогами: `evals/results/*.md`. Код выхода 1, если провален критический сценарий.

## Данные

- Обычные логи (сообщения, ходы, закрытые запросы) удаляются через 60 дней (`LOG_RETENTION_DAYS`).
- Медиа проверки не скачиваются — только копируются в админ-чат.
- Обезличенный экспорт для тестов: `python -m scripts.export_anonymized out.jsonl [--labeled]`.

## Сервер

`docker compose up -d --build` — бот + Postgres. Порт 8080 закрыть reverse-proxy с HTTPS (Tribute шлёт на https).
Факты и промпт смонтированы томами: правка `config/facts.yaml` → `docker compose restart si01`.
Аккаунты (сервер, Tribute API, Anthropic, домен) — на проект, не на личный аккаунт разработчика.

## Что ещё не решено (бот на это отвечает «не зафиксировано»)

См. `docs/OPEN_QUESTIONS.md`.
