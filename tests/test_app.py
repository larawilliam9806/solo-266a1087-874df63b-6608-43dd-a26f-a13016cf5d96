import os
import sqlite3
import tempfile
import threading

import pytest

_tmpdir = tempfile.mkdtemp(prefix="metrodp-")
os.environ["APP_DB"] = os.path.join(_tmpdir, "test.db")

import app as app_module  # noqa: E402

CSV_V1 = "乘客ID,进站,出站\np1,A,X\np2,A,X\np3,B,X\np4,A,Y\np5,C,Z\n"
CSV_V2 = "乘客ID,进站,出站\np1,A,X\np2,B,Y\n"


@pytest.fixture(autouse=True)
def clean_db():
    conn = sqlite3.connect(os.environ["APP_DB"])
    conn.executescript(
        "DELETE FROM queries; DELETE FROM day_state; DELETE FROM passengers;"
        " DELETE FROM withdrawal_log; DELETE FROM withdrawals;")
    conn.close()
    yield


@pytest.fixture
def client():
    return app_module.app.test_client()


def upload(client, date, csv_text):
    return client.post("/upload", data={"date": date, "csv": csv_text})


def query(client, date, ins, outs):
    return client.post("/query", json={"date": date, "in_stations": ins,
                                       "out_stations": outs})


def status(client, date):
    return client.get(f"/api/status?date={date}").get_json()


# ------------------------------------------------------------- 上传与锁定

def test_upload_ok_and_replace_before_lock(client):
    r = upload(client, "2026-11-01", CSV_V1)
    assert r.status_code == 200 and r.get_json()["rows"] == 5
    # 锁定前可整日替换
    r = upload(client, "2026-11-01", CSV_V2)
    assert r.status_code == 200 and r.get_json()["rows"] == 2
    st = status(client, "2026-11-01")
    assert st["rows"] == 2 and not st["locked"]


def test_upload_rejected_after_lock(client):
    upload(client, "2026-11-02", CSV_V1)
    r = query(client, "2026-11-02", ["A"], ["X"])
    assert r.status_code == 200
    r = upload(client, "2026-11-02", CSV_V2)
    assert r.status_code == 409
    assert "锁定" in r.get_json()["error"]
    # 源数据未被改动
    assert status(client, "2026-11-02")["rows"] == 5


def test_duplicate_passenger_rejected(client):
    r = upload(client, "2026-11-03", "乘客ID,进站,出站\np1,A,X\np1,B,Y\n")
    assert r.status_code == 400
    assert "重复" in r.get_json()["error"]


def test_csv_without_header_and_english_header(client):
    r = upload(client, "2026-11-04", "p1,A,X\np2,B,Y\n")
    assert r.status_code == 200 and r.get_json()["rows"] == 2
    r = upload(client, "2026-11-05", "passenger_id,in_station,out_station\np1,A,X\n")
    assert r.status_code == 200 and r.get_json()["rows"] == 1


# ------------------------------------------------------------- 查询与缓存

def test_query_returns_noisy_only(client):
    upload(client, "2026-11-06", CSV_V1)
    r = query(client, "2026-11-06", ["A"], ["X"])
    body = r.get_json()
    assert r.status_code == 200 and body["ok"]
    assert isinstance(body["noisy_count"], float)
    assert "true_count" not in body and "count" not in body
    assert status(client, "2026-11-06")["budget_spent"] == 0.5
    assert status(client, "2026-11-06")["locked"]


def test_same_query_reused_regardless_of_order_and_duplicates(client):
    upload(client, "2026-11-07", CSV_V1)
    r1 = query(client, "2026-11-07", ["A", "B"], ["X", "Y"]).get_json()
    r2 = query(client, "2026-11-07", ["B", "A", "A"], ["Y", "X", "X"]).get_json()
    assert r1["noisy_count"] == r2["noisy_count"]
    assert r2["cached"] is True
    # 第二次未扣费
    assert status(client, "2026-11-07")["budget_spent"] == 0.5


