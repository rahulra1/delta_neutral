import React, { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../api';
import { StrategySelector } from '../components/StrategyInfoCard';
import StrategyTemplate from '../components/StrategyTemplate';

// Keep in sync with api/chart.py calc_candlestick_patterns and strategy/futures_signal_trader.py.
const CANDLE_PATTERN_DETAILS = [
  { heading: '1. Patterns', points: [
    'Bullish Engulfing: a red candle, then a bigger green body that covers it → buy.',
    'Bearish Engulfing: a green candle, then a bigger red body that covers it → sell.',
    'Hammer: green candle, lower wick > 2× body, tiny upper wick → buy.',
    'Shooting Star: red candle, upper wick > 2× body, tiny lower wick → sell.',
  ] },
  { heading: '2. Filters', points: [
    'Trend: buy patterns only when the candle closes above the 100 EMA; sell patterns only below it.',
    'Volume: the pattern candle must trade at least 1.5× its 20-candle average volume.',
    'Uses closed candles only; 4H candles and volume come from Delta (the market we trade).',
  ] },
  { heading: '3. Entry, stop & target', points: [
    'Market order at about the next candle open.',
    'SL = 1.5 × ATR(14) from the close — room for normal 4H noise (the old candle-low stop was hit too often).',
    'TP = 2× the risk. No time exit.',
  ] },
  { heading: '4. Live trade handling', points: [
    'Checks every 30 s; closes at market when the mark price crosses the SL or TP.',
    'One trade at a time: signals while a trade is open are skipped, not taken later.',
  ] },
  { heading: '5. What to expect (backtest, BTC 4H, 100 lots, fees incl.)', points: [
    '≈25 trades a year (about 2 a month), ~40–60% win rate.',
    'Oct 2024–25: +$1,775 · Oct 2025–26: +$873 · untested Jan–Oct 2024: +$2,229.',
    'Max drawdown $430–1,000 (≈4–10% of $10k).',
  ] },
  { heading: '⚠ Risks & limits', points: [
    'Few trades — results can swing a lot by luck; judge it over many months.',
    'Early 2024 was a strong trend that suited it (even the old version made money then).',
    'Depends on the volume filter; validated on BTC 4H only — not 15m/1h or ETH.',
  ] },
];

// Shown in the "How this strategy works" panel. Keep in sync with
// api/chart.py calc_ema_trendline_breakout and strategy/futures_signal_trader.py.
const EMA_TRENDLINE_DETAILS = [
  { heading: '1. Trend direction', points: [
    'Longs only when the candle closes above the 100 EMA; shorts only below it.',
    'Trades the pullback inside the trend, never against it.',
  ] },
  { heading: '2. Swing points & trendline', points: [
    'Swing high/low = highest high / lowest low of 5 candles on each side, used only after those 5 candles have closed (no look-ahead).',
    'Long: line through the last two falling swing highs (the end of a dip). Short: line through the last two rising swing lows (the end of a bounce).',
    'A line is watched for up to 30 candles after its 2nd swing point.',
  ] },
  { heading: '3. Entry', points: [
    'Signal = a candle CLOSES through the line after the previous close was on the other side.',
    'Each line gets one try: if that first break is on the wrong side of the EMA, the line is dropped.',
    'Order: market, about the next candle open. No volume filter.',
  ] },
  { heading: '4. Stop loss & target', points: [
    'SL at the last swing low (long) / swing high (short): if price goes back past it, the pullback was not over.',
    'Trade skipped if the SL is closer than 0.5% of price (fees would eat most of the risk).',
    'TP = 2× the risk (2:1 reward-to-risk). No time exit.',
  ] },
  { heading: '5. Live trade handling', points: [
    'Checks every 30 s: new closed-candle signal → order; mark price beyond SL/TP → close at market.',
    'One trade at a time: signals that arrive while a trade is open are skipped, not taken later.',
    'Max 10 entries per day.',
  ] },
  { heading: '6. What to expect (backtest, BTC 1H, 100 lots, fees incl.)', points: [
    '≈70–85 trades a year, ~45% win rate; winners are 2× losers.',
    'Oct 2024–25: +$1,995 · Oct 2025–26: +$5,412 · untested Jan–Oct 2024: +$1,280.',
    'Max drawdown $1,250–1,700 (≈13–17% of $10k); losing streaks of 5–7 trades are normal.',
  ] },
  { heading: '⚠ Risks & limits', points: [
    'Struggles in sideways/choppy markets where price keeps crossing the EMA.',
    'Sensitive to the EMA length: the same rules with a 200 EMA lost money on the untested period.',
    'Signals use Yahoo prices but orders fill on Delta; ~20% of signals can differ.',
    'Validated on BTC 1H only — not 15m, other timeframes or ETH.',
  ] },
];

const STRATEGIES = [
  { key: 'delta_neutral', label: 'Delta Neutral', icon: '⚡', type: 'Options',
    desc: 'Sells a call and put at matching deltas. Monitors premiums and rebalances when one leg spikes. Auto-exits at target P&L.',
    features: ['Short Strangle', 'Auto Rebalance', 'Premium Monitoring', 'WebSocket'],
    rec: '⏱ Any expiry · BTC/ETH' },
  { key: 'rsi_div_mss', label: 'Div + MSS', icon: '📊', type: 'Futures',
    desc: 'RSI Divergence + Market Structure Shift. Reversal entry on a close through structure after an RSI divergence. SL at the swing, 1:1 target.',
    features: ['RSI Divergence', 'Structure Shift', 'Swing Points', '1:1 R:R'],
    rec: '⚠ Backtest 2024-26: ~breakeven, few trades (BTC 1H ~12/yr) — no proven edge' },
  { key: 'sma_vol_breakout', label: 'SMA + Breakout', icon: '📈', type: 'Futures',
    desc: 'Price Action + SMA50 + Volume. Strong breakouts confirmed by high increasing volume.',
    features: ['SMA 50', 'Volume Confirm', 'Trend Entry', '2:1 R:R'],
    rec: '⏱ Best on 1H · All assets' },
  { key: 'box_theory', label: 'Box Theory', icon: '📦', type: 'Futures',
    desc: "Previous day's high/low form a box. Buy at the bottom, sell at the top, avoid the middle.",
    features: ['Prev Day H/L', 'Buy Zone', 'Sell Zone', 'Mean Revert'],
    rec: '⏱ Best on 1H (84% WR BANKNIFTY) · 1D for crypto' },
  { key: 'ema_trendline', label: 'EMA + Trendline', icon: '📐', type: 'Futures',
    desc: 'Trendline breakouts in the direction of the 100 EMA. Stop at the last swing point, 2:1 target, one position at a time.',
    features: ['100 EMA', 'Swing Trendline', 'Swing Stop', '1:2 R:R'],
    rec: '⏱ BTC 1H only · backtest 2024-26: ~45% WR, PF 1.2–1.8' },
  { key: 'ema920_pullback', label: '9/20 EMA Pullback', icon: '🔄', type: 'Futures',
    desc: '9 & 20 EMA pullback entries. Buy dips above EMAs, sell rallies below EMAs, skip the chop.',
    features: ['9 EMA', '20 EMA', 'Pullback', 'Directional'],
    rec: '⏱ Best on 15M · BTC, BANKNIFTY, NIFTY' },
  { key: 'darvas_box', label: 'Darvas Box', icon: '🏗️', type: 'Futures',
    desc: 'Classic Darvas Box breakout. Buy when price breaks above consolidation box with high volume.',
    features: ['Breakout', 'Volume Confirm', 'Trend Follow', '1:2 R:R'],
    rec: '⏱ Best on 1H · Works in trending markets only' },
  { key: 'fib_retracement', label: 'Fib Retracement', icon: '🌀', type: 'Futures',
    desc: 'Auto-detect swing H/L and signal on pullback to 0.382/0.618 Fibonacci levels.',
    features: ['Fibonacci', 'Pullback', 'Swing H/L', '1:2 R:R'],
    rec: '⏱ Best on 1H/1D · All assets' },
  { key: 'fvg', label: 'Fair Value Gap', icon: '⚡', type: 'Futures',
    desc: 'Detect imbalance candles (FVG) and signal when price revisits the gap zone.',
    features: ['FVG', 'Imbalance', 'Gap Fill', 'Volume'],
    rec: '⏱ Best on 15M/1H · BTC, ETH' },
  { key: 'supply_demand', label: 'Supply & Demand', icon: '🧱', type: 'Futures',
    desc: 'Detect order blocks / S&D zones from strong moves. Signal on zone revisit.',
    features: ['Order Blocks', 'S/D Zones', 'Revisit', '1:2 R:R'],
    rec: '⏱ Best on 1H · All assets' },
  { key: 'candle_patterns', label: 'Candlestick Patterns', icon: '🕯️', type: 'Futures',
    desc: 'Engulfing, Hammer & Shooting Star in the direction of the 100 EMA, on high volume. ATR stop, 2:1 target, one position at a time.',
    features: ['Engulfing', 'Hammer / Star', '100 EMA', 'ATR Stop'],
    rec: '⏱ BTC 4H only · backtest 2024-26: ~40-60% WR, ~25 trades/yr' },
  { key: 'vol_imbalance', label: 'Volume Imbalance', icon: '🐋', type: 'Futures',
    desc: 'Institutional volume spike → consolidation → breakout. 1:4 R:R, only 20-30% win rate needed.',
    features: ['10x Vol Spike', 'Consolidation', 'Breakout', '1:4 R:R'],
    rec: '⏱ Best on 15M · BTC, ETH (needs real-time volume)' },
  { key: 'iv_crush', label: 'IV Crush', icon: '💎', type: 'Options',
    desc: 'Sell ATM straddle when IV is overpriced vs realized vol. Profit from IV crush after events.',
    features: ['IV/RV Filter', 'Short Straddle', 'IV Crush', 'Auto Exit'],
    rec: '⏱ Event-driven · BTC/ETH options on Delta Exchange' },
  { key: 'confluence_scalp', label: 'Confluence Scalp', icon: '🎯', type: 'Futures',
    desc: 'Trendline break + support/resistance bounce + EMA reclaim. High-probability scalp entries.',
    features: ['Trendline Break', 'S/R Bounce', 'EMA Reclaim', 'Volume'],
    rec: '⏱ Best on 1H (44% WR SENSEX, 39% NIFTY) · 15M for BANKNIFTY' },
  { key: 'call_ratio', label: 'Call Ratio Spread', icon: '📐', type: 'Options',
    desc: 'Monthly call ratio: Buy 1 OTM, Sell 2 further OTM, Hedge. No downside risk. High win rate.',
    features: ['Call Ratio 1:2', 'No Downside Risk', 'Set & Forget', '~5% Monthly'],
    rec: '⏱ Monthly expiry · Enter last Friday · BTC/ETH options' },
  { key: 'renko_redbar', label: 'Renko Red Bar', icon: '🧱', type: 'Futures',
    desc: 'ATR-based Renko trend + Red Bar reversal entry. Skip first candle, trade the trap. OTM option buying.',
    features: ['Renko Line', 'Red Bar Entry', 'EMA 10/30', 'SMA 150'],
    rec: '⏱ Best on 5M/15M · Intraday scalp · BTC, NIFTY, BANKNIFTY' },
  { key: 'prev_day_breakout', label: 'Prev Day Breakout', icon: '🚀', type: 'Futures',
    desc: "Mark prev day H/L → wait for breakout → retest with hammer/engulfing confirmation. SL below pattern, 1:2/1:3 R:R.",
    features: ['Prev Day H/L', 'Breakout + Retest', 'Hammer/Engulfing', '1:2 R:R'],
    rec: '⏱ Best on 15M/5M · BTC, NIFTY, BANKNIFTY' },
  { key: 'daily_strangle', label: 'Daily Strangle', icon: '🎯', type: 'Options',
    desc: '0DTE short strangle: sell ~$100 call + put at 9 AM, 100% SL per leg (premium doubles), exit 5:15 PM. Daily recurring.',
    features: ['0DTE Expiry', '9AM Entry / 5PM Exit', '100% SL Per Leg', 'Daily Auto-Trade'],
    rec: '⏱ Daily 9:00 AM · BTC options · $200 margin for 0.1 BTC' },
  { key: 'nse_strangle', label: 'NSE Strangle', icon: '🇮🇳', type: 'Options',
    desc: 'Live short strangle on NIFTY/BANKNIFTY. Sell OTM call + put nearest ₹target premium, configurable SL & trading days.',
    features: ['NSE Index Options', 'Configurable Days', 'Live Orders', 'Per-Leg SL + Re-entry'],
    rec: '⏱ Market Hours 9:15–3:30 · NIFTY, BANKNIFTY, FINNIFTY' },
  { key: 'nse_delta_neutral', label: 'NSE Delta Neutral', icon: '🎯🇮🇳', type: 'Options',
    desc: 'Weekly positional short strangle on NSE indices. Enters Wednesday 9:20, exits on TP/SL/max adj or Tuesday 15:15. NRML orders.',
    features: ['Delta-Based Entry', 'Auto Rebalance', 'TP/SL % Premium', 'Market Hours Aware'],
    rec: '⏱ Market Hours 9:15–3:30 · NIFTY, BANKNIFTY' },
  { key: 'nse_ema_spread', label: 'NSE EMA Spread', icon: '📈🇮🇳', type: 'Options',
    desc: 'Daily EMA10 direction → bear call or bull put spread on NSE indices. 90% TP / 150% SL of premium. Trades Mon-Fri at 10:25 AM.',
    features: ['EMA10 Direction', 'Credit Spread', 'Daily Auto-Trade', '90% TP / 150% SL'],
    rec: '⏱ Daily 10:25 AM IST · NIFTY, BANKNIFTY' },
  { key: 'pivot_supertrend', label: 'Pivot + SuperTrend', icon: '📡', type: 'Options',
    desc: '0DTE directional option selling: SuperTrend(7,3) + Daily Pivot R1/S1. Sell ATM put on breakout above R1, sell ATM call below S1. Exit on ST flip.',
    features: ['0DTE ATM Selling', 'SuperTrend Exit', 'Pivot Filter', 'Max 3 Trades/Day'],
    rec: '⏱ Daily 9:20 AM · BTC options · Directional + Theta' },
  { key: 'portfolio_strangle', label: 'Portfolio Strangle', icon: '📊', type: 'Options',
    desc: '0DTE 3-entry strangle: sell OTM5 at 9:15, 10:20, 11:15 (30 lots each). 200% SL + recost re-entry. Skip Fri & Sun.',
    features: ['3 Time Entries', 'OTM5 Strangle', '200% SL + Recost', 'Skip Fri/Sun'],
    rec: '⏱ Daily 9:15/10:20/11:15 · BTC options · Drawdown optimized' },
  { key: 'hybrid_switch', label: 'Hybrid Switch BTST', icon: '⚡', type: 'Options',
    desc: 'Sell strangle at 7:15 PM, on SL hit switch to 10x buying with trailing SL. Holds across 2 sessions (BTST).',
    features: ['BTST (2 Sessions)', 'Non-Directional → Directional', '10x Buy on SL', 'Trailing SL'],
    rec: '⏱ Daily 7:15 PM · D2 expiry · ₹10,000 capital' },
  { key: 'oi_strategy', label: 'OI Strategy', icon: '🔍', type: 'Options',
    desc: 'Daily option selling at max OI strikes (support/resistance). Auto-trades at 6:30 PM IST, exits on TP/SL.',
    features: ['Max OI Strikes', 'Daily Auto-Trade', 'Support/Resistance', 'OI Shift Detection'],
    rec: '⏱ Daily 6:30 PM · BTC options' },
  { key: 'weekly_dn', label: 'Weekly Delta Neutral', icon: '📅', type: 'Options',
    desc: 'Runs Delta Neutral strategy every Friday 9 PM IST. Sells strangle, monitors, exits, repeats weekly.',
    features: ['Weekly Friday 9 PM', 'Short Strangle', 'Auto Rebalance', '3-week expiry'],
    rec: '⏱ Every Friday · BTC/ETH options' },
  { key: 'ema_spread', label: 'EMA Credit Spread', icon: '📉', type: 'Options',
    desc: 'Daily EMA14 direction → bear call or bull put spread. 90% TP / 100% SL of premium. Runs daily at 6:30 PM.',
    features: ['EMA14 Direction', 'Credit Spread', 'Daily Auto-Trade', '90% TP / 100% SL'],
    rec: '⏱ Daily 6:30 PM · BTC options' },
  { key: 'ema_spread_scheduled', label: 'EMA Spread (Scheduled)', icon: '📉🗓️', type: 'Options',
    desc: 'EMA14 credit spread you run on SELECTED weekdays instead of daily. Bear call / bull put at 20Δ/10Δ, TP 90% / SL 100%.',
    features: ['EMA14 Direction', 'Credit Spread', 'Pick Trading Days', '90% TP / 100% SL'],
    rec: '⏱ Selected days 6:30 PM · BTC options' },
  { key: 'ema_spread_v2', label: 'EMA Credit Spread V2', icon: '📉✌️', type: 'Options',
    desc: 'EMA14 credit spread with two-tier partial exits. Books half at TP1/SL1; the remaining half then exits on TP2 or SL2. Defaults: TP1 50% / TP2 90% · SL1 90% / SL2 150%.',
    features: ['EMA14 Direction', 'Two-Tier TP/SL', '50/50 Scale-Out', 'TP2 / SL2 on remainder'],
    rec: '⏱ Daily 6:30 PM · BTC options · De-risked exits' },
  { key: 'ema_trend', label: 'EMA Trend Follower', icon: '🚀', type: 'Futures',
    desc: 'Daily EMA10/30 trend following across the 10 most liquid Delta perps. Long above, short below (BTC regime filter). Exits only when the trend flips. Volatility-sized.',
    features: ['Daily EMA10/30', 'Long + Short', 'Top-10 by OI', 'Exit on Flip'],
    rec: '⏱ Daily · backtests 2022-26: ~+9-12%/yr, DD ~30% · profit comes in bursts' },
  { key: 'dual_momentum', label: 'Dual Momentum Basket', icon: '⚖️', type: 'Futures',
    desc: 'Twice-daily long/short basket. At 6 AM & 6 PM IST, longs the top-N 24h gainers and shorts the top-N losers ($10 each). Tracks TP +10% / SL -5% per position and closes any coin no longer in its side\'s list.',
    features: ['Top-N Gainers/Losers', 'Long + Short', '6 AM / 6 PM IST', 'TP 10% / SL 5%'],
    rec: '⏱ Twice daily · Delta perpetuals · Market-neutral momentum' },
  { key: 'nse_dual_momentum', label: 'NSE Dual Momentum', icon: '⚖️🇮🇳', type: 'Equity',
    desc: 'Intraday long/short NSE stock basket. Buys top-N gainers, shorts top-N losers (₹10,000 each, whole shares). TP +5% / SL -3% side-aware, squared off at 3:15 PM. Paper or live.',
    features: ['Top-N Gainers/Losers', 'Long + Short Stocks', 'Intraday Square-off', 'Paper Trading'],
    rec: '⏱ Intraday 9:30–3:15 IST · NSE equities' },
  { key: 'nse_sector_options', label: 'NSE Sector Options (Paper)', icon: '🧭🇮🇳', type: 'Options',
    desc: 'Rotates a watchlist every minute from the current top green/red sectors (Sector Scanner) and paper-buys the ATM CALL / PUT when a stock\'s 5-min candle closes beyond its 09:15–09:45 range (and the 5-min VWAP). Max 5 open, 6 entries a day. Exits when the STOCK hits its SL/TP or at 15:15.',
    features: ['30-min range breakout', '5m candle close + 5m VWAP', 'Max 5 open · 6 a day', 'Paper only · Groww chain'],
    rec: '⏱ Intraday 9:45–3:15 IST · NSE stock options · needs a Groww profile' },
  { key: 'nse_scanner_overnight', label: 'Scanner Overnight Options (Paper)', icon: '🌙🇮🇳', type: 'Options',
    desc: 'At 15:15 takes the top Sector Scanner setup of each green/red sector (max 5, one per sector, F&O stocks only) and paper-buys the ATM CALL (LONG) / PUT (SHORT) — expiry at least 15 days away, else next month. Sells everything at 09:17 the next trading day. No stop / target.',
    features: ['15:15 entry → next-day 09:17 exit', 'Top 5 · one per sector', 'Expiry ≥ 15 days', 'Paper only · Groww chain'],
    rec: '⏱ Overnight · NSE stock options · needs a Groww profile' },
  { key: 'crypto_futures', label: 'Crypto Scanner Futures', icon: '🪙⚡', type: 'Futures',
    desc: 'Trades Delta Exchange perpetual futures picked by the Crypto Scanner: top LONG/SHORT setups (RS + 24h VWAP + volume) in the strongest green/red crypto sectors. You set the dollars per trade; lots = dollars ÷ (price × coins per lot). Stop at VWAP or a fixed %, target in R, time stop. Paper or LIVE.',
    features: ['Crypto Scanner pick', '$ per trade → lots', 'Stop / target / time stop', 'Paper or Live (Delta)'],
    rec: '⏱ 24/7 · Delta perps · Live needs a Delta Exchange profile' },
];

const DN_FIELDS = [
  { key: 'expiry_date', label: 'Expiry (DD-MM-YYYY)', type: 'text', default: '', placeholder: '29-05-2026' },
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 10 },
  { key: 'target_delta', label: 'Target Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'delta_tolerance', label: 'Delta Tolerance', type: 'number', step: '0.01', default: 0.05 },
  { key: 'premium_threshold', label: 'Premium Threshold (%)', type: 'number', default: 40, hint: 'Adjustment triggers at this %' },
  { key: 'target_pnl', label: 'Target P&L ($)', type: 'number', default: 10 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 5 },
  { key: 'max_adjustments', label: 'Max Adjustments', type: 'number', default: 10 },
];

const IVC_FIELDS = [
  { key: 'expiry_date', label: 'Expiry (DD-MM-YYYY)', type: 'text', default: '', placeholder: 'Auto-select best', hint: 'Leave empty to auto-pick highest IV expiry' },
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 10 },
  { key: 'iv_rv_threshold', label: 'IV/RV Threshold', type: 'number', step: '0.1', default: 1.3, hint: 'Min IV/RV ratio to enter (1.3 = IV 30% above RV)' },
  { key: 'target_profit_pct', label: 'Target Profit (%)', type: 'number', default: 30, hint: '% of premium collected' },
  { key: 'max_loss_pct', label: 'Max Loss (%)', type: 'number', default: 50, hint: '% of premium collected' },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 10 },
];

