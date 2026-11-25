# Стиль кода

Что здесь: как выглядит код maya_harness — инструменты проверки, импорты, типы, данные,
язык комментариев.

Чего здесь нет: git-процесса ([contributing.md](contributing.md)), устройства раннера и
решений ([design.md](design.md)), инвариантов проекта ([AGENTS.md](../AGENTS.md)).

---

## Инструменты

Конфигурация — в `pyproject.toml`, гоняет всё `just check`:

* ruff: `line-length = 120`, `target-version = "py311"`, `select = ["E", "F", "I", "UP", "B"]`.
  Порядок импортов проверяет `I`, современный синтаксис типов (`list[str]`, `X | None`) — `UP`.
* mypy `strict = true` на пакет `maya_harness` и на `scripts/release.py`; тесты под mypy не
  попадают.

## Код

* Только стандартная библиотека: `dependencies = []`, Win32 API — через `ctypes`.
* `from __future__ import annotations` — первой строкой кода в каждом модуле пакета, кроме
  `__init__.py`.
* Контракты и настройки — `@dataclass(frozen=True)` (`config.py`, `session.py`,
  `transport.py`, `call.py`, `registry.py`). Изменяемый `@dataclass` — только у объекта, который держит живой ресурс и
  меняет своё состояние: `Session`, `Slot`.
* Вызовы dcc-mcp — только в `transport.py`; остальной пакет зовёт `execute(code, timeout)` у
  `call.Target` или `Session`.
* Объявления Win32 API — только в `win32.py`: модуль, который зовёт функцию, не задаёт ей
  `argtypes`.

## Язык

* docstring и комментарии — на английском; комментарий объясняет «почему», а не «что».
* Документы — на русском, без личных путей и имён машин.
