"""
examples/fixed_flow_lineage_check.py — 用 LangGraph 定义"固定流程"的示范

本仓库主线 `trace_agent.py` 用的是 `create_react_agent`：把工具丢给 LLM，
下一步做什么由模型自己决定。本例是它的**对照组**：同样是查 DB + 读源文件
抓 ETL bug，但每一步走哪个节点由我们用 `StateGraph` 写死。

因此本例**不需要 LLM，不需要 API key**，纯确定性执行：

    python3 setup_warehouse.py                       # 先建好仓库（若还没建）
    python3 examples/fixed_flow_lineage_check.py     # 跑全部 4 张源表
    python3 examples/fixed_flow_lineage_check.py customer_b_orders_raw --stream
    python3 examples/fixed_flow_lineage_check.py --mermaid

图结构（fan-out 并行 → join → 条件分支 → 汇合）：

                        ┌──> count_db_rows ──┐
    START ──> lookup_registry                ├──> assess ──?──> investigate_missing_rows ──┐
                        └──> count_file_rows ┘         │                                   │
                                                       ├──?──> compare_amounts ────────────┤
                                                       └──?────────────────────────────────┴──> report ──> END
"""
from __future__ import annotations

import argparse
import csv
import glob
import operator
import os
import sqlite3
import sys
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from setup_warehouse import DB_PATH, TODAY  # noqa: E402

REPORT_DATE = TODAY.isoformat()

# 只有这两张表有 amount 字段，值得做逐笔金额比对。
AMOUNT_TABLES = {"customer_a_orders_raw", "customer_b_orders_raw"}


# --------------------------------------------------------------------------
# 1. State —— 图里所有节点共享的那一份数据
# --------------------------------------------------------------------------
class CheckState(TypedDict, total=False):
    """节点的返回值会被**合并**进 State，而不是替换整个 State。

    默认合并策略是"后写覆盖"，所以并行节点若同时写同一个 key 会报
    InvalidUpdateError。`findings` 用 Annotated[..., operator.add] 声明了
    reducer，两个并行节点各自 append 自己的发现，LangGraph 会把两个列表
    拼起来 —— 这是并行 fan-out 能安全写同一个 key 的唯一办法。
    """

    source_table: str                          # 输入：要体检的原始表
    report_date: str                           # 输入：体检哪一天
    source_uri: str                            # 由 _source_registry 查出
    loader: str
    file_path: str
    db_rows: int
    file_rows: int
    findings: Annotated[list[str], operator.add]
    verdict: str


