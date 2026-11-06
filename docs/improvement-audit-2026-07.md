# Improvement Audit — privacyvpn (2026-07-03)

Multi-agent аудит улучшений (10 измерений × чтение реального кода → адверсариальная верификация каждой находки по коду → синтез). **72 сырых → 61 подтверждено → 11 отсеяно (уже решено/ложь).** Не баг-хант — ищем улучшения (приватность, деньги, надёжность, чистота, безопасность). Ядро зрелое (2 прошлых аудита), новых critical нет.

## Executive summary
Три кластера риска доминируют: **(1) Приватность-несвязуемость** (флагманское обещание no-logs) на практике пробита — скраб инвойса оставляет on-chain-идентифицирующие финансовые поля и полноразрешающие таймстемпы, которые пере-связывают скрабленный инвойс с аккаунтом; терминально-неоплаченные инвойсы не скрабятся вообще. **(2) Корректность денег** — параллельные webhook'и BlockBee могут дважды начислить аккаунту (нет row-lock); нет reconcile-фолбэка → потерянный callback тихо превращает реальную on-chain оплату в «ничего не выдано». **(3) Надёжность** — SMTP без таймаута может заклинить весь однопоточный scheduler; снятие пиров у истёкших глотается при блипе node-канала (бесплатный VPN); нет Docker-healthcheck и pool_pre_ping. Сквозное: нет ruff/CI (19 латентных ошибок), нет edge-лимита размера тела, недоверенный/непиннутый Tor-образ держит .onion-ключ, EN-страницы legal/UX показывают русский текст.

## Сквозные темы
1. **Приватность-несвязуемость протекает** — скраб инвойса неполон (финполя + таймстемпы выживают и пере-связываются с аккаунтом), терминально-неоплаченные не скрабятся, нет account-level retention. No-logs надо закрыть end-to-end, не только по account_id.
2. **Корректность и долговечность денег** — конкурентные webhook'и дважды начисляют (нет lock / нет UNIQUE на subscription.invoice_id); потерянный BlockBee-callback тихо теряет оплаченный заказ (нет reconcile/алерта).
3. **Хрупкость однопоточного scheduler** — SMTP без таймаута, нет per-account изоляции в reminders, глотание провала диспатча ревокации, нет pool_pre_ping → один внешний сбой клинит/пропускает всю lifecycle-работу, и нет healthcheck заметить зависший-но-живой процесс.
4. **Связность control/data-plane ноды + харднинг канала** — bootstrap крашится при недоступном CP (control-outage → data-outage), неатомарный persist identity рискует окирпичить ноду, нода не аутентифицирует CP (http-дефолт, нет пиннинга).
5. **Ops supply-chain и edge** — непиннутые/недоверенные образы (особенно Tor с HS-ключом), нет Docker-healthcheck, нет edge body-size лимита, неотхардненный SSH на публично-IP exit-нодах.
6. **Нет автоматического quality-gate** — не в git/CI, ruff объявлен но не гоняется (19 ошибок, мёртвые BLE noqa), тесты обходят миграции (дрейф не ловится), i18n без проверки покрытия. Один CI-джоб (ruff + migration-drift + i18n-extractor) снял бы весь класс регрессий.

---

## Ранжированные находки (32)

### 🔴 HIGH
**#1 [HIGH/S] privacy · `control-plane/app/services/billing.py:84`** — Скраб инвойса оставляет on-chain-связуемые финполя и таймстемпы → пере-идентификация аккаунта после скраба.
`scrub_invoice_linkage` нулит только account_id/payment_address/external_id, но оставляет crypto_amount_paid, crypto_currency, exchange_rate, paid_at (кортеж, фингерпринтящий одну on-chain-tx), а days_granted + Subscription.starts_at(=paid_at) остаются FK-связаны с account_id. Джойн on-chain-tx → финансы инвойса → subscription → account_id переживает скраб.
_Fix:_ в scrub также нулить crypto_amount/crypto_amount_paid/crypto_currency/exchange_rate/rate_locked_at/checkout_url; огрубить/занулить paid_at; не бампать updated_at на точный момент скраба; забакетить Subscription.starts_at. Тест: ни одно финполе/полноразрешающий таймстемп не выживает скраб.

