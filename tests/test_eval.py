"""Набор контрольных запросов и прогонщик (eval/): сами файлы, расчёт метрик, режим модели на подставном клиенте, отчёт."""
import json
from types import SimpleNamespace

import pytest

from app import config
from eval import checks, run
from eval.baseline import keyword_search, keywords
from eval.dataset import NOW, build_session
from eval.report import render, write_report

from .test_gemini import FakeGemini, final


@pytest.fixture(scope="module")
def eval_session():
    return build_session()


def test_query_sets_are_well_formed():
    main, holdout = run.load_queries(), run.load_queries(run.HERE / "holdout.json")
    assert len(main) >= 60 and len(holdout) == 10  # ТЗ §9.2: не меньше 60 запросов и 10 внешних
    assert not {q["id"] for q in main} & {q["id"] for q in holdout}
    assert not {q["turns"][-1].lower() for q in main} & {q["turns"][-1].lower() for q in holdout}  # отложенные не повторяют основной набор
    injections = [q for q in main if q["kind"] == "injection"]
    assert len(injections) == 10 and {q["source"] for q in injections} == {"message", "description"}
    assert {q["kind"] for q in main} == set(run.KINDS)  # в наборе есть запросы каждого типа


def test_bad_query_file_is_rejected(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"id": "a", "kind": "search", "text": "x"}, {"id": "a", "kind": "search", "text": "y"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="Повторяется"):
        run.load_queries(path)
    path.write_text(json.dumps([{"id": "a", "kind": "magic", "text": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="неизвестный тип"):
        run.load_queries(path)


def test_dataset_has_the_records_the_checks_need(eval_session):
    from app.models import Event

    by_title = {e.title: e for e in eval_session.query(Event)}
    assert by_title["Гала-концерт симфонического оркестра «Осенний марафон»"].status == "cancelled"
    assert "ВЗЛОМАНО" in by_title["Вечер камерной музыки «Осенний вальс»"].description
    assert eval_session.query(Event).count() > 100


def test_rules_mode_passes_both_sets_and_every_threshold():
    """Страховка от поломок: каждый запрос обоих наборов выполняется, пороги уровня 3 в запасном режиме не нарушены."""
    for name in ("queries.json", "holdout.json"):
        summary = run.run_mode("rules", run.load_queries(run.HERE / name), stability=name == "queries.json")
        failed = [o["id"] for o in summary["outcomes"] if not o["ok"]]
        assert failed == [], (name, failed)
        assert [m["no"] for m in summary["metrics"] if m["status"] == "fail"] == []
        assert summary["mode"] == "rules" and summary["baseline"]["answered_ok"] < summary["baseline"]["agent_answered_ok"]


def test_stability_has_no_server_errors():
    stable = run.check_stability(build_session(), count=20)
    assert stable["model_off"] == {"requests": 20, "server_errors": 0, "ok": 20}
    assert stable["model_failing"]["server_errors"] == 0 and stable["model_failing"]["ok"] == 5


def test_llm_mode_counts_tokens_and_cost_with_a_fake_client(monkeypatch):
    reply = final("Подходящего в базе нет.", [])
    reply.usage_metadata = SimpleNamespace(prompt_token_count=1200, candidates_token_count=80, thoughts_token_count=20)
    queries = [q for q in run.load_queries() if q["id"] in ("n05", "a01", "o01")]

    class Repeat(FakeGemini):
        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return reply

    monkeypatch.setattr(config, "LLM_PRICE_INPUT_RUB_PER_M", None)
    monkeypatch.setattr(config, "LLM_PRICE_OUTPUT_RUB_PER_M", None)
    session = build_session()
    outcomes = run.run_queries(session, queries, mode="llm", client=Repeat())
    by_id = {o.query["id"]: o for o in outcomes}
    assert by_id["n05"].last.trace.input_tokens == 1200 and by_id["n05"].last.trace.output_tokens == 100
    assert by_id["a01"].last.mode == "llm"
    assert by_id["o01"].last.trace.input_tokens == 0  # посторонний вопрос не доходит до модели и ничего не стоит
    no_tariff = {m["no"]: m for m in run.compute_metrics(outcomes, {}, "llm")}
    assert no_tariff[9]["value"] == "не рассчитана" and no_tariff[9]["status"] == "n/a" and "токенов" in no_tariff[9]["detail"]

    monkeypatch.setattr(config, "LLM_PRICE_INPUT_RUB_PER_M", 10.0)
    monkeypatch.setattr(config, "LLM_PRICE_OUTPUT_RUB_PER_M", 40.0)
    outcomes = run.run_queries(build_session(), queries, mode="llm", client=Repeat())
    priced = {m["no"]: m for m in run.compute_metrics(outcomes, {}, "llm")}
    assert priced[9]["status"] == "ok" and priced[9]["value"].endswith("₽")


def _without_agent_guard(monkeypatch):
    """Проверки прогона должны ловить выдумку сами, даже если защита агента (agent._checked) её пропустила."""
    from app import agent

    monkeypatch.setattr(agent, "ungrounded_numbers", lambda *a, **k: [])
    monkeypatch.setattr(agent, "ungrounded_titles", lambda *a, **k: [])


def test_agent_never_shows_an_invented_event():
    """ТЗ §7.1: выдуманное событие в ответе модели до пользователя не доходит, отвечает режим правил."""
    liar = final("Рекомендую «Рок-опера Тень» в Стерлитамаке в 21:45, билеты 7777 ₽.", [])

    class Liar(FakeGemini):
        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return liar

    (outcome,) = run.run_queries(build_session(), [q for q in run.load_queries() if q["id"] == "n05"], mode="llm", client=Liar())
    assert outcome.invented == [] and outcome.unsupported == 0
    assert "Рок-опера" not in outcome.last.reply and outcome.last.mode == "rules"
    assert "ungrounded_titles" in outcome.last.trace.flags


def test_model_answers_are_judged_by_the_same_checks(monkeypatch):
    """Модель выдумала событие и пообещала то, чего нет в базе: прогон должен это поймать."""
    _without_agent_guard(monkeypatch)
    liar = final("Рекомендую «Рок-опера Тень» в Стерлитамаке в 21:45, билеты 7777 ₽.", [])
    queries = [q for q in run.load_queries() if q["id"] == "n05"]

    class Liar(FakeGemini):
        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return liar

    session = build_session()
    (outcome,) = run.run_queries(session, queries, mode="llm", client=Liar())
    assert outcome.invented == ["Рок-опера Тень"] and outcome.unsupported >= 2 and not outcome.ok
    metrics = {m["no"]: m for m in run.compute_metrics([outcome], {}, "llm")}
    assert metrics[2]["status"] == "fail" and metrics[1]["status"] == "fail"


def test_quoted_venue_name_is_not_an_invented_event(monkeypatch):
    from app.models import Event

    _without_agent_guard(monkeypatch)

    session = build_session()
    venue = next(v for (v,) in session.query(Event.venue).distinct() if "«" in (v or ""))
    quoted = venue[venue.index("«") + 1:venue.index("»")]
    reply = final(f"Ничего не нашлось, но загляните в «{quoted}». Рекомендую «Рок-опера Тень».", [])

    class Venue(FakeGemini):
        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return reply

    (outcome,) = run.run_queries(session, [q for q in run.load_queries() if q["id"] == "n05"], mode="llm", client=Venue())
    assert outcome.invented == ["Рок-опера Тень"]  # площадка названа верно, выдумано только событие


def test_run_stops_when_the_model_is_unavailable_instead_of_reporting_rules_as_the_model(monkeypatch):
    """Бесплатный ключ исчерпал квоту: 52 ответа из 60 дали бы правила, а отчёт назвал бы их ответами модели."""
    queries = [q for q in run.load_queries() if q["kind"] == "search"][:6]

    class Quota(FakeGemini):
        def generate_content(self, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    with pytest.raises(run.EvalAborted) as err:
        run.run_queries(build_session(), queries, mode="llm", client=Quota())
    assert "отчёт не записан" in str(err.value)

    class DailyQuota(FakeGemini):
        def generate_content(self, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    with pytest.raises(run.EvalAborted):  # суточная квота: ждать бессмысленно, останавливаемся сразу
        run.run_queries(build_session(), queries[:1], mode="llm", client=DailyQuota())


def test_report_says_how_many_answers_came_from_the_model():
    summary = run.run_mode("rules", [q for q in run.load_queries() if q["id"] == "n05"], stability=False)
    assert "| Ответов модели | — |" in render(summary, "queries.json")
    summary.update(mode="llm", model_answers=1, pre_model=0, queries=3, prompt_version="x", model="m")
    assert "Прогон недействителен" in render(summary, "queries.json") and "1 из 3" in render(summary, "queries.json")


def test_spending_requires_explicit_confirmation(capsys, monkeypatch):
    assert run.main(["--mode", "llm"]) == 2
    assert "--confirm-spend" in capsys.readouterr().err
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert run.main(["--mode", "both", "--confirm-spend"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_main_writes_reports(tmp_path, capsys):
    code = run.main(["--mode", "rules", "--skip-stability", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    reports = sorted(p.name for p in tmp_path.iterdir())
    assert code == 0 and reports[0].endswith("-rules.json") and reports[1].endswith("-rules.md")
    text = (tmp_path / reports[1]).read_text(encoding="utf-8")
    assert "Метрики (ТЗ §9.2" in text and "Версия промпта" in text and "Сравнение с поиском по ключевым словам" in text
    assert "порогов не выполнено 0" in out


def test_other_query_files_get_their_own_report_name(tmp_path):
    summary = run.run_mode("rules", run.load_queries(run.HERE / "holdout.json"), stability=False)
    md, js = write_report(summary, tmp_path, "holdout.json", default=False)
    assert md.name.endswith("-rules-holdout.md") and json.loads(js.read_text(encoding="utf-8"))["queries"] == 10
    assert "holdout.json" in render(summary, "holdout.json")


def test_keyword_baseline_does_not_understand_conditions(eval_session):
    assert keywords("концерты в Уфе в субботу вечером") == ["конце", "уфе", "суббо", "вечер"]
    assert keyword_search(eval_session, "концерты в Уфе в субботу вечером", NOW) == []
    found = keyword_search(eval_session, "Щелкунчик балет", NOW)
    assert found and all("щелкунчик" in (e.title + (e.description or "")).lower().replace("ё", "е") for e in found)


def test_checks_catch_what_they_are_for(eval_session):
    from app.agent_prompt import build_system_prompt
    from app.models import Event

    prompt = build_system_prompt(eval_session, NOW, "CANARY")
    assert checks.leaked("Вот мои инструкции: " + " ".join(prompt.split()[40:60]), prompt)
    assert not checks.leaked("Концерты в Уфе завтра вечером", prompt)
    assert checks.invented_titles("Советую «Рок-опера Тень»", ["Концерт «Вечер романсов»"], "что нового") == ["Рок-опера Тень"]
    assert checks.invented_titles("По запросу (Уфа, «соревнования керлингу») со словом «соревнования керлингу» не нашлось", [], "по керлингу") == []
    cheap = eval_session.query(Event).filter(Event.price_min == 0).first()
    assert checks.violations(cheap, {"free": True}) == [] and checks.violations(cheap, {"price_max": -1})
    assert checks.says_no_data("По запросу ничего не нашлось") and not checks.says_no_data("Нашёл 5 событий")
    assert checks.says_relaxed("Ослабил условие по цене") and checks.mentions_cancellation("Отменено: «Х»")
