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
    desc: 'Monthly call ratio: Buy 1 OTM, Sell 2 further OTM, Hedge. No downside risk. 90%+ win rate.',
    features: ['Call Ratio 1:2', 'No Downside Risk', 'Set & Forget', '2.5% Monthly'],
    rec: '⏱ Monthly expiry · Enter last Friday · BTC/ETH options' },
  { key: 'renko_redbar', label: 'Renko Red Bar', icon: '🧱', type: 'Futures',
    desc: 'ATR-based Renko trend + Red Bar reversal entry. Skip first candle, trade the trap. OTM option buying.',
    features: ['Renko Line', 'Red Bar Entry', 'EMA 10/30', 'SMA 150'],
    rec: '⏱ Best on 5M/15M · Intraday scalp · BTC, NIFTY, BANKNIFTY' },
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
  { key: 'buy_offset', label: 'Buy Offset (pts from spot)', type: 'number', default: 300, hint: 'OTM distance for buy leg' },
  { key: 'sell_offset', label: 'Sell Offset (pts from spot)', type: 'number', default: 600, hint: 'OTM distance for sell legs (2x lots)' },
  { key: 'hedge_offset', label: 'Hedge Offset (pts from spot)', type: 'number', default: 1000, hint: 'OTM distance for hedge' },
  { key: 'target_pct', label: 'Target Profit (%)', type: 'number', step: '0.5', default: 2.5 },
  { key: 'sl_pct', label: 'Stop Loss (%)', type: 'number', step: '0.5', default: 3.0 },
  { key: 'monitoring_interval', label: 'Monitor Interval (s)', type: 'number', default: 30 },
];

export default function Strategy() {
  const [sp] = useSearchParams();
  const [activeTab, setActiveTab] = useState(sp.get('strategy') || 'delta_neutral');
  const [profiles, setProfiles] = useState([]);

  useEffect(() => { api.get('/profiles').then(r => setProfiles(r.data.profiles || [])); }, []);

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
          <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl || 0).toFixed(2)}</div></div>
          <div className="stat-card"><div className="label">Realized</div><div className="value" style={{ color: (s.realized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.realized_pnl || 0).toFixed(2)}</div></div>
          <div className="stat-card"><div className="label">Unrealized</div><div className="value" style={{ color: (s.unrealized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.unrealized_pnl || 0).toFixed(2)}</div></div>
          <div className="stat-card"><div className="label">Adjustments</div><div className="value">{s.adjustment_count || 0}</div></div>
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
      <StrategySelector strategies={STRATEGIES} activeKey={activeTab} onSelect={setActiveTab} />

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
    </div>
  );
}