# --------------------------------------------------------------------------
# 2. Nodes —— 每个节点是一个普通函数：吃 State，吐"要写回 State 的字段"
# --------------------------------------------------------------------------
def lookup_registry(state: CheckState) -> dict:
    """查 `_source_registry`：这张表的物理文件在磁盘哪里、谁加载的。

    对应 ReAct agent 里模型自己决定去 `execute_query` 查元数据表这一步 ——
    区别是这里由我们写死，模型没有跳过它的自由。
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT source_uri, file_dir, loader FROM _source_registry WHERE source_table = ?",
            (state["source_table"],),
        ).fetchone()
    if row is None:
        raise ValueError(f"{state['source_table']} 不在 _source_registry 中")

    source_uri, file_dir, loader = row
    # 文件名形如 <file_dir>/<YYYY-MM-DD>.<ext>，扩展名各源不同（csv/json/log），
    # 用 glob 兜住，不必把格式硬编码进流程。
    matches = glob.glob(os.path.join(file_dir, f"{state['report_date']}.*"))
    if not matches:
        raise FileNotFoundError(f"{file_dir} 下没有 {state['report_date']} 的源文件")

    return {"source_uri": source_uri, "loader": loader, "file_path": matches[0]}


def count_db_rows(state: CheckState) -> dict:
    """DB 侧：加载器**实际写进来**了多少行。"""
    with sqlite3.connect(DB_PATH) as conn:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {state['source_table']} WHERE date(ts) = ?",  # noqa: S608
            (state["report_date"],),
        ).fetchone()[0]
    return {"db_rows": n, "findings": [f"DB `{state['source_table']}` 当天 {n} 行"]}


def count_file_rows(state: CheckState) -> dict:
    """文件侧：上游**本来给了**多少行。CSV 要去掉表头，其余按非空行算。"""
    path = state["file_path"]
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    n = len(lines) - 1 if path.endswith(".csv") else len(lines)
    return {"file_rows": n, "findings": [f"源文件 `{os.path.basename(path)}` {n} 行"]}


def assess(state: CheckState) -> dict:
    """join 节点：两个并行分支都跑完才会执行到这里。"""
    gap = state["file_rows"] - state["db_rows"]
    if gap:
        return {"findings": [f"行数对不上：文件比 DB 多 {gap} 行"]}
    return {"findings": ["行数一致"]}


def investigate_missing_rows(state: CheckState) -> dict:
    """行数对不上 → 把文件里丢失的那些行捞出来，看它们有什么共同点。"""
    with sqlite3.connect(DB_PATH) as conn:
        db_ids = {
            r[0]
            for r in conn.execute(
                f"SELECT order_id FROM {state['source_table']} WHERE date(ts) = ?",  # noqa: S608
                (state["report_date"],),
            )
        }

    with open(state["file_path"], encoding="utf-8") as f:
        missing = [r for r in csv.DictReader(f) if int(r["order_id"]) not in db_ids]

    # 丢掉的行是不是共享某个特征？这里按币种分组 —— 一眼看出加载器的过滤条件。
    by_currency: dict[str, int] = {}
    for r in missing:
        by_currency[r["currency"]] = by_currency.get(r["currency"], 0) + 1

    dropped = ", ".join(f"{c}×{n}" for c, n in sorted(by_currency.items()))
    return {
        "verdict": (
            f"BUG：`{state['loader']}` 静默丢行 —— 文件 {state['file_rows']} 行，"
            f"DB 只有 {state['db_rows']} 行；丢失的 {len(missing)} 行币种分布为 {dropped}"
        ),
        "findings": [f"丢失行按币种分组：{by_currency}"],
    }


def compare_amounts(state: CheckState) -> dict:
    """行数对得上也不代表没 bug —— 逐笔比对金额，抓值被改写的情况。"""
    with sqlite3.connect(DB_PATH) as conn:
        db_amounts = dict(
            conn.execute(
                f"SELECT order_id, amount FROM {state['source_table']} WHERE date(ts) = ?",  # noqa: S608
                (state["report_date"],),
            )
        )

    mismatches: list[tuple[int, float, float]] = []
    with open(state["file_path"], encoding="utf-8") as f:
        for r in csv.DictReader(f):
            oid = int(r["order_id"])
            if oid not in db_amounts:
                continue
            file_amount = float(r["amount"])
            if abs(file_amount - db_amounts[oid]) > 1e-9:
                mismatches.append((oid, file_amount, db_amounts[oid]))

    if not mismatches:
        return {"verdict": "OK：行数与金额均与源文件一致", "findings": ["逐笔金额比对全部一致"]}

    # 每一笔都是"文件有小数、DB 没有"→ 加载器把 float 当 int 用了。
    truncated = all(db == int(fa) for _, fa, db in mismatches)
    sample = "; ".join(f"order {o}: 文件 {fa} → DB {db}" for o, fa, db in mismatches[:3])
    return {
        "verdict": (
            f"BUG：`{state['loader']}` "
            + ("把金额截断成整数" if truncated else "改写了金额")
            + f" —— {len(mismatches)}/{state['file_rows']} 笔不一致"
        ),
        "findings": [f"金额不一致样例：{sample}"],
    }


def report(state: CheckState) -> dict:
    """汇合节点：三条分支最终都落到这里，保证输出格式统一。"""
    return {"verdict": state.get("verdict") or f"OK：`{state['loader']}` 未见异常"}


# --------------------------------------------------------------------------
# 3. Router —— 条件边的判断函数：只返回"下一个节点的名字"，不改 State
# --------------------------------------------------------------------------
def route_after_assess(
    state: CheckState,
) -> Literal["investigate_missing_rows", "compare_amounts", "report"]:
    if state["db_rows"] != state["file_rows"]:
        return "investigate_missing_rows"        # customer_b：80 行进来只剩 5 行
    if state["source_table"] in AMOUNT_TABLES:
        return "compare_amounts"                 # customer_a：行数对得上，但金额被截断
    return "report"                              # 点击流 / 应用日志：对照组，直接收工


# --------------------------------------------------------------------------
# 4. Graph —— 把节点和边连起来，compile() 之后就是个可调用对象
# --------------------------------------------------------------------------
def build_graph():
    g = StateGraph(CheckState)

    for fn in (lookup_registry, count_db_rows, count_file_rows, assess,
               investigate_missing_rows, compare_amounts, report):
        g.add_node(fn.__name__, fn)

    g.add_edge(START, "lookup_registry")

    # fan-out：查 DB 和读文件互不依赖，同一步并发跑，join 在 assess。
    g.add_edge("lookup_registry", "count_db_rows")
    g.add_edge("lookup_registry", "count_file_rows")
    g.add_edge("count_db_rows", "assess")
    g.add_edge("count_file_rows", "assess")

    g.add_conditional_edges("assess", route_after_assess)

    g.add_edge("investigate_missing_rows", "report")
    g.add_edge("compare_amounts", "report")
    g.add_edge("report", END)

    return g.compile()


# --------------------------------------------------------------------------
# 5. 跑起来
# --------------------------------------------------------------------------
ALL_TABLES = [
    "s3_clickstream_raw",
    "app_logs_raw",
    "customer_a_orders_raw",
    "customer_b_orders_raw",
]


def main() -> None:
    ap = argparse.ArgumentParser(description="固定流程版数据体检（无需 LLM）")
    ap.add_argument("tables", nargs="*", default=ALL_TABLES, help="要体检的原始表，默认全部")
    ap.add_argument("--date", default=REPORT_DATE, help=f"体检日期，默认 {REPORT_DATE}")
    ap.add_argument("--stream", action="store_true", help="逐节点打印中间状态")
    ap.add_argument("--mermaid", action="store_true", help="只打印图结构后退出")
    args = ap.parse_args()

    graph = build_graph()

    if args.mermaid:
        print(graph.get_graph().draw_mermaid())
        return

    for table in args.tables:
        payload = {"source_table": table, "report_date": args.date}
        print(f"\n=== {table} ===")

        if args.stream:
            # stream_mode="updates"：每个节点跑完吐一次它写回 State 的增量，
            # 和 trace_agent.astream_events() 观察 ReAct agent 是同一个机制。
            for chunk in graph.stream(payload, stream_mode="updates"):
                for node, update in chunk.items():
                    print(f"  [{node}] {update}")
            continue

        final = graph.invoke(payload)
        for line in final["findings"]:
            print(f"  · {line}")
        print(f"  → {final['verdict']}")


if __name__ == "__main__":
    main()
