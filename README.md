# Agentic RAG Studio

一个支持持久化 PDF 知识库、历史对话恢复和多工具调用的 Agentic RAG 应用。

用户可以上传 PDF 并持续提问。Agent 会根据问题在 PDF 语义检索、Serper Web 搜索和 arXiv 论文检索之间选择工具。PDF 索引、会话元数据和 LangChain 消息会保存在本地，重启 FastAPI 或重新打开 Streamlit 后无需再次上传文档。

## 核心能力

- **Agent 工作流**：使用 LangGraph 构建 LLM 节点、ToolNode 与条件路由，形成“决策—工具执行—观察—继续决策”的循环。
- **完整 RAG 链路**：PDF 解析、递归文本切分、HuggingFace Embedding、Chroma 持久化和 Top-K 语义检索。
- **多工具调用**：PDF Retriever、Google Serper 和 arXiv 均封装为 LangChain Tool。
- **持久化会话**：SQLite 保存会话元数据与完整 LangChain 消息，Chroma 保存 PDF 向量索引。
- **启动恢复**：后端重启后按需重新加载 Chroma，并重建对应 Agent；前端自动恢复最近会话。
- **Context Harness**：在每次 LLM 调用前执行 token 预算、最近轮次保留、旧历史压缩和超大 Tool Result 截断，同时保证工具消息协议完整。
- **Execution Harness**：统一限制 Agent 步数、总超时与重试次数；同会话串行、不同会话并行，并返回可观测的 run 统计。
- **PDF Ingestion Pipeline**：流式限流、真实 PDF 结构校验、隔离建库、原子发布、失败清理和持久化索引状态。
- **前后端分离**：FastAPI 提供结构化接口，Streamlit 提供历史会话选择、PDF 上传和聊天界面。
- **评测与追踪**：包含 LangSmith tracing 配置和 RAGAS 离线评测脚本。
- **可靠性测试**：覆盖会话持久化、公开历史过滤、接口响应、前端恢复和工具异常处理。

## 系统架构

```text
┌──────────────────────── Streamlit ────────────────────────┐
│ 历史会话选择 │ PDF 上传 │ 对话恢复 │ Chat UI              │
└────────────────────────────┬───────────────────────────────┘
                             │ HTTP / JSON / multipart
┌────────────────────────────▼───────────────────────────────┐
│                         FastAPI                            │
│ /sessions │ /indexes/{file_id} │ /upload_pdf │ /chat      │
└────────────────────────────┬───────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────┐
│                  AgentSessionManager                       │
│ 会话创建 │ SQLite持久化 │ Agent按需恢复 │ 对话事务锁         │
└───────────────┬──────────────────────────┬─────────────────┘
                │                          │
        ┌───────▼────────┐         ┌──────▼─────────────────┐
        │ SQLite         │         │ Execution Harness     │
        │ 完整历史消息   │         │ 超时/步数/重试/并发   │
        └────────────────┘         └──────┬─────────────────┘
                                          │
                              ┌───────────▼───────────┐
                              │ LangGraph Agent      │
                              │ LLM ↔ ToolNode       │
                              └───────────┬───────────┘
                                          │ 每次LLM调用前
                              ┌───────────▼───────────┐
                              │ Context Harness      │
                              │ 预算/压缩/截断/统计  │
                              └───────────┬───────────┘
                                          │
                              ┌───────────▼───────────┐
                              │ PDF │ Web │ arXiv Tool│
                              └───────────┬───────────┘
                                          │
                              ┌───────────▼───────────┐
                              │ Chroma Vector Store  │
                              └───────────────────────┘
```

## 持久化设计

运行数据默认保存在 `.rag_workspace/`：

```text
.rag_workspace/
├── sessions.sqlite3          # 会话元数据和完整消息历史
├── indexes.sqlite3           # validating/indexing/ready/failed状态
├── .ingestion/               # 仅在上传和建库期间存在的临时目录
├── <file_hash>.pdf           # 按内容哈希保存的PDF
└── chroma_<file_hash>_<config_hash>/ # 模型/切分配置隔离的Chroma索引
```

相同 PDF 在 Embedding 与切分配置不变时会复用已有 Chroma 索引，避免重复切分和 Embedding。索引目录包含配置指纹，切换 Embedding 模型后会自动建立新索引，防止复用维度或语义空间不兼容的旧向量。会话会记录建库时使用的 Embedding 模型，因此配置升级后仍能正确恢复旧会话。

后端重启后不会一次性加载所有模型和索引。用户真正选择某个历史会话并提问时，`AgentSessionManager` 才会加载对应 Chroma、重新绑定工具并构建 Agent。

