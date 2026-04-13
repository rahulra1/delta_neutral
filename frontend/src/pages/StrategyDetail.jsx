import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../api';

export default function StrategyDetail() {
  const { sid } = useParams();
  const nav = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const pollRef = useRef(null);

  const load = () => api.get(`/strategy-detail/${sid}`).then(r => setData(r.data)).catch(() => {});

  useEffect(() => {
    load();
    pollRef.current = setInterval(load, 8000);
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
          <button className="btn btn-outline" onClick={() => nav(-1)}>← Back</button>
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      <div className="top-stats" style={{ gridTemplateColumns: 'repeat(2,1fr)' }}>
        <div className="stat-card"><div className="label">P&L</div><div className="value" style={{ color: (d.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(d.pnl || 0).toFixed(2)}</div></div>
        <div className="stat-card"><div className="label">Status</div><div className="value">{d.status}</div></div>
      </div>

      {legs.length > 0 && (
        <div className="card">
          <div style={{ fontWeight: 700, marginBottom: 12 }}>Legs</div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.85rem' }}>
            <thead><tr>{['Side', 'Type', 'Strike', 'Symbol', 'Size', 'Entry Price'].map(h => <th key={h} style={{ textAlign: 'left', padding: '8px', color: 'var(--muted)', borderBottom: '2px solid var(--border)', fontSize: '.75rem' }}>{h}</th>)}</tr></thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i}>
                  <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{l.side}</td>
                  <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{l.type}</td>
                  <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{l.strike}</td>
                  <td style={{ padding: 8, borderBottom: '1px solid var(--border)', fontWeight: 600 }}>{l.symbol}</td>
                  <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>{l.size}</td>
                  <td style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>${(l.entry_price || 0).toFixed(2)}</td>
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
