# Orchestrator 升级评审与落地设计（PDF Review）

> **评审对象**：`references/Orchestrator 升级讨论-WIP .pdf`（下称 **WIP 提案**）
>
> **参照材料**：
> - `design/`（LEAP/Arbor 设计笔记：01 数据模型 / 04 insight backprop / 05 falsification / 06 cycle / 07 explore-exploit）
> - Arbor 原论文 `references/Toward Generalist Autonomous Research via Hypothesis-Tree Refinement.pdf`
> - `codex/codex-rs/core/src/compact.rs` + `codex/codex-rs/prompts/templates/compact/`（上下文压缩参考实现）
> - 冻结边界：`docs/orchestrator-harness-optimization-scope.md`、`docs/orchestrator-harness-benchmark-expected-behavior.md`
>
> **本期落地范围（均已确认可落）**：原 A、原 C、原 B-平局、原 E、补 E 负证据、原 F、原 H、原 D，外加**上下文压缩（参考 codex）**。
>
> **本期不落**：原 J（弱模型做 reasoning 的归属）、原 I（熵度量）、原 K（监控指标闭环）、原 L 的照搬部分——见 §9 后续。

---

## 0. 一句话结论

WIP 提案**方向对、诊断准**，但把它命名的 "MCTS + UCB" 直接实现会踩坑：数学场景**采样极贵、reward 极稀疏、方向不可比**，UCB/Elo 赖以收敛的统计前提不成立。真正的病根是 **AlphaSolve 缺少 Arbor 赖以运转的"廉价、连续、分级评估信号"**——补上这个 proxy 信号，提案里 gap / UCB / E&E 的大部分难点会自然缓解。同时 WIP 提案与 `design/` 的 LEAP/Arbor 机制**高度互补**，应合并选型：提案的 pairwise critic 补 `design/07` "exploit 分从哪来"的空白，`design/` 的 insight backprop + falsification 补提案"负证据回传"与"合并一致性"的空白。

---

## 1. 名词与范式对齐（原 0：概念正名）

WIP 提案通篇称 "MCTS/MCGS"，但缺 rollout、terminal reward 极稀疏、状态不可逆（证明步骤不能回退重来）——**它不是 MCTS**。强行套 MCTS 四段式（selection/expansion/simulation/backprop）会卡在 simulation 上。

**正名**：本方案实现的是 **tree/graph-structured dueling bandit + best-first search（带 UCB-style 探索偏置）**。这与 Arbor 自身的定位一致——Arbor 的 Select 步骤原文是 "frontier control under partial and delayed feedback"，并非 score maximization，也不叫 MCTS。

### 术语映射表（WIP 提案 ↔ 本方案 / design）

| WIP 提案术语 | 本方案 / design 对应 | 说明 |
|---|---|---|
| MCTS/MCGS | tree/graph dueling bandit + best-first | 正名，避免套 rollout |
| state = 整个 prop set | pool 的一个**视图/增量**（§3） | 修正"整套 prop 复制"的歧义 |
| UCB 的 Q 值 | 局部 pairwise 胜率 + proxy 分级信号（§4/§5） | 不做全局收敛 |
| UCB 的 N（访问次数） | `visit_count`，全祖先回传（§5.4） | design/07 §3 已定义 |
| 新 arm（全新 hint） | 配额 C：depth-1 explore（§5.5） | 硬配额替代裸 ε-greedy |
| LLM-as-Critic pairwise | 四值 critic（§5.2） | 显式区分不可比 |
| 和目标的 gap | 结构化 `open_subgoals` 计数（§4） | gap 不落绝对分 |
| crossover / 图结构 | 合并前过 falsification gate（§6.4） | 一致性保证 |
| "先抄袭 codex" 压缩 | 分层无损/有损压缩（§7） | 权威内容无损 |
| harness / reasoning 拆分 | 复用现有 `research_reviewer` 拆分（§8） | 尊重冻结边界 |

---

## 2. 数据模型（原 H）

WIP 提案在"state 是整个 prop set" 与 "每个 state 和父节点只差一个 prop" 之间自相矛盾，且没说清如何映射到现有全局共享的 `verified_propositions/`。**定案如下**（与 `design/01` 对齐，并采纳提案 §"数据结构与 state 压缩" 里"复用 prop 文件结构 + UUID"的思路）：

### 2.1 全局单一 pool + 节点是"视图/增量"

