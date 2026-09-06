import time

from fastapi.testclient import TestClient

from bankrag import api
from bankrag.ingest.progress import report


def test_job_progress_and_listing():
    client = TestClient(api.app)

    def work(log):
        log("start")
        report(log, "scan", 1, 3, message="a.md", added=1)
        report(log, "embed", 5, 10)
        report(log, "done", None, None, uploaded=10)
        return {"summary": {"added": 1}, "uploaded": 10}

    job_id = api._start_job("ingest", work, {"category": "credit-card"})
    for _ in range(50):
        j = client.get(f"/jobs/{job_id}").json()
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "done" and j["progress"]["phase"] == "done"
    assert j["progress"]["stats"] == {"added": 1, "uploaded": 10}
    assert j["meta"] == {"category": "credit-card"} and j["finished"] and j["elapsed_ms"] >= 0
    rows = client.get("/jobs?kind=ingest,crawl&limit=5").json()
    assert rows[0]["id"] == job_id and "log" not in rows[0]
    assert client.get("/jobs?kind=crawl").json() == [r for r in client.get("/jobs?kind=crawl").json() if r["kind"] == "crawl"]


def test_job_error_sets_phase():
    client = TestClient(api.app)

    def boom(log):
        report(log, "scan", 0, 1)
        raise RuntimeError("index unreachable")

    job_id = api._start_job("ingest", boom)
    for _ in range(50):
        j = client.get(f"/jobs/{job_id}").json()
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "error" and j["progress"]["phase"] == "error" and "index unreachable" in j["progress"]["message"]


def test_progress_ignores_plain_loggers():
    report(print, "scan", 1, 1)  # no .progress attribute: must not raise
