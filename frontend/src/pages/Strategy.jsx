import React, { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../api';

export default function Strategy() {
  const [sp] = useSearchParams();
  const asset = sp.get('asset') || 'BTC';
  const [profiles, setProfiles] = useState([]);
  const [sid, setSid] = useState(null);
  const [running, setRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const logRef = useRef(null);
  const esRef = useRef(null);
  const pollRef = useRef(null);

  // Div+MSS state
  const [mssSignals, setMssSignals] = useState([]);
  const [mssLoading, setMssLoading] = useState(false);
  const [mssAsset, setMssAsset] = useState('BTC');
  const [mssLots, setMssLots] = useState(10);
  const [mssTf, setMssTf] = useState('1h');
  const [mssBacktest, setMssBacktest] = useState(null);
  const [mssDeployed, setMssDeployed] = useState(false);

  const [form, setForm] = useState({
    expiry_date: '', lot_size: 10, target_delta: 0.20, delta_tolerance: 0.05,
    premium_threshold: 40, target_pnl: 10, monitoring_interval: 5,
    max_adjustments: 10, profile_id: ''
  });

  const fetchMssSignals = (a, tf) => {
    setMssLoading(true);
    setMssBacktest(null);
    api.get('/chart-data', { params: { symbol: a, interval: tf || mssTf, indicators: 'rsi_div_mss' } })
      .then(r => {
        const sigs = r.data?.indicators?.rsi_div_mss?.signals || [];
        setMssSignals(sigs.slice(-10).reverse());
      }).catch(() => setMssSignals([]))
      .finally(() => setMssLoading(false));
  };

  const runBacktest = () => {
    setMssLoading(true);
    api.get('/chart-data', { params: { symbol: mssAsset, interval: mssTf, indicators: 'rsi_div_mss' } })
      .then(r => {
        const sigs = r.data?.indicators?.rsi_div_mss?.signals || [];
        const candles = r.data?.candles || [];
        const lotSize = mssAsset === 'BTC' ? 0.001 : 0.01;
        let totalPnl = 0, wins = 0, losses = 0;

        const trades = sigs.map(s => {
          const isBuy = s.type === 'buy';
          // Find candles after signal to check if TP or SL was hit first
          const startIdx = candles.findIndex(c => c.t >= s.time);
          let outcome = 'open';
          let pnl = 0;

          for (let i = startIdx + 1; i < candles.length; i++) {
            const c = candles[i];
            if (isBuy) {
              if (c.l <= s.sl) { outcome = 'sl'; pnl = -(s.price - s.sl) * mssLots * lotSize; break; }
              if (c.h >= s.tp1) { outcome = 'tp'; pnl = (s.tp1 - s.price) * mssLots * lotSize; break; }
            } else {
              if (c.h >= s.sl) { outcome = 'sl'; pnl = -(s.sl - s.price) * mssLots * lotSize; break; }
              if (c.l <= s.tp1) { outcome = 'tp'; pnl = (s.price - s.tp1) * mssLots * lotSize; break; }
            }
          }

          pnl = parseFloat(pnl.toFixed(2));
          totalPnl += pnl;
          if (outcome === 'tp') wins++;
          else if (outcome === 'sl') losses++;
          return { ...s, pnl, outcome };
        });

        setMssBacktest({ totalPnl: parseFloat(totalPnl.toFixed(2)), wins, losses, total: sigs.length, trades });
      }).catch(() => {})
      .finally(() => setMssLoading(false));
  };

  const deployMss = () => {
    if (!window.confirm(`Deploy Div+MSS strategy on ${mssAsset} (${mssTf}) with ${mssLots} lots?`)) return;
    api.post('/strategy-builder/deploy', {
      name: `Div+MSS ${mssAsset} ${mssTf}`,
      underlying: mssAsset,
      strategy_type: 'div_mss',
      timeframe: mssTf,
      lots: mssLots,
      profile_id: form.profile_id,
      legs: [],
      risk: { sl_pct: 0, target_pct: 0 },
      execution: { lots: mssLots },
    }).then(r => {
      setMssDeployed(true);
      setTimeout(() => setMssDeployed(false), 5000);
    }).catch(e => setError(e.response?.data?.error || 'Deploy failed'));
  };

  useEffect(() => {
    api.get('/profiles').then(r => {
      const p = r.data.profiles || [];
      setProfiles(p);
      if (p.length && !form.profile_id) setForm(f => ({ ...f, profile_id: p[0].id }));
    });
    fetchMssSignals(mssAsset, mssTf);
    return () => { esRef.current?.close(); clearInterval(pollRef.current); };
  }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const start = async () => {
    setError('');
    try {
      const { data } = await api.post('/start', { ...form, asset });
      setSid(data.sid);
      setRunning(true);
      setLogs([]);
      connectStream(data.sid);
      startPoll(data.sid);
    } catch (e) { setError(e.response?.data?.error || 'Failed to start'); }
  };

  const stop = async () => {
    if (!sid) return;
    await api.post('/stop', { sid });
    setRunning(false);
    esRef.current?.close();
    clearInterval(pollRef.current);
  };

  const connectStream = id => {
    esRef.current?.close();
    const token = localStorage.getItem('token');
    const es = new EventSource(`/api/stream/${id}?token=${token}`);
    es.onmessage = e => {
      setLogs(l => [...l, e.data]);
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    };
    esRef.current = es;
  };

  const startPoll = id => {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await api.get(`/status/${id}`);
        setStatus(data);
        if (!data.running) { setRunning(false); clearInterval(pollRef.current); esRef.current?.close(); }
      } catch {}
    }, 3000);
  };

  const s = status || {};
  const call = s.call || {};
  const put = s.put || {};

  return (
    <div className="container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div className="page-title" style={{ marginBottom: 0 }}>Delta Neutral — {asset}</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <a href="/strategy/new?asset=BTC" className="btn btn-outline">₿ New BTC Strategy</a>
          <a href="/strategy/new?asset=ETH" className="btn btn-outline">⟠ New ETH Strategy</a>
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      {/* Div+MSS Strategy Signals */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <div style={{ fontWeight: 700, fontSize: '.95rem' }}>📊 Div+MSS Strategy</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <select value={mssAsset} onChange={e => { setMssAsset(e.target.value); fetchMssSignals(e.target.value, mssTf); }} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }}>
              <option value="BTC">₿ BTC</option>
              <option value="ETH">⟠ ETH</option>
            </select>
            <select value={mssTf} onChange={e => { setMssTf(e.target.value); fetchMssSignals(mssAsset, e.target.value); }} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }}>
              <option value="15m">15m</option>
              <option value="1h">1H</option>
              <option value="4h">4H</option>
              <option value="1d">1D</option>
            </select>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <label style={{ fontSize: '.7rem', color: 'var(--muted)' }}>Lots</label>
              <input type="number" min={1} value={mssLots} onChange={e => setMssLots(Math.max(1, +e.target.value))} style={{ width: 70, padding: '6px 8px', textAlign: 'center', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg)' }} />
            </div>
            <span style={{ fontSize: '.68rem', color: 'var(--muted)' }}>1 lot = {mssAsset === 'BTC' ? '0.001 BTC' : '0.01 ETH'}</span>
          </div>
        </div>

        {/* Backtest + Deploy buttons */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
          <button className="btn" style={{ background: '#f59e0b', color: '#fff' }} onClick={runBacktest} disabled={mssLoading}>
            {mssLoading ? '⏳ Testing...' : '📝 Backtest'}
          </button>
          <button className="btn btn-green" onClick={deployMss} disabled={mssLoading || mssDeployed}>
            {mssDeployed ? '✅ Deployed!' : '🚀 Deploy Live'}
          </button>
          <select value={form.profile_id} onChange={e => setForm(f => ({ ...f, profile_id: e.target.value }))} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }}>
            <option value="">Default Keys</option>
            {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>

        {/* Backtest Results */}
        {mssBacktest && (
          <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 14, marginBottom: 12 }}>
            <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 10 }}>📈 Backtest Results — {mssAsset} {mssTf.toUpperCase()} · {mssLots} lots</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 10, marginBottom: 10 }}>
              {[
                ['Total', mssBacktest.total, 'var(--text)'],
                ['Wins (TP)', mssBacktest.wins, 'var(--green)'],
                ['Losses (SL)', mssBacktest.losses, 'var(--red)'],
                ['Open', mssBacktest.total - mssBacktest.wins - mssBacktest.losses, '#f59e0b'],
                ['Win Rate', (mssBacktest.wins + mssBacktest.losses) > 0 ? `${(mssBacktest.wins / (mssBacktest.wins + mssBacktest.losses) * 100).toFixed(0)}%` : '—', 'var(--text)'],
                ['Total P&L', `$${mssBacktest.totalPnl.toFixed(2)}`, mssBacktest.totalPnl >= 0 ? 'var(--green)' : 'var(--red)'],
              ].map(([k, v, c]) => (
                <div key={k} style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '.65rem', color: 'var(--muted)', textTransform: 'uppercase' }}>{k}</div>
                  <div style={{ fontSize: '1rem', fontWeight: 800, color: c }}>{v}</div>
                </div>
              ))}
            </div>
            {/* Trade list */}
            <details>
              <summary style={{ cursor: 'pointer', fontSize: '.78rem', color: 'var(--muted)' }}>Show all {mssBacktest.total} trades</summary>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.78rem', marginTop: 8 }}>
                <thead><tr>{['Signal', 'Entry', 'SL', 'TP1', 'Result', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
                <tbody>
                  {mssBacktest.trades.map((t, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td style={{ padding: '4px 8px' }}><span className={`badge ${t.type === 'buy' ? 'badge-green' : 'badge-red'}`}>{t.type === 'buy' ? '▲' : '▼'}</span></td>
                      <td style={{ padding: '4px 8px' }}>${t.price.toFixed(2)}</td>
                      <td style={{ padding: '4px 8px', color: 'var(--red)' }}>${t.sl.toFixed(2)}</td>
                      <td style={{ padding: '4px 8px', color: 'var(--green)' }}>${t.tp1.toFixed(2)}</td>
                      <td style={{ padding: '4px 8px' }}><span className={`badge ${t.outcome === 'tp' ? 'badge-green' : t.outcome === 'sl' ? 'badge-red' : 'badge-yellow'}`}>{t.outcome === 'tp' ? '✓ TP' : t.outcome === 'sl' ? '✗ SL' : '⏳ Open'}</span></td>
                      <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </div>
        )}

        {/* Signals table */}

        {mssLoading ? <div style={{ color: 'var(--muted)', padding: 10 }}>Loading signals...</div> : mssSignals.length === 0 ? <div style={{ color: 'var(--muted)', padding: 10 }}>No signals found</div> : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
            <thead><tr>{['Signal', 'Price', 'SL', 'TP1', 'Risk/Reward', 'Size', 'Risk ($)'].map(h => <th key={h} style={{ textAlign: 'left', padding: '8px', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)' }}>{h}</th>)}</tr></thead>
            <tbody>
              {mssSignals.map((s, i) => {
                const lotSize = mssAsset === 'BTC' ? 0.001 : 0.01;
                const risk = Math.abs(s.price - s.sl) * mssLots * lotSize;
                const reward = Math.abs(s.tp1 - s.price) * mssLots * lotSize;
                const rr = risk > 0 ? (reward / risk).toFixed(2) : '—';
                const dt = new Date(s.time * 1000);
                return (
                  <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ padding: 8 }}>
                      <span className={`badge ${s.type === 'buy' ? 'badge-green' : 'badge-red'}`}>{s.type === 'buy' ? '▲ BUY' : '▼ SELL'}</span>
                      <div style={{ fontSize: '.68rem', color: 'var(--muted)', marginTop: 2 }}>{dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })} {dt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}</div>
                    </td>
                    <td style={{ padding: 8, fontWeight: 700 }}>${s.price.toFixed(2)}</td>
                    <td style={{ padding: 8, color: 'var(--red)' }}>${s.sl.toFixed(2)}</td>
                    <td style={{ padding: 8, color: 'var(--green)' }}>${s.tp1.toFixed(2)}</td>
                    <td style={{ padding: 8, fontWeight: 600 }}>1:{rr}</td>
                    <td style={{ padding: 8 }}>{mssLots} lots ({(mssLots * lotSize).toFixed(3)} {mssAsset})</td>
                    <td style={{ padding: 8, color: 'var(--red)', fontWeight: 600 }}>${risk.toFixed(2)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <div className="grid-3">
          <div className="field"><label>Expiry Date</label><input value={form.expiry_date} onChange={e => set('expiry_date', e.target.value)} placeholder="DD-MM-YYYY" /></div>
          <div className="field"><label>Lot Size</label><input type="number" value={form.lot_size} onChange={e => set('lot_size', +e.target.value)} /></div>
          <div className="field"><label>Target Delta</label><input type="number" step="0.01" value={form.target_delta} onChange={e => set('target_delta', +e.target.value)} /></div>
          <div className="field"><label>Delta Tolerance</label><input type="number" step="0.01" value={form.delta_tolerance} onChange={e => set('delta_tolerance', +e.target.value)} /></div>
          <div className="field"><label>Premium Threshold (%)</label><input type="number" value={form.premium_threshold} onChange={e => set('premium_threshold', +e.target.value)} /></div>
          <div className="field"><label>Target P&L ($)</label><input type="number" value={form.target_pnl} onChange={e => set('target_pnl', +e.target.value)} /></div>
          <div className="field"><label>Monitor Interval (s)</label><input type="number" value={form.monitoring_interval} onChange={e => set('monitoring_interval', +e.target.value)} /></div>
          <div className="field"><label>Max Adjustments</label><input type="number" value={form.max_adjustments} onChange={e => set('max_adjustments', +e.target.value)} /></div>
          <div className="field"><label>Profile</label>
            <select value={form.profile_id} onChange={e => set('profile_id', e.target.value)}>
              {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
          <button className="btn btn-green" onClick={start} disabled={running}>▶ Start</button>
          <button className="btn btn-red" onClick={stop} disabled={!running}>■ Stop</button>
          {sid && <span style={{ fontSize: '.8rem', color: 'var(--muted)', alignSelf: 'center' }}>SID: {sid}</span>}
        </div>
      </div>

      {status && (
        <>
          <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)' }}>
            <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (s.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Realized</div><div className="value" style={{ color: (s.realized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.realized_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Unrealized</div><div className="value" style={{ color: (s.unrealized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.unrealized_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Adjustments</div><div className="value">{s.adjustment_count || 0}<span style={{ fontSize: '.7rem', color: 'var(--muted)' }}>/{form.max_adjustments}</span></div></div>
          </div>

          <div className="grid-2" style={{ marginBottom: 16 }}>
            {[['📈 Short Call', call], ['📉 Short Put', put]].map(([label, leg]) => {
              if (!leg || !leg.symbol) return <div className="card" key={label}><div style={{ fontWeight: 700, marginBottom: 10 }}>{label}</div><div style={{ color: 'var(--muted)', fontSize: '.85rem' }}>No position</div></div>;
              const premChg = leg.entry ? ((leg.mark - leg.entry) / leg.entry * 100) : 0;
              const premColor = premChg > 0 ? 'var(--red)' : 'var(--green)'; // premium up = bad for seller
              return (
                <div className="card" key={label}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                    <span style={{ fontWeight: 700 }}>{label}</span>
                    <span style={{ fontSize: '1.1rem', fontWeight: 800, color: (leg.payoff || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(leg.payoff || 0).toFixed(2)}</span>
                  </div>
                  <table style={{ width: '100%', fontSize: '.85rem' }}>
                    <tbody>
                      {[
                        ['Symbol', leg.symbol],
                        ['Strike', leg.strike],
                        ['Entry Price', `$${(leg.entry || 0).toFixed(2)}`],
                        ['Mark Price', <span style={{ fontWeight: 700 }}>${(leg.mark || 0).toFixed(2)}</span>],
                        ['Premium Δ', <span style={{ color: premColor, fontWeight: 600 }}>{premChg >= 0 ? '+' : ''}{premChg.toFixed(2)}%</span>],
                        ['Delta', (leg.delta || 0).toFixed(4)],
                        ['Size', `${leg.size} lots`],
                        ['Payoff', <span style={{ fontWeight: 700, color: (leg.payoff || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(leg.payoff || 0).toFixed(2)}</span>],
                      ].map(([k, v]) => (
                        <tr key={k} style={{ borderBottom: '1px solid var(--border)' }}>
                          <td style={{ padding: '6px 0', color: 'var(--muted)' }}>{k}</td>
                          <td style={{ padding: '6px 0', fontWeight: 600, textAlign: 'right' }}>{v}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {/* Premium threshold bar */}
                  <div style={{ marginTop: 8 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '.7rem', color: 'var(--muted)' }}>
                      <span>Premium Change</span>
                      <span>Threshold: {form.premium_threshold}%</span>
                    </div>
                    <div style={{ height: 6, borderRadius: 3, background: 'var(--border)', marginTop: 4, overflow: 'hidden' }}>
                      <div style={{ height: '100%', borderRadius: 3, width: `${Math.min(100, Math.abs(premChg) / form.premium_threshold * 100)}%`, background: Math.abs(premChg) >= form.premium_threshold ? 'var(--red)' : '#f59e0b', transition: 'width 0.3s' }} />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Adjustment History */}
          {(s.adjustment_history || []).length > 0 && (
            <div className="card" style={{ marginBottom: 16 }}>
              <div style={{ fontWeight: 700, marginBottom: 10 }}>🔄 Adjustment History ({s.adjustment_history.length})</div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
                <thead><tr>{['#', 'Leg', 'Symbol', 'Entry', 'Exit', 'P&L', 'Time'].map(h => <th key={h} style={{ textAlign: 'left', padding: '6px 8px', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)' }}>{h}</th>)}</tr></thead>
                <tbody>
                  {s.adjustment_history.map((a, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td style={{ padding: '6px 8px', fontWeight: 700 }}>{a.adjustment}</td>
                      <td style={{ padding: '6px 8px' }}><span className={`badge ${a.leg === 'call' ? 'badge-green' : 'badge-red'}`}>{a.leg.toUpperCase()}</span></td>
                      <td style={{ padding: '6px 8px', fontSize: '.8rem' }}>{a.symbol}</td>
                      <td style={{ padding: '6px 8px' }}>${a.entry.toFixed(2)}</td>
                      <td style={{ padding: '6px 8px' }}>${a.exit.toFixed(2)}</td>
                      <td style={{ padding: '6px 8px', fontWeight: 700, color: a.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${a.pnl.toFixed(2)}</td>
                      <td style={{ padding: '6px 8px', fontSize: '.75rem', color: 'var(--muted)' }}>{(a.timestamp || '').replace('T', ' ').slice(11, 19)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div style={{ textAlign: 'right', padding: '8px', fontWeight: 700, fontSize: '.85rem' }}>
                Realized from adjustments: <span style={{ color: s.adjustment_history.reduce((sum, a) => sum + a.pnl, 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${s.adjustment_history.reduce((sum, a) => sum + a.pnl, 0).toFixed(2)}</span>
              </div>
            </div>
          )}
        </>
      )}

      <div className="card">
        <div style={{ fontWeight: 700, marginBottom: 10 }}>Live Logs</div>
        <div ref={logRef} style={{ background: '#0f172a', color: '#e2e8f0', padding: 14, borderRadius: 8, height: 260, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.78rem', whiteSpace: 'pre-wrap' }}>
          {logs.length ? logs.map((l, i) => <div key={i}>{l}</div>) : <span style={{ color: '#64748b' }}>Waiting for logs...</span>}
        </div>
      </div>
    </div>
  );
}