- **权威材料只存一份**：`verified_propositions/`、`knowledge/` 保持现状的全局共享单一副本。参考 Arbor：树只存**对外部 artifact 的引用**，不复制材料本身。
- **节点 = pool 的一个视图**：节点不复制整套 prop，只持有
  - `state_id`（UUID，程序聚合成树/图用，采纳 WIP 提案原文）；
  - `delta`：本节点相对父节点新增的那个 prop/lemma 引用（`proof_ref` / `code_ref`）；
  - `hypothesis`：本节点要推进的假设/子目标（Ideate 写入后不可改）。
- 因此"整套 prop set 当 state"在**语义**上成立（节点视图 = 父视图 ∪ 自己的 delta），但**存储**上是增量，不复制。这直接决定 §6 crossover、§7 压缩、依赖追踪怎么做。

### 2.2 节点 schema（front matter，摘 `design/01`，本期最小集）

| 字段 | 写入者 | 本期用途 |
|---|---|---|
| `state_id` (UUID) | system | 聚合树/图；压缩去重 |
| `parents` / `children` | Orchestrator | DAG 边（图结构，非纯树） |
| `depth` | system | cost 惩罚 + 深度上限退出 |
| `status` | system | `pending/running/done/cooled/pruned/merged` |
| `hypothesis` | Orchestrator | 本节点子目标（不可改） |
| `delta` (`proof_ref`/`code_ref`) | Worker | 相对父的新增 prop 引用 |
| `result` | Worker | **事实**：raw 错误、卡在哪一步、试到哪（§6.1） |
| `insight` (yaml) | Worker→Curator | **因果教训 + open_subgoals**（§6.2） |
| `visit_count` | system | 子树累计 dispatch，全祖先回传（§5.4） |
| `self_picked_count` | system | 自身被直接 dispatch 次数 |
| `wins` / `duels` | system | 局部 pairwise 胜率池（§5.3，仅同父兄弟内） |
| `prune_reason` | Orchestrator | pruned 时必填，进 constraints view（§6.3） |

> **图结构**：`parents` 为 list（非单一），支持 WIP 提案的 crossover（多父合并）。纯树是图的特例。

---

## 3. 分级评估信号：本期根因（统摄原 A + 原 C）

这是读完 Arbor 后最重要的结论：**Arbor 不需要估 gap、能跑类 UCB 选择，是因为它每个 trial 都有便宜、连续、分级的 dev 评估器（如 `pass@4 − pass@1`、val loss）。AlphaSolve 只有 `theorem_checker` 的二元稀疏信号（解了/没解），这才是真正的病根。**

- **原 C 定案**：不要指望从 Arbor 抄到 gap——Arbor 根本没有 gap 度量，它用 dev score 绕过。"离证完还差多远"若能准确知道，问题本身就解了；所以 gap 只能是启发式，**绝不落成绝对分**。
- **原 A 定案**：有了下面的 proxy 分级信号后，selection **不依赖统计收敛**；pairwise 只当软偏置，并设"每对最多比 K 次"上限（§5.6）。

### 3.1 Proxy 分级信号（每 cycle 可算，替代二元 theorem_checker 做过程信号）

对每个活跃节点，Curator 每 cycle 计算一个**廉价代理分**，由三个可得量组合，**只用于相对比较，不做绝对阈值**：

| 分量 | 含义 | 数据来源 | 方向 |
|---|---|---|---|
| `open_subgoals` 剩余计数 | 还剩几个明确未证子目标 | insight schema（§6.2） | 越少越接近目标 |
| `verified_implications` 净增量/增长率 | 本 cycle 这条路新验证几条推导 | insight 差分 | 越大进展越快（类 dev score 一阶差分） |
| falsification 命中次数 | 子目标被证伪的负信号 | §6.3 全局扫描 | 越多越该退出 |

**防过拟合硬约束**（继承 `docs/orchestrator-harness-optimization-scope.md`）：proxy 分的计算**不得硬编码只对特定数学领域生效的名词**（upper bound / group / estimate 等），只允许硬编码连接逻辑词（therefore/hence 等）。`open_subgoals` 的计数按"结构化条目数"算，不按内容关键词算。

> 注意：`theorem_checker` 仍是**唯一的终止判据**（proxy 分**不**决定是否完成任务）。proxy 分只喂给：① 局部 pairwise 排 exploit 优先级；② exhaustion 判定；③ 退火的 `progress_rate`。

---

## 4. E&E 调度总览

