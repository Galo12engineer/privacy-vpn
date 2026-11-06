# Деплой privacyvpn (host-agnostic)

Поднимает **control-plane** (мозг): Postgres + API + scheduler + Caddy(TLS) + Tor HS (.onion).
**Ноды** (RAM-only WireGuard exit) разворачиваются отдельно — см. §6. Работает на любом VPS с Docker;
выбор хостера не зашит (см. `docs/operator-opsec.md` про разделение ролей brain/exit).

## 0. Что нужно от сервера
- Linux VPS (Debian 12/13 или Ubuntu 22.04/24.04), **≥2GB RAM** для мозга (Docker-стек + Postgres).
  Абуз-толерантность мозгу не критична (это веб-приложение), важнее аптайм + persistent-диск под БД.
- **Установить Docker + Compose** (свежий VPS обычно без них):
  ```bash
  curl -fsSL https://get.docker.com | sh        # ставит docker + compose-plugin
  docker compose version                         # проверка
  ```
- DNS: **A-запись** `PRIMARY_DOMAIN` → IP сервера (например `privacyvpn.example.com`). Для авто-TLS открой
  **порты 80 и 443** (firewall/панель хостера).
- **Зеркало `privacyvpn.example.net` — НЕ 301-редирект, а независимое зеркало.** A-запись `.si` на тот же IP;
  Caddy вторым site-блоком отдаёт тот же сайт (сниппет ниже). 301 на `.com` бесполезен при изъятии
  `.com`. Настоящий несминаемый запасной вход — `.onion` (рекламируется в футере/гайдах).

  Когда `.si` указывает на сервер, добавь в `infra/reverse-proxy/Caddyfile` второй блок (или допиши
  адрес в существующий) — оба домена получат TLS и отдадут кабинет:
  ```
  privacyvpn.example.net {
      reverse_proxy control-plane:8000 {
          header_up -X-Forwarded-For
          header_up -X-Real-IP
      }
      log { format filter { fields { request>remote_ip delete } } output stderr }
  }
  ```

## 1. Конфиг
```bash
git clone <repo> privacyvpn && cd privacyvpn
cp .env.example .env
./scripts/gen-secrets.sh            # печатает сильные секреты — вставь их в .env
```
В `.env` задай минимум:
- `PRIMARY_DOMAIN=privacyvpn.example.com`, `ACME_EMAIL=<почта для Let's Encrypt>`
- `CP_PUBLIC_BASE_URL=https://privacyvpn.example.com`
- `POSTGRES_PASSWORD`, `CP_ACCOUNT_NUMBER_PEPPER`, `CP_SESSION_SECRET` (из gen-secrets)
- `CP_BOOTSTRAP_ADMIN_USER` / `CP_BOOTSTRAP_ADMIN_PASSWORD` (пароль ≥12)
- `CP_EMAIL_ENCRYPTION_KEY` — нужен только если включаешь email-напоминания/TOTP
- `CP_ADMIN_PATH` — **секретный путь админки** (defense-in-depth от брутфорса/сканеров):
  задай высокоэнтропийный путь, тогда `/admin` отдаёт 404, а панель живёт под секретом.
  Сгенерить: `printf '/c-%s\n' "$(openssl rand -hex 8)"`. Дефолт `/admin`. Docs/OpenAPI в
  проде выключены → путь не утекает. Это слой **поверх** 2FA+rate-limit, а не замена.

> ⚠️ `CP_ACCOUNT_NUMBER_PEPPER` менять можно только **до** первого запуска — смена ломает уже
> выданные account-number.

## 2. Старт
```bash
docker compose up -d --build
```
Контейнер `control-plane` на старте сам применяет `alembic upgrade head` (миграции, вкл. 0006).
Caddy получит TLS-сертификат на `PRIMARY_DOMAIN` автоматически (нужны корректная A-запись + порт 80).

## 3. Первый админ
```bash
docker compose run --rm control-plane python scripts/bootstrap_admin.py
# → "admin '<user>' created" (идемпотентно: повтор не дублирует)
```
Зайди на `https://PRIMARY_DOMAIN<CP_ADMIN_PATH>/login` (по умолчанию `/admin/login`; если задал
`CP_ADMIN_PATH` — используй свой секретный путь), **включи TOTP/2FA** (требует `CP_EMAIL_ENCRYPTION_KEY`).

