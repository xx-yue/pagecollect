# pagecollate

扫描件页序智能重排服务。

纸质材料扫描成 PDF 后，页面顺序往往是乱的（翻页、双面扫描、多次分批都会打乱）。
本服务把 PDF 交给多模态大模型读懂每页内容，推断正确的阅读顺序，
并通过**索引交换协议**把排序结果返回给客户端。

```
Qt 客户端  ──上传 PDF──▶  FastAPI 后端  ──逐页摘要 + 全局排序──▶  大模型
           ◀─返回整数数组─              （如 [3, 1, 2, 5, 4]）
```

## 核心设计

### 1. 两阶段判序（关键，不要改成单阶段）

**❌ 错误做法**：逐页把图片丢给模型问「这页该排第几」。
单页没有全局视野，模型只能瞎猜，结果不可用。

**✅ 正确做法**：

```
阶段 1（可并发）  每页 → 摘要  {"page_index": 3, "summary": "甲方签章页", ...}
                  ↓
阶段 2（单次调用） 把全部 N 条摘要一次性交给模型 → 输出完整排列 [3,1,2,5,4]
```

阶段 2 拿到全部页面摘要才有全局判断能力。
且阶段 1 的结果可缓存——内容相同的页（空白页、重复封面）能跳过重复调用。

### 2. 索引交换协议

后端不回传重排后的 PDF，只返回一个整数数组，客户端据此重排。

**三条铁律：**

| # | 铁律 | 原因 |
|---|---|---|
| 1 | **物理编号无意义** | `1..N` 只是扫描顺序，不携带语义。写进提示词、写进接口文档，否则后端同事很容易误以为"编号 1 就是第一页" |
| 2 | **上传响应必须回传 `page_count`，客户端核对** | 不一致意味着后续整数数组整体错位，必须立即中止而不是抱侥幸继续 |
| 3 | **后端绝不增删页，只重排** | 一旦引入"删除空白页""拆分跨页"，位置索引全面失效（届时升级为 UUID 方案） |

**双端校验**：`ordered_indices` 必须是 `1..N` 的合法全排列，
服务端校验（`OrderResult.validate_permutation`）、客户端也要再校验一次。

### 3. 成本开关：有文本层走纯文本

扫描仪若自带 OCR 文本层，抽取文本走纯文本模型，成本约为多模态的 1/10。
只有真正无文本层的页才渲染成图（150 dpi）。

### 4. 人工复核闭环

无监督排序没有标准答案，指望模型 100% 正确不现实。所以：

```
后端返回 ordered_indices + 每页摘要 + 置信度
        ↓
客户端按此顺序预览，低置信度页高亮
        ↓
用户拖拽微调（纯本地操作，不碰后端）
        ↓
POST /finalize 提交最终顺序 → 服务端出成品（客户端无需引入 PDF 库）
```

## 快速开始

```bash
# 1. 激活虚拟环境（本项目复用已配置好的 conda 环境 web_learn，依赖已装齐）
conda activate web_learn
#    在其他机器上重建依赖时执行：pip install -r requirements-dev.txt

# 2. 准备配置。首次体验保持 MOCK_LLM=true 即可，无需 API Key
cp .env.example .env

# 3. 生成一份「顺序被打乱」的示例 PDF
python scripts/make_sample_pdf.py

# 4. 启动服务
uvicorn app.main:app --reload

# 5. 打开交互式接口文档
#    http://127.0.0.1:8000/docs
```

**零成本验证全链路**：`MOCK_LLM=true` 时内置 mock 会读取示例 PDF 里的
`逻辑页序` 标记完成排序，可以直接验证「上传 → 排序 → 重建」是否打通。

跑测试：

```bash
pytest -v
```

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/tasks` | 上传 PDF，返回 `task_id` 与 `page_count`（202，不等结果） |
| `GET` | `/api/v1/tasks/{id}` | 查状态与进度（建议 3 秒轮询一次） |
| `GET` | `/api/v1/tasks/{id}/detail` | 排序结果 + 每页摘要 + 置信度（人工复核依据） |
| `POST` | `/api/v1/tasks/{id}/finalize` | 提交人工确认后的最终顺序 |
| `GET` | `/api/v1/tasks/{id}/result` | 下载成品 PDF |
| `DELETE` | `/api/v1/tasks/{id}` | 清理任务与落盘文件 |

状态机：`PENDING → PARSING → SUMMARIZING → ORDERING → REBUILDING → DONE / FAILED`

### 典型调用

```bash
# 上传
curl -X POST http://127.0.0.1:8000/api/v1/tasks \
     -F "file=@sample_scrambled.pdf"
