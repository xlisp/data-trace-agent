# 从 CoT 到 ReAct，再到"会自己思考"的模型

## —— 以 data-trace-agent 为例，深度解析推理范式的演进

> 本文以本仓库 `data-trace-agent`（一个用 LangGraph `create_react_agent` + 两个 MCP server 抓 ETL bug 的 POC）为活体样本，串起 Chain-of-Thought、Tree-of-Thoughts、ReAct、Agent Harness 几个核心概念，并把它们放进一条更大的主线里：**推理能力是如何从"用提示词激发"一步步变成"被训练进模型权重本身"的**——这条线从 CoT prompting 走到 OpenAI o1，再走到 DeepSeek-R1。

---

## 0. 开场：一个会查库、读文件、抓 bug 的 agent

先看本项目跑起来时发生了什么。用户问：

> 今天的 `total_revenue` 比上个月低很多——具体低了多少，根本原因是什么？也请检查一下上游原始源文件。

agent 没有直接"猜"，而是：先去查 `daily_metrics` 最近 30 天的序列、算出今天比均值低了约 63%，再读 `_field_lineage` 找到 `total_revenue` 的上游是两个客户订单表，接着去查 `customer_b_orders_raw` 发现今天只有 5 行、再用文件系统工具把 `data/sources/customer_b/2026-04-26.csv` 读出来——发现源文件里有 80 行（75 EUR + 5 USD），最后给出结论：`load_customer_b_orders` 这个加载器静默丢掉了所有非 USD 的行。

这个过程里同时出现了两件事：**思考**（"今天低了多少？上游是谁？哪一步丢了数据？"）和**行动**（执行 SQL、读磁盘文件）。这两件事交织在一起，就是 **ReAct**。而能让这两件事稳定地一圈一圈跑下去、能在工具报错时自动恢复、能管理多轮历史的那套代码，就是 **Harness**。

本文就从这个具体场景出发，把几个概念逐层拆开。

---

## 1. 第一性原理：为什么一个 next-token predictor 需要"思考"和"行动"

大语言模型的本质，是一个在给定上文条件下预测下一个 token 的概率分布的函数。它一次前向传播的计算量是固定的——无论问题是"1+1 等于几"还是"证明这道竞赛数学题"，模型在产出第一个答案 token 时所用的算力是一样多的。

这带来一个根本矛盾：**难题需要更多的"计算步数"，但直接输出答案只给了模型一步。** 于是有了两条互补的破解思路：

1. **把推理过程显式写出来（CoT）**——让模型在给出最终答案前，先生成一串中间推导 token。每多写一个中间步骤，就等于多给了模型一次前向传播去"算"。这是在**时间维度**上为模型争取算力。
2. **让模型接触外部世界（Acting）**——模型权重里没有今天的数据库内容、没有那个 CSV 文件里的 80 行数据，光靠"想"永远想不出来。必须给它工具去**读取真实状态**。

`data-trace-agent` 故意埋的两个 bug，正是这两条思路的绝佳隐喻：

```python
# setup_warehouse.py —— 两个故意埋下的加载器 bug

# bug 1: 精度截断。文件里是 119.06，加载时用 int() 砍掉了小数
(int(row["order_id"]), int(row["user_id"]), int(float(row["amount"])), row["ts"])
#                                            ^^^ 应该是 float()

# bug 2: 币种过滤。静默跳过所有非 USD 行
if row["currency"] != "USD":
    continue
```

这两个 bug 的共同点是：**只看数据库（DB）永远发现不了，只靠模型"凭空推理"也发现不了。** 你必须把数据库里的值和源文件里的值并排比，差异才会浮现。这就是为什么本项目要同时挂两个 MCP server——查 DB 回答"现在有什么"，读文件回答"本来应该有什么"，两者之差就是 bug。这是对"光思考不够、必须行动"这一原理最干净的演示。

---

## 2. CoT（Chain of Thought）：把推理"说出来"

### 2.1 它是什么

