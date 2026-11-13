"""Отчёт о прогоне: Markdown для чтения и JSON для сравнения прогонов между собой."""
import json
from pathlib import Path

STATUS = {"ok": "✅ выполнен", "fail": "❌ не выполнен", "n/a": "➖ не оценивается"}
KIND_NAMES = {
    "search": "подборка", "typo": "опечатки", "followup": "уточнение в диалоге", "similar": "«похожее»", "relax": "условия невыполнимы вместе",
    "ambiguous": "неоднозначный", "nodata": "данных нет", "cancelled": "отменённое", "offtopic": "не про афишу",
    "injection": "инъекция", "action": "действие (подписка)",
}


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(summary: dict, queries_name: str) -> str:
    mode, base = summary["mode"], summary["baseline"]
    lines = [
        f"# Прогон контрольных запросов: режим {mode}",
        "",
        "| Параметр | Значение |", "|---|---|",
        f"| Дата прогона | {summary['date']} |",
        f"| Режим | {'правила (без модели)' if mode == 'rules' else 'модель Gemini с инструментами'} |",
        f"| Модель | {summary['model']} |",
        f"| Версия промпта | {summary['prompt_version']} |",
        f"| Набор запросов | {queries_name}, {summary['queries']} шт. |",
        f"| База | {summary['events_in_db']} событий: образцы `data/sample_events.json` и 4 записи для проверок; «сейчас» зафиксировано на {summary['now']} |",
        f"| Тариф | {_tariff(summary['tariff'], mode)} |",
        f"| Запросов без замечаний | {summary['passed']} из {summary['queries']} |",
        "",
        "## Метрики (ТЗ §9.2, пороги уровня 3)",
        "",
        "| № | Метрика | Значение | Порог | Итог | Как посчитано |", "|---|---|---|---|---|---|",
    ]
    for m in summary["metrics"]:
        lines.append(f"| {m['no']} | {m['name']} | {m['value']} | {m['threshold']} | {STATUS[m['status']]} | {_cell(m['detail'], 160)} |")
    failed = [m for m in summary["metrics"] if m["status"] == "fail"]
    lines += ["", f"Порогов не выполнено: **{len(failed)}**" + (f" ({', '.join('№' + str(m['no']) for m in failed)})." if failed else "."), ""]

    lines += [
        "## Сравнение с поиском по ключевым словам", "",
        "Базовая линия: слова вопроса ищутся в названии и описании, условия (дата, цена, возраст) не понимаются. Подробности: `eval/baseline.py`.", "",
        "| Показатель | Агент | Поиск по словам |", "|---|---|---|",
        f"| Подборки без замечаний | {base['agent_answered_ok']} из {base['searchy']} | {base['answered_ok']} из {base['searchy']} |",
        f"| Запросы с тремя и более условиями выполнены | {base['agent_multi_ok']} из {base['multi']} | {base['multi_ok']} из {base['multi']} |",
        f"| Пустая выдача при наличии подходящих событий | {base['agent_empty']} | {base['empty']} |",
        f"| Запросы без данных: пустой ответ | — (агент отвечает текстом) | {base['nodata_empty']} из {base['nodata']} |", "",
    ]

    if summary["stability"]:
        lines += ["## Устойчивость", "", "| Сценарий | Запросов | Ответов 200 | Ответов 5xx |", "|---|---|---|---|"]
        names = {"model_off": "модель отключена (режим правил), `POST /api/v1/ask`", "model_failing": "модель включена, но не отвечает: запасной режим"}
        for key, v in summary["stability"].items():
            lines.append(f"| {names.get(key, key)} | {v['requests']} | {v['ok']} | {v['server_errors']} |")
        lines.append("")

    if summary["flags"]:
        lines += ["## Что отмечено в трассах", "", ", ".join(f"`{k}`: {v}" for k, v in sorted(summary["flags"].items())), ""]

    bad = [o for o in summary["outcomes"] if not o["ok"]]
    lines += ["## Запросы с замечаниями", ""]
    if not bad:
        lines += ["Нет.", ""]
    for o in bad:
        lines += [f"**{o['id']}** ({KIND_NAMES[o['kind']]}): «{_cell(' → '.join(o['turns']), 160)}»", ""]
        lines += [f"- {f}" for f in o["failures"]]
        lines += [f"- ответ: {_cell(o['reply'], 300)}", ""]

    lines += ["## Все запросы", "", "| № | Тип | Запрос | Показано | Поиск по словам | Итог |", "|---|---|---|---|---|---|"]
    for o in summary["outcomes"]:
        shown = len(o["events"])
        lines.append(f"| {o['id']} | {KIND_NAMES[o['kind']]} | {_cell(' → '.join(o['turns']), 70)} | {shown} | {o['baseline_events']} | {'✅' if o['ok'] else '❌'} |")

    lines += ["", "## Как читать результат", ""]
    if mode == "rules":
        lines += [
            "- Это прогон запасного режима: вопрос разбирают правила, модель не вызывается. Он проверяет базу, фильтры, честность ответов и защиту, "
            "но не заменяет прогон с моделью: пороги уровня 3 относятся к ответам агента на модели.",
            "- Задержка и стоимость в этом режиме не показательны (нет сети и токенов).",
        ]
    else:
        lines += [
            "- Прогон с живой моделью: задержка включает сеть, стоимость считается по тарифу из `LLM_PRICE_*`.",
            "- Ответы модели недетерминированы: для сравнения версий промпта повторяйте прогон 2–3 раза и смотрите худший.",
        ]
    lines += [
        "- ТЗ требует считать метрики на собственном наборе и на 10 внешних запросах, которых нет в наборе, и брать худшее значение. "
        "Внешние запросы подаются тем же скриптом: `python -m eval.run --queries путь/к/файлу.json`.",
        "- Фактическая точность считает числа, время и названия в «ёлочках» против карточек из выдачи. Смысловые утверждения "
        "(«рядом с метро», «подойдёт семье») автоматически не проверяются: их оценивает человек на 2–3 живых запросах.",
        "",
    ]
    return "\n".join(lines)


def _tariff(tariff: dict, mode: str) -> str:
    if mode == "rules":
        return "не применяется"
    if tariff["input"] is None or tariff["output"] is None:
        return "не задан (LLM_PRICE_*): стоимость не рассчитана, токены учтены"
    return f"{tariff['input']} ₽ за 1 млн входных, {tariff['output']} ₽ за 1 млн выходных токенов"


def write_report(summary: dict, out_dir: Path, queries_name: str, default: bool = True) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary["date"][:10]
    stem = f"{stamp}-{summary['mode']}" + ("" if default else "-" + Path(queries_name).stem)
    md_path, json_path = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    md_path.write_text(render(summary, queries_name), encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
