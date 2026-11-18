# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 半截盖印收口（watermark-02 增量）

盖印断流后同一作业内部分运行码已落水印、部分仍待盖印，缺口对账扫出半截账。收口只许二选一，不允许两套命运并存：

- **整笔回滚（rollback）**：已落印段全部清回待盖印、抹掉水印值，作业标记 `整笔回滚`。
- **部分留存（retain）**：已落印段保留，对剩余待盖印码补齐同一水印，作业标记 `部分留存`。

要点：

- 收口在单事务（`BEGIN IMMEDIATE`）+ 进程级排他锁内一次做完；两名仓管同时点「继续盖印」，第二个请求 409，杜绝双写。
- 收口前后各核一次批次态与最近对账的应盖印 / 已盖印 / 缺口数，偏了报中文错误并停手，作业保持半截。
- 收口标记写入 `stamp_jobs`（库内可查），盖印卡片与缺口复跑台分别亮出回滚 / 留存标记。
- 页面入口：批次页多选运行码「制造半截盖印」→ 盖印卡片或缺口复跑台点「继续盖印」。

接口：`GET/POST /api/stamp-jobs`、`GET /api/stamp-jobs/{id}`、`POST /api/stamp-jobs/{id}/close`（body：`{"strategy":"rollback|retain"}`）。

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、跨表依赖冻结、追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
