import React, { useMemo, useState, useCallback, useRef } from 'react';
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip } from 'chart.js';
import { Line } from 'react-chartjs-2';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip);

function bsPrice(type, S, K, T, r, sigma) {
  if (T <= 0) return type === 'call' ? Math.max(S - K, 0) : Math.max(K - S, 0);
  const d1 = (Math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * Math.sqrt(T));
  const d2 = d1 - sigma * Math.sqrt(T);
  return type === 'call' ? S * cdf(d1) - K * Math.exp(-r * T) * cdf(d2) : K * Math.exp(-r * T) * cdf(-d2) - S * cdf(-d1);
}
function cdf(x) { const a = 0.2316419, b = [0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429]; const k = 1 / (1 + a * Math.abs(x)); const n = Math.exp(-x * x / 2) / Math.sqrt(2 * Math.PI) * k * (b[0] + k * (b[1] + k * (b[2] + k * (b[3] + k * b[4])))); return x >= 0 ? 1 - n : n; }

export default function PayoffChart({ legs = [], lotSize = 1, spot = 0, sym = '$', daysToExpiry = 30, height = 100 }) {
  const [targetPrice, setTargetPrice] = useState(spot || 0);
  const [targetDays, setTargetDays] = useState(Math.max(1, Math.floor(daysToExpiry / 2)));
  const effectiveTarget = targetPrice || spot;
  const targetRef = useRef(effectiveTarget);
  targetRef.current = effectiveTarget;

  const calcPnl = useCallback((S, T) => {
    let pnl = 0;
    legs.forEach(l => {
      const K = parseFloat(l.strike), t = (l.type || '').toLowerCase();
      const isCall = t === 'call' || t === 'ce';
      const dir = (l.side || '').toLowerCase() === 'buy' ? 1 : -1;
      pnl += dir * (bsPrice(isCall ? 'call' : 'put', S, K, T, 0.05, l.iv || 0.5) - (l.mark || 0)) * (l.size || l.lots || 1) * lotSize;
    });
    return parseFloat(pnl.toFixed(2));
  }, [legs, lotSize]);

  const { xs, expiryData, targetData, maxProfit, maxLoss, breakevens } = useMemo(() => {
    if (!legs.length) return { xs: [], expiryData: [], targetData: [], maxProfit: 0, maxLoss: 0, breakevens: [] };
    const strikes = legs.map(l => parseFloat(l.strike));
    const center = spot || strikes.reduce((a, b) => a + b, 0) / strikes.length;
    const range = center * 0.2;
    const xs = [], expiryData = [], targetData = [];
    let maxP = -Infinity, maxL = Infinity;
    const step = (range * 2) / 150;
    for (let s = center - range; s <= center + range; s += step) {
      xs.push(Math.round(s));
      const ep = calcPnl(s, 0), tp = calcPnl(s, targetDays / 365);
      expiryData.push(ep); targetData.push(tp);
      maxP = Math.max(maxP, ep); maxL = Math.min(maxL, ep);
    }
    const be = [];
    for (let i = 1; i < expiryData.length; i++) {
      if ((expiryData[i - 1] <= 0 && expiryData[i] >= 0) || (expiryData[i - 1] >= 0 && expiryData[i] <= 0)) be.push(xs[i]);
    }
    return { xs, expiryData, targetData, maxProfit: maxP, maxLoss: maxL, breakevens: be };
  }, [legs, spot, targetDays, calcPnl]);

  const projectedPnl = useMemo(() => calcPnl(effectiveTarget, targetDays / 365), [effectiveTarget, targetDays, calcPnl]);
  const expiryPnl = useMemo(() => calcPnl(effectiveTarget, 0), [effectiveTarget, calcPnl]);
  const rr = maxLoss < 0 ? Math.abs(maxProfit / maxLoss).toFixed(2) : 'NA';

  if (!xs.length) return null;
  const minS = xs[0], maxS = xs[xs.length - 1];

  // Slider position as % for the green marker
  const sliderPct = ((effectiveTarget - minS) / (maxS - minS)) * 100;

  return (
    <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 12, padding: 20 }}>
      {/* Header stats */}
      <div style={{ display: 'flex', gap: 20, marginBottom: 16, flexWrap: 'wrap' }}>
        {[
          ['Max Profit', maxProfit > 1e6 ? '∞' : `${sym}${maxProfit.toFixed(2)}`, 'var(--green)'],
          ['Max Loss', Math.abs(maxLoss) > 1e6 ? 'Unlimited' : `${sym}${maxLoss.toFixed(2)}`, 'var(--red)'],
          ['Reward / Risk', rr, 'var(--text)'],
          ['Breakeven', breakevens.length ? breakevens.map(b => b.toLocaleString()).join(', ') : '—', 'var(--text)'],
        ].map(([lbl, val, clr]) => (
          <div key={lbl}>
            <div style={{ fontSize: '.68rem', color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.3px' }}>{lbl}</div>
            <div style={{ fontWeight: 700, fontSize: '.88rem', color: clr }}>{val}</div>
          </div>
        ))}
      </div>

      {/* Legend */}
      <div style={{ display: 'flex', gap: 20, marginBottom: 6, fontSize: '.72rem', color: 'var(--muted)' }}>
        <span><span style={{ display: 'inline-block', width: 18, height: 3, background: 'var(--green)', marginRight: 6, verticalAlign: 'middle', borderRadius: 2 }} />On Expiry Date</span>
        <span style={{ color: 'var(--text)', fontWeight: 600 }}>Index {spot ? spot.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}</span>
        <span><span style={{ display: 'inline-block', width: 18, height: 3, background: '#f59e0b', marginRight: 6, verticalAlign: 'middle', borderRadius: 2 }} />On Target Date</span>
      </div>

      {/* Current Price badge */}
      <div style={{ textAlign: 'center', marginBottom: -6, position: 'relative', zIndex: 2 }}>
        <span style={{ background: 'var(--bg)', border: '1px solid var(--border)', padding: '4px 16px', borderRadius: 6, fontSize: '.8rem', fontWeight: 700 }}>Current Price {effectiveTarget.toLocaleString()}</span>
      </div>

      {/* Chart */}
      <Line
        data={{
          labels: xs,
          datasets: [
            { label: 'On Expiry', data: expiryData, borderWidth: 2, pointRadius: 0, tension: 0.1,
              segment: { borderColor: ctx => (ctx.p0.parsed.y >= 0 && ctx.p1.parsed.y >= 0) ? '#22c55e' : (ctx.p0.parsed.y <= 0 && ctx.p1.parsed.y <= 0) ? '#ef4444' : '#9ca3af' },
              fill: { target: 'origin', above: 'rgba(34,197,94,0.12)', below: 'rgba(239,68,68,0.12)' } },
            { label: 'On Target Date', data: targetData, borderColor: '#f59e0b', borderWidth: 2, pointRadius: 0, tension: 0.2, fill: false },
          ],
        }}
        options={{
          responsive: true, animation: false, interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#ffffff', borderColor: '#e8e8e8', borderWidth: 1, titleColor: '#9ca3af', bodyColor: '#1a1a2e',
              titleFont: { size: 11 }, bodyFont: { size: 12, weight: 'bold' }, padding: 12, cornerRadius: 8, displayColors: false,
              callbacks: {
                title: ctx => `When price is at: ${Number(ctx[0].label).toLocaleString()}`,
                afterTitle: () => 'Expected PNL on',
                label: ctx => {
                  const lbl = ctx.datasetIndex === 0 ? 'Expiry' : `Target (${targetDays}d)`;
                  return `${lbl}:  ${sym}${ctx.parsed.y.toFixed(2)}`;
                },
                labelTextColor: ctx => ctx.parsed.y >= 0 ? '#22c55e' : '#ef4444',
              },
            },
          },
          scales: {
            x: { grid: { color: 'rgba(0,0,0,0.04)' }, ticks: { color: '#9ca3af', maxTicksLimit: 8, font: { size: 10 } } },
            y: { grid: { color: 'rgba(0,0,0,0.04)' }, ticks: { color: '#9ca3af', font: { size: 10 } },
              afterDataLimits: s => { if (s.min > 0) s.min = -s.max * 0.1; if (s.max < 0) s.max = -s.min * 0.1; } },
          },
        }}
        height={height}
        plugins={[{
          id: 'lines',
          afterDraw(chart) {
            const ctx = chart.ctx, a = chart.chartArea, yS = chart.scales.y, xS = chart.scales.x;
            const y0 = yS.getPixelForValue(0);
            if (y0 >= a.top && y0 <= a.bottom) {
              ctx.save(); ctx.beginPath(); ctx.moveTo(a.left, y0); ctx.lineTo(a.right, y0);
              ctx.strokeStyle = 'rgba(0,0,0,0.08)'; ctx.lineWidth = 1; ctx.stroke(); ctx.restore();
            }
            const t = targetRef.current;
            const idx = xs.findIndex(v => v >= t);
            if (idx >= 0) {
              const x = xS.getPixelForValue(idx);
              ctx.save(); ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom);
              ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.restore();
            }
          }
        }]}
      />

      {/* Projected badge */}
      <div style={{ textAlign: 'center', marginTop: 10 }}>
        <span style={{ background: projectedPnl >= 0 ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)', border: `1px solid ${projectedPnl >= 0 ? 'var(--green)' : 'var(--red)'}`, color: projectedPnl >= 0 ? 'var(--green)' : 'var(--red)', padding: '6px 20px', borderRadius: 6, fontSize: '.85rem', fontWeight: 700 }}>
          Projected {projectedPnl >= 0 ? 'Profit' : 'Loss'}: {sym}{Math.abs(projectedPnl).toFixed(2)}
        </span>
      </div>

      {/* Targets */}
      <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 16, marginTop: 16 }}>
        <div style={{ textAlign: 'center', fontSize: '.82rem', fontWeight: 700, marginBottom: 16 }}>Targets ▲</div>

        {/* Target Price */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontSize: '.82rem', color: 'var(--muted)' }}>Target Price</span>
            <span style={{ fontSize: '1rem', fontWeight: 800 }}>{effectiveTarget.toLocaleString()}</span>
          </div>
          <div style={{ position: 'relative', height: 32 }}>
            <input type="range" min={minS} max={maxS} step={Math.max(1, Math.round((maxS - minS) / 500))} value={effectiveTarget}
              onChange={e => setTargetPrice(+e.target.value)}
              style={{ width: '100%', position: 'absolute', top: 8, left: 0, accentColor: '#22c55e', zIndex: 2, opacity: 0, cursor: 'pointer', height: 20 }} />
            {/* Custom track */}
            <div style={{ position: 'absolute', top: 12, left: 0, right: 0, height: 6, background: 'var(--border)', borderRadius: 3 }}>
              <div style={{ position: 'absolute', left: 0, top: 0, height: '100%', width: `${sliderPct}%`, background: 'linear-gradient(90deg, var(--green), #6366f1)', borderRadius: 3 }} />
            </div>
            {/* Thumb */}
            <div style={{ position: 'absolute', top: 4, left: `calc(${sliderPct}% - 14px)`, width: 28, height: 22, background: '#22c55e', borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 8px rgba(34,197,94,0.4)', transition: 'left 0.05s', pointerEvents: 'none' }}>
              <span style={{ color: '#fff', fontSize: 10, fontWeight: 800 }}>|||</span>
            </div>
          </div>
        </div>

        {/* Target Date */}
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontSize: '.82rem', color: 'var(--muted)' }}>Target Date</span>
            <span style={{ fontSize: '1rem', fontWeight: 800 }}>{targetDays}d <span style={{ fontSize: '.75rem', color: 'var(--muted)', fontWeight: 400 }}>to Expiry</span></span>
          </div>
          <div style={{ position: 'relative', height: 32 }}>
            <input type="range" min={0} max={daysToExpiry} value={targetDays} onChange={e => setTargetDays(+e.target.value)}
              style={{ width: '100%', position: 'absolute', top: 8, left: 0, opacity: 0, cursor: 'pointer', height: 20, zIndex: 2 }} />
            <div style={{ position: 'absolute', top: 12, left: 0, right: 0, height: 6, background: 'var(--border)', borderRadius: 3 }}>
              <div style={{ position: 'absolute', left: 0, top: 0, height: '100%', width: `${(targetDays / daysToExpiry) * 100}%`, background: 'linear-gradient(90deg, #f59e0b, #ef4444)', borderRadius: 3 }} />
            </div>
            <div style={{ position: 'absolute', top: 4, left: `calc(${(targetDays / daysToExpiry) * 100}% - 14px)`, width: 28, height: 22, background: '#f59e0b', borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 8px rgba(245,158,11,0.4)', transition: 'left 0.05s', pointerEvents: 'none' }}>
              <span style={{ color: '#fff', fontSize: 10, fontWeight: 800 }}>|||</span>
            </div>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '.6rem', color: 'var(--muted)', marginTop: 6 }}><span>Expiry</span><span>Today</span></div>
        </div>
      </div>

      {/* PNL Summary */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 16 }}>
        <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 14, textAlign: 'center' }}>
          <div style={{ fontSize: '.7rem', color: 'var(--muted)', textTransform: 'uppercase' }}>Total Target PNL</div>
          <div style={{ fontSize: '1.1rem', fontWeight: 800, color: projectedPnl >= 0 ? 'var(--green)' : 'var(--red)', marginTop: 4 }}>{sym}{projectedPnl.toFixed(2)}</div>
        </div>
        <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 14, textAlign: 'center' }}>
          <div style={{ fontSize: '.7rem', color: 'var(--muted)', textTransform: 'uppercase' }}>Expiry PNL @ Target</div>
          <div style={{ fontSize: '1.1rem', fontWeight: 800, color: expiryPnl >= 0 ? 'var(--green)' : 'var(--red)', marginTop: 4 }}>{sym}{expiryPnl.toFixed(2)}</div>
        </div>
      </div>
    </div>
  );
}
