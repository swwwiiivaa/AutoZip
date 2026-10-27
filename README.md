# auto-unpack

Наблюдает за папкой и **автоматически распаковывает новые архивы** в отдельные подпапки. Работает на Linux и Windows, запускается при старте системы.

- Форматы: `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, `.txz`, `.7z`, `.rar`
- Каждый архив → своя папка `имя_архива/` рядом (или в `extract_root`)
- Не распаковывает недокачанные файлы (ждёт, пока размер перестанет меняться)
- Пароли: пробует из конфига / кэша, иначе шлёт уведомление и ждёт ручного `retry`
- Защита от zip-slip, чистка `__MACOSX` / `.DS_Store` / `Thumbs.db`
- Уведомления на Linux / macOS / Windows
- Логи с ротацией
- `--dry-run` для проверки без изменений

---

## 1. Установка

### 1.1 Зависимости

Нужен **Python 3.9+**.

```bash
pip install watchdog
```

Опционально — по форматам:

```bash
pip install py7zr          # для .7z
pip install rarfile        # для .rar (нужен unrar / unar / 7z в системе)
```

- Linux: `sudo apt install unrar` (или `p7zip-full`, если используешь `7z` вместо `unrar`)
- Windows: поставь [7-Zip](https://www.7-zip.org/) и добавь `7z.exe` в `PATH`, либо `unrar.exe`

На Python 3.9–3.10 нужен `tomli`:

```bash
pip install tomli
```

### 1.2 Файлы

Разложи куда-нибудь, например в `~/auto-unpack/`:

```
auto-unpack/
├── auto_unpack.py
├── config.toml
├── requirements.txt
└── README.md
```

---

## 2. Конфигурация (`config.toml`)

Минимально нужно указать `watch_dir`:

```toml
watch_dir = "~/Downloads/archives"
```

Полный пример со всеми опциями:

```toml
# Папка, за которой следим. Обязательно.
watch_dir = "~/Downloads/archives"

# Куда распаковывать. Если не указано — рядом с архивом.
# extract_root = "~/Downloads/archives/extracted"

