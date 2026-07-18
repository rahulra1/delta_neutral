import React, { useEffect, useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Filler, ArcElement, Tooltip } from 'chart.js';
import { Line, Doughnut } from 'react-chartjs-2';
import api from '../api';
import { PositionTable } from '../components/PositionCard';
import PositionCard from '../components/PositionCard';
import PayoffChart from '../components/PayoffChart';
import { StrategyGrid } from '../components/StrategyCard';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, ArcElement, Tooltip);

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [strats, setStrats] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [positions, setPositions] = useState([]);
  const [posProfile, setPosProfile] = useState('');
  const [selectedPos, setSelectedPos] = useState(new Set());
  const [chartRange, setChartRange] = useState('7d');
  const [chartFrom, setChartFrom] = useState('');
  const [chartTo, setChartTo] = useState('');
  const [pnlSeries, setPnlSeries] = useState([]);
  const [livePnl, setLivePnl] = useState({ total_live_pnl: 0, active_count: 0, strategies: [] });
  const [livePnlHistory, setLivePnlHistory] = useState([]);
  const nav = useNavigate();

  useEffect(() => {
    api.get('/dashboard').then(r => setData(r.data));
    api.get('/profiles').then(r => setProfiles(r.data.profiles || []));
    loadStrats();
    const t = setInterval(() => {
      loadStrats();
      api.get('/dashboard').then(r => setData(r.data));
    }, 10000);
    return () => clearInterval(t);
  }, []);

  // Live P&L SSE stream
  useEffect(() => {
    const token = localStorage.getItem('token');
    const es = new EventSource(`/api/dashboard/live-pnl?token=${token}`);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        setLivePnl(d);
        setLivePnlHistory(prev => {
          const updated = [...prev, { time: new Date(d.timestamp).toLocaleTimeString(), pnl: d.total_live_pnl }];
          return updated.slice(-60);
        });
      } catch {}
    };
    es.onerror = () => {};
    return () => es.close();
  }, []);

  useEffect(() => {
    const params = {};
    if (chartRange === 'custom') {
      if (chartFrom) params.since = chartFrom;
      if (chartTo) params.until = chartTo;
    } else if (chartRange !== 'all') {
      const days = { '7d': 7, '30d': 30, '90d': 90 }[chartRange] || 0;
      if (days) params.since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    }
    const fetchPnl = () => api.get('/pnl-series', { params }).then(r => setPnlSeries(r.data.pnl_series || []));
    fetchPnl();
    const t = setInterval(fetchPnl, 5000);
    return () => clearInterval(t);
  }, [chartRange, chartFrom, chartTo]);

  const loadStrats = () => api.get('/strategies').then(r => {
    const s = r.data.strategies || [];
    s.sort((a, b) => (a.status === 'running' ? -1 : 1) - (b.status === 'running' ? -1 : 1) || (b.started_at || '').localeCompare(a.started_at || ''));
    setStrats(s);
  });

  const closeStrategy = sid => { if (window.confirm('Close this strategy?')) api.post(`/strategies/${sid}/close`).then(loadStrats); };
  const closeAll = () => { if (window.confirm('Close ALL running strategies?')) api.post('/strategies/close-all').then(loadStrats); };

  const loadPositions = () => {
    api.get('/tracked-positions', { params: { profile_id: posProfile } }).then(r => {
      if (r.data.error) { setPositions([]); return; }
      setPositions(r.data.positions || []);
      setSelectedPos(new Set());
    }).catch(() => setPositions([]));
  };
  const closeLeg = (p) => {
    if (!window.confirm(`Close ${p.side.toUpperCase()} ${p.size} lots of ${p.symbol}?`)) return;
    api.post('/close-position', { product_id: p.product_id, symbol: p.symbol, size: p.size, side: p.side, profile_id: posProfile })
      .then(() => loadPositions());
  };

  if (!data) return <div className="container">Loading...</div>;

  const RANGE_BTNS = [
    { key: '7d', label: '7D' }, { key: '30d', label: '1M' },
    { key: '90d', label: '3M' }, { key: 'all', label: 'All' },
    { key: 'custom', label: '📅 Custom' },
  ];
  const alloc = data.asset_allocation || {};
  const allocLabels = Object.keys(alloc);
  const allocValues = Object.values(alloc);
  const colors = ['#6366f1', '#f59e0b', '#ef4444', '#22c55e', '#3b82f6', '#ec4899'];

  return (
    <div className="container">
      <div className="page-title">Hello, {JSON.parse(localStorage.getItem('user'))?.username} 👋</div>

      <div className="top-stats">
        <div className="stat-card"><div className="label">💰 Total P&L</div><div className="value" style={{ color: (data.total_pnl + livePnl.total_live_pnl) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(data.total_pnl + livePnl.total_live_pnl).toFixed(2)}</div><div className="sub">All time (live)</div></div>
        <div className="stat-card"><div className="label">📊 Open Positions</div><div className="value">{data.open_positions}</div><div className="sub">Active</div></div>
        <div className="stat-card"><div className="label">📈 Total Trades</div><div className="value">{data.total_trades}</div><div className="sub">Completed</div></div>
        <div className="stat-card"><div className="label">🎯 Win Rate</div><div className="value">{data.win_rate.toFixed(1)}%</div><div className="sub">All closed</div></div>
      </div>

      {/* Live P&L Ticker */}
      {livePnl.active_count > 0 && (
        <div className="card" style={{ marginBottom: 20, background: 'linear-gradient(135deg, var(--card) 0%, var(--bg) 100%)', border: '1.5px solid var(--accent)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#22c55e', animation: 'pulse 2s infinite' }} />
              <span style={{ fontWeight: 700, fontSize: '.9rem' }}>Live P&L</span>
              <span style={{ fontSize: '.75rem', color: 'var(--muted)' }}>({livePnl.active_count} active)</span>
            </div>
            <div style={{ fontSize: '1.4rem', fontWeight: 800, color: livePnl.total_live_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
              ${livePnl.total_live_pnl.toFixed(4)}
            </div>
          </div>

          {/* Per-strategy breakdown */}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {livePnl.strategies.map((s, i) => (
              <div key={i} style={{ padding: '6px 12px', borderRadius: 6, background: 'var(--bg)', border: '1px solid var(--border)', fontSize: '.78rem' }}>
                <span style={{ color: 'var(--muted)', marginRight: 6 }}>{s.name}</span>
                <span style={{ fontWeight: 700, color: s.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${s.pnl.toFixed(4)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 300px', gap: 20, marginBottom: 24 }}>
        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', gap: 8 }}>
            <div style={{ fontWeight: 700 }}>Portfolio Performance</div>
            <div style={{ display: 'flex', gap: 4, alignItems: 'center', flexWrap: 'wrap' }}>
              {RANGE_BTNS.map(r => (
                <button key={r.key} onClick={() => setChartRange(r.key)} style={{
                  padding: '5px 12px', border: chartRange === r.key ? '1.5px solid var(--accent)' : '1.5px solid var(--border)', borderRadius: 6,
                  background: chartRange === r.key ? 'var(--accent)' : 'var(--bg)',
                  color: chartRange === r.key ? '#fff' : 'var(--text)',
                  fontSize: '.78rem', fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap',
                }}>{r.label}</button>
              ))}
            </div>
          </div>
          {chartRange === 'custom' && (
            <div style={{ display: 'flex', gap: 10, marginBottom: 12, alignItems: 'center', fontSize: '.82rem', flexWrap: 'wrap' }}>
              <label style={{ color: 'var(--muted)', fontWeight: 600 }}>From</label>
              <input type="date" value={chartFrom} onChange={e => setChartFrom(e.target.value)}
                style={{ padding: '6px 10px', border: '1.5px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)', color: 'var(--text)' }} />
              <label style={{ color: 'var(--muted)', fontWeight: 600 }}>To</label>
              <input type="date" value={chartTo} onChange={e => setChartTo(e.target.value)}
                style={{ padding: '6px 10px', border: '1.5px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)', color: 'var(--text)' }} />
            </div>
          )}
          {pnlSeries.length > 0 || livePnlHistory.length > 2 ? (
            <Line data={{
              labels: [
                ...pnlSeries.map(s => s.date),
                ...livePnlHistory.map(p => p.time),
              ],
              datasets: [{
                label: 'Total P&L',
                data: [
                  ...pnlSeries.map(s => s.pnl),
                  ...livePnlHistory.map(p => {
                    // Total = last historical cumulative + current live pnl
                    const basePnl = pnlSeries.length > 0 ? pnlSeries[pnlSeries.length - 1].pnl : 0;
                    return basePnl + p.pnl - (livePnlHistory.length > 0 ? 0 : 0);
                  }),
                ],
                borderColor: (data.total_pnl + livePnl.total_live_pnl) >= 0 ? '#22c55e' : '#ef4444',
                borderWidth: 2,
                pointRadius: (pnlSeries.length + livePnlHistory.length) < 30 ? 3 : 0,
                tension: 0.3,
                fill: { target: 'origin', above: 'rgba(34,197,94,0.08)', below: 'rgba(239,68,68,0.08)' },
              }]
            }}
              options={{ responsive: true, animation: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `P&L: $${ctx.parsed.y.toFixed(2)}` } } }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8, font: { size: 10 }, color: '#aaa' } }, y: { grid: { color: '#f0f0f0' }, ticks: { font: { size: 10 }, color: '#aaa', callback: v => '$' + v } } } }} />
          ) : (
            <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 20, textAlign: 'center' }}>No data for this range</div>
          )}
        </div>
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 8 }}>ROI</div>
          <div style={{ fontSize: '1.8rem', fontWeight: 800, color: (data.total_pnl + livePnl.total_live_pnl) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(data.total_pnl + livePnl.total_live_pnl).toFixed(2)}</div>
          <div style={{ fontSize: '.75rem', color: 'var(--muted)', marginBottom: 16 }}>All time P&L (incl. live)</div>
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
            {positions.length > 0 && (
              <button className="btn btn-outline" onClick={() => setSelectedPos(prev => prev.size === positions.length ? new Set() : new Set(positions.map((_, i) => i)))} style={{ padding: '6px 14px', fontSize: '.8rem' }}>
                {selectedPos.size === positions.length ? 'Deselect All' : 'Select All'}
              </button>
            )}
          </div>
        </div>
        {positions.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 10 }}>Click "Load" to fetch positions from broker</div>
        ) : (
          <>
            {/* Position Cards */}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
              {positions.map((p, i) => (
                <div key={i} style={{ position: 'relative', cursor: 'pointer' }} onClick={() => setSelectedPos(prev => { const n = new Set(prev); n.has(i) ? n.delete(i) : n.add(i); return n; })}>
                  <div style={{ position: 'absolute', top: 8, left: 8, zIndex: 1 }}>
                    <input type="checkbox" checked={selectedPos.has(i)} readOnly style={{ accentColor: '#6366f1' }} />
                  </div>
                  <div style={{ paddingLeft: 8, border: selectedPos.has(i) ? '2px solid #6366f1' : '2px solid transparent', borderRadius: 10, transition: 'border-color .15s' }}>
                    <PositionCard position={p} sym="$" onClose={() => closeLeg(p)} />
                  </div>
                </div>
              ))}
            </div>

            {/* Total P&L */}
            <div style={{ textAlign: 'right', padding: '8px', fontWeight: 800, fontSize: '.9rem', borderTop: '1px solid var(--border)' }}>
              Total P&L: <span style={{ color: positions.reduce((s, p) => s + (p.pnl || p.current_pnl || 0), 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                ${positions.reduce((s, p) => s + (p.pnl || p.current_pnl || 0), 0).toFixed(2)}
              </span>
            </div>

            {/* Payoff Chart for selected positions */}
            {selectedPos.size > 0 && (
              <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                <div style={{ fontSize: '.85rem', fontWeight: 700, marginBottom: 8 }}>📈 Payoff at Expiry ({selectedPos.size} leg{selectedPos.size > 1 ? 's' : ''} selected)</div>
                <PayoffChart
                  legs={[...selectedPos].map(i => {
                    const p = positions[i];
                    return {
                      side: p.side, type: p.type,
                      strike: parseFloat(p.strike),
                      mark: p.entry_price || p.mark_price || 0,
                      size: p.size, iv: 0.5,
                    };
                  })}
                  lotSize={positions[0]?.asset === 'ETH' ? 0.01 : 0.001}
                  spot={0}
                  sym="$"
                  daysToExpiry={30}
                />
              </div>
            )}
          </>
        )}
      </div>

      <StrategyGrid
        strategies={strats} title="📋 All Strategies"
        onSelect={s => nav(`/strategy/${s.sid}`)}
        onClose={sid => { if (window.confirm('Close this strategy?')) api.post(`/strategies/${sid}/close`).then(loadStrats); }}
        onLogs={sid => nav(`/strategy/${sid}/logs`)}
        onRefresh={loadStrats}
        onCloseAll={closeAll}
      />
    </div>
  );
}