def test_budget_limit_four_queries(client):
    upload(client, "2026-11-08", CSV_V1)
    ok, rejected = 0, 0
    for i in range(6):
        r = query(client, "2026-11-08", [f"S{i}"], ["X"])
        if r.status_code == 200:
            ok += 1
        else:
            rejected += 1
            assert r.status_code == 403
            assert "预算不足" in r.get_json()["error"]
    assert ok == 4 and rejected == 2  # 2 / 0.5 = 4
    assert status(client, "2026-11-08")["budget_spent"] == 2.0
    # 预算用完后, 已缓存的查询仍然返回原值
    r = query(client, "2026-11-08", ["S0"], ["X"])
    assert r.status_code == 200 and r.get_json()["cached"] is True


def test_budget_is_per_date(client):
    upload(client, "2026-11-09", CSV_V1)
    upload(client, "2026-11-10", CSV_V1)
    for _ in range(4):
        assert query(client, "2026-11-09", [f"S{_}"], ["X"]).status_code == 200
    # 另一个日期仍有完整预算
    assert query(client, "2026-11-10", ["S0"], ["X"]).status_code == 200


# ------------------------------------------------------------- 并发

def test_concurrent_distinct_queries_never_overspend(client):
    upload(client, "2026-11-11", CSV_V1)
    results = []

    def worker(i):
        c = app_module.app.test_client()
        results.append(query(c, "2026-11-11", [f"T{i}"], ["X"]).status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 4
    assert results.count(403) == 8
    assert status(client, "2026-11-11")["budget_spent"] == 2.0


def test_concurrent_identical_query_single_result(client):
    upload(client, "2026-11-12", CSV_V1)
    bodies = []

    def worker():
        c = app_module.app.test_client()
        bodies.append(query(c, "2026-11-12", ["A", "B"], ["X"]).get_json())

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b["ok"] for b in bodies)
    assert len({b["noisy_count"] for b in bodies}) == 1  # 只有一份结果
    assert status(client, "2026-11-12")["budget_spent"] == 0.5  # 只扣一次
    conn = sqlite3.connect(os.environ["APP_DB"])
    n = conn.execute("SELECT COUNT(*) FROM queries WHERE date='2026-11-12'").fetchone()[0]
    conn.close()
    assert n == 1


# ------------------------------------------------------------- 持久化

def test_restart_keeps_results_and_budget(client):
    upload(client, "2026-11-13", CSV_V1)
    r1 = query(client, "2026-11-13", ["A"], ["X"]).get_json()
    # 模拟重启: 重新初始化数据库连接(文件不变)
    app_module.init_db()
    client2 = app_module.app.test_client()
    r2 = query(client2, "2026-11-13", ["A"], ["X"]).get_json()
    assert r2["cached"] is True and r2["noisy_count"] == r1["noisy_count"]
    assert status(client2, "2026-11-13")["budget_spent"] == 0.5
    # 重启后上传仍被拒绝
    assert upload(client2, "2026-11-13", CSV_V2).status_code == 409


# ------------------------------------------------------------- 授权撤回

def withdraw(client, pid):
    return client.post("/withdraw", json={"passenger_id": pid})


def test_withdraw_removes_rows_across_all_dates_and_registers(client):
    # p1 同时出现在两个日期
    upload(client, "2026-11-20", "乘客ID,进站,出站\np1,A,X\np2,A,X\n")
    upload(client, "2026-11-21", "乘客ID,进站,出站\np1,B,Y\np3,C,Z\n")
    r = withdraw(client, "p1")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and not body["already_withdrawn"]
    assert body["total_deleted_rows"] == 2
    dates = {a["date"]: a["deleted_rows"] for a in body["affected_dates"]}
    assert dates == {"2026-11-20": 1, "2026-11-21": 1}
    # 两个日期的源明细都已移除, 其他人保留
    assert status(client, "2026-11-20")["rows"] == 1
    assert status(client, "2026-11-21")["rows"] == 1
    # 登记可回查
    listing = client.get("/api/withdrawals").get_json()["withdrawals"]
    assert {w["passenger_id"] for w in listing} == {"p1"}
    assert listing[0]["total_deleted_rows"] == 2