**#2 [HIGH/M] payments · `control-plane/app/services/purchase.py:150`** — Конкурентные settled-webhook'и дважды начисляют (нет row-lock на idempotency-гуарде).
`on_payment_settled` идемпотентен только по plain-read invoice.paid_at без SELECT…FOR UPDATE. Два SETTLED в двух сессиях под READ COMMITTED оба читают paid_at=None → apply_credit дважды. Единственный тест идемпотентности — последовательный, кейс не покрыт.
_Fix:_ `.with_for_update()` в _dispatch ИЛИ условный `UPDATE … SET paid_at=:now WHERE id=:id AND paid_at IS NULL` и apply_credit только при rowcount==1. UNIQUE на Subscription.invoice_id как DB-бэкстоп. Тест на гонку.

**#3 [HIGH/S] reliability · `control-plane/app/services/emailer.py:49`** — SMTP без таймаута → зависший mail-сервер вечно морозит весь scheduler.
`smtplib.SMTP(host,port)` без `timeout=`, нет setdefaulttimeout; scheduler — однопоточный блокирующий цикл, reminders идёт до invoice_expiry/scrub/node_health. Реле, принявшее TCP но не отвечающее, блокирует навсегда → ревокации/протухание/детект падения нод стоят. (Латентно, пока SMTP не сконфигурен, но фикс однострочный.)
_Fix:_ `timeout=10` в smtplib.SMTP + бандить starttls/login/send. Defense-in-depth: heartbeat-healthcheck у scheduler.

**#11 [HIGH/M] ops/security · `docker-compose.yml:114`** — .onion Tor работает на недоверенном непиннутом `osminogin/tor-simple`.
.onion-зеркало — деанон-критичный компонент, держит v3 HS-приватник в tordata. Образ single-maintainer, плавающий тег/нет digest → бэкдор-апдейт может слить HS-ключ/MITM. (Бэкап уже чист — backup-anonvm.sh только pg_dump.)
_Fix:_ собрать Tor из офиц. Tor Project Debian-repo в своём Dockerfile (или взять поддерживаемый образ), пиннить по digest. Убедиться, что tordata никогда не уходит в бэкап/rsync.

**#14 [HIGH/M] nodeagent/reliability · `node-agent/agent/bootstrap.py:55`** — bootstrap крашится если CP недоступен на старте → wg не поднимается (полный outage ноды вместо деградации).
bootstrap делает reattach/register (raise_for_status, 10s, без ретраев) ДО bring_up_interface и nft.apply. Недоступный CP на старте ноды (редеплой CP, Tor-блип) → reattach бросает → uvicorn не стартует → systemd рестарт-луп 5с, и всё это время wg не поднят, существующие клиенты отрезаны — регресс reboot-survivable дизайна.
_Fix:_ разделить data/control-plane bring-up: кэшировать server_address/subnet (и peer-set) в identity.json; поднять wg с persisted-ключом и применить nft из last-known ПЕРВЫМ, потом reattach с экспон. backoff в фоне. Минимум — обернуть reattach в backoff-ретрай вместо краша.

### 🟡 MEDIUM
**#4 [MEDIUM/M] privacy · `app/scheduler/invoice_scrub.py:23`** — Неоплаченные EXPIRED/UNDERPAID инвойсы держат account_id + профинансированный payment_address вечно (скраб не срабатывает: селектит только paid_at IS NOT NULL). Для UNDERPAID адрес реально профинансирован → стойкая связь account↔deposit-address. _Fix:_ расширить скраб на терминально-неоплаченные (EXPIRED/LATE_PAYMENT/CANCELLED) после retention. Тест.

