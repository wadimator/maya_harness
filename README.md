# maya_harness

Изолированная GUI-Maya 2025 для разработки плагина: поднять, выполнить код, посмотреть
состояние, закрыть. Удобна человеку и агенту: Maya работает на скрытом рабочем столе, со своими
настройками и окружением, и не трогает Maya и настройки художника.

* `maya-harness start` — поднять Maya проекта и оставить её работать;
* `maya-harness exec -c "<код>"` — выполнить код в ней и напечатать результат;
* `maya-harness status` — какие Maya запущены;
* `maya-harness stop` — закрыть;
* `maya-harness run <сценарии | all>` — прогнать сценарии проверок плагина в нескольких Maya
  сразу ([docs/scenarios.md](docs/scenarios.md));
* `maya-harness doctor` — готова ли машина.

Код выполняется через [dcc-mcp-maya](https://github.com/dcc-mcp/dcc-mcp-maya), MCP-сервер внутри
Maya. Как устроен запуск и почему — [docs/design.md](docs/design.md), что делать, если не
работает, — [docs/diagnostics.md](docs/diagnostics.md).

## Что нужно

* Windows и Autodesk Maya 2025. Maya ищется так: `maya.exe` рядом с `MAYAPY_EXE`, иначе в
  `MAYA_LOCATION`, иначе в `C:\Program Files\Autodesk\Maya2025`.
* Установленный модуль dcc-mcp-maya: `.mod` из `MAYA_HARNESS_DCC_MCP_MOD`, иначе
  `Documents\maya\modules\dcc_mcp_maya.mod`. Пути в `.mod` — абсолютные: раннер копирует его в
  каталог сеанса.
* Python 3.11 и [uv](https://docs.astral.sh/uv/). Пакет — только стандартная библиотека.

## Установка

В плагин — dev-зависимостью, **готовым колесом из GitHub Release тега**:

```toml
[dependency-groups]
dev = [
    "maya-harness",
]

[tool.uv.sources]
maya-harness = { url = "https://github.com/wadimator/maya_harness/releases/download/v0.3.0/maya_harness-0.3.0-py3-none-any.whl" }
```

Затем `uv lock` и `uv sync`. Git-источник (`{ git = "..." }`) не подходит: uv собирает его из
исходников, а у проекта с `no-build = true` `uv sync` тогда падает («marked as `--no-build` but
has no binary distribution»; `uv lock` при этом проходит). Новая версия — правка URL на колесо
нового тега.

Команда в проекте — `uv run --frozen --exact maya-harness <команда>`; удобно завести рецепт
`just maya *arguments`.

## Настройки проекта

`[tool.maya_harness]` в `pyproject.toml` проекта; ищется ближайший вверх от текущего каталога.
Все ключи необязательны, неизвестный ключ — ошибка.

```toml
[tool.maya_harness]
project = "UE_exporter"           # имя в status и в каталоге сеанса; по умолчанию [project].name
sys_path = ["..", "vendor"]       # относительно корня проекта; первыми в sys.path Maya
plugins = ["fbxmaya"]             # плагины Maya, загружаемые при старте
plug_in_path = ["maya_plugins"]   # MAYA_PLUG_IN_PATH: свои плагины Maya проекта
reload = ["UE_exporter"]          # пакеты, которые exec --reload и run выгружают из sys.modules

[tool.maya_harness.scenarios]     # только для run; нужны оба ключа
dir = "dev_tools/maya_checks"     # сценарии check_*.py
runs = "dev_tools/maya_checks/runs.toml"  # таблица прогонов: что гонит run all
```

`sys_path` и `plugins` запоминаются при `start`: правка настроек действует со следующего
`start`.

## Команды

| Команда | Что делает |
| --- | --- |
| `start [--visible] [--ttl МИН]` | поднять Maya проекта; печатает pid и адрес. `--visible` — на обычном рабочем столе; `--ttl` — закрыться после стольких минут простоя, 60 по умолчанию, `0` — без предела |
| `exec [URL] (-c КОД \| -f ФАЙЛ) [--reload] [--json]` | выполнить код; печатает stdout, stderr и значение последнего выражения, если код им кончается. Без URL — последняя запущенная Maya этого корня проекта (worktree — другой корень, своя Maya). `--reload` — сначала выгрузить пакеты из `reload` |
| `status [--json]` | все Maya maya_harness этого пользователя: проект, pid, адрес, возраст, простой |
| `stop [URL \| --project \| --all-mine]` | закрыть последнюю Maya этого проекта, Maya по адресу, все Maya проекта или все свои. Maya, не принявшую `quit` за 60 с, — убить, если pid по-прежнему её |
| `run (СЦЕНАРИИ \| all) [ПАРАМЕТР=ЗНАЧЕНИЕ] [--jobs N] [--url URL] [--visible] [--json]` | прогнать сценарии в своих Maya (`--jobs`, 3 по умолчанию) или по очереди в Maya по адресу; `ПАРАМЕТР` — `control` или `section` одного сценария. Отчёт каждого прогона и сводка; `--json` — сводка одной строкой, отчёты в stderr. Сценарии и контроли — [docs/scenarios.md](docs/scenarios.md) |
| `doctor` | Maya, `.mod`, пути в нём, версия модуля; патч DCC_MCP — если `MAYA_HARNESS_DCC_MCP_PATCH` указывает на клон DCC_MCP |

Коды возврата: `0` — успех; `1` — код упал или проверка не прошла; `2` — ничего не запущено
(нет Maya, нет `.mod`, нет сеанса, Maya не поднялась за 180 с). `exec` без ответа Maya
(таймаут, обрыв, нечитаемый ответ) — `1`: код мог выполниться; отказ в соединении — `2`: запрос
до Maya не дошёл. `--json` — одна JSON-строка на stdout и больше ничего.

Код, который кончается присваиванием, циклом или `def`, просто выполняется: значения нет.
Значение последнего выражения печатается как `str()`: словарь — `{'a': 1}`, не JSON. Нужен
JSON — заканчивай код `json.dumps(...)`.

## API для библиотеки

Свой раннер поверх пакета берёт сеанс из `maya_harness.session`, а не собирает его из
`process` и `transport`:

```python
from pathlib import Path

from maya_harness import session

params = session.SessionParams("probe", Path.cwd(), sys_path=(Path.cwd() / "src",))
with session.start(params, visible=False, env=None) as s:
    s.wait(180)
    result = s.execute("import maya.cmds as c; c.about(v=True)", 60)
    print(result.success, result.value())
```

* `start` заводит каталог сеанса и поднимает Maya, но сервер не ждёт: так пул поднимает
  несколько Maya сразу. До `execute` обязателен `wait(timeout)`.
* Нет `maya.exe`, нет `.mod`, Maya не поднялась за `timeout` — `session.StartError`; в тексте
  pid, окна Maya и её логи.
* При выходе из `with`, исключении и Ctrl+C Maya убита (job object), каталог сеанса удалён.
  Когда сеансов несколько или сеанс меняется в цикле — без `with`: `start`, `wait`, а
  `close()` — в `finally`.
* `release()` — только чтобы Maya пережила процесс (так делает `maya-harness start`): ни Maya,
  ни каталог тогда не удаляются.
* `env` — базовое окружение, по умолчанию `os.environ`: по нему ищутся `maya.exe` и `.mod`, из
  него строится окружение Maya. `MAYA_*`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP` и
  `VIRTUAL_ENV` пакет убирает из него всё равно.
* `execute(code, timeout)` возвращает `Result`: `success`, `error`, `stdout`, `stderr`,
  `output` — значение последнего выражения текстом; `value()` — оно же, разобранное как JSON
  (не JSON — текст как есть, нет значения — `None`).

## Примеры для агента

```powershell
uv run --frozen --exact maya-harness start
uv run --frozen --exact maya-harness exec -c "import json, maya.cmds as c; json.dumps(c.about(v=True))"
uv run --frozen --exact maya-harness exec --reload -f probe.py --json
uv run --frozen --exact maya-harness stop
```

* Разовая проба — файлом во временном каталоге и `exec -f`, а не файлом в репозитории.
* Код идёт в главный поток Maya. Пока Maya открывает или сохраняет сцену, вызов получает отказ
  `maya-busy`, и `exec` сам повторяет его раз в 0.5 с.
* Модальный диалог вешает скрытую Maya: вызов не вернётся до таймаута
  ([docs/diagnostics.md](docs/diagnostics.md)).
* Ctrl+C во время `start` убивает поднимаемую Maya. Проверять это — из PowerShell: процесс,
  запущенный из Git Bash, Ctrl+C не получает.
* Забытая Maya закрывается сама после `--ttl` минут простоя.

## Подключение к плагину

Скопируй промпт в агента (Claude Code, Codex, Cursor), открытого в репозитории плагина:

```text
Подключи к этому плагину Maya раннер maya_harness. Сначала прочитай его README:
https://github.com/wadimator/maya_harness

1. Зависимость. Добавь `maya-harness` в dev-группу `pyproject.toml` и источник
   `[tool.uv.sources]` — колесо из последнего GitHub Release (URL на .whl, не git:
   при `no-build = true` git-источник не ставится). Выполни `uv lock`, затем `uv sync`.
2. Настройки. Найди, как плагин попадает в Maya: каталог пакета, vendored-зависимости,
   свои плагины Maya, нужные плагины Autodesk (fbxmaya и т.п.). Запиши это в
   `[tool.maya_harness]`: `sys_path`, `plug_in_path`, `plugins`, `reload` — пути
   относительно корня проекта.
3. Команда. Если в проекте есть justfile — рецепт
   `maya *arguments: uv run --frozen --exact maya-harness {{ arguments }}`.
   Если у плагина уже есть свой раннер Maya — скажи об этом и не удаляй его без моего решения.
4. Инструкции агентам. В AGENTS.md (или что использует проект) — строка: GUI-Maya для проверок —
   `maya-harness start`, затем `exec`, в конце `stop`; разовые пробы — файлом вне репозитория.
5. Проверка. `maya-harness doctor`, затем `start`, `exec -c` с импортом пакета плагина,
   `stop`. Сообщи, что изменено и что напечатала каждая команда.
```

## Разработка

Правила — [AGENTS.md](AGENTS.md), git-процесс и выпуск версии —
[docs/contributing.md](docs/contributing.md).
