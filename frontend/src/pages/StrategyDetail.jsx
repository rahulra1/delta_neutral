import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../api';

export default function StrategyDetail() {
  const { sid } = useParams();
  const nav = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const pollRef = useRef(null);

  const load = () => {
    api.get(`/strategy-detail/${sid}`)
      .then(r => { setData(r.data); setError(''); })
      .catch(() => {
        // Fallback: try tracker endpoint
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

  const close = async () => {
    if (!window.confirm('Close this strategy?')) return;
    try {
      await api.post(`/strategies/${sid}/close`);
      load();
    } catch (e) { setError(e.response?.data?.error || 'Failed to close'); }
  };

  if (!data) return <div className="container">Loading...</div>;

  const d = data;
  const legs = d.legs || [];
  const config = d.details || {};
  const logs = d.logs || [];
  const isRunning = d.status === 'running';

  return (
    <div className="container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div>
          <div className="page-title" style={{ marginBottom: 4 }}>{d.name || `Strategy ${sid}`}</div>
          <div style={{ fontSize: '.85rem', color: 'var(--muted)' }}>
            {d.source && <span className="badge" style={{ marginRight: 8 }}>{d.source}</span>}
            <span className={`badge ${isRunning ? 'badge-green' : 'badge-red'}`}>{d.status}</span>
            {d.started_at && <span style={{ marginLeft: 10 }}>Started: {d.started_at.replace('T', ' ').slice(0, 19)}</span>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {isRunning && <button className="btn btn-red" onClick={close}>✕ Close</button>}
          <button className="btn btn-outline" onClick={() => nav(`/strategy/${sid}/logs`)}>📋 Logs</button>
          <button className="btn btn-outline" onClick={() => nav(-1)}>← Back</button>
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      <div className="top-stats" style={{ gridTemplateColumns: d.realized_pnl !== undefined ? 'repeat(4,1fr)' : 'repeat(2,1fr)' }}>
        <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: (d.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(d.pnl || 0).toFixed(2)}</div></div>
        {d.realized_pnl !== undefined && <div className="stat-card"><div className="label">Realized</div><div className="value" style={{ color: (d.realized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(d.realized_pnl || 0).toFixed(2)}</div></div>}
        {d.unrealized_pnl !== undefined && <div className="stat-card"><div className="label">Unrealized</div><div className="value" style={{ color: (d.unrealized_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(d.unrealized_pnl || 0).toFixed(2)}</div></div>}
        {d.adjustment_count !== undefined ? <div className="stat-card"><div className="label">Adjustments</div><div className="value">{d.adjustment_count}</div></div> : <div className="stat-card"><div className="label">Status</div><div className="value">{d.status}</div></div>}
      </div>

      {legs.length > 0 && (
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 12 }}>Legs</div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.85rem' }}>
            <thead><tr>{['Side', 'Type', 'Strike', 'Symbol', 'Size', 'Entry', 'Mark', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '8px', color: 'var(--muted)', borderBottom: '2px solid var(--border)', fontSize: '.75rem' }}>{h}</th>)}</tr></thead>
            <tbody>
              {legs.map((l, i) => {
                const entry = l.entry_price || l.entry || 0;
                const mark = l.current_mark || l.mark || entry;
                const pnl = l.current_pnl || l.payoff || 0;
                const chg = entry ? ((mark - entry) / entry * 100) : 0;
                return (
                  <tr key={i}>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}><span className={`badge ${(l.side||'').toLowerCase() === 'sell' ? 'badge-red' : 'badge-green'}`}>{(l.side||'').toUpperCase()}</span></td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{(l.type||'').toUpperCase()}</td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{l.strike}</td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)', fontWeight: 600, fontSize: '.8rem' }}>{l.symbol}</td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{l.size}</td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>${entry.toFixed(2)}</td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)', fontWeight: 700 }}>${mark.toFixed(2)} <span style={{ fontSize: '.7rem', color: chg >= 0 ? 'var(--red)' : 'var(--green)', fontWeight: 600 }}>({chg >= 0 ? '+' : ''}{chg.toFixed(2)}%)</span></td>
                    <td style={{ padding: 8, borderBottom: '1px solid var(--border)', fontWeight: 700, color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${pnl.toFixed(2)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {legs.some(l => (l.current_pnl || l.payoff) !== undefined) && (
            <div style={{ textAlign: 'right', padding: '10px 8px', fontWeight: 800, fontSize: '.9rem' }}>
              Total: <span style={{ color: legs.reduce((s, l) => s + (l.current_pnl || l.payoff || 0), 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${legs.reduce((s, l) => s + (l.current_pnl || l.payoff || 0), 0).toFixed(2)}</span>
            </div>
          )}
        </div>
      )}

      {/* Adjustment History */}
      {(d.adjustment_history || []).length > 0 && (
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 12 }}>🔄 Closed Legs (Adjustments)</div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.85rem' }}>
            <thead><tr>{['#', 'Leg', 'Symbol', 'Strike', 'Size', 'Entry', 'Exit', 'P&L', 'Time'].map(h => <th key={h} style={{ textAlign: 'left', padding: '6px 8px', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)' }}>{h}</th>)}</tr></thead>
            <tbody>
              {d.adjustment_history.map((a, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '6px 8px', fontWeight: 700 }}>{a.adjustment}</td>
                  <td style={{ padding: '6px 8px' }}><span className={`badge ${a.leg === 'call' ? 'badge-green' : 'badge-red'}`}>{a.leg.toUpperCase()}</span></td>
                  <td style={{ padding: '6px 8px', fontSize: '.8rem' }}>{a.symbol}</td>
                  <td style={{ padding: '6px 8px' }}>{a.strike}</td>
                  <td style={{ padding: '6px 8px' }}>{a.size}</td>
                  <td style={{ padding: '6px 8px' }}>${a.entry.toFixed(2)}</td>
                  <td style={{ padding: '6px 8px' }}>${a.exit.toFixed(2)}</td>
                  <td style={{ padding: '6px 8px', fontWeight: 700, color: a.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${a.pnl.toFixed(2)}</td>
                  <td style={{ padding: '6px 8px', fontSize: '.75rem', color: 'var(--muted)' }}>{(a.timestamp || '').replace('T', ' ').slice(11, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {Object.keys(config).length > 0 && (
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 12 }}>Configuration</div>
          <div className="grid-2">
            {Object.entries(config).map(([k, v]) => (
              <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border)', fontSize: '.85rem' }}>
                <span style={{ color: 'var(--muted)' }}>{k}</span>
                <span style={{ fontWeight: 600 }}>{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {logs.length > 0 && (
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 10 }}>Logs</div>
          <div style={{ background: '#0f172a', color: '#e2e8f0', padding: 14, borderRadius: 8, maxHeight: 300, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.78rem', whiteSpace: 'pre-wrap' }}>
            {logs.map((l, i) => <div key={i}>{l}</div>)}
          </div>
        </div>
      )}
    </div>
  );
}