**#5 [MEDIUM/M] security/reliability · `app/scheduler/run.py:44`** — Ревокация истёкшего аккаунта глотает провал диспатча (`except Exception: pass`) → пир остаётся живым на ноде, юзер сохраняет VPN до ребута ноды. Нет reconcile. _Fix:_ не считать провал успехом — держать pending-revocation до ack ноды ИЛИ reconcile-джоб пушит полный active-peer set. Логировать node_id+тип (без PII).

**#6 [MEDIUM/L] payments · `app/payments/blockbee.py:166`** — Нет reconcile/poll-фолбэка для потерянного BlockBee-callback → оплаченный инвойс висит в CREATED навсегда, позже EXPIRED без лога/сигнала. get_status — заглушка SEEN без вызовов. _Fix:_ reconcile-джоб по pending-инвойсам через BlockBee /logs → тот же on_payment_settled. Минимум: алерт когда инвойс с non-null crypto_amount протухает неоплаченным.

**#7 [MEDIUM/M] i18n · `web/templates/privacy.html:13`** — /aup и /privacy не переведены на EN → английский посетитель видит русский юр-текст (в en_by_ru.json только заголовки; 13 строк privacy + 8 aup отсутствуют). Confidence 0.98. _Fix:_ добавить EN-переводы всех body-строк в en_by_ru.json + CI-чек, извлекающий каждый `_('…')` из шаблонов и падающий если исходной строки нет в словаре.

**#8 [MEDIUM/M] ux · `app/api/cabinet.py:470`** — Обновление email глотает все ошибки (`except Exception: pass`, нет rollback/лога): невалидный email тихо редиректит на /dashboard, ошибка commit оставляет success-редирект над грязной сессией; дашборд не показывает opt-in-статус. Reminders — единственное, что спасает невосстановимый аккаунт после EXPIRED. _Fix:_ ловить InvalidEmail отдельно с баннером; иначе rollback+лог; показывать статус+masked-email.

**#9 [MEDIUM/S] reliability · `app/db.py:20`** — Нет pool_pre_ping/pool_recycle → протухшие Postgres-коннекты бросают после рестарта БД/idle. scheduler держит коннект между 300с-циклами. _Fix:_ pool_pre_ping=True + pool_recycle=1800 для не-SQLite ветки.

**#10 [MEDIUM/S] ops · `docker-compose.yml:30`** — Нет Docker-healthcheck у control-plane, хотя /healthz с DB-пробой готов. Зависший-но-живой процесс не самолечится, зависимые гейтятся только на service_started. _Fix:_ healthcheck через python urllib на /healthz (в образе нет curl) + start_period под alembic; затем scheduler на service_healthy.

**#12 [MEDIUM/S] ops · `infra/reverse-proxy/Caddyfile:14`** — Нет edge-лимита размера тела. Неаутентифицированный webhook буферит полное тело (`await request.body()`) до проверки подписи → аноним POST'ит гигантское тело против 512m-воркера; Caddy режет IP → app-защита слепа. _Fix:_ `request_body { max_size 1MB }` в site-блоке.

**#13 [MEDIUM/M] nodeagent/security · `node-agent/agent/config.py:31`** — Канал node→CP дефолтит на http:// без верификации серта CP → agent_secret в register-ответе утекает MITM. Пиннинг односторонний. (Прод на .onion → E2E, это default-insecure/харднинг.) _Fix:_ требовать https/.onion для CP иначе отказ старта; TOFU-пиннить серт CP; убрать http-дефолт.

**#16 [MEDIUM/M] nodeagent/security · `infra/provision-node.sh:61`** — provision не хардит SSH (22/tcp открыт всему WAN by design, но PasswordAuth/PermitRootLogin не выключены, нет fail2ban); git clone с непиннутого example.invalid; swapoff не проверяется (критично для no-swap). _Fix:_ SSH-харднинг + fail2ban; пиннить AGENT_REPO_URL к тегу/коммиту; assert `swapon --show` пуст; убрать example.invalid-дефолт.