const CR_FIELDS = [
  { key: 'expiry_date', label: 'Expiry (DD-MM-YYYY)', type: 'text', default: '', placeholder: 'Auto-select nearest', hint: 'Leave empty for auto-select' },
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 10 },
  { key: 'buy_offset_pct', label: 'Buy Offset (% OTM)', type: 'number', step: '0.5', default: 2, hint: 'e.g. 2 = 2% above spot' },
  { key: 'sell_offset_pct', label: 'Sell Offset (% OTM)', type: 'number', step: '0.5', default: 4, hint: 'Sell 2x lots at this offset' },
  { key: 'hedge_offset_pct', label: 'Hedge Offset (% OTM)', type: 'number', step: '0.5', default: 7, hint: 'Far OTM hedge leg' },
  { key: 'target_pct', label: 'Target Profit (%)', type: 'number', step: '0.5', default: 5 },
  { key: 'sl_pct', label: 'Stop Loss (%)', type: 'number', step: '0.5', default: 8 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 30 },
];

const OI_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'target_pct', label: 'Target Profit (% of premium)', type: 'number', default: 50 },
  { key: 'stop_loss_pct', label: 'Stop Loss (% of premium)', type: 'number', default: 50 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 5 },
  { key: 'entry_hour', label: 'Entry Hour (24h)', type: 'number', default: 18 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 30 },
];

const WDN_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'target_delta', label: 'Target Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'delta_tolerance', label: 'Delta Tolerance', type: 'number', step: '0.01', default: 0.05 },
  { key: 'premium_threshold', label: 'Premium Threshold (%)', type: 'number', default: 40 },
  { key: 'tp_percent', label: 'Take Profit (% of premium)', type: 'number', default: 70 },
  { key: 'sl_percent', label: 'Stop Loss (% of premium)', type: 'number', default: 70 },
  { key: 'max_adjustments', label: 'Max Adjustments', type: 'number', default: 5 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 5 },
  { key: 'expiry_week', label: 'Expiry Week (nth Friday)', type: 'number', default: 3 },
  { key: 'start_day', label: 'Start Day', type: 'select', options: [{value:'monday',label:'Monday'},{value:'tuesday',label:'Tuesday'},{value:'wednesday',label:'Wednesday'},{value:'thursday',label:'Thursday'},{value:'friday',label:'Friday'},{value:'saturday',label:'Saturday'},{value:'sunday',label:'Sunday'}], default: 'friday' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 21 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 0 },
];

const EMA_SPREAD_SCHEDULED_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'sell_delta', label: 'Sell Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'buy_delta', label: 'Buy Delta', type: 'number', step: '0.01', default: 0.10 },
  { key: 'ema_period', label: 'EMA Period', type: 'number', default: 14 },
  { key: 'tp_pct', label: 'Target Profit (% of premium)', type: 'number', default: 90 },
  { key: 'sl_pct', label: 'Stop Loss (% of premium)', type: 'number', default: 100 },
  { key: 'min_expiry_days', label: 'Min Expiry Days', type: 'number', default: 8 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 5 },
  { key: 'entry_hour', label: 'Entry Hour (24h)', type: 'number', default: 18 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 30 },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon,1=Tue,2=Wed,3=Thu,4=Fri,5=Sat,6=Sun' },
];

const EMA_SPREAD_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'sell_delta', label: 'Sell Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'buy_delta', label: 'Buy Delta', type: 'number', step: '0.01', default: 0.10 },
  { key: 'ema_period', label: 'EMA Period', type: 'number', default: 14 },
  { key: 'tp_pct', label: 'Target Profit (% of premium)', type: 'number', default: 90 },
  { key: 'sl_pct', label: 'Stop Loss (% of premium)', type: 'number', default: 100 },
  { key: 'min_expiry_days', label: 'Min Expiry Days', type: 'number', default: 8 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 5 },
  { key: 'entry_hour', label: 'Entry Hour (24h)', type: 'number', default: 18 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 30 },
];

const EMA_SPREAD_V2_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'sell_delta', label: 'Sell Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'buy_delta', label: 'Buy Delta', type: 'number', step: '0.01', default: 0.10 },
  { key: 'ema_period', label: 'EMA Period', type: 'number', default: 14 },
  { key: 'tp1_pct', label: 'TP1 — close half (% of full premium)', type: 'number', default: 50, hint: 'Books half the position at this profit level' },
  { key: 'tp2_pct', label: 'TP2 — close rest (% of remaining half)', type: 'number', default: 90, hint: 'Remaining half exits at this % of its own premium (also the recovery exit after SL1)' },
  { key: 'sl1_pct', label: 'SL1 — close half (% of full premium)', type: 'number', default: 90, hint: 'Cuts half the position at this loss level' },
  { key: 'sl2_pct', label: 'SL2 — close rest (% of remaining half)', type: 'number', default: 150, hint: 'Remaining half exits at this % of its own premium (hard floor after either TP1 or SL1)' },
  { key: 'min_expiry_days', label: 'Min Expiry Days', type: 'number', default: 8 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 5 },
  { key: 'entry_hour', label: 'Entry Hour (24h)', type: 'number', default: 18 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 30 },
];

