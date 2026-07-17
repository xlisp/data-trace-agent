# examples

独立的、可单独运行的 LangGraph 示例。主线 `trace_agent.py` 把决策权交给模型
（`create_react_agent`），这里放的是它的对照组。

| 文件 | 说明 | 需要 LLM？ |
| --- | --- | --- |
| [`fixed_flow_lineage_check.py`](./fixed_flow_lineage_check.py) | 用 `StateGraph` 把数据体检流程**写死**：查 `_source_registry` → 并行数 DB 行数 / 文件行数 → 条件分支（丢行 / 金额被改写 / 干净）→ 汇总。演示 State + reducer、Node、固定边与条件边、fan-out 并行、`stream_mode="updates"`。 | ❌ 不需要 |

```bash
python3 setup_warehouse.py                                        # 先建好仓库

python3 examples/fixed_flow_lineage_check.py                      # 体检全部 4 张源表
python3 examples/fixed_flow_lineage_check.py customer_b_orders_raw --stream   # 逐节点看中间状态
python3 examples/fixed_flow_lineage_check.py --mermaid            # 打印图结构
```

配套文章：[`docs/LangGraph-in-practice.md`](../docs/LangGraph-in-practice.md)