Chain-of-Thought 由 Wei 等人在 2022 年提出，核心动作极其朴素：在提示里给出几个"带推理过程的范例"（few-shot CoT），或者干脆加一句"Let's think step by step"（zero-shot CoT），模型就会在输出答案前先生成一串推导。

它为什么有效，回到第 1 节的原理：把一个"一步到位的映射"拆成"一连串可计算的中间步骤"，相当于在推理时（test-time）给模型分配了更多算力预算。模型不是变聪明了，而是被允许"多想几步再答"。

### 2.2 在本项目里的影子

本项目用的是 ReAct（下一节讲），但它的 system prompt 里其实塞满了 CoT 式的"思维脚手架"——把解题套路用自然语言写给模型看：

```text
# trace_agent.py 的 SYSTEM_PROMPT（节选）

- Anomaly playbook: query the recent series, compute today vs prior-30d
  average, look up lineage in `_field_lineage`, then drill into each upstream
  raw table for the same day. If a raw table looks short, *also* look up its
  file in `_source_registry` and read the file ...

- Discrepancy playbook: when comparing DB and source, pick a small set of
  primary keys, read the source file, parse the matching rows, and contrast
  the values column-by-column.
```

这两个 "playbook" 本质上是**人类把推理链预先写好喂给模型**：先做什么、再做什么、什么条件下该转向。它不是 ReAct 的工具调用，而是 CoT 思想在 prompt 工程里的体现——用语言结构去约束和引导模型的思考路径。

### 2.3 CoT 的两个天花板

- **闭门造车**：CoT 完全跑在模型内部表征里，无法接触外部世界。本项目那个 CSV 文件里的 80 行数据，CoT 再怎么想也想不出来。
- **误差累积**：链是单向的，前面一步推错，后面会沿着错误一路滑下去，且没有自我纠正的机制。

这两个天花板，分别由 ToT 和 ReAct 来突破。

---

## 3. ToT（Tree of Thoughts）：把线性链升级为搜索树

### 3.1 它是什么

Tree-of-Thoughts（Yao 等人，2023）针对的是 CoT 的"误差累积 + 无回溯"问题。它把每一个"中间思路"看成搜索树上的一个节点，让模型：

- 在每一步**生成多个候选思路**（分叉），
- 用一个评估函数给每条思路打分（"这条路有没有希望"），
- 按 BFS / DFS 去**探索、剪枝、回溯**。

如果说 CoT 是在迷宫里只走一条路、撞墙了也只能继续往前，那么 ToT 就是允许你站在岔路口先评估几条路、走不通就退回来换一条。

### 3.2 和本项目的关系

本项目没有显式实现 ToT 的搜索树，但 ReAct agent 在实践中会自发表现出类似的"试错—回退"行为。看 demo 里这段真实日志（用户问 customer_a 金额是否一致）：

```text
[tool-call] execute_query
  query: SELECT * FROM customer_a_orders_raw WHERE date(order_date) = '2026-04-26' ...
[tool-result] Error executing query: no such column: order_date   ← 撞墙了

[tool-call] execute_query
  query: SELECT * FROM customer_a_orders_raw WHERE date(ts) = '2026-04-26' ...   ← 换条路
```

第一条 SQL 用了不存在的列 `order_date`，撞墙；agent 观察到错误，改用 `ts` 重试成功。这正是"评估当前路径不通 → 回退 → 换一条"的微缩版。区别在于：ToT 是把这种搜索**结构化、外置成一套显式算法**；而现代 agent 越来越多地把这种"换思路"内化成了模型自身的行为（这点第 7 节会展开，它正是 o1/R1 范式的关键）。

ToT 的代价也很直接：每个节点都要多次采样和评估，token 成本和延迟成倍上升。它适合搜索空间明确、答案可验证的硬问题（数学、规划、24 点游戏），不适合本项目这种"主要瓶颈是接触外部数据"的任务。

---

## 4. ReAct（Reasoning + Acting）：把思考与行动交织，给模型装上手脚

