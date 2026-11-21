"""核心业务：用电补报/更正、因子维护、预览快照、确认发布。

预览快照（payload）只包含「该省该月」的发布输入：
  - 当期逐表用电明细（表号、千瓦时）
  - 所用因子的来源（省级/全国）与数值
确认发布时重算当前 payload 与预览快照逐字节比对：
  - 不一致 -> 必须重新预览（哪怕排放总量相同）
  - 其他省份/月份/年份的数据变动不影响本快照，无需重新预览
"""
import json
import re
from datetime import datetime
from sqlite3 import Connection

from fastapi import HTTPException

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EMISSIONS_DIGITS = 6  # kg CO2 保留小数位，保证快照可稳定比对
INDEX_BATCH_SIZE = 50  # 每批补建的期数，可在页面/接口调整

# 索引建齐后列表只读「每省月一行的最新发布索引」，不再扫描历次发布。
LIST_INDEXED_SQL = (
    "SELECT province, month, publication_id AS id, preview_id,"
    " total_kwh, total_emissions, published_at"
    " FROM published_latest ORDER BY month DESC, province"
)

# 补建未完成时列表仍按发布号取每期最新：单条语句、单一读取快照，保证各期版本一致。
LIST_FALLBACK_SQL = (
    "SELECT p.province, p.month, p.id, p.preview_id, p.total_kwh,"
    " p.total_emissions, p.published_at"
    " FROM publications p"
    " JOIN (SELECT province, month, MAX(id) AS max_id FROM publications"
    "       GROUP BY province, month) t ON p.id = t.max_id"
    " ORDER BY p.month DESC, p.province"
)

