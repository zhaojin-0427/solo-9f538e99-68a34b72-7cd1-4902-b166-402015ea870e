"""业务编排：把持久化数据喂给求解器，落库确认结果，生成入仓 CSV。"""
from __future__ import annotations

import csv
import io
import json
import sqlite3

from . import solver as csp
from .db import (
    batch_state_stamp,
    latest_batch_id,
    load_entries,
    load_groups,
    row_candidates,
    row_feasible,
)
from .parsing import ParsedBatch


class InfeasibleError(Exception):
    """当前确认状态与两套合计无共同可行解。"""


class StaleExportError(Exception):
    """打开校对页后批次或确认状态已变化，下载被拒绝。"""


def assert_export_current(
    conn: sqlite3.Connection, batch_id: int, expected_state: int
) -> None:
    """校验下载链接仍与当前库内状态一致，否则抛 StaleExportError。

    - 链接对应批次已不是最新批次：提示刷新校对页；
    - 本批确认状态戳已变化（有新的确认/撤销成功提交）：提示重新核对。
    """
    current_batch = latest_batch_id(conn)
    if current_batch is None:
        raise StaleExportError("当前没有任何批次，请刷新校对页")
    if current_batch != batch_id:
        raise StaleExportError(
            f"批次已更换（链接对应第 {batch_id} 批，当前最新为第 {current_batch} 批），"
            "请刷新校对页后重新下载"
        )
    stamp = batch_state_stamp(conn, batch_id)
    if stamp != expected_state:
        raise StaleExportError(
            "打开校对页后本批有新的确认或撤销，金额可能已变化，"
            "请重新核对当前页面后再下载"
        )


def build_problem(conn: sqlite3.Connection, batch_id: int):
    """从库里组装求解器输入。

    已确认金额的笔，候选域收缩为单值（确认值）；未确认笔保留原候选。
    返回 (domains, memberships, totals, entry_rows)，其中分组 id 布局为
    前半段窗口分组、后半段售票日分组。
    """
    entry_rows = load_entries(conn, batch_id)
    group_rows = load_groups(conn, batch_id)

    window_keys = [r["group_key"] for r in group_rows if r["group_type"] == "window"]
    date_keys = [r["group_key"] for r in group_rows if r["group_type"] == "date"]
    window_index = {k: i for i, k in enumerate(window_keys)}
    date_offset = len(window_keys)
    date_index = {k: i + date_offset for i, k in enumerate(date_keys)}
    totals = [r["total"] for r in group_rows if r["group_type"] == "window"]
    totals += [r["total"] for r in group_rows if r["group_type"] == "date"]

    domains: list[tuple[int, ...]] = []
    memberships: list[tuple[int, int]] = []
    for row in entry_rows:
        confirmed = row["confirmed_amount"]
        if confirmed is None:
            domains.append(row_candidates(row))
        else:
            domains.append((confirmed,))
        memberships.append(
            (window_index[row["window_key"]], date_index[row["sale_date"]])
        )
    return domains, memberships, totals, entry_rows


def recompute_feasible(
    conn: sqlite3.Connection, batch_id: int, *, persist: bool = True
) -> list[tuple[int, ...]]:
    """按当前确认状态重算每笔可行金额，可选择写回 entries.feasible。"""
    domains, memberships, totals, entry_rows = build_problem(conn, batch_id)
    feasible = csp.compute_feasible(domains, memberships, totals)
    if not feasible or any(len(v) == 0 for v in feasible):
        raise InfeasibleError("当前票款候选与两套纸面合计不存在可行解")
    if persist:
        for row, vals in zip(entry_rows, feasible):
            conn.execute(
                "UPDATE entries SET feasible = ? WHERE id = ?",
                (json.dumps(list(vals)), row["id"]),
            )
    return [tuple(v) for v in feasible]


def create_batch(conn: sqlite3.Connection, parsed: ParsedBatch) -> int:
    """整批入库并完成首次可行性计算；无解则抛 InfeasibleError 并不留数据。"""
    cur = conn.execute(
        "INSERT INTO batches (entry_count) VALUES (?)", (len(parsed.entries),)
    )
    batch_id = cur.lastrowid

    window_total_rows = {r.group_key: r.total for r in parsed.window_totals}
    date_total_rows = {r.group_key: r.total for r in parsed.date_totals}
    for key, total in window_total_rows.items():
        conn.execute(
            "INSERT INTO groups (batch_id, group_type, group_key, total) "
            "VALUES (?, 'window', ?, ?)",
            (batch_id, key, total),
        )
    for key, total in date_total_rows.items():
        conn.execute(
            "INSERT INTO groups (batch_id, group_type, group_key, total) "
            "VALUES (?, 'date', ?, ?)",
            (batch_id, key, total),
        )
    for pos, e in enumerate(parsed.entries):
        conn.execute(
            "INSERT INTO entries "
            "(batch_id, position, ticket_no, window_key, sale_date, "
            " candidates, confirmed_amount, feasible) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, '[]')",
            (
                batch_id,
                pos,
                e.ticket_no,
                e.window,
                e.sale_date,
                json.dumps(list(e.candidates)),
            ),
        )
    try:
        recompute_feasible(conn, batch_id)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return batch_id


