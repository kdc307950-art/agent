# V1 回归记录

> 实际执行日：2026-09-03（命令输出 `date +%F`） · 目标发布日期：2026-09-12（计划）

## 后端

| 检查 | 结果 |
| --- | --- |
| `uv run ruff check backend src tests` | 通过 |
| `uv run mypy backend src` | 98 个文件无问题 |
| `uv run pytest -q` | **330 passed / 64 skipped**（本地未配置 TEST_DATABASE_URL/REDIS_URL） |
| 真实数据库评测 `run_ticket_eval --require-db` | **未执行**（无 PostgreSQL），`docs/evaluation/v1-report.md` 保持 N/A |
| 真实仓储生命周期 `test_ticket_lifecycle_postgres.py` | **未执行**（无 PostgreSQL，待 CI/本地 DB） |

## 前端

| 检查 | 结果 |
| --- | --- |
| `npm run lint`（oxlint） | 0 warning / 0 error |
| `npm run typecheck` | 通过 |
| `npm test -- --run`（Vitest 单测） | 30 passed |
| `npm run build` | 通过 |
| `npm run test:e2e:mock`（Playwright Mock） | **22 passed** |
| `npm run test:e2e:real` | 需 `E2E_WEB_BASE` + `E2E_API_TOKEN`，本次未配置 → **未执行**（决策点 2：简历写“Mock E2E 已覆盖”） |

## 演示与渠道

| 项 | 状态 |
| --- | --- |
| Docker / Compose | 本机 PATH 无 Docker，`docker compose` 未执行 → 决策点 1：**渠道功能从 V1 移除，只展示 Web 闭环** |
| 企业微信/钉钉真实闭环 | 未验证（代码保留，非 V1；自动化验签/解密/幂等测试已覆盖） |
| 真实模型生成建议 | 未验证（无真实模型单价/线上 key，报告中 P95/成本 N/A） |
| 演示视频 | 待录制 |

## 结论口径

- 「已验证」仅指：确定性单元/路由测试、Mock 前端 E2E、静态门禁与文档一致性。
- 「真实 API / 真实渠道 / 真实 DB 检索」均以 CI 与配置为准，未执行前一律不写进简历或报告结论。
