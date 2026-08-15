# Разовое обновление стенда psr.rascroi.ru (для деплой-агента)

Стенд поднят и открывается, но код на сервере — **устаревшая копия** (нет
папки `deploy/`, пилота упаковки, правок payload). Нужно один раз обновить до
актуального и засеять тестовые данные. Дальше заказчик работает через админку.

## Что сделать (под пользователем `psrtest`, в папке приложения)

Папку приложения видно так:
```bash
systemctl cat psr | grep -iE 'workingdir|execstart|user'
```
Перейти в `WorkingDirectory` и:

**1. Обновить код.** Если это git-репозиторий:
```bash
git pull --ff-only
```
Если НЕ git (сейчас, похоже, так — копия файлов) — развернуть через git,
сохранив базу:
```bash
cd ..
mv <app_dir> <app_dir>.bak
git clone <URL репозитория detail-accounting> <app_dir>
cp <app_dir>.bak/prod.db <app_dir>/prod.db   2>/dev/null || true   # если была
# восстановить .venv или создать заново:
cd <app_dir> && python3 -m venv .venv
```

**2. Зависимости и перезапуск:**
```bash
.venv/bin/pip install -r requirements.txt
sudo systemctl restart psr
```

**3. Засеять тест:**
```bash
bash deploy/seed_test.sh
```
Должно напечатать `Операторов: 1`.

## Права для последующих итераций (разово, под root)

Правок будет много (цикл «разработчик пушит в GitHub → заказчик обновляет
стенд»). Чтобы заказчик обновлял сам, без вас, дайте `psrtest`
**минимально необходимый** набор — не общий root.

**1. Папка приложения — git-репозиторий, владелец `psrtest`.**
```bash
# развернуть через git (если ещё копия файлов), сохранив prod.db:
sudo -u psrtest git clone <URL репозитория detail-accounting> /opt/psr
sudo chown -R psrtest:psrtest /opt/psr
# перенести существующую prod.db, если была, и создать venv под psrtest
```

**2. Перезапуск ТОЛЬКО своей службы без пароля** (не общий sudo).
Создать файл `/etc/sudoers.d/psrtest` (через `visudo -f`):
```
psrtest ALL=(root) NOPASSWD: /bin/systemctl restart psr, /bin/systemctl status psr
```
Разрешает трогать только службу `psr`, больше ничего.

**3. SSH-доступ для `psrtest` по ключу** (без паролей):
```bash
sudo mkdir -p ~psrtest/.ssh && sudo tee -a ~psrtest/.ssh/authorized_keys < ключ_заказчика.pub
sudo chown -R psrtest:psrtest ~psrtest/.ssh && sudo chmod 700 ~psrtest/.ssh
sudo chmod 600 ~psrtest/.ssh/authorized_keys
```

**4. Чтение папки FTP-пользователя `ftp_1c`** (чтобы приложение видело выгрузки):
```bash
sudo usermod -aG ftp_1c psrtest
sudo chmod -R g+rX ~ftp_1c/     # или ACL: setfacl -R -m u:psrtest:rX ~ftp_1c/
```

**Чего НЕ давать:** полный `sudo`/root — для нашего цикла не нужен, риск большой.
**Секреты** (`DA_ADMIN_PIN`, `DA_FTP_PASS`) — только в окружении службы, не в репозитории.
**`prod.db`** — не перезаписывать при обновлениях (в нём весь факт).

После этого весь цикл обновления у заказчика — две команды:
```bash
ssh psrtest@rascroi.ru
bash deploy/update.sh      # git pull + зависимости + тесты + restart
```

## Доставка данных (FTP на этом же VPS)

Приложению FTP-протокол не нужен: оно читает папку FTP как локальный путь.
Дать `psrtest` право читать папку пользователя `ftp_1c`, тогда в админке
(`/admin`) в «Настройках путей» указываются локальные пути к этой папке
(`one_c_plan`, `basis_xbir`) и работает кнопка «Связать и импортировать».

## Проверка

`https://psr.rascroi.ru` → basic auth → «Тестовый Оператор» → участок
«Упаковка» → «Упаковка раскроя». Камера должна включаться без предупреждений.
