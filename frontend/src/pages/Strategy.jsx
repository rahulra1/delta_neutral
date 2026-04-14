import React, { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../api';
import SignalStrategy from '../components/SignalStrategy';
import { StrategySelector } from '../components/StrategyInfoCard';

const STRATEGIES = [
  { key: 'delta_neutral', label: 'Delta Neutral', icon: '⚡', type: 'Options',
    desc: 'Sells a call and put at matching deltas (short strangle). Monitors premiums and rebalances when one leg spikes. Auto-exits at target P&L.',
    features: ['Short Strangle', 'Auto Rebalance', 'Premium Monitoring', 'WebSocket'] },
  { key: 'rsi_div_mss', label: 'Div + MSS', icon: '📊', type: 'Futures',
    desc: 'RSI Divergence + Market Structure Shift. Detects momentum divergence with price structure breaks for high-probability reversal entries.',
    features: ['RSI Divergence', 'Structure Shift', 'Swing Points', '2:1 R:R'] },
  { key: 'sma_vol_breakout', label: 'SMA + Breakout', icon: '📈', type: 'Futures',
    desc: 'Price Action + SMA50 + Volume. Enters on strong breakouts confirmed by high increasing volume (institutional participation).',
    features: ['SMA 50', 'Volume Confirm', 'Trend Entry', '2:1 R:R'] },
];

export default function Strategy() {
  const [sp] = useSearchParams();
  const [activeTab, setActiveTab] = useState(sp.get('strategy') || 'delta_neutral');
  const [profiles, setProfiles] = useState([]);

  useEffect(() => { api.get('/profiles').then(r => setProfiles(r.data.profiles || [])); }, []);

  return (
    <div className="container">
      <div className="page-title">Strategies</div>

      {/* Strategy Tabs */}
      <StrategySelector strategies={STRATEGIES} activeKey={activeTab} onSelect={setActiveTab} />

      {/* Delta Neutral */}
      {activeTab === 'delta_neutral' && <DeltaNeutralPanel profiles={profiles} asset={sp.get('asset') || 'BTC'} />}

      {/* Div + MSS */}
      {activeTab === 'rsi_div_mss' && <SignalStrategy strategyKey="rsi_div_mss" title="RSI Divergence + MSS" icon="📊" description="Detects RSI divergence combined with market structure shifts (HH/HL/LH/LL) for reversal entries. SL at swing point, TP at 2:1 R:R." profiles={profiles} />}

      {/* SMA + Breakout */}
      {activeTab === 'sma_vol_breakout' && <SignalStrategy strategyKey="sma_vol_breakout" title="SMA + Volume Breakout" icon="📈" description="Enters when price crosses SMA50 with high increasing volume (institutional participation). Entry on next candle break of high. SL below breakout candle. 2:1 R:R." profiles={profiles} />}
    </div>
  );
}

// ── Delta Neutral Panel ──

function DeltaNeutralPanel({ profiles, asset: initAsset }) {
  const [asset, setAsset] = useState(initAsset);
  const [sid, setSid] = useState(null);
  const [running, setRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const logRef = useRef(null);
  const esRef = useRef(null);
  const pollRef = useRef(null);

  const [form, setForm] = useState({
    expiry_date: '', lot_size: 10, target_delta: 0.20, delta_tolerance: 0.05,
    premium_threshold: 40, target_pnl: 10, monitoring_interval: 5,
    max_adjustments: 10, profile_id: ''
  });

  useEffect(() => { if (profiles.length && !form.profile_id) setForm(f => ({ ...f, profile_id: profiles[0].id })); }, [profiles]);
  useEffect(() => () => { esRef.current?.close(); clearInterval(pollRef.current); }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const start = async () => {
    setError(''); setLogs([]);
    try {
      const { data } = await api.post('/start', { ...form, asset });
      if (data.error) { setError(data.error); return; }
      setSid(data.sid); setRunning(true);
      connectStream(data.sid); startPoll(data.sid);
    } catch (e) { setError(e.response?.data?.error || 'Failed to start'); }
  };

  const stop = async () => {
    if (!sid) return;
    await api.post('/stop', { sid });
    setRunning(false); esRef.current?.close(); clearInterval(pollRef.current);
  };

  const connectStream = id => {
    esRef.current?.close();
    const es = new EventSource(`/api/stream/${id}?token=${localStorage.getItem('token')}`);
    es.onmessage = e => { setLogs(l => [...l, e.data]); if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; };
    esRef.current = es;
  };

  const startPoll = id => {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try { const { data } = await api.get(`/status/${id}`); setStatus(data); if (!data.running) { setRunning(false); clearInterval(pollRef.current); esRef.current?.close(); } } catch {}
    }, 3000);
  };

  const s = status || {};
  const call = s.call || {};
  const put = s.put || {};

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: '1rem' }}>⚡ Delta Neutral — Options Strategy</div>
          <div style={{ fontSize: '.78rem', color: 'var(--muted)', marginTop: 2 }}>Sells a call and put at matching deltas. Monitors and rebalances when premiums deviate.</div>
        </div>
        <div style={{ display: 'flex', borderRadius: 6, overflow: 'hidden', border: '1px solid var(--border)' }}>
          {['BTC', 'ETH'].map(a => <button key={a} onClick={() => setAsset(a)} style={{ padding: '6px 16px', border: 'none', fontSize: '.82rem', fontWeight: 700, cursor: 'pointer', background: asset === a ? 'var(--accent)' : 'var(--card)', color: asset === a ? '#fff' : 'var(--muted)' }}>{a === 'BTC' ? '₿ BTC' : '⟠ ETH'}</button>)}
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      <div className="grid-3" style={{ gap: 10, marginBottom: 12 }}>
        <div className="field"><label>Expiry (DD-MM-YYYY)</label><input value={form.expiry_date} onChange={e => set('expiry_date', e.target.value)} placeholder="29-05-2026" /></div>
        <div className="field"><label>Lot Size</label><input type="number" value={form.lot_size} onChange={e => set('lot_size', +e.target.value)} /></div>
        <div className="field"><label>Target Delta</label><input type="number" step="0.01" value={form.target_delta} onChange={e => set('target_delta', +e.target.value)} /></div>
        <div className="field"><label>Delta Tolerance</label><input type="number" step="0.01" value={form.delta_tolerance} onChange={e => set('delta_tolerance', +e.target.value)} /></div>
        <div className="field"><label>Premium Threshold (%)</label><input type="number" value={form.premium_threshold} onChange={e => set('premium_threshold', +e.target.value)} /></div>
        <div className="field"><label>Target P&L ($)</label><input type="number" value={form.target_pnl} onChange={e => set('target_pnl', +e.target.value)} /></div>
        <div className="field"><label>Monitor Interval (s)</label><input type="number" value={form.monitoring_interval} onChange={e => set('monitoring_interval', +e.target.value)} /></div>
        <div className="field"><label>Max Adjustments</label><input type="number" value={form.max_adjustments} onChange={e => set('max_adjustments', +e.target.value)} /></div>
        <div className="field"><label>API Profile</label><select value={form.profile_id} onChange={e => set('profile_id', e.target.value)}><option value="">Default</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        <button className="btn btn-green" onClick={start} disabled={running}>▶ Start</button>
        <button className="btn btn-red" onClick={stop} disabled={!running}>■ Stop</button>
        {sid && <span style={{ fontSize: '.8rem', color: 'var(--muted)', alignSelf: 'center' }}>SID: {sid}</span>}
      </div>

      {/* Live Stats */}
      {status && (
        <>
          <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
            <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Realized</div><div className="value" style={{ color: (s.realized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.realized_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Unrealized</div><div className="value" style={{ color: (s.unrealized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.unrealized_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Adjustments</div><div className="value">{s.adjustment_count || 0}/{form.max_adjustments}</div></div>
          </div>
          <div className="grid-2" style={{ marginBottom: 12 }}>
            {[['📈 Short Call', call], ['📉 Short Put', put]].map(([label, leg]) => {
              if (!leg?.symbol) return <div className="card" key={label} style={{ margin: 0 }}><div style={{ fontWeight: 700 }}>{label}</div><div style={{ color: 'var(--muted)', fontSize: '.85rem' }}>No position</div></div>;
              const chg = leg.entry ? ((leg.mark - leg.entry) / leg.entry * 100) : 0;
              return (
                <div className="card" key={label} style={{ margin: 0 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}><span style={{ fontWeight: 700 }}>{label}</span><span style={{ fontWeight: 800, color: (leg.payoff || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(leg.payoff || 0).toFixed(2)}</span></div>
                  {[['Symbol', leg.symbol], ['Strike', leg.strike], ['Entry', `$${(leg.entry || 0).toFixed(2)}`], ['Mark', <><span style={{ fontWeight: 700 }}>${(leg.mark || 0).toFixed(2)}</span> <span style={{ fontSize: '.72rem', color: chg >= 0 ? 'var(--red)' : 'var(--green)' }}>({chg >= 0 ? '+' : ''}{chg.toFixed(2)}%)</span></>], ['Delta', (leg.delta || 0).toFixed(4)], ['Size', `${leg.size} lots`]].map(([k, v]) => (
                    <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: '.85rem', borderBottom: '1px solid var(--border)' }}><span style={{ color: 'var(--muted)' }}>{k}</span><span style={{ fontWeight: 600 }}>{v}</span></div>
                  ))}
                  <div style={{ marginTop: 6 }}><div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '.7rem', color: 'var(--muted)' }}><span>Premium Δ</span><span>{form.premium_threshold}% threshold</span></div>
                    <div style={{ height: 5, borderRadius: 3, background: 'var(--border)', marginTop: 3, overflow: 'hidden' }}><div style={{ height: '100%', borderRadius: 3, width: `${Math.min(100, Math.abs(chg) / form.premium_threshold * 100)}%`, background: Math.abs(chg) >= form.premium_threshold ? 'var(--red)' : '#f59e0b' }} /></div>
                  </div>
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
      )}

      {/* Logs */}
      <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 6 }}>Live Logs</div>
      <div ref={logRef} style={{ background: '#0f172a', color: '#e2e8f0', padding: 14, borderRadius: 8, height: 220, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.78rem', whiteSpace: 'pre-wrap' }}>
        {logs.length ? logs.map((l, i) => <div key={i}>{l}</div>) : <span style={{ color: '#64748b' }}>Waiting for logs...</span>}
      </div>
    </div>
  );
}