### 4.1 它是什么：一个 Thought → Action → Observation 的循环

ReAct（Yao 等人，2022，普林斯顿 + Google Research）是本项目的核心范式，也被广泛视为"Agentic LLM"的奠基性工作。它的洞察是：此前**推理（CoT）**和**行动（生成动作计划）**一直被当成两个独立课题研究，而真正强大的是把两者交错起来：

- **推理轨迹（Thought）** 帮模型归纳、追踪、更新行动计划，并处理异常；
- **行动（Action）** 让模型去接触外部世界（知识库、数据库、文件系统、网页），把观察到的真实信息（**Observation**）喂回推理。

于是一次 ReAct 就是这样一圈一圈转：

```text
Thought:  我得先看 total_revenue 最近的走势
Action:   execute_query("SELECT ... FROM daily_metrics ORDER BY report_date DESC LIMIT 30")
Observation: 今天 6808.62，前 30 天均值约 18501
Thought:  跌了 ~63%，去查 total_revenue 的血缘是谁
Action:   execute_query("SELECT * FROM _field_lineage WHERE target_field='total_revenue'")
Observation: 来自 customer_a / customer_b 两个订单表
Thought:  分别看这两个表今天的量 ...
... （循环直到能给出根因）
```

它一举解决了前面两个范式的短板：相比纯 CoT，它能**接触外部世界**、用真实观察打断幻觉；相比纯 acting（只会按计划执行、不会反思），它能**动态调整计划、处理意外**。

### 4.2 在 data-trace-agent 里，ReAct 是怎么落地的

整个 agent 的装配只有几行——`create_react_agent` 把"一个 LLM + 一堆工具 + 一段 system prompt"组合成一个会自己跑 ReAct 循环的图：

```python
# trace_agent.py
async def build_agent():
    client = MultiServerMCPClient({
        "sqlite-db":   {"command": sys.executable, "args": [SQLITE_MCP_MAIN], "transport": "stdio", "cwd": MCP_DIR},
        "filesystem":  {"command": sys.executable, "args": [FS_MCP_MAIN],     "transport": "stdio", "cwd": MCP_DIR},
    })
    tools = await client.get_tools()
    agent = create_react_agent(_make_llm(), tools, prompt=SYSTEM_PROMPT)
    return client, agent
```

这里有三个关键角色，正好对应 ReAct 的三要素：

1. **Reasoning 的载体 = LLM + SYSTEM_PROMPT。** prompt 里那两段 playbook（见 2.2 节）告诉模型该怎么想；模型每一轮产出的 `Thought` 就是它的推理轨迹。
2. **Action 空间 = 两个 MCP server 暴露的 18 个工具。** SQL 侧有 `execute_query / describe_table / trace_field_lineage` 等；文件系统侧有 `read_file / list_directory / search_files_ag / execute_command` 等。模型能"做"的所有事，被这组工具严格界定。
3. **Observation = 工具返回值。** 比如 `read_file` 把 CSV 内容读回来、`execute_query` 把查询结果或报错读回来——这些观察被塞回上下文，驱动下一轮推理。

### 4.3 一条完整的真实 trace（这才是 ReAct 的精髓）

下面是 demo 里 agent 回答"customer_a 金额是否与源文件一致"时打印出的真实工具调用序列，我把它标注成 Thought/Action/Observation：

```text
(Thought) 先搞清楚表结构和源文件在哪
(Action)  describe_table(customer_a_orders_raw)
(Action)  execute_query("SELECT * FROM _source_registry WHERE source_table='customer_a_orders_raw'")
(Observation) 文件在 .../customer_a/，loader = load_customer_a_orders，schema_note 说金额是 2 位小数

(Action)  execute_query("... WHERE date(order_date)='2026-04-26' ...")
(Observation) Error: no such column: order_date          ← 工具报错

(Thought) 列名错了，schema 里是 ts 不是 order_date，改一下
(Action)  execute_query("... WHERE date(ts)='2026-04-26' ...")
(Observation) 拿到 DB 里的金额：119.0, 100.0, 148.0 ...

(Action)  read_file(".../customer_a/2026-04-26.csv")
(Observation) 源文件金额：119.06, 100.88, 148.09 ...

(Thought) DB 全是整数、文件有小数 → loader 用了 int() 截断
(Final)   指认 load_customer_a_orders，解释 int() vs float() 的 bug
```

