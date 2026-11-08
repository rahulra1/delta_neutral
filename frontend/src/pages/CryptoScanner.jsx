import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../api';

const REFRESH_MS = 60000;
const SIZING_KEY = 'cryptoScannerSizing';
const DEFAULT_SIZING = { capital: 10000, riskPct: 0.5, rr: 2, leverage: 5 };
const WEIGHTS_KEY = 'cryptoScannerWeights';
// Default weights (user's choice, Nov 2026): Twitter 25%, News 35%, Technical 40% = RSI 20% + EMA 7/20 cross 20%.
const DEFAULT_WEIGHTS = { news: 35, twitter: 25, rsi: 20, ema: 20, setup: 0 };
const COMPONENTS = [
  { key: 'news', label: 'News', title: 'Graded headlines (half-life 24h) + exchange listing notices, in the trade direction. No news = neutral.' },
  { key: 'twitter', label: 'Twitter / X', title: 'Engagement-weighted bullish vs bearish tweets (official X API; needs X_BEARER_TOKEN). Left out until configured.' },
  { key: 'rsi', label: 'RSI 14 (1h)', title: 'LONG: RSI 50–70 = +1, 70–80 = +0.3, >80 = −0.5, 45–50 = −0.3, <45 = −1 (SHORT mirrored)' },
  { key: 'ema', label: 'EMA 7/20 (1h)', title: 'EMA 7 above EMA 20 = +1 for LONG / −1 for SHORT (and the reverse)' },
  { key: 'setup', label: 'Scanner checks', title: 'RS / VWAP / RVOL / OI / funding checks passed, −1..+1' },
];

function loadWeights() {
  try { return { ...DEFAULT_WEIGHTS, ...JSON.parse(localStorage.getItem(WEIGHTS_KEY) || '{}') }; } catch { return DEFAULT_WEIGHTS; }
}

// Same formula as api/crypto_scanner.rating(): weighted mean of the −1..+1 components, missing ones drop out, 50 = neutral.
function rate(components, w) {
  let num = 0, den = 0;
  for (const c of COMPONENTS) {
    const v = components?.[c.key];
    if (v == null || !w[c.key]) continue;
    num += w[c.key] * v; den += w[c.key];
  }
  return den ? Math.round((50 + 50 * num / den) * 10) / 10 : null;
}
const rateColor = v => (v == null ? 'var(--muted)' : v >= 70 ? 'var(--green)' : v <= 40 ? 'var(--red)' : 'var(--text)');

function WeightInputs({ weights, setWeights, xConfigured }) {
  const total = COMPONENTS.reduce((s, c) => s + (Number(weights[c.key]) || 0), 0);
  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 8 }}>
      <div style={{ fontSize: '.7rem', fontWeight: 700, color: 'var(--muted)', alignSelf: 'center' }}>Rating weights (%)</div>
      {COMPONENTS.map(c => (
        <label key={c.key} title={c.title} style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: '.64rem', color: 'var(--muted)', fontWeight: 600 }}>
          {c.label}{c.key === 'twitter' && !xConfigured ? ' (not set up)' : ''}
          <input type="number" min="0" step="5" value={weights[c.key]}
            onChange={e => setWeights(w => ({ ...w, [c.key]: Math.max(0, Number(e.target.value) || 0) }))}
            style={{ width: 70, padding: '5px 7px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', fontFamily: 'inherit', fontSize: '.8rem', color: 'var(--text)' }} />
        </label>
      ))}
      <span style={{ fontSize: '.66rem', color: total === 100 ? 'var(--muted)' : 'var(--red)', alignSelf: 'center' }}>total {total}%{total !== 100 ? ' (weights are re-normalised)' : ''}</span>
      <button type="button" className="btn btn-outline" style={{ padding: '4px 10px', fontSize: '.7rem' }} onClick={() => setWeights(DEFAULT_WEIGHTS)}>Reset</button>
    </div>
  );
}

const CHECKS = [
  { key: 'rel_strength', label: 'RS', title: 'Stronger (long) / weaker (short) than its sector over 24h' },
  { key: 'vwap', label: 'VWAP', title: 'Price above (long) / below (short) the rolling 24h VWAP' },
  { key: 'rvol', label: 'RVOL', title: 'Last-24h volume ≥ 1.5× the average of the previous 7 days' },
  { key: 'oi', label: 'OI', title: 'Open interest confirms in the last 6h: long build-up / short covering (long), short build-up / long unwinding (short)' },
  { key: 'funding', label: 'FUND', title: 'Funding is not crowded on our side (longs not paying > 0.05%, shorts not paying > 0.05%)' },
];
const TIER = {
  A: { label: 'A strong', cls: 'badge badge-green' },
  B: { label: 'B moderate', cls: 'badge badge-yellow' },
  C: { label: 'C rumour', cls: 'badge' },
  R: { label: 'R unlock', cls: 'badge badge-red' },
};
const LISTING = { new: 'New listing', add: 'Market added', delist: 'Delisting', caution: 'Caution' };