INDEX_UPSERT_SQL = (
    "INSERT INTO published_latest"
    "(province, month, publication_id, preview_id, total_kwh, total_emissions, published_at, indexed_at)"
    " VALUES (?,?,?,?,?,?,?,?)"
    " ON CONFLICT(province, month) DO UPDATE SET"
    "   publication_id=excluded.publication_id, preview_id=excluded.preview_id,"
    "   total_kwh=excluded.total_kwh, total_emissions=excluded.total_emissions,"
    "   published_at=excluded.published_at, indexed_at=excluded.indexed_at"
    " WHERE excluded.publication_id > published_latest.publication_id"
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_month(month: str) -> str:
    if not MONTH_RE.match(month or ""):
        raise HTTPException(422, f"月份格式应为 YYYY-MM：{month!r}")
    return month


def year_of(month: str) -> int:
    return int(month[:4])


# ---------- 用电明细 ----------

def upsert_usage(conn: Connection, rows: list[dict]) -> dict:
    """按 (省份, 月份, 表号) 覆盖写入：新键为补报，已有键为更正。"""
    inserted, updated = 0, 0
    ts = now_iso()
    with conn:
        for r in rows:
            cur = conn.execute(
                "UPDATE usage_records SET kwh=?, updated_at=? "
                "WHERE province=? AND month=? AND meter_no=?",
                (r["kwh"], ts, r["province"], r["month"], r["meter_no"]),
            )
            if cur.rowcount:
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO usage_records(province, month, meter_no, kwh, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (r["province"], r["month"], r["meter_no"], r["kwh"], ts),
                )
                inserted += 1
    return {"inserted": inserted, "updated": updated}


def list_usage(conn: Connection, province: str | None, month: str | None) -> list[dict]:
    sql = "SELECT province, month, meter_no, kwh, updated_at FROM usage_records"
    cond, args = [], []
    if province:
        cond.append("province=?"); args.append(province)
    if month:
        cond.append("month=?"); args.append(month)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY province, month, meter_no"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------- 排放因子 ----------

def upsert_factor(conn: Connection, scope: str, province: str, year: int, factor: float) -> dict:
    if scope not in ("province", "national"):
        raise HTTPException(422, "scope 须为 province 或 national")
    if scope == "province" and not province:
        raise HTTPException(422, "省级因子必须指定省份")
    if scope == "national":
        province = ""
    if not (2000 <= year <= 2100):
        raise HTTPException(422, "年份超出合理范围")
    if factor <= 0:
        raise HTTPException(422, "排放因子必须为正数")
    with conn:
        conn.execute(
            "INSERT INTO emission_factors(scope, province, year, factor, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(scope, province, year) DO UPDATE SET factor=excluded.factor, updated_at=excluded.updated_at",
            (scope, province, year, factor, now_iso()),
        )
    return {"scope": scope, "province": province, "year": year, "factor": factor}


def list_factors(conn: Connection, year: int | None = None) -> list[dict]:
    sql = "SELECT scope, province, year, factor, updated_at FROM emission_factors"
    args: list = []
    if year:
        sql += " WHERE year=?"; args.append(year)
    sql += " ORDER BY year, scope, province"
    return [dict(r) for r in conn.execute(sql, args)]


def delete_factor(conn: Connection, scope: str, province: str, year: int) -> bool:
    if scope == "national":
        province = ""
    with conn:
        cur = conn.execute(
            "DELETE FROM emission_factors WHERE scope=? AND province=? AND year=?",
            (scope, province, year),
        )
    return cur.rowcount > 0


def resolve_factor(conn: Connection, province: str, year: int) -> dict | None:
    """优先同年省级因子，其次同年全国因子；都缺返回 None。"""
    row = conn.execute(
        "SELECT factor FROM emission_factors WHERE scope='province' AND province=? AND year=?",
        (province, year),
    ).fetchone()
    if row:
        return {"source": "province", "value": row["factor"], "year": year}
    row = conn.execute(
        "SELECT factor FROM emission_factors WHERE scope='national' AND year=?",
        (year,),
    ).fetchone()
    if row:
        return {"source": "national", "value": row["factor"], "year": year}
    return None


# ---------- 预览与发布 ----------

def compute_payload(conn: Connection, province: str, month: str) -> dict:
    """计算当期发布输入的规范快照。因子缺失时逐表因子/排放为 null，publishable=False。"""
    validate_month(month)
    year = year_of(month)
    factor = resolve_factor(conn, province, year)
    usage = conn.execute(
        "SELECT meter_no, kwh FROM usage_records WHERE province=? AND month=? ORDER BY meter_no",
        (province, month),
    ).fetchall()
    details = []
    total_kwh = 0.0
    total_emissions = 0.0
    for u in usage:
        kwh = float(u["kwh"])
        emissions = round(kwh * factor["value"], EMISSIONS_DIGITS) if factor else None
        total_kwh += kwh
        total_emissions += emissions or 0.0
        details.append({
            "meter_no": u["meter_no"],
            "kwh": kwh,
            "factor_source": factor["source"] if factor else None,
            "factor_value": factor["value"] if factor else None,
            "factor_year": year if factor else None,
            "emissions": emissions,
        })
    missing = []
    if usage and not factor:
        missing = [
            {"scope": "province", "province": province, "year": year},
            {"scope": "national", "province": "", "year": year},
        ]
    return {
        "province": province,
        "month": month,
        "year": year,
        "details": details,
        "total_kwh": round(total_kwh, EMISSIONS_DIGITS),
        "total_emissions": round(total_emissions, EMISSIONS_DIGITS),
        "missing_factors": missing,
        "publishable": bool(usage) and not missing,
    }


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_preview(conn: Connection, province: str, month: str) -> dict:
    payload = compute_payload(conn, province, month)
    with conn:
        cur = conn.execute(
            "INSERT INTO previews(province, month, payload, publishable, created_at)"
            " VALUES (?,?,?,?,?)",
            (province, month, _canonical(payload), int(payload["publishable"]), now_iso()),
        )
    return {"preview_id": cur.lastrowid, **payload}


def _change_reasons(old: dict, new: dict) -> list[str]:
    reasons = set()
    old_map = {d["meter_no"]: d for d in old["details"]}
    new_map = {d["meter_no"]: d for d in new["details"]}
    if set(old_map) != set(new_map):
        reasons.add("usage")
    for meter in set(old_map) & set(new_map):
        o, n = old_map[meter], new_map[meter]
        if o["kwh"] != n["kwh"]:
            reasons.add("usage")
        if (o["factor_source"], o["factor_value"]) != (n["factor_source"], n["factor_value"]):
            reasons.add("factor")
    if old.get("missing_factors") != new.get("missing_factors"):
        reasons.add("factor")
    return sorted(reasons)


def publish(conn: Connection, preview_id: int) -> dict:
    row = conn.execute("SELECT * FROM previews WHERE id=?", (preview_id,)).fetchone()
    if not row:
        raise HTTPException(404, "预览不存在，请重新预览")
    old_payload = json.loads(row["payload"])
    current = compute_payload(conn, row["province"], row["month"])
    if _canonical(current) != row["payload"]:
        # 当期用电明细或所用因子来源/数值已变化（与总量是否相同无关）
        raise HTTPException(409, detail={
            "error": "预览后数据已变化，须重新预览后再确认",
            "changed": _change_reasons(old_payload, current),
        })
    if not current["publishable"]:
        year = current["year"]
        raise HTTPException(422, detail={
            "error": f"缺少 {current['province']} {year} 年省级及全国排放因子，请先补录，暂缓发布",
            "missing_factors": current["missing_factors"],
        })
    # 单事务写入新发布快照；旧发布记录不被修改，提交前旧数据照常可查
    with conn:
        cur = conn.execute(
            "INSERT INTO publications(province, month, preview_id, total_kwh, total_emissions, published_at)"
            " VALUES (?,?,?,?,?,?)",
            (current["province"], current["month"], preview_id,
             current["total_kwh"], current["total_emissions"], now_iso()),
        )
        pub_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO publication_details"
            "(publication_id, meter_no, kwh, factor_source, factor_value, factor_year, emissions)"
            " VALUES (?,?,?,?,?,?,?)",
            [(pub_id, d["meter_no"], d["kwh"], d["factor_source"],
              d["factor_value"], d["factor_year"], d["emissions"]) for d in current["details"]],
        )
        # 同一事务内推进该期最新发布索引；与发布一起提交或一起回滚。
        # 索引只允许发布号更大者覆盖：补建期间并发改发绝不会被旧版本盖回。
        conn.execute(
            INDEX_UPSERT_SQL,
            (current["province"], current["month"], pub_id, preview_id,
             current["total_kwh"], current["total_emissions"], now_iso(), now_iso()),
        )
    return {"publication_id": pub_id, **current}