**#17 [MEDIUM/M] session · `app/security/sessions.py:42`** — Cabinet-сессию нельзя отозвать: stateless-токен, 24h TTL, нет привязки. Украденная cookie работает сутки, владелец не может инвалидировать (в отличие от admin, привязанного к password_hash). _Fix:_ добавить session_epoch (int) на Account в payload как 'bnd', «выйти везде» инкрементит epoch; минимум — снизить TTL.

**#18 [MEDIUM/M] node-channel · `app/services/node_registration.py:73`** — reattach безусловно перезаписывает endpoint_host/public_key/agent_tls_cert (re-пиннинг) из тела по одному agent_secret, без лога/инварианта. Утёкший secret → перепиннить серт (все CP→node дубли идут на атакера) + сменить endpoint_host (его IP в новых WG-конфигах). _Fix:_ запретить тихую смену endpoint_host (только через admin) или алерт; no-PII warning на любую смену серта/endpoint; ротировать agent_secret на reattach.

**#19 [MEDIUM/S] concurrency · `app/services/provisioning.py:59`** — Device-limit проверка TOCTOU-racy: `active_peer_count() >= device_limit` затем insert без lock аккаунта. Два конкурентных POST /devices/add (двойной клик) оба проходят → превышение лимита. _Fix:_ `session.get(Account, id, with_for_update=True)` перед подсчётом ИЛИ advisory-lock по account_id.

**#20 [MEDIUM/M] reliability · `app/api/cabinet.py:295`** — Add-device: dispatch успешен, потом commit падает → осиротевший живой пир на ноде (Peer/IpLease откатились, но пир сконфигурен на ноде). Нет reconcile. _Fix:_ commit ДО dispatch (с retryable-ошибкой на провал dispatch); ИЛИ идемпотентный add на ноде + reconcile; минимум — компенсирующий remove при провале commit.

**#23 [MEDIUM/M] 2FA · `app/security/totp.py:30`** — TOTP без replay-защиты: код переиспользуем ~90с. Перехваченный код реиграется. Rate-limit считает попытки, не переиспользование. _Fix:_ last_totp_counter на AdminUser; verify возвращает принятый counter, отвергать не-строго-больший. Одна nullable-колонка + миграция.

**#24 [MEDIUM/S] reliability · `app/scheduler/reminders.py:45`** — Один плохой аккаунт рвёт весь reminders-батч (нет per-account try/except): InvalidToken/SMTP-ошибка → _job откатывает весь батч, теряя уже выставленные sent_at-маркеры → дубли писем; аккаунты после плохого не получают напоминание. _Fix:_ обернуть per-account тело в try/except с логом+continue; опц. commit per account.

**#25 [MEDIUM/S] performance · `app/api/payments.py:60`** — Sync httpx attribution-вызов блокирует async event-loop на webhook-пути денег. Хендлеры async, но зовут `attribution.maybe_fire` с блокирующим httpx.get(10s) на loop-треде → медленный трекер морозит все async-запросы, включая другие webhook'и. _Fix:_ сделать хендлеры plain `def` (threadpool) ИЛИ `run_in_threadpool` ИЛИ fire-and-forget.

**#28 [LOW/S] reliability · `app/scheduler/node_health.py:27`** — sweep флипает здоровые ноды DOWN/off-sale сразу после даунтайма CP (wall-clock now, игнорит длительность простоя scheduler). После редеплоя >150с первый sweep снимает все ноды с продажи разом. _Fix:_ startup-timestamp + грейс sweep'а на node_stale_seconds после старта; ИЛИ сидить last_health_at на reattach.

### 🟢 LOW
**#21 [LOW/M] tech-debt · `control-plane/pyproject.toml:33`** — Нет [tool.ruff.lint], ruff не в CI → 19 ошибок CP (E402/F821/F401) + 4 node-agent; 4 `# noqa: BLE001` мёртвы (BLE не включён). Confidence 0.97. _Fix:_ [tool.ruff.lint] с select(E,F,I,B,UP,SIM,BLE,RUF100)+per-file-ignores; починить 19; ruff-шаг в CI; туда же i18n+migration-drift чеки.