const pctColor = v => (v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--text)');
const fmtPct = v => (v == null ? '–' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`);
const fmtUsd = v => (v == null ? '–' : `$${Math.round(v).toLocaleString('en-US')}`);
const fmtBig = v => (v == null ? '–' : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${(v / 1e3).toFixed(0)}K`);
// Prices from $100k BTC down to sub-cent memes: keep ~5 significant digits
const fmtPx = v => {
  if (v == null) return '–';
  const a = Math.abs(v);
  const d = a >= 1000 ? 1 : a >= 1 ? 3 : Math.min(8, 4 - Math.floor(Math.log10(a)));
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: Math.min(d, 2) });
};
const ago = ts => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};
const ist = ts => new Date(ts * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });

function heat(p) {
  const a = Math.min(Math.abs(p) / 6, 1) * 0.35 + 0.05;   // crypto: full colour at ±6%
  return p >= 0 ? `rgba(16,185,129,${a})` : `rgba(239,68,68,${a})`;
}

function loadSizing() {
  try { return { ...DEFAULT_SIZING, ...JSON.parse(localStorage.getItem(SIZING_KEY) || '{}') }; } catch { return DEFAULT_SIZING; }
}

// Delta perps trade in lots of `contract_value` coins. Risk-based lots, capped by capital × leverage.
function sizePlan(plan, cv, sizing, fee) {
  if (!plan || !cv) return null;
  const perLot = Math.abs(plan.entry - plan.stop) * cv;
  if (!perLot) return null;
  const riskBudget = (sizing.capital * sizing.riskPct) / 100;
  const maxLots = Math.floor((sizing.capital * sizing.leverage) / (plan.entry * cv));
  const byRisk = Math.floor(riskBudget / perLot);
  const lots = Math.max(0, Math.min(byRisk, maxLots));
  const sign = plan.side === 'LONG' ? 1 : -1;
  const notional = lots * plan.entry * cv;
  return {
    lots, notional, capped: lots === maxLots && maxLots < byRisk,
    target: plan.entry + sign * sizing.rr * Math.abs(plan.entry - plan.stop),
    risk: lots * perLot, reward: lots * perLot * sizing.rr,
    fees: notional * fee * 2, margin: notional / Math.max(sizing.leverage, 1),
  };
}

const BIAS = { BULLISH: 'badge badge-green', BEARISH: 'badge badge-red', MIXED: 'badge badge-yellow' };

