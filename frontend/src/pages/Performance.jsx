import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import TradeHistory from '../components/TradeHistory';

export default function Performance() {
  const [trades, setTrades] = useState([]);
  const nav = useNavigate();

  useEffect(() => { api.get('/history').then(r => setTrades(r.data.trades || r.data || [])); }, []);

  const total = trades.length;
  const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
  const wins = trades.filter(t => (t.pnl || 0) > 0).length;
  const losses = trades.filter(t => (t.pnl || 0) < 0).length;
  const winRate = total ? (wins / total * 100) : 0;

  return (
    <div className="container">
      <div className="page-title">📊 Performance</div>

      <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)' }}>
        <div className="stat-card"><div className="label">Total Trades</div><div className="value">{total}</div></div>
        <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${totalPnl.toFixed(2)}</div></div>
        <div className="stat-card"><div className="label">Win Rate</div><div className="value">{winRate.toFixed(1)}%</div><div className="sub">{wins}W / {losses}L</div></div>
        <div className="stat-card"><div className="label">Avg P&L</div><div className="value" style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${total ? (totalPnl / total).toFixed(2) : '0.00'}</div></div>
      </div>

      <div className="card">
        <div style={{ fontWeight: 700, marginBottom: 16 }}>Trade History</div>
        <TradeHistory trades={trades} onSelect={t => nav(`/strategy/${t.sid}`)} />
      </div>
    </div>
  );
}