WIP 提案想用 UCB 统一裁决 E&E。本方案改为 **"配额保下界 + 局部 pairwise 排序做偏好 + 结构反馈做动态调节"** 三层，不依赖统计显著性。

```
                       ┌─ exploit 名额（配额）── 局部 pairwise 软偏置(§5.2/5.3) ─→ 给有进展节点加码
Worker 预算 ─ 每 cycle ─┤
                       └─ explore 名额（配额）── 强制投新 hint / 冷门兄弟(§5.5)

  反馈调节（无需 UCB 公式）：
  insight backprop(§6) → 有新 verified_impl / open_subgoal 减少 → 下轮 exploit 优先级↑
  exhaustion/falsif(§6.3) → 路线耗尽/证伪 → prune → 预算回流 explore + 死因入硬约束
  visit_count 全祖先回传(§5.4) → 某支被深挖 → 整条祖先链探索偏置↓ → 算力推向别处
```

以下逐条展开落地细节。

---

## 5. 局部 pairwise selection（原 A / 原 B-平局 / 原 E / 原 F）

### 5.1 只做局部同父兄弟比较，不做全局 Elo（原 B）

WIP 提案 note-4 已自曝风险：`a>b, c>a, 但 c 是死胡同`——**全局线性化会淹没冷门正解**。Elo 假设单一隐变量 + 强传递性，但数学方向是偏序/不可比。

**定案**：pairwise 比较**只在同一父节点的兄弟之间**进行，**不做跨父/全局 Elo 线性化**。这从架构上消除了传递性破坏（根本不会把 a/b/c 拉到一条全局链上）。

### 5.2 critic 输出四值，而非三值（原 B 的前提）

局部 pairwise critic（LLM，作为独立 subagent，采纳 WIP 提案）输出**四值**，而非 `A>B / B>A / tie`：

| critic 输出 | 含义 | E&E 分支 |
|---|---|---|
| `A > B` / `B > A` | 有明确高下 | 正常排 exploit 优先级 |
| **comparable-tie** | 可比、势均力敌 | 落二级信号 tie-break（§5.3） |
| **incomparable** | 正交、根本无法比较 | **不排序**，走多样性保护（§5.3） |

critic 的 rubric 输入（采纳 WIP 提案 §Critic）：当前节点 state 压缩视图、其叶子的 efficient frontier、多样性、与目标 gap（用 §4 的 open_subgoals，不用绝对分）、hint 轨迹对比。**rubric 同样受 §3.1 防过拟合约束。**

### 5.3 平局定案（原 B-平局，本次核心增量）

区分两类平局，**处理完全相反**：

**comparable-tie（势均力敌）→ 计入胜率池，各 +0.5**

```
comparable-tie:  A.wins += 0.5 ;  A.duels += 1
                 B.wins += 0.5 ;  B.duels += 1
Q = wins / duels
```

- **必须 +0.5，不能 +1**：各 +1 等于"两边都赢一场"，会系统性抬高所有参与平局节点的 Q，压低从没比过/比输过的节点（尤其新 arm），抑制探索。+0.5 对 Q 的拉力指向 0.5，是无偏的（Elo/Bradley-Terry 标准做法）。
- tie-break 优先级：① §4 proxy 分（open_subgoals 少者优先 / verified_impl 增长快者优先）→ ② `visit_count` 少者优先（等价 UCB 探索项，偏向欠探索支）→ ③ novelty 高者优先。
- **comparable-tie 同时标记为 crossover 候选**（§6.4），但合并前必须过 falsification 一致性检查。

**incomparable（正交/不可比）→ 不计入胜率池**

```
incomparable:    不改 wins，不改 duels，不回传任何排序
                 两支各从 explore 配额独立保底演化
```

- "不可比" ≠ "实力相等"。用半分记录会把"测不出"错误编码成"测出来相等"，稀释真实相对信号（正是 note-4 的 `a` 被 `b` 假平局拖平的场景），冷门正解就是这样被淹没的。
- 处理：两支视为不同"方向簇"，从 explore 配额**各自独立分资源**并行演化，谁也不压制谁——给"当前看着差、却可能唯一能通"的正交方向留活路。

**平局决策树**

```
critic(A, B)
├─ A>B / B>A ──────────→ 正常排 exploit 优先级
├─ comparable-tie ─────→ 各 +0.5 入池 → proxy → visit_count 少 → novelty 打破
│                        并标记 crossover 候选（须先过 falsification）
└─ incomparable ───────→ 不入池、不排序；两支各从 explore 配额独立保底（多样性保护）
```