def test_withdraw_on_locked_date_keeps_history_budget_and_lock(client):
    upload(client, "2026-11-22", CSV_V1)  # 5 行, A/X 命中 p1,p2
    before = query(client, "2026-11-22", ["A"], ["X"]).get_json()
    st_before = status(client, "2026-11-22")
    assert st_before["locked"] and st_before["budget_spent"] == 0.5

    r = withdraw(client, "p1")
    assert r.status_code == 200
    assert r.get_json()["total_deleted_rows"] == 1

    st = status(client, "2026-11-22")
    assert st["rows"] == 4                      # 源明细已删除
    assert st["locked"] is True                 # 锁定状态不变
    assert st["budget_spent"] == 0.5            # 预算不变

    # 历史加噪结果按原值回查(走缓存, 不扣费)
    cached = query(client, "2026-11-22", ["A"], ["X"]).get_json()
    assert cached["cached"] is True
    assert cached["noisy_count"] == before["noisy_count"]
    assert status(client, "2026-11-22")["budget_spent"] == 0.5


def test_new_query_after_withdraw_counts_remaining_rows_and_charges(client):
    upload(client, "2026-11-23", CSV_V1)  # A/X 真实计数 = 2 (p1,p2)
    withdraw(client, "p1")
    # 未发布过的条件: 按剩余明细计数。A/X 只剩 p2 -> 真实计数 1, 噪声仍为 Laplace(0,2)
    r = query(client, "2026-11-23", ["A"], ["X"]).get_json()
    assert r["cached"] is False
    assert abs(r["noisy_count"] - 1.0) < 20.0
    assert status(client, "2026-11-23")["budget_spent"] == 0.5  # 照常扣费


def test_repeat_withdraw_is_idempotent(client):
    upload(client, "2026-11-24", "乘客ID,进站,出站\np1,A,X\np2,A,X\n")
    first = withdraw(client, "p1").get_json()
    assert first["total_deleted_rows"] == 1
    second = withdraw(client, "p1").get_json()
    assert second["already_withdrawn"] is True
    assert second["total_deleted_rows"] == 1
    assert second["affected_dates"] == first["affected_dates"]
    # 数据没有再变化
    assert status(client, "2026-11-24")["rows"] == 1
    logs = client.get("/api/withdrawals?passenger_id=p1").get_json()["withdrawals"]
    assert len(logs) == 1  # 只有一条登记


def test_upload_batch_containing_withdrawn_id_rejected_wholly(client):
    upload(client, "2026-11-25", "乘客ID,进站,出站\np1,A,X\np2,A,X\n")
    withdraw(client, "p1")
    # 整日 CSV 含已撤回 ID: 整批拒绝, 原数据不变
    r = upload(client, "2026-11-25",
               "乘客ID,进站,出站\np1,B,Y\np9,B,Y\n")
    assert r.status_code == 422
    assert "p1" in r.get_json()["error"]
    assert status(client, "2026-11-25")["rows"] == 1  # 只剩 p2, 未被替换
    # 换一个新日期同样整批拒绝
    r2 = upload(client, "2026-11-26",
                "乘客ID,进站,出站\np1,A,X\np3,A,X\n")
    assert r2.status_code == 422
    assert status(client, "2026-11-26")["rows"] == 0
    # 不含撤回 ID 的批次照常上传
    ok = upload(client, "2026-11-25",
                "乘客ID,进站,出站\np2,A,X\np3,C,Z\n")
    assert ok.status_code == 200
    assert status(client, "2026-11-25")["rows"] == 2