def latest_publication(conn: Connection, province: str, month: str) -> dict | None:
    pub = conn.execute(
        "SELECT * FROM publications WHERE province=? AND month=? ORDER BY id DESC LIMIT 1",
        (province, month),
    ).fetchone()
    if not pub:
        return None
    details = conn.execute(
        "SELECT meter_no, kwh, factor_source, factor_value, factor_year, emissions"
        " FROM publication_details WHERE publication_id=? ORDER BY meter_no",
        (pub["id"],),
    ).fetchall()
    return {**dict(pub), "details": [dict(d) for d in details]}


def list_publications(conn: Connection) -> list[dict]:
    """每个 省+月 的最新一条发布。

    索引建齐后只读 published_latest；补建期间退回 publications 的即时聚合。
    两条语句包在同一个只读事务里：各期版本取自同一已提交状态，
    不会出现「部分期次来自索引、部分期次来自旧表」的混合结果。
    """
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT completed FROM published_index_state WHERE id=1"
        ).fetchone()
        sql = LIST_INDEXED_SQL if row and row["completed"] else LIST_FALLBACK_SQL
        rows = [dict(r) for r in conn.execute(sql)]
    except Exception:
        if owned:
            conn.rollback()
        raise
    if owned:
        conn.commit()
    return rows


# ---------- 最新发布索引：分批补建 ----------

def _period_count(conn: Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT province, month FROM publications)"
    ).fetchone()[0]


