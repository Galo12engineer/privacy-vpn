# Disaster Recovery — восстановление control-plane после потери сервера

Сценарий: **anonvm (мозг) умер / изъят / необратимо потерян** (диск, VPS, доступ). Ноды — «скот»
(RAM-only, восстанавливаются провижном, состояния не хранят), поэтому этот runbook — только про
**control-plane**: аккаунты, биллинг, инвойсы, тарифы.

Восстановление держится на двух артефактах, которые **обязан хранить оператор ОФЛАЙН**, отдельно от
сервера (иначе они сгорят вместе с ним):

1. **Офлайн-копия секретов** (`.env`-значения ниже) — часть из них **невосстановима**.
2. **Свежий зашифрованный дамп БД** (`cp-<ts>.sql.age` из `infra/backups/backup.sh`) **плюс ключ для
   его расшифровки** (`BACKUP_AGE_IDENTITY`).

> ⚠️ **Без офлайн-копии секретов и без ключа расшифровки бэкапа восстановление НЕВОЗМОЖНО.**
> Наличие того и другего — **обязанность оператора** (см. §4). Код этого не гарантирует.

---

## 1. Что невосстановимо (держать офлайн; менять НЕЛЬЗЯ)

Эти значения не выводятся заново — при их потере данные становятся нечитаемыми навсегда, даже с
дампом БД на руках:

| Переменная | Роль | Что будет при потере |
|---|---|---|
| `CP_ACCOUNT_NUMBER_PEPPER` | HMAC-pepper для account-number | **Катастрофа.** Логин клиента — по account-number = HMAC(pepper, …). Другой pepper → все выданные номера перестают совпадать → **все клиенты заблокированы навсегда**. Значение должно быть **бит-в-бит прежним**. |
| `CP_EMAIL_ENCRYPTION_KEY` | Fernet-ключ шифрования email/TOTP at-rest | Если включались email-напоминания или админ-2FA: без ключа зашифрованные колонки в дампе **не расшифровать** → 2FA админов и сохранённые email потеряны. Если email/2FA не использовались — не критично. |
| `BACKUP_AGE_IDENTITY` | age-приватник для расшифровки бэкапов | **Линчпин.** Без него **любой** `*.sql.age` — мусор. Хранить ОФЛАЙН и ОТДЕЛЬНО от сервера. Если он лежал только на умершем anonvm — все бэкапы потеряны. |

## 2. Что восстановимо / ротируемо (удобно сохранить, но можно задать заново)

| Переменная | Почему не катастрофа |
|---|---|
| `POSTGRES_PASSWORD` | БД пересоздаётся из дампа — задай новый пароль в `.env`, лишь бы совпадал с `CP_DATABASE_URL` (обе строки в одном `.env`). |
| `CP_SESSION_SECRET` | Ротация просто разлогинит всех — клиенты войдут снова по account-number. Сохранить ради непрерывности сессий. |
| `CP_BLOCKBEE_API_KEY` | Перевыпускается в дашборде BlockBee. |
| `CP_BLOCKBEE_CALLBACK_SECRET` | Токен callback = HMAC(secret, "order:expected"). Смена → callback'и **уже висящих** инвойсов не пройдут аутентификацию (оплаты в полёте потеряются). Сохрани, чтобы дочесть in-flight платежи; для новых инвойсов — ротируем свободно. |
| `CP_BLOCKBEE_PAYOUT_*` | Payout-адреса — кошельки оператора; ввести заново. |

> Практика: держи офлайн **весь `.env` целиком** (шифрованно — age/gpg/менеджер паролей). Тогда
> разделение «восстановимо/нет» не приходится вспоминать под стрессом.

## 3. Процедура восстановления

Порядок важен: **сначала поднять только БД и залить дамп, потом — остальной стек**. Если поднять
control-plane на пустой БД, он выполнит `alembic upgrade head` и создаст схему — тогда заливка дампа
(с `CREATE TABLE`) конфликтует. Дамп `pg_dump` уже содержит `alembic_version`, поэтому после заливки
`alembic upgrade head` при старте — no-op.

