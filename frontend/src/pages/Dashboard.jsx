import React, { useEffect, useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Filler, ArcElement, Tooltip } from 'chart.js';
import { Line, Doughnut } from 'react-chartjs-2';
import api from '../api';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, ArcElement, Tooltip);

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [strats, setStrats] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [positions, setPositions] = useState([]);
  const [posProfile, setPosProfile] = useState('');
  const nav = useNavigate();

  useEffect(() => {
    api.get('/dashboard').then(r => setData(r.data));
    api.get('/profiles').then(r => setProfiles(r.data.profiles || []));
    loadStrats();
    const t = setInterval(loadStrats, 10000);
    return () => clearInterval(t);
  }, []);

  const loadStrats = () => api.get('/strategies').then(r => {
    const s = r.data.strategies || [];
    s.sort((a, b) => (a.status === 'running' ? -1 : 1) - (b.status === 'running' ? -1 : 1) || (b.started_at || '').localeCompare(a.started_at || ''));
    setStrats(s);
  });

  const closeStrategy = sid => { if (window.confirm('Close this strategy?')) api.post(`/strategies/${sid}/close`).then(loadStrats); };
  const closeAll = () => { if (window.confirm('Close ALL running strategies?')) api.post('/strategies/close-all').then(loadStrats); };

  const loadPositions = () => {
    api.get('/positions', { params: { profile_id: posProfile } }).then(r => {
      if (r.data.error) { setPositions([]); return; }
      setPositions(r.data.positions || []);
    }).catch(() => setPositions([]));
  };
  const closeLeg = (p) => {
    if (!window.confirm(`Close ${p.side.toUpperCase()} ${p.size} lots of ${p.symbol}?`)) return;
    api.post('/close-position', { product_id: p.product_id, symbol: p.symbol, size: p.size, side: p.side, profile_id: posProfile })
      .then(() => loadPositions());
  };

  if (!data) return <div className="container">Loading...</div>;

  const pnlSeries = data.pnl_series || [];
  const alloc = data.asset_allocation || {};
  const allocLabels = Object.keys(alloc);
  const allocValues = Object.values(alloc);
  const colors = ['#6366f1', '#f59e0b', '#ef4444', '#22c55e', '#3b82f6', '#ec4899'];

  return (
    <div className="container">
      <div className="page-title">Hello, {JSON.parse(localStorage.getItem('user'))?.username} 👋</div>

      <div className="top-stats">
        <div className="stat-card"><div className="label">💰 Total P&L</div><div className="value" style={{ color: data.total_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${data.total_pnl.toFixed(2)}</div><div className="sub">All time</div></div>
        <div className="stat-card"><div className="label">📊 Open Positions</div><div className="value">{data.open_positions}</div><div className="sub">Active</div></div>
        <div className="stat-card"><div className="label">📈 Total Trades</div><div className="value">{data.total_trades}</div><div className="sub">Completed</div></div>
        <div className="stat-card"><div className="label">🎯 Win Rate</div><div className="value">{data.win_rate.toFixed(1)}%</div><div className="sub">All closed</div></div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 20, marginBottom: 24 }}>
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 16 }}>Portfolio Performance</div>
          {pnlSeries.length > 0 && (
            <Line data={{ labels: pnlSeries.map(s => s.date), datasets: [{ data: pnlSeries.map(s => s.pnl), borderColor: data.total_pnl >= 0 ? '#22c55e' : '#ef4444', borderWidth: 2, pointRadius: 0, tension: 0.3, fill: { target: 'origin', above: 'rgba(34,197,94,0.08)', below: 'rgba(239,68,68,0.08)' } }] }}
              options={{ responsive: true, animation: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { grid: { color: '#f0f0f0' } } } }} />
          )}
        </div>
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 8 }}>ROI</div>
          <div style={{ fontSize: '1.8rem', fontWeight: 800, color: data.total_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${data.total_pnl.toFixed(2)}</div>
          <div style={{ fontSize: '.75rem', color: 'var(--muted)', marginBottom: 16 }}>All time P&L</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
            <div><div style={{ fontSize: '.7rem', color: 'var(--muted)' }}>Avg Gain</div><div style={{ fontWeight: 700, color: 'var(--green)' }}>${data.avg_gain.toFixed(2)}</div></div>
            <div><div style={{ fontSize: '.7rem', color: 'var(--muted)' }}>Avg Loss</div><div style={{ fontWeight: 700, color: 'var(--red)' }}>${data.avg_loss.toFixed(2)}</div></div>
          </div>
          <div style={{ fontSize: '.75rem', color: 'var(--muted)' }}>Win Rate</div>
          <div style={{ height: 6, borderRadius: 3, background: 'var(--border)', margin: '8px 0', overflow: 'hidden' }}><div style={{ height: '100%', borderRadius: 3, background: 'var(--green)', width: `${data.win_rate}%` }} /></div>
          <div style={{ textAlign: 'right', fontSize: '.85rem', fontWeight: 700 }}>{data.win_rate.toFixed(1)}%</div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24 }}>
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 14 }}>Asset Allocation</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
            {allocLabels.length > 0 && <div style={{ maxWidth: 120 }}><Doughnut data={{ labels: allocLabels, datasets: [{ data: allocValues, backgroundColor: colors.slice(0, allocLabels.length), borderWidth: 0 }] }} options={{ responsive: false, cutout: '65%', plugins: { legend: { display: false } } }} width={120} height={120} /></div>}
            <div>{allocLabels.map((l, i) => <div key={l} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '5px 0', fontSize: '.85rem' }}><span style={{ width: 10, height: 10, borderRadius: '50%', background: colors[i] }} />{l}</div>)}</div>
          </div>
        </div>
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 14 }}>Performance Stats</div>
          {[['Average Gain', data.avg_gain, 'var(--green)'], ['Average Loss', data.avg_loss, 'var(--red)'], ['Biggest Win', data.big_win, 'var(--green)'], ['Biggest Loss', data.big_loss, 'var(--red)'], ['Max Drawdown', data.max_drawdown, 'var(--text)']].map(([k, v, c]) => (
            <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', fontSize: '.85rem', borderBottom: '1px solid var(--border)' }}><span style={{ color: 'var(--muted)' }}>{k}</span><span style={{ fontWeight: 700, color: c }}>${v.toFixed(2)}</span></div>
          ))}
        </div>
      </div>

      {/* Live Positions */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div style={{ fontWeight: 700 }}>📋 Live Positions (Broker)</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <select value={posProfile} onChange={e => setPosProfile(e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.8rem', background: 'var(--card)' }}>
              <option value="">Default Keys</option>
              {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <button className="btn btn-outline" onClick={loadPositions} style={{ padding: '6px 14px', fontSize: '.8rem' }}>🔄 Load</button>
          </div>
        </div>
        {positions.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 10 }}>Click "Load" to fetch positions from broker</div>
        ) : (
          <>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
              <thead><tr>{['Symbol', 'Side', 'Type', 'Strike', 'Size', 'Entry', 'Mark', 'P&L', 'Action'].map(h => <th key={h} style={{ textAlign: 'left', padding: '8px', color: 'var(--muted)', fontSize: '.75rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)' }}>{h}</th>)}</tr></thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ padding: 8, fontWeight: 600, fontSize: '.8rem' }}>{p.symbol}</td>
                    <td style={{ padding: 8 }}><span className={`badge ${p.side === 'buy' ? 'badge-green' : 'badge-red'}`}>{p.side.toUpperCase()}</span></td>
                    <td style={{ padding: 8 }}>{p.type.toUpperCase()}</td>
                    <td style={{ padding: 8, textAlign: 'right' }}>{Number(p.strike).toLocaleString()}</td>
                    <td style={{ padding: 8, textAlign: 'right' }}>{p.size}</td>
                    <td style={{ padding: 8, textAlign: 'right' }}>${p.entry_price.toFixed(2)}</td>
                    <td style={{ padding: 8, textAlign: 'right', fontWeight: 600 }}>${p.mark_price.toFixed(2)}</td>
                    <td style={{ padding: 8, textAlign: 'right', fontWeight: 700, color: p.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${p.pnl.toFixed(2)}</td>
                    <td style={{ padding: 8, textAlign: 'center' }}><button className="btn btn-red" onClick={() => closeLeg(p)} style={{ padding: '4px 12px', fontSize: '.75rem' }}>Close</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ textAlign: 'right', padding: '10px', fontWeight: 800, fontSize: '.9rem' }}>
              Total P&L: <span style={{ color: positions.reduce((s, p) => s + p.pnl, 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${positions.reduce((s, p) => s + p.pnl, 0).toFixed(2)}</span>
            </div>
          </>
        )}
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <div style={{ fontWeight: 700 }}>📋 All Strategies</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn btn-outline" onClick={loadStrats} style={{ padding: '4px 12px', fontSize: '.8rem' }}>🔄 Refresh</button>
            <button className="btn btn-red" onClick={closeAll} style={{ padding: '4px 12px', fontSize: '.8rem' }}>✕ Close All</button>
          </div>
        </div>
        {strats.length === 0 && <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 10 }}>No strategies yet</div>}
        {strats.map(s => {
          const isRunning = s.status === 'running' || s.status === 'open (no monitor)';
          return (
            <div key={s.sid} className="strat-row" onClick={() => nav(`/strategy/${s.sid}`)}>
              <div>
                <span className={`source-badge ${s.source === 'AlgoX DN' ? 'source-dn' : 'source-oc'}`}>{s.source}</span>
                <div style={{ fontWeight: 700, marginTop: 4 }}>{s.name}</div>
                <div style={{ fontSize: '.75rem', color: 'var(--muted)' }}>ID: {s.sid} · {(s.started_at || '').replace('T', ' ').slice(0, 16)}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontWeight: 700, fontSize: '1rem', color: (s.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.pnl || 0).toFixed(2)}</div>
                <div style={{ fontSize: '.75rem' }}>{isRunning ? <span className="badge badge-green">● {s.status}</span> : <span className="badge" style={{ background: '#f0f0f0', color: 'var(--muted)' }}>{s.status}</span>}</div>
                {isRunning && <button className="btn btn-red" onClick={e => { e.stopPropagation(); closeStrategy(s.sid); }} style={{ padding: '4px 12px', fontSize: '.75rem', marginTop: 4 }}>✕ Close</button>}
                <button className="btn btn-outline" onClick={e => { e.stopPropagation(); nav(`/strategy/${s.sid}/logs`); }} style={{ padding: '4px 12px', fontSize: '.75rem', marginTop: 4 }}>📋 Logs</button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="card">
        <div style={{ fontWeight: 700, marginBottom: 16 }}>Trade History</div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
          <thead><tr>{['ID', 'Source', 'Status', 'Asset', 'Expiry', 'Lots', 'Details', 'P&L', 'Started'].map(h => <th key={h} style={{ textAlign: 'left', padding: '8px 10px', color: 'var(--muted)', fontSize: '.75rem', borderBottom: '2px solid var(--border)' }}>{h}</th>)}</tr></thead>
          <tbody>
            {(data.recent_trades || []).map(t => {
              const p = t.params || {};
              return (
              <tr key={t.sid + t.started_at} onClick={() => nav(`/strategy/${t.sid}`)} style={{ cursor: 'pointer' }}>
                <td style={{ padding: '10px', fontWeight: 600, borderBottom: '1px solid var(--border)' }}>{t.sid}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}><span className="badge" style={{ background: '#ede9fe', color: '#6366f1', fontSize: '.7rem' }}>{p.source || 'DN'}</span></td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}><span className={t.status === 'running' ? 'badge badge-yellow' : (t.pnl || 0) >= 0 ? 'badge badge-green' : 'badge badge-red'}>{t.status === 'running' ? 'Running' : (t.pnl || 0) >= 0 ? 'Profit' : 'Loss'}</span></td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)', fontWeight: 600 }}>{p.asset || 'BTC'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{p.expiry_date || '—'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{p.lot_size || p.legs || '—'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)', fontSize: '.75rem', color: 'var(--muted)' }}>{p.leg_details || (p.target_delta ? `Δ${p.target_delta}` : p.name || '—')}</td>
                <td style={{ padding: '10px', fontWeight: 700, color: (t.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)', borderBottom: '1px solid var(--border)' }}>${(t.pnl || 0).toFixed(2)}</td>
                <td style={{ padding: '10px', fontSize: '.75rem', color: 'var(--muted)', borderBottom: '1px solid var(--border)' }}>{(t.started_at || '').replace('T', ' ').slice(0, 16)}</td>
              </tr>
            );})}
          </tbody>
        </table>
      </div>
    </div>
  );
}
