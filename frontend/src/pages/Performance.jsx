import React, { useState, useEffect } from 'react';
import api from '../api';

export default function Performance() {
  const [trades, setTrades] = useState([]);

  useEffect(() => { api.get('/history').then(r => setTrades(r.data.trades || r.data || [])); }, []);

  const total = trades.length;
  const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
  const wins = trades.filter(t => (t.pnl || 0) > 0).length;

  return (
    <div className="container">
      <div className="page-title">Performance</div>

      <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)' }}>
        <div className="stat-card"><div className="label">Total Trades</div><div className="value">{total}</div></div>
        <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${totalPnl.toFixed(2)}</div></div>
        <div className="stat-card"><div className="label">Win Rate</div><div className="value">{total ? ((wins / total) * 100).toFixed(1) : 0}%</div></div>
      </div>

      <div className="card">
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.85rem' }}>
          <thead><tr>{['SID', 'Started', 'Ended', 'Expiry', 'Lots', 'Delta', 'Adj', 'Status', 'PnL'].map(h => <th key={h} style={{ textAlign: 'left', padding: '8px 10px', color: 'var(--muted)', fontSize: '.75rem', borderBottom: '2px solid var(--border)' }}>{h}</th>)}</tr></thead>
          <tbody>
            {trades.map(t => (
              <tr key={t.sid}>
                <td style={{ padding: '10px', fontWeight: 600, borderBottom: '1px solid var(--border)' }}>{t.sid}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)', fontSize: '.8rem' }}>{(t.started_at || '').replace('T', ' ').slice(0, 16)}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)', fontSize: '.8rem' }}>{(t.ended_at || '—').replace('T', ' ').slice(0, 16)}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{t.expiry || t.params?.expiry_date || '—'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{t.lots || t.params?.lot_size || '—'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{t.delta || t.params?.target_delta || '—'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{t.adjustments ?? '—'}</td>
                <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}><span className={`badge ${t.status === 'running' ? 'badge-yellow' : (t.pnl || 0) >= 0 ? 'badge-green' : 'badge-red'}`}>{t.status}</span></td>
                <td style={{ padding: '10px', fontWeight: 700, borderBottom: '1px solid var(--border)', color: (t.pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(t.pnl || 0).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!trades.length && <div style={{ padding: 20, textAlign: 'center', color: 'var(--muted)' }}>No trade history</div>}
      </div>
    </div>
  );
}