请重点看中间那次报错。`order_date` 列不存在，工具返回了 `no such column` 错误。在纯 CoT 里这会直接拖垮整条推理链；但在 ReAct 里，这个**错误本身成了一个 Observation**，模型读到它、意识到列名不对、改用 `ts` 重试。**这种"观察—纠错—重试"的闭环，正是 ReAct 相对纯推理最本质的优势**：它把模型从自己的错误里拽了回来。

### 4.4 为什么这个项目"非 ReAct 不可"

回到第 1 节那两个埋下的 bug。`int()` 截断和币种过滤，**在 SQL 层完全是合法数据**（119.0 是个正常的 REAL，5 行 USD 订单也是正常的行），没有任何约束会报错。唯一能发现它们的办法，就是**跳出数据库、去读那个 loader 当初看到的源文件，再两相对比**。

这件事 CoT 做不到（它接触不到文件），纯 acting 也做不好（它不会主动推断"我应该去比对源文件"）。只有 ReAct——一边推理"如果 DB 数据看起来对但结果异常，可能是 loader 在落库时改了值，我得去读源文件验证"，一边真的去 `read_file`——才能闭合这个回路。这个 POC 本质上是一台"为什么需要 ReAct"的演示机。

---

## 5. Harness（脚手架）：约束在退潮，能力在涨潮

ReAct 循环要稳定地一圈圈跑下去——管理多轮历史、把工具返回喂回上下文、出错时让模型恢复、给出安全边界——这套"模型之外的一切"，就是 **Harness（脚手架）**。换句话说，Harness = 把 ReAct loop + 反思 + 规划 + 工具编排 + 持久记忆 + 子任务委派沉淀成一个可复用框架，模型只负责"想"和"决策"，其余交给框架。本项目里，LangGraph 的 `create_react_agent` + `astream_events` 事件循环就是这个框架的本体。

但 Harness 真正的要害不在那层框架代码——`create_react_agent` 这类框架已经高度收敛、彼此趋同——而在两个此消彼长的趋势：

- **约束在退潮**：早期要靠 prompt 把"先读结构、再读文件、动手前确认"这类 SOP 写死，是因为模型容易跑偏。模型越强，这些约束越可以变松，乃至直接删掉——让模型自己找最优路径。本项目 `SYSTEM_PROMPT` 里那两段 playbook，正属于"会随底座模型变强而越来越可以精简"的部分。
- **能力在涨潮**：模型决定一个问题能不能解决，往往不在于它"够不够聪明"，而在于它**有没有足够有用的 tools**去把世界的状态读进来、把行动作用回去。`data-trace-agent` 就是这一侧的标本：那两个 bug 模型再聪明也想不出来，唯一的钥匙是 `read_file` 这个 action——**tools 决定了能不能解决，而不是模型智商**。

由此引出一条贯穿本项目的设计原则：**精准 tools 即精准上下文。** 一个好工具，能把模型本来要绕好几个 loop 才能拼出来的信息一次返回——这等价于给模型一个更短、更准、更便宜的 prompt。本项目刻意把能力拆成 `sqlite-db` 与 `filesystem` 两个职责清晰的 MCP server，而 `filesystem_mcp_server.py` 里的 `ALLOWED_EXTENSIONS` / `BLOCKED_COMMANDS` / `is_safe_path` 则属于 Harness 中**唯一不该退潮的部分**——安全边界。模型越敢做事，这层护栏越重要。