def confirm_entry(
    conn: sqlite3.Connection, batch_id: int, entry_id: int, amount: int
) -> None:
    """确认单笔金额：与最新已确认金额一起必须仍有解，否则全部回滚。"""
    row = conn.execute(
        "SELECT * FROM entries WHERE id = ? AND batch_id = ?",
        (entry_id, batch_id),
    ).fetchone()
    if row is None:
        raise InfeasibleError("票款记录不存在或不属于当前批次")
    candidates = row_candidates(row)
    if amount not in candidates:
        raise InfeasibleError("所选金额不在该笔票款的候选金额中")

    try:
        conn.execute(
            "UPDATE entries SET confirmed_amount = ? WHERE id = ?",
            (amount, entry_id),
        )
        recompute_feasible(conn, batch_id)
    except Exception:
        conn.rollback()
        raise
    conn.execute(
        "INSERT INTO confirmations (batch_id, entry_id, amount, action) "
        "VALUES (?, ?, ?, 'confirm')",
        (batch_id, entry_id, amount),
    )
    conn.commit()


def revoke_entry(conn: sqlite3.Connection, batch_id: int, entry_id: int) -> None:
    """撤销单笔确认，恢复其候选域并重算其余可行金额。"""
    row = conn.execute(
        "SELECT id, confirmed_amount FROM entries WHERE id = ? AND batch_id = ?",
        (entry_id, batch_id),
    ).fetchone()
    if row is None:
        raise InfeasibleError("票款记录不存在或不属于当前批次")
    if row["confirmed_amount"] is None:
        return
    conn.execute(
        "UPDATE entries SET confirmed_amount = NULL WHERE id = ?", (entry_id,)
    )
    conn.execute(
        "INSERT INTO confirmations (batch_id, entry_id, amount, action) "
        "VALUES (?, ?, ?, 'revoke')",
        (batch_id, entry_id, row["confirmed_amount"]),
    )
    recompute_feasible(conn, batch_id)
    conn.commit()


def all_singletons(conn: sqlite3.Connection, batch_id: int) -> bool:
    """每笔只剩一个可行金额（含已确认笔）时才允许下载。"""
    rows = load_entries(conn, batch_id)
    return bool(rows) and all(len(row_feasible(r)) == 1 for r in rows)


def export_csv(conn: sqlite3.Connection, batch_id: int, expected_state: int) -> str:
    """生成入仓 CSV：原票号、窗口、售票日、确定金额（单位：分）。

    一致性保证（下载内容必须与校对页所见一致）：
    1. 生成前校验链接携带的批次与确认状态戳仍是当前最新；
    2. 生成过程在只读快照事务内完成（WAL 下不阻塞其他复核员提交确认/撤销），
       保证文件内容来自同一状态、每笔只有一个可行金额；
    3. 生成完毕再次校验：期间若换了批或本批有新的确认/撤销提交，
       丢弃已生成内容并抛 StaleExportError 拒绝下载。
    """
    assert_export_current(conn, batch_id, expected_state)
    conn.execute("BEGIN")  # 只读快照：导出期间看到同一批数据
    try:
        # 导出前再实时求解一次，防止库内 feasible 过期
        feasible = recompute_feasible(conn, batch_id, persist=False)
        rows = load_entries(conn, batch_id)
        content = None
        if all(len(v) == 1 for v in feasible):
            buf = io.StringIO()
            buf.write("﻿")
            writer = csv.writer(buf)
            writer.writerow(["票号", "窗口", "售票日", "金额"])
            for row, vals in zip(rows, feasible):
                writer.writerow(
                    [row["ticket_no"], row["window_key"], row["sale_date"], vals[0]]
                )
            content = buf.getvalue()
    finally:
        conn.rollback()  # 结束只读快照（快照内无写入，rollback 即可释放）
    # 生成完毕再校验一次：期间若换了批或本批有新的确认/撤销提交，
    # 丢弃已生成内容并拒绝下载（优先于“多可行金额”提示，原因更准确）
    assert_export_current(conn, batch_id, expected_state)
    if content is None:
        raise InfeasibleError("仍有票款存在多个可行金额，暂不能入仓")
    return content