def index_status(conn: Connection) -> dict:
    state = conn.execute(
        "SELECT cursor_province, cursor_month, done_periods, total_periods, completed, updated_at"
        " FROM published_index_state WHERE id=1"
    ).fetchone()
    if state is None:  # 兜底：状态行缺失时视为未建齐、从头开始
        state = {"cursor_province": "", "cursor_month": "", "done_periods": 0,
                 "total_periods": 0, "completed": 0, "updated_at": ""}
    indexed = conn.execute("SELECT COUNT(*) FROM published_latest").fetchone()[0]
    completed = bool(state["completed"])
    # 开始补建后以首批快照的历史期数为进度分母，补建期间新发布的期不干扰进度判定。
    total = state["total_periods"] or indexed
    if completed:
        # 建齐后仍有新期次发布：进度以当前索引期数为准
        total = max(total, indexed)
    return {
        "completed": completed,
        "list_source": "index" if completed else "live",
        "cursor_province": state["cursor_province"],
        "cursor_month": state["cursor_month"],
        "total_periods": total,
        "backfilled_periods": state["done_periods"],
        "indexed_periods": indexed,
        "remaining_periods": max(0, total - indexed) if completed else max(0, total - state["done_periods"]),
        "batch_size": INDEX_BATCH_SIZE,
        "updated_at": state["updated_at"],
    }


def backfill_index_batch(conn: Connection, batch_size: int = INDEX_BATCH_SIZE) -> dict:
    """补建一批历史发布索引，返回本批处理情况与最新进度。

    每批一个写事务：写完索引行与游标后一起提交；中断时整批回滚，
    重启后从上一个已提交批次的游标继续。游标按 (province, month) 稳定排序。
    首批开始时对历史期数取快照，补建期间新发布的期不干扰进度判定。
    索引写入只允许发布号更大者覆盖，因此补建期间发生的改发不会被旧版本盖回。
    """
    if not (1 <= batch_size <= 1000):
        raise HTTPException(422, "每批期数须在 1 到 1000 之间")

    try:
        # 写锁在批开始时取得，批内不会被其他写者穿插。
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        conn.rollback()
        raise
    try:
        state = conn.execute(
            "SELECT cursor_province, cursor_month, done_periods, total_periods, completed"
            " FROM published_index_state WHERE id=1"
        ).fetchone()
        if state is None:
            conn.execute(
                "INSERT INTO published_index_state"
                " (id, cursor_province, cursor_month, done_periods, total_periods, completed, updated_at)"
                " VALUES (1,'','',0,0,0,'')"
            )
            state = {"cursor_province": "", "cursor_month": "", "done_periods": 0,
                     "total_periods": 0, "completed": 0}
        if state["completed"]:
            conn.commit()
            return {"processed": 0, "completed_now": True, **index_status(conn)}

        total = state["total_periods"]
        if not total:
            total = _period_count(conn)

        periods = conn.execute(
            "SELECT province, month FROM publications"
            " WHERE province > ? OR (province = ? AND month > ?)"
            " GROUP BY province, month"
            " ORDER BY province, month LIMIT ?",
            (state["cursor_province"], state["cursor_province"],
             state["cursor_month"], batch_size),
        ).fetchall()

        if not periods:
            conn.execute(
                "UPDATE published_index_state"
                " SET completed=1, total_periods=?, updated_at=? WHERE id=1",
                (total, now_iso()),
            )
            conn.commit()
            return {"processed": 0, "completed_now": True, **index_status(conn)}

        ts = now_iso()
        for p in periods:
            row = conn.execute(
                "SELECT id, province, month, preview_id, total_kwh, total_emissions, published_at"
                " FROM publications WHERE province=? AND month=? ORDER BY id DESC LIMIT 1",
                (p["province"], p["month"]),
            ).fetchone()
            conn.execute(
                INDEX_UPSERT_SQL,
                (row["province"], row["month"], row["id"], row["preview_id"],
                 row["total_kwh"], row["total_emissions"], row["published_at"], ts),
            )
        last = periods[-1]
        conn.execute(
            "UPDATE published_index_state"
            " SET cursor_province=?, cursor_month=?, done_periods=done_periods+?,"
            "     total_periods=?, updated_at=? WHERE id=1",
            (last["province"], last["month"], len(periods), total, ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"processed": len(periods), "completed_now": False, **index_status(conn)}