## PDF Ingestion Pipeline 设计

上传入口不会调用 `file.read()` 一次性复制整份文件，而是把二进制流交给 `DocumentIngestionPipeline`，按 64 KiB 分块计算大小和 SHA-256。默认最大 PDF 为 25 MiB，超过限制立即抛出 `UploadTooLargeError`，FastAPI 映射为 `413 Payload Too Large`。

管线不信任文件名和 multipart 的 Content-Type。文件必须同时满足：

1. 文件名扩展名为 `.pdf`。
2. 前 1,024 字节内存在 PDF 魔数 `%PDF-`。
3. `pypdf.PdfReader` 能解析页树，文档至少包含一页。
4. 文档未加密；扫描版虽是真实 PDF，但仍需 OCR 才能建立可检索文本索引。

校验成功后，状态按下面的状态机持久化到 `indexes.sqlite3`：

```text
validating ──成功──> indexing ──原子发布──> ready
     │                    │
     └────校验失败────────┴────建库失败────> failed
```

Chroma 首先在 `.ingestion/<operation_id>/index` 中构建，完整完成后才通过同一磁盘上的目录重命名发布为带文件内容 Hash 与索引配置指纹的正式目录。因此 Retriever 不会看到半成品索引，也不会把不同 Embedding 模型的索引混用。Windows 下 Chroma 会长期持有 SQLite/Rust 文件句柄，项目把建库放到短生命周期子进程；子进程退出并释放句柄后，父进程才执行原子重命名。普通异常和下次启动都会清理遗留 staging 目录，重启时仍处于 `validating/indexing` 的记录会转为带明确原因的 `failed`。

同一内容并发上传由 file-level lock 串行化；已经 `ready` 的索引直接加载并返回 `index_reused=true`。为兼容项目升级前的数据，管线还能识别原先 16 位短哈希索引，新建数据则保留完整 SHA-256。

成功上传会返回：

```json
{
  "session_id": "...",
  "file_id": "...",
  "file_name": "paper.pdf",
  "index_status": "ready",
  "index_reused": false,
  "size_bytes": 542706,
  "created_at": "...",
  "updated_at": "...",
  "turn_count": 0
}
```

也可以通过 `GET /indexes/{file_id}` 查询跨重启保存的索引状态。

## Context Harness 设计

### 为什么需要 Harness

SQLite 中保存的是**完整历史**，它用于页面恢复和数据追溯；发送给模型的是**工作上下文**，它必须受到 token 预算控制。两者不能混为一谈，否则长对话会不断增加成本和延迟，最终超过模型窗口。

