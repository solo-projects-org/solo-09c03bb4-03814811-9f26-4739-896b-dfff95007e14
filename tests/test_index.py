"""最新发布索引：分批补建、补建期间改发/查询、批次中断回滚、重启续建、建齐后查询范围。"""
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("APP_DB", path)
    # 自动补建交给用例显式驱动，避免测试与非确定的后台线程竞争。
    monkeypatch.setenv("AUTO_BACKFILL", "0")
    return path


@pytest.fixture()
def client(db_path):
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload(client, text):
    return client.post("/api/usage/upload", files={"file": ("u.csv", text.encode("utf-8"), "text/csv")})


def add_factor(client, scope, year, factor, province=""):
    return client.post("/api/factors", json={
        "scope": scope, "province": province, "year": year, "factor": factor})


def seed_period(client, province, month, kwh, factor=0.5):
    """为某省月建立一份已发布版本，返回 publication_id。"""
    year = int(month[:4])
    add_factor(client, "national", year, factor)
    upload(client, f"省份,月份,表号,千瓦时\n{province},{month},M01,{kwh}\n")
    p = client.post("/api/preview", json={"province": province, "month": month}).json()
    r = client.post("/api/publish", json={"preview_id": p["preview_id"]})
    assert r.status_code == 200, r.text
    return r.json()["publication_id"]


def backfill(client, size):
    r = client.post("/api/published/index/backfill", json={"batch_size": size})
    assert r.status_code == 200, r.text
    return r.json()


def list_rows(client):
    return client.get("/api/published/list").json()["rows"]


def expected_latest():
    """不经列表接口，直接从发布表取每期最大发布号作为基准。"""
    with sqlite3.connect(os.environ["APP_DB"]) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, province, month, total_kwh, total_emissions FROM publications p"
            " WHERE id = (SELECT MAX(id) FROM publications q"
            "             WHERE q.province=p.province AND q.month=p.month)"
            " ORDER BY month DESC, province"
        ).fetchall()
    return [dict(r) for r in rows]


def comparable(rows):
    """列表行含 preview_id/published_at，比较时只取与基准共有的字段。"""
    keys = ("id", "province", "month", "total_kwh", "total_emissions")
    return [{k: r[k] for k in keys} for r in rows]


def index_row(db_path, province, month):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT publication_id, total_kwh FROM published_latest"
            " WHERE province=? AND month=?", (province, month)
        ).fetchone()


def reset_index(client):
    """把库还原为「索引尚未建齐」的旧库状态，用于驱动补建流程。"""
    with sqlite3.connect(os.environ["APP_DB"]) as conn:
        conn.execute("DELETE FROM published_latest")
        conn.execute(
            "UPDATE published_index_state"
            " SET cursor_province='', cursor_month='', done_periods=0,"
            " total_periods=0, completed=0, updated_at='' WHERE id=1"
        )
    assert client.get("/api/published/index/status").json()["completed"] is False


def test_publish_updates_index_in_same_transaction(client, db_path):
    pub = seed_period(client, "江苏", "2026-03", 100)
    # 发布事务同时推进该期索引
    assert index_row(db_path, "江苏", "2026-03") == (pub, 100.0)


def test_list_equals_latest_during_and_after_backfill(client):
    # 先用旧库结构造历史（不经过新发布路径），再走补建
    for i, prov in enumerate(["江苏", "浙江", "北京"]):
        seed_period(client, prov, "2026-03", 100 + i)
        seed_period(client, prov, "2026-04", 200 + i)
        seed_period(client, prov, "2026-03", 300 + i)  # 改发，制造多版本历史
    reset_index(client)

    # 补建中：列表按发布号取各期最新，与基准一致
    first = backfill(client, 2)
    assert first["completed"] is False and first["processed"] == 2
    assert first["list_source"] == "live"
    assert len(list_rows(client)) == 6
    assert comparable(list_rows(client)) == expected_latest()

    # 建齐后：列表只查索引行，结果仍与基准一致
    while not backfill(client, 2)["completed"]:
        pass
    assert backfill(client, 2)["list_source"] == "index"
    assert comparable(list_rows(client)) == expected_latest()