function Spark({ data, up }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data), max = Math.max(...data), w = 70, h = 20;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - ((v - min) / (max - min || 1)) * h}`).join(' ');
  return <svg width={w} height={h} aria-hidden="true"><polyline points={pts} fill="none" stroke={up ? '#10b981' : '#ef4444'} strokeWidth="1.3" /></svg>;
}

function SectorCard({ s, active, onClick }) {
  const total = s.advances + s.declines || 1;
  return (
    <button type="button" onClick={onClick} aria-pressed={active}
      style={{ textAlign: 'left', cursor: 'pointer', borderRadius: 10, padding: '10px 12px', fontFamily: 'inherit', color: 'var(--text)',
        background: heat(s.pchange), border: active ? '2px solid var(--purple)' : '1px solid var(--border)' }}>
      <div style={{ fontSize: '.74rem', fontWeight: 700, display: 'flex', justifyContent: 'space-between' }}>
        <span>{s.sector}</span><span style={{ color: 'var(--muted)', fontWeight: 500 }}>{s.coins}</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginTop: 2 }}>
        <span style={{ fontSize: '1.15rem', fontWeight: 800, color: pctColor(s.pchange) }} title="Median 24h change of the sector's coins">{fmtPct(s.pchange)}</span>
        <span style={{ fontSize: '.64rem', color: 'var(--muted)' }} title="24h change weighted by turnover (dominated by the most-traded coins)">vol-wtd {fmtPct(s.tv_weighted)}</span>
      </div>
      <div style={{ display: 'flex', height: 4, borderRadius: 2, overflow: 'hidden', margin: '6px 0 4px', background: 'var(--border)' }} title={`${s.advances} up / ${s.declines} down`}>
        <div style={{ width: `${(s.advances / total) * 100}%`, background: 'var(--green)' }} />
        <div style={{ width: `${(s.declines / total) * 100}%`, background: 'var(--red)' }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '.64rem' }}>
        <span style={{ color: 'var(--muted)' }}>▲{s.advances} ▼{s.declines} · OI 6h {fmtPct(s.oi_chg_6h_pct)}</span>
        <span className={BIAS[s.bias]} style={{ fontSize: '.6rem', padding: '1px 6px' }}>{s.bias}</span>
      </div>
      <div style={{ fontSize: '.62rem', color: 'var(--muted)', marginTop: 3 }}>{s.leaders.join(' · ')}</div>
    </button>
  );
}

function CheckDots({ checks }) {
  return (
    <div style={{ display: 'flex', gap: 3 }}>
      {CHECKS.map(c => {
        const v = checks[c.key];
        const state = v === true ? 'pass' : v === false ? 'fail' : 'n/a';
        return (
          <span key={c.key} title={`${c.title} — ${state}`} aria-label={`${c.label} ${state}`}
            style={{ fontSize: '.56rem', fontWeight: 700, padding: '2px 4px', borderRadius: 3, border: '1px solid var(--border)',
              background: v === true ? 'var(--green)' : v === false ? 'var(--light-red)' : 'var(--bg)',
              color: v === true ? '#fff' : v === false ? 'var(--red)' : 'var(--muted)' }}>{c.label}</span>
        );
      })}
    </div>
  );
}

function NewsBadges({ r }) {
  const out = [];
  if (r.listing) out.push(<span key="l" className={r.listing.kind === 'delist' || r.listing.kind === 'caution' ? 'badge badge-red' : 'badge badge-green'} style={{ fontSize: '.58rem', padding: '1px 5px' }} title={`${r.listing.exchange}: ${r.listing.title}`}>🏦 {LISTING[r.listing.kind]}</span>);
  if (r.news_count) out.push(<span key="n" className={r.news_tier ? TIER[r.news_tier].cls : 'badge'} style={{ fontSize: '.58rem', padding: '1px 5px' }} title={`${r.news_count} headline(s) in the last 3 days${r.news_tier ? `, best grade ${TIER[r.news_tier].label}` : ''}`}>📰 {r.news_count}{r.news_tier ? ` ${r.news_tier}` : ''}</span>);
  return out.length ? <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>{out}</div> : <span style={{ color: 'var(--muted)' }}>–</span>;
}

function SizingInputs({ sizing, setSizing }) {
  const field = (key, label, step, hint) => (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: '.66rem', color: 'var(--muted)', fontWeight: 600 }}>
      {label}
      <input type="number" min="0" step={step} value={sizing[key]} title={hint}
        onChange={e => setSizing(s => ({ ...s, [key]: Math.max(0, Number(e.target.value) || 0) }))}
        style={{ width: 100, padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', fontFamily: 'inherit', fontSize: '.82rem', color: 'var(--text)' }} />
    </label>
  );
  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
      {field('capital', 'Capital ($)', 100, 'Account equity for this trade')}
      {field('riskPct', 'Risk / trade (%)', 0.1, 'Loss if the stop is hit, as % of capital')}
      {field('rr', 'Reward : Risk', 0.1, 'Target distance as a multiple of the stop distance')}
      {field('leverage', 'Leverage cap (×)', 1, 'Max notional = capital × this')}
    </div>
  );
}

function CoinNews({ ticker, onClose }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    setD(null); setErr(null);
    api.get('/crypto-scanner/coin-news', { params: { ticker } }).then(r => setD(r.data)).catch(e => setErr(e.response?.data?.error || e.message));
  }, [ticker]);
  return (
    <div className="card" style={{ border: '1.5px solid var(--purple)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ fontWeight: 800 }}>📰 {ticker} — news & events</div>
        <button type="button" className="btn btn-outline" style={{ padding: '4px 12px', fontSize: '.74rem' }} onClick={onClose}>Close</button>
      </div>
      {err && <div style={{ color: 'var(--red)', fontSize: '.8rem' }}>Unavailable: {err}</div>}
      {!d && !err && <div style={{ color: 'var(--muted)', fontSize: '.8rem' }}>Loading headlines and the CoinMarketCal calendar…</div>}
      {d && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 14 }}>
          <div>
            <div style={{ fontSize: '.72rem', fontWeight: 700, color: 'var(--muted)', marginBottom: 6 }}>HEADLINES (last 3 days)</div>
            {d.listing && (
              <div style={{ fontSize: '.78rem', marginBottom: 8 }}>
                <span className={d.listing.kind === 'delist' ? 'badge badge-red' : 'badge badge-green'}>🏦 {d.listing.exchange} · {LISTING[d.listing.kind]}</span>{' '}
                <a href={d.listing.link} target="_blank" rel="noreferrer">{d.listing.title}</a> <span style={{ color: 'var(--muted)' }}>{ist(d.listing.ts)} IST</span>
              </div>
            )}
            {d.headlines.length === 0 && <div style={{ fontSize: '.78rem', color: 'var(--muted)' }}>No headline naming {ticker} in the major crypto feeds.</div>}
            {d.news_score != null && <div style={{ fontSize: '.72rem', marginBottom: 6 }}>News score <b style={{ color: d.news_score > 0 ? 'var(--green)' : d.news_score < 0 ? 'var(--red)' : 'var(--text)' }}>{d.news_score > 0 ? '+' : ''}{d.news_score}</b> <span style={{ color: 'var(--muted)' }}>(−1 bearish … +1 bullish)</span></div>}
            {d.headlines.map((h, i) => (
              <div key={i} style={{ fontSize: '.78rem', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                {h.direction ? <span style={{ color: h.direction > 0 ? 'var(--green)' : 'var(--red)', fontWeight: 800, marginRight: 4 }} title={h.direction > 0 ? 'bullish' : 'bearish'}>{h.direction > 0 ? '▲' : '▼'}</span> : null}
                {h.tier && <span className={TIER[h.tier].cls} style={{ fontSize: '.6rem', padding: '1px 6px', marginRight: 6 }}>{TIER[h.tier].label} · {h.category}</span>}
                <a href={h.link} target="_blank" rel="noreferrer" style={{ fontWeight: 600 }}>{h.title}</a>
                <div style={{ color: 'var(--muted)', fontSize: '.68rem' }}>{h.source} · {ago(h.ts)} · {ist(h.ts)} IST</div>
              </div>
            ))}
          </div>
          <div>
            <div style={{ fontSize: '.72rem', fontWeight: 700, color: 'var(--muted)', marginBottom: 6 }}>TWITTER / X (last 7 days)</div>
            {!d.x_configured && <div style={{ fontSize: '.78rem', color: 'var(--muted)' }}>Not set up — add an X API bearer token with search access as X_BEARER_TOKEN in .env and restart.</div>}
            {d.x_configured && !d.x && <div style={{ fontSize: '.78rem', color: 'var(--muted)' }}>Unavailable right now (rate limit or API error).</div>}
            {d.x && (
              <>
                <div style={{ fontSize: '.74rem', marginBottom: 6 }}>Score <b style={{ color: d.x.score > 0 ? 'var(--green)' : d.x.score < 0 ? 'var(--red)' : 'var(--text)' }}>{d.x.score > 0 ? '+' : ''}{d.x.score}</b> · {d.x.tweets} tweets · {d.x.bullish} bullish / {d.x.bearish} bearish</div>
                {d.x.top.map((t, i) => (
                  <div key={i} style={{ fontSize: '.76rem', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                    <a href={`https://x.com/i/web/status/${t.id}`} target="_blank" rel="noreferrer">{t.text}</a>
                    <div style={{ color: 'var(--muted)', fontSize: '.66rem' }}>♥ {t.likes} · {t.created_at ? new Date(t.created_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) : ''} IST</div>
                  </div>
                ))}
              </>
            )}
          </div>
          <div>
            <div style={{ fontSize: '.72rem', fontWeight: 700, color: 'var(--muted)', marginBottom: 6 }}>CALENDAR (CoinMarketCal, last 7 days → next 30 days)</div>
            {d.events.length === 0 && <div style={{ fontSize: '.78rem', color: 'var(--muted)' }}>No dated events found.</div>}
            {d.events.map((e, i) => (
              <div key={i} style={{ fontSize: '.78rem', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                <b style={{ color: e.past ? 'var(--muted)' : 'var(--text)' }}>{e.date}</b>{e.precision !== 'day' ? ` (${e.precision})` : ''} {e.past ? <span style={{ color: 'var(--muted)' }}>(past)</span> : <span className="badge badge-green" style={{ fontSize: '.56rem', padding: '0 5px' }}>upcoming</span>} · <a href={e.url} target="_blank" rel="noreferrer">{e.title}</a>
                <div style={{ color: 'var(--muted)', fontSize: '.68rem' }}>{e.category} · impact {e.impact ?? '–'}/10</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function CoinsTable({ data, sizing, onlySetups, onNews, newsFor, weights, sortByRating }) {
  const filtered = (onlySetups ? data.stocks.filter(r => r.setup) : data.stocks).map(r => ({ ...r, _rating: rate(r.components, weights) }));
  const rows = sortByRating ? [...filtered].sort((a, b) => (b._rating ?? -1) - (a._rating ?? -1)) : filtered;
  if (!rows.length) return <div style={{ padding: 20, color: 'var(--muted)', fontSize: '.85rem' }}>No coins match.</div>;
  const p = data.params;
  const fmtC = v => (v == null ? 'n/a' : `${v > 0 ? '+' : ''}${v.toFixed(2)}`);
  const ratingTitle = r => COMPONENTS.map(c => `${c.label}: ${fmtC(r.components?.[c.key])} × ${weights[c.key]}%`).join('\n')
    + `\n= ${r._rating ?? '–'} / 100 (50 = neutral, in the ${r.direction} direction)`;
  const th = { padding: '8px 6px', fontSize: '.64rem', color: 'var(--muted)', fontWeight: 700, textAlign: 'right', whiteSpace: 'nowrap', borderBottom: '1px solid var(--border)', position: 'sticky', top: 0, background: 'var(--card)' };
  const td = { padding: '7px 6px', fontSize: '.76rem', textAlign: 'right', whiteSpace: 'nowrap', borderBottom: '1px solid var(--border)' };
  const fee = data.params.taker_fee;
  return (
    <div style={{ overflowX: 'auto', maxHeight: 640 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            {[['Coin', 'left'], ['Price'], ['24h'], ['4h'], ['1h'], ['vs Sector', 'right', 'Coin 24h % minus sector median'], ['48h'],
              ['RVOL', 'right', 'Last-24h volume ÷ 7-day average (1h: last hour ÷ average hour)'], ['VWAP 24h'], ['OI 6h', 'left'], ['Funding', 'right', '% per funding interval'],
              ['Turnover'], ['News', 'left'], ['Rating', 'center', 'Weighted rating 0–100 in the trade direction (hover a value for the breakdown)'],
              ['RSI', 'right', `RSI ${p.rsi_len ?? 14} on 1h closes`], ['EMA', 'center', `EMA ${p.ema_fast ?? 7}/${p.ema_slow ?? 20} on 1h — ▲ fast above slow, ★ = crossed within ${p.fresh_cross_bars ?? 3} bars`],
              ['X', 'center', p.x_configured ? `Twitter/X sentiment (top ${p.x_max_coins} setups/WATCH per sector)` : 'Twitter/X not set up — add X_BEARER_TOKEN'],
              ['Checks', 'left'], ['Setup', 'center'], ['Lots'], ['Entry'], ['Stop'], ['Target'], ['Risk / Reward']].map(([h, a, t]) => (
              <th key={h} scope="col" title={t} style={{ ...th, textAlign: a || 'right' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(r => {
            const sz = sizePlan(r.plan, r.contract_value, sizing, fee);
            const show = sz && (r.setup === 'LONG' || r.setup === 'SHORT');
            return (
              <tr key={r.symbol} style={{ background: newsFor === r.ticker ? 'rgba(99,102,241,.08)' : r.setup === 'LONG' ? 'var(--light-green)' : r.setup === 'SHORT' ? 'var(--light-red)' : undefined }}>
                <td style={{ ...td, textAlign: 'left' }}>
                  <div style={{ fontWeight: 700 }}>{r.ticker}{r.thin && <span style={{ fontSize: '.56rem', color: 'var(--red)', marginLeft: 4 }} title={`24h turnover under ${fmtBig(data.params.min_turnover)} — too thin for a setup`}>THIN</span>}</div>
                  <div style={{ fontSize: '.6rem', color: 'var(--muted)', maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis' }} title={`${r.name} · ${r.symbol} · ${r.contract_value} coin(s)/lot`}>{r.name}</div>
                </td>
                <td style={td}>{fmtPx(r.price)}</td>
                <td style={{ ...td, color: pctColor(r.pchange), fontWeight: 700 }}>{fmtPct(r.pchange)}</td>
                <td style={{ ...td, color: pctColor(r.chg_4h) }}>{fmtPct(r.chg_4h)}</td>
                <td style={{ ...td, color: pctColor(r.chg_1h) }}>{fmtPct(r.chg_1h)}</td>
                <td style={{ ...td, color: pctColor(r.rel_to_sector) }}>{fmtPct(r.rel_to_sector)}</td>
                <td style={td}><Spark data={r.spark} up={(r.spark?.at(-1) ?? 0) >= (r.spark?.[0] ?? 0)} /></td>
                <td style={{ ...td, fontWeight: r.rvol >= data.params.rvol_hot ? 800 : 400, color: r.rvol >= data.params.rvol_hot ? 'var(--purple)' : 'var(--text)' }}>
                  {r.rvol == null ? '–' : `${r.rvol.toFixed(2)}×`}<div style={{ fontSize: '.6rem', color: 'var(--muted)', fontWeight: 400 }}>1h {r.rvol_1h == null ? '–' : `${r.rvol_1h.toFixed(1)}×`}</div>
                </td>
                <td style={{ ...td, color: r.vwap == null ? 'var(--muted)' : pctColor(r.price - r.vwap) }}>{fmtPx(r.vwap)}</td>
                <td style={{ ...td, textAlign: 'left', fontSize: '.68rem' }}>
                  <span style={{ color: /Long build|Short cover/.test(r.buildup || '') ? 'var(--green)' : r.buildup ? 'var(--red)' : 'var(--muted)' }}>{r.buildup || 'flat'}</span>{' '}
                  <span style={{ color: 'var(--muted)' }}>{fmtPct(r.oi_chg_6h_pct)}</span>
                </td>
                <td style={{ ...td, color: Math.abs(r.funding) > data.params.funding_hot ? 'var(--red)' : 'var(--text)' }}>{r.funding.toFixed(4)}%</td>
                <td style={td} title={`Open interest ${fmtBig(r.oi_usd)}`}>{fmtBig(r.turnover_usd)}</td>
                <td style={{ ...td, textAlign: 'left' }}>
                  <button type="button" onClick={() => onNews(r.ticker)} title="Show headlines and upcoming events"
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'inherit' }}>
                    <NewsBadges r={r} /><span style={{ fontSize: '.6rem', color: 'var(--purple)' }}>details ›</span>
                  </button>
                </td>
                <td style={{ ...td, textAlign: 'center', fontWeight: 800, color: rateColor(r._rating) }} title={ratingTitle(r)}>
                  {r._rating ?? '–'}
                  <div style={{ fontSize: '.56rem', fontWeight: 500, color: 'var(--muted)' }}>news {fmtC(r.components?.news)}</div>
                </td>
                <td style={{ ...td, color: r.rsi == null ? 'var(--muted)' : r.rsi >= 70 ? 'var(--red)' : r.rsi <= 30 ? 'var(--green)' : 'var(--text)' }}>{r.rsi ?? '–'}</td>
                <td style={{ ...td, textAlign: 'center' }} title={r.ema_bars_since_cross != null ? `last cross ${r.ema_bars_since_cross} bar(s) ago` : 'no cross in the window'}>
                  {r.ema_state ? <span style={{ color: r.ema_state === 'BULL' ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{r.ema_state === 'BULL' ? '▲' : '▼'}{r.ema_fresh ? '★' : ''}</span> : '–'}
                </td>
                <td style={{ ...td, textAlign: 'center', fontSize: '.66rem' }}
                  title={r.x ? `${r.x.tweets} tweets · ${r.x.bullish} bullish / ${r.x.bearish} bearish` : (p.x_configured ? 'not fetched (only the top setups get X data)' : 'X API not set up')}>
                  {r.x ? <span style={{ color: r.x.score > 0 ? 'var(--green)' : r.x.score < 0 ? 'var(--red)' : 'var(--muted)', fontWeight: 700 }}>{fmtC(r.x.score)}</span> : <span style={{ color: 'var(--muted)' }}>–</span>}
                </td>
                <td style={{ ...td, textAlign: 'left' }}><CheckDots checks={r.checks} /></td>
                <td style={{ ...td, textAlign: 'center' }}>
                  {r.setup ? <span className={r.setup === 'LONG' ? 'badge badge-green' : r.setup === 'SHORT' ? 'badge badge-red' : 'badge badge-yellow'}
                    title={r.setup === 'WATCH' ? '3+ checks pass but not RS + VWAP + RVOL together (or the coin is too thin)' : 'RS, VWAP and RVOL all confirm'}>{r.setup}</span> : <span style={{ color: 'var(--muted)' }}>–</span>}
                </td>
                {show ? (
                  <>
                    <td style={{ ...td, fontWeight: 700 }} title={`${fmtUsd(sz.notional)} notional · margin ${fmtUsd(sz.margin)}${sz.capped ? ' · capped by capital × leverage' : ''}`}>{sz.lots}{sz.capped ? '*' : ''}</td>
                    <td style={td}>{fmtPx(r.plan.entry)}</td>
                    <td style={{ ...td, color: 'var(--red)' }} title={`${r.plan.stop_pct}% — ${r.plan.stop_basis}`}>{fmtPx(r.plan.stop)}</td>
                    <td style={{ ...td, color: 'var(--green)' }}>{fmtPx(sz.target)}</td>
                    <td style={td} title={`Est. fees ${fmtUsd(sz.fees)} (taker 0.05% + GST, both sides)`}>
                      <span style={{ color: 'var(--red)' }}>-{fmtUsd(sz.risk)}</span> / <span style={{ color: 'var(--green)' }}>+{fmtUsd(sz.reward)}</span>
                    </td>
                  </>
                ) : <td colSpan={5} style={{ ...td, textAlign: 'center', color: 'var(--muted)' }}>–</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function NewsBoard({ news }) {
  const [tierOnly, setTierOnly] = useState(true);
  if (!news) return <div className="card" style={{ color: 'var(--muted)' }}>Loading crypto news…</div>;
  const items = news.headlines.filter(h => !tierOnly || h.tier || h.tickers.length);
  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
        <div>
          <div style={{ fontWeight: 700 }}>📰 Crypto news</div>
          <div style={{ fontSize: '.68rem', color: 'var(--muted)' }}>CoinDesk · Cointelegraph · The Block · Decrypt · CryptoSlate + Upbit listing notices · graded A strong / B moderate / C rumour / R unlock (keyword based — read the headline)</div>
        </div>
        <label style={{ fontSize: '.74rem', display: 'inline-flex', gap: 6, alignItems: 'center', cursor: 'pointer' }}>
          <input type="checkbox" checked={tierOnly} onChange={e => setTierOnly(e.target.checked)} /> Only catalysts / coin-specific
        </label>
      </div>
      {news.errors?.length > 0 && <div style={{ fontSize: '.7rem', color: 'var(--red)', marginBottom: 6 }}>Feed errors: {news.errors.join('; ')}</div>}
      {news.listings.map((l, i) => (
        <div key={`l${i}`} style={{ fontSize: '.78rem', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
          <span className={l.kind === 'delist' || l.kind === 'caution' ? 'badge badge-red' : 'badge badge-green'} style={{ fontSize: '.6rem', padding: '1px 6px', marginRight: 6 }}>🏦 {l.exchange} · {LISTING[l.kind]}</span>
          <b>{l.tickers.join(', ')}</b> — <a href={l.link} target="_blank" rel="noreferrer">{l.title}</a>
          <span style={{ color: 'var(--muted)', fontSize: '.68rem' }}> · {ist(l.ts)} IST</span>
        </div>
      ))}
      <div style={{ maxHeight: 420, overflowY: 'auto' }}>
        {items.map((h, i) => (
          <div key={i} style={{ fontSize: '.78rem', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
            {h.tier && <span className={TIER[h.tier].cls} style={{ fontSize: '.6rem', padding: '1px 6px', marginRight: 6 }}>{TIER[h.tier].label}</span>}
            {h.tickers.map(t => <span key={t} className="badge" style={{ fontSize: '.6rem', padding: '1px 5px', marginRight: 4, background: 'var(--bg)', border: '1px solid var(--border)' }}>{t}</span>)}
            <a href={h.link} target="_blank" rel="noreferrer" style={{ fontWeight: 600 }}>{h.title}</a>
            <span style={{ color: 'var(--muted)', fontSize: '.68rem' }}> · {h.source} · {ago(h.ts)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function CryptoScanner() {
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [coins, setCoins] = useState(null);
  const [coinsErr, setCoinsErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [onlySetups, setOnlySetups] = useState(false);
  const [news, setNews] = useState(null);
  const [newsFor, setNewsFor] = useState(null);
  const [sizing, setSizing] = useState(loadSizing);
  const [weights, setWeights] = useState(loadWeights);
  const [sortByRating, setSortByRating] = useState(false);

  useEffect(() => { localStorage.setItem(SIZING_KEY, JSON.stringify(sizing)); }, [sizing]);
  useEffect(() => { localStorage.setItem(WEIGHTS_KEY, JSON.stringify(weights)); }, [weights]);

  const loadOverview = useCallback(() => {
    api.get('/crypto-scanner').then(r => { setOverview(r.data); setError(null); }).catch(e => setError(e.response?.data?.error || e.message));
    api.get('/crypto-scanner/news').then(r => setNews(r.data)).catch(() => {});
  }, []);

  const loadCoins = useCallback((sector, quiet = false) => {
    if (!sector) return;
    if (!quiet) { setLoading(true); setCoins(null); }
    api.get('/crypto-scanner/coins', { params: { sector } })
      .then(r => { setCoins(r.data); setCoinsErr(null); })
      .catch(e => setCoinsErr(e.response?.data?.error || e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { loadOverview(); const t = setInterval(loadOverview, REFRESH_MS); return () => clearInterval(t); }, [loadOverview]);
  useEffect(() => {
    if (!selected) return undefined;
    loadCoins(selected);
    const t = setInterval(() => loadCoins(selected, true), REFRESH_MS);
    return () => clearInterval(t);
  }, [selected, loadCoins]);

  const sectors = useMemo(() => overview?.sectors || [], [overview]);
  useEffect(() => {
    if (!selected && sectors.length) setSelected([...sectors].sort((a, b) => Math.abs(b.pchange) - Math.abs(a.pchange))[0].sector);
  }, [sectors, selected]);

  const m = overview?.market;
  return (
    <div className="container" style={{ maxWidth: 1500 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}>
        <div>
          <h1 className="page-title" style={{ marginBottom: 4 }}>🪙 Crypto Scanner</h1>
          <div style={{ fontSize: '.76rem', color: 'var(--muted)' }}>
            Delta Exchange perpetual futures · sectors by Delta's tags · coins with signs of big-player buying / selling + the news behind them · rolling 24h (crypto never closes) · auto-refresh 60s
          </div>
        </div>
        <button type="button" className="btn btn-outline" style={{ padding: '6px 14px', fontSize: '.76rem' }} onClick={() => { loadOverview(); loadCoins(selected, true); }}>↻ Refresh</button>
      </div>

      {error && <div className="card" style={{ color: 'var(--red)' }}>Could not load Delta data: {error}</div>}

      {overview && (
        <>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
            {overview.majors.map(c => (
              <div key={c.ticker} className="card" style={{ padding: '8px 14px', marginBottom: 0 }}>
                <div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700 }}>{c.ticker} perp · funding {c.funding.toFixed(4)}%</div>
                <div style={{ fontWeight: 800 }}>{fmtPx(c.price)} <span style={{ fontSize: '.78rem', color: pctColor(c.pchange) }}>{fmtPct(c.pchange)}</span></div>
              </div>
            ))}
            {m && (
              <div className="card" style={{ padding: '8px 14px', marginBottom: 0 }}>
                <div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700 }}>Market breadth · {m.coins} crypto perps</div>
                <div style={{ fontWeight: 800 }}>
                  <span style={{ color: 'var(--green)' }}>▲{m.advances}</span> / <span style={{ color: 'var(--red)' }}>▼{m.declines}</span>
                  <span style={{ fontSize: '.78rem', color: pctColor(m.median_pchange) }}> median {fmtPct(m.median_pchange)}</span>
                </div>
              </div>
            )}
            {m && (
              <div className="card" style={{ padding: '8px 14px', marginBottom: 0 }}>
                <div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700 }}>Delta 24h turnover / open interest</div>
                <div style={{ fontWeight: 800 }}>{fmtBig(m.turnover_usd)} / {fmtBig(m.oi_usd)}</div>
              </div>
            )}
            <div className="card" style={{ padding: '8px 14px', marginBottom: 0, fontSize: '.72rem' }}>
              <div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700 }}>Top movers 24h</div>
              <div><span style={{ color: 'var(--green)' }}>{overview.gainers.slice(0, 3).map(g => `${g.ticker} ${fmtPct(g.pchange)}`).join(' · ')}</span></div>
              <div><span style={{ color: 'var(--red)' }}>{overview.losers.slice(0, 3).map(g => `${g.ticker} ${fmtPct(g.pchange)}`).join(' · ')}</span></div>
            </div>
          </div>

          <div className="card">
            <div style={{ fontWeight: 700 }}>Crypto sectors <span style={{ color: 'var(--muted)', fontWeight: 500, fontSize: '.78rem' }}>· {sectors.filter(s => s.pchange > 0).length} green / {sectors.filter(s => s.pchange <= 0).length} red</span></div>
            <div style={{ fontSize: '.68rem', color: 'var(--muted)', marginBottom: 10 }}>
              Sector change = median 24h move of its coins. BULLISH / BEARISH = |median| ≥ {overview.thresholds.bias_pct}% and ≥ {overview.thresholds.bias_breadth * 100}% net coins moving the same way. A coin can be in several sectors (Delta tags). Tokenized stocks / metals excluded.
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 8 }}>
              {sectors.map(s => <SectorCard key={s.sector} s={s} active={s.sector === selected} onClick={() => { setSelected(s.sector); setNewsFor(null); }} />)}
            </div>
          </div>

          {newsFor && <CoinNews ticker={newsFor} onClose={() => setNewsFor(null)} />}

          {selected && (
            <div className="card">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 12, marginBottom: 10 }}>
                <div>
                  <div style={{ fontWeight: 800, fontSize: '1.05rem' }}>
                    {selected}{' '}
                    {coins && coins.sector === selected && <><span style={{ color: pctColor(coins.pchange) }}>{fmtPct(coins.pchange)}</span> <span className={BIAS[coins.bias]}>{coins.bias}</span></>}
                  </div>
                  {coins && coins.sector === selected && (
                    <div style={{ fontSize: '.7rem', color: 'var(--muted)', marginTop: 2 }}>
                      ▲{coins.advances} ▼{coins.declines} · {coins.stocks.filter(r => r.setup === 'LONG' || r.setup === 'SHORT').length} setups · {new Date(coins.timestamp).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST
                      {coins.intraday_missing > 0 && ` · ${coins.intraday_missing} coin(s) without candles`}
                    </div>
                  )}
                </div>
                <SizingInputs sizing={sizing} setSizing={setSizing} />
              </div>
              <WeightInputs weights={weights} setWeights={setWeights} xConfigured={coins?.params?.x_configured} />
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 8 }}>
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '.76rem', cursor: 'pointer' }}>
                  <input type="checkbox" checked={onlySetups} onChange={e => setOnlySetups(e.target.checked)} /> Show only setups / watch-list
                </label>
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '.76rem', cursor: 'pointer' }}>
                  <input type="checkbox" checked={sortByRating} onChange={e => setSortByRating(e.target.checked)} /> Sort by rating
                </label>
              </div>
              {loading && <div style={{ padding: 20, color: 'var(--muted)', fontSize: '.85rem' }}>Loading coins and 1h candles…</div>}
              {coinsErr && !loading && <div style={{ padding: 12, color: 'var(--red)', fontSize: '.85rem' }}>Unavailable: {coinsErr}</div>}
              {coins && coins.sector === selected && !loading && <CoinsTable data={coins} sizing={sizing} onlySetups={onlySetups} onNews={setNewsFor} newsFor={newsFor} weights={weights} sortByRating={sortByRating} />}
              <div style={{ fontSize: '.66rem', color: 'var(--muted)', marginTop: 12, lineHeight: 1.6 }}>
                Rating = weighted mean of News, Twitter/X, RSI {coins?.params.rsi_len ?? 14} and EMA {coins?.params.ema_fast ?? 7}/{coins?.params.ema_slow ?? 20} crossover (1h), each scored −1..+1 in the trade direction; 50 = neutral, 100 = everything agrees. A component with no data is left out and the other weights are re-normalised (no headline counts as neutral news).
                {!coins?.params.x_configured && ' Twitter/X is not set up (needs an X API bearer token with search access in X_BEARER_TOKEN), so it is left out of the rating for now.'}
                {' '}The rating is NOT backtested and does not change the LONG/SHORT setup or the Crypto Scanner Futures strategy.<br />
                Direction follows the sector bias (MIXED sectors use each coin's own 24h direction). Setup = RS + VWAP + RVOL all pass and 24h turnover ≥ {fmtBig(coins?.params.min_turnover ?? 250000)}; WATCH = 3+ of the 5 checks.
                Stop = rolling 24h VWAP clamped to {coins?.params.stop_min_pct ?? 0.75}–{coins?.params.stop_max_pct ?? 3}% from entry. Lots = capital × risk% ÷ (stop distance × coins per lot), capped at capital × leverage. Hover Lots for notional and margin, Risk/Reward for estimated fees.
                Big-player activity is inferred from volume, VWAP, open interest and funding — it is not observed directly. These rules are NOT backtested; treat this as a screener, not a signal.
              </div>
            </div>
          )}

          <NewsBoard news={news} />
        </>
      )}
      {!overview && !error && <div className="card" style={{ color: 'var(--muted)' }}>Loading Delta perpetuals…</div>}
    </div>
  );
}
