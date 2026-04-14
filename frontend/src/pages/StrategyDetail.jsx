import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../api';
import { PositionTable } from '../components/PositionCard';

export default function StrategyDetail() {
  const { sid } = useParams();
  const nav = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState('overview');
  const pollRef = useRef(null);
  const logRef = useRef(null);

  const load = () => {
    api.get(`/strategy-detail/${sid}`)
      .then(r => { setData(r.data); setError(''); })
      .catch(() => {
        api.get(`/tracker/${sid}`)
          .then(r => { setData(r.data); setError(''); })
          .catch(e => setError(e.response?.data?.error || 'Strategy not found'));
      });
  };

  useEffect(() => {
    load();
    pollRef.current = setInterval(load, 5000);
    return () => clearInterval(pollRef.current);
  }, [sid]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [data?.logs]);

  const close = () => {
    if (!window.confirm('Close this strategy and all positions?')) return;
    api.post(`/strategies/${sid}/close`).then(load).catch(() =>
      api.post(`/tracker/${sid}/close`).then(load)
    );
  };

  if (error) return <div className="container"><div className="error-msg">{error}</div><button className="btn btn-outline" onClick={() => nav(-1)}>← Back</button></div>;
  if (!data) return <div className="container" style={{ padding: 40, color: 'var(--muted)' }}>Loading...</div>;

  const d = data;
  const legs = d.legs || [];
  const logs = d.logs || [];
  const adjHistory = d.adjustment_history || [];
  const config = d.details || d.params || {};
  const isRunning = d.running || d.status === 'running' || d.status === 'open (no monitor)';
  const pnl = d.pnl || 0;

  const TABS = [
    { key: 'overview', label: '📊 Overview' },
    { key: 'legs', label: `📋 Legs (${legs.length})` },
    { key: 'logs', label: `📝 Logs (${logs.length})` },
    ...(adjHistory.length ? [{ key: 'adjustments', label: `🔄 Adjustments (${adjHistory.length})` }] : []),
    ...(Object.keys(config).length ? [{ key: 'config', label: '⚙️ Config' }] : []),
  ];

  return (
    <div className="container" style={{ maxWidth: 1000 }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20 }}>
        <div>
          <button className="btn btn-outline" onClick={() => nav(-1)} style={{ padding: '4px 12px', fontSize: '.78rem', marginBottom: 8 }}>← Back</button>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4 }}>
            <span style={{ fontSize: '1.2rem', fontWeight: 800 }}>{d.name || `Strategy ${sid}`}</span>
            {d.source && <span style={{ fontSize: '.68rem', padding: '2px 10px', borderRadius: 4, fontWeight: 700, background: '#ede9fe', color: '#6366f1' }}>{d.source}</span>}
            <span className={`badge ${isRunning ? 'badge-green' : d.status === 'completed' ? 'badge-yellow' : 'badge-red'}`} style={{ fontSize: '.72rem' }}>
              {isRunning ? '● Live' : d.status}
            </span>
          </div>
          <div style={{ fontSize: '.78rem', color: 'var(--muted)' }}>
            ID: {sid} · Started: {(d.started_at || '').replace('T', ' ').slice(0, 19)}
            {d.ended_at && ` · Ended: ${d.ended_at.replace('T', ' ').slice(0, 19)}`}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: '1.6rem', fontWeight: 800, color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${pnl.toFixed(2)}</div>
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            {isRunning && <button className="btn btn-red" onClick={close} style={{ padding: '6px 16px' }}>✕ Close</button>}
            <button className="btn btn-outline" onClick={() => nav(`/strategy/${sid}/logs`)} style={{ padding: '6px 16px' }}>📋 Full Logs</button>
          </div>
        </div>
      </div>

      {/* Stats */}
      <div className="top-stats" style={{ gridTemplateColumns: d.realized_pnl !== undefined ? 'repeat(4,1fr)' : 'repeat(3,1fr)' }}>
        <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${pnl.toFixed(2)}</div></div>
        {d.realized_pnl !== undefined && <div className="stat-card"><div className="label">Realized</div><div className="value" style={{ color: (d.realized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(d.realized_pnl || 0).toFixed(2)}</div></div>}
        {d.unrealized_pnl !== undefined && <div className="stat-card"><div className="label">Unrealized</div><div className="value" style={{ color: (d.unrealized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(d.unrealized_pnl || 0).toFixed(2)}</div></div>}
        <div className="stat-card"><div className="label">{d.adjustment_count !== undefined ? 'Adjustments' : 'Status'}</div><div className="value">{d.adjustment_count !== undefined ? d.adjustment_count : d.status}</div></div>
      </div>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 16, borderBottom: '2px solid var(--border)', paddingBottom: 0 }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)} style={{
            padding: '8px 16px', border: 'none', borderBottom: tab === t.key ? '2px solid var(--accent)' : '2px solid transparent',
            background: 'none', color: tab === t.key ? 'var(--text)' : 'var(--muted)',
            fontWeight: tab === t.key ? 700 : 500, fontSize: '.84rem', cursor: 'pointer', fontFamily: 'inherit', marginBottom: -2,
          }}>{t.label}</button>
        ))}
      </div>

      {/* Tab Content */}
      {tab === 'overview' && (
        <>
          {legs.length > 0 && (
            <div className="card"><div style={{ fontWeight: 700, marginBottom: 12 }}>Active Legs</div><PositionTable positions={legs} sym="$" /></div>
          )}
          {logs.length > 0 && (
            <div className="card">
              <div style={{ fontWeight: 700, marginBottom: 8 }}>Recent Logs</div>
              <div ref={logRef} style={{ background: '#0f172a', color: '#e2e8f0', padding: 12, borderRadius: 8, maxHeight: 200, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.76rem', whiteSpace: 'pre-wrap' }}>
                {logs.slice(-20).map((l, i) => <div key={i} style={{ color: l.includes('✗') || l.includes('🛑') ? '#fca5a5' : l.includes('✓') || l.includes('🎯') ? '#6ee7b7' : l.includes('📊') ? '#fcd34d' : '#e2e8f0' }}>{l}</div>)}
              </div>
            </div>
          )}
          {adjHistory.length > 0 && (
            <div className="card">
              <div style={{ fontWeight: 700, marginBottom: 8 }}>Last Adjustment</div>
              {(() => { const a = adjHistory[adjHistory.length - 1]; return (
                <div style={{ display: 'flex', gap: 16, fontSize: '.85rem', flexWrap: 'wrap' }}>
                  <span>#{a.adjustment}</span>
                  <span><span className={`badge ${a.leg === 'call' ? 'badge-green' : 'badge-red'}`}>{a.leg.toUpperCase()}</span></span>
                  <span>{a.symbol}</span>
                  <span>${a.entry.toFixed(2)} → ${a.exit.toFixed(2)}</span>
                  <span style={{ fontWeight: 700, color: a.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${a.pnl.toFixed(2)}</span>
                </div>
              );})()}
            </div>
          )}
        </>
      )}

      {tab === 'legs' && (
        <div className="card">
          {legs.length > 0 ? <PositionTable positions={legs} sym="$" /> : <div style={{ color: 'var(--muted)', padding: 16 }}>No legs data available</div>}
        </div>
      )}

      {tab === 'logs' && (
        <div className="card">
          <div ref={logRef} style={{ background: '#0f172a', color: '#e2e8f0', padding: 14, borderRadius: 8, height: 'calc(100vh - 380px)', minHeight: 300, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.76rem', whiteSpace: 'pre-wrap' }}>
            {logs.length ? logs.map((l, i) => (
              <div key={i} style={{ padding: '1px 0', color: l.includes('✗') || l.includes('🛑') || l.includes('⚠') ? '#fca5a5' : l.includes('✓') || l.includes('✅') || l.includes('🎯') ? '#6ee7b7' : l.includes('📊') ? '#fcd34d' : '#e2e8f0' }}>{l}</div>
            )) : <div style={{ color: '#64748b' }}>No logs available</div>}
          </div>
        </div>
      )}

      {tab === 'adjustments' && (
        <div className="card">
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.85rem' }}>
            <thead><tr>{['#', 'Leg', 'Symbol', 'Strike', 'Size', 'Entry', 'Exit', 'P&L', 'Time'].map(h =>
              <th key={h} style={{ textAlign: 'left', padding: '8px', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)' }}>{h}</th>
            )}</tr></thead>
            <tbody>
              {adjHistory.map((a, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: 8, fontWeight: 700 }}>{a.adjustment}</td>
                  <td style={{ padding: 8 }}><span className={`badge ${a.leg === 'call' ? 'badge-green' : 'badge-red'}`}>{a.leg.toUpperCase()}</span></td>
                  <td style={{ padding: 8, fontSize: '.8rem' }}>{a.symbol}</td>
                  <td style={{ padding: 8 }}>{a.strike || '—'}</td>
                  <td style={{ padding: 8 }}>{a.size || '—'}</td>
                  <td style={{ padding: 8 }}>${a.entry.toFixed(2)}</td>
                  <td style={{ padding: 8 }}>${a.exit.toFixed(2)}</td>
                  <td style={{ padding: 8, fontWeight: 700, color: a.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${a.pnl.toFixed(2)}</td>
                  <td style={{ padding: 8, fontSize: '.75rem', color: 'var(--muted)' }}>{(a.timestamp || '').replace('T', ' ').slice(11, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ textAlign: 'right', padding: 8, fontWeight: 700, fontSize: '.9rem' }}>
            Total: <span style={{ color: adjHistory.reduce((s, a) => s + a.pnl, 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${adjHistory.reduce((s, a) => s + a.pnl, 0).toFixed(2)}</span>
          </div>
        </div>
      )}

      {tab === 'config' && (
        <div className="card">
          <div className="grid-2">
            {Object.entries(config).filter(([k, v]) => typeof v !== 'object' && k !== 'legs' && k !== 'profile_id').map(([k, v]) => (
              <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: '.85rem' }}>
                <span style={{ color: 'var(--muted)' }}>{k.replace(/_/g, ' ')}</span>
                <span style={{ fontWeight: 600 }}>{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