### 3.1 Ротация `CP_ADMIN_PATH` (если секретный путь утёк)
Путь — слой defense-in-depth поверх пароля+2FA; при `CP_ADMIN_ONION_ONLY=true` он ещё и виден
**только по .onion**. Смена — на живом проде, само значение НИКОГДА не пишем в репо/`docs`
(живёт в `.env` + приватной записи оператора):
```bash
cp .env ~/predeploy-env-$(date -u +%Y%m%dT%H%M%SZ).bak          # бэкап
NEW="/c-$(openssl rand -hex 8)"                                  # новый высокоэнтропийный путь
sed -i "s|^CP_ADMIN_PATH=.*|CP_ADMIN_PATH=$NEW|" .env
docker compose config | grep 'CP_ADMIN_PATH:'                    # убедиться, что compose подставил новый
docker compose up -d control-plane                               # НЕ `restart` — он не перечитывает env
```
🔴 **`up -d control-plane` ПЕРЕсоздаёт контейнер → новый IP → tor держит устаревшую связь.** Если
пересобираешь/пересоздаёшь ТОЛЬКО control-plane, tor **сам НЕ пересоздаётся** (в отличие от полного
`up -d`, где зависимый сервис его тянет) — значит правило Tor обязательно вручную, иначе весь .onion
(и admin, раз он onion-only) ляжет при зелёном клирнете:
```bash
docker compose restart tor
sudo systemctl start privacyvpn-tor-refresh                      # сбросить залипший дескриптор монитора
```
Проверка — только живым Tor-фетчем, и через ДОЛГОЖИВУЩИЙ tor (свежий эфемерный клиент минуту-две не
видит новый дескриптор HS — это норма, они расходятся):
```bash
curl --socks5-hostname 127.0.0.1:9050 -o /dev/null -w '%{http_code}\n' http://<onion>$NEW/login     # 200
curl --socks5-hostname 127.0.0.1:9050 -o /dev/null -w '%{http_code}\n' http://<onion>/c-<старый>/login # 404
```
Старые admin-сессии инвалидируются сменой префикса роутера — залогиниться заново по новому пути.
**После смены обнови приватную запись доступов** (не репо).

### 3.2 Перезагрузка хоста — тот же ритуал Tor, и его легко забыть

Ребут anonvm (обновление ядра и т.п.) пересоздаёт **все** контейнеры разом, а вместе с ними и
host-tor `:9050`. Формально это подпадает под правило 3 из CLAUDE.md, но на слух «перезагрузил
сервер» не звучит как «пересоздал control-plane», поэтому проговорено отдельно.

```bash
# 1. дождаться сервиса — НЕ ssh-циклом
curl -sS -o /dev/null --retry 90 --retry-delay 5 --retry-connrefused --retry-all-errors \
     --max-time 20 -w '%{http_code}\n' https://<domain>/healthz
# 2. контейнеры поднимутся сами (restart=unless-stopped), затем ОБЯЗАТЕЛЬНО:
cd /opt/privacyvpn && docker compose restart tor
sudo systemctl start privacyvpn-tor-refresh      # без этого монитор шлёт ложный 🔴 onion=000 30-50 мин
# 3. проверка — живым Tor-фетчем, не «контейнеры up»
```

**Адрес `.onion` при ребуте НЕ меняется.** Ключ HiddenService лежит в docker-томе (`tor-data`),
переживает и перезагрузку, и пересоздание контейнера; в томе их два — сайт и onion-only админка.
Адрес меняется только при восстановлении на НОВОМ сервере: ключ Tor в бэкап не входит, см.
[disaster-recovery.md](disaster-recovery.md) — §3 «Процедура восстановления», шаг 5.

