from datetime import timedelta

from app.config import now_local
from app.seed import seed


def payload(**kw):
    start = (now_local() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    return {"title": "Концерт симфонического оркестра", "start_at": start.isoformat(), "source": "philharmonia.example",
            "venue": "Филармония", "price_min": 300, "price_max": 1200, **kw}


def test_list_events_and_filters(client, session):
    seed(session)
    data = client.get("/api/events", params={"limit": 5}).json()
    assert data["total"] > 5 and len(data["items"]) == 5
    assert data["items"][0]["start_at"] <= data["items"][-1]["start_at"]

    free = client.get("/api/events", params={"free": True, "limit": 100}).json()
    assert free["total"] > 0 and all(e["is_free"] for e in free["items"])

    cheap_kids = client.get("/api/events", params={"kids": True, "price_max": 500, "limit": 100}).json()
    assert all(e["kids_friendly"] and e["price_min"] <= 500 for e in cheap_kids["items"])

    cancelled = client.get("/api/events", params={"status": "cancelled"}).json()
    assert [e["status"] for e in cancelled["items"]] == ["cancelled"]


def test_seed_deduplicates_and_flags_demo_data(client, session):
    seed(session)
    data = client.get("/api/events", params={"q": "лебединое озеро", "limit": 10}).json()
    assert data["total"] == 1
    swan = data["items"][0]
    assert swan["source_count"] == 2 and swan["is_demo"] is True
    assert client.get("/api/meta").json()["demo_data"] is True


def test_validation_errors(client):
    assert client.get("/api/events", params={"when": "someday"}).status_code == 422
    assert client.get("/api/events", params={"date_from": "2026-11-10", "date_to": "2026-11-01"}).status_code == 422
    assert client.get("/api/events", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/events", params={"limit": 1000}).status_code == 422
    assert client.get("/api/events/999999").status_code == 404


def test_ingest_requires_a_key(client, admin_key, monkeypatch):
    assert client.post("/api/events", json=payload()).status_code == 401
    assert client.post("/api/events", json=payload(), headers={"X-API-Key": "wrong"}).status_code == 401
    monkeypatch.setattr("app.config.ADMIN_API_KEY", "")
    assert client.post("/api/events", json=payload(), headers=admin_key).status_code == 503


def test_ingest_merges_the_same_event_from_another_source(client, admin_key):
    created = client.post("/api/events", json=payload(), headers=admin_key).json()
    assert created["outcome"] == "created"
    merged = client.post(
        "/api/events",
        json=payload(title="Концерт симфонического оркестра (билеты)", source="afisha.example", status="sold_out"),
        headers=admin_key,
    ).json()
    assert merged["outcome"] == "merged"
    assert merged["event"]["id"] == created["event"]["id"]
    assert merged["event"]["source_count"] == 2 and merged["event"]["status"] == "sold_out"
    assert client.get("/api/events", params={"q": "оркестра"}).json()["total"] == 1


def test_bulk_ingest_and_status_patch(client, admin_key):
    batch = [payload(title=f"Лекция номер {i}", category="lecture") for i in range(3)] + [payload(title="Лекция номер 0")]
    data = client.post("/api/events/bulk", json=batch, headers=admin_key).json()
    assert (data["created"], data["unchanged"] + data["merged"]) == (3, 1)

    event_id = data["events"][0]["id"]
    patched = client.patch(f"/api/events/{event_id}/status", json={"status": "cancelled"}, headers=admin_key)
    assert patched.status_code == 200 and patched.json()["status"] == "cancelled"
    assert client.patch(f"/api/events/{event_id}/status", json={"status": "boom"}, headers=admin_key).status_code == 422
    assert client.patch("/api/events/9999/status", json={"status": "cancelled"}, headers=admin_key).status_code == 404
    hidden = client.get("/api/events", params={"q": "лекция"}).json()
    assert event_id not in [e["id"] for e in hidden["items"]]


def test_ask_endpoint_in_rules_mode(client, session):
    seed(session)
    data = client.post("/api/ask", json={"message": "бесплатно в Уфе"}).json()
    assert data["mode"] == "rules" and data["events"] and all(e["is_free"] for e in data["events"])
    vague = client.post("/api/ask", json={"message": "что посоветуешь?"}).json()
    assert vague["needs_clarification"] is True and vague["events"] == []
    assert client.post("/api/ask", json={"message": ""}).status_code == 422


def test_ask_is_rate_limited(client, monkeypatch):
    from app.main import ask_limiter

    monkeypatch.setattr(ask_limiter, "limit", 2)
    codes = [client.post("/api/ask", json={"message": "бесплатно"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limit_is_per_visitor_behind_a_proxy(client, monkeypatch):
    from app import config
    from app.main import ask_limiter

    monkeypatch.setattr(ask_limiter, "limit", 1)
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    ask = lambda ip: client.post("/api/ask", json={"message": "бесплатно"}, headers={"X-Forwarded-For": f"9.9.9.9, {ip}"}).status_code
    assert [ask("1.1.1.1"), ask("2.2.2.2"), ask("1.1.1.1")] == [200, 200, 429]  # левая запись подделывается и не учитывается


def test_unknown_category_is_rejected_and_several_are_accepted(client, session):
    seed(session)
    assert client.get("/api/events", params={"category": "theatr"}).status_code == 422
    assert client.get("/api/events", params={"category": "theatre,foo"}).status_code == 422
    both = client.get("/api/events", params={"category": "theatre,concert", "limit": 100}).json()
    assert both["total"] > 0 and {e["category"] for e in both["items"]} == {"theatre", "concert"}
    assert client.get("/api/events", params={"category": "other"}).status_code == 200


def test_manual_status_is_not_erased_by_a_resync_of_one_of_several_sources(client, admin_key):
    created = client.post("/api/events", json=payload(), headers=admin_key).json()["event"]
    client.post("/api/events", json=payload(title="Концерт симфонического оркестра (билеты)", source="afisha.example"),
                headers=admin_key)
    client.patch(f"/api/events/{created['id']}/status", json={"status": "cancelled"}, headers=admin_key)
    again = client.post("/api/events", json=payload(), headers=admin_key).json()["event"]
    assert again["status"] == "cancelled"  # первый источник по-прежнему говорит «состоится», но второй источник отмечен отменой


def test_static_pages_and_widget_are_served(client):
    assert "Афиша культуры" in client.get("/").text
    widget = client.get("/widget.js")
    assert widget.status_code == 200 and "javascript" in widget.headers["content-type"]
    assert client.get("/widget-demo").status_code == 200
    assert client.get("/api/events", headers={"Origin": "https://ministry.example"}).headers["access-control-allow-origin"] == "*"