def test_republication_during_backfill_wins(client):
    seed_period(client, "江苏", "2026-03", 100)
    seed_period(client, "浙江", "2026-03", 200)
    seed_period(client, "北京", "2026-03", 300)
    reset_index(client)

    backfill(client, 1)  # 只完成（北京）
    # 补建中途：对已建（北京）、未建（浙江）两期都改发
    bj_new = seed_period(client, "北京", "2026-03", 999)
    zj_new = seed_period(client, "浙江", "2026-03", 888)

    while not backfill(client, 1)["completed"]:
        pass
    rows = {(r["province"], r["month"]): r for r in list_rows(client)}
    assert rows[("北京", "2026-03")]["id"] == bj_new
    assert rows[("北京", "2026-03")]["total_kwh"] == 999
    assert rows[("浙江", "2026-03")]["id"] == zj_new
    assert rows[("浙江", "2026-03")]["total_kwh"] == 888
    assert comparable(list_rows(client)) == expected_latest()


def test_backfill_never_overwrites_newer_publication(db_path):
    """并发改发已把索引推进到更大发布号时，补建旧版本之行不得回退索引。"""
    from app import services
    from app.db import init_db

    # 先建表，再直接构造「索引已是新版」的状态
    init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        pid = conn.execute(
            "INSERT INTO publications(province, month, preview_id, total_kwh, total_emissions, published_at)"
            " VALUES ('江苏','2026-03',1,777,777,'x')"
        ).lastrowid
        conn.execute(
            "INSERT INTO published_latest"
            " VALUES ('江苏','2026-03',?,1,777,777,'x','x')", (pid,))
        conn.commit()

        conn.execute(
            services.INDEX_UPSERT_SQL,
            ("江苏", "2026-03", pid - 1, 1, 100.0, 50.0, "old", "old"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT publication_id, total_kwh FROM published_latest"
            " WHERE province='江苏' AND month='2026-03'"
        ).fetchone()
    assert row == (pid, 777)


def test_interrupted_batch_rolls_back_and_resumes(client, db_path, monkeypatch):
    """批次内写第二期时失败：整批回滚、游标不动；修复后从该批重新开始。"""
    for i, prov in enumerate(["江苏", "浙江", "北京"]):
        seed_period(client, prov, "2026-03", 100 + i)
    reset_index(client)

    from app import services
    real_upsert = services.INDEX_UPSERT_SQL
    state = {"fail_on": 2}

    class FlakyConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, args=()):
            if sql == real_upsert:
                state["fail_on"] -= 1
                if state["fail_on"] == 0:
                    raise RuntimeError("模拟批次中途中断")
            return self._conn.execute(sql, args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(RuntimeError):
            services.backfill_index_batch(FlakyConnection(conn), 2)
        assert conn.execute("SELECT COUNT(*) FROM published_latest").fetchone()[0] == 0
        cur = conn.execute(
            "SELECT cursor_province, cursor_month, completed FROM published_index_state WHERE id=1"
        ).fetchone()
        assert (cur["cursor_province"], cur["cursor_month"], cur["completed"]) == ("", "", 0)

    # 修复后重跑同一批，全部完成
    while not backfill(client, 2)["completed"]:
        pass
    assert comparable(list_rows(client)) == expected_latest()
    assert len(list_rows(client)) == 3


def test_restart_resumes_from_committed_cursor(db_path):
    from app.main import app

    provinces = ["江苏", "浙江", "北京", "山西"]
    with TestClient(app) as c:
        for i, prov in enumerate(provinces):
            seed_period(c, prov, "2026-03", 100 + i)
        reset_index(c)
        assert backfill(c, 2)["processed"] == 2
        status = c.get("/api/published/index/status").json()
        assert status["indexed_periods"] == 2
        assert status["backfilled_periods"] == 2
        assert status["total_periods"] == 4
        cursor = (status["cursor_province"], status["cursor_month"])

    # 重启（新进程，同一数据卷）：从已提交游标继续，不重复、不遗漏
    with TestClient(app) as c:
        status = c.get("/api/published/index/status").json()
        assert status["completed"] is False
        assert status["indexed_periods"] == 2
        assert (status["cursor_province"], status["cursor_month"]) == cursor
        while not backfill(c, 2)["completed"]:
            pass
        status = c.get("/api/published/index/status").json()
        assert status["indexed_periods"] == 4 and status["list_source"] == "index"
        assert comparable(list_rows(c)) == expected_latest()


def test_new_period_after_completion_is_still_listed(client):
    seed_period(client, "江苏", "2026-03", 100)
    reset_index(client)
    while not backfill(client, 10)["completed"]:
        pass
    assert client.get("/api/published/index/status").json()["list_source"] == "index"
    seed_period(client, "浙江", "2026-05", 50)
    rows = list_rows(client)
    assert [r["province"] for r in rows] == ["浙江", "江苏"]
    assert comparable(rows) == expected_latest()


def test_list_query_only_touches_index_when_completed(client):
    """建齐后列表 SQL 不再引用 publications 表。"""
    from app import services
    assert "publications " not in services.LIST_INDEXED_SQL
    assert "MAX(id)" not in services.LIST_INDEXED_SQL
    seed_period(client, "江苏", "2026-03", 100)
    reset_index(client)
    while not backfill(client, 10)["completed"]:
        pass
    with sqlite3.connect(os.environ["APP_DB"]) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + services.LIST_INDEXED_SQL).fetchall()
    assert all("publications" not in str(row) for row in plan)
    assert any("published_latest" in str(row) for row in plan)


