/**
 * Realistic backtest for chart-signal strategies (used by the Strategy page "Backtest" button).
 *
 * Mirrors strategy/futures_signal_trader.py + backtest/btc_signal_strategy_report.py:
 *  - Entry: market order at the OPEN of the candle after the signal candle
 *    (sma_vol_breakout: stop fill at the breakout level on the signal candle, or its open on a gap).
 *  - Exits: the signal's SL / TP. If one candle touches both, SL is assumed first (worst case).
 *    A gap through the SL fills at the open. No time exit.
 *  - Fees: taker 0.05% + 18% GST on entry and exit, plus 1bp slippage per side.
 *  - Strategies in ONE_POSITION_KEYS hold one position at a time (signals during a trade are
 *    skipped, as the live trader does); others count every signal, like the live trader.
 *  - Max drawdown = largest peak-to-trough fall of the closed-trade equity curve.
 */
export const ONE_POSITION_KEYS = new Set(['ema_trendline']);
export const FEE_RATE = 0.0005 * 1.18;
export const SLIPPAGE = 0.0001;

export function runSignalBacktest(candles, signals, { signalKey, lots, lotSize, onePosition }) {
  const qty = lots * lotSize;
  const single = onePosition ?? ONE_POSITION_KEYS.has(signalKey);
  const stopEntry = signalKey === 'sma_vol_breakout';
  const idxOf = new Map(candles.map((c, i) => [c.t, i]));
  const trades = [];
  let busyUntil = -1, skipped = 0;

  for (const s of signals) {
    const si = idxOf.has(s.time) ? idxOf.get(s.time) : candles.findIndex(c => c.t >= s.time);
    if (si < 0) continue;
    const buy = s.type === 'buy';
    const ei = stopEntry ? si : si + 1;                      // entry candle
    if (ei >= candles.length) { trades.push({ ...s, outcome: 'pending', pnl: 0, gross: 0, fees: 0 }); continue; }
    if (single && ei <= busyUntil) { skipped++; continue; }
    const o = candles[ei].o;
    const rawEntry = stopEntry ? (buy ? Math.max(o, s.price) : Math.min(o, s.price)) : o;
    const entry = buy ? rawEntry * (1 + SLIPPAGE) : rawEntry * (1 - SLIPPAGE);

    let outcome = 'open', exitRaw = null, xi = candles.length - 1;
    if (buy ? rawEntry <= s.sl : rawEntry >= s.sl) { outcome = 'sl'; exitRaw = rawEntry; xi = ei; }
    else {
      for (let i = ei; i < candles.length; i++) {
        const c = candles[i];
        if (buy) {
          if (c.l <= s.sl) { outcome = 'sl'; exitRaw = i > ei ? Math.min(s.sl, c.o) : s.sl; xi = i; break; }
          if (c.h >= s.tp1) { outcome = 'tp'; exitRaw = s.tp1; xi = i; break; }
        } else {
          if (c.h >= s.sl) { outcome = 'sl'; exitRaw = i > ei ? Math.max(s.sl, c.o) : s.sl; xi = i; break; }
          if (c.l <= s.tp1) { outcome = 'tp'; exitRaw = s.tp1; xi = i; break; }
        }
      }
    }
    const lastPx = candles[candles.length - 1].c;
    const xRaw = exitRaw ?? lastPx;                          // open trades marked at the last price
    const exit = buy ? xRaw * (1 - SLIPPAGE) : xRaw * (1 + SLIPPAGE);
    const gross = (buy ? exit - entry : entry - exit) * qty;
    const fees = (entry + exit) * qty * FEE_RATE;
    trades.push({ ...s, entry, exit, outcome, gross, fees, pnl: gross - fees,
                  entryTime: candles[ei].t, exitTime: candles[xi].t });
    busyUntil = outcome === 'open' ? candles.length : xi;
  }

  const closedTrades = trades.filter(t => t.outcome === 'tp' || t.outcome === 'sl');
  let eq = 0, peak = 0, maxDD = 0;
  for (const t of [...closedTrades].sort((a, b) => a.exitTime - b.exitTime)) {
    eq += t.pnl; peak = Math.max(peak, eq); maxDD = Math.max(maxDD, peak - eq);
  }
  const sum = (arr, k) => arr.reduce((a, t) => a + t[k], 0);
  const wins = closedTrades.filter(t => t.pnl > 0).length;
  const winsGross = sum(closedTrades.filter(t => t.pnl > 0), 'pnl');
  const lossGross = -sum(closedTrades.filter(t => t.pnl <= 0), 'pnl');
  return {
    trades, skipped, onePosition: single,
    total: trades.length, closed: closedTrades.length,
    wins, losses: closedTrades.length - wins,
    open: trades.filter(t => t.outcome === 'open').length,
    gross: sum(closedTrades, 'gross'), fees: sum(closedTrades, 'fees'),
    totalPnl: sum(closedTrades, 'pnl'),
    openPnl: sum(trades.filter(t => t.outcome === 'open'), 'pnl'),
    maxDD, profitFactor: lossGross > 0 ? winsGross / lossGross : null,
  };
}
