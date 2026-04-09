#!/usr/bin/env python3
"""Cybotrade 5m strategy research with iterative search until target is met or budget is exhausted."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependencies. Install with: pip install -r research/requirements_cybotrade.txt"
    ) from exc

BARS_PER_YEAR_5M = 365 * 24 * 12


@dataclass
class Metrics:
    strategy: str
    config: dict
    bars: int
    trades: int
    win_rate: float
    avg_return: float
    total_return: float
    cagr: float
    sharpe_annualized: float
    max_drawdown: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default="bybit-linear|candle?interval=5m&symbol=BTCUSDT")
    parser.add_argument("--candles-csv", default=None, help="Optional existing candles CSV to skip API download.")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-01-01")
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-seconds", type=float, default=2.0)
    parser.add_argument("--min-cagr", type=float, default=0.30)
    parser.add_argument("--max-drawdown-limit", type=float, default=-0.20)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--research-rounds", type=int, default=10, help="Max adaptive research rounds.")
    parser.add_argument("--samples-per-round", type=int, default=40, help="Random configs per round.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--outdir", default="research/output")
    return parser.parse_args()


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "start_time": "ts",
        "timestamp": "ts",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    existing = {k: v for k, v in rename_map.items() if k in df.columns}
    df = df.rename(columns=existing)

    required = ["ts", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}. Existing: {list(df.columns)}")

    df = df[required].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if pd.api.types.is_numeric_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.dropna().sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def strategy_vwap_revert(df: pd.DataFrame, lookback: int, z_thr: float, rsi_low: int, rsi_high: int) -> pd.Series:
    out = df.copy()
    typical = (out["high"] + out["low"] + out["close"]) / 3
    out["vwap"] = (typical * out["volume"]).rolling(lookback).sum() / out["volume"].rolling(lookback).sum()
    out["z"] = (out["close"] - out["vwap"]) / out["close"].rolling(lookback).std()
    out["rsi"] = rsi(out["close"], 14)
    pos = pd.Series(0.0, index=out.index)
    pos[(out["z"] < -z_thr) & (out["rsi"] < rsi_low)] = 1.0
    pos[(out["z"] > z_thr) & (out["rsi"] > rsi_high)] = -1.0
    return pos.ffill().fillna(0.0)


def strategy_breakout(df: pd.DataFrame, chan: int, atr_len: int, vol_filter_len: int) -> pd.Series:
    out = df.copy()
    out["hh"] = out["high"].rolling(chan).max().shift(1)
    out["ll"] = out["low"].rolling(chan).min().shift(1)
    out["atr"] = (out["high"] - out["low"]).rolling(atr_len).mean()
    out["vol_ok"] = out["atr"] > out["atr"].rolling(vol_filter_len).median()
    pos = pd.Series(0.0, index=out.index)
    pos[(out["close"] > out["hh"]) & out["vol_ok"]] = 1.0
    pos[(out["close"] < out["ll"]) & out["vol_ok"]] = -1.0
    return pos.ffill().fillna(0.0)


def strategy_ema_pullback(df: pd.DataFrame, fast: int, slow: int, pullback_thr: float) -> pd.Series:
    out = df.copy()
    ema_fast = out["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = out["close"].ewm(span=slow, adjust=False).mean()
    dist = (out["close"] - ema_fast) / out["close"]

    pos = pd.Series(0.0, index=out.index)
    pos[(ema_fast > ema_slow) & (dist < -pullback_thr)] = 1.0
    pos[(ema_fast < ema_slow) & (dist > pullback_thr)] = -1.0
    return pos.ffill().fillna(0.0)


def evaluate(df: pd.DataFrame, pos: pd.Series, fee_bps: float, name: str, config: dict) -> Metrics:
    ret = df["close"].pct_change().fillna(0.0)
    trades = pos.diff().abs().fillna(0.0)
    strat_ret = pos.shift(1).fillna(0.0) * ret - trades * (fee_bps / 10_000)
    equity = (1 + strat_ret).cumprod()

    dd = equity / equity.cummax() - 1
    n_bars = len(strat_ret)
    years = max(n_bars / BARS_PER_YEAR_5M, 1e-9)
    final_equity = float(equity.iloc[-1]) if n_bars else 1.0
    cagr = final_equity ** (1 / years) - 1 if final_equity > 0 else -1.0

    sharpe = 0.0
    if strat_ret.std() > 0:
        sharpe = (strat_ret.mean() / strat_ret.std()) * np.sqrt(BARS_PER_YEAR_5M)

    trade_mask = trades > 0
    trade_rets = strat_ret[trade_mask]
    return Metrics(
        strategy=name,
        config=config,
        bars=n_bars,
        trades=int(trade_mask.sum()),
        win_rate=float((trade_rets > 0).mean()) if len(trade_rets) else 0.0,
        avg_return=float(strat_ret.mean()) if n_bars else 0.0,
        total_return=float(final_equity - 1),
        cagr=float(cagr),
        sharpe_annualized=float(sharpe),
        max_drawdown=float(dd.min()) if n_bars else 0.0,
    )


def format_api_error(exc: Exception) -> str:
    msg = str(exc)
    lower = msg.lower()
    if "api key" in lower and ("invalid" in lower or "permission" in lower):
        return (
            f"Datasource API authentication failed: {msg}\n"
            "- Ensure API key is passed (--api-key or API_KEY env).\n"
            "- Ensure this API key has datasource permissions."
        )
    if "auth server unavailable" in lower or "connection timeout" in lower:
        return (
            f"Datasource connectivity issue: {msg}\n"
            "- Retry later, or switch DNS to 8.8.8.8 / 1.1.1.1.\n"
            "- If blocked by region, test via VPN."
        )
    return f"Datasource request failed: {msg}"


async def fetch_candles(topic: str, start: datetime, end: datetime, api_key: str, max_retries: int, retry_seconds: float) -> pd.DataFrame:
    try:
        import cybotrade_datasource
    except ImportError as exc:
        raise SystemExit(
            "cybotrade-datasource is required (Python >=3.12). Install with: pip install -r research/requirements_cybotrade.txt"
        ) from exc

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            data = await cybotrade_datasource.query_paginated(
                api_key=api_key,
                topic=topic,
                start_time=start,
                end_time=end,
            )
            return pd.DataFrame(data)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                print(f"Attempt {attempt}/{max_retries} failed. Retrying in {retry_seconds}s...")
                await asyncio.sleep(retry_seconds)

    assert last_exc is not None
    raise SystemExit(format_api_error(last_exc))


def split_train_test(df: pd.DataFrame, train_ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not 0.5 <= train_ratio < 0.95:
        raise SystemExit("--train-ratio should be in [0.5, 0.95).")
    cut = int(len(df) * train_ratio)
    if cut < 300:
        raise SystemExit("Not enough bars for optimization. Increase date range.")
    return df.iloc[:cut].reset_index(drop=True), df.iloc[cut:].reset_index(drop=True)


def base_grid() -> list[tuple[str, dict]]:
    configs: list[tuple[str, dict]] = []
    for lookback, z_thr, rsi_low, rsi_high in itertools.product([16, 24, 32], [1.0, 1.2, 1.5], [30, 35], [65, 70]):
        configs.append(("vwap_reversion", {"lookback": lookback, "z_thr": z_thr, "rsi_low": rsi_low, "rsi_high": rsi_high}))
    for chan, atr_len, vol_filter_len in itertools.product([16, 20, 24], [10, 14, 20], [72, 120, 180]):
        configs.append(("vol_breakout", {"chan": chan, "atr_len": atr_len, "vol_filter_len": vol_filter_len}))
    for fast, slow, pb in itertools.product([8, 12, 16], [32, 48, 64], [0.0015, 0.0025, 0.0035]):
        if fast < slow:
            configs.append(("ema_pullback", {"fast": fast, "slow": slow, "pullback_thr": pb}))
    return configs


def mutate_config(name: str, cfg: dict, rnd: random.Random) -> dict:
    new_cfg = dict(cfg)
    if name == "vwap_reversion":
        new_cfg["lookback"] = int(np.clip(new_cfg["lookback"] + rnd.randint(-4, 4), 8, 48))
        new_cfg["z_thr"] = float(np.clip(new_cfg["z_thr"] + rnd.uniform(-0.2, 0.2), 0.6, 2.0))
        new_cfg["rsi_low"] = int(np.clip(new_cfg["rsi_low"] + rnd.randint(-3, 3), 20, 45))
        new_cfg["rsi_high"] = int(np.clip(new_cfg["rsi_high"] + rnd.randint(-3, 3), 55, 80))
    elif name == "vol_breakout":
        new_cfg["chan"] = int(np.clip(new_cfg["chan"] + rnd.randint(-3, 3), 8, 64))
        new_cfg["atr_len"] = int(np.clip(new_cfg["atr_len"] + rnd.randint(-3, 3), 6, 40))
        new_cfg["vol_filter_len"] = int(np.clip(new_cfg["vol_filter_len"] + rnd.randint(-20, 20), 40, 240))
    elif name == "ema_pullback":
        new_cfg["fast"] = int(np.clip(new_cfg["fast"] + rnd.randint(-2, 2), 4, 24))
        new_cfg["slow"] = int(np.clip(new_cfg["slow"] + rnd.randint(-8, 8), 20, 120))
        if new_cfg["slow"] <= new_cfg["fast"]:
            new_cfg["slow"] = new_cfg["fast"] + 8
        new_cfg["pullback_thr"] = float(np.clip(new_cfg["pullback_thr"] + rnd.uniform(-0.0008, 0.0008), 0.0005, 0.01))
    return new_cfg


def gen_positions(df: pd.DataFrame, strategy_name: str, cfg: dict) -> pd.Series:
    if strategy_name == "vwap_reversion":
        return strategy_vwap_revert(df, **cfg)
    if strategy_name == "vol_breakout":
        return strategy_breakout(df, **cfg)
    if strategy_name == "ema_pullback":
        return strategy_ema_pullback(df, **cfg)
    raise ValueError(f"Unknown strategy: {strategy_name}")


def iterative_search(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    fee_bps: float,
    min_cagr: float,
    max_drawdown_limit: float,
    rounds: int,
    samples_per_round: int,
    seed: int,
) -> pd.DataFrame:
    rnd = random.Random(seed)
    current_candidates = base_grid()
    history: list[dict] = []

    for round_id in range(1, rounds + 1):
        train_rows: list[dict] = []
        for name, cfg in current_candidates:
            metrics = evaluate(train_df, gen_positions(train_df, name, cfg), fee_bps, name, cfg)
            row = asdict(metrics)
            row["round"] = round_id
            train_rows.append(row)

        ranked_train = pd.DataFrame(train_rows).sort_values(["cagr", "sharpe_annualized"], ascending=False)
        top_train = ranked_train.head(min(20, len(ranked_train)))

        round_test_rows: list[dict] = []
        for _, row in top_train.iterrows():
            name = row["strategy"]
            cfg = row["config"]
            test_m = evaluate(test_df, gen_positions(test_df, name, cfg), fee_bps, name, cfg)
            rec = asdict(test_m)
            rec["round"] = round_id
            rec["meets_target"] = rec["cagr"] >= min_cagr and rec["max_drawdown"] >= max_drawdown_limit
            round_test_rows.append(rec)

        ranked_test = pd.DataFrame(round_test_rows).sort_values(["meets_target", "cagr", "sharpe_annualized"], ascending=False)
        history.extend(ranked_test.to_dict(orient="records"))

        if bool(ranked_test.iloc[0]["meets_target"]):
            print(f"Target met in round {round_id}.")
            break

        # Continue researching: mutate top configurations and resample.
        parents = [(row["strategy"], row["config"]) for _, row in ranked_test.head(5).iterrows()]
        next_candidates: list[tuple[str, dict]] = parents.copy()
        while len(next_candidates) < samples_per_round:
            pname, pcfg = rnd.choice(parents)
            next_candidates.append((pname, mutate_config(pname, pcfg, rnd)))
        current_candidates = next_candidates

    all_test = pd.DataFrame(history).sort_values(["meets_target", "cagr", "sharpe_annualized"], ascending=False)
    return all_test


def load_or_fetch_candles(args: argparse.Namespace) -> pd.DataFrame:
    if args.candles_csv:
        return ensure_columns(pd.read_csv(args.candles_csv))

    api_key = args.api_key or os.environ.get("API_KEY")
    if not api_key:
        raise SystemExit("Missing API key. Set API_KEY or provide --api-key.")

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    if end <= start:
        raise SystemExit("Invalid time range: --end must be greater than --start")

    raw = asyncio.run(fetch_candles(args.topic, start, end, api_key, args.max_retries, args.retry_seconds))
    return ensure_columns(raw)


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    candles = load_or_fetch_candles(args)
    candles.to_csv(outdir / "candles_5m.csv", index=False)

    train_df, test_df = split_train_test(candles, args.train_ratio)
    test_results = iterative_search(
        train_df,
        test_df,
        args.fee_bps,
        args.min_cagr,
        args.max_drawdown_limit,
        args.research_rounds,
        args.samples_per_round,
        args.seed,
    )

    test_results.to_csv(outdir / "iterative_test_results.csv", index=False)
    qualified = test_results[test_results["meets_target"] == True].copy()  # noqa: E712
    qualified.to_csv(outdir / "qualified_strategies.csv", index=False)

    if qualified.empty:
        print("No strategy met target after all rounds.")
    else:
        best = qualified.iloc[0].to_dict()
        (outdir / "best_strategy.json").write_text(json.dumps(best, indent=2), encoding="utf-8")
        print("Best qualified strategy:")
        print(json.dumps(best, indent=2))

    print("Saved outputs:")
    print(f"- {outdir / 'candles_5m.csv'}")
    print(f"- {outdir / 'iterative_test_results.csv'}")
    print(f"- {outdir / 'qualified_strategies.csv'}")


if __name__ == "__main__":
    main()
