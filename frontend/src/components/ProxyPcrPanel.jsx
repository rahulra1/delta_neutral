import React, { useEffect, useState } from 'react';
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Tooltip } from 'chart.js';
import { Line } from 'react-chartjs-2';
import api from '../api';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip);

const RANGES = [{ h: 1, label: '1H' }, { h: 6, label: '6H' }, { h: 24, label: '24H' }, { h: 72, label: '3D' }];

const fmtTime = (ts, multiDay) => {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return multiDay ? `${d.toLocaleDateString([], { day: '2-digit', month: 'short' })} ${t}` : t;
};

function pcrColor(pcr, high, low) {
  if (pcr == null) return 'var(--muted)';
  if (pcr > high) return 'var(--green)';  // put-heavy -> bullish -> sell PE
  if (pcr < low) return 'var(--red)';     // call-heavy -> bearish -> sell CE
  return 'var(--text)';
}

function PcrCard({ sym, d, meta, hours }) {
  const latest = d?.latest;
  const hist = d?.history || [];
  const first = hist.length ? hist[0].pcr : null;
  const change = latest && first != null ? latest.pcr - first : null;
  const prev = hist.length > 1 ? hist[hist.length - 2].pcr : null;
  const tick = latest && prev != null ? latest.pcr - prev : null;
  const color = pcrColor(latest?.pcr, meta.high, meta.low);
  const multiDay = hours > 24;
  const lineColor = latest?.pcr > meta.high ? '#22c55e' : latest?.pcr < meta.low ? '#ef4444' : '#6366f1';

  return (
    <div className="card" style={{ padding: 14, minWidth: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div style={{ fontWeight: 700 }}>{sym}</div>
        <div style={{ fontSize: '.7rem', color: 'var(--muted)' }}>
          {latest ? `${latest.source} · exp ${latest.expiry}` : ''}
        </div>
      </div>

      {latest ? (
        <>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 6 }}>
            <div style={{ fontSize: '1.6rem', fontWeight: 800, color }}>{latest.pcr.toFixed(2)}</div>
            {tick != null && (
              <div style={{ fontSize: '.78rem', fontWeight: 700, color: tick >= 0 ? 'var(--green)' : 'var(--red)' }}>
                {tick >= 0 ? '▲' : '▼'} {Math.abs(tick).toFixed(3)}
              </div>
            )}
            {latest.stale && <span style={{ fontSize: '.68rem', color: 'var(--muted)' }}>(market closed / no update)</span>}
          </div>
          <div style={{ fontSize: '.74rem', color: 'var(--muted)', display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 4 }}>
            <span>Spot {Number(latest.spot).toLocaleString()} · ATM {latest.atm}</span>
            {change != null && (
              <span>{RANGES.find(r => r.h === hours)?.label} chg <b style={{ color: change >= 0 ? 'var(--green)' : 'var(--red)' }}>{change >= 0 ? '+' : ''}{change.toFixed(2)}</b></span>
            )}
          </div>
          <div style={{ fontSize: '.74rem', color: 'var(--muted)', marginTop: 2 }}>
            PE OI {Math.round(latest.put_oi).toLocaleString()} / CE OI {Math.round(latest.call_oi).toLocaleString()}
          </div>
          <div style={{ marginTop: 6, display: 'inline-block', padding: '2px 8px', borderRadius: 4, fontSize: '.72rem', fontWeight: 700, background: 'var(--bg)', border: `1px solid ${lineColor}`, color: lineColor }}>
            {latest.signal}
          </div>
          <div style={{ height: 90, marginTop: 8 }}>
            {hist.length > 1 ? (
              <Line
                data={{
                  labels: hist.map(p => fmtTime(p.ts, multiDay)),
                  datasets: [
                    { label: 'PCR', data: hist.map(p => p.pcr), borderColor: lineColor, borderWidth: 1.8, pointRadius: 0, tension: 0.25 },
                    { label: `${meta.high}`, data: hist.map(() => meta.high), borderColor: 'rgba(34,197,94,.5)', borderDash: [4, 4], borderWidth: 1, pointRadius: 0 },
                    { label: `${meta.low}`, data: hist.map(() => meta.low), borderColor: 'rgba(239,68,68,.5)', borderDash: [4, 4], borderWidth: 1, pointRadius: 0 },
                  ],
                }}
                options={{
                  responsive: true, maintainAspectRatio: false, animation: false,
                  plugins: { legend: { display: false }, tooltip: { filter: i => i.datasetIndex === 0, callbacks: { label: c => `PCR ${c.parsed.y.toFixed(3)}  · spot ${hist[c.dataIndex]?.spot}` } } },
                  scales: {
                    x: { grid: { display: false }, ticks: { maxTicksLimit: 4, font: { size: 9 }, color: '#aaa' } },
                    y: { grid: { color: 'rgba(0,0,0,.05)' }, ticks: { maxTicksLimit: 4, font: { size: 9 }, color: '#aaa' } },
                  },
                }}
              />
            ) : (
              <div style={{ fontSize: '.75rem', color: 'var(--muted)', paddingTop: 30, textAlign: 'center' }}>Collecting history…</div>
            )}
          </div>
        </>
      ) : (
        <div style={{ fontSize: '.78rem', color: 'var(--red)', marginTop: 10 }}>
          {d?.error ? `Unavailable: ${d.error}` : 'Waiting for first sample…'}
        </div>
      )}
    </div>
  );
}

export default function ProxyPcrPanel() {
  const [data, setData] = useState(null);
  const [hours, setHours] = useState(6);

  useEffect(() => {
    const load = () => api.get('/proxy-pcr', { params: { hours } }).then(r => setData(r.data)).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [hours]);

  if (!data) return null;
  const meta = { high: data.high, low: data.low };

  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', gap: 8 }}>
        <div>
          <div style={{ fontWeight: 700 }}>📊 Proxy PCR</div>
          <div style={{ fontSize: '.72rem', color: 'var(--muted)' }}>
            Top {data.top} PE OI ÷ top {data.top} CE OI within ATM ±{data.window} strikes, nearest expiry · updates every {data.sample_secs}s ·
            &gt;{data.high} sell PE · &lt;{data.low} sell CE · else sell both
          </div>
        </div>
        <div style={{ display: 'flex', gap: 4 }}>
          {RANGES.map(r => (
            <button key={r.h} onClick={() => setHours(r.h)} style={{
              padding: '4px 10px', borderRadius: 6, fontSize: '.75rem', fontWeight: 600, cursor: 'pointer',
              border: hours === r.h ? '1.5px solid var(--accent)' : '1.5px solid var(--border)',
              background: hours === r.h ? 'var(--accent)' : 'var(--bg)', color: hours === r.h ? '#fff' : 'var(--text)',
            }}>{r.label}</button>
          ))}
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
        {(data.order || Object.keys(data.symbols)).map(sym => (
          <PcrCard key={sym} sym={sym} d={data.symbols[sym]} meta={meta} hours={hours} />
        ))}
      </div>
    </div>
  );
}