> Harness 这件事的完整展开——从 CoT → ToT → ReAct → Reflexion → Harness 的简史、"模型越强 harness 越松"、"精准 tools 即精准上下文"，以及逆向工程、训练模型（autoresearch）等"光读代码推不出、必须靠实验逼近"的反例——本文不再赘述，感兴趣的读者可移步这篇文章：
>
> **https://mp.weixin.qq.com/s/l7p1E0WMnPzkMI2CCVkWpw**


---

## 6. 主线：从"提示模型思考"到"把思考训练进模型本身"

前面五节讲的 CoT、ToT、ReAct、Harness，有一个共同前提：**它们几乎都是 in-context、prompt 层面的技巧。** 模型权重不变，我们靠提示词、靠外置的搜索/循环结构，把模型本来就藏在权重里的推理能力"激发"或"组织"出来。本节要讲的，是过去一年多最重要的范式转移——**这些原本要靠外部脚手架强加的"思维结构"，正在被直接训练进模型本身。** 这正是从模型发展角度看 CoT 演进最关键的一段。

### 6.1 阶段一：能力在权重里，结构在 prompt 上（2022–2024）

CoT 提示之所以"一句 Let's think step by step 就能涨点"，恰恰说明推理能力**早就存在于预训练好的权重里**，只是默认不会被触发。ReAct 和 ToT 更进一步，用外部结构去**编排**这种能力：ReAct 在外面套一个"思考—行动—观察"循环，ToT 在外面套一棵搜索树。

换句话说，这个阶段的分工是：**模型负责"能想"，人类工程师负责"怎么想"**——把推理路径、回溯策略、工具调用时机，全部写进 prompt 和 harness 里。`data-trace-agent` 的两段 playbook 就是这种分工的标本：解题套路是人写死的，模型只是顺着套路填空。

### 6.2 阶段二：o1 —— 用大规模 RL 把 CoT 训练进权重（2024 下半年）

OpenAI 的 o1 是这条线上的分水岭。它不再把 CoT 当成一个"提示技巧"，而是用**大规模强化学习**直接训练模型"在回答前先产生一长串内部思维链"。按 OpenAI 的官方说法，o1 是"先思考再回答"的——这串长 CoT 是模型**内生的**，不需要用户在 prompt 里教它"一步步来"或给范例。

更关键的是 o1 揭示的两条全新 scaling 轴：

- **训练时 RL 算力（train-time compute）**：投入越多 RL 训练，推理能力越强；
- **推理时思考算力（test-time compute）**：让模型"想得越久"（生成越长的思维链），答案越准。

这第二条尤其颠覆——它意味着可以**在推理阶段、用花更多算力去想的方式换取更高准确率**，而不必把模型做得更大。通过 RL，o1 学会了打磨自己的思维链：识别并纠正错误、把难步骤拆成简单步骤、此路不通时换一种方法。请注意这几个行为——**"纠错""换思路"**——正是我们在第 3 节 ToT 和第 4 节 ReAct 里靠外部结构去实现的东西，现在它们变成了模型在 RL 训练中自发学会的内生行为。

o1 还顺带带来一个安全维度的副产品：deliberative alignment——模型可以在那串思维链里**对照安全策略进行推理**，从而更稳地拒绝越狱、给出合规回答。思维链不再只是为了答对题，也成了对齐的载体。

### 6.3 阶段三：DeepSeek-R1 —— 纯 RL 就能"自发涌现"推理（2025 初）

如果说 o1 证明了"可以把推理训进模型"，DeepSeek-R1（及其前身 R1-Zero）则把这件事**开源、透明地**摆在了所有人面前，对中文社区尤其重要。

R1-Zero 最惊人的地方在于：它**直接在 base model 上做 RL，完全不用 SFT 冷启动**，仅靠基于规则的奖励（答案对不对、格式合不合规），模型就**自发涌现**出了一系列复杂推理行为：

- 自己学会把思维链越拉越长（难题想得更久）；
- 自我验证（self-verification）与反思（reflection）；
- 甚至出现所谓的"aha moment"——模型中途停下、意识到前面错了、退回去重来。