### 5.4 visit_count 全祖先回传（原 E）

WIP 提案只回传胜率，**漏了 visit_count 的回传**——这是 `design/07 §3` 明确要求的、治"一条道走到黑"的核心：

```python
def on_dispatch(n):
    n.self_picked_count += 1
    for a in [n] + ancestors(n):      # 关键：传到所有祖先
        a.visit_count += 1
    blueprint.total_visits += 1
```

某分支被深挖 → 整条祖先链的探索偏置（UCB-style `sqrt(log N / n)`）下降 → 算力被推向其他分支。**胜率同样只在局部兄弟池累加，但 visit_count 沿全祖先链累加。**（跨 pool 借调时 visit_count 只在源 pool 累加，避免双重 inflation，见 `design/07 §5`。）

### 5.5 探索配额 + 自适应退火（原 F）

WIP 提案用固定 ε-greedy 开新 arm，要么发散要么早熟。**改用硬配额 + 进展率驱动退火**（`design/07 §2/§4`）：

| 配额 | 触发 | 来源节点 |
|---|---|---|
| **A. Exploit** | 总是 | 局部 pairwise + proxy 分最高 |
| **B. Sibling-explore** | exploit 节点有兄弟 | 同父不同方向（含 incomparable 支） |
| **C. Depth-1 explore（新 arm）** | cycle ≥ 阈值 | 强制开新 depth-1 hint（替代裸 ε-greedy） |
| **E. Fill** | 剩余 | 按 score + Jaccard 多样性 |

- 配额是**硬约束**，不是 LLM 建议；无合格候选时**少派不凑数**。
- 退火：`progress_rate = 最近 W cycle 的 vp 增量 / dispatch 数`（复用 §3.1 proxy 信号）驱动自适应 τ——进展快 → 早转 exploit；进展慢 → 多留 explore。替代固定常数。

### 5.6 反复平局止损（呼应原 A：采样贵）

- 同一对最多比 **K 次（建议 1–2）**；K 次仍 tie → 判定 critic 确实无法区分，**停止比较**，交给 proxy 分 + 探索配额接管，**不再为这对花采样**。
- 这把"贵采样 + 不可比"从"必须收敛"降级为"只需相对排序"，选错代价可控。

---

## 6. Insight Backpropagation（补 E：负证据回传）

WIP 提案只回传胜率，**不传"为什么这条路不行"的因果教训**。引入 `design/04` 的 insight backprop 补齐——Arbor ablation 显示 `w/o insight feedback` 比 `w/o tree` 还差，说明经验传播比树结构本身更关键。

### 6.1 result / insight 严格分离

| 字段 | 内容 | 写入者 |
|---|---|---|
| `result` | **事实**：raw 错误、卡住的 step、试到哪 | Worker |
| `insight` | **因果教训**：为什么通 / 为什么不通 | Worker distill → Curator abstract |

合并会让 LLM 混淆"发生了什么"和"为什么"，破坏抽象质量。**这个分离同时是 §7 压缩的基础**（result 可有损摘要，权威 prop 无损）。

### 6.2 半结构化 insight schema（承载 gap）

```yaml
verified_implications:                       # 已验证推导（proxy 增长率来源）
  - "X ⇒ Y₁"
falsified_implications:                       # 已证伪（falsification 来源）
  - implication: "X ⇏ Z"
    counterexample: "..."
open_subgoals:                                # ← gap 的结构化落地（§4）
  - "需要证明 W 才能继续"
  - "缺 X 的上界估计"
direction_summary: "<200 字自然语言总结>"
```

**gap 定案**：不存"还差 40%"这种绝对分（自欺欺人），改存 `open_subgoals`——"还剩几个明确未证子目标"比"拍脑袋百分比"可信，且天然可回传、可合成、可被 falsification 判死（某 subgoal 被证 `¬W` → 该路不是"gap 变大"而是**直接判死**，让 gap 有硬的一端兜底）。

### 6.3 横向兄弟合成 + falsification（原理见 design/04 §3、design/05）

```python
def propagate_insights(leaf):
    for ancestor in reverse(path(root → leaf.parent)):
        ancestor.insight = LLM_abstract(
            [c.insight for c in children(ancestor)], max_words=200)
```

