import React, { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../api';
import { StrategySelector } from '../components/StrategyInfoCard';
import StrategyTemplate from '../components/StrategyTemplate';

const STRATEGIES = [
  { key: 'delta_neutral', label: 'Delta Neutral', icon: '⚡', type: 'Options',
    desc: 'Sells a call and put at matching deltas. Monitors premiums and rebalances when one leg spikes. Auto-exits at target P&L.',
    features: ['Short Strangle', 'Auto Rebalance', 'Premium Monitoring', 'WebSocket'] },
  { key: 'rsi_div_mss', label: 'Div + MSS', icon: '📊', type: 'Futures',
    desc: 'RSI Divergence + Market Structure Shift. High-probability reversal entries with defined risk.',
    features: ['RSI Divergence', 'Structure Shift', 'Swing Points', '2:1 R:R'] },
  { key: 'sma_vol_breakout', label: 'SMA + Breakout', icon: '📈', type: 'Futures',
    desc: 'Price Action + SMA50 + Volume. Strong breakouts confirmed by high increasing volume.',
    features: ['SMA 50', 'Volume Confirm', 'Trend Entry', '2:1 R:R'] },
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
    </div>
  );
}