const EMA_TREND_FIELDS = [
  { key: 'top_n', label: 'Universe Size (top N by 30-day OI)', type: 'number', default: 10, hint: 'Validated with 10' },
  { key: 'ema_fast', label: 'Fast EMA (daily)', type: 'number', default: 10 },
  { key: 'ema_slow', label: 'Slow EMA (daily)', type: 'number', default: 30, hint: 'Also used for the BTC regime filter' },
  { key: 'risk_per_atr_usd', label: 'Risk per ATR move ($)', type: 'number', default: 25, hint: 'Size so one average daily move (ATR20) ≈ this many $' },
  { key: 'notional_usd', label: 'Max Notional per Coin ($)', type: 'number', default: 500 },
  { key: 'max_gross_usd', label: 'Max Total Exposure ($)', type: 'number', default: 7500, hint: 'New entries are skipped above this' },
  { key: 'allow_shorts', label: 'Shorts', type: 'select', options: [{value:true,label:'Long + Short (validated)'},{value:false,label:'Long only'}], default: true, hint: 'Long-only lost heavily in the 2022 crash' },
  { key: 'btc_regime', label: 'BTC Regime Filter', type: 'select', options: [{value:true,label:'On (validated)'},{value:false,label:'Off'}], default: true },
  { key: 'dry_run', label: 'Mode', type: 'select', options: [{value:true,label:'Paper (dry run)'},{value:false,label:'Live'}], default: true },
];

// Keep in sync with strategy/ema_trend_follower.py.
const EMA_TREND_DETAILS = [
  { heading: '1. Universe (daily)', points: [
    'Each day after the 00:00 UTC candle closes, takes the 10 Delta perps with the highest 30-day average open interest (USD).',
    'This includes Delta\'s gold, silver and stock-index perps (e.g. XAUT, PAXG, SLVON, QQQX, SPYX) when they rank — the Delta backtest included them too.',
    'A coin needs more than 50 daily candles of history.',
  ] },
  { heading: '2. Signal', points: [
    'Per coin, on daily closes: LONG while EMA10 > EMA30, SHORT while EMA10 < EMA30.',
    'BTC regime: longs only when BTC closes above its EMA30, shorts only when below.',
  ] },
  { heading: '3. Exit', points: [
    'A position is closed only when its own EMA10/30 state flips — no take-profit, no stop, no time limit. Big trends are allowed to run.',
    'After a flip the coin can be entered on the other side the same day. Coins that drop out of the top 10 are kept until they flip.',
  ] },
  { heading: '4. Size', points: [
    'Notional = $25 ÷ ATR20% (one average daily move ≈ $25), max $500 per coin, max $7,500 total.',
    'Market orders, integer lots. One position per coin.',
  ] },
  { heading: '5. What to expect (backtests, at these sizes)', points: [
    'Delta Apr 2024–Oct 2026 (real funding): +$2,414 (+9.5%/yr on $10k), max drawdown $4,510 (29%); positive every year (+$322 / +$612 / +$1,062).',
    'Binance 2022–Mar 2024 (crypto only, incl. the 2022 crash): +$2,811, max drawdown $3,526.',
    '~120 trades a year, ~31-35% win rate — a few big trends pay for many small losses.',
  ] },
  { heading: '⚠ Risks & limits', points: [
    'Profit comes in bursts: removing the 5 best trades turns every test negative. Expect long flat or losing stretches.',
    'Drawdowns of ~30% at these sizes; larger sizes scale both profit and drawdown.',
    'Gold appears twice (XAUT and PAXG), so gold trends get double weight.',
    'Displayed P&L excludes fees and funding (backtest funding ≈ −$420 per 2.5 years at this size).',
  ] },
];

const DUAL_MOMENTUM_FIELDS = [
  { key: 'top_n', label: 'Top N per Side (gainers & losers)', type: 'number', default: 10 },
  { key: 'notional_usd', label: 'Notional per Coin ($)', type: 'number', default: 10 },
  { key: 'tp_pct', label: 'Take Profit (fraction)', type: 'number', step: '0.01', default: 0.10, hint: '0.10 = +10% in trade\'s favour' },
  { key: 'sl_pct', label: 'Stop Loss (fraction)', type: 'number', step: '0.01', default: 0.05, hint: '0.05 = -5% against trade' },
  { key: 'min_oi_change_pct', label: 'Min 6h OI Change (%)', type: 'number', step: '0.1', default: 10, hint: 'Only trade coins whose 6h open interest rose more than this %' },
  { key: 'price_check_interval', label: 'TP/SL Check Interval (s)', type: 'number', default: 60 },
  { key: 'rotate_on_runs', label: 'Rotate Delisted', type: 'text', default: 'all', hint: "'all' = every run (6AM & 6PM), 'evening' = 6PM only" },
];

const STRANGLE_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'target_premium', label: 'Target Premium ($)', type: 'number', default: 100, hint: 'Premium per leg to sell' },
  { key: 'sl_pct', label: 'Stop Loss (%)', type: 'number', default: 200, hint: '200 = SL when premium doubles (loss = 100% of premium)' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 9 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 0 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 17 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 10 },
];

const NSE_STRANGLE_FIELDS = [
  { key: 'symbol', label: 'Symbol', type: 'select', options: [{value:'NIFTY',label:'NIFTY'},{value:'BANKNIFTY',label:'BANKNIFTY'},{value:'FINNIFTY',label:'FINNIFTY'},{value:'MIDCPNIFTY',label:'MIDCPNIFTY'},{value:'SENSEX',label:'SENSEX'}], default: 'NIFTY' },
  { key: 'lots', label: 'Number of Lots', type: 'number', default: 1 },
  { key: 'lot_size', label: 'Lot Size (override)', type: 'number', default: 65, hint: 'NIFTY=65, BANKNIFTY=30, FINNIFTY=65, SENSEX=20' },
  { key: 'target_premium', label: 'Target Premium (₹)', type: 'number', default: 100, hint: 'Premium per leg to sell' },
  { key: 'sl_pct', label: 'Stop Loss (%)', type: 'number', default: 200, hint: '200 = SL when premium doubles' },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon,1=Tue,2=Wed,3=Thu,4=Fri' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 9 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 20 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 15 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 15 },
];

const NSE_DUAL_MOMENTUM_FIELDS = [
  { key: 'top_n', label: 'Top N (Gainers & Losers)', type: 'number', default: 5 },
  { key: 'amount_per_stock', label: 'Amount per Stock (₹)', type: 'number', default: 10000 },
  { key: 'tp_pct', label: 'Take Profit (fraction)', type: 'number', step: '0.01', default: 0.05, hint: '0.05 = +5%' },
  { key: 'sl_pct', label: 'Stop Loss (fraction)', type: 'number', step: '0.01', default: 0.03, hint: '0.03 = -3%' },
  { key: 'min_oi_change_pct', label: 'Min OI Change (%)', type: 'number', step: '0.1', default: 10, hint: 'Only trade F&O stocks whose OI rose more than this %. 0 = off' },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon..4=Fri' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 9 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 30 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 15 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 30 },
  { key: 'dry_run', label: 'Mode', type: 'select', options: [{value:true,label:'Paper'},{value:false,label:'Live'}], default: true },
  { key: 'run_immediately', label: 'Run Immediately (test)', type: 'select', options: [{value:false,label:'No — wait for entry time'},{value:true,label:'Yes — enter now once'}], default: false, hint: 'Testing: enter now on start, then daily at entry time' },
];

const CRYPTO_FUTURES_FIELDS = [
  { key: 'dry_run', label: 'Mode', type: 'select', options: [{value:true,label:'Paper (simulated fills)'},{value:false,label:'LIVE (real orders on Delta)'}], default: true, hint: 'LIVE places real market orders with the selected Delta Exchange profile' },
  { key: 'amount_usd', label: 'Dollars per Trade ($)', type: 'number', default: 100, hint: 'Position size in USD. Lots = $ ÷ (price × coins per lot), rounded down; coins whose 1 lot costs more are skipped' },
  { key: 'max_positions', label: 'Max Open Positions', type: 'number', default: 3, hint: 'Total exposure ≈ dollars per trade × this' },
  { key: 'max_trades_per_day', label: 'Max Entries per Day (IST)', type: 'number', default: 6 },
  { key: 'stop_mode', label: 'Stop-Loss', type: 'select', options: [{value:'vwap',label:'24h VWAP (scanner, 0.75–3%)'},{value:'fixed',label:'Fixed %'}], default: 'vwap' },
  { key: 'stop_pct', label: 'Fixed Stop (%)', type: 'number', step: '0.1', default: 1.5, hint: 'Used only when Stop-Loss = Fixed %' },
  { key: 'rr', label: 'Target (R multiple)', type: 'number', step: '0.1', default: 2, hint: '2 = target is twice the stop distance' },
  { key: 'max_hold_hours', label: 'Max Hold (hours)', type: 'number', default: 24, hint: 'Close at market after this long if neither stop nor target hit' },
  { key: 'min_turnover_usd', label: 'Min 24h Turnover ($)', type: 'number', default: 1000000, hint: 'Skip thin coins (bad fills, noisy signals)' },
  { key: 'require_sector_bias', label: 'Sectors Used', type: 'select', options: [{value:true,label:'Only BULLISH / BEARISH sectors'},{value:false,label:'All sectors (incl. MIXED)'}], default: true },
  { key: 'sectors', label: 'Only These Sectors (optional)', type: 'text', default: '', placeholder: 'e.g. Layer 1, DeFi, AI', hint: 'Comma-separated: Layer 1, Layer 2, Smart Contracts, DeFi, AI, Meme, Gaming, NFT, New Listings, Other (untagged). Empty = all' },
  { key: 'allow_long', label: 'Longs', type: 'select', options: [{value:true,label:'Allowed'},{value:false,label:'Off'}], default: true },
  { key: 'allow_short', label: 'Shorts', type: 'select', options: [{value:true,label:'Allowed'},{value:false,label:'Off'}], default: true },
  { key: 'cooldown_hours', label: 'Re-entry Cooldown (h)', type: 'number', default: 24, hint: 'Do not trade the same coin again within this many hours' },
  { key: 'scan_interval_min', label: 'Scan Every (min)', type: 'number', default: 15 },
  { key: 'price_check_sec', label: 'Price Check (s)', type: 'number', default: 15 },
];

// Keep in sync with strategy/crypto_scanner_futures.py.
const CRYPTO_FUTURES_DETAILS = [
  { heading: '1. Pick (every scan, default 15 min, 24/7)', points: [
    'Reads the Crypto Scanner: Delta perps grouped by Delta\'s sector tags. By default only BULLISH/BEARISH sectors (median 24h move ≥ 1.5% with breadth ≥ 30%), biggest move first.',
    'Takes LONG/SHORT setups: stronger/weaker than the sector, on the right side of the 24h VWAP, 24h volume ≥ 1.5× its 7-day average, and 24h turnover ≥ the minimum.',
    'Skips coins already held or traded within the cooldown. Stops when max open positions or max entries per day is reached.',
  ] },
  { heading: '2. Size — your dollars → lots', points: [
    'Lots = Dollars per Trade ÷ (price × coins per lot), rounded down. Example: $100 on SOL at $94 with 1 SOL/lot = 1 lot; on DOGE at $0.075 with 100 DOGE/lot ($7.53 a lot) = 13 lots.',
    'If a single lot costs more than the dollars set, the coin is skipped (no rounding up). Margin used depends on the leverage set for that product in your Delta account.',
  ] },
  { heading: '3. Exit', points: [
    'Stop at the 24h VWAP (clamped 0.75–3% from entry) or a fixed %. Target = R multiple × stop distance. Time stop after Max Hold hours. Checked on the Delta mark price every price check.',
    'Stopping the strategy closes all its open positions (market orders in LIVE).',
  ] },
  { heading: '4. Paper vs LIVE', points: [
    'Paper: fills at the live Delta mark price, no orders. A profile is optional.',
    'LIVE: real market orders on Delta Exchange (production) through the selected Delta Exchange profile; fills use Delta\'s average fill price. If a close order fails, the position is kept and retried every price check — watch the log.',
    'P&L = (exit − entry) × lots × coins per lot minus estimated taker fees (0.05% + 18% GST each side). Funding is NOT included.',
  ] },
  { heading: '5. What to expect', points: [
    'These scanner rules are NOT backtested on crypto. The NSE version of the same idea lost money in its backtest, and earlier research here showed strong crypto news tends to fade. Run Paper first and judge it on its own results.',
  ] },
];

const NSE_SCANNER_OVERNIGHT_FIELDS = [
  { key: 'max_trades', label: 'Max Trades per Day', type: 'number', default: 5, hint: 'Top setup of each bullish/bearish sector, one per sector' },
  { key: 'min_dte', label: 'Min Days to Expiry', type: 'number', default: 15, hint: 'Nearest expiry with at least this many days left, else the next month' },
  { key: 'lots', label: 'Lots per Trade', type: 'number', default: 1, hint: 'Lot size comes from the Groww instruments list' },
  { key: 'max_premium', label: 'Max Premium per Trade (₹)', type: 'number', default: 100000, hint: 'Skip a contract if lots × lot size × premium is above this' },
  { key: 'sector_group', label: 'Sector Group', type: 'select', options: [{value:'Sectoral',label:'Sectoral (21)'},{value:'Thematic',label:'Thematic (41)'}], default: 'Sectoral' },
  { key: 'trading_days', label: 'Entry Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon..4=Fri (the exit is always the next trading day)' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 15 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 15 },
  { key: 'exit_hour', label: 'Exit Hour (next day)', type: 'number', default: 9 },
  { key: 'exit_minute', label: 'Exit Minute (next day)', type: 'number', default: 17 },
  { key: 'monitoring_interval', label: 'Price Check Interval (s)', type: 'number', default: 15 },
  { key: 'run_immediately', label: 'Run Immediately (test)', type: 'select', options: [{value:false,label:'No — wait for 15:15'},{value:true,label:'Yes — scan & enter now once'}], default: false, hint: 'Testing: scans and enters now, ignoring the time and market-open check once' },
];