def test_withdraw_unknown_passenger_is_registered_without_deletion(client):
    r = withdraw(client, "ghost")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"]
    assert body["total_deleted_rows"] == 0 and body["affected_dates"] == []
    # 登记仍然生效: 之后上传 ghost 也被整批拒绝
    r2 = upload(client, "2026-11-27", "乘客ID,进站,出站\nghost,A,X\n")
    assert r2.status_code == 422


def test_withdraw_requires_passenger_id(client):
    r = client.post("/withdraw", json={"passenger_id": "  "})
    assert r.status_code == 400


def test_withdraw_is_atomic_across_all_dates(client, monkeypatch):
    upload(client, "2026-11-28", "乘客ID,进站,出站\np1,A,X\n")
    upload(client, "2026-11-29", "乘客ID,进站,出站\np1,B,Y\n")
    db_path = os.environ["APP_DB"]

    # 令提交阶段抛错, 模拟失败/重启: 事务必须整体回滚
    def boom():
        raise sqlite3.OperationalError("simulated crash at commit")
    monkeypatch.setattr(app_module, "commit_failure_hook", boom)
    r = client.post("/withdraw", json={"passenger_id": "p1"})
    assert r.status_code == 500
    monkeypatch.setattr(app_module, "commit_failure_hook", None)
    # 不存在部分日期已移除: 两个日期的源明细都还在, 也没有撤回登记
    check = sqlite3.connect(db_path)
    assert check.execute(
        "SELECT COUNT(*) FROM passengers WHERE passenger_id='p1'").fetchone()[0] == 2
    assert check.execute(
        "SELECT COUNT(*) FROM withdrawals").fetchone()[0] == 0
    assert check.execute(
        "SELECT COUNT(*) FROM withdrawal_log").fetchone()[0] == 0
    check.close()
    # 回滚后可正常重新撤回
    assert withdraw(client, "p1").status_code == 200
    assert status(client, "2026-11-28")["rows"] == 0


def test_concurrent_withdraw_and_query_leave_consistent_state(client):
    upload(client, "2026-11-30", CSV_V1)
    errors = []

    def do_withdraw():
        c = app_module.app.test_client()
        try:
            r = c.post("/withdraw", json={"passenger_id": "p1"})
            if r.status_code != 200:
                errors.append(("withdraw", r.status_code))
        except Exception as exc:  # 串行事务下不应出现未捕获异常
            errors.append(("withdraw", repr(exc)))

    def do_query(i):
        c = app_module.app.test_client()
        try:
            r = query(c, "2026-11-30", [f"W{i}"], ["X"])
            if r.status_code not in (200, 403):
                errors.append(("query", r.status_code))
        except Exception as exc:
            errors.append(("query", repr(exc)))

    threads = [threading.Thread(target=do_withdraw)]
    threads += [threading.Thread(target=do_query, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # 最终状态唯一且完整: p1 已从全部(唯一)日期移除, 撤回恰好登记一次
    assert status(client, "2026-11-30")["rows"] == 4
    listing = client.get("/api/withdrawals?passenger_id=p1").get_json()["withdrawals"]
    assert len(listing) == 1 and listing[0]["total_deleted_rows"] == 1
    # 并发成功请求各自完整: 预算只可能是 0.5 的整数倍且不超额
    spent = status(client, "2026-11-30")["budget_spent"]
    assert spent <= 2.0 + 1e-9
    assert abs((spent / 0.5) - round(spent / 0.5)) < 1e-9


# ------------------------------------------------------------- 噪声分布

def test_laplace_noise_distribution():
    samples = [app_module.laplace_noise() for _ in range(20000)]
    mean = sum(samples) / len(samples)
    var = sum((s - mean) ** 2 for s in samples) / len(samples)
    assert abs(mean) < 0.1            # 均值 ≈ 0
    assert abs(var - 8.0) < 0.8       # Laplace(0,2) 方差 = 2 * 2^2 = 8