```bash
# 0. Новый VPS (Debian 12/13/Ubuntu, ≥2GB RAM), Docker+Compose:
curl -fsSL https://get.docker.com | sh
git clone <repo> privacyvpn && cd privacyvpn            # → /opt/privacyvpn

# 1. Восстановить .env из ОФЛАЙН-копии секретов (§1–§2).
#    Критично: CP_ACCOUNT_NUMBER_PEPPER и (если использовался) CP_EMAIL_ENCRYPTION_KEY —
#    бит-в-бит прежние. Обнови PRIMARY_DOMAIN/ACME_EMAIL/CP_PUBLIC_BASE_URL при смене домена/IP.
cp /secure/offline/privacyvpn.env .env                  # источник — твоё офлайн-хранилище

# 2. Поднять ТОЛЬКО БД и дождаться healthy:
docker compose up -d db
docker compose ps db                                    # ожидаем (healthy)

# 3. Расшифровать последний бэкап ключом BACKUP_AGE_IDENTITY и залить в БД:
#    (артефакты живут в /opt/privacyvpn/backups-out/ на старом сервере ИЛИ в твоём оффсайте)
age -d -i "$BACKUP_AGE_IDENTITY" -o /tmp/cp.sql backups-out/cp-<ts>.sql.age
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < /tmp/cp.sql
shred -u /tmp/cp.sql 2>/dev/null || rm -f /tmp/cp.sql   # не оставляем открытый дамп

# 4. Поднять остальной стек (alembic upgrade head → no-op на восстановленной схеме):
docker compose up -d --build

# 5. Проверить (см. §5). .onion сменится (новый ключ Tor) — опубликуй новый адрес.
```

**Проверка бэкапа ДО катастрофы (обязательно, периодически):** бэкап без проверенного restore — не
бэкап. Гоняй `infra/backups/restore-test.sh` (нужны `BACKUP_FILE`, `SCRATCH_DATABASE_URL` на пустую
БД; `BACKUP_AGE_IDENTITY` — только для `.age`) — он расшифрует дамп, зальёт в scratch-БД и проверит,
что восстановилась именно **служба**: схема на актуальной alembic-ревизии, тарифы и админ на месте.

> **Прогнан впервые 2026-07-21** — и вскрыл в себе два дефекта, оба одного вида: скрипт сообщал
> `restore OK` на сломанном бэкапе. (1) `psql` без `ON_ERROR_STOP` идёт мимо ошибок и выходит `0`;
> (2) число строк `account` печаталось, но ни с чем не сравнивалось. Доказано на живом Postgres:
> на дампе с поломанной `price_plan` старая версия дала `exit=0 · restore OK — account rows: 0`,
> новая — `exit=3`. Ни то, ни другое нельзя было увидеть чтением: скрипт до этого **ни разу не
> запускался**. Поэтому он теперь принимает и обычный `.sql` — требование офлайн-ключа как раз и
> держало его незапускаемым; ключ нужен только для полного прогона по `.age`.
>
> Как гонять на проде, ничего не задев (psql живёт в контейнере `db`):
> ```sh
> ssh deploy@<host> 'cd /opt/privacyvpn
>   DBU=$(sudo docker compose exec -T db printenv POSTGRES_USER | tr -d "\r")
>   sudo docker compose exec -T db psql -U "$DBU" -d postgres -c "CREATE DATABASE restoretest;"
>   sudo docker compose cp <dump>.sql db:/tmp/d.sql
>   sudo docker compose cp infra/backups/restore-test.sh db:/tmp/rt.sh
>   sudo docker compose exec -T -e BACKUP_FILE=/tmp/d.sql \
>     -e SCRATCH_DATABASE_URL="postgresql://$DBU@localhost/restoretest" db /tmp/rt.sh'
> ```
> **После прогона обязательно:** `DROP DATABASE restoretest` и `shred -u` дампа — в нём хэши
> номеров аккаунтов и хэш пароля админа.

