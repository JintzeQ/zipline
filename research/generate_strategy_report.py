#!/usr/bin/env python3
"""Generate a one-click markdown/json report from iterative strategy search results."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="research/output/iterative_test_results.csv",
        help="CSV produced by cybotrade_5m_scalping_research.py",
    )
    parser.add_argument("--output-dir", default="research/output", help="Where report files are written")
    parser.add_argument("--top-n", type=int, default=10, help="Top rows to include in markdown report")
    return parser.parse_args()


def parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "t", "yes", "y"}


def parse_float(raw: str, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def parse_int(raw: str, default: int = 0) -> int:
    try:
        return int(float(raw))
    except Exception:
        return default


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")

    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "round": parse_int(row.get("round", "0")),
                    "strategy": row.get("strategy", "unknown"),
                    "config": row.get("config", "{}"),
                    "cagr": parse_float(row.get("cagr", "0")),
                    "max_drawdown": parse_float(row.get("max_drawdown", "0")),
                    "sharpe_annualized": parse_float(row.get("sharpe_annualized", "0")),
                    "win_rate": parse_float(row.get("win_rate", "0")),
                    "trades": parse_int(row.get("trades", "0")),
                    "meets_target": parse_bool(row.get("meets_target", "false")),
                }
            )
    return rows


def rank_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda r: (r["meets_target"], r["cagr"], r["sharpe_annualized"], -r["max_drawdown"]),
        reverse=True,
    )


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def build_markdown(rows: list[dict], top_n: int) -> str:
    qualified = [r for r in rows if r["meets_target"]]
    top = rows[:top_n]

    lines = [
        "# 策略研究一鍵報告",
        "",
        f"- 總候選數: **{len(rows)}**",
        f"- 達標策略數: **{len(qualified)}**",
        "",
    ]

    if qualified:
        best = qualified[0]
        lines.extend(
            [
                "## 最佳達標策略",
                "",
                f"- Strategy: `{best['strategy']}`",
                f"- Round: `{best['round']}`",
                f"- CAGR: **{pct(best['cagr'])}**",
                f"- Max Drawdown: **{pct(best['max_drawdown'])}**",
                f"- Sharpe: **{best['sharpe_annualized']:.3f}**",
                f"- Win Rate: **{pct(best['win_rate'])}**",
                f"- Trades: **{best['trades']}**",
                f"- Config: `{best['config']}`",
                "",
            ]
        )
    else:
        lines.extend(["## 結論", "", "目前沒有策略達標（CAGR/回撤門檻）。", ""])

    lines.extend(
        [
            f"## Top {len(top)} 候選",
            "",
            "| Rank | Round | Strategy | CAGR | Max DD | Sharpe | Win Rate | Meets Target |",
            "|---:|---:|---|---:|---:|---:|---:|:---:|",
        ]
    )

    for i, row in enumerate(top, 1):
        lines.append(
            f"| {i} | {row['round']} | {row['strategy']} | {pct(row['cagr'])} | {pct(row['max_drawdown'])} | {row['sharpe_annualized']:.3f} | {pct(row['win_rate'])} | {'✅' if row['meets_target'] else '❌'} |"
        )

    lines.append("")
    return "\n".join(lines)


def build_summary(rows: list[dict]) -> dict:
    qualified = [r for r in rows if r["meets_target"]]
    best = qualified[0] if qualified else (rows[0] if rows else None)
    return {
        "total_candidates": len(rows),
        "qualified_candidates": len(qualified),
        "best_candidate": best,
    }


def main() -> None:
    args = parse_args()
    rows = load_rows(Path(args.input))
    if not rows:
        raise SystemExit("Input CSV contains no rows.")

    ranked = rank_rows(rows)
    summary = build_summary(ranked)
    markdown = build_markdown(ranked, args.top_n)

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    md_path = outdir / "strategy_report.md"
    json_path = outdir / "strategy_report_summary.json"

    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Saved report: {md_path}")
    print(f"Saved summary: {json_path}")
    print("Top candidate snapshot:")
    print(json.dumps(summary["best_candidate"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