// Keep in sync with strategy/nse_scanner_overnight.py.
const NSE_SCANNER_OVERNIGHT_DETAILS = [
  { heading: '1. Pick (15:15 IST)', points: [
    'Reads the live NSE sectors. Only BULLISH/BEARISH sectors count: |change| ≥ 0.5% and breadth ≥ 30% net advancers/decliners. They are walked from the biggest move down.',
    'From EACH sector it takes the top Sector Scanner setup (RS vs sector + right side of VWAP + RVOL ≥ 1.5; labelled LATE after 14:30) in the sector\'s direction — one stock per sector, max 5, F&O stocks only, never the same stock twice. If a sector\'s top stock has no tradeable option it tries that sector\'s next setup.',
    'If the scan fails it retries until 15:25; after that no entry that day.',
  ] },
  { heading: '2. Trade (paper)', points: [
    'LONG → buys the ATM CALL, SHORT → buys the ATM PUT, 1 lot. Expiry: the nearest expiry with at least 15 days left, otherwise the next month. Strike nearest the stock price, price = Groww option-chain LTP. No real orders.',
  ] },
  { heading: '3. Exit (09:17 next trading day)', points: [
    'Every position is sold at 09:17 on the next trading day at the Groww option LTP. No stop-loss or target. Weekends / holidays: sold at 09:17 on the next open day.',
    'If the app was down at 09:17 the positions are sold as soon as it is back during market hours. Realized P&L is after estimated Groww F&O charges.',
  ] },
  { heading: '4. What to expect', points: [
    'Backtest of these rules (Aug–Oct 2026, real NSE prices: entry = 15:00–15:30 average, exit = next-day opening trade): the raw profit came mostly from unreliable opening prints of thin stock options; with those removed and a 0.5% bid-ask spread per side the top-5-per-day version was about breakeven (−₹1.4k on 173 trades). Treat the paper run as a forward test.',
  ] },
];

const NSE_SECTOR_OPTIONS_FIELDS = [
  { key: 'stop_pct', label: 'Stock Stop-Loss (%)', type: 'number', step: '0.05', default: 0.5, hint: 'Sell the option when the STOCK moves this % against the trade' },
  { key: 'target_pct', label: 'Stock Target (%)', type: 'number', step: '0.05', default: 0.75, hint: 'Sell the option when the STOCK moves this % in favour (0.75 = 1.5 × 0.5)' },
  { key: 'lots', label: 'Lots', type: 'number', default: 1, hint: 'Lot size comes from the Groww instruments list' },
  { key: 'max_premium', label: 'Max Premium per Trade (₹)', type: 'number', default: 100000, hint: 'Skip a contract if lots × lot size × premium is above this' },
  { key: 'min_dte', label: 'Min Days to Expiry', type: 'number', default: 10, hint: 'Nearest expiry until it is closer than this many days, then the next month' },
  { key: 'sector_group', label: 'Sector Group', type: 'select', options: [{value:'Sectoral',label:'Sectoral (21)'},{value:'Thematic',label:'Thematic (41)'}], default: 'Sectoral' },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon..4=Fri' },
  { key: 'max_open', label: 'Max Open Positions', type: 'number', default: 5, hint: 'At most this many options held at the same time' },
  { key: 'max_entries', label: 'Max Entries per Day', type: 'number', default: 6 },
  { key: 'max_watchlist', label: 'Watchlist Size', type: 'number', default: 15, hint: 'Stocks picked at 09:45 and watched for a 30-min range breakout' },
  { key: 'candle_min', label: 'Breakout Candle', type: 'select', options: [{value:5,label:'5-minute close'},{value:15,label:'15-minute close'}], default: 5, hint: 'Candle whose CLOSE must be beyond the 09:15–09:45 range' },
  { key: 'use_vwap', label: '5-min VWAP Filter', type: 'select', options: [{value:true,label:'Yes — close must also be beyond the 5m VWAP'},{value:false,label:'No — range break only'}], default: true },
  { key: 'entry_hour', label: 'Range End / Watchlist Hour (24h IST)', type: 'number', default: 9, hint: 'Opening range = 09:15 to this time' },
  { key: 'entry_minute', label: 'Range End / Watchlist Minute', type: 'number', default: 45 },
  { key: 'last_entry_hour', label: 'Last Entry Candle Hour', type: 'number', default: 14 },
  { key: 'last_entry_minute', label: 'Last Entry Candle Minute', type: 'number', default: 45, hint: 'No new entries on 15m candles closing after this' },
  { key: 'rescan_sec', label: 'Watchlist Rescan (s)', type: 'number', default: 60, hint: 'Rebuild the watchlist from the current top bullish/bearish sectors this often (min 30)' },
  { key: 'exit_hour', label: 'Square-off Hour (24h IST)', type: 'number', default: 15 },
  { key: 'exit_minute', label: 'Square-off Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Price Check Interval (s)', type: 'number', default: 15 },
  { key: 'run_immediately', label: 'Run Immediately (test)', type: 'select', options: [{value:false,label:'No — wait for 09:45'},{value:true,label:'Yes — build watchlist & check breakouts now once'}], default: false, hint: 'Testing: ignores the time windows and market-open check for the first cycle' },
];

// Keep in sync with strategy/nse_sector_options.py.
const NSE_SECTOR_OPTIONS_DETAILS = [
  { heading: '1. Watchlist (rotated every minute from 09:45 IST)', points: [
    'Every minute (setting) it re-reads the live NSE sectors. Only BULLISH/BEARISH sectors count: |change| ≥ 0.5% and breadth ≥ 30% net advancers/decliners. They are walked from the biggest move down.',
    'From those sectors it takes the Sector Scanner\'s LONG / SHORT / WATCH stocks (direction = sector bias) that have listed stock options, up to the watchlist size (default 15). Stocks whose sector drops out of the top leave the list; stocks of newly strong sectors join it. Stocks already traded today are not re-added.',
    'For each stock it uses the 30-minute opening range: high and low of 09:15–09:45 (5-minute bars).',
  ] },
  { heading: '2. Entry — 5-minute candle breakout (09:50 to 14:45)', points: [
    'After every 5-minute candle closes: a bullish stock whose candle CLOSES above its 30-min high (and above the 5-minute VWAP) → buys the ATM CALL; a bearish stock closing below its 30-min low (and below the 5-minute VWAP) → buys the ATM PUT. A wick through the level without a close beyond it does not count.',
    'Late joiners: a stock that joins the watchlist after 09:50 and is ALREADY beyond its range (and the 5-minute VWAP) at the live price is bought immediately — no wait for a candle close. The Groww spot used for the fill must confirm it.',
    'Max 5 positions open at once and max 6 entries a day; each stock is traded at most once a day. A breakout skipped because the slots are full can still be taken on a later candle if it is still beyond the range.',
    'Expiry: the nearest expiry with at least 10 days left (setting), else the next month. Strike nearest the stock price. Price = Groww option-chain LTP. No real orders are placed.',
  ] },
  { heading: '3. Exit (decided by the STOCK price, per position)', points: [
    'Stop: the stock moves the stop % against the entry stock price. Target: it moves the target % in favour. Checked every price-check interval.',
    'Otherwise every open option is sold at the square-off time (default 15:15).',
    'Realized P&L is after estimated Groww F&O charges (₹40 brokerage + STT 0.1% on sell premium + exchange/SEBI/stamp + GST).',
  ] },
  { heading: '4. What to expect', points: [
    'This breakout version has NOT been backtested yet. The earlier one-trade-a-day version lost money in the Aug–Oct 2026 backtest, so treat the paper run as a forward test.',
    'Needs a Groww profile with a valid API token — without it the option chain is unavailable and no trade is taken.',
  ] },
];

const NSE_DN_FIELDS = [
  { key: 'symbol', label: 'Symbol', type: 'select', options: [{value:'NIFTY',label:'NIFTY'},{value:'BANKNIFTY',label:'BANKNIFTY'},{value:'FINNIFTY',label:'FINNIFTY'},{value:'MIDCPNIFTY',label:'MIDCPNIFTY'},{value:'SENSEX',label:'SENSEX'}], default: 'NIFTY' },
  { key: 'lots', label: 'Number of Lots', type: 'number', default: 1 },
  { key: 'lot_size', label: 'Lot Size (override)', type: 'number', default: 65, hint: 'NIFTY=65, BANKNIFTY=30' },
  { key: 'target_delta', label: 'Target Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'delta_tolerance', label: 'Delta Tolerance', type: 'number', step: '0.01', default: 0.05 },
  { key: 'premium_threshold', label: 'Adjustment Threshold (%)', type: 'number', default: 40, hint: 'Trigger adjustment when premium rises this %' },
  { key: 'tp_percent', label: 'Take Profit (% of Premium)', type: 'number', default: 70 },
  { key: 'sl_percent', label: 'Stop Loss (% of Premium)', type: 'number', default: 70 },
  { key: 'max_adjustments', label: 'Max Adjustments', type: 'number', default: 5 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 15 },
];

const NSE_EMA_SPREAD_FIELDS = [
  { key: 'symbol', label: 'Symbol', type: 'select', options: [{value:'NIFTY',label:'NIFTY'},{value:'BANKNIFTY',label:'BANKNIFTY'},{value:'FINNIFTY',label:'FINNIFTY'},{value:'MIDCPNIFTY',label:'MIDCPNIFTY'},{value:'SENSEX',label:'SENSEX'}], default: 'NIFTY' },
  { key: 'lots', label: 'Number of Lots', type: 'number', default: 1 },
  { key: 'lot_size', label: 'Lot Size (override)', type: 'number', default: 65, hint: 'NIFTY=65, BANKNIFTY=30' },
  { key: 'ema_period', label: 'EMA Period', type: 'number', default: 10 },
  { key: 'sell_delta', label: 'Sell Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'buy_delta', label: 'Buy Delta', type: 'number', step: '0.01', default: 0.10 },
  { key: 'tp_pct', label: 'Take Profit (% of Premium)', type: 'number', default: 90 },
  { key: 'sl_pct', label: 'Stop Loss (% of Premium)', type: 'number', default: 150 },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon,1=Tue,2=Wed,3=Thu,4=Fri' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 10 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 25 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 15 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 15 },
];

const PIVOT_ST_FIELDS = [
  { key: 'lot_size', label: 'Lot Size', type: 'number', default: 100 },
  { key: 'st_period', label: 'SuperTrend Period', type: 'number', default: 7 },
  { key: 'st_multiplier', label: 'SuperTrend Multiplier', type: 'number', default: 3 },
  { key: 'max_trades', label: 'Max Trades / Day', type: 'number', default: 3 },
  { key: 'target_delta', label: 'Target Delta (ATM)', type: 'number', step: '0.01', default: 0.50, hint: '0.50 = ATM' },
  { key: 'delta_tolerance', label: 'Delta Tolerance', type: 'number', step: '0.01', default: 0.15 },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 9 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 20 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 17 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 0 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 10 },
];

const PORTFOLIO_STRANGLE_FIELDS = [
  { key: 'lot_size', label: 'Lot Size (per entry)', type: 'number', default: 30, hint: '30 lots × 3 entries = 90 total/day' },
  { key: 'otm_index', label: 'OTM Index', type: 'number', default: 5, hint: 'OTM5 = 5th strike from ATM' },
  { key: 'sl_pct', label: 'Stop Loss (%)', type: 'number', default: 300, hint: '300 = premium triples (200% loss)' },
  { key: 'recost_entries', label: 'Recost Re-entries', type: 'number', default: 1, hint: 'Re-enter if premium drops back to entry after SL' },
  { key: 'entry_times', label: 'Entry Times (comma-sep)', type: 'text', default: '9:15,10:20,11:15', hint: '3 entries per day' },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 17 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 29 },
  { key: 'skip_weekdays', label: 'Skip Days (0=Mon..6=Sun)', type: 'text', default: '4,6', hint: '4=Fri, 6=Sun' },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 10 },
];

const HYBRID_FIELDS = [
  { key: 'lot_size', label: 'Sell Lot Size', type: 'number', default: 1, hint: 'Lots for selling legs' },
  { key: 'buy_multiplier', label: 'Buy Multiplier', type: 'number', default: 10, hint: 'Multiply lots on switch (e.g. 10x)' },
  { key: 'otm_index', label: 'OTM Index', type: 'number', default: 5, hint: 'OTM5 = 5th strike from ATM' },
  { key: 'sell_sl_pct', label: 'Sell SL (%)', type: 'number', default: 200, hint: '200 = premium doubles' },
  { key: 'buy_sl_pct', label: 'Buy SL (%)', type: 'number', default: 50, hint: '50 = lose half premium' },
  { key: 'trail_points', label: 'Trail Points ($)', type: 'number', default: 10, hint: 'Trailing SL on buy legs' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 19 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 15 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 17 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 10 },
];