Замер на живом ребуте 2026-08-04 (ядро 6.1.0-50 → 6.1.0-51): простой ~70 с (ребут 22:07:04Z →
healthz 200 к 22:08:14Z), все шесть контейнеров поднялись сами. **Шаг `tor-refresh` окупился
измеримо:** первый же прогон монитора после загрузки взял онион за 2 с. Без сброса устаревшего
дескриптора он бы отдал `onion=000` и, накопив streak, поднял ложную тревогу. Инварианты после
ребута проверены целиком: `/guides/ru-vless` → 404 и по клирнету, и по `.onion` (прод-версия
`cabinet.py` не откатилась), лендинг 200 на обоих транспортах, обе ноды переподключились сами.

⚠️ Внешний чек с Мака простой такой длины **не увидит** — он ходит раз в 5 минут, и 70-секундное
окно легло между двумя пробами. Тишина в Telegram после планового ребута означает «не попали в
окно», а не «проверено, что всё было хорошо». Проверять руками по списку выше.

## 4. .onion-адрес
```bash
docker compose exec tor cat /var/lib/tor/vpn_cabinet/hostname
```
Опубликуй полученный `xxxxx.onion` в кабинете/доках как зеркало.

## 5. Наполнение и smoke
1. В `/admin`: создай **тарифы** (data-driven price plans) и **ноду** — получишь одноразовый
   registration-token для неё.
2. Открой `https://PRIMARY_DOMAIN`: лендинг → `/buy` (без платёжного провайдера будет «способ
   недоступен» — это норма до §7) → проверь генерацию ключей и выдачу конфига на демо-аккаунте.
3. Идемпотентность платежа покрыта тестами; live-проверку оплаты см. §7.

## 6. Нода (RAM-only exit) — отдельный VPS
Ноды — «скот»: эфемерные, IP сгорают. Разворачивай через `infra/`:
- `infra/cloud-init/node.yaml` — diskless/RAM-only bootstrap (FDE, no-swap, логи не на диск),
  параметризован под транспорт (`@AGENT_PROTOCOL@` / `@AGENT_WG_LISTEN_PORT@` / `@AGENT_XRAY_VERSION@`).
- `infra/provision-node.sh` — идемпотентный provision (nftables NAT/forward; для amneziawg ставит
  AmneziaWG-DKMS, для vless_reality — Xray-core + `xray.service`).
- Нода авто-регистрируется в control-plane по одноразовому токену (из §5.1) → появляется в продаже.
- Хостер: абуз-толерантный/DMCA-ignored + API (план: SporeStack). Обязательно **режь исходящий спам**
  (block/ratelimit порт 25), иначе IP ловит SPAMHAUS → саспенд.

### 6.1 Значения по транспорту (cloud-init → provision → CP)
Порядок: (1) создать ноду в админке `/admin/nodes` с нужным протоколом → получить токен;
(2) подставить плейсхолдеры cloud-init. Порт ноды в CP (`endpoint_port`) ДОЛЖЕН совпадать с
`AGENT_WG_LISTEN_PORT`.

| Протокол | AGENT_PROTOCOL | порт (= endpoint_port) | Доп. поля в админке |
|---|---|---|---|
| WireGuard | `wireguard` | `51820` | — |
| AmneziaWG | `amneziawg` | нестандартный, напр. `39000` (НЕ 51820) | — (профиль генерит CP) |
| VLESS+Reality | `vless_reality` | `443` | `reality_dest` (см. ниже), `reality_server_names` (SNI = хост dest); опц. `AGENT_XRAY_VERSION` |

**Выбор `reality_dest`/SNI — главный рычаг стойкости Reality** (cert на ноде НЕ нужен: Reality
заимствует хендшейк внешнего dest, как в 3x-ui/Marzban/Hiddify). Требования к dest: иностранный,
**TLS 1.3 + ALPN h2 + X25519, НЕ за CDN**, без редиректов; `serverNames` = хост dest. При создании
ноды в админке dest **проверяется автоматически** (TLS1.3 + h2 + не-CDN) — негодный не даст завести.

