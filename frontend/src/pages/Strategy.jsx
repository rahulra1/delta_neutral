import React, { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../api';
import { StrategySelector } from '../components/StrategyInfoCard';
import StrategyTemplate from '../components/StrategyTemplate';

const STRATEGIES = [
  { key: 'delta_neutral', label: 'Delta Neutral', icon: '⚡', type: 'Options',
    desc: 'Sells a call and put at matching deltas. Monitors premiums and rebalances when one leg spikes. Auto-exits at target P&L.',
    features: ['Short Strangle', 'Auto Rebalance', 'Premium Monitoring', 'WebSocket'],
    rec: '⏱ Any expiry · BTC/ETH' },
  { key: 'rsi_div_mss', label: 'Div + MSS', icon: '📊', type: 'Futures',
    desc: 'RSI Divergence + Market Structure Shift. High-probability reversal entries with defined risk.',
    features: ['RSI Divergence', 'Structure Shift', 'Swing Points', '2:1 R:R'],
    rec: '⏱ Best on 1H · BTC, NIFTY' },
  { key: 'sma_vol_breakout', label: 'SMA + Breakout', icon: '📈', type: 'Futures',
    desc: 'Price Action + SMA50 + Volume. Strong breakouts confirmed by high increasing volume.',
    features: ['SMA 50', 'Volume Confirm', 'Trend Entry', '2:1 R:R'],
    rec: '⏱ Best on 1H · All assets' },
  { key: 'box_theory', label: 'Box Theory', icon: '📦', type: 'Futures',
    desc: "Previous day's high/low form a box. Buy at the bottom, sell at the top, avoid the middle.",
    features: ['Prev Day H/L', 'Buy Zone', 'Sell Zone', 'Mean Revert'],
    rec: '⏱ Best on 1H (84% WR BANKNIFTY) · 1D for crypto' },
  { key: 'ema_trendline', label: 'EMA + Trendline', icon: '📐', type: 'Futures',
    desc: '200 EMA for trend direction + trendline breakouts. Only longs above EMA, only shorts below.',
    features: ['200 EMA', 'Trendline', 'Breakout', '1:2+ R:R'],
    rec: '⏱ Best on 1H (59% WR BTC) · Volume filtered' },
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
    desc: 'Engulfing, Hammer, Shooting Star patterns with volume confirmation.',
    features: ['Engulfing', 'Hammer', 'Shooting Star', 'Volume'],
    rec: '⏱ Best on 1H/1D · All assets' },
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
    desc: 'Paper-trade short strangle on NIFTY/BANKNIFTY. Sell OTM call + put nearest ₹target premium, configurable SL & trading days.',
    features: ['NSE Index Options', 'Configurable Days', 'Paper Trading', 'Per-Leg SL + Re-entry'],
    rec: '⏱ Market Hours 9:15–3:30 · NIFTY, BANKNIFTY, FINNIFTY' },
  { key: 'nse_delta_neutral', label: 'NSE Delta Neutral', icon: '🎯🇮🇳', type: 'Options',
    desc: 'Delta-neutral short strangle on NSE indices. Sells at matching deltas, adjusts when premium spikes (close opposite, re-enter). TP/SL as % of premium.',
    features: ['Delta-Based Entry', 'Auto Rebalance', 'TP/SL % Premium', 'Market Hours Aware'],
    rec: '⏱ Market Hours 9:15–3:30 · NIFTY, BANKNIFTY' },
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
  { key: 'symbol', label: 'Symbol', type: 'select', options: [{value:'NIFTY',label:'NIFTY'},{value:'BANKNIFTY',label:'BANKNIFTY'},{value:'FINNIFTY',label:'FINNIFTY'},{value:'MIDCPNIFTY',label:'MIDCPNIFTY'}], default: 'NIFTY' },
  { key: 'lots', label: 'Number of Lots', type: 'number', default: 1 },
  { key: 'lot_size', label: 'Lot Size (override)', type: 'number', default: 65, hint: 'NIFTY=65, BANKNIFTY=30, FINNIFTY=65' },
  { key: 'target_premium', label: 'Target Premium (₹)', type: 'number', default: 100, hint: 'Premium per leg to sell' },
  { key: 'sl_pct', label: 'Stop Loss (%)', type: 'number', default: 200, hint: '200 = SL when premium doubles' },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon,1=Tue,2=Wed,3=Thu,4=Fri' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 9 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 20 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 15 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 15 },
];

const NSE_DN_FIELDS = [
  { key: 'symbol', label: 'Symbol', type: 'select', options: [{value:'NIFTY',label:'NIFTY'},{value:'BANKNIFTY',label:'BANKNIFTY'},{value:'FINNIFTY',label:'FINNIFTY'},{value:'MIDCPNIFTY',label:'MIDCPNIFTY'}], default: 'NIFTY' },
  { key: 'lots', label: 'Number of Lots', type: 'number', default: 1 },
  { key: 'lot_size', label: 'Lot Size (override)', type: 'number', default: 65, hint: 'NIFTY=65, BANKNIFTY=30' },
  { key: 'target_delta', label: 'Target Delta', type: 'number', step: '0.01', default: 0.20 },
  { key: 'delta_tolerance', label: 'Delta Tolerance', type: 'number', step: '0.01', default: 0.05 },
  { key: 'premium_threshold', label: 'Adjustment Threshold (%)', type: 'number', default: 40, hint: 'Trigger adjustment when premium rises this %' },
  { key: 'tp_percent', label: 'Take Profit (% of Premium)', type: 'number', default: 70 },
  { key: 'sl_percent', label: 'Stop Loss (% of Premium)', type: 'number', default: 70 },
  { key: 'max_adjustments', label: 'Max Adjustments', type: 'number', default: 5 },
  { key: 'trading_days', label: 'Trading Days', type: 'text', default: '0,1,2,3,4', hint: '0=Mon,1=Tue,2=Wed,3=Thu,4=Fri' },
  { key: 'entry_hour', label: 'Entry Hour (24h IST)', type: 'number', default: 9 },
  { key: 'entry_minute', label: 'Entry Minute', type: 'number', default: 20 },
  { key: 'exit_hour', label: 'Exit Hour (24h IST)', type: 'number', default: 15 },
  { key: 'exit_minute', label: 'Exit Minute', type: 'number', default: 15 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 15 },
  { key: 'paper_trade', label: 'Paper Trade', type: 'select', options: [{value:true,label:'Yes (Paper)'},{value:false,label:'No (Live)'}], default: true },
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
          description="Detects RSI divergence with market structure shifts for reversal entries" profiles={profiles} />
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
        <StrategyTemplate signalMode signalKey="ema_trendline" title="200 EMA + Trendline Breakout" icon="📐" type="Futures"
          description="200 EMA trend filter + trendline breakout entries with 1:2 minimum R:R" profiles={profiles} />
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
          description="Engulfing, Hammer, Shooting Star patterns with volume confirmation." profiles={profiles} />
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
        <StrategyTemplate title="NSE Strangle (Paper)" icon="🇮🇳" type="Options" description="Paper-trade short strangle on NIFTY/BANKNIFTY. Sell OTM call + put at target premium with per-leg SL." profiles={profiles}
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
            if (c.paper_trade === 'true' || c.paper_trade === true) c.paper_trade = true;
            else c.paper_trade = false;
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
                <div className="stat-card"><div className="label">Days Traded</div><div className="value">{s.days_traded || 0}</div></div>
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
    </div>
  );
}