## 4. Обязанности оператора (НЕ покрыто кодом — сделать вручную)

- [ ] **Офлайн-копия секретов.** Держать зашифрованную копию всего `.env` (минимум невосстановимых
      из §1) вне сервера: менеджер паролей / оффлайн-носитель / age-шифр в другом месте. Обновлять
      при каждой ротации.
- [ ] **`BACKUP_AGE_IDENTITY` хранить отдельно от anonvm.** Если он только на сервере — бэкапы
      бесполезны после его потери.
- [x] **systemd-timer бэкапа на anonvm — УСТАНОВЛЕН (2026-07-03).** Ежедневно ~03:17 UTC:
      `privacyvpn-backup.timer` → `.service` → `infra/backups/backup-anonvm.sh` (дамп через
      db-контейнер → age-шифр на хосте → ротация 14, root-only 0600). Публичный recipient в
      `/etc/privacyvpn-backup.env`. Restore-тест при установке прошёл (node-строки совпали).
- [ ] **ОФФСАЙТ — ЕЩЁ НЕ НАСТРОЕН (остаток оператора).** Бэкапы лежат в
      `/opt/privacyvpn/backups-out/` НА ТОМ ЖЕ VPS → потеря VPS = потеря бэкапов. Настрой
      `BACKUP_REMOTE`+rclone в `/etc/privacyvpn-backup.env` ИЛИ регулярно тяни `backups-out/` к
      себе (`rsync`/`scp`). Без оффсайта DR неполон.
- [ ] **Регулярный restore-тест** через `infra/backups/restore-test.sh` (или повтори процедуру §3
      в scratch-БД).
- [ ] После восстановления: перепровизии ноды при необходимости и **обнови .onion-адрес** в
      кабинете/доках (ключ Tor не переносится в этом runbook).

## 5. Автобэкап (УСТАНОВЛЕН на anonvm 2026-07-03)

Юниты в репо: `infra/systemd/privacyvpn-backup.{service,timer}` → скрипт
`infra/backups/backup-anonvm.sh`. На проде скопированы в `/etc/systemd/system/`, таймер
`enabled+active` (ежедневно ~03:17 UTC, `Persistent=true` догоняет пропуски).

Особенность docker-compose-деплоя: на хосте НЕТ `pg_dump`, а `db` резолвится только в
docker-сети → `backup-anonvm.sh` дампит **через контейнер** (`docker compose exec -T db
pg_dump`) и шифрует age на хосте. Открытый дамп живёт только в `/dev/shm` (RAM) и шредится;
на диск ложится лишь `cp-<ts>.sql.age` (root-only 0600) в `/opt/privacyvpn/backups-out/`,
ротация — последние `BACKUP_KEEP` (14).

Конфиг: `/etc/privacyvpn-backup.env` (root 0600) — `BACKUP_AGE_RECIPIENT=age1…` (публичный),
`BACKUP_KEEP`, опц. `BACKUP_REMOTE` (rclone-оффсайт).

```bash
sudo systemctl start privacyvpn-backup.service        # прогнать вручную
systemctl list-timers privacyvpn-backup.timer          # следующий запуск
sudo ls -l /opt/privacyvpn/backups-out/                # артефакты (root-only)
```

> **age-пара:** публичный `BACKUP_AGE_RECIPIENT` шифрует (живёт на сервере в env); приватный
> `BACKUP_AGE_IDENTITY` (`AGE-SECRET-KEY-1…`) расшифровывает — **хранится ТОЛЬКО офлайн у
> оператора**, на сервере его нет. Потеря identity = все бэкапы = мусор (§1).

## 6. Smoke после восстановления

```bash
docker compose ps                              # все running, db (healthy)
curl -fsS https://<domain>/healthz             # 200 {"status":"ok"} (проверяет БД)
```
Затем вход в `/admin/login`, проверка, что тарифы/аккаунты на месте, и (если был) повторный вход
клиента по прежнему account-number — подтверждает, что `CP_ACCOUNT_NUMBER_PEPPER` восстановлен верно.
