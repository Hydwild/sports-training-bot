"""Страж синтаксиса CI-конфигов.

Битый YAML в .github/workflows GitHub не показывает как упавший шаг: прогон
завершается без единой работы, и «красное» выглядит как загадка. Дешевле
поймать это локально — первый же прогон споткнулся о двоеточие в названии
шага без кавычек.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent /
                    ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses_and_has_jobs(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: корень не словарь"
    # ключ `on` YAML читает как булево True — проверяем оба варианта
    assert data.get("on") or data.get(True), f"{path.name}: нет триггеров"
    jobs = data.get("jobs")
    assert jobs, f"{path.name}: нет работ"
    for name, job in jobs.items():
        assert job.get("steps"), f"{path.name}: работа {name} без шагов"


def test_ci_workflow_runs_tests_and_lint():
    ci = yaml.safe_load(
        (Path(__file__).resolve().parent.parent /
         ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    runs = " ".join(s.get("run", "")
                    for s in ci["jobs"]["tests"]["steps"])
    assert "pytest" in runs
    assert "ruff check" in runs