**#22 [LOW/M] replay · `app/security/signing.py:33`** — HMAC без nonce → CP↔node replay в 300с-окне (реиграный add_peer восстанавливает снятый пир; health флипает DOWN→ACTIVE). Узко (нужен on-path за mTLS), самолечится. _Fix:_ сузить окно до 30-60с для state-changing; nonce+TTL-set виденных (RAM) ИЛИ строго растущий ts.

**#26 [LOW/M] privacy · `app/models/account.py:28`** — Нет account-level retention/erasure: email_encrypted/хэш/таймстемпы держатся бессрочно на давно мёртвых аккаунтах. (Шифрован+opt-in → гигиена, не live-утечка.) _Fix:_ джоб, нулящий email/opt-in/маркеры после окна (напр. 90д без активной подписки), опц. удалять аккаунт (каскад).

**#27 [LOW/S] observability · `node-agent/agent/registration.py:94`** — push_health игнорит HTTP-статус (нет raise_for_status): 401 (десинк agent_secret)/404/500 трактуются как успех, нода молчит, CP флипает DOWN по staleness — маскирует именно класс secret-desync. _Fix:_ raise_for_status в push_health; логировать 401 как критический десинк.

**#29 [LOW/M] reliability · `app/scheduler/lifecycle.py:57`** — run_revocations делает блокирующий node-HTTP внутри открытой DB-транзакции, батч атомарен → держит коннект/локи через N×10с вызовов; провал общего commit откатывает пиров, которых ноды уже сняли (десинк). _Fix:_ разделить DB-мутацию и сеть: commit сначала (или per account), потом dispatch вне транзакции.

**#30 [LOW/S] datamodel · `app/models/peer.py:34`** — peer.revoked_at — мёртвая колонка (нигде не присваивается, все пути hard-delete), но 3 горячих запроса фильтруют `revoked_at.is_(None)` — мёртвые предикаты + латентная ловушка (uq_peer_node_pubkey несовместим с будущим soft-delete). _Fix:_ выбрать модель — при no-logs hard-delete верен → дропнуть колонку+предикаты миграцией.

**#31 [LOW/M] performance · `app/models/account.py:25`** — Sweep'ы scheduler = unindexed full-scan каждый тик (account по expires_at+peers_revoked_at, invoice с time-сравнением в Python). Ничтожно на MVP-масштабе. _Fix:_ partial/composite индексы под горячие предикаты; сравнения времени в WHERE.

**#32 [LOW/S] timing · `app/services/admin_auth.py:51`** — Admin-login timing-oracle: scrypt (N=2^14) бежит только для существующего username → измеримая разница = оракул существования админа. _Fix:_ всегда гонять scrypt против фиксированного dummy-хэша в no-user ветке (constant-work).

---

## Quick wins (высокая ценность, effort S)
1. **billing.py:88-90** — донулить финполя + огрубить paint_at в скрабе (закрывает on-chain пере-идентификацию). ⭐ №1 по ценности.
2. **emailer.py:49** — `timeout=10` в smtplib.SMTP.
3. **db.py:20** — pool_pre_ping=True + pool_recycle=1800.
4. **docker-compose.yml:30** — python-healthcheck на /healthz для control-plane.
5. **en_by_ru.json** — EN-переводы privacy.html/aup.html + CI-экстрактор.
6. **Caddyfile:14** — `request_body { max_size 1MB }`.
7. **provisioning.py:59** — lock аккаунта вокруг device-limit проверки.
8. **reminders.py:45** — per-account try/except.
9. **payments.py:57/65** — webhook-хендлеры plain def (снять блок event-loop).
10. **node-agent store.py:53** — атомарная запись identity.json/wg-private.key (temp+fsync+os.replace).

_Отсеяно верификацией (11): ложные/уже-решённые — no-PII фильтр, non-custodial, nft-изоляция, backup-путь чист и т.п._