# → {"task_id":"a1b2c3d4e5f6","page_count":5,"status":"PENDING","message":"已接收，共 5 页"}

# 轮询
curl http://127.0.0.1:8000/api/v1/tasks/a1b2c3d4e5f6
# → {"status":"SUMMARIZING","progress":3,"total":5,"message":"已分析 3/5 页", ...}

# 拉详情
curl http://127.0.0.1:8000/api/v1/tasks/a1b2c3d4e5f6/detail
# → {"ordered_indices":[2,4,1,5,3],"confidence":1.0,"pages":[...]}

# 下载成品
curl -o result.pdf http://127.0.0.1:8000/api/v1/tasks/a1b2c3d4e5f6/result
```

## 接入真实模型

改 `.env` 两项即可（走 OpenAI 兼容协议，换厂商不动代码）：

```bash
MOCK_LLM=false
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=qwen-vl-max
```

常见国内端点：

| 厂商 | Base URL |
|---|---|
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |
| 豆包 | `https://ark.cn-beijing.volces.com/api/v3` |

> 选型建议：扫描件多为中文，优先 `qwen-vl` 系列（中文文档场景成熟）。
> 上线前务必与厂商书面确认**数据不用于模型训练**。

## 配置说明

全部配置见 `.env.example`，几个关键的：

| 配置 | 默认 | 说明 |
|---|---|---|
| `MOCK_LLM` | `true` | 是否使用内置 mock |
| `LLM_CONCURRENCY` | `5` | 并发调用页数。受厂商 RPM 限制，建议 3~8 |
| `RENDER_DPI` | `150` | 渲染分辨率。150 足够，再高白烧 token |
| `TEXT_LAYER_MIN_CHARS` | `50` | 文本层阈值，超过走纯文本链路 |
| `ORDER_MAX_ATTEMPTS` | `3` | 排序非法时的重试次数 |
| `LOW_CONFIDENCE_THRESHOLD` | `0.7` | 低于此值标记「需人工复核」 |

## 部署注意

1. **必须单 worker**：`uvicorn app.main:app --workers 1`。
   任务状态存放在进程内存中，多 worker 会出现「上传打到 A 进程、轮询打到 B 进程」查不到任务。
   需要横向扩展时，把 `app/core/task_store.py` 换成 Redis 实现（接口已隔离好）。
2. **Nginx 上传体积**：`client_max_body_size` 默认 1M，100 页扫描件可能上百 MB，需调大。
3. **CORS**：`settings.cors_origins` 生产环境改成具体域名，不要留 `["*"]`。
4. **数据合规**：扫描件常含身份证、合同、病历。原文件落盘在 `storage/uploads/`，
   任务处理完客户端应调用 `DELETE` 清理；必要时加定时清理任务。
5. **敏感信息**：如业务允许，可在上传前对页面做敏感区域遮罩，但会影响判序准确率，需权衡。

## 项目结构

```
pagecollate/
├── app/
│   ├── main.py                 应用入口（lifespan / CORS / 路由挂载）
│   ├── config.py               全局配置（pydantic-settings 读 .env）
│   ├── api/v1/tasks.py         六个接口
│   ├── schemas/task.py         Pydantic 模型 + 全排列校验
│   ├── services/
│   │   ├── pdf_service.py      拆页 / 渲染 / 重建（省钱开关在这里）
│   │   ├── llm_service.py      模型调用（OpenAI 兼容 + mock）
│   │   └── reorder_service.py  四阶段流水线编排（核心）
│   ├── core/
│   │   ├── task_store.py       任务状态（内存，可换 Redis）
│   │   └── log_config.py       日志
│   └── prompts/reorder.py      提示词集中管理
├── scripts/make_sample_pdf.py  生成打乱顺序的示例 PDF
├── tests/test_reorder.py       离线测试（mock 模式）
└── storage/{uploads,results}/  落盘目录
```

## 后续可做的优化

- **页数超上下文**（>100 页）：分批摘要 + 归并排序（先分组各自排序，再对组边界做二次判定）
- **PDF 加密 / 权限**：当前未处理，需加解密后再解析
- **结果缓存**：按页面图片 hash 缓存摘要，跨任务复用（同一批材料常有重复页）
- **SSE 进度推送**：把轮询换成 `sse-starlette`，长任务体验更好
- **逐页置信度**：当前只有整体置信度，可让模型对每页单独打分
