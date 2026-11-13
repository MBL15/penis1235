"""Форма обратной связи и заявки организаторов (ТЗ §6.3, §10): защита от спама, минимум персональных данных."""
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect

from app import feedback
from app.config import now_local
from app.feedback import FeedbackIn, check_content, check_token, issue_token, purge_old, save_feedback
from app.models import AuditLog, Feedback
from app.observability import metrics

MESSAGE = "На странице концерта указана неверная дата: он пройдёт на неделю позже."


def good_token():
    return issue_token(time.time() - 10)  # форма была открыта 10 секунд назад


def body(**kw):
    return {"kind": "correction", "message": MESSAGE, "token": good_token(), **kw}


@pytest.fixture(autouse=True)
def fresh_metrics():
    metrics.reset()


# ---------- форма и метка времени ----------

def test_form_endpoint_gives_a_signed_token_and_the_list_of_kinds(client):
    response = client.get("/api/v1/feedback/form")
    data = response.json()
    assert response.headers["cache-control"] == "no-store" and data["min_seconds"] == feedback.MIN_SECONDS
    assert {k["slug"] for k in data["kinds"]} == {"organizer", "correction", "question", "other"}
    assert check_token(data["token"], time.time() + 10) is None


def test_token_is_checked_for_age_and_signature():
    now = time.time()
    assert check_token(issue_token(now - 10), now) is None
    assert "быстро" in check_token(issue_token(now - 1), now)  # роботы отправляют форму мгновенно
    assert "давно" in check_token(issue_token(now - feedback.MAX_SECONDS - 1), now)
    for forged in ("", "abc", "123.deadbeef", f"{int(now) - 50}.{'0' * 32}", "..", "-5.x"):
        assert "устарела" in check_token(forged, now), forged
    other_time = issue_token(now - 50).partition(".")[2]
    assert "устарела" in check_token(f"{int(now) - 20}.{other_time}", now)  # подпись от другого времени не подходит


# ---------- приём ----------

def test_a_normal_message_is_saved_without_any_visitor_data(client, session):
    response = client.post("/api/v1/feedback", json=body(contact="@ivan", event_id=None),
                           headers={"X-Forwarded-For": "203.0.113.7", "User-Agent": "TestBrowser/1.0", "Accept-Language": "ru"})
    assert response.status_code == 200 and response.json()["ok"] is True and "Спасибо" in response.json()["message"]
    item = session.query(Feedback).one()
    assert (item.kind, item.message, item.contact, item.status) == ("correction", MESSAGE, "@ivan", "new")
    columns = {c["name"] for c in inspect(session.get_bind()).get_columns("feedback")}
    assert not columns & {"ip", "user_agent", "ip_address", "headers", "name", "email", "phone"}  # персональных данных в таблице нет
    stored = " ".join(str(getattr(item, c)) for c in columns)
    assert "203.0.113.7" not in stored and "TestBrowser" not in stored
    assert 'afisha_feedback_total{result="saved"} 1' in client.get("/metrics").text


def test_contact_is_optional(client, session):
    assert client.post("/api/v1/feedback", json=body()).status_code == 200
    assert session.query(Feedback).one().contact == ""


def test_honeypot_is_accepted_silently_and_nothing_is_stored(client, session):
    response = client.post("/api/v1/feedback", json=body(website="http://spam.example"))
    assert response.status_code == 200 and response.json()["ok"] is True  # робот не должен понять, что его раскрыли
    assert session.query(Feedback).count() == 0
    assert 'afisha_feedback_total{result="honeypot"} 1' in client.get("/metrics").text


@pytest.mark.parametrize("token, text", [
    ("", "устарела"),
    ("12345.abc", "устарела"),
    (lambda: issue_token(time.time() - 1), "быстро"),
    (lambda: issue_token(time.time() - feedback.MAX_SECONDS - 60), "давно"),
])
def test_missing_forged_fast_and_stale_forms_are_rejected(client, session, token, text):
    value = token() if callable(token) else token
    response = client.post("/api/v1/feedback", json=body(token=value))
    assert response.status_code == 422 and text in response.json()["error"]["message"]
    assert session.query(Feedback).count() == 0


def test_too_many_links_and_meaningless_text_are_rejected(client):
    spam = "Смотрите http://a.example и http://b.example, а ещё https://c.example/x пожалуйста"
    assert "не больше 2 ссылок" in client.post("/api/v1/feedback", json=body(message=spam)).json()["error"]["message"]
    assert client.post("/api/v1/feedback", json=body(message="1234567890 !!! ???")).status_code == 422
    assert check_content("Две ссылки http://a.example и www.b.example допустимы") is None
    assert check_content("Привет, это обычное сообщение") is None


def test_field_limits_give_plain_messages(client):
    short = client.post("/api/v1/feedback", json=body(message="коротко")).json()["error"]
    assert short["code"] == "validation_error" and any(d["field"] == "message" and "не меньше 10" in d["message"] for d in short["details"])
    assert client.post("/api/v1/feedback", json=body(message="я" * 2001)).status_code == 422
    assert client.post("/api/v1/feedback", json=body(contact="к" * 201)).status_code == 422
    assert client.post("/api/v1/feedback", json=body(kind="spam")).status_code == 422
    assert client.post("/api/v1/feedback", json=body(event_id=0)).status_code == 422


def test_control_characters_are_stripped(client, session):
    client.post("/api/v1/feedback", json=body(message="Текст\x00 с\x07 управляющими\x1b символами в сообщении"))
    assert session.query(Feedback).one().message == "Текст с управляющими символами в сообщении"