def test_failed_publish_leaves_index_and_list_unchanged(client, db_path, monkeypatch):
    """发布失败（索引写入环节出错）时整笔回滚：列表与索引都停留在上一次成功发布。"""
    old = seed_period(client, "江苏", "2026-03", 100)
    reset_index(client)
    while not backfill(client, 10)["completed"]:
        pass
    before = list_rows(client)
    assert before[0]["id"] == old

    # 新预览准备发布，但索引写入失败
    upload(client, "省份,月份,表号,千瓦时\n江苏,2026-03,M01,999\n")
    p = client.post("/api/preview", json={"province": "江苏", "month": "2026-03"}).json()
    from app import services
    monkeypatch.setattr(services, "INDEX_UPSERT_SQL", "INSERT INTO no_such_table VALUES (1)")
    with pytest.raises(sqlite3.OperationalError):
        # TestClient 默认抛出服务端异常；直接驱动服务层更直观
        with sqlite3.connect(os.environ["APP_DB"]) as conn:
            conn.row_factory = sqlite3.Row
            services.publish(conn, p["preview_id"])

    with sqlite3.connect(os.environ["APP_DB"]) as conn:
        pub_count = conn.execute(
            "SELECT COUNT(*) FROM publications WHERE province='江苏' AND month='2026-03'"
        ).fetchone()[0]
    assert pub_count == 1  # 失败的新发布已回滚
    assert list_rows(client) == before
    assert index_row(db_path, "江苏", "2026-03") == (old, 100.0)


def test_list_is_single_consistent_snapshot(client, db_path, monkeypatch):
    """同一次列表查询中，各期版本必须来自同一已提交状态（不混用索引与旧表）。"""
    from app import services
    seed_period(client, "江苏", "2026-03", 100)
    seed_period(client, "浙江", "2026-03", 200)
    reset_index(client)

    # 列表读取期间，另一连接提交了改发并推进索引（模拟并发发布）
    real_list = services.LIST_FALLBACK_SQL
    fired = {"done": False}

    class ConcurrentConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, args=()):
            if sql == real_list and not fired["done"]:
                fired["done"] = True
                with sqlite3.connect(db_path) as other:
                    other.row_factory = sqlite3.Row
                    pid = other.execute(
                        "INSERT INTO publications(province,month,preview_id,total_kwh,total_emissions,published_at)"
                        " VALUES ('江苏','2026-03',1,999,999,'x')").lastrowid
                    other.execute(
                        services.INDEX_UPSERT_SQL, ("江苏","2026-03",pid,1,999.0,999.0,"x","x"))
                    other.execute("UPDATE published_index_state SET completed=1 WHERE id=1")
                    other.commit()
            return self._conn.execute(sql, args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = services.list_publications(ConcurrentConnection(conn))
    jiangsu = [r for r in rows if r["province"] == "江苏"]
    assert len(jiangsu) == 1 and jiangsu[0]["total_kwh"] == 100.0  # 仍取旧快照，不混入新索引
    assert any(r["province"] == "浙江" for r in rows)


def test_invalid_batch_size_rejected(client):
    r = client.post("/api/published/index/backfill", json={"batch_size": 0})
    assert r.status_code == 422