DeepSeek 官方当前给 `deepseek-v4-flash` 标注的上下文长度是 1M tokens，但“模型最多能接收多少”不等于“应用每次应该发送多少”。本项目默认只使用 60K 输入工作预算，以控制成本、延迟和无关历史带来的注意力稀释。模型规格以 [DeepSeek 官方模型文档](https://api-docs.deepseek.com/quick_start/pricing/) 为准。

有效输入预算由以下公式确定：

```text
effective_input_budget = min(
    CONTEXT_INPUT_BUDGET_TOKENS,
    MODEL_CONTEXT_WINDOW_TOKENS
    - MAX_OUTPUT_TOKENS
    - CONTEXT_SAFETY_TOKENS
)
```

默认配置下：

```text
min(60,000, 1,000,000 - 4,096 - 4,096) = 60,000 tokens
```

其中安全余量用于容纳系统协议、工具 Schema 和 token 估算误差。Harness 使用 LangChain 的消息级近似计数，并采用更保守的中文字符系数；统计包含消息角色、Tool Call 和 `tool_call_id`。供应商 tokenizer 才是最终准确值，因此工程上需要“保守估算 + 安全余量”，而不是宣称本地估算完全精确。

### 压缩策略

每次 `llm.invoke()` 前，Harness 执行以下流程：

1. 未超过输入预算时，完整保留当前上下文。
2. 超过预算时，始终保留 System Prompt 和当前对话轮次。
3. 优先保留最近 `CONTEXT_RECENT_TURNS` 个完整轮次。
4. 更早的用户问题与最终回答压缩为确定性的 memory digest；内部 Tool 消息不会重复进入摘要。
5. 如果当前 PDF/Web 工具结果本身过大，只截断消息内容，不删除 `AI tool_call → ToolMessage` 配对，避免破坏 OpenAI 工具调用协议。
6. 完整消息仍写回 SQLite；压缩只影响当次模型输入，不会让前端历史丢失。

`POST /chat` 会返回本次 Harness 统计，Streamlit 也会显示 token 使用率、压缩消息数和截断消息数：

```json
{
  "answer": "...",
  "context": {
    "model_context_window_tokens": 1000000,
    "input_budget_tokens": 60000,
    "max_output_tokens": 4096,
    "safety_tokens": 4096,
    "estimated_tokens_before": 12450,
    "estimated_tokens_after": 12450,
    "compacted_messages": 0,
    "truncated_messages": 0,
    "strategy": "full"
  },
  "execution": {
    "run_id": "6296919d-12da-4b94-9cb2-34c39f58d150",
    "attempts": 1,
    "duration_ms": 3258.41,
    "max_graph_steps": 20
  }
}
```

## Execution Harness 设计

Context Harness 解决“模型看到什么”，Execution Harness 解决“Agent 被允许做什么”。所有 LangGraph 执行都通过一个小接口：

```python
result = execution_harness.run(
    session_id=session_id,
    agent=agent,
    messages=messages,
)
```

这个接口内部集中处理：

1. 通过 LangGraph `recursion_limit` 限制最大 graph steps，防止 Agent 在 LLM 与工具之间无限循环。
2. DeepSeek 连接失败、429 和 5xx 最多执行有限次数的指数退避重试。
3. 401、400、403 和模型不存在等配置错误立即失败，不进行无意义重试。
4. SDK 单请求超时默认为 60 秒；完整任务总超时默认为 120 秒。
5. 总超时发生后设置取消信号，已进入第三方同步调用的当前 attempt 依赖 SDK 超时退出，但不会再启动后续 attempt。
6. 同一个 `session_id` 的执行串行，避免两次请求从相同旧历史出发；不同会话使用不同锁，可以并行执行。
7. 只有成功结果才会写入 SQLite。超时、重试耗尽或步数耗尽不会污染持久化历史。

成功执行返回 `ExecutionReport`：

- `run_id`：每次 Agent run 的唯一标识。
- `attempts`：本轮实际尝试次数。
- `duration_ms`：排队、重试和 Agent 执行的总耗时。
- `max_graph_steps`：本轮使用的 LangGraph 步数预算。

FastAPI 将领域错误稳定映射为 HTTP 状态：

| 执行结果 | HTTP状态 | 含义 |
|---|---:|---|
| `ExecutionLimitError` | `422` | Agent超过graph-step预算 |
| `ExecutionTimeoutError` | `504` | 本轮超过总时间预算 |
| `ExecutionUnavailableError` | `503` | 网络、429或5xx重试耗尽 |
| `ExecutionConfigurationError` | `503` | 密钥、模型名或请求配置无效 |

## 技术栈

- Python 3.11+
- FastAPI / Pydantic / Uvicorn
- Streamlit
- LangChain / LangGraph / LangSmith
- DeepSeek OpenAI-Compatible API
- HuggingFace Sentence Transformers
- Chroma / SQLite
- Serper / arXiv / RAGAS

## 项目结构

```text
client/
└── app.py                     # Streamlit页面与历史会话恢复
server/
├── main.py                    # FastAPI路由
├── sessions.py                # 持久化会话模块
├── agent/
│   ├── graph.py               # LangGraph执行图
│   ├── context_harness.py     # 上下文预算、压缩与统计
│   ├── execution_harness.py   # 执行步数、超时、重试与并发
│   ├── state.py               # AgentState与消息Reducer
│   └── tools.py               # PDF/Web/arXiv工具
├── rag/
│   ├── loaders.py             # PDF解析
│   ├── embeddings.py          # 本地缓存优先的Embedding加载
│   ├── ingestion.py           # 上传校验、原子建库与状态管理
│   ├── index_worker.py        # 隔离的Chroma建库子进程
│   └── vectorstore.py         # Chroma创建与恢复
└── observability/
    └── langsmith.py           # LangSmith配置
evaluation/
├── benchmark_retrieval.py     # 自定义PDF检索基准
├── benchmark_engineering.py   # 上下文与并发工程基准
└── run_ragas.py               # RAGAS离线评测
```

## 本地运行

### 1. 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
```

也可以使用普通虚拟环境：

```bash
python -m venv .venv
pip install -e .
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，至少填写：

```env
DEEPSEEK_API_KEY=your_deepseek_api_key
SERPER_API_KEY=your_serper_api_key
```

不要把 `.env` 或真实 API Key 提交到 GitHub。

Context Harness 可选配置：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `MODEL_CONTEXT_WINDOW_TOKENS` | `1000000` | 模型上下文硬上限 |
| `CONTEXT_INPUT_BUDGET_TOKENS` | `60000` | 应用主动使用的最大输入预算 |
| `MAX_OUTPUT_TOKENS` | `4096` | 单次模型最大输出及预留 |
| `CONTEXT_SAFETY_TOKENS` | `4096` | 工具Schema、协议和估算误差余量 |
| `CONTEXT_SUMMARY_TOKENS` | `2048` | 旧历史memory digest预算 |
| `CONTEXT_RECENT_TURNS` | `4` | 超限时优先保留的最近完整轮次 |

Execution Harness 可选配置：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `AGENT_MAX_GRAPH_STEPS` | `20` | 单次Agent run最大LangGraph步数 |
| `AGENT_MAX_ATTEMPTS` | `2` | 可恢复错误的最大尝试次数 |
| `AGENT_RETRY_BASE_SECONDS` | `0.5` | 指数退避的初始等待时间 |
| `AGENT_EXECUTION_TIMEOUT_SECONDS` | `120` | 包含排队、退避和Agent循环的总超时 |
| `MODEL_REQUEST_TIMEOUT_SECONDS` | `60` | DeepSeek单次HTTP请求超时 |

PDF Ingestion Pipeline 可选配置：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `MAX_PDF_UPLOAD_BYTES` | `26214400` | 单个 PDF 最大字节数（默认 25 MiB） |

### 3. 启动 FastAPI

```bash
uvicorn server.main:app --host 127.0.0.1 --port 8001
```

### 4. 启动 Streamlit

另开一个终端：

```bash
streamlit run client/app.py --server.port 8501
```

访问 <http://127.0.0.1:8501>。

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/health` | 后端健康检查 |
| `GET` | `/sessions` | 按最近更新时间列出持久化会话 |
| `GET` | `/sessions/{session_id}/messages` | 获取可展示的用户/助手历史 |
| `GET` | `/indexes/{file_id}` | 查询持久化索引状态与失败原因 |
| `POST` | `/upload_pdf` | 上传PDF、复用或创建索引并创建会话 |
| `POST` | `/chat` | 恢复指定Agent并进行一轮对话 |

FastAPI 文档：<http://127.0.0.1:8001/docs>

## 测试

自动化测试不会访问 DeepSeek 或 Serper 真实接口：

```bash
python -m unittest discover -v
```

需要真实 API 的冒烟检查必须手动运行：

```bash
python test_deepseek.py
python test_tool.py
```

## 可复现实验

项目提供检索质量与工程可靠性两类可复现实验，结果写入 `evaluation/results/`。

检索实验使用真实 PDF、Embedding 和 Chroma，报告索引首建/内容 Hash 复用耗时、Hit@1/3/5、MRR@5 和检索延迟。运行时需传入 PDF 与对应 JSONL 数据集；每行包含 `id`、`query` 和能在 PDF 中定位的 `anchor`：

```bash
python -m evaluation.benchmark_retrieval \
  --pdf path/to/corpus.pdf \
  --dataset path/to/retrieval_cases.jsonl
```

工程实验完全离线，验证 60K Token 工作预算下的上下文压缩、最新问题保留、Tool Call/Tool Message 协议完整性，以及“同 Session 串行、跨 Session 并行”的执行策略：

```bash
python -m evaluation.benchmark_engineering
```

离线工程基准将 146 条、估算 69,037 tokens 的长会话压缩到 4,535 tokens，缩减 93.43%，同时保留最新问题和 Tool Call/Tool Message 协议完整性；4 个 100 ms 模拟请求的跨 Session 执行相对同 Session 串行达到 3.96 倍加速。并发数字仅用于验证锁语义，不代表真实 LLM 吞吐量。

检索数据集应与目标 PDF 独立维护，并避免从被评测答案直接复制查询文本。若要报告生成答案质量，还需要使用人工复核或独立 Judge 模型评测 Faithfulness、Answer Relevancy 等指标。

## 当前限制与路线图

- SQLite适合本地单机项目；多实例部署可替换为PostgreSQL或Redis。
- 当前Retriever使用向量Top-K检索，后续可加入BM25混合检索与Reranker。
- 当前PDF依赖可提取文本，扫描版PDF需要增加OCR。
- Context Harness 当前使用确定性memory digest；后续可加入基于精确usage校准的滚动语义摘要。
- 后续计划增加MCP工具接入、Docker部署和可复现的RAGAS对比报告。

## 项目来源

本项目基于 [IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit](https://github.com/IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit) 进行二次开发，新增了DeepSeek适配、持久化会话、历史恢复、PDF Ingestion Pipeline、索引复用、Context Harness、Execution Harness、前端重构、错误降级和自动化测试等功能。

发布衍生项目时请保留原项目来源，并遵守原仓库的许可证要求。