- 每个祖先 = 对其**直接子节点** insight 的再抽象，**逐层刷新**（不是把所有叶子平铺汇总到根，也不是 append）。
- **falsification**（`design/05`）：
  - 通用触发源：单节点失败被祖先吸收、parent exhaustion、convergence stop；worker 实现挂了/评估器崩了 → **不剪**，留作 informative failure。
  - **数学场景硬证伪（自研超纲增强，非 Arbor 原生，需明确标注）**：新引理证出 `¬B` → 每 cycle 末 Decide 阶段扫全局 DAG，硬砍**所有** pool 里含 ansatz B 的节点。
  - **节点不删除**，仅 `status=pruned`，保留为 evidence；`prune_reason` 必填 → 进 constraints view → 下轮作为 hard constraint 注入，死因本身变成可复用负经验。

### 6.4 crossover 一致性 gate（补 §5.3 的 comparable-tie 候选）

WIP 提案的 crossover（合并路径成图）无一致性保证。**定案**：两条 comparable-tie 兄弟标为 crossover 候选后，**合并前必须过 falsification 检查**——确认两支不基于互相矛盾/已被证伪的假设，才允许合并成多父图节点。这样平局从"选不出"变成"发现可合并机会"的正面信号。

---

## 7. 上下文压缩（参考 codex）

> 用户明确要求参考 `codex/codex-rs`。关键澄清：**codex 的压缩并非"全量有损"**——它把权威输入逐字保留，只对模型自己的推理轨迹做摘要。这恰好化解了"数学压缩会丢常数/命题/依赖"的担忧，也和 §6.1 的 result/insight 分离同构。

### 7.1 codex 压缩机制拆解（`compact.rs` + `templates/compact/`）

