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

export default function PayoffChart({ legs = [], lotSize = 1, spot = 0, sym = '$', daysToExpiry = 30, height = 180 }) {
  const [showScenario, setShowScenario] = useState(true);
  const [ivOffset, setIvOffset] = useState(0);
  const [spotOffset, setSpotOffset] = useState(0);
  const [targetDays, setTargetDays] = useState(Math.max(1, Math.floor(daysToExpiry / 2)));

  const adjSpot = spot + spotOffset;
  const spotRef = useRef(adjSpot);
  spotRef.current = adjSpot;

  const calcPnl = useCallback((S, T) => {
    let pnl = 0;
    legs.forEach(l => {
      const K = parseFloat(l.strike), t = (l.type || '').toLowerCase();
      const isCall = t === 'call' || t === 'ce';
      const dir = (l.side || '').toLowerCase() === 'buy' ? 1 : -1;
      const iv = Math.max(0.01, (l.iv || 0.5) + ivOffset / 100);
      pnl += dir * (bsPrice(isCall ? 'call' : 'put', S, K, T, 0.05, iv) - (l.mark || 0)) * (l.size || 1) * lotSize;
    });
    return parseFloat(pnl.toFixed(2));
  }, [legs, lotSize, ivOffset]);

  const { xs, expiryData, targetData, maxProfit, maxLoss, breakevens } = useMemo(() => {
    if (!legs.length) return { xs: [], expiryData: [], targetData: [], maxProfit: 0, maxLoss: 0, breakevens: [] };
    const strikes = legs.map(l => parseFloat(l.strike));
    const center = adjSpot || strikes.reduce((a, b) => a + b, 0) / strikes.length;
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
  }, [legs, adjSpot, targetDays, calcPnl]);

  const greeks = useMemo(() => {
    let delta = 0;
    legs.forEach(l => { delta += (l.side === 'buy' ? 1 : -1) * (parseFloat(l.delta) || 0) * (l.size || 1); });
    return { delta: delta.toFixed(2) };
  }, [legs]);

  const mtm = calcPnl(adjSpot, targetDays / 365);
  const rr = maxLoss < 0 ? Math.abs(maxProfit / maxLoss).toFixed(2) : 'NA';
  const spotPctStr = spot ? `${((spotOffset / spot) * 100).toFixed(1)}%` : '0%';

  if (!xs.length) return null;

  const avgIV = legs.reduce((s, l) => s + (l.iv || 0.5), 0) / (legs.length || 1);

  return (
    <div>
      {/* ── Scenario Analysis Header ── */}
      <div onClick={() => setShowScenario(!showScenario)}
        style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600, color: 'var(--accent)', marginBottom: showScenario ? 12 : 0, userSelect: 'none' }}>
        <span style={{ fontSize: 10, transform: showScenario ? 'rotate(0)' : 'rotate(-90deg)', transition: 'transform .15s' }}>▼</span>
        Scenario Analysis
      </div>

      {showScenario && (
        <div style={{ display: 'flex', gap: 16, marginBottom: 16 }}>
          {/* Left controls */}
          <div style={{ flex: '0 0 160px', borderRight: '1px solid #eee', paddingRight: 14 }}>
            {/* IV Offset */}
            <div style={{ marginBottom: 14 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: '#999', fontWeight: 600 }}>IV Offset</span>
                <span onClick={() => setIvOffset(0)} style={{ fontSize: 9, color: 'var(--accent)', cursor: 'pointer' }}>Reset</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 0, border: '1px solid #e0e0e0', borderRadius: 4, overflow: 'hidden' }}>
                <button onClick={() => setIvOffset(v => v - 0.5)} style={{ ...btnStyle }}>−</button>
                <span style={{ flex: 1, textAlign: 'center', fontSize: 12, fontWeight: 700, padding: '4px 0' }}>{ivOffset}</span>
                <button onClick={() => setIvOffset(v => v + 0.5)} style={{ ...btnStyle }}>+</button>
              </div>
            </div>

            {/* Spot */}
            <div style={{ marginBottom: 14 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: '#999', fontWeight: 600 }}>Spot <span style={{ color: spotOffset >= 0 ? '#22c55e' : '#ef4444' }}>{spotPctStr}</span></span>
                <span onClick={() => setSpotOffset(0)} style={{ fontSize: 9, color: 'var(--accent)', cursor: 'pointer' }}>Reset</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 0, border: '1px solid #e0e0e0', borderRadius: 4, overflow: 'hidden' }}>
                <button onClick={() => setSpotOffset(v => v - (spot * 0.005))} style={{ ...btnStyle }}>−</button>
                <span style={{ flex: 1, textAlign: 'center', fontSize: 12, fontWeight: 700, padding: '4px 0' }}>{Math.round(adjSpot).toLocaleString()}</span>
                <button onClick={() => setSpotOffset(v => v + (spot * 0.005))} style={{ ...btnStyle }}>+</button>
              </div>
            </div>

            {/* Date */}
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: '#999', fontWeight: 600 }}>Date</span>
                <span onClick={() => setTargetDays(Math.max(1, Math.floor(daysToExpiry / 2)))} style={{ fontSize: 9, color: 'var(--accent)', cursor: 'pointer' }}>Reset</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 0, border: '1px solid #e0e0e0', borderRadius: 4, overflow: 'hidden' }}>
                <button onClick={() => setTargetDays(v => Math.max(0, v - 1))} style={{ ...btnStyle }}>‹</button>
                <span style={{ flex: 1, textAlign: 'center', fontSize: 11, fontWeight: 700, padding: '4px 0' }}>{targetDays}d DTE</span>
                <button onClick={() => setTargetDays(v => Math.min(daysToExpiry, v + 1))} style={{ ...btnStyle }}>›</button>
              </div>
            </div>
          </div>

          {/* Right stats */}
          <div style={{ flex: 1 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '10px 20px', marginBottom: 12 }}>
              <ScVal label="Total MTM" value={`${sym} ${mtm.toFixed(2)}`} sub={spot ? `(${((mtm / spot) * 100).toFixed(2)}%)` : ''} color={mtm >= 0 ? '#22c55e' : '#ef4444'} />
              <ScVal label="Maximum Profit" value={maxProfit > 1e6 ? 'Unlimited' : `${sym} ${maxProfit.toFixed(2)}`} color="#22c55e" />
              <ScVal label="Risk/Reward" value={rr} />
              <ScVal label="POP" value="—" />
              <ScVal label="Maximum Loss" value={Math.abs(maxLoss) > 1e6 ? 'Unlimited' : `${sym} ${maxLoss.toFixed(2)}`} color="#ef4444" />
              <ScVal label="Margin Approx" value="—" />
            </div>
            <div style={{ marginBottom: 0 }}>
              <ScVal label="Breakeven" value={breakevens.length ? breakevens.map(b => `${b.toLocaleString()} (${((b - spot) / spot * 100).toFixed(1)}%)`).join('  ') : '—'} />
            </div>
          </div>
        </div>
      )}

      {/* ── Greeks Row ── */}
      <div style={{ display: 'flex', gap: 28, alignItems: 'center', padding: '10px 0', borderTop: '1px solid #eee', borderBottom: '1px solid #eee', marginBottom: 12, fontSize: 12, color: '#666' }}>
        <span>Delta: <b style={{ color: '#333' }}>{greeks.delta}</b></span>
        <span>Gamma: <b style={{ color: '#333' }}>—</b></span>
        <span style={{ flex: 1 }} />
        <span>Theta: <b style={{ color: '#333' }}>—</b></span>
        <span>Vega: <b style={{ color: '#333' }}>—</b></span>
      </div>

      {/* ── Chart ── */}
      <div style={{ position: 'relative' }}>
        <div style={{ textAlign: 'center', marginBottom: 4, fontSize: 11, color: '#666' }}>
          MTM: <b style={{ color: mtm >= 0 ? '#22c55e' : '#ef4444' }}>{sym}{mtm.toFixed(2)}</b>
          <span style={{ margin: '0 8px', color: '#ccc' }}>|</span>
          Spot: <b>{sym}{adjSpot ? adjSpot.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}</b>
        </div>

        <Line
          data={{
            labels: xs,
            datasets: [
              {
                label: 'On Expiry', data: expiryData, borderWidth: 2, pointRadius: 0, tension: 0.1,
                segment: { borderColor: ctx => (ctx.p0.parsed.y >= 0 && ctx.p1.parsed.y >= 0) ? '#22c55e' : (ctx.p0.parsed.y <= 0 && ctx.p1.parsed.y <= 0) ? '#ef4444' : '#9ca3af' },
                fill: { target: 'origin', above: 'rgba(34,197,94,0.15)', below: 'rgba(239,68,68,0.15)' },
              },
              {
                label: 'Target Date', data: targetData, borderColor: '#6366f1', borderWidth: 1.5, pointRadius: 0, tension: 0.2, borderDash: [4, 3], fill: false,
              },
            ],
          }}
          options={{
            responsive: true, animation: false, interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { display: false },
              tooltip: {
                backgroundColor: '#fff', borderColor: '#e0e0e0', borderWidth: 1, titleColor: '#999', bodyColor: '#333',
                titleFont: { size: 10 }, bodyFont: { size: 11, weight: 'bold' }, padding: 10, cornerRadius: 6, displayColors: false,
                callbacks: {
                  title: ctx => `Price: ${Number(ctx[0].label).toLocaleString()}`,
                  label: ctx => `${ctx.datasetIndex === 0 ? 'Expiry' : `Target (${targetDays}d)`}: ${sym}${ctx.parsed.y.toFixed(2)}`,
                  labelTextColor: ctx => ctx.parsed.y >= 0 ? '#22c55e' : '#ef4444',
                },
              },
            },
            scales: {
              x: { grid: { color: 'rgba(0,0,0,0.04)' }, ticks: { color: '#aaa', maxTicksLimit: 8, font: { size: 10 } }, border: { color: '#e0e0e0' } },
              y: { grid: { color: 'rgba(0,0,0,0.04)' }, ticks: { color: '#aaa', font: { size: 10 }, callback: v => sym + v.toLocaleString() }, border: { color: '#e0e0e0' },
                afterDataLimits: s => { if (s.min > 0) s.min = -s.max * 0.1; if (s.max < 0) s.max = -s.min * 0.1; } },
            },
            layout: { padding: { top: 10 } },
          }}
          height={height}
          plugins={[{
            id: 'overlays',
            afterDraw(chart) {
              const ctx = chart.ctx, a = chart.chartArea, yS = chart.scales.y, xS = chart.scales.x;
              // Zero line
              const y0 = yS.getPixelForValue(0);
              if (y0 >= a.top && y0 <= a.bottom) {
                ctx.save(); ctx.beginPath(); ctx.moveTo(a.left, y0); ctx.lineTo(a.right, y0);
                ctx.strokeStyle = 'rgba(0,0,0,0.12)'; ctx.lineWidth = 1; ctx.stroke(); ctx.restore();
              }
              // Spot line
              const spotIdx = xs.findIndex(v => v >= adjSpot);
              if (spotIdx >= 0) {
                const x = xS.getPixelForValue(spotIdx);
                ctx.save(); ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom);
                ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.5; ctx.stroke(); ctx.restore();
              }
              // SD markers
              const sd = adjSpot * avgIV * Math.sqrt(daysToExpiry / 365);
              ctx.save();
              for (let n = -3; n <= 3; n++) {
                if (n === 0) continue;
                const idx = xs.findIndex(v => v >= adjSpot + n * sd);
                if (idx < 0) continue;
                const x = xS.getPixelForValue(idx);
                if (x < a.left || x > a.right) continue;
                ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom);
                ctx.strokeStyle = 'rgba(0,0,0,0.08)'; ctx.lineWidth = 1; ctx.setLineDash([2, 4]); ctx.stroke(); ctx.setLineDash([]);
                ctx.fillStyle = '#aaa'; ctx.font = '9px sans-serif'; ctx.textAlign = 'center';
                ctx.fillText(`${n > 0 ? '+' : ''}${n} SD`, x, a.top - 4);
              }
              ctx.restore();
            }
          }]}
        />
      </div>
    </div>
  );
}

const btnStyle = { background: 'none', border: 'none', borderRight: '1px solid #e0e0e0', padding: '4px 10px', cursor: 'pointer', fontSize: 14, color: '#666', fontWeight: 700 };

function ScVal({ label, value, sub, color }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: '#999', marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 13, fontWeight: 700, color: color || '#333' }}>
        {value} {sub && <span style={{ fontSize: 10, fontWeight: 400 }}>{sub}</span>}
      </div>
    </div>
  );
}
