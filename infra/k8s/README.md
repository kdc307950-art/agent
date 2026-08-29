# K8s 部署配置（模板）

> ⚠️ **模板限制（不得当作可直接生产部署的文件）**：
> - 镜像地址 `ghcr.io/your-org/langgraph-agent` 与 digest 占位 **必须替换**；
> - `Secret langgraph-runtime` 需接入 Secret Manager（External Secrets / SOPS），
>   **禁止**在 YAML 或 git 中写密钥；
> - Ingress TLS 由实际网关/证书管理器提供，host 需替换；
> - PostgreSQL / Redis / OIDC issuer 地址需按环境替换（base 中未写死连接地址，
>   全部来自 `Secret langgraph-runtime` 的 `database-url` 等键）；
> - NetworkPolicy 的 Ingress 标签、出站端口需按集群实际拓扑核对。

## 目录

```text
infra/k8s/
├── base/                     # 公共形态：Deployment、Service、CronJob、迁移 Job
├── overlays/
│   ├── dev/                  # 开发覆盖：dev 镜像 tag、AUTH_MODE=dev、auto_setup
│   └── prod/                 # 生产覆盖：digest、OIDC/Redis、只读根 FS、Ingress、
│                             #   NetworkPolicy、PDB、独立迁移 Job
└── README.md                 # 本文件
```

## 使用（需 kubectl/kustomize，本机未验证）

```bash
# 校验（服务器端 dry-run 需集群；本机无集群时用 kustomize build 本地渲染）
kubectl diff -k infra/k8s/overlays/prod
kubectl apply --server-side --dry-run=server -k infra/k8s/overlays/prod

# 渲染检查（无需集群）
kubectl kustomize infra/k8s/overlays/prod
```

## 生产 overlay 覆盖项

- 私有镜像地址 + 固定 image digest（不可变部署）；
- 生产强制 env（OIDC、Redis fail-closed、无 auto_setup）；
- securityContext：非 root（10001）、`readOnlyRootFilesystem: true`、
  `allowPrivilegeEscalation: false`、drop ALL capabilities；
- readiness/liveness probes（base 已有，production 语义见 /readyz）；
- CPU/内存 requests/limits（base 已有）；
- Ingress + TLS、NetworkPolicy、PodDisruptionBudget；
- 独立一次性 migration Job（升级前执行）。

## 与 CI 的关系

CI 会构建镜像并验证 `Dockerfile` 可构建（`docker build ... -t langgraph-agent:ci`），
但**不会**对这份 K8s 配置做 apply 验证——因此它仍是模板，实际可用性需在目标集群
验证后再宣称。
