import React, { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip } from 'chart.js';
import { Line } from 'react-chartjs-2';
import api from '../api';
import TradeHistory from '../components/TradeHistory';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip);

const RANGES = [
  { key: '7d', label: '7D', days: 7 },
  { key: '30d', label: '30D', days: 30 },
  { key: '90d', label: '90D', days: 90 },
  { key: 'all', label: 'All', days: 0 },
  { key: 'custom', label: 'Custom', days: 0 },
];

function toISO(d) { return d.toISOString().slice(0, 10); }

export default function Performance() {
  const [trades, setTrades] = useState([]);
  const [pnlSeries, setPnlSeries] = useState([]);
  const [range, setRange] = useState('all');
  const [customFrom, setCustomFrom] = useState('');
  const [customTo, setCustomTo] = useState('');
  const nav = useNavigate();

  useEffect(() => {
    api.get('/history').then(r => setTrades(r.data.trades || r.data || []));
  }, []);

  useEffect(() => {
    const params = {};
    if (range === 'custom') {
      if (customFrom) params.since = customFrom;
      if (customTo) params.until = customTo;
    } else if (range !== 'all') {
      const days = RANGES.find(r => r.key === range)?.days || 0;
      if (days) params.since = toISO(new Date(Date.now() - days * 86400000));
    }
    api.get('/pnl-series', { params }).then(r => setPnlSeries(r.data.pnl_series || []));
  }, [range, customFrom, customTo]);

  const total = trades.length;
  const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
  const wins = trades.filter(t => (t.pnl || 0) > 0).length;
  const losses = trades.filter(t => (t.pnl || 0) < 0).length;
  const winRate = total ? (wins / total * 100) : 0;
  const avgPnl = total ? totalPnl / total : 0;

  const chartColor = totalPnl >= 0 ? '#22c55e' : '#ef4444';

  return (
    <div className="container">
      <div className="page-title">📊 Performance</div>

      <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)' }}>
        <div className="stat-card"><div className="label">Total Trades</div><div className="value">{total}</div></div>
        <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${totalPnl.toFixed(2)}</div></div>
        <div className="stat-card"><div className="label">Win Rate</div><div className="value">{winRate.toFixed(1)}%</div><div className="sub">{wins}W / {losses}L</div></div>
        <div className="stat-card"><div className="label">Avg P&L</div><div className="value" style={{ color: avgPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${avgPnl.toFixed(2)}</div></div>
      </div>

      {/* P&L Chart */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14, flexWrap: 'wrap', gap: 8 }}>
          <div style={{ fontWeight: 700 }}>Cumulative P&L</div>
          <div style={{ display: 'flex', gap: 4, alignItems: 'center', flexWrap: 'wrap' }}>
            {RANGES.map(r => (
              <button key={r.key} onClick={() => setRange(r.key)} style={{
                padding: '5px 12px', border: '1px solid var(--border)', borderRadius: 6,
                background: range === r.key ? 'var(--accent)' : 'var(--card)',
                color: range === r.key ? '#fff' : 'var(--text)',
                fontSize: '.78rem', fontWeight: 600, cursor: 'pointer',
              }}>{r.label}</button>
            ))}
          </div>
        </div>

        {range === 'custom' && (
          <div style={{ display: 'flex', gap: 10, marginBottom: 14, alignItems: 'center', fontSize: '.82rem' }}>
            <label style={{ color: 'var(--muted)' }}>From</label>
            <input type="date" value={customFrom} onChange={e => setCustomFrom(e.target.value)}
              style={{ padding: '5px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }} />
            <label style={{ color: 'var(--muted)' }}>To</label>
            <input type="date" value={customTo} onChange={e => setCustomTo(e.target.value)}
              style={{ padding: '5px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }} />
          </div>
        )}

        {pnlSeries.length > 0 ? (
          <Line
            data={{
              labels: pnlSeries.map(s => s.date),
              datasets: [{
                data: pnlSeries.map(s => s.pnl),
                borderColor: chartColor, borderWidth: 2, pointRadius: pnlSeries.length < 30 ? 3 : 0,
                pointBackgroundColor: chartColor, tension: 0.3,
                fill: { target: 'origin', above: 'rgba(34,197,94,0.08)', below: 'rgba(239,68,68,0.08)' },
              }],
            }}
            options={{
              responsive: true, animation: false,
              plugins: {
                legend: { display: false },
                tooltip: {
                  backgroundColor: '#fff', borderColor: '#e0e0e0', borderWidth: 1,
                  titleColor: '#999', bodyColor: '#333', bodyFont: { weight: 'bold' },
                  padding: 10, cornerRadius: 6, displayColors: false,
                  callbacks: { label: ctx => `P&L: $${ctx.parsed.y.toFixed(2)}` },
                },
              },
              scales: {
                x: { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 10 }, color: '#aaa' } },
                y: { grid: { color: '#f0f0f0' }, ticks: { font: { size: 10 }, color: '#aaa', callback: v => '$' + v } },
              },
            }}
            height={70}
            plugins={[{
              id: 'zeroLine',
              afterDraw(chart) {
                const yS = chart.scales.y, a = chart.chartArea;
                const y0 = yS.getPixelForValue(0);
                if (y0 >= a.top && y0 <= a.bottom) {
                  const ctx = chart.ctx;
                  ctx.save(); ctx.beginPath(); ctx.moveTo(a.left, y0); ctx.lineTo(a.right, y0);
                  ctx.strokeStyle = 'rgba(0,0,0,0.1)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
                  ctx.stroke(); ctx.setLineDash([]); ctx.restore();
                }
              },
            }]}
          />
        ) : (
          <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 20, textAlign: 'center' }}>
            No completed trades in this range
          </div>
        )}
      </div>

      <div className="card">
        <div style={{ fontWeight: 700, marginBottom: 16 }}>Trade History</div>
        <TradeHistory trades={trades} onSelect={t => nav(`/strategy/${t.sid}`)} />
      </div>
    </div>
  );
}