НЕ копируй `microsoft.com`/`apple.com` из гайдов — они за CDN, «выгорели» (все их берут) и
нестабильны (наш live-тест это и показал). Правильный способ 2025-2026 — **сканером найти non-CDN
домен в ASN/ДЦ своей ноды** ([XTLS/RealiTLScanner](https://github.com/XTLS/RealiTLScanner) —
запускать ЛОКАЛЬНО, не с VPS; или [Hiddify-Reality-Scanner](https://github.com/hiddify/Hiddify-Reality-Scanner)).
Ручная проверка кандидата: `openssl s_client -connect DEST:443 -servername DEST -tls1_3 -alpn h2`
→ `Protocol: TLSv1.3` + `ALPN protocol: h2` + `Server Temp Key: X25519`; `curl -sI https://DEST` →
не должно быть `server: cloudflare/akamai`. Reality-ключи (x25519+shortId) генерятся **per-node**
(нет общего фингерпринта на весь флот, в отличие от Marzban).

**Готовые кандидаты для СТАРТА** (прошли наш валидатор TLS1.3+h2+не-CDN; для прода ЛУЧШЕ
отсканировать домен в ASN своей ноды): `dl.google.com:443` (классика), `www.samsung.com:443`,
`www.lovelive-anime.jp:443`, `www.bing.com:443`. `serverNames` = тот же хост. Отсеяны как CDN:
nvidia/tesla (akamai), vimeo (cloudflare).

steal-oneself (свой домен+cert+вебсервер на ноде, dest → localhost) — нишевый апгрейд против
reverse-DNS-детекта, но плохо масштабируется на эфемерные RAM-only ноды; отложен.

### 6.2 Live-проверка VLESS+Reality (тест-нода, из РФ)
> 📋 Полный пошаговый прогон валидации (грант → смок через РФ-SOCKS5 → инфра-достижимость →
> живой РФ-тестер → снятие гейта) вынесен в [`docs/ru-validation.md`](ru-validation.md) вместе с
> готовыми скриптами `control-plane/scripts/ru_smoke.py` и `infra/ru-validation/ru_reach.py`.
> Ниже — краткая версия. Под ноду годится **любой свой VPS вне РФ** (не только Oracle
> free-tier): нужен только абуз-толерантный хостинг и открытый входящий TCP 443.

1. Админка → создать `vless_reality`-ноду (порт 443, dest/SNI); `available_for_sale` можно оставить
   выкл для теста; получить токен.
2. Поднять VPS (Debian 12) → cloud-init с `AGENT_PROTOCOL=vless_reality`, `AGENT_WG_LISTEN_PORT=443`.
3. На ноде проверить: `systemctl status xray` (running), `ss -ltnp | grep :443` (слушает Xray),
   `nft list chain inet vpn_filter input | grep 'tcp dport 443'` (открыт), `journalctl -u vpn-node-agent`
   (регистрация прошла). Access-логов трафика быть НЕ должно.
4. Кабинет → добавить устройство на этой ноде → получить `vless://`-ссылку/QR.
5. Импорт в **Hiddify / v2rayNG / Streisand** (НЕ WireGuard-клиент) → подключиться из РФ, проверить
   выход (myip → IP ноды) и стабильность.
6. Если рвётся: чаще всего дело в подсети/ASN сервера (иностранный DC + ~16КБ-порог ТСПУ), а не в
   транспорте — попробуй другой хостинг/ASN, steal-oneself dest (свой домен+cert на ноде), fp=firefox
   (уже дефолт). Смена только SNI/порта обычно НЕ помогает — режут по объёму+географии.

### 6.2.1 Turnkey: оператор (НЕ из РФ) поднимает тест-ноду и проверяет
Готовый заполненный шаблон: **`infra/cloud-init/node-vless.example.yaml`** (это `node.yaml` с
подставленными пример-значениями: `AGENT_PROTOCOL=vless_reality`, `AGENT_WG_LISTEN_PORT=443`, пин
`AGENT_XRAY_VERSION`). Замени `<...>` и токен.

1. **VPS** вне РФ, абуз-толерантный. Бесплатный вариант для теста — **Oracle Cloud free-tier**
   (Always Free, Ubuntu 22.04). ⚠️ у Oracle WAN-интерфейс = `ens3` (не `eth0`) — впиши в
   `AGENT_WAN_INTERFACE`; в Security List открой входящий **TCP 443** (+ 22 для SSH). Порт
   control-канала **8443** держи закрытым для всех, кроме IP control-plane (`AGENT_CONTROL_ALLOW_CIDRS`).
2. **Создать ноду в админке** `/admin/nodes`: протокол `vless_reality`, `endpoint_port=443`,
   `reality_dest` (для старта `dl.google.com:443` — валидатор проверит TLS1.3+h2+не-CDN),
   `reality_server_names=dl.google.com`. Скопируй **одноразовый токен**.
3. **Cloud-init:** впиши токен / подсеть / `CP_BASE_URL` / `AGENT_WAN_INTERFACE` / `AGENT_REPO_URL`
   в `node-vless.example.yaml` и отдай его VPS как user-data (Oracle: *Advanced → Cloud-init*).
   Нода сама тянет код, ставит Xray и регистрируется в CP.
4. **Диагностика на ноде** (SSH):
   ```bash
   systemctl status vpn-node-agent          # active; в логе — регистрация в CP прошла
   systemctl status xray                     # active (running); поднят агентом после генерации конфига
   ss -ltnp | grep ':443'                    # Xray слушает 443/tcp (Reality)
   nft list chain inet vpn_filter input | grep '443'   # tcp dport 443 accept
   journalctl -u vpn-node-agent -n 50 --no-pager       # enroll/reattach, без PII
   lsmod | grep amneziawg                    # ТОЛЬКО для amneziawg-ноды: kernel-модуль загружен (DKMS ок)
   ```
   Access-логов трафика быть НЕ должно (Xray `loglevel none` + `StandardError=null`).

**Выдать себе доступ без крипты** (тест-аккаунт — метод «оператор-грант», не оплата). Два пути,
одна и та же логика (`admin_service.create_account`) — расходиться они не могут:

*Из админки* (обычный случай): `<CP_ADMIN_PATH>/accounts` → «Создать аккаунт» → тариф или число
дней. **Номер показывается ровно один раз** в ответе: сервер хранит только его HMAC и повторно
показать не сможет — сохраните до ухода со страницы. В логе остаётся факт без номера
(`admin grant: NEW account=<uuid> +Nd`).

*Из шелла* (когда админка недоступна — например, лёг онион, а она onion-only):
```bash
docker compose exec control-plane python scripts/grant_time.py --new --days 30
#  → печатает ACCOUNT_NUMBER (вход в /login) и срок. Пишет ledger-строку Subscription
#    (price_plan_code=operator-grant, без платёжного инвойса) — грант виден в истории.
#  продлить существующий:  … grant_time.py --account "<номер>" --days 30
```
Продление существующего аккаунта из админки — там же, через поиск по номеру, который назвал
клиент (в БД лежит только хэш, поэтому найти аккаунт «по списку» нельзя — список показывает лишь
маску внутреннего id). Если у аккаунта есть счета, которые автоматика ещё может зачесть сама
(`late_payment`, см. `admin_service.rescuable_invoices`), форма **не отправится, пока вы не
укажете**, за какой из них продлеваете. Ничего не преселечено намеренно: «подарочный грант» —
такой же осознанный ответ, и он тоже требует клика. Связав грант со счётом, вы делаете повторный
авто-зачёт невозможным (дедуп по `invoice_id`); выбрав «ни за какой», получаете обычный подарок —
пришедшие позже монеты зачтутся отдельно. Отказ даёт сервер, а не браузер: без JS и из `curl`
атрибут `required` ничего не значит.
Затем кабинет `/login` (этим номером) → `/devices/add` на тест-ноде → `vless://`-ссылка + QR.

### 6.2.2 Удалённая проверка достижимости из РФ — БЕЗ присутствия
Оператор не в РФ → «подключиться лично» нельзя. Удалённо проверяется только **инфра-достижимость**
(IP/ASN/порт); **поведенческий DPI ТСПУ так не измерить** — его ловит лишь живой клиент (§6.2.3).

- **RIPE Atlas** (пробы есть в сетях РФ): measurement типа **SSLCert (TLS)** или **TCP** к
  `<IP-ноды>:443`, probes `country=RU` (напр. 20), можно по ASN: Ростелеком `AS12389`,
  МТС `AS8359`, Билайн `AS3216`, Мегафон `AS31133`.
  CLI: `ripe-atlas measure sslcert --target <IP> --port 443 --probes 20 --from-country ru`.
  **Смотреть:** долю RU-проб с успешным TLS/TCP. Успех из RU → IP/ASN не в блоклисте, 443 доступен.
  Массовые timeout/RST из RU при успехе из других стран → узел/подсеть **режут по IP/ASN**
  (менять хостинг/ASN, транспорт ни при чём).
- **check-host.net** (веб, без регистрации): вкладки **TCP connect** и **TLS** к `<IP>:443` →
  выбери российские узлы (Москва/СПб/Новосибирск/Екатеринбург). Быстрый sanity-чек того же:
  зелёные из РФ = порт достижим; красные только из РФ = IP-блок.

**Ловит:** блок по IP/подсети/ASN и недоступность 443 (инфраструктура). **НЕ ловит:** активный
пробинг Reality, DPI по объёму/таймингам (~16КБ-порог ТСПУ), throttling после установления сессии —
это видно только реальному клиенту.

### 6.2.3 Финальный гейт: РФ-бета-тестер
Инфра-достижимость (§6.2.2) ≠ выживаемость под ТСПУ. Дай тестеру `vless://`-ссылку/QR и
одностраничку **`docs/ru-beta-tester.md`** (Hiddify: Android APK / iOS сменой региона; импорт
ссылки; репорт — коннект, exit-IP, стабильность 5–10 мин, логи при отказе). 🔴 Гейт roadmap:
транспорт **не продаём**, пока этот тест не пройден.

## 7. Платежи (OxaPay) — включение + live smoke
1. В `.env`: `CP_OXAPAY_ENABLED=true`, `CP_OXAPAY_API_KEY=<merchant key>`,
   `CP_OXAPAY_AUTO_WITHDRAWAL=false` (см. `docs/payments-setup.md` §3a).
2. `docker compose up -d` (перечитать env).
3. В дашборде OxaPay укажи callback `https://PRIMARY_DOMAIN/v1/payments/webhook/oxapay`.
4. Live smoke: из кабинета `/buy` → «Перейти к оплате» → оплати малую сумму → убедись, что вебхук
   дошёл и подписка активировалась. Идемпотентность: повторный вебхук не начисляет дважды.

## 8. Бэкапы
```bash
# крон на хосте (например ежедневно):
0 3 * * *  cd /path/to/privacyvpn && ./infra/backups/backup.sh
```
Периодически прогоняй `infra/backups/restore-test.sh` (проверка восстановления). Шифруй артефакты
(см. сам скрипт) и держи копию вне сервера.

## 9. Локальный smoke без домена (без TLS)
В `.env`: `PRIMARY_DOMAIN=:80`, `ACME_EMAIL=` (пусто), `CP_COOKIE_SECURE=false`,
`CP_PUBLIC_BASE_URL=http://localhost`. Тогда Caddy отдаёт по HTTP на `:80`, cookie ставится по http.

## 10. No-logs / opsec (встроено)
- Caddy: access-лог **без client-IP** (фильтр `remote_ip delete`, проверяется CI); IP не пробрасывается
  в апстрим. Альт. nginx-конфиг — `infra/reverse-proxy/nginx.conf`.
- CP: `NoPIIFilter` режет PII из логов; security-заголовки (CSP/HSTS/no-store) — middleware в `main.py`.
- Tor HS отдаёт кабинет в .onion (`HiddenServicePort 80 control-plane:8000`).
- Ноды RAM-only; персистентного диска у них нет.

## Траблшутинг
- **TLS не выдаётся**: проверь A-запись и что порт 80 открыт/не занят; глянь `docker compose logs proxy`.
- **scheduler рестартует на старте**: он ждёт миграции control-plane (`depends_on` не ждёт их
  завершения) — поднимется сам по `restart: unless-stopped`.
- **`/buy` пустой**: не созданы price plans (см. §5.1).
- **.onion пустой файл**: tor ещё генерит ключ — подожди ~10–30 c и перечитай `hostname`.
