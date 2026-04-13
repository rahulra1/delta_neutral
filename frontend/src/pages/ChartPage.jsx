import React, { useState, useEffect, useRef } from 'react';
import { createChart } from 'lightweight-charts';
import api from '../api';

const SYMBOLS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX'];
const TIMEFRAMES = ['15m', '1H', '1D'];
const INDICATORS = ['SMA20', 'SMA50', 'EMA20', 'BB', 'VWAP', 'Supertrend', 'RSI'];
const COLORS = { SMA20: '#2196f3', SMA50: '#ff9800', EMA20: '#e91e63', BB: '#9c27b0', VWAP: '#00bcd4', Supertrend: '#4caf50', RSI: '#ffeb3b' };

export default function ChartPage() {
  const [symbol, setSymbol] = useState('BTC');
  const [interval, setInterval_] = useState('1D');
  const [indicators, setIndicators] = useState([]);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('positions');
  const chartRef = useRef(null);
  const containerRef = useRef(null);
  const seriesRef = useRef({});

  const toggleInd = ind => setIndicators(prev => prev.includes(ind) ? prev.filter(i => i !== ind) : [...prev, ind]);

  useEffect(() => {
    api.get('/chart-data', { params: { symbol, interval: interval, indicators: indicators.join(',') } })
      .then(r => { if (r.data?.error) { setError(r.data.error); setData(null); } else { setError(null); setData(r.data); } })
      .catch(e => { setError(e.response?.data?.error || 'Failed to fetch data'); setData(null); });
  }, [symbol, interval, indicators]);

  useEffect(() => {
    if (!containerRef.current || !data?.candles) return;
    if (chartRef.current) { chartRef.current.remove(); seriesRef.current = {}; }

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth, height: 500,
      layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
      grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
      crosshair: { mode: 0 }, timeScale: { borderColor: '#2B2B43' },
    });
    chartRef.current = chart;

    const candles = chart.addCandlestickSeries({ upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350' });
    candles.setData(data.candles);

    if (data.volume) {
      const vol = chart.addHistogramSeries({ color: '#26a69a', priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
      chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
      vol.setData(data.volume);
    }

    indicators.forEach(ind => {
      if (data[ind]) {
        if (ind === 'BB' && data.BB_upper && data.BB_lower) {
          chart.addLineSeries({ color: COLORS.BB, lineWidth: 1 }).setData(data.BB_upper);
          chart.addLineSeries({ color: COLORS.BB, lineWidth: 1 }).setData(data.BB_lower);
        } else if (ind === 'RSI') {
          // RSI on separate scale
          const rsi = chart.addLineSeries({ color: COLORS.RSI, lineWidth: 1, priceScaleId: 'rsi' });
          chart.priceScale('rsi').applyOptions({ scaleMargins: { top: 0.8, bottom: 0.05 } });
          rsi.setData(data[ind]);
        } else {
          chart.addLineSeries({ color: COLORS[ind] || '#fff', lineWidth: 1 }).setData(data[ind]);
        }
      }
    });

    chart.timeScale().fitContent();
    const ro = new ResizeObserver(() => { if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth }); });
    ro.observe(containerRef.current);
    return () => { ro.disconnect(); chart.remove(); };
  }, [data, indicators]);

  return (
    <div style={{ background: '#131722', minHeight: '100vh', color: '#d1d4dc', padding: 16 }}>
      {/* Top bar */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <select value={symbol} onChange={e => setSymbol(e.target.value)} style={{ background: '#1e222d', color: '#d1d4dc', border: '1px solid #2a2e39', padding: '6px 10px', borderRadius: 4 }}>
          {SYMBOLS.map(s => <option key={s}>{s}</option>)}
        </select>
        {TIMEFRAMES.map(tf => (
          <button key={tf} onClick={() => setInterval_(tf)} style={{ background: tf === interval ? '#2962ff' : '#1e222d', color: '#d1d4dc', border: 'none', padding: '6px 14px', borderRadius: 4, cursor: 'pointer' }}>{tf}</button>
        ))}
        <span style={{ width: 1, height: 24, background: '#2a2e39', margin: '0 4px' }} />
        {INDICATORS.map(ind => (
          <button key={ind} onClick={() => toggleInd(ind)} style={{ background: indicators.includes(ind) ? (COLORS[ind] || '#555') : '#1e222d', color: '#d1d4dc', border: `1px solid ${indicators.includes(ind) ? COLORS[ind] || '#555' : '#2a2e39'}`, padding: '4px 10px', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}>{ind}</button>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 16 }}>
        {/* Chart */}
        <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
          {error && <div style={{ background: '#2a1a1a', border: '1px solid #ef5350', borderRadius: 6, padding: 16, color: '#ef5350', marginBottom: 8 }}>{error}</div>}
          {!error && !data?.candles && <div style={{ color: '#555', padding: 16 }}>No data available</div>}
          <div ref={containerRef} />
        </div>

        {/* Side panel */}
        <div style={{ width: 260, flexShrink: 0 }}>
          {data?.signal && (
            <div style={{ background: '#1e222d', borderRadius: 6, padding: 12, marginBottom: 12 }}>
              <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>Signal</div>
              <div style={{ color: data.signal.type === 'BUY' ? '#26a69a' : '#ef5350', fontWeight: 700, fontSize: 18 }}>{data.signal.type}</div>
              <div style={{ fontSize: 12, color: '#aaa' }}>{data.signal.reason}</div>
            </div>
          )}
          {data?.stats && (
            <div style={{ background: '#1e222d', borderRadius: 6, padding: 12, marginBottom: 12 }}>
              <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>Stats</div>
              {Object.entries(data.stats).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, padding: '2px 0' }}><span style={{ color: '#888' }}>{k}</span><span>{v}</span></div>
              ))}
            </div>
          )}
          {data?.levels && (
            <div style={{ background: '#1e222d', borderRadius: 6, padding: 12, marginBottom: 12 }}>
              <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>Key Levels (S/R)</div>
              {data.levels.map((l, i) => (
                <div key={i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, padding: '2px 0' }}>
                  <span style={{ color: l.type === 'support' ? '#26a69a' : '#ef5350' }}>{l.type}</span><span>{l.price}</span>
                </div>
              ))}
            </div>
          )}
          {data?.swings && (
            <div style={{ background: '#1e222d', borderRadius: 6, padding: 12 }}>
              <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>Structure</div>
              {data.swings.map((s, i) => (
                <div key={i} style={{ fontSize: 13, padding: '2px 0' }}>{s.type}: {s.price} ({s.time})</div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Bottom tabs */}
      <div style={{ marginTop: 16 }}>
        <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
          {['positions', 'levels', 'swings'].map(t => (
            <button key={t} onClick={() => setTab(t)} style={{ background: tab === t ? '#2962ff' : '#1e222d', color: '#d1d4dc', border: 'none', padding: '6px 16px', borderRadius: '4px 4px 0 0', cursor: 'pointer', textTransform: 'capitalize' }}>{t === 'levels' ? 'S/R Levels' : t === 'swings' ? 'Swing Points' : 'Positions'}</button>
          ))}
        </div>
        <div style={{ background: '#1e222d', borderRadius: '0 6px 6px 6px', padding: 12, minHeight: 80 }}>
          {tab === 'positions' && (
            data?.positions?.length ? (
              <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
                <thead><tr>{['Symbol', 'Side', 'Qty', 'Entry', 'LTP', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: 4, color: '#888' }}>{h}</th>)}</tr></thead>
                <tbody>{data.positions.map((p, i) => (
                  <tr key={i}><td style={{ padding: 4 }}>{p.symbol}</td><td>{p.side}</td><td>{p.qty}</td><td>{p.entry}</td><td>{p.ltp}</td><td style={{ color: p.pnl >= 0 ? '#26a69a' : '#ef5350' }}>{p.pnl}</td></tr>
                ))}</tbody>
              </table>
            ) : <div style={{ color: '#555' }}>No open positions</div>
          )}
          {tab === 'levels' && data?.levels?.map((l, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 13 }}>
              <span style={{ color: l.type === 'support' ? '#26a69a' : '#ef5350' }}>{l.type}</span><span>{l.price}</span><span style={{ color: '#555' }}>{l.strength}</span>
            </div>
          ))}
          {tab === 'swings' && data?.swings?.map((s, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 13 }}>
              <span>{s.type}</span><span>{s.price}</span><span style={{ color: '#555' }}>{s.time}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
