import React, { useState, useEffect, useRef } from 'react';
import { createChart } from 'lightweight-charts';
import api from '../api';

const SYMBOLS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX'];
const TIMEFRAMES = ['15m', '1h', '1d'];
const TF_LABELS = { '15m': '15m', '1h': '1H', '1d': '1D' };
const IND_LIST = [
  { key: 'sma20', label: 'SMA 20', color: '#6366f1' },
  { key: 'sma50', label: 'SMA 50', color: '#f59e0b' },
  { key: 'ema20', label: 'EMA 20', color: '#8b5cf6' },
  { key: 'bb', label: 'BB', color: '#2196f3' },
  { key: 'vwap', label: 'VWAP', color: '#ec4899' },
  { key: 'supertrend', label: 'Supertrend', color: '#22c55e' },
  { key: 'rsi', label: 'RSI', color: '#7c3aed' },
  { key: 'rsi_div_mss', label: 'Div+MSS', color: '#ef4444' },
];

export default function ChartPage() {
  const [symbol, setSymbol] = useState('BTC');
  const [tf, setTf] = useState('1h');
  const [activeInds, setActiveInds] = useState(new Set());
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('positions');
  const chartRef = useRef(null);
  const containerRef = useRef(null);

  const toggleInd = key => setActiveInds(prev => {
    const n = new Set(prev);
    n.has(key) ? n.delete(key) : n.add(key);
    if (key === 'rsi_div_mss' && n.has('rsi_div_mss') && !n.has('rsi')) n.add('rsi');
    return n;
  });

  useEffect(() => {
    setError(null);
    api.get('/chart-data', { params: { symbol, interval: tf, indicators: [...activeInds].join(',') } })
      .then(r => { if (r.data?.error) { setError(r.data.error); setData(null); } else { setError(null); setData(r.data); } })
      .catch(e => { setError(e.response?.data?.error || 'Failed to fetch'); setData(null); });
  }, [symbol, tf, activeInds]);

  useEffect(() => {
    if (!containerRef.current || !data?.candles?.length) return;
    if (chartRef.current) chartRef.current.remove();

    const el = containerRef.current;
    const chart = createChart(el, {
      width: el.clientWidth, height: el.clientHeight || 500,
      layout: { background: { type: 'solid', color: '#131722' }, textColor: '#d1d4dc', fontSize: 11 },
      grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
      crosshair: { mode: 0, vertLine: { color: '#758696', width: 1, style: 3, labelBackgroundColor: '#2a2e39' }, horzLine: { color: '#758696', width: 1, style: 3, labelBackgroundColor: '#2a2e39' } },
      rightPriceScale: { borderColor: '#2a2e39' },
      timeScale: { timeVisible: tf !== '1d', borderColor: '#2a2e39', rightOffset: 5, barSpacing: 6 },
    });
    chartRef.current = chart;

    // Candles
    const cs = chart.addCandlestickSeries({ upColor: '#26a65b', downColor: '#ea3943', borderUpColor: '#26a65b', borderDownColor: '#ea3943', wickUpColor: '#26a65b', wickDownColor: '#ea3943' });
    cs.setData(data.candles.map(c => ({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c })));

    // Volume
    const vs = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    vs.setData(data.candles.map(c => ({ time: c.t, value: c.v || 0, color: c.c >= c.o ? 'rgba(38,166,91,0.25)' : 'rgba(234,57,67,0.25)' })));

    // Indicators
    const inds = data.indicators || {};
    if (inds.sma20) chart.addLineSeries({ color: '#6366f1', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, title: 'SMA 20' }).setData(inds.sma20.data);
    if (inds.sma50) chart.addLineSeries({ color: '#f59e0b', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, title: 'SMA 50' }).setData(inds.sma50.data);
    if (inds.ema20) chart.addLineSeries({ color: '#8b5cf6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, title: 'EMA 20' }).setData(inds.ema20.data);
    if (inds.vwap) chart.addLineSeries({ color: '#ec4899', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, title: 'VWAP' }).setData(inds.vwap.data);
    if (inds.bb) {
      chart.addLineSeries({ color: 'rgba(33,150,243,0.5)', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(inds.bb.upper);
      chart.addLineSeries({ color: 'rgba(33,150,243,0.8)', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, title: 'BB' }).setData(inds.bb.mid);
      chart.addLineSeries({ color: 'rgba(33,150,243,0.5)', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(inds.bb.lower);
    }
    if (inds.supertrend?.data) {
      const bull = [], bear = [];
      inds.supertrend.data.forEach(p => {
        if (p.dir === 1) { bull.push({ time: p.time, value: p.value }); bear.push({ time: p.time, value: NaN }); }
        else { bear.push({ time: p.time, value: p.value }); bull.push({ time: p.time, value: NaN }); }
      });
      chart.addLineSeries({ color: '#26a65b', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, title: 'ST' }).setData(bull);
      chart.addLineSeries({ color: '#ea3943', lineWidth: 2, priceLineVisible: false, lastValueVisible: false }).setData(bear);
    }
    if (inds.rsi?.data) {
      const rsiS = chart.addLineSeries({ color: '#7c3aed', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true, priceScaleId: 'rsi' });
      chart.priceScale('rsi').applyOptions({ scaleMargins: { top: 0.8, bottom: 0.02 } });
      rsiS.setData(inds.rsi.data);
    }

    // RSI Divergence + MSS signals
    if (inds.rsi_div_mss?.signals?.length) {
      const ohlc = data.candles;
      const markers = inds.rsi_div_mss.signals.map(s => ({
        time: s.time,
        position: s.type === 'buy' ? 'belowBar' : 'aboveBar',
        color: s.type === 'buy' ? '#26a65b' : '#ea3943',
        shape: s.type === 'buy' ? 'arrowUp' : 'arrowDown',
        text: s.type === 'buy' ? 'BUY (MSS)' : 'SELL (MSS)',
      }));
      markers.sort((a, b) => a.time - b.time);
      cs.setMarkers(markers);
      // SL and TP lines for each signal
      const ext = Math.min(30, Math.max(15, ohlc.length / 10 | 0));
      inds.rsi_div_mss.signals.forEach(s => {
        if (s.sl) {
          const sl = chart.addLineSeries({ color: '#ea3943', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, title: 'SL' });
          sl.setData([{ time: s.time, value: s.sl }, { time: s.time + ext * 3600, value: s.sl }]);
        }
        if (s.tp1) {
          const tp = chart.addLineSeries({ color: '#26a65b', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, title: 'TP1' });
          tp.setData([{ time: s.time, value: s.tp1 }, { time: s.time + ext * 3600, value: s.tp1 }]);
        }
      });
    }

    chart.timeScale().fitContent();
    setTimeout(() => { const ts = chart.timeScale(); const r = ts.getVisibleLogicalRange(); if (r) { const ns = (r.to - r.from) * Math.pow(0.6, 5); ts.setVisibleLogicalRange({ from: r.to - ns, to: r.to }); } }, 50);

    const ro = new ResizeObserver(() => { if (el.clientWidth) chart.applyOptions({ width: el.clientWidth }); });
    ro.observe(el);
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; };
  }, [data]);

  const last = data?.candles?.[data.candles.length - 1];
  const first = data?.candles?.[0];
  const chg = last && first ? ((last.c - first.o) / first.o * 100) : 0;
  const trend = data?.trend || 'Ranging';
  const trendCls = trend === 'Bullish' ? '#26a65b' : trend === 'Bearish' ? '#ea3943' : '#f59e0b';
  const srZones = data?.sr_zones || [];
  const swings = data?.swings || [];

  return (
    <div style={{ background: '#131722', minHeight: '100vh', color: '#d1d4dc', fontFamily: "'Inter',-apple-system,sans-serif", fontSize: 12 }}>
      {/* Top bar */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '6px 12px', background: '#1e222d', borderBottom: '1px solid #2a2e39', flexWrap: 'wrap' }}>
        <select value={symbol} onChange={e => setSymbol(e.target.value)} style={{ background: '#2a2e39', border: '1px solid #363a45', color: '#d1d4dc', padding: '4px 10px', borderRadius: 4, fontSize: '.8rem' }}>
          {SYMBOLS.map(s => <option key={s}>{s}</option>)}
        </select>
        <div style={{ display: 'flex', gap: 1, background: '#2a2e39', borderRadius: 4, overflow: 'hidden' }}>
          {TIMEFRAMES.map(t => (
            <button key={t} onClick={() => setTf(t)} style={{ padding: '4px 12px', border: 'none', fontSize: '.75rem', fontWeight: 600, cursor: 'pointer', background: t === tf ? '#2962ff' : 'transparent', color: t === tf ? '#fff' : '#787b86' }}>{TF_LABELS[t]}</button>
          ))}
        </div>
        {last && <span style={{ fontWeight: 800, fontSize: '.95rem', marginLeft: 10 }}>{last.c.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>}
        {last && <span style={{ fontSize: '.8rem', fontWeight: 600, color: chg >= 0 ? '#26a65b' : '#ea3943', marginLeft: 4 }}>{chg >= 0 ? '+' : ''}{chg.toFixed(2)}%</span>}
        <span style={{ background: trendCls + '22', color: trendCls, padding: '2px 8px', borderRadius: 3, fontSize: '.72rem', fontWeight: 700, marginLeft: 4 }}>{trend === 'Bullish' ? '▲' : trend === 'Bearish' ? '▼' : '◆'} {trend}</span>
        <div style={{ display: 'flex', gap: 2, marginLeft: 8 }}>
          {IND_LIST.map(ind => (
            <button key={ind.key} onClick={() => toggleInd(ind.key)} style={{ padding: '3px 8px', border: `1px solid ${activeInds.has(ind.key) ? ind.color : '#363a45'}`, borderRadius: 3, fontSize: '.7rem', fontWeight: 600, cursor: 'pointer', background: activeInds.has(ind.key) ? ind.color + '33' : 'transparent', color: activeInds.has(ind.key) ? ind.color : '#787b86' }}>{ind.label}</button>
          ))}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', height: 'calc(100vh - 95px)' }}>
        {/* Chart + bottom tabs */}
        <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
          <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
            {error && <div style={{ background: 'rgba(234,57,67,0.1)', border: '1px solid rgba(234,57,67,0.3)', borderRadius: 6, padding: 16, color: '#ea3943', margin: 12 }}>{error}</div>}
            {!error && !data?.candles && <div style={{ color: '#787b86', padding: 16 }}>Loading...</div>}
            <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
          </div>

          {/* Bottom tabs */}
          <div style={{ display: 'flex', borderTop: '1px solid #2a2e39', background: '#1e222d' }}>
            {[['positions', 'Positions'], ['sr', 'S/R Levels'], ['swings', 'Swing Points']].map(([k, l]) => (
              <div key={k} onClick={() => setTab(k)} style={{ padding: '6px 14px', fontSize: '.75rem', fontWeight: 600, color: tab === k ? '#2962ff' : '#787b86', borderBottom: tab === k ? '2px solid #2962ff' : '2px solid transparent', cursor: 'pointer' }}>{l}</div>
            ))}
          </div>
          <div style={{ background: '#1e222d', maxHeight: 160, overflowY: 'auto', fontSize: '.78rem' }}>
            {tab === 'positions' && <div style={{ padding: 10, color: '#787b86' }}>No open positions. <a href="/option-chain" style={{ color: '#2962ff' }}>Go to Option Chain →</a></div>}
            {tab === 'sr' && (
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr>{['Type', 'Price', 'Strength'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 10px', color: '#787b86', fontSize: '.68rem', textTransform: 'uppercase', borderBottom: '1px solid #2a2e39', position: 'sticky', top: 0, background: '#1e222d' }}>{h}</th>)}</tr></thead>
                <tbody>{srZones.map((z, i) => (
                  <tr key={i}><td style={{ padding: '4px 10px', borderBottom: '1px solid #2a2e39' }}><span style={{ fontSize: '.63rem', fontWeight: 700, padding: '1px 6px', borderRadius: 3, background: z.type === 'support' ? 'rgba(38,166,91,.15)' : 'rgba(234,57,67,.15)', color: z.type === 'support' ? '#26a65b' : '#ea3943' }}>{z.type === 'support' ? 'S' : 'R'}</span></td><td style={{ padding: '4px 10px', fontWeight: 700, fontFamily: 'monospace', borderBottom: '1px solid #2a2e39' }}>{z.price?.toLocaleString()}</td><td style={{ padding: '4px 10px', color: '#787b86', borderBottom: '1px solid #2a2e39' }}>×{z.strength}</td></tr>
                ))}</tbody>
              </table>
            )}
            {tab === 'swings' && (
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr>{['Type', 'Price', 'Time'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 10px', color: '#787b86', fontSize: '.68rem', textTransform: 'uppercase', borderBottom: '1px solid #2a2e39', position: 'sticky', top: 0, background: '#1e222d' }}>{h}</th>)}</tr></thead>
                <tbody>{[...swings].reverse().map((s, i) => {
                  const bull = s.type === 'HH' || s.type === 'HL';
                  const dt = new Date(s.time * 1000);
                  return <tr key={i}><td style={{ padding: '4px 10px', borderBottom: '1px solid #2a2e39' }}><span style={{ fontSize: '.63rem', fontWeight: 700, padding: '1px 5px', borderRadius: 3, background: bull ? 'rgba(38,166,91,.15)' : 'rgba(234,57,67,.15)', color: bull ? '#26a65b' : '#ea3943' }}>{s.type}</span></td><td style={{ padding: '4px 10px', fontWeight: 700, fontFamily: 'monospace', borderBottom: '1px solid #2a2e39' }}>{s.price?.toLocaleString()}</td><td style={{ padding: '4px 10px', color: '#787b86', borderBottom: '1px solid #2a2e39' }}>{dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })} {dt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}</td></tr>;
                })}</tbody>
              </table>
            )}
          </div>
        </div>

        {/* Right side panel */}
        <div style={{ display: 'flex', flexDirection: 'column', overflowY: 'auto', background: '#1e222d', borderLeft: '1px solid #2a2e39' }}>
          {/* Signal */}
          <div style={{ padding: '10px 12px', borderBottom: '1px solid #2a2e39' }}>
            <div style={{ fontSize: '.72rem', fontWeight: 700, marginBottom: 6, color: '#787b86', textTransform: 'uppercase', letterSpacing: '.4px' }}>🧠 Signal</div>
            <div style={{ padding: '8px 10px', borderRadius: 5, fontSize: '.8rem', fontWeight: 600, lineHeight: 1.4, background: trendCls + '15', border: `1px solid ${trendCls}33`, color: trendCls }}>
              {data?.signal || 'Loading...'}
            </div>
          </div>

          {/* Stats */}
          <div style={{ padding: '10px 12px', borderBottom: '1px solid #2a2e39' }}>
            <div style={{ fontSize: '.72rem', fontWeight: 700, marginBottom: 6, color: '#787b86', textTransform: 'uppercase', letterSpacing: '.4px' }}>📊 Stats</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4 }}>
              {last && first && [
                ['Open', first.o], ['Close', last.c],
                ['High', Math.max(...(data?.candles || []).map(c => c.h))],
                ['Low', Math.min(...(data?.candles || []).map(c => c.l))],
                ['Swings', swings.length], ['S/R Zones', srZones.length],
              ].map(([k, v]) => (
                <div key={k} style={{ background: '#131722', borderRadius: 4, padding: 6, textAlign: 'center' }}>
                  <div style={{ fontSize: '.6rem', color: '#787b86', textTransform: 'uppercase', letterSpacing: '.3px' }}>{k}</div>
                  <div style={{ fontSize: '.85rem', fontWeight: 800, marginTop: 1, color: k === 'High' ? '#26a65b' : k === 'Low' ? '#ea3943' : '#d1d4dc' }}>{typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Key Levels */}
          <div style={{ padding: '10px 12px', borderBottom: '1px solid #2a2e39' }}>
            <div style={{ fontSize: '.72rem', fontWeight: 700, marginBottom: 6, color: '#787b86', textTransform: 'uppercase', letterSpacing: '.4px' }}>🎯 Key Levels</div>
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {srZones.length ? srZones.map((z, i) => (
                <li key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '3px 0', borderBottom: '1px solid #2a2e39', fontSize: '.76rem' }}>
                  <span style={{ fontSize: '.63rem', fontWeight: 700, padding: '1px 6px', borderRadius: 3, background: z.type === 'support' ? 'rgba(38,166,91,.15)' : 'rgba(234,57,67,.15)', color: z.type === 'support' ? '#26a65b' : '#ea3943' }}>{z.type === 'support' ? 'S' : 'R'}</span>
                  <span style={{ fontWeight: 700, fontFamily: 'monospace', fontSize: '.78rem' }}>{z.price?.toLocaleString()}</span>
                  <span style={{ color: '#787b86', fontSize: '.63rem' }}>×{z.strength}</span>
                </li>
              )) : <li style={{ color: '#787b86' }}>—</li>}
            </ul>
          </div>

          {/* Structure */}
          <div style={{ padding: '10px 12px' }}>
            <div style={{ fontSize: '.72rem', fontWeight: 700, marginBottom: 6, color: '#787b86', textTransform: 'uppercase', letterSpacing: '.4px' }}>📐 Structure</div>
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {swings.length ? [...swings].slice(-8).reverse().map((s, i) => {
                const bull = s.type === 'HH' || s.type === 'HL';
                return (
                  <li key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '3px 0', borderBottom: '1px solid #2a2e39', fontSize: '.76rem' }}>
                    <span style={{ fontSize: '.63rem', fontWeight: 700, padding: '1px 5px', borderRadius: 3, background: bull ? 'rgba(38,166,91,.15)' : 'rgba(234,57,67,.15)', color: bull ? '#26a65b' : '#ea3943' }}>{s.type}</span>
                    <span style={{ fontWeight: 700, fontFamily: 'monospace', fontSize: '.78rem' }}>{s.price?.toLocaleString()}</span>
                  </li>
                );
              }) : <li style={{ color: '#787b86' }}>—</li>}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