1. **定位**：一次 compaction = **"给下一个 LLM 的交接摘要（handoff summary），让它无缝接着干"**。prompt 只要四类内容：当前进展与关键决策、重要上下文/约束/偏好、剩余待办（清晰 next steps）、继续所需的关键数据/示例/引用。
2. **分层保留（核心）**：压缩后的 `replacement_history = 初始上下文 + 近期 user 消息（逐字保留）+ summary`。
   - **user 消息逐字保留**（`build_compacted_history`，预算上限 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20000`，从旧到新截断、保新）——即"权威输入不摘要"。
   - **只有模型自己的推理/工具轨迹被摘要**成 summary（带 `SUMMARY_PREFIX`）。
3. **锚点重注入**：每次压缩后把 canonical 初始上下文重新插到"最后一条真实 user 消息之前"的固定锚点（`insert_initial_context_before_last_real_user_or_summary`）。
4. **降级兜底**：context window 溢出时从**最旧** item 开始丢（`remove_first_item`），保住近期与 summary。

### 7.2 映射到 AlphaSolve（分层无损 / 有损）

| codex 概念 | AlphaSolve 对应 | 压缩策略 |
|---|---|---|
| user 消息（权威输入，逐字保留） | `verified_propositions/` 命题**精确陈述**、常数、prop 依赖、`delta` 引用 | **无损**：绝不摘要、绝不改写数学陈述 |
| 模型推理/工具轨迹（可摘要） | 节点 `result`（raw 错误、死路、尝试轨迹） | **有损**：LLM 蒸馏为 `insight`（§6.1） |
| SUMMARY_PREFIX + 交接摘要 | 节点 / 祖先链的 `insight`（半结构化 yaml，§6.2） | 结构化摘要，保 open_subgoals/implications |
| 初始上下文锚点重注入 | 每 cycle 固定注入 problem.md + ROOT insight + vp 索引（`design/04 §5`） | 固定顺序注入 |
| 预算上限 + 丢最旧 | Worker 上下文上限：ancestor insights ≤ 8 层、vp 索引 ≤ 20 条（`design/06 §3`） | 超限压缩，保近祖先 |

**结论（同时修正原 L）**：可以"参考 codex"，但**不是照搬其对轨迹的有损摘要用到全部内容**。做法是——**分层**：数学权威内容（陈述/常数/依赖）走 codex 里"user 消息逐字保留"那条路，**无损**；探索轨迹 `result` 走 codex 里"轨迹摘要"那条路，压成 `insight`，**有损可接受**。

### 7.3 复用 prop 结构，减少 LLM 压缩调用（采纳 WIP 提案）

WIP 提案的关键观察正确并采纳：**每个 state 和父节点差异很小（就一个新 prop）**，因此**不必每次都调 LLM 压缩整个 state**：

- 平时：state = pool 视图 + `delta`（§2.1），靠"复用 prop 文件结构 + UUID"程序化聚合，**零 LLM 成本**。
- 仅当抽到 2 个节点做 critic（§5.2）时：找二者**公共祖先**，组装
  `[公共祖先视图] + [子节点 A 增量] + [子节点 B 增量]`
  交给长程规划部分（harness），由它按 §7.1 的 handoff 方式压缩后喂给 critic。这正是 WIP 提案原文的做法，且天然避免重复压缩公共部分。

---

## 8. 工程治理与冻结边界（原 D）

WIP 提案缺"如何在不破坏既有冻结边界下落地"的一段——**这是治理硬缺口，必须先解决**。

### 8.1 必须尊重的冻结项（`docs/orchestrator-harness-optimization-scope.md` 第三圈）

以下**默认禁止修改**，除非用户明确解冻：

- `src/alphasolve/solver/prompts/orchestrator.md`
- 第二层 `alphasolve.agent` 的任何东西
- `WorkerManager` 调度语义、worker 生命周期、`SpawnWorker` / `TaskOutput` 真实执行语义
- `RoleWorkspaceAccess` 权限边界与跨 worker 隔离
- generator / verifier / reviser / theorem_checker / curator 的 prompt 与 YAML

### 8.2 落地策略：在冻结边界"之外"实现

本方案绝大部分能力**不需要触碰**上述冻结项，落点如下：

| 能力 | 落点 | 是否触碰冻结项 |
|---|---|---|
| harness / reasoning 拆分（WIP 提案架构升级） | 复用**现有** `research_reviewer`，拆成"读上下文的 harness"+"纯数学 reasoning" 两个 subagent，二者作为 orchestrator 的 subagent | 否（`research_reviewer.md` / `.yaml` 在**第一圈允许改**） |
| 局部 pairwise critic | 新增独立 `critic` subagent（走正式 `ToolRegistry` 注册） | 否（新增，不改冻结项） |
| proxy 分级信号 / visit_count / 配额 / 退火 | orchestrator **调度层状态**（DAG 元数据、`design/01` schema 字段），不改 `orchestrator.md` prompt 语义 | 否（数据结构层，非 prompt/WorkerManager） |
| insight backprop / falsification | Curator 侧（若需改 curator prompt 则**需先解冻**，见下） | ⚠ 见 §8.3 |
| 上下文压缩 | harness subagent 内 + 节点 delta 存储 | 否 |

### 8.3 需要用户显式解冻才能做的部分（明确列出，不擅动）

- 若 insight backprop 需要修改 **curator 的 prompt / YAML**（把 result/insight 分离、横向兄弟合成写进 curator 职责）→ **属第三圈冻结，需用户批准**。
- 若硬证伪扫描需要 `WorkerManager` 或 worker 生命周期层面的钩子 → **需用户批准解冻**。

> 落地顺序上，先做不触碰冻结项的部分（§9 P0），把需要解冻的部分单独列成"解冻申请"提交用户，不擅自改动。

### 8.4 benchmark 与防过拟合（贯穿全程）

- 所有"是否比现状选得准"的评测，必须在**四个真实 workspace 快照**上离线跑（`docs/orchestrator-harness-benchmark-expected-behavior.md`）：`2d elastic wave gwp toy model` / `bmo optimization` / `real material blowup 1` / `real material blowup 2`；不得用复制/重命名/缩减的 fixture。
- 每轮改动需启动 subagent 审查：① 是否对特定数学领域过拟合；② 是否对"单轮推荐下一命题"过拟合而在真实研究场景失效。任一不通过则回退。
- proxy 分 / critic rubric **不得硬编码领域名词**（§3.1）。

---

## 9. 落地路径（P0 / P1 / P2）

### P0（低风险，不触碰冻结项）

1. **数据模型**（§2）：全局单 pool + 节点视图/增量 + `state_id`(UUID) + `delta`；图结构（`parents` list）。
2. **廉价 proxy 分级信号**（§3，**本期根因，最优先**）：`open_subgoals` 计数 + `verified_implications` 增量 + falsification 命中，每 cycle 可算，只做相对比较。
3. **insight backprop 骨架**（§6.1/§6.2）：result/insight 分离 + 半结构化 schema（先在 harness/`research_reviewer` 侧落，暂不改 curator prompt）。
4. **上下文压缩**（§7）：复用 prop 结构 + UUID + 公共祖先增量；权威无损 / 轨迹有损分层。
5. `theorem_checker` 保持唯一终止判据；暂不追求任何统计收敛。

### P1（谨慎验证，部分需解冻）

1. **局部 pairwise selection**（§5）：四值 critic（新增 subagent）+ 平局定案（comparable-tie +0.5 / incomparable 不入池）+ 反复平局止损。
2. **visit_count 全祖先回传 + 配额 + 自适应退火**（§5.4/§5.5）。
3. 在四个 benchmark 快照上**离线评测**"是否比现状选得准"，出对比数据。
4. 若涉及 curator prompt / WorkerManager 钩子 → 提交解冻申请（§8.3）。

### P2（依赖 P1 数据）

1. **crossover / 图合并**（§6.4）：必须先有 falsification 一致性 gate。
2. **数学硬证伪全局扫描**（§6.3，自研增强）。
3. 全局机制、熵度量（原 I）、监控指标闭环（原 K）——本期不做，后续再议。

---

## 10. 附：WIP 提案问题 → 改进映射表

| 编号 | WIP 提案问题 | 本方案改进 | 落点 |
|---|---|---|---|
| 0 | 命名 "MCTS" 但无 rollout / 状态不可逆 | 正名 tree/graph dueling bandit + best-first | §1 |
| 根因 | 只有二元 `theorem_checker`，缺 Arbor 的廉价分级评估器 | 造 proxy 分级信号（open_subgoals/impl 增量/falsif 命中） | §3 |
| 原 A | 采样极贵 + reward 极稀疏，UCB/Elo 统计前提不成立 | 不依赖统计收敛；pairwise 只做软偏置 + 每对比 ≤ K 次 | §5.1/§5.6 |
| 原 C | gap "离证完多远" 口径空 | 用结构化 open_subgoals，只相对不绝对，被 falsif 判死兜底 | §3/§6.2 |
| 原 B | Elo 全局线性化淹没冷门正解（note-4） | 只做局部同父兄弟 pairwise，不做全局 Elo | §5.1 |
| 原 B-平局 | 平局简单各 +1 / 随机选会污染信号 | 四值 critic；comparable-tie 各 +0.5 入池，incomparable 不入池走多样性保护 | §5.2/§5.3 |
| 原 E | 只回传胜率，漏 visit_count + 负证据 | visit_count 全祖先回传 + insight backprop 回传因果 | §5.4/§6 |
| 补 E | 不传"为什么不行" | result/insight 分离 + 横向兄弟合成 | §6 |
| 原 F | 固定 ε-greedy 开新 arm 要么发散要么早熟 | explore/exploit 硬配额 + progress_rate 自适应退火 | §5.5 |
| 原 G | crossover 无合并一致性保证 | 合并前过 falsification gate；数学硬证伪为自研增强 | §6.3/§6.4 |
| 原 H | 数据模型自相矛盾（整套 prop vs 只差一个 prop） | 全局单 pool + 节点视图/增量 + UUID | §2 |
| 原 D | 未说明如何不破坏冻结边界 | 在冻结外实现；需解冻部分单列申请 | §8 |
| 压缩 | "先抄袭 codex" | 分层：权威无损（=codex 逐字保留 user 消息）+ 轨迹有损（=codex 摘要），复用 prop 结构减少 LLM 调用 | §7 |

---

## 11. 实现落点（本次已落地，冻结边界之外）

按 §8.2/§9 P0 的"在冻结边界之外新增独立模块"策略，已落地一个**纯算法/数据结构**子包
`src/alphasolve/solver/search/`（不含任何 LLM 调用、不 import `agent`/`llm` 层、不触碰
`orchestrator.md` 与 `WorkerManager` 调度语义），并配套单元测试。需 LLM 的三处能力
（insight 抽象、pairwise critic、轨迹摘要）均以 `Protocol` 表达，接入点留待解冻后填。

| 设计章节 | 代码文件 | 关键 API |
|---|---|---|
| §2 数据模型 + §5.4 visit_count 全祖先回传 | `search/graph.py` | `SearchGraph` / `SearchNode` / `Delta`；`on_dispatch`（全祖先回传）、`materialize_view`（pool 视图增量聚合）、`common_ancestor`、`explore_bonus`、`prune` |
| §3 廉价分级信号 | `search/proxy.py` | `compute_proxy`（open_subgoals / verified_delta / falsif 命中，纯计数防过拟合）、`rank_by_proxy`、`progress_rate` |
| §5.2/§5.3 四值 critic + 平局定案 | `search/selection.py` | `DuelOutcome`（四值）、`record_duel`（tie 各 +0.5、incomparable 不入池）、`tie_break`、`DuelLedger`（§5.6 K 次止损） |
| §5.5 配额 + 退火 | `search/selection.py` | `allocate_quota`（硬配额，少派不凑数）、`anneal_exploit_fraction`、`select_diverse_fill`（Jaccard 多样性） |
| §6 insight backprop + falsification | `search/insight.py` | `InsightRecord`（半结构化 yaml，open_subgoals 承载 gap）、`backpropagate_insights`（横向兄弟逐层合成）、`apply_falsification`（硬砍，节点不删除） |
| §7 分层压缩 | `search/compaction.py` | `assemble_critic_view`（公共祖先增量，零 LLM）、`build_worker_context`（无损权威 + 有损轨迹）、`trim_*`（预算裁剪、丢最远） |
| §5.2 四值 critic prompt | `src/alphasolve/solver/prompts/pairwise_critic.md` | 新增文件，四值 rubric + 防过拟合，未改任何现有 role |

测试：`tests/test_search_{graph,insight,selection,compaction}.py`（29 passed）。

---

## 12. 接入 orchestrator（本次已落地，含最小解冻）

在 §11 纯算法内核之上，本轮把它接进真实 orchestrator 运行链路。接入尽量落在冻结边界之外，
只对 `orchestrator.md` 做**加法式**改动（唯一解冻项，只追加一节、不改任何现有段落）。

| 落点 | 文件 | 是否触碰冻结项 | 说明 |
|---|---|---|---|
| 调度层编排门面 | `search/session.py`（新增 `SearchSession`） | 否 | 把 worker 生命周期映射进 `SearchGraph`：`on_spawn`（建 depth-1 节点 + §5.4 dispatch 回传）、`on_worker_result`（回填 status/delta/最小 insight）、`frontier_ranking`/`quota`/`sibling_duel_candidates`/`advise`。纯调度状态，不 import agent/llm、对 worker 只读。 |
| critic subagent | `config/subagents/critic.yaml`（新增，指向已有 `pairwise_critic.md`） | 否（§8.2 明确允许新增 subagent 走正式注册） | 走标准 `subagents_dir` 加载，`suite.subagents["critic"]`，只读导航工具。 |
| orchestrator 装配 | `orchestrator.py`（仅改 `Orchestrator` 类，不动 `WorkerManager`） | 否 | 持有 `SearchSession`；`_spawn_tool`/`_wait_tool` 登记节点并把 `selection_advice` 作为 TaskOutput 的**只读附加字段**（沿用既有 `free_exploration_worker` 扩展模式）；`_build_registry` 的 Agent enum 加 `critic`；新增 `RecordDuel` 工具（新增工具，不改 SpawnWorker/TaskOutput 语义）回写四值胜率池。 |
| selection 引导 | `prompts/orchestrator.md`（**加法式追加一节**） | ⚠ 解冻（最小） | 新增 "Selection Advisory" 一节，说明 `selection_advice` 结构、如何用 `critic` 做同父兄弟 pairwise、用 `RecordDuel` 回写、按 `quota` 配置 hint。明确"advisory only，`theorem_checker` 仍是唯一终止判据"。未改动任何原有段落。 |

测试：`tests/test_search_session.py`（15 项）+ 原 search 测试 + 层边界测试，共 **46 passed**；
`load_agent_suite` 能发现 critic；orchestrator 导入与接入链路冒烟通过。

**已知结构性限制（诚实标注，见 `session.py` docstring）**：现有 orchestrator 由 LLM 用 `hint`
自主驱动 spawn，既不提供节点父子关系，worker 也不产出结构化 `InsightRecord`。因此本轮：
① 所有 worker 节点先作为 root 的 depth-1 兄弟登记（pairwise 语义成立，但暂无多层链/crossover）；
② `insight` 由 worker 结果 payload 最小合成（verified→一条 implication；informative failure→一条
open_subgoal 且不剪枝）。要得到 §6.2 富 insight（三分）、真实多层 parent、以及 §6.3 数学硬证伪
的自动触发，需要 worker/curator 侧协作产出结构化信号——属 §9 P2，仍待后续与用户确认后推进。