def test_event_reference_must_point_to_a_published_event(client, add, session):
    from app.pipeline import process_record

    visible, _ = add("Концерт симфонического оркестра", now_local() + timedelta(days=3))
    draft = process_record(session, {"title": "Черновик события", "start_at": (now_local() + timedelta(days=3)).isoformat(), "source": "x"},
                           publish=False).event
    assert client.post("/api/v1/feedback", json=body(event_id=visible.id)).status_code == 200
    assert session.query(Feedback).one().event_id == visible.id
    for bad in (999999, draft.id):
        response = client.post("/api/v1/feedback", json=body(event_id=bad, message=MESSAGE + f" {bad}"))
        assert response.status_code == 422 and "Такого события нет" in response.json()["error"]["message"]


def test_the_same_text_is_stored_once_a_day(client, session):
    assert client.post("/api/v1/feedback", json=body()).status_code == 200
    assert client.post("/api/v1/feedback", json=body()).json()["ok"] is True  # человеку отвечаем так же, но второй копии нет
    assert session.query(Feedback).count() == 1
    assert 'afisha_feedback_total{result="duplicate"} 1' in client.get("/metrics").text
    data = FeedbackIn(kind="correction", message=MESSAGE)
    _, again = save_feedback(session, data, now_local() + timedelta(days=2))
    assert again == "saved" and session.query(Feedback).count() == 2


def test_rate_limit_per_visitor_and_overall(client, monkeypatch):
    codes = [client.post("/api/v1/feedback", json=body(message=MESSAGE + f" №{i}")).status_code for i in range(7)]
    assert codes == [200] * 5 + [429, 429]
    response = client.post("/api/v1/feedback", json=body(message=MESSAGE + " ещё"))
    assert response.json()["error"]["code"] == "rate_limited" and response.headers["retry-after"] == "3600"
    from app.feedback import feedback_global_limiter, feedback_limiter

    feedback_limiter.hits.clear()
    feedback_global_limiter.hits.clear()
    monkeypatch.setattr(feedback_global_limiter, "limit", 1)
    assert client.post("/api/v1/feedback", json=body(message=MESSAGE + " общий 1")).status_code == 200
    assert client.post("/api/v1/feedback", json=body(message=MESSAGE + " общий 2")).status_code == 429


def test_other_sites_cannot_post_into_the_form(client):
    response = client.post("/api/v1/feedback", json=body(), headers={"Origin": "https://spam.example"})
    assert response.status_code == 403


# ---------- модератор и срок хранения ----------

def test_moderator_sees_new_messages_and_marks_them_processed(client, admin_key, session):
    client.post("/api/v1/feedback", json=body(contact="@ivan"))
    inbox = client.get("/api/v1/admin/feedback", headers=admin_key).json()
    assert len(inbox) == 1 and inbox[0]["kind_label"] == "Ошибка в афише" and inbox[0]["contact"] == "@ivan"
    done = client.patch(f"/api/v1/admin/feedback/{inbox[0]['id']}", json={"status": "processed"}, headers=admin_key).json()
    assert done["status"] == "processed" and done["processed_by"] == "admin" and done["processed_at"]
    assert client.get("/api/v1/admin/feedback", headers=admin_key).json() == []
    assert len(client.get("/api/v1/admin/feedback", params={"status": "processed"}, headers=admin_key).json()) == 1
    assert len(client.get("/api/v1/admin/feedback", params={"status": "all"}, headers=admin_key).json()) == 1
    assert session.query(AuditLog).filter_by(action="feedback").count() == 1
    client.patch(f"/api/v1/admin/feedback/{inbox[0]['id']}", json={"status": "new"}, headers=admin_key)
    assert client.get("/api/v1/admin/feedback", headers=admin_key).json()[0]["processed_by"] == ""
    assert client.patch("/api/v1/admin/feedback/9999", json={"status": "processed"}, headers=admin_key).status_code == 404
    assert client.patch(f"/api/v1/admin/feedback/{inbox[0]['id']}", json={"status": "x"}, headers=admin_key).status_code == 422
    assert client.get("/api/v1/admin/feedback").status_code == 401


def test_retention_removes_old_messages(session):
    now = now_local()
    old = Feedback(kind="other", message="старое необработанное", created_at=now - timedelta(days=feedback.RETENTION_DAYS + 1))
    processed = Feedback(kind="other", message="обработанное давно", status="processed", created_at=now - timedelta(days=100),
                         processed_at=now - timedelta(days=feedback.RETENTION_PROCESSED_DAYS + 1))
    fresh = Feedback(kind="other", message="свежее", created_at=now - timedelta(days=3))
    recent = Feedback(kind="other", message="обработано недавно", status="processed", created_at=now - timedelta(days=50),
                      processed_at=now - timedelta(days=5))
    session.add_all([old, processed, fresh, recent])
    session.commit()
    assert purge_old(session, now) == 2
    assert {f.message for f in session.query(Feedback)} == {"свежее", "обработано недавно"}


# ---------- страницы ----------

def test_feedback_page_and_event_link_work_together(client, add):
    event, _ = add("Концерт симфонического оркестра", now_local() + timedelta(days=3))
    page = client.get("/feedback", params={"event": event.id})
    assert page.status_code == 200 and "Обратная связь" in page.text and 'id="website"' in page.text and "tabindex=\"-1\"" in page.text
    assert f"/feedback?event={event.id}" in client.get(f"/event/{event.id}").text
    assert "180 дней" in page.text and "не сохраняем ваш адрес" in page.text  # срок хранения и обещание про данные


def test_form_script_uses_the_token_and_the_trap_field():
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "app" / "static" / "feedback.js").read_text(encoding="utf-8")
    for needle in ("/api/v1/feedback/form", "/api/v1/feedback", "website", "token"):
        assert needle in script, needle