这是第一份公开验证"推理能力可以纯靠 RL 激励出来、无需人工标注的推理轨迹"的研究，2025 年还登上了 Nature。它用的核心算法是 **GRPO**（Group Relative Policy Optimization）——通过组内奖励归一化来稳定训练，省掉了单独的价值网络。

把这件事和前几节连起来看，会有一种强烈的"收束感"：

- ToT 当年要在外部显式搭建的**搜索与回溯**，在 R1-Zero 里变成了模型自己学会的"想不通就换条路"；
- ReAct 当年要靠 observation 反喂才能实现的**自我纠错**，在 R1-Zero 里变成了思维链内部自发的 self-verification；
- CoT 当年要靠 prompt 激发的**分步推理**，现在直接长在了权重里。

**过去挂在模型外面的思维脚手架，正在被一寸寸吸收进模型本体。**

> 顺带一提：你正在做的 MathGPT（GRPO / REINFORCE / Pass@k / KL 正则那套，目标 GSM8K）走的正是 R1-Zero 这条路线——用规则奖励 + RL 在数学题上逼出长 CoT。数学之所以成为 RL-reasoning 最先被攻克的战场，原因也很朴素：答案可自动验证，奖励信号干净，正好喂得动 RL。

### 6.4 那么 ReAct 和 Harness 会被"训没"吗？——不会，而且原因很深刻

读到这里容易产生一个错觉：既然思考能被训进模型，那 ReAct、harness 这些外部脚手架是不是迟早被淘汰？

答案是否定的，理由恰恰是本项目的设计初衷。**RL 能把"推理（Reasoning）"内化进权重，但无法把"行动（Acting）"内化进去。** 模型再会想，它的权重里也不会有：

- 今天那个 `warehouse.db` 的实时内容；
- `customer_b/2026-04-26.csv` 里那 80 行数据；
- 你公司明天才会到的上游文件。

这些只能靠 **action** 去现场读取。无论模型多强，要发现"文件说 119.06、DB 却是 119"这个 bug，**永远需要 `read_file` 这个动作、需要一个 harness 去执行它、需要护栏去约束它**。这就是为什么即便进入了 o1/R1 时代，`data-trace-agent` 这类系统的骨架——ReAct 循环 + 工具 + harness——依然不可替代。

更准确的图景是一种**分工的再平衡**：

- **能想的部分**（推理、反思、规划、纠错）正越来越多地从 prompt/harness **下沉进模型权重**；
- **要碰真实世界的部分**（工具、数据、副作用、安全边界）则继续**留在 harness 层**，而且随着模型敢做的事越多，harness 的护栏（参考 5.2 的 `BLOCKED_COMMANDS`、`is_safe_path`、`recursion_limit`）反而**越来越重要**。

模型越聪明，你越需要一个可靠的"缰绳"（harness 的本义）。

---

## 7. 一张全景图：能力在内、结构在外的此消彼长

把全文压缩成一条演化轴：

```
        外部结构（prompt / harness）                内化进模型权重（training）
        ────────────────────────────────►  ─────────────────────────────►

CoT      "Let's think step by step"          o1: 长 CoT 由 RL 训练内生
         （提示激发分步推理）                  R1-Zero: 分步推理自发涌现

ToT      显式搜索树 + 评估 + 回溯              o1/R1: "换条路"成为模型自发行为
         （人搭的搜索算法）

ReAct    Thought/Action/Observation 循环      推理侧（Thought）内化进权重；
         （harness 闭合的循环）                行动侧（Action）永远留在 harness

Harness  约束（SOP）+ 能力（tools）           约束退潮：SOP 越来越不用写死
         （模型之外的一切）                    能力/护栏涨潮：tools 与安全边界无法被训练吸收
```

读这张图的正确方式是：**沿时间从左往右，"怎么想"这件事正不断从右侧的工程层向左侧的模型权重迁移；但"碰什么、能碰到什么程度"这件事，始终是 harness 的领地。** `data-trace-agent` 恰好同时站在这条轴的两端——它用 ReAct/playbook 组织思考（会随底座模型变强而越来越省心），又用两个 MCP server + 安全护栏锚定行动（无论模型多强都省不掉）。