# Какие расширения считать архивами.
formats = [".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".7z", ".rar"]

# Что делать с архивом после успешной распаковки:
#   keep   — оставить на месте (по умолчанию)
#   delete — удалить
#   move   — переместить в move_to
remove_after = "keep"
# move_to = "~/Downloads/archives/_done"

# Какие имена архивов игнорировать (glob-паттерны).
ignore_globs = ["*.backup.zip", "_*.zip", "*.partial.*"]

# Сколько секунд размер файла должен «молчать», чтобы считать загрузку законченной.
stability_seconds = 5
# Максимум ждать стабилизации, потом бросить (секунды).
stability_timeout = 300

# Распаковывать ли архивы, найденные внутри уже распакованных.
recursive_nested = false
max_depth = 2

# Удалять __MACOSX, .DS_Store, Thumbs.db, desktop.ini и т.п.
clean_junk = true

# Системные уведомления.
notify = true

# Уровень логов: DEBUG | INFO | WARNING | ERROR
log_level = "INFO"

# Путь к лог-файлу. По умолчанию (если пусто и нет терминала):
#   Linux:   ~/.local/state/auto-unpack/auto-unpack.log
#   Windows: %LOCALAPPDATA%\auto-unpack\auto-unpack.log
# log_file = "~/auto-unpack.log"

# Пароли к архивам, пробуются по порядку.
passwords = []

# Где хранить кэш паролей и очередь «ждут пароль».
# По умолчанию — рядом с логом в state.json.
# state_file = "~/.local/state/auto-unpack/state.json"
```

---

## 3. Использование

Все команды принимают `--config PATH` (по умолчанию `./config.toml`).

### 3.1 `scan` — разово распаковать то, что уже лежит

Полезно при первом запуске или если накопилось.

```bash
# Только посмотреть, что было бы сделано:
python auto_unpack.py scan --config config.toml --dry-run

# Реально распаковать всё, что есть в watch_dir:
python auto_unpack.py scan --config config.toml
```

Уже распакованные архивы `scan` **не перетирает**: рядом будет `имя (1)/`, `имя (2)/` и т.д.

### 3.2 `watch` — постоянное наблюдение

Запусти и оставь работать. Новые архивы распаковываются автоматически.

```bash
python auto_unpack.py watch --config config.toml
```

Останавливается по `Ctrl+C`. Для автозапуска см. раздел 5.

### 3.3 `retry` — дозапросить пароли

Если встретился запароленный архив и он не подошёл ни под один пароль из конфига/кэша — придёт уведомление, а запись попадёт в очередь. Чтобы ввести пароль:

```bash
python auto_unpack.py retry --config config.toml
```

Для каждого ожидающего архива спросит пароль. Введённый пароль кэшируется — повторно спрашивать не будет.

> **Почему не спрашивает сразу?** Демон работает в фоне, интерактивно спросить не может. `retry` — ручной шаг.

---

## 4. Как это работает

1. В `watch_dir` появился файл, оканчивающийся на один из `formats`.
2. Скрипт **ждёт стабилизации**: пока размер не перестанет меняться `stability_seconds` секунд. Это защищает от распаковки недокачанных файлов.
3. Создаёт папку `watch_dir/имя_архива/` (или в `extract_root/`). Если такая уже есть — `имя (1)/`, `имя (2)/` и т.д.
4. Распаковывает туда. Пути с `..`, абсолютные и опасные симлинки **пропускаются** (zip-slip protection).
5. Чистит `__MACOSX`, `.DS_Store`, `Thumbs.db` и т.п. (если `clean_junk = true`).
6. Если `recursive_nested = true` — ищет архивы внутри и распаковывает (до `max_depth`).
7. Дальше с оригиналом — по `remove_after`: оставить / удалить / переместить в `move_to`.
8. Отправляет уведомление и пишет в лог.

**Ошибки** (битый архив, нет пароля, диск заполнен) — пишутся в лог, приходят уведомлением, оригинал **не удаляется**.

---

## 5. Автозапуск при старте системы

### 5.1 Linux (systemd — user unit)

Создай `~/.config/systemd/user/auto-unpack.service`:

```ini
[Unit]
Description=auto-unpack watcher
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/auto-unpack/auto_unpack.py watch --config %h/auto-unpack/config.toml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Поправь пути под свою систему. Затем:

```bash
systemctl --user daemon-reload
systemctl --user enable --now auto-unpack

# Смотреть логи:
journalctl --user -u auto-unpack -f

# Чтобы работало даже когда не залогинен в GUI:
loginctl enable-linger "$USER"
```

Остановить / перезапустить:

```bash
systemctl --user stop auto-unpack
systemctl --user restart auto-unpack
```

### 5.2 Windows (без окна, через автозапуск)

Создай файл `auto_unpack.vbs` рядом со скриптом:

```vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw.exe ""C:\auto-unpack\auto_unpack.py"" watch --config ""C:\auto-unpack\config.toml""", 0, False
```

- `pythonw.exe` — Python без консольного окна.
- `0, False` — окно скрыто.

Помести ярлык на этот `.vbs` в папку автозапуска:

1. `Win+R` → `shell:startup` → Enter
2. Скопируй туда ярлык на `auto_unpack.vbs`

При следующем входе в систему watcher запустится сам.

**Проверить, что работает:** положи в `watch_dir` тестовый `.zip` — должен распаковаться, появится уведомление, в лог-файле будет запись.

---

## 6. Уведомления

| ОС | Как | Требует |
|---|---|---|
| Linux | `notify-send` | `libnotify-bin` (`sudo apt install libnotify-bin`) |
| Windows | PowerShell balloon tip | встроено |
| macOS | `osascript` | встроено |

Отключаются через `notify = false`.

---

## 7. Пароли

Порядок попыток:

1. `passwords = [...]` из `config.toml` — по порядку.
2. Кэш `state.json` (сохраняется после успешной распаковки).
3. Если ни один не подошёл — архив **не трогается**, оригинал остаётся, приходит уведомление, запись попадает в очередь.

Ввести пароль вручную:

```bash
python auto_unpack.py retry --config config.toml
```

> ⚠️ **AES-зашифрованные zip** (WinZip AES, 7-Zip AES для zip) стандартная библиотека `zipfile` **не читает**. Работает обычный ZipCrypto (`zip -e ...`). Для AES — распакуй через 7-Zip вручную.

---

## 8. Логи

- Если запущено из терминала — пишет в stderr.
- Всегда (если не терминал) — в файл с ротацией 5 МБ × 3.

Пути по умолчанию:

- **Linux:** `~/.local/state/auto-unpack/auto-unpack.log`
- **Windows:** `%LOCALAPPDATA%\auto-unpack\auto-unpack.log`

Переопределить:

```toml
log_file = "~/auto-unpack.log"
log_level = "DEBUG"   # подробнее
```

Быстрый просмотр:

```bash
# Linux
tail -f ~/.local/state/auto-unpack/auto-unpack.log

# Windows (PowerShell)
Get-Content "$env:LOCALAPPDATA\auto-unpack\auto-unpack.log" -Wait
```

---

## 9. Типичные сценарии

**«Хочу, чтобы после распаковки архив удалялся»**

```toml
remove_after = "delete"
```

**«Хочу складывать архивы в `_done/`»**

```toml
remove_after = "move"
move_to = "~/Downloads/archives/_done"
```

**«Не трогай мои бэкапы»**

```toml
ignore_globs = ["*backup*.zip", "photos_*.7z"]
```

**«Всё распакованное складывать в одно место»**

```toml
extract_root = "~/Downloads/extracted"
```

**«У меня есть пароль от всех рабочих архивов»**

```toml
passwords = ["secret123"]
```

**«Разово распаковать старое, потом — только новое»**

```bash
python auto_unpack.py scan  --config config.toml
python auto_unpack.py watch --config config.toml
```

**«Хочу посмотреть, что скрипт сделает, прежде чем запускать»**

```bash
python auto_unpack.py scan --config config.toml --dry-run
```

---

## 10. Траблшутинг

| Симптом | Что делать |
|---|---|
| Ничего не распаковывается | Проверь лог. Часто — `watch_dir` не существует, или файл ещё «не стабилен», или попадает под `ignore_globs`. |
| «File never stabilized» | Файл очень медленно качается. Увеличь `stability_timeout`. |
| «Password required» | `python auto_unpack.py retry --config config.toml` |
| «py7zr not installed» | `pip install py7zr` |
| «rarfile not installed» / «Cannot find working tool» | `pip install rarfile` + поставь `unrar` / `unar` / `7z` в PATH. |
| Не приходят уведомления | Linux: `notify-send "test" "ok"` в терминале — работает? Если нет, поставь `libnotify-bin`. |
| Не стартует при логине (Linux) | Забыл `loginctl enable-linger "$USER"`, или не включён сервис (`systemctl --user enable --now auto-unpack`). |
| Не стартует при логине (Windows) | Ярлык на `.vbs` должен быть именно в `shell:startup`, а `pythonw.exe` — в PATH. |
| Распаковалась папка `имя (1)` вместо перезаписи | Так и задумано: конфликты не перезаписываются. |
| AES-зашифрованный zip не идёт | Библиотека `zipfile` не умеет AES. Распакуй вручную через 7-Zip. |

---

## 11. Ограничения (честные)

- **AES-zip** не поддерживается. Только ZipCrypto.
- **Многотомные архивы** (`.zip.001` + `.z01…`, `.7z.001`) **не** поддерживаются — обработается только «главный» кусок, если он один и достаточно полный. Если нужны — скажи, добавим.
- **Пароли в демоне** не спрашиваются интерактивно — только из конфига/кэша + ручной `retry`.
- Скрипт смотрит **только прямых детей** `watch_dir` (не подпапки), если `recursive_nested` не включён.
- `tarfile.filter="data"` (безопасная распаковка) — только Python 3.12+. На старых версиях защита ручная (проверка путей и симлинков), но она чуть слабее.

---

## 12. Лицензия / вопросы

Скрипт — один файл, можно свободно менять. Если что-то не работает — сначала смотри лог с `log_level = "DEBUG"`.