export default function Strategy() {
  const [sp] = useSearchParams();
  const [activeTab, setActiveTab] = useState(sp.get('strategy') || 'delta_neutral');
  const [profiles, setProfiles] = useState([]);
  const [enabledKeys, setEnabledKeys] = useState(null);

  useEffect(() => {
    api.get('/profiles').then(r => setProfiles(r.data.profiles || []));
    api.get('/enabled-strategies').then(r => setEnabledKeys(r.data.enabled)).catch(() => setEnabledKeys(null));
  }, []);

  const visibleStrategies = enabledKeys === null ? STRATEGIES : STRATEGIES.filter(s => enabledKeys.includes(s.key));

  const dnStart = async (config) => {
    const { data } = await api.post('/start', config);
    return data;
  };
  const dnStop = async (sid) => { await api.post('/stop', { sid }); };

  const renderDnStatus = (s) => {
    const call = s.call || {}, put = s.put || {};
    return (
      <>
        <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
          <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl || 0).toFixed(2)} <span style={{ fontSize: '.7rem' }}>({(s.pnl_pct || 0) >= 0 ? '+' : ''}{(s.pnl_pct || 0).toFixed(1)}%)</span></div></div>
          <div className="stat-card"><div className="label">Premium Collected</div><div className="value">${(s.total_premium || 0).toFixed(2)}</div></div>
          <div className="stat-card"><div className="label">TP / SL</div><div className="value" style={{ fontSize: '.85rem' }}><span style={{ color: 'var(--green)' }}>+${(s.target_pnl || 0).toFixed(2)}</span> / <span style={{ color: 'var(--red)' }}>-${(s.stop_loss || 0).toFixed(2)}</span></div></div>
          <div className="stat-card"><div className="label">Adjustments</div><div className="value">{s.adjustment_count || 0}</div></div>
        </div>
        <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)', marginBottom: 12 }}>
          <div className="stat-card"><div className="label">Realized</div><div className="value" style={{ color: (s.realized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.realized_pnl || 0).toFixed(2)}</div></div>
          <div className="stat-card"><div className="label">Unrealized</div><div className="value" style={{ color: (s.unrealized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.unrealized_pnl || 0).toFixed(2)}</div></div>
          <div className="stat-card"><div className="label">P&L Progress</div><div className="value">{s.total_premium > 0 ? <div style={{ background: 'var(--border)', borderRadius: 4, height: 8, width: '100%', marginTop: 4 }}><div style={{ background: (s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)', borderRadius: 4, height: 8, width: `${Math.min(100, Math.abs(s.pnl_pct || 0))}%` }}></div></div> : '—'}</div></div>
        </div>
        <div className="grid-2" style={{ marginBottom: 12 }}>
          {[['📈 Short Call', call], ['📉 Short Put', put]].map(([label, leg]) => {
            if (!leg?.symbol) return <div className="card" key={label} style={{ margin: 0 }}><div style={{ fontWeight: 700 }}>{label}</div><div style={{ color: 'var(--muted)', fontSize: '.85rem' }}>No position</div></div>;
            const chg = leg.entry ? ((leg.mark - leg.entry) / leg.entry * 100) : 0;
            return (
              <div className="card" key={label} style={{ margin: 0 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}><span style={{ fontWeight: 700 }}>{label}</span><span style={{ fontWeight: 800, color: (leg.payoff || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(leg.payoff || 0).toFixed(2)}</span></div>
                {[['Symbol', leg.symbol], ['Strike', leg.strike], ['Entry', `$${(leg.entry || 0).toFixed(2)}`], ['Mark', <><b>${(leg.mark || 0).toFixed(2)}</b> <span style={{ fontSize: '.72rem', color: chg >= 0 ? 'var(--red)' : 'var(--green)' }}>({chg >= 0 ? '+' : ''}{chg.toFixed(2)}%)</span></>], ['Delta', (leg.delta || 0).toFixed(4)], ['Size', `${leg.size} lots`]].map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: '.85rem', borderBottom: '1px solid var(--border)' }}><span style={{ color: 'var(--muted)' }}>{k}</span><span style={{ fontWeight: 600 }}>{v}</span></div>
                ))}
              </div>
            );
          })}
        </div>
        {(s.adjustment_history || []).length > 0 && (
          <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
            <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>🔄 Adjustments</div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
              <thead><tr>{['#', 'Leg', 'Symbol', 'Entry', 'Exit', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
              <tbody>{s.adjustment_history.map((a, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '4px 8px', fontWeight: 700 }}>{a.adjustment}</td>
                  <td style={{ padding: '4px 8px' }}><span className={`badge ${a.leg === 'call' ? 'badge-green' : 'badge-red'}`}>{a.leg.toUpperCase()}</span></td>
                  <td style={{ padding: '4px 8px', fontSize: '.78rem' }}>{a.symbol}</td>
                  <td style={{ padding: '4px 8px' }}>${a.entry.toFixed(2)}</td>
                  <td style={{ padding: '4px 8px' }}>${a.exit.toFixed(2)}</td>
                  <td style={{ padding: '4px 8px', fontWeight: 700, color: a.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${a.pnl.toFixed(2)}</td>
                </tr>))}</tbody>
            </table>
          </div>
        )}
      </>
    );
  };

  return (
    <div className="container">
      <div className="page-title">Strategies</div>
      <StrategySelector strategies={visibleStrategies} activeKey={activeTab} onSelect={setActiveTab} />

      {activeTab === 'delta_neutral' && (
        <StrategyTemplate title="Delta Neutral" icon="⚡" type="Options" description="Short strangle with auto-rebalancing" profiles={profiles}
          configFields={DN_FIELDS} onStart={dnStart} onStop={dnStop}
          statusEndpoint="/status" streamEndpoint="/stream" renderStatus={renderDnStatus} />
      )}
      {activeTab === 'rsi_div_mss' && (
        <StrategyTemplate signalMode signalKey="rsi_div_mss" title="RSI Div + MSS" icon="📊" type="Futures"
          description="RSI divergence (RSI >70 / <30) + close through market structure. SL at the 2nd swing, TP 1:1. Closed candles only." profiles={profiles} />
      )}
      {activeTab === 'sma_vol_breakout' && (
        <StrategyTemplate signalMode signalKey="sma_vol_breakout" title="SMA + Volume Breakout" icon="📈" type="Futures"
          description="Strong breakouts above SMA50 confirmed by high increasing volume" profiles={profiles} />
      )}
      {activeTab === 'box_theory' && (
        <StrategyTemplate signalMode signalKey="box_theory" title="Box Theory" icon="📦" type="Futures"
          description="Previous day's high/low box — buy at bottom zone, sell at top zone, skip the middle" profiles={profiles} />
      )}
      {activeTab === 'ema_trendline' && (
        <StrategyTemplate signalMode signalKey="ema_trendline" title="EMA + Trendline Breakout" icon="📐" type="Futures"
          description="Fresh close through a confirmed swing trendline, with the 100 EMA trend. SL at last swing (min 0.5%), TP 2R. Use BTC 1H." profiles={profiles}
          details={EMA_TRENDLINE_DETAILS} />
      )}
      {activeTab === 'ema920_pullback' && (
        <StrategyTemplate signalMode signalKey="ema920_pullback" title="9/20 EMA Pullback" icon="🔄" type="Futures"
          description="Buy pullbacks above 9/20 EMA, sell rallies below. Skip when price chops through EMAs." profiles={profiles} />
      )}
      {activeTab === 'darvas_box' && (
        <StrategyTemplate signalMode signalKey="darvas_box" title="Darvas Box Breakout" icon="🏗️" type="Futures"
          description="Buy breakouts above Darvas Box consolidation with volume > 1.5x average confirmation." profiles={profiles} />
      )}
      {activeTab === 'fib_retracement' && (
        <StrategyTemplate signalMode signalKey="fib_retracement" title="Fibonacci Retracement" icon="🌀" type="Futures"
          description="Buy/sell on pullbacks to 0.382/0.618 Fibonacci levels from swing H/L." profiles={profiles} />
      )}
      {activeTab === 'fvg' && (
        <StrategyTemplate signalMode signalKey="fvg" title="Fair Value Gap" icon="⚡" type="Futures"
          description="Detect imbalance candles and trade when price revisits the gap zone." profiles={profiles} />
      )}
      {activeTab === 'supply_demand' && (
        <StrategyTemplate signalMode signalKey="supply_demand" title="Supply & Demand Zones" icon="🧱" type="Futures"
          description="Order blocks — trade when price revisits zones of previous strong moves." profiles={profiles} />
      )}
      {activeTab === 'candle_patterns' && (
        <StrategyTemplate signalMode signalKey="candle_patterns" title="Candlestick Patterns" icon="🕯️" type="Futures"
          description="Engulfing / Hammer / Shooting Star with the 100 EMA trend and volume ≥ 1.5× average. SL 1.5 ATR, TP 2R. Use BTC 4H."
          profiles={profiles} defaultTf="4h" details={CANDLE_PATTERN_DETAILS} />
      )}
      {activeTab === 'vol_imbalance' && (
        <StrategyTemplate signalMode signalKey="vol_imbalance" title="Volume Imbalance" icon="🐋" type="Futures"
          description="Institutional footprint: 10x volume spike → consolidation → breakout entry with 1:4 R:R." profiles={profiles} />
      )}
      {activeTab === 'iv_crush' && (
        <StrategyTemplate title="IV Crush" icon="💎" type="Options" description="Sell ATM straddle when IV is overpriced. Profit from IV crush." profiles={profiles}
          configFields={IVC_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/iv-crush/start', config); return data; }}
          onStop={async (sid) => { await api.post('/iv-crush/stop', { sid }); }}
          statusEndpoint="/iv-crush/status" streamEndpoint="/iv-crush/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">P&L %</div><div className="value" style={{ color: (s.pnl_pct||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{(s.pnl_pct||0).toFixed(1)}%</div></div>
                <div className="stat-card"><div className="label">IV Crush</div><div className="value" style={{ color: (s.iv_crush_pct||0) > 0 ? 'var(--green)' : 'var(--muted)' }}>{s.iv_crush_pct||0}%</div></div>
                <div className="stat-card"><div className="label">Premium</div><div className="value">${(s.total_premium||0).toFixed(2)}</div></div>
              </div>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">IV at Entry</div><div className="value">{(s.iv_at_entry||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Current IV</div><div className="value">{(s.current_iv||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">IV/RV Ratio</div><div className="value">{(s.iv_rv_ratio||0).toFixed(2)}</div></div>
              </div>
              <div className="grid-2" style={{ marginBottom: 12 }}>
                {[['📈 Short Call', s.call], ['📉 Short Put', s.put]].map(([label, leg]) => (
                  <div className="card" key={label} style={{ margin: 0 }}>
                    <div style={{ fontWeight: 700, marginBottom: 6 }}>{label}</div>
                    {leg ? [['Symbol', leg.symbol], ['Strike', leg.strike], ['Entry', `$${leg.entry.toFixed(2)}`], ['Mark', `$${leg.mark.toFixed(2)}`]].map(([k,v]) => (
                      <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', fontSize: '.85rem', borderBottom: '1px solid var(--border)' }}><span style={{ color: 'var(--muted)' }}>{k}</span><span style={{ fontWeight: 600 }}>{v}</span></div>
                    )) : <div style={{ color: 'var(--muted)', fontSize: '.85rem' }}>No position</div>}
                  </div>
                ))}
              </div>
            </>
          )} />
      )}
      {activeTab === 'confluence_scalp' && (
        <StrategyTemplate signalMode signalKey="confluence_scalp" title="Confluence Scalp" icon="🎯" type="Futures"
          description="Trendline break + S/R bounce + EMA reclaim — high-probability scalp entries with volume confirmation." profiles={profiles} />
      )}
      {activeTab === 'call_ratio' && (
        <StrategyTemplate title="Call Ratio Spread" icon="📐" type="Options" description="Monthly call ratio: Buy 1 OTM call, Sell 2 further OTM, Buy 1 hedge. No downside risk, 2.5% monthly target." profiles={profiles}
          configFields={CR_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/call-ratio/start', config); return data; }}
          onStop={async (sid) => { await api.post('/call-ratio/stop', { sid }); }}
          statusEndpoint="/call-ratio/status" streamEndpoint="/call-ratio/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">P&L %</div><div className="value" style={{ color: (s.pnl_pct||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{(s.pnl_pct||0).toFixed(1)}%</div></div>
                <div className="stat-card"><div className="label">Margin</div><div className="value">${(s.deployed_margin||0).toFixed(2)}</div></div>
              </div>
              {s.legs && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Side', 'Strike', 'Size', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side.toUpperCase()} {l.size}</span></td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>{l.size}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${l.mark.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: l.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${l.pnl.toFixed(2)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'renko_redbar' && (
        <StrategyTemplate signalMode signalKey="renko_redbar" title="Renko Red Bar" icon="🧱" type="Futures"
          description="ATR-based Renko trend + EMA 10/30 + SMA 150. Skip first candle, enter on Red Bar reversal. OTM option buying." profiles={profiles} />
      )}
      {activeTab === 'prev_day_breakout' && (
        <StrategyTemplate title="Prev Day Breakout + Retest" icon="🚀" type="Futures"
          description="Previous day H/L breakout → retest with bullish/bearish hammer or engulfing confirmation. SL below pattern, 1:3 R:R."
          profiles={profiles}
          configFields={[
            { key: 'timeframe', label: 'Timeframe', type: 'text', default: '15m', hint: '15m or 1h' },
            { key: 'lots', label: 'Lot Size', type: 'number', default: 10 },
            { key: 'scan_interval', label: 'Scan Interval (s)', type: 'number', default: 30 },
            { key: 'max_trades_per_day', label: 'Max Trades/Day', type: 'number', default: 3 },
          ]}
          onStart={async (config) => {
            const { data } = await api.post('/futures-signal/start', { signal_key: 'prev_day_breakout', asset: config.asset, timeframe: config.timeframe, lots: parseInt(config.lots), scan_interval: parseInt(config.scan_interval), max_trades_per_day: parseInt(config.max_trades_per_day), profile_id: config.profile_id });
            return data;
          }}
          onStop={async (sid) => { await api.post('/futures-signal/stop', { sid }); }}
          statusEndpoint="/futures-signal/logs"
          streamEndpoint="/futures-signal/stream"
          renderStatus={(s) => (
            <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Status</div><div className="value" style={{ color: s.running ? 'var(--green)' : 'var(--muted)' }}>{s.running ? '🟢 Running' : '⏹ Stopped'}</div></div>
                <div className="stat-card"><div className="label">Scans</div><div className="value">{s.scan_count || 0}</div></div>
                <div className="stat-card"><div className="label">Trades Today</div><div className="value">{s.trades_today || 0}</div></div>
                <div className="stat-card"><div className="label">Total Trades</div><div className="value">{(s.logs || []).length}</div></div>
              </div>
              {(s.logs || []).length > 0 && (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                  <thead><tr>{['Time', 'Side', 'Price', 'SL', 'TP', 'Status'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                  <tbody>{(s.logs || []).slice(-10).reverse().map((t, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td style={{ padding: '4px 8px' }}>{t.time}</td>
                      <td style={{ padding: '4px 8px' }}><span className={`badge ${t.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{t.side?.toUpperCase()}</span></td>
                      <td style={{ padding: '4px 8px', fontWeight: 600 }}>${t.price}</td>
                      <td style={{ padding: '4px 8px', color: 'var(--red)' }}>${t.sl}</td>
                      <td style={{ padding: '4px 8px', color: 'var(--green)' }}>${t.tp}</td>
                      <td style={{ padding: '4px 8px', fontWeight: 700, color: t.success ? 'var(--green)' : 'var(--red)' }}>{t.success ? '✓ Filled' : '✗ Failed'}</td>
                    </tr>))}</tbody>
                </table>
              )}
            </div>
          )}
        />
      )}
      {activeTab === 'daily_strangle' && (
        <StrategyTemplate title="Daily Strangle (0DTE)" icon="🎯" type="Options" description="Sell ~$100 call + put at 9 AM IST, 100% SL per leg, exit 5:15 PM. Daily recurring." profiles={profiles}
          configFields={STRANGLE_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/strangle/start', config); return data; }}
          onStop={async (sid) => { await api.post('/strangle/stop', { sid }); }}
          statusEndpoint="/strangle/status" streamEndpoint="/strangle/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl || 0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Total (incl. Open)</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Active Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Type', 'Strike', 'Entry', 'Mark', 'SL', 'P&L', 'Status'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.type === 'call' ? 'badge-green' : 'badge-red'}`}>{l.type.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', color: 'var(--red)' }}>${(l.entry_price * 2).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px' }}>{l.stopped ? <span className="badge badge-red">Stopped</span> : <span className="badge badge-green">Active</span>}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'P&L', 'Exit Reason'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'both_sl' ? '🛑 Both SL' : t.exit_reason === 'one_sl' ? '⚠️ One SL' : '⏰ EOD Exit'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'nse_strangle' && (
        <StrategyTemplate title="NSE Strangle" icon="🇮🇳" type="Options" description="Live short strangle on NIFTY/BANKNIFTY. Sell OTM call + put at target premium with per-leg SL." profiles={profiles}
          configFields={NSE_STRANGLE_FIELDS}
          onStart={async (config) => {
            const c = { ...config };
            if (typeof c.trading_days === 'string') c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim()));
            const { data } = await api.post('/nse-strangle/start', c);
            return data;
          }}
          onStop={async (sid) => { await api.post('/nse-strangle/stop', { sid }); }}
          statusEndpoint="/nse-strangle/status" streamEndpoint="/nse-strangle/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.session_pnl || 0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Total (incl. Open)</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Active Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Type', 'Strike', 'Entry', 'Mark', 'SL', 'P&L', 'Status'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.type === 'call' ? 'badge-green' : 'badge-red'}`}>{l.type.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>₹{l.entry_price.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>₹{(l.mark_price || 0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', color: 'var(--red)' }}>₹{(l.entry_price * 2).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(l.pnl || 0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px' }}>{l.stopped ? <span className="badge badge-red">Stopped</span> : <span className="badge badge-green">Active</span>}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'P&L', 'Exit Reason'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'both_sl' ? '🛑 Both SL' : t.exit_reason?.includes('sl') ? '⚠️ SL Hit' : '⏰ EOD Exit'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'nse_delta_neutral' && (
        <StrategyTemplate title="NSE Delta Neutral" icon="🎯🇮🇳" type="Options" description="Delta-neutral short strangle on NSE indices. Sells at matching deltas, adjusts when premium spikes. TP/SL as % of premium collected." profiles={profiles}
          configFields={NSE_DN_FIELDS}
          onStart={async (config) => {
            const c = { ...config };
            if (typeof c.trading_days === 'string') c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim()));
            const { data } = await api.post('/nse-dn/start', c);
            return data;
          }}
          onStop={async (sid) => { await api.post('/nse-dn/stop', { sid }); }}
          statusEndpoint="/nse-dn/status" streamEndpoint="/nse-dn/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Realized P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Adjustments</div><div className="value">{s.adjustment_count || 0} / {s.max_adjustments || 3}</div></div>
                <div className="stat-card"><div className="label">Weeks Traded</div><div className="value">{s.weeks_traded || 0}</div></div>
              </div>
              {s.premium_collected > 0 && (
                <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)', marginBottom: 12 }}>
                  <div className="stat-card"><div className="label">Premium Collected</div><div className="value">₹{(s.premium_collected||0).toFixed(2)}</div></div>
                  <div className="stat-card"><div className="label">Target Profit</div><div className="value" style={{ color: 'var(--green)' }}>₹{(s.target_pnl||0).toFixed(2)}</div></div>
                  <div className="stat-card"><div className="label">Stop Loss</div><div className="value" style={{ color: 'var(--red)' }}>₹{(s.stop_loss||0).toFixed(2)}</div></div>
                </div>
              )}
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Active Positions</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Type', 'Strike', 'Delta', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.type === 'call' ? 'badge-green' : 'badge-red'}`}>{l.type.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>{(l.delta||0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px' }}>₹{(l.entry_price||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>₹{(l.current_mark||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.current_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(l.current_pnl||0).toFixed(2)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'P&L', 'Adj', 'Exit Reason'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.adjustments || 0}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'target_profit' ? '🎯 TP' : t.exit_reason === 'stop_loss' ? '🛑 SL' : t.exit_reason === 'max_adjustments' ? '⚙️ Max Adj' : '⏰ EOD'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'nse_ema_spread' && (
        <StrategyTemplate title="NSE EMA Credit Spread" icon="📈🇮🇳" type="Options" description="Daily EMA10-based credit spread on NSE indices. Bearish = bear call, bullish = bull put. 90% TP / 150% SL." profiles={profiles}
          configFields={NSE_EMA_SPREAD_FIELDS}
          onStart={async (config) => {
            const c = { ...config };
            if (typeof c.trading_days === 'string') c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim()));
            const { data } = await api.post('/nse-ema/start', c);
            return data;
          }}
          onStop={async (sid) => { await api.post('/nse-ema/stop', { sid }); }}
          statusEndpoint="/nse-ema/status" streamEndpoint="/nse-ema/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
                <div className="stat-card"><div className="label">Direction</div><div className="value">{s.direction || '—'}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Spread Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Side', 'Type', 'Strike', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'sell' ? 'badge-red' : 'badge-green'}`}>{l.side.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px' }}>{(l.type||'').toUpperCase()}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>₹{(l.entry_price||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>₹{(l.current_mark||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.current_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(l.current_pnl||0).toFixed(2)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Direction', 'P&L', 'Exit'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.direction === 'bear_call' ? '🐻 Bear Call' : '🐂 Bull Put'}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'target' ? '🎯 TP' : t.exit_reason === 'stoploss' ? '🛑 SL' : '⏰ EOD'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'nse_dual_momentum' && (
        <StrategyTemplate title="NSE Dual Momentum" icon="⚖️🇮🇳" type="Equity" description="Intraday long/short NSE stock basket. Buys top-N gainers, shorts top-N losers (₹10,000 each, whole shares). TP +5% / SL -3% side-aware, squared off at 3:15 PM." profiles={profiles}
          configFields={NSE_DUAL_MOMENTUM_FIELDS}
          onStart={async (config) => {
            const c = { ...config };
            if (typeof c.trading_days === 'string') c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim()));
            const { data } = await api.post('/nse-dual-momentum/start', c);
            return data;
          }}
          onStop={async (sid) => { await api.post('/nse-dual-momentum/stop', { sid }); }}
          statusEndpoint="/nse-dual-momentum/status" streamEndpoint="/nse-dual-momentum/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.session_pnl || 0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Total (incl. Open)</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Open Positions</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Symbol', 'Side', 'Qty', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.symbol}</td>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side === 'buy' ? 'LONG' : 'SHORT'}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.qty}</td>
                        <td style={{ padding: '4px 8px' }}>₹{(l.entry_price||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>₹{(l.mark_price||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{(l.pnl||0).toFixed(2)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Time', 'Action', 'Symbol', 'Qty', 'Price', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{(t.ts || '').slice(11, 16) || t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.action}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{t.symbol}</td>
                        <td style={{ padding: '4px 8px' }}>{t.qty}</td>
                        <td style={{ padding: '4px 8px' }}>₹{(t.price||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (t.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{t.pnl != null ? `₹${t.pnl}` : '—'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'crypto_futures' && (
        <StrategyTemplate title="Crypto Scanner Futures" icon="🪙⚡" type="Futures"
          description="Delta Exchange perpetual futures from the Crypto Scanner. Set the dollars per trade — lots are calculated from it. Paper or LIVE (LIVE needs your Delta Exchange profile)." profiles={profiles}
          configFields={CRYPTO_FUTURES_FIELDS} details={CRYPTO_FUTURES_DETAILS}
          onStart={async (config) => {
            const live = String(config.dry_run) === 'false';
            if (live && !window.confirm(`Start LIVE trading on Delta Exchange?\n\nReal market orders, $${config.amount_usd} per trade, up to ${config.max_positions} positions at once.`)) {
              return { error: 'Cancelled — LIVE start not confirmed' };
            }
            const { data } = await api.post('/crypto-futures/start', config);
            return data;
          }}
          onStop={async (sid) => { await api.post('/crypto-futures/stop', { sid }); }}
          statusEndpoint="/crypto-futures/status" streamEndpoint="/crypto-futures/stream"
          renderStatus={(s) => {
            const money = v => `${(v || 0) >= 0 ? '+' : '−'}$${Math.abs(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
            const col = v => ((v || 0) >= 0 ? 'var(--green)' : 'var(--red)');
            const px = v => (v == null ? '–' : Number(v).toPrecision(6).replace(/\.?0+$/, ''));
            const th = { textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' };
            const td = { padding: '4px 8px' };
            const reasons = { take_profit: 'Target', stop_loss: 'Stop', time_stop: 'Time stop', manual_stop: 'Manual' };
            return (
              <>
                <div style={{ marginBottom: 8 }}><span className={s.mode === 'LIVE' ? 'badge badge-red' : 'badge badge-yellow'}>{s.mode === 'LIVE' ? '🔴 LIVE — real orders' : '📝 PAPER'}</span>
                  {s.next_scan_in_s != null && <span style={{ fontSize: '.72rem', color: 'var(--muted)', marginLeft: 8 }}>next scan in {Math.ceil(s.next_scan_in_s / 60)} min</span>}</div>
                <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                  <div className="stat-card"><div className="label">Realized P&L (net)</div><div className="value" style={{ color: col(s.cumulative_pnl) }}>{money(s.cumulative_pnl)}</div><div className="sub">fees ${(s.total_fees || 0).toFixed(2)}</div></div>
                  <div className="stat-card"><div className="label">Total (incl. open)</div><div className="value" style={{ color: col(s.total_pnl) }}>{money(s.total_pnl)}</div></div>
                  <div className="stat-card"><div className="label">Trades</div><div className="value">{s.total_trades || 0}</div><div className="sub">{s.win_rate != null ? `${s.win_rate}% win of ${s.closed_trades} closed` : 'none closed yet'}</div></div>
                  <div className="stat-card"><div className="label">Last scan</div><div className="value" style={{ fontSize: '.9rem' }}>{s.last_scan ? (s.last_scan.taken?.length ? s.last_scan.taken.join(', ') : 'no entry') : '—'}</div><div className="sub">{s.last_scan ? new Date(s.last_scan.time).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' }) + ' IST' : 'starting…'}</div></div>
                </div>
                {s.legs && s.legs.length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12, overflowX: 'auto' }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Open Positions</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['Symbol', 'Side', 'Lots', 'Notional', 'Entry', 'Mark', 'Stop', 'Target', 'P&L'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{s.legs.map((l, i) => (
                        <tr key={i}>
                          <td style={{ ...td, fontWeight: 700 }}>{l.symbol}<div style={{ fontSize: '.64rem', color: 'var(--muted)', fontWeight: 400 }}>{l.sector}</div></td>
                          <td style={td}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side === 'buy' ? 'LONG' : 'SHORT'}</span></td>
                          <td style={td} title={`${l.contract_value} coin(s) per lot`}>{l.lots}</td>
                          <td style={td}>${(l.notional || 0).toFixed(2)}</td>
                          <td style={td}>{px(l.entry_price)}</td>
                          <td style={{ ...td, fontWeight: 600 }}>{px(l.current_mark)}</td>
                          <td style={{ ...td, color: 'var(--red)' }}>{px(l.stop)}</td>
                          <td style={{ ...td, color: 'var(--green)' }}>{px(l.target)}</td>
                          <td style={{ ...td, fontWeight: 700, color: col(l.current_pnl) }}>{money(l.current_pnl)}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
                {(s.trade_log || []).length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, overflowX: 'auto' }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['Time (IST)', 'Action', 'Symbol', 'Lots', 'Price', 'Notional', 'Exit', 'Net P&L'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{(s.trade_log || []).slice(-20).reverse().map((t, i) => (
                        <tr key={i}>
                          <td style={td}>{new Date(t.ts).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}</td>
                          <td style={td}><span className={`badge ${t.action === 'BUY' ? 'badge-green' : t.action === 'SELL' ? 'badge-red' : ''}`}>{t.action.startsWith('CLOSE') ? 'CLOSE' : t.action === 'BUY' ? 'LONG' : 'SHORT'}</span></td>
                          <td style={{ ...td, fontWeight: 600 }}>{t.symbol}</td>
                          <td style={td}>{t.lots}</td>
                          <td style={td}>{px(t.price)}</td>
                          <td style={td}>{t.notional != null ? `$${t.notional.toFixed(2)}` : '—'}</td>
                          <td style={td}>{t.reason ? (reasons[t.reason] || t.reason) : '—'}{t.held_h != null ? ` · ${t.held_h}h` : ''}</td>
                          <td style={{ ...td, fontWeight: 700, color: col(t.pnl) }}>{t.pnl != null ? money(t.pnl) : '—'}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
              </>
            );
          }} />
      )}
      {activeTab === 'nse_scanner_overnight' && (
        <StrategyTemplate title="Scanner Overnight Options (Paper)" icon="🌙🇮🇳" type="Options"
          description="At 15:15 paper-buys the ATM CALL / PUT of the top Sector Scanner setup in each bullish/bearish sector (max 5, one per sector, F&O stocks, expiry ≥ 15 days else next month) and sells them at 09:17 the next trading day. Select a Groww profile." profiles={profiles}
          configFields={NSE_SCANNER_OVERNIGHT_FIELDS} details={NSE_SCANNER_OVERNIGHT_DETAILS}
          onStart={async (config) => {
            const c = { ...config, mode: 'overnight' };
            if (typeof c.trading_days === 'string') c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim(), 10)).filter(n => !isNaN(n));
            const { data } = await api.post('/nse-sector-options/start', c);
            return data;
          }}
          onStop={async (sid) => { await api.post('/nse-sector-options/stop', { sid }); }}
          statusEndpoint="/nse-sector-options/status" streamEndpoint="/nse-sector-options/stream"
          renderStatus={(s) => {
            const money = v => `${(v || 0) >= 0 ? '+' : '−'}₹${Math.abs(Math.round(v || 0)).toLocaleString('en-IN')}`;
            const col = v => ((v || 0) >= 0 ? 'var(--green)' : 'var(--red)');
            const th = { textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' };
            const td = { padding: '4px 8px' };
            const reasons = { next_day_0917: '09:17 exit', next_day_no_ltp: '09:17 (last mark)', manual_stop: 'Manual' };
            return (
              <>
                <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                  <div className="stat-card"><div className="label">Realized P&L (net)</div><div className="value" style={{ color: col(s.cumulative_pnl) }}>{money(s.cumulative_pnl)}</div><div className="sub">charges ₹{Math.round(s.total_charges || 0).toLocaleString('en-IN')}</div></div>
                  <div className="stat-card"><div className="label">Total (incl. open)</div><div className="value" style={{ color: col(s.total_pnl) }}>{money(s.total_pnl)}</div></div>
                  <div className="stat-card"><div className="label">Trades / days</div><div className="value">{s.total_trades || 0} / {s.days_traded || 0}</div></div>
                  <div className="stat-card"><div className="label">Next</div><div className="value" style={{ fontSize: '.85rem' }}>{s.next_action || '—'}</div><div className="sub">{s.last_scan?.picks?.length ? `last picks: ${s.last_scan.picks.join(', ')}` : ''}</div></div>
                </div>
                {s.legs && s.legs.length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>🌙 Overnight Paper Positions ({s.legs.length})</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['#', 'Option', 'Qty', 'Buy', 'LTP', 'P&L', 'Stock', 'Stock now', 'Bought'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{s.legs.map((l, i) => (
                        <tr key={i}>
                          <td style={td}>{l.rank}</td>
                          <td style={{ ...td, fontWeight: 700 }}>{l.symbol}<div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 400 }}>{l.direction} · {l.sector} · exp {l.expiry}</div></td>
                          <td style={td}>{l.qty}</td>
                          <td style={td}>₹{(l.entry_price || 0).toFixed(2)}</td>
                          <td style={{ ...td, fontWeight: 600 }}>₹{(l.current_mark || 0).toFixed(2)}</td>
                          <td style={{ ...td, fontWeight: 700, color: col(l.current_pnl) }}>{money(l.current_pnl)}</td>
                          <td style={td}>{l.underlying} @ {l.stock_entry}</td>
                          <td style={{ ...td, fontWeight: 600 }}>{l.stock_mark}</td>
                          <td style={td}>{(l.opened_at || '').slice(0, 16).replace('T', ' ')}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
                {(s.trade_log || []).length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Paper Trade Log</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['Date', 'Time', 'Action', 'Option', 'Qty', 'Premium', 'Stock', 'Note', 'Net P&L'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{(s.trade_log || []).slice(-30).reverse().map((t, i) => (
                        <tr key={i}>
                          <td style={td}>{t.date}</td>
                          <td style={td}>{(t.ts || '').slice(11, 16)}</td>
                          <td style={td}><span className={`badge ${t.action === 'BUY' ? 'badge-green' : 'badge-red'}`}>{t.action}</span></td>
                          <td style={{ ...td, fontWeight: 600 }}>{t.symbol}</td>
                          <td style={td}>{t.qty}</td>
                          <td style={td}>₹{(t.price || 0).toFixed(2)}</td>
                          <td style={td}>{t.underlying} {t.stock_price ?? ''}</td>
                          <td style={td}>{t.reason ? (reasons[t.reason] || t.reason) : '—'}</td>
                          <td style={{ ...td, fontWeight: 700, color: col(t.pnl) }}>{t.pnl != null ? money(t.pnl) : '—'}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
              </>
            );
          }} />
      )}
      {activeTab === 'nse_sector_options' && (
        <StrategyTemplate title="NSE Sector Options (Paper)" icon="🧭🇮🇳" type="Options"
          description="Rebuilds the watchlist every minute from the current top bullish/bearish sectors and paper-buys the ATM CALL / PUT when a stock's 5-min candle closes beyond its 09:15–09:45 range and the 5-min VWAP (max 5 open, 6 a day). Exits on the stock's SL/TP or at 15:15. Select a Groww profile." profiles={profiles}
          configFields={NSE_SECTOR_OPTIONS_FIELDS} details={NSE_SECTOR_OPTIONS_DETAILS}
          onStart={async (config) => {
            const c = { ...config };
            if (typeof c.trading_days === 'string') c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim(), 10)).filter(n => !isNaN(n));
            const { data } = await api.post('/nse-sector-options/start', c);
            return data;
          }}
          onStop={async (sid) => { await api.post('/nse-sector-options/stop', { sid }); }}
          statusEndpoint="/nse-sector-options/status" streamEndpoint="/nse-sector-options/stream"
          renderStatus={(s) => {
            const money = v => `${(v || 0) >= 0 ? '+' : '−'}₹${Math.abs(Math.round(v || 0)).toLocaleString('en-IN')}`;
            const col = v => ((v || 0) >= 0 ? 'var(--green)' : 'var(--red)');
            const th = { textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' };
            const td = { padding: '4px 8px' };
            const reasons = { take_profit: 'Target', stop_loss: 'Stop', square_off: '15:15', manual_stop: 'Manual' };
            return (
              <>
                <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                  <div className="stat-card"><div className="label">Realized P&L (net)</div><div className="value" style={{ color: col(s.cumulative_pnl) }}>{money(s.cumulative_pnl)}</div><div className="sub">charges ₹{Math.round(s.total_charges || 0).toLocaleString('en-IN')}</div></div>
                  <div className="stat-card"><div className="label">Total (incl. open)</div><div className="value" style={{ color: col(s.total_pnl) }}>{money(s.total_pnl)}</div></div>
                  <div className="stat-card"><div className="label">Trades</div><div className="value">{s.total_trades || 0}</div></div>
                  <div className="stat-card"><div className="label">Today</div><div className="value" style={{ fontSize: '.95rem' }}>{(s.watchlist || []).filter(w => w.status === 'watching').length} watching · {s.entries_today || 0}/{s.params?.max_entries ?? 6} entries</div><div className="sub">{s.entered_day && s.entered_day === s.last_scan?.day ? `done for ${s.entered_day}` : (s.last_scan?.top_sectors ? `${(s.last_scan.time || '').slice(11, 16)} top: ${s.last_scan.top_sectors}` : 'waiting for 09:45')}</div></div>
                </div>
                {(s.watchlist || []).length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>👀 Breakout Watchlist (30-min range 09:15–09:45)</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['Stock', 'Side', 'Range low', 'Range high', 'Last close', '5m VWAP', 'Status'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{s.watchlist.map((w, i) => (
                        <tr key={i}>
                          <td style={{ ...td, fontWeight: 700 }}>{w.symbol}<div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 400 }}>{w.sector} {w.sector_pchg > 0 ? '+' : ''}{w.sector_pchg}%</div></td>
                          <td style={{ ...td, color: w.direction === 'LONG' ? 'var(--green)' : 'var(--red)', fontWeight: 600 }}>{w.direction}</td>
                          <td style={td}>{w.or_low}</td>
                          <td style={td}>{w.or_high}</td>
                          <td style={{ ...td, fontWeight: 600 }}>{w.last_close ?? '—'}{w.last_candle ? <span style={{ color: 'var(--muted)', fontSize: '.66rem' }}> @{w.last_candle}</span> : null}</td>
                          <td style={td}>{w.vwap ?? '—'}</td>
                          <td style={td}>{w.status}{w.note && w.status === 'watching' ? <div style={{ fontSize: '.66rem', color: 'var(--muted)' }}>{w.note}</div> : null}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
                {s.legs && s.legs.length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Open Paper Positions ({s.legs.length}/{s.params?.max_open ?? 5})</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['Option', 'Qty', 'Buy', 'LTP', 'P&L', 'Stock', 'Stock now', 'Stock SL', 'Stock TP'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{s.legs.map((l, i) => (
                        <tr key={i}>
                          <td style={{ ...td, fontWeight: 700 }}>{l.symbol}<div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 400 }}>{l.direction} · {l.sector}</div></td>
                          <td style={td}>{l.qty}</td>
                          <td style={td}>₹{(l.entry_price || 0).toFixed(2)}</td>
                          <td style={{ ...td, fontWeight: 600 }}>₹{(l.current_mark || 0).toFixed(2)}</td>
                          <td style={{ ...td, fontWeight: 700, color: col(l.current_pnl) }}>{money(l.current_pnl)}</td>
                          <td style={td}>{l.underlying} @ {l.stock_entry}</td>
                          <td style={{ ...td, fontWeight: 600 }}>{l.stock_mark}</td>
                          <td style={{ ...td, color: 'var(--red)' }}>{l.stock_stop}</td>
                          <td style={{ ...td, color: 'var(--green)' }}>{l.stock_target}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
                {(s.trade_log || []).length > 0 && (
                  <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                    <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Paper Trade Log</div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                      <thead><tr>{['Date', 'Time', 'Action', 'Option', 'Qty', 'Premium', 'Stock', 'Exit', 'Net P&L'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
                      <tbody>{(s.trade_log || []).slice(-20).reverse().map((t, i) => (
                        <tr key={i}>
                          <td style={td}>{t.date}</td>
                          <td style={td}>{(t.ts || '').slice(11, 16)}</td>
                          <td style={td}><span className={`badge ${t.action === 'BUY' ? 'badge-green' : 'badge-red'}`}>{t.action}</span></td>
                          <td style={{ ...td, fontWeight: 600 }}>{t.symbol}</td>
                          <td style={td}>{t.qty}</td>
                          <td style={td}>₹{(t.price || 0).toFixed(2)}</td>
                          <td style={td}>{t.underlying} {t.stock_price ?? ''}</td>
                          <td style={td}>{t.reason ? (reasons[t.reason] || t.reason) : '—'}</td>
                          <td style={{ ...td, fontWeight: 700, color: col(t.pnl) }}>{t.pnl != null ? money(t.pnl) : '—'}</td>
                        </tr>))}</tbody>
                    </table>
                  </div>
                )}
              </>
            );
          }} />
      )}
      {activeTab === 'pivot_supertrend' && (
        <StrategyTemplate title="Pivot + SuperTrend (0DTE)" icon="📡" type="Options" description="Directional ATM option selling: SuperTrend(7,3) + Pivot R1/S1. Exit on ST flip or 5 PM." profiles={profiles}
          configFields={PIVOT_ST_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/pivot-st/start', config); return data; }}
          onStop={async (sid) => { await api.post('/pivot-st/stop', { sid }); }}
          statusEndpoint="/pivot-st/status" streamEndpoint="/pivot-st/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Total (incl. Open)</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
                <div className="stat-card"><div className="label">Today's Trades</div><div className="value">{s.today_trades || 0} / {s.max_trades || 3}</div></div>
              </div>
              {(s.pivot || s.r1 || s.s1) && (
                <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                  <div className="stat-card"><div className="label">Pivot</div><div className="value">${s.pivot || '-'}</div></div>
                  <div className="stat-card"><div className="label">R1</div><div className="value" style={{ color: 'var(--green)' }}>${s.r1 || '-'}</div></div>
                  <div className="stat-card"><div className="label">S1</div><div className="value" style={{ color: 'var(--red)' }}>${s.s1 || '-'}</div></div>
                  <div className="stat-card"><div className="label">SuperTrend</div><div className="value" style={{ color: s.st_direction === 'bullish' ? 'var(--green)' : 'var(--red)' }}>{s.st_direction === 'bullish' ? '↑ Bullish' : s.st_direction === 'bearish' ? '↓ Bearish' : '—'}</div></div>
                </div>
              )}
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Active Position</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Signal', 'Type', 'Strike', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.signal === 'sell_put' ? 'badge-green' : 'badge-red'}`}>{l.signal === 'sell_put' ? '🐂 SELL PUT' : '🐻 SELL CALL'}</span></td>
                        <td style={{ padding: '4px 8px' }}>{(l.type || '').toUpperCase()}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Trades', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.trades || 0}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'portfolio_strangle' && (
        <StrategyTemplate title="Portfolio Strangle (0DTE)" icon="📊" type="Options" description="3-entry OTM5 strangle with recost re-entry. Time-diversified, skip Fri & Sun." profiles={profiles}
          configFields={PORTFOLIO_STRANGLE_FIELDS}
          onStart={async (config) => {
            const c = {...config};
            c.entry_times = (c.entry_times || '9:15,10:20,11:15').split(',').map(t => t.trim());
            c.skip_weekdays = (c.skip_weekdays || '4,6').split(',').map(d => parseInt(d.trim()));
            const { data } = await api.post('/portfolio-strangle/start', c); return data;
          }}
          onStop={async (sid) => { await api.post('/portfolio-strangle/stop', { sid }); }}
          statusEndpoint="/portfolio-strangle/status" streamEndpoint="/portfolio-strangle/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl || 0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Total (incl. Open)</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Active Legs ({s.legs.length})</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Type', 'Strike', 'Entry', 'Mark', 'SL', 'P&L', 'Status'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.type === 'call' ? 'badge-green' : 'badge-red'}`}>{l.type.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', color: 'var(--red)' }}>${(l.entry_price * 3).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px' }}>{l.stopped ? <span className="badge badge-red">Stopped</span> : <span className="badge badge-green">Active</span>}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Slots', 'SLs', 'Recost', 'P&L', 'Exit'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.slots || 3}</td>
                        <td style={{ padding: '4px 8px' }}>{t.sl_count || 0}</td>
                        <td style={{ padding: '4px 8px' }}>{t.recost_count || 0}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'eod' ? '⏰ EOD' : t.exit_reason}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'hybrid_switch' && (
        <StrategyTemplate title="Hybrid Switch BTST" icon="⚡" type="Options" description="Sell strangle at 7:15 PM, on SL hit switch to 10x buying with trailing SL. BTST." profiles={profiles}
          configFields={HYBRID_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/hybrid/start', config); return data; }}
          onStop={async (sid) => { await api.post('/hybrid/stop', { sid }); }}
          statusEndpoint="/hybrid/status" streamEndpoint="/hybrid/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl || 0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Total (incl. Open)</div><div className="value" style={{ color: (s.total_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Active Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Role', 'Type', 'Strike', 'Size', 'Entry', 'Mark', 'P&L', 'Status'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.role === 'sell' ? 'badge-red' : 'badge-green'}`}>{l.role === 'sell' ? 'SELL' : '⚡ BUY'}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.type.toUpperCase()}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>{l.size}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px' }}>{l.active ? <span className="badge badge-green">Active</span> : <span className="badge badge-red">Closed</span>}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'P&L', 'Sell Legs', 'Buy Activated'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.sell_legs}</td>
                        <td style={{ padding: '4px 8px' }}>{t.buy_legs_activated || 0}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'oi_strategy' && (
        <StrategyTemplate title="OI Strategy" icon="🔍" type="Options" description="Daily option selling at max Open Interest strikes. Runs at 6:30 PM IST." profiles={profiles}
          configFields={OI_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/oi-strategy/start', config); return data; }}
          onStop={async (sid) => { await api.post('/oi-strategy/stop', { sid }); }}
          statusEndpoint="/oi-strategy/status" streamEndpoint="/oi-strategy/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Today P&L</div><div className="value" style={{ color: (s.today_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.today_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Cumulative</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Premium</div><div className="value">${(s.max_premium||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Max Call OI</div><div className="value">{s.max_call_oi_strike || '-'}</div></div>
                <div className="stat-card"><div className="label">Max Put OI</div><div className="value">{s.max_put_oi_strike || '-'}</div></div>
                <div className="stat-card"><div className="label">Spot</div><div className="value">{s.spot_price ? `$${s.spot_price.toFixed(0)}` : '-'}</div></div>
              </div>
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'P&L', 'Premium', 'Exit'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>${t.premium}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'target' ? '🎯' : t.exit_reason === 'stoploss' ? '🛑' : '⏹'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'weekly_dn' && (
        <StrategyTemplate title="Weekly Delta Neutral" icon="📅" type="Options" description="Runs Delta Neutral every Friday 9 PM IST. Auto-repeats weekly." profiles={profiles}
          configFields={WDN_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/weekly-dn/start', config); return data; }}
          onStop={async (sid) => { await api.post('/weekly-dn/stop', { sid }); }}
          statusEndpoint="/weekly-dn/status" streamEndpoint="/weekly-dn/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Current P&L</div><div className="value" style={{ color: (s.current_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.current_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Cumulative</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(2)}</div></div>
                <div className="stat-card"><div className="label">Weeks Traded</div><div className="value">{s.weeks_traded || 0}</div></div>
                <div className="stat-card"><div className="label">Active Sessions</div><div className="value">{s.active_count || 0}</div></div>
              </div>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Start Day</div><div className="value">{s.start_day || 'Friday'}</div></div>
                <div className="stat-card"><div className="label">Entry Time</div><div className="value">{s.entry_time || '21:00'} IST</div></div>
                <div className="stat-card"><div className="label">Expiry Week</div><div className="value">Week {s.expiry_week || 3}</div></div>
                <div className="stat-card"><div className="label">TP / SL</div><div className="value">{s.tp_percent || 70}% / {s.sl_percent || 70}%</div></div>
              </div>
              {(s.active_sessions || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>🟢 Active Sessions</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Session', 'Expiry', 'Call', 'Put', 'P&L', 'Adj'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.active_sessions || []).map((sess, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{sess.session_id}</td>
                        <td style={{ padding: '4px 8px' }}>{sess.expiry}</td>
                        <td style={{ padding: '4px 8px' }}>{sess.call_strike || '—'}</td>
                        <td style={{ padding: '4px 8px' }}>{sess.put_strike || '—'}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (sess.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(sess.pnl||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px' }}>{sess.adjustments}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.sessions || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Session History</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Session', 'Expiry', 'Status', 'P&L', 'Adj', 'Started'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.sessions || []).slice(-10).reverse().map((sess, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{sess.session_id}</td>
                        <td style={{ padding: '4px 8px' }}>{sess.expiry}</td>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${sess.status === 'completed' ? 'badge-green' : sess.status === 'running' ? 'badge-blue' : 'badge-red'}`}>{sess.status}</span></td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (sess.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(sess.pnl||0).toFixed(2)}</td>
                        <td style={{ padding: '4px 8px' }}>{sess.adjustments}</td>
                        <td style={{ padding: '4px 8px', fontSize: '.72rem' }}>{sess.started_at ? sess.started_at.split('T')[0] : '—'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Weekly Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Session', 'P&L', 'Adjustments'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{t.session_id || `S${t.week}`}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>{t.adjustments}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'ema_spread' && (
        <StrategyTemplate title="EMA Credit Spread" icon="📉" type="Options" description="Daily EMA14 direction → credit spread. 90% TP / 100% SL. Runs at 6:30 PM IST." profiles={profiles}
          configFields={EMA_SPREAD_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/ema-spread/start', config); return data; }}
          onStop={async (sid) => { await api.post('/ema-spread/stop', { sid }); }}
          statusEndpoint="/ema-spread/status" streamEndpoint="/ema-spread/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl||s.today_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl||s.today_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Net Premium</div><div className="value">${(s.net_premium||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Current Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Side', 'Type', 'Strike', 'Delta', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.type.toUpperCase()}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>{l.delta.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Direction', 'P&L', 'Premium', 'Exit'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.direction === 'bear_call' ? '🐻 Bear Call' : '🐂 Bull Put'}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>${t.premium}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'target' ? '🎯' : t.exit_reason === 'stoploss' ? '🛑' : '⏹'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'ema_spread_scheduled' && (
        <StrategyTemplate title="EMA Spread (Scheduled)" icon="📉🗓️" type="Options" description="EMA14 credit spread on selected weekdays. 90% TP / 100% SL. Runs at 6:30 PM IST on chosen days." profiles={profiles}
          configFields={EMA_SPREAD_SCHEDULED_FIELDS}
          onStart={async (config) => { const c = { ...config }; if (typeof c.trading_days === 'string') { c.trading_days = c.trading_days.split(',').map(d => parseInt(d.trim())).filter(n => !isNaN(n)); } const { data } = await api.post('/ema-spread-scheduled/start', c); return data; }}
          onStop={async (sid) => { await api.post('/ema-spread-scheduled/stop', { sid }); }}
          statusEndpoint="/ema-spread-scheduled/status" streamEndpoint="/ema-spread-scheduled/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl||s.today_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl||s.today_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Net Premium</div><div className="value">${(s.net_premium||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Current Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Side', 'Type', 'Strike', 'Delta', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.type.toUpperCase()}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>{l.delta.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Direction', 'P&L', 'Premium', 'Exit'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.direction === 'bear_call' ? '🐻 Bear Call' : '🐂 Bull Put'}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>${t.premium}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'target' ? '🎯' : t.exit_reason === 'stoploss' ? '🛑' : '⏹'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'ema_spread_v2' && (
        <StrategyTemplate title="EMA Credit Spread V2" icon="📉✌️" type="Options" description="EMA14 credit spread with two-tier partial exits. Books half at TP1/SL1, remainder at TP2/SL2. Runs at 6:30 PM IST." profiles={profiles}
          configFields={EMA_SPREAD_V2_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/ema-spread-v2/start', config); return data; }}
          onStop={async (sid) => { await api.post('/ema-spread-v2/stop', { sid }); }}
          statusEndpoint="/ema-spread-v2/status" streamEndpoint="/ema-spread-v2/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl||s.today_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl||s.today_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Net Premium</div><div className="value">${(s.net_premium||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Current Legs</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Side', 'Type', 'Strike', 'Delta', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side.toUpperCase()}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.type.toUpperCase()}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.strike}</td>
                        <td style={{ padding: '4px 8px' }}>{l.delta.toFixed(2)}</td>
                        <td style={{ padding: '4px 8px' }}>${l.entry_price.toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(4)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Date', 'Direction', 'P&L', 'Premium', 'Exit'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{t.date}</td>
                        <td style={{ padding: '4px 8px' }}>{t.direction === 'bear_call' ? '🐻 Bear Call' : '🐂 Bull Put'}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl}</td>
                        <td style={{ padding: '4px 8px' }}>${t.premium}</td>
                        <td style={{ padding: '4px 8px' }}>{t.exit_reason === 'target' ? '🎯' : t.exit_reason === 'stoploss' ? '🛑' : '⏹'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'ema_trend' && (
        <StrategyTemplate title="EMA Trend Follower" icon="🚀" type="Futures" description="Daily EMA10/30 long/short trend following over the 10 most liquid Delta perps, with a BTC regime filter. Exits when the trend flips. Evaluates once a day after 00:00 UTC." profiles={profiles}
          details={EMA_TREND_DETAILS}
          configFields={EMA_TREND_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/ema-trend/start', config); return data; }}
          onStop={async (sid) => { await api.post('/ema-trend/stop', { sid }); }}
          statusEndpoint="/ema-trend/status" streamEndpoint="/ema-trend/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Open Positions</div><div className="value">{s.open_positions || 0}</div></div>
                <div className="stat-card"><div className="label">Total Trades</div><div className="value">{s.total_trades || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Open Positions</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Coin', 'Symbol', 'Side', 'Size', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.coin || ''}</td>
                        <td style={{ padding: '4px 8px' }}>{l.symbol}</td>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side === 'buy' ? 'LONG' : 'SHORT'}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.size}</td>
                        <td style={{ padding: '4px 8px' }}>${(l.entry_price || 0).toFixed(6)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(6)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Time', 'Action', 'Symbol', 'Lots', 'Price', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{(t.ts || '').slice(0, 19).replace('T', ' ')}</td>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${t.action === 'BUY' ? 'badge-green' : 'badge-red'}`}>{t.action}</span></td>
                        <td style={{ padding: '4px 8px' }}>{t.symbol}</td>
                        <td style={{ padding: '4px 8px' }}>{t.lots}</td>
                        <td style={{ padding: '4px 8px' }}>${(t.price || 0).toFixed(6)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (t.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{t.pnl != null ? `$${t.pnl}` : '—'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
      {activeTab === 'dual_momentum' && (
        <StrategyTemplate title="Dual Momentum Basket" icon="⚖️" type="Futures" description="Twice-daily long/short basket. At 6 AM & 6 PM IST, longs the top-N 24h gainers and shorts the top-N losers ($10 each). TP +10% / SL -5% per position; closes any coin no longer in its side's list." profiles={profiles}
          configFields={DUAL_MOMENTUM_FIELDS}
          onStart={async (config) => { const { data } = await api.post('/dual-momentum/start', config); return data; }}
          onStop={async (sid) => { await api.post('/dual-momentum/stop', { sid }); }}
          statusEndpoint="/dual-momentum/status" streamEndpoint="/dual-momentum/stream"
          renderStatus={(s) => (
            <>
              <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
                <div className="stat-card"><div className="label">Session P&L</div><div className="value" style={{ color: (s.session_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.session_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Cumulative P&L</div><div className="value" style={{ color: (s.cumulative_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.cumulative_pnl||0).toFixed(4)}</div></div>
                <div className="stat-card"><div className="label">Open Positions</div><div className="value">{s.open_positions || 0}</div></div>
                <div className="stat-card"><div className="label">Total Trades</div><div className="value">{s.total_trades || 0}</div></div>
              </div>
              {s.legs && s.legs.length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📊 Open Positions</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Coin', 'Symbol', 'Side', 'Size', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{s.legs.map((l, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>{l.coin || ''}</td>
                        <td style={{ padding: '4px 8px' }}>{l.symbol}</td>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${l.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{l.side === 'buy' ? 'LONG' : 'SHORT'}</span></td>
                        <td style={{ padding: '4px 8px' }}>{l.size}</td>
                        <td style={{ padding: '4px 8px' }}>${(l.entry_price || 0).toFixed(6)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 600 }}>${(l.mark_price || 0).toFixed(6)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (l.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(l.pnl || 0).toFixed(4)}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
              {(s.trade_log || []).length > 0 && (
                <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                  <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 8 }}>📋 Trade Log</div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
                    <thead><tr>{['Time', 'Action', 'Symbol', 'Lots', 'Price', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                    <tbody>{(s.trade_log || []).slice(-10).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 8px' }}>{(t.ts || '').slice(0, 19).replace('T', ' ')}</td>
                        <td style={{ padding: '4px 8px' }}><span className={`badge ${t.action === 'BUY' ? 'badge-green' : (t.action === 'SELL' ? 'badge-red' : 'badge')}`}>{t.action}</span></td>
                        <td style={{ padding: '4px 8px' }}>{t.symbol}</td>
                        <td style={{ padding: '4px 8px' }}>{t.lots}</td>
                        <td style={{ padding: '4px 8px' }}>${(t.price || 0).toFixed(6)}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: (t.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{t.pnl != null ? `$${t.pnl}` : '—'}</td>
                      </tr>))}</tbody>
                  </table>
                </div>
              )}
            </>
          )} />
      )}
    </div>
  );
}
