# BidVault v2 · 标书工作台

Windows 单机标书生产管理系统：本地文档/素材/项目管理 + 云端 AI 辅助（默认智谱 GLM-4-Flash，可自配其他 OpenAI 兼容服务）。

## 功能总览

- **项目库 / 文档库 / 素材库**：招标与投标文件导入、拆解、检索（全文 + 向量混合）
- **拆解规则中心**：锚点 + 表头特征规则可配置、可调序、带版本快照；规则试运行（Preview）所见即所得
- **图片证据链**：图片素材 OCR 识别、溯源标注、图文原位预览
- **标书工作区**：项目工作区骨架、历史资料导入、素材引用闭环、章节级精准插入（OOXML 级，含锚点校验与写入自愈）
- **AI 能力**：大模型结构化抽取、AI 草稿提案（人工确认后写入）、废标风险检查
- **交付形态**：WebView2 薄壳 + 本机 FastAPI，用户数据与程序目录分离

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12+ · FastAPI · SQLAlchemy · SQLite（sqlite-vec 向量检索） |
| 文档处理 | python-docx · PyMuPDF · openpyxl · PaddleOCR（可选离线扩展） |
| 前端 | Vue 3 · TypeScript · Vite |
| AI 网关 | OpenAI 兼容协议（默认智谱 GLM，Key 全配置化，不落明文） |

## 目录结构

```
backend/    # FastAPI 后端（app/routers 接口层 · app/services 业务层 · app/workers 队列）
frontend/   # Vue 3 前端
原型/       # 系统原型 HTML（步骤 4 产物）
架构/       # 功能/产品/数据架构图
视觉稿/     # 视觉方案
*.md        # 需求 / PRD / 架构草案 / 开发规范 / 拆解规则契约 / 状态看板
```

## 快速开始（开发环境）

### 后端

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -e .
copy ..\.env.example ..\.env      # 填入 ZHIPU_API_KEY（可选，不填则 AI 功能静默降级）
uvicorn app.main:app --port 8000
```

访问 http://127.0.0.1:8000/ 。数据默认落在 `./data`（可用环境变量 `DATA_DIR` 指定），不入版本库。

### 前端

```bash
cd frontend
npm install
npm run dev
```

### 运行测试

```bash
cd backend
.venv/Scripts/python -m pytest tests/ -q
```

## 配置说明

复制 `.env.example` 为 `.env` 后按需填写（`.env` 已被 .gitignore 排除，永不入库）：

| 变量 | 说明 |
|---|---|
| `ZHIPU_API_KEY` | 智谱开放平台 Key（GLM-4-Flash / GLM-4V-Flash 免费额度即可运行）；也可在应用「设置 → 模型服务」里自配任意 OpenAI 兼容服务 |
| `ZHIPU_BASE_URL` | 默认 `https://open.bigmodel.cn/api/paas/v4` |
| `DATA_DIR` | 数据目录，默认 `./data` |

AI Key 存放于系统凭据管理器，不写明文文件、接口不回显。

## 分支说明

- `main`：仓库基线
- `feat/prototype`：主开发线（最新）
- `dev`：开发基线
- `release/rule-center-v1`：拆解规则中心发布线

## 文档索引

- [01-需求整理.md](01-需求整理.md) — 需求六要素与边界
- [标库-PRD.md](标库-PRD.md) — 产品需求（功能点 / 页面 / 里程碑判据）
- [06-架构草案.md](06-架构草案.md) — 架构决策与变更台账
- [标库-拆解规则契约.md](标库-拆解规则契约.md) — 拆解规则中心契约
- [标库-STATUS.md](标库-STATUS.md) — 进度看板与决策台账

## 许可证

暂未开源授权，保留所有权利。未经许可请勿商用。
