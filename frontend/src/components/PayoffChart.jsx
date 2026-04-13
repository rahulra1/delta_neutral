import React, { useMemo } from 'react';
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip } from 'chart.js';
import { Line } from 'react-chartjs-2';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip);

/**
 * Reusable payoff-at-expiry chart.
 * @param {Array} legs - [{side:'buy'|'sell', type:'call'|'put'|'CE'|'PE', strike:number, mark:number, size:number}]
 * @param {number} lotSize - contract multiplier (e.g. 0.001 for BTC, 75 for NIFTY)
 * @param {number} spot - current spot price (optional, for vertical line)
 * @param {string} sym - currency symbol ('$' or '₹')
 * @param {number} height - chart height (default 70)
 */
export default function PayoffChart({ legs = [], lotSize = 1, spot = 0, sym = '$', height = 70 }) {
  const { xs, ys, maxPnl, minPnl } = useMemo(() => {
    if (!legs.length) return { xs: [], ys: [], maxPnl: 0, minPnl: 0 };
    const strikes = legs.map(l => parseFloat(l.strike));
    const center = spot || strikes.reduce((a, b) => a + b, 0) / strikes.length;
    const minS = Math.min(...strikes, center) * 0.85;
    const maxS = Math.max(...strikes, center) * 1.15;
    const step = (maxS - minS) / 100 || 1;
    const xs = [], ys = [];
    let maxPnl = -Infinity, minPnl = Infinity;
    for (let s = minS; s <= maxS; s += step) {
      xs.push(Math.round(s));
      let pnl = 0;
      legs.forEach(l => {
        const K = parseFloat(l.strike);
        const t = (l.type || '').toLowerCase();
        const isCall = t === 'call' || t === 'ce';
        const dir = (l.side || '').toLowerCase() === 'buy' ? 1 : -1;
        const intrinsic = isCall ? Math.max(s - K, 0) : Math.max(K - s, 0);
        pnl += dir * (intrinsic - (l.mark || 0)) * (l.size || l.lots || 1) * lotSize;
      });
      const rounded = parseFloat(pnl.toFixed(2));
      ys.push(rounded);
      maxPnl = Math.max(maxPnl, rounded);
      minPnl = Math.min(minPnl, rounded);
    }
    return { xs, ys, maxPnl, minPnl };
  }, [legs, lotSize, spot]);

  if (!xs.length) return null;

  return (
    <div>
      <Line
        data={{
          labels: xs,
          datasets: [{
            data: ys, borderColor: '#1a1a2e', borderWidth: 2, pointRadius: 0, tension: 0.1,
            fill: { target: 'origin', above: 'rgba(2,192,118,0.1)', below: 'rgba(246,70,93,0.1)' },
          }],
        }}
        options={{
          responsive: true, animation: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `P&L: ${sym}${ctx.parsed.y.toFixed(2)}` } } },
          scales: {
            x: { title: { display: true, text: 'Spot at Expiry', color: '#848e9c', font: { size: 10 } }, grid: { display: false }, ticks: { color: '#9ca3af', maxTicksLimit: 10, font: { size: 10 } } },
            y: { title: { display: true, text: 'P&L', color: '#848e9c', font: { size: 10 } }, grid: { color: 'rgba(0,0,0,0.06)' }, ticks: { color: '#9ca3af', font: { size: 10 } },
              afterDataLimits: s => { if (s.min > 0) s.min = -s.max * 0.1; if (s.max < 0) s.max = -s.min * 0.1; } },
          },
        }}
        height={height}
        plugins={[{
          id: 'zeroAndSpot',
          afterDraw(chart) {
            const ctx = chart.ctx, area = chart.chartArea, yScale = chart.scales.y;
            // Zero line
            const y0 = yScale.getPixelForValue(0);
            if (y0 >= area.top && y0 <= area.bottom) {
              ctx.save(); ctx.beginPath(); ctx.moveTo(area.left, y0); ctx.lineTo(area.right, y0);
              ctx.strokeStyle = 'rgba(0,0,0,0.15)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]); ctx.stroke(); ctx.restore();
            }
            // Spot line
            if (spot) {
              const xIdx = xs.findIndex(v => v >= spot);
              if (xIdx >= 0) {
                const x = chart.scales.x.getPixelForValue(xIdx);
                ctx.save(); ctx.beginPath(); ctx.moveTo(x, area.top); ctx.lineTo(x, area.bottom);
                ctx.strokeStyle = 'rgba(26,26,46,0.4)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]); ctx.stroke();
                ctx.fillStyle = '#1a1a2e'; ctx.font = '10px monospace'; ctx.fillText('Spot', x - 12, area.top - 4); ctx.restore();
              }
            }
          }
        }]}
      />
      <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: '.82rem' }}>
        <div><span style={{ color: 'var(--muted)' }}>Max Profit:</span> <span style={{ color: 'var(--green)', fontWeight: 700 }}>{maxPnl > 1e5 ? '∞' : `${sym}${maxPnl.toFixed(2)}`}</span></div>
        <div><span style={{ color: 'var(--muted)' }}>Max Loss:</span> <span style={{ color: 'var(--red)', fontWeight: 700 }}>{Math.abs(minPnl) > 1e5 ? '∞' : `${sym}${minPnl.toFixed(2)}`}</span></div>
      </div>
    </div>
  );
}