---

## 8. 概念速查

- **CoT（Chain of Thought）**：让模型在答案前先生成中间推理步骤；本质是在推理时为模型争取更多"计算步数"。
- **ToT（Tree of Thoughts）**：把线性推理升级为可分叉、可评估、可回溯的搜索树；以成本换取在硬问题上的可靠性。
- **ReAct（Reasoning + Acting）**：思考与行动交织的循环（Thought→Action→Observation），让模型既能推理又能接触真实世界，并借 observation 自我纠错。本项目的核心范式。
- **Harness / Scaffold（脚手架）**：把 ReAct loop + 反思 + 规划 + 工具编排 + 记忆等沉淀成的可复用框架，模型只负责"想"。其要害是约束退潮、能力涨潮——决定上限的是 tools 够不够精准（"精准 tools 即精准上下文"），而非模型智商。本项目里是 `create_react_agent` + `astream_events` + 两个 MCP server + 安全策略的总和。
- **o1 范式**：用大规模 RL 把 CoT 训练进模型权重，开启 train-time 与 test-time 两条算力 scaling 轴。
- **DeepSeek-R1 / R1-Zero**：纯 RL（GRPO + 规则奖励，无需 SFT 冷启动）即可让推理能力自发涌现，开源验证了 o1 路线。
- **MCP（Model Context Protocol）**：标准化的"模型↔工具"接口；本项目用两个 stdio MCP server 分别提供 SQL 与文件系统能力，构成 agent 的 Action 空间。

---

## 9. 结语

`data-trace-agent` 表面上是个几百行的 ETL bug 猎手 POC，但它把这一整条范式演进史浓缩在了一个能跑的系统里：playbook 是 CoT 的影子，错误重试是 ReAct 闭环的体现，`create_react_agent` 与那套安全护栏是 harness 的实体，而两个"只能靠读文件才能抓到"的 bug，则是对"为什么推理永远替代不了行动"最简洁的论证。

当模型从 GPT 一路走到 o1、R1，越来越多的"思考"被训进了权重，写这类系统会越来越省心——你不再需要把每一步推理都手把手写进 prompt。但只要任务还需要触碰那个具体的数据库、那个具体的 CSV 文件，ReAct 的骨架和 harness 的护栏就不会消失。**模型负责越来越强的"想"，框架负责始终可靠的"做"与"守"——这大概就是 agent 工程在可见未来里最稳的一条分界线。**

---

## 参考文献

1. Yao, S. et al. (2022). *ReAct: Synergizing Reasoning and Acting in Language Models.* arXiv:2210.03629.
2. Wei, J. et al. (2022). *Chain-of-Thought Prompting Elicits Reasoning in Large Language Models.* arXiv:2201.11903.
3. Yao, S. et al. (2023). *Tree of Thoughts: Deliberate Problem Solving with Large Language Models.* arXiv:2305.10601.
4. OpenAI (2024). *OpenAI o1 System Card.* arXiv:2412.16720 / openai.com/index/openai-o1-system-card.
5. OpenAI (2024). *Learning to Reason with LLMs.*（o1 技术介绍）
6. DeepSeek-AI (2025). *DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning.* arXiv:2501.12948；Nature (2025) s41586-025-09422-z.
7. Shao, Z. et al. (2024). *DeepSeekMath / GRPO.* arXiv:2402.03300.
8. 关于 Harness 的完整论述（CoT→ToT→ReAct→Reflexion→Harness 简史、"模型越强 harness 越松"、"精准 tools 即精准上下文"、autoresearch 等反例）：https://mp.weixin.qq.com/s/l7p1E0WMnPzkMI2CCVkWpw

> 本文中所有代码片段均摘自本仓库（`trace_agent.py`、`setup_warehouse.py`、`mcp/filesystem_mcp_server.py`、`demo_use_log.md`）。
