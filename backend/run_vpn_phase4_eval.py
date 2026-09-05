"""Phase 4 —— VPN 诊断深度冻结样本评测 CLI（backend/run_vpn_phase4_eval.py）。

用法：
    .\\.venv\\Scripts\\python.exe -m backend.run_vpn_phase4_eval
    .\\.venv\\Scripts\\python.exe -m backend.run_vpn_phase4_eval --samples backend/vpn/eval/frozen_samples.json

行为：
    - 加载冻结样本集（默认 backend/vpn/eval/frozen_samples.json）；
    - 运行 rules.guardrail_evaluate 对每条样本确定性诊断；
    - 计算 6 项 Phase-4 指标并打印报告（JSON）；
    - 若任一门禁不通过（无证据自动回复>0 / 多人影响误判>0 / 分类<0.90 /
      假设识别<0.95 / 证据引用<1.00 / 高风险误自动>0）则退出码非 0。

重要声明：指标基于**本任务编写的冻结样本集**（真实/脱敏措辞），当前无真实生产语料库，
故输出数字代表确定性规则在该冻结集上的自洽性，**不构成生产级准确率**。
"""

from __future__ import annotations

import argparse
import json
import sys

from .vpn.eval_metrics import (
    DEFAULT_FROZEN_SAMPLES_PATH,
    load_frozen_samples,
    run_frozen_metrics,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4 VPN 诊断深度冻结样本评测")
    parser.add_argument(
        "--samples",
        default=DEFAULT_FROZEN_SAMPLES_PATH,
        help="冻结样本 JSON 路径（默认 %(default)s）",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="把报告写入该 JSON 文件（可选）",
    )
    args = parser.parse_args(argv)

    samples = load_frozen_samples(args.samples)
    report = run_frozen_metrics(samples)

    payload = {
        "dataset": args.samples,
        **report,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text)

    gates = report["gates"]
    failed = [name for name, gate in gates.items() if not gate["pass"]]
    if failed:
        print(f"\n[FAIL] 门禁未通过：{', '.join(failed)}", file=sys.stderr)
        return 1
    print("\n[PASS] 全部 Phase-4 门禁通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
