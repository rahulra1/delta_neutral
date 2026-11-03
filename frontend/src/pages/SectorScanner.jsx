import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../api';

const REFRESH_MS = 60000;
const SIZING_KEY = 'sectorScannerSizing';
const DEFAULT_SIZING = { capital: 100000, riskPct: 0.5, rr: 1.5, leverage: 1 };

const CHECKS = [
  { key: 'rel_strength', label: 'RS', title: 'Stronger (long) / weaker (short) than its sector today' },
  { key: 'vwap', label: 'VWAP', title: 'Price above VWAP (long) / below VWAP (short)' },
  { key: 'orb', label: 'ORB', title: 'Broke the 09:15-09:30 opening range high (long) / low (short)' },
  { key: 'rvol', label: 'RVOL', title: 'Volume ≥ 1.5× its average up to this time on the prior sessions' },
  { key: 'oi', label: 'OI', title: 'F&O OI confirms: long build-up / short covering (long), short build-up / long unwinding (short)' },
];

const pctColor = v => (v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--text)');
const fmtPct = v => (v == null ? '–' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`);
const fmtNum = (v, d = 2) => (v == null ? '–' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: d }));
const fmtInr = v => (v == null ? '–' : `₹${Math.round(v).toLocaleString('en-IN')}`);

// Heat-map tint: stronger colour for bigger moves (caps at ±3%)
function heat(p) {
  const a = Math.min(Math.abs(p) / 3, 1) * 0.35 + 0.05;
  return p >= 0 ? `rgba(16,185,129,${a})` : `rgba(239,68,68,${a})`;
}

function loadSizing() {
  try {
    return { ...DEFAULT_SIZING, ...JSON.parse(localStorage.getItem(SIZING_KEY) || '{}') };
  } catch {
    return DEFAULT_SIZING;
  }
}

// Position size from capital, % risk per trade, reward:risk and leverage cap
function sizePlan(plan, sizing) {
  if (!plan) return null;
  const perShare = Math.abs(plan.entry - plan.stop);
  if (!perShare) return null;
  const riskBudget = (sizing.capital * sizing.riskPct) / 100;
  const maxByCapital = Math.floor((sizing.capital * sizing.leverage) / plan.entry);
  const qty = Math.max(0, Math.min(Math.floor(riskBudget / perShare), maxByCapital));
  const sign = plan.side === 'LONG' ? 1 : -1;
  return {
    qty,
    target: plan.entry + sign * sizing.rr * perShare,
    risk: qty * perShare,
    reward: qty * perShare * sizing.rr,
    exposure: qty * plan.entry,
    capped: qty === maxByCapital && maxByCapital < Math.floor(riskBudget / perShare),
  };
}

// News grade = tier (A strong / B moderate / C weak) + direction (▲ good / ▼ bad for the stock)
const DIR = { pos: { arrow: '▲', cls: 'badge badge-green', word: 'positive' }, neg: { arrow: '▼', cls: 'badge badge-red', word: 'negative' },
  mixed: { arrow: '◆', cls: 'badge badge-yellow', word: 'mixed' } };
const TIER_WORD = { A: 'A strong', B: 'B moderate', C: 'C weak' };
const gradeCls = g => (g.direction ? DIR[g.direction].cls : 'badge');
const gradeLabel = g => `${g.tier}${g.direction ? ` ${DIR[g.direction].arrow}` : ''}${g.category ? ` · ${g.category}` : ''}`;
const ist = ts => new Date(ts * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
const ago = ts => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};

function NewsItem({ x }) {
  return (
    <div style={{ fontSize: '.78rem', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
      {x.tier && <span className={gradeCls(x)} style={{ fontSize: '.6rem', padding: '1px 6px', marginRight: 6 }} title={`${TIER_WORD[x.tier]}${x.direction ? ', ' + DIR[x.direction].word : ''}`}>{gradeLabel(x)}</span>}
      {x.kind === 'filing' && <span className="badge" style={{ fontSize: '.58rem', padding: '1px 5px', marginRight: 4, background: 'var(--bg)', border: '1px solid var(--border)' }}>🏛 NSE</span>}
      {x.symbols?.map(t => <span key={t} className="badge" style={{ fontSize: '.58rem', padding: '1px 5px', marginRight: 4, background: 'var(--bg)', border: '1px solid var(--border)' }}>{t}</span>)}
      {x.link ? <a href={x.link} target="_blank" rel="noreferrer" style={{ fontWeight: 600 }}>{x.title}</a> : <b>{x.title}</b>}
      {x.kind === 'filing' && x.summary && <div style={{ fontSize: '.7rem', color: 'var(--text)', marginTop: 2 }}>{x.summary}</div>}
      <div style={{ color: 'var(--muted)', fontSize: '.66rem' }}>{x.source} · {ago(x.ts)} · {ist(x.ts)} IST</div>
    </div>
  );
}

function StockNews({ symbol, name, onClose }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const boxRef = React.useRef(null);
  useEffect(() => { boxRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, [symbol]);
  useEffect(() => {
    setD(null); setErr(null);
    api.get('/sector-scanner/stock-news', { params: { symbol, name } }).then(r => setD(r.data)).catch(e => setErr(e.response?.data?.error || e.message));
  }, [symbol, name]);
  return (
    <div ref={boxRef} className="card" style={{ border: '1.5px solid var(--purple)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <div style={{ fontWeight: 800 }}>📰 {symbol} — headlines & NSE filings (last 3 days)
          {d && d.news_tier && <span className={gradeCls({ direction: d.news_direction })} style={{ marginLeft: 8, fontSize: '.66rem' }}>best {d.news_tier} · ▲{d.news_pos} ▼{d.news_neg}</span>}</div>
        <button type="button" className="btn btn-outline" style={{ padding: '4px 12px', fontSize: '.74rem' }} onClick={onClose}>Close</button>
      </div>
      {err && <div style={{ color: 'var(--red)', fontSize: '.8rem' }}>Unavailable: {err}</div>}
      {!d && !err && <div style={{ color: 'var(--muted)', fontSize: '.8rem' }}>Loading…</div>}
      {d && d.items.length === 0 && <div style={{ color: 'var(--muted)', fontSize: '.8rem' }}>No headline or NSE filing for {symbol} in the last 3 days.</div>}
      {d && d.items.map((x, i) => <NewsItem key={i} x={x} />)}
    </div>
  );
}

function CompaniesInNews({ list, onOpen }) {
  const [showAll, setShowAll] = useState(false);
  if (!list?.length) return null;
  const rows = showAll ? list : list.slice(0, 15);
  const th = { textAlign: 'left', padding: '6px', fontSize: '.64rem', color: 'var(--muted)', fontWeight: 700, borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap' };
  const td = { padding: '6px', fontSize: '.76rem', borderBottom: '1px solid var(--border)', verticalAlign: 'top' };
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 4 }}>🏢 Companies in news <span style={{ color: 'var(--muted)', fontWeight: 500, fontSize: '.72rem' }}>· ranked by strongest news, then number of A/B items, then latest</span></div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr>{['#', 'Company', 'Grade', '▲ / ▼', 'Headlines', 'Filings', 'Latest', 'Top news'].map(h => <th key={h} scope="col" style={th}>{h}</th>)}</tr></thead>
          <tbody>{rows.map((c, i) => (
            <tr key={c.symbol} style={{ background: c.news_direction === 'pos' ? 'var(--light-green)' : c.news_direction === 'neg' ? 'var(--light-red)' : undefined }}>
              <td style={{ ...td, color: 'var(--muted)' }}>{i + 1}</td>
              <td style={td}>
                <button type="button" onClick={() => onOpen(c)} title="Show all headlines and filings" style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontFamily: 'inherit', textAlign: 'left' }}>
                  <div style={{ fontWeight: 700 }}>{c.symbol} <span style={{ fontSize: '.58rem', color: 'var(--purple)' }}>details ›</span></div>
                  <div style={{ fontSize: '.62rem', color: 'var(--muted)', maxWidth: 190, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.name}>
                    {c.name}{c.listing === 'sme' ? ' · SME' : c.listing === 'main' ? '' : ' · Nifty 500'}{c.industry ? ` · ${c.industry}` : ''}</div>
                </button>
              </td>
              <td style={td}>{c.news_tier ? <span className={gradeCls({ direction: c.news_direction })} style={{ fontSize: '.6rem', padding: '1px 6px' }}>{c.news_tier}{c.news_direction ? ` ${DIR[c.news_direction].arrow}` : ''}</span> : '–'}</td>
              <td style={td}><span style={{ color: 'var(--green)' }}>{c.news_pos}</span> / <span style={{ color: 'var(--red)' }}>{c.news_neg}</span></td>
              <td style={td}>{c.news_count}</td>
              <td style={td}>{c.filings_count}</td>
              <td style={{ ...td, whiteSpace: 'nowrap' }}>{ago(c.latest_ts)}</td>
              <td style={{ ...td, maxWidth: 420 }}>
                {c.top.tier && <span className={gradeCls(c.top)} style={{ fontSize: '.58rem', padding: '1px 5px', marginRight: 4 }}>{gradeLabel(c.top)}</span>}
                {c.top.link ? <a href={c.top.link} target="_blank" rel="noreferrer">{c.top.title}</a> : c.top.title}
                <span style={{ color: 'var(--muted)', fontSize: '.64rem' }}> · {c.top.source}</span>
              </td>
            </tr>))}</tbody>
        </table>
      </div>
      {list.length > 15 && <button type="button" className="btn btn-outline" style={{ padding: '4px 12px', fontSize: '.72rem', marginTop: 6 }} onClick={() => setShowAll(v => !v)}>{showAll ? 'Show top 15' : `Show all ${list.length}`}</button>}
    </div>
  );
}

function NewsBoard({ onOpen }) {
  const [d, setD] = useState(null);
  const [filter, setFilter] = useState('graded');
  const [sort, setSort] = useState('impact');
  useEffect(() => {
    const load = () => api.get('/sector-scanner/news', { params: { sort } }).then(r => setD(r.data)).catch(() => {});
    load();
    const t = setInterval(load, 120000);
    return () => clearInterval(t);
  }, [sort]);
  if (!d) return <div className="card" style={{ color: 'var(--muted)' }}>Loading Indian market news…</div>;
  const f = {
    graded: x => x.tier === 'A' || x.tier === 'B' || (x.kind === 'news' && x.symbols.length),
    pos: x => x.direction === 'pos', neg: x => x.direction === 'neg', filings: x => x.kind === 'filing' && x.tier !== 'C', all: () => true,
  }[filter];
  const items = d.items.filter(f);
  const btn = active => ({
    padding: '4px 10px', borderRadius: 6, fontSize: '.72rem', fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit',
    border: active ? '1.5px solid var(--accent)' : '1.5px solid var(--border)',
    background: active ? 'var(--accent)' : 'var(--bg)', color: active ? '#fff' : 'var(--text)' });
  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, flexWrap: 'wrap', gap: 8 }}>
        <div>
          <div style={{ fontWeight: 700 }}>📰 Stock news</div>
          <div style={{ fontSize: '.68rem', color: 'var(--muted)' }}>
            {d.sources.join(' · ')} · matched against {d.universe_size ? d.universe_size.toLocaleString('en-IN') : 'all'} NSE companies (mainboard + SME) · grade = A strong / B moderate / C weak with ▲ good · ▼ bad · ◆ mixed (keyword based — read the headline)
          </div>
        </div>
      </div>
      <CompaniesInNews list={d.companies} onOpen={onOpen} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6, flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', gap: 4 }} role="group" aria-label="Sort news">
          <span style={{ fontSize: '.7rem', color: 'var(--muted)', alignSelf: 'center', marginRight: 4 }}>Sort</span>
          <button type="button" style={btn(sort === 'impact')} aria-pressed={sort === 'impact'} onClick={() => setSort('impact')} title="Strongest news first (A > B > C, clear ▲/▼ before neutral, company news before market news), then latest">Top impact</button>
          <button type="button" style={btn(sort === 'latest')} aria-pressed={sort === 'latest'} onClick={() => setSort('latest')}>Latest</button>
        </div>
        <div style={{ display: 'flex', gap: 4 }} role="group" aria-label="News filter">
          {[['graded', 'Catalysts'], ['pos', '▲ Positive'], ['neg', '▼ Negative'], ['filings', '🏛 NSE filings'], ['all', 'All']].map(([k, l]) => (
            <button key={k} type="button" onClick={() => setFilter(k)} aria-pressed={filter === k} style={btn(filter === k)}>{l}</button>
          ))}
        </div>
      </div>
      {d.errors?.length > 0 && <div style={{ fontSize: '.7rem', color: 'var(--red)', marginBottom: 6 }}>Feed errors: {d.errors.join('; ')}</div>}
      <div style={{ maxHeight: 520, overflowY: 'auto' }}>
        {items.length ? items.map((x, i) => <NewsItem key={i} x={x} />) : <div style={{ color: 'var(--muted)', fontSize: '.8rem' }}>Nothing in this filter.</div>}
      </div>
    </div>
  );
}

const BIAS_STYLE = {
  BULLISH: 'badge badge-green',
  BEARISH: 'badge badge-red',
  MIXED: 'badge badge-yellow',
};

function SectorCard({ s, active, onClick }) {
  const total = s.advances + s.declines || 1;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      style={{
        textAlign: 'left', cursor: 'pointer', borderRadius: 10, padding: '10px 12px', fontFamily: 'inherit',
        background: heat(s.pchange), color: 'var(--text)',
        border: active ? '2px solid var(--purple)' : '1px solid var(--border)',
      }}
    >
      <div style={{ fontSize: '.72rem', fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={s.index}>
        {s.index.replace(/^NIFTY\s?/, '') || s.index}
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginTop: 2 }}>
        <span style={{ fontSize: '1.15rem', fontWeight: 800, color: pctColor(s.pchange) }}>{fmtPct(s.pchange)}</span>
        <span style={{ fontSize: '.66rem', color: 'var(--muted)' }} title="Change since today's open">open {fmtPct(s.from_open)}</span>
      </div>
      <div style={{ display: 'flex', height: 4, borderRadius: 2, overflow: 'hidden', margin: '6px 0 4px', background: 'var(--border)' }}
        title={`${s.advances} advancing / ${s.declines} declining`}>
        <div style={{ width: `${(s.advances / total) * 100}%`, background: 'var(--green)' }} />
        <div style={{ width: `${(s.declines / total) * 100}%`, background: 'var(--red)' }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '.66rem' }}>
        <span style={{ color: 'var(--muted)' }}>▲{s.advances} ▼{s.declines}</span>
        <span className={BIAS_STYLE[s.bias]} style={{ fontSize: '.6rem', padding: '1px 6px' }}>{s.bias}</span>
      </div>
    </button>
  );
}

function CheckDots({ checks }) {
  return (
    <div style={{ display: 'flex', gap: 3 }}>
      {CHECKS.map(c => {
        const v = checks[c.key];
        const bg = v === true ? 'var(--green)' : v === false ? 'var(--light-red)' : 'var(--bg)';
        const fg = v === true ? '#fff' : v === false ? 'var(--red)' : 'var(--muted)';
        const state = v === true ? 'pass' : v === false ? 'fail' : 'n/a';
        return (
          <span key={c.key} title={`${c.title} — ${state}`} aria-label={`${c.label} ${state}`}
            style={{ fontSize: '.58rem', fontWeight: 700, padding: '2px 4px', borderRadius: 3, background: bg, color: fg, border: '1px solid var(--border)' }}>
            {c.label}
          </span>
        );
      })}
    </div>
  );
}

function SetupBadge({ setup }) {
  if (!setup) return <span style={{ color: 'var(--muted)' }}>–</span>;
  const cls = setup === 'LONG' ? 'badge badge-green' : setup === 'SHORT' ? 'badge badge-red' : 'badge badge-yellow';
  const title = setup === 'LATE'
    ? 'Setup is valid but it is after the late-entry cut-off — not enough time left to work before the 15:15 square-off'
    : setup === 'WATCH' ? 'Two or more checks pass but not the core trio (RS + VWAP + RVOL)' : 'RS, VWAP and RVOL all confirm';
  return <span className={cls} title={title}>{setup}</span>;
}

function SizingInputs({ sizing, setSizing }) {
  const field = (key, label, step, hint) => (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: '.66rem', color: 'var(--muted)', fontWeight: 600 }}>
      {label}
      <input
        type="number" min="0" step={step} value={sizing[key]}
        onChange={e => setSizing(s => ({ ...s, [key]: Math.max(0, Number(e.target.value) || 0) }))}
        title={hint}
        style={{ width: 110, padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', fontFamily: 'inherit', fontSize: '.82rem', color: 'var(--text)' }}
      />
    </label>
  );
  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
      {field('capital', 'Capital (₹)', 1000, 'Total capital for this trade')}
      {field('riskPct', 'Risk / trade (%)', 0.1, 'Loss if the stop-loss is hit, as % of capital')}
      {field('rr', 'Reward : Risk', 0.1, 'Target distance as a multiple of the stop distance')}
      {field('leverage', 'Leverage cap (×)', 0.5, 'Max exposure = capital × this (1 = no margin)')}
    </div>
  );
}

function NewsCell({ r, onNews }) {
  const has = r.news_count || r.filings_count;
  return (
    <button type="button" onClick={() => onNews(r)} title="Show headlines and NSE filings"
      style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, fontFamily: 'inherit', textAlign: 'left' }}>
      {has ? (
        <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
          {r.news_tier && <span className={gradeCls({ direction: r.news_direction })} style={{ fontSize: '.58rem', padding: '1px 5px' }}
            title={`Best grade ${TIER_WORD[r.news_tier]} · ${r.news_pos} positive / ${r.news_neg} negative`}>{r.news_tier}{r.news_direction ? ` ${DIR[r.news_direction].arrow}` : ''}</span>}
          {r.news_count > 0 && <span className="badge" style={{ fontSize: '.58rem', padding: '1px 5px' }} title="Headlines naming the stock">📰 {r.news_count}</span>}
          {r.filings_count > 0 && <span className="badge" style={{ fontSize: '.58rem', padding: '1px 5px' }} title="NSE filings (routine paperwork excluded)">🏛 {r.filings_count}</span>}
        </div>
      ) : <span style={{ color: 'var(--muted)' }}>–</span>}
      <span style={{ fontSize: '.58rem', color: 'var(--purple)' }}>details ›</span>
    </button>
  );
}

function StocksTable({ data, sizing, onlySetups, onNews }) {
  const rows = onlySetups ? data.stocks.filter(r => r.setup) : data.stocks;
  if (!rows.length) return <div style={{ padding: 20, color: 'var(--muted)', fontSize: '.85rem' }}>No stocks match.</div>;
  const th = { padding: '8px 6px', fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700, textAlign: 'right', whiteSpace: 'nowrap', borderBottom: '1px solid var(--border)', position: 'sticky', top: 0, background: 'var(--card)' };
  const td = { padding: '7px 6px', fontSize: '.78rem', textAlign: 'right', whiteSpace: 'nowrap', borderBottom: '1px solid var(--border)' };
  return (
    <div style={{ overflowX: 'auto', maxHeight: 620 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th scope="col" style={{ ...th, textAlign: 'left' }}>Stock</th>
            <th scope="col" style={th}>LTP</th>
            <th scope="col" style={th}>Chg</th>
            <th scope="col" style={th} title="Stock % change minus sector % change">vs Sector</th>
            <th scope="col" style={th} title="Volume so far vs average up to the same time on prior sessions">RVOL</th>
            <th scope="col" style={th}>VWAP</th>
            <th scope="col" style={th} title="09:15-09:30 opening range">OR H / L</th>
            <th scope="col" style={th}>Value</th>
            <th scope="col" style={{ ...th, textAlign: 'left' }}>OI</th>
            <th scope="col" style={{ ...th, textAlign: 'left' }} title={`Bulk / block deals on ${data.deals_as_on || 'last session'}`}>Deals</th>
            <th scope="col" style={{ ...th, textAlign: 'left' }} title="Headlines + NSE filings, last 3 days">News</th>
            <th scope="col" style={{ ...th, textAlign: 'left' }}>Checks</th>
            <th scope="col" style={{ ...th, textAlign: 'center' }}>Setup</th>
            <th scope="col" style={th}>Qty</th>
            <th scope="col" style={th}>Entry</th>
            <th scope="col" style={th}>Stop</th>
            <th scope="col" style={th}>Target</th>
            <th scope="col" style={th}>Risk / Reward</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => {
            const sz = sizePlan(r.plan, sizing);
            const showPlan = sz && r.setup && r.setup !== 'WATCH';
            return (
              <tr key={r.symbol} style={{ background: r.setup === 'LONG' ? 'var(--light-green)' : r.setup === 'SHORT' ? 'var(--light-red)' : undefined }}>
                <td style={{ ...td, textAlign: 'left' }}>
                  <div style={{ fontWeight: 700 }}>{r.symbol}{r.fno && <span style={{ fontSize: '.58rem', color: 'var(--purple)', marginLeft: 4 }} title="F&O stock">F&O</span>}</div>
                  <div style={{ fontSize: '.62rem', color: 'var(--muted)', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis' }} title={r.name}>{r.name}</div>
                </td>
                <td style={td}>{fmtNum(r.ltp)}</td>
                <td style={{ ...td, color: pctColor(r.pchange), fontWeight: 700 }}>{fmtPct(r.pchange)}</td>
                <td style={{ ...td, color: pctColor(r.rel_to_sector) }}>{fmtPct(r.rel_to_sector)}</td>
                <td style={{ ...td, fontWeight: r.rvol >= data.params.rvol_hot ? 800 : 400, color: r.rvol >= data.params.rvol_hot ? 'var(--purple)' : 'var(--text)' }}>
                  {r.rvol == null ? '–' : `${r.rvol.toFixed(2)}×`}
                </td>
                <td style={{ ...td, color: r.vwap == null ? 'var(--muted)' : pctColor(r.ltp - r.vwap) }}>{fmtNum(r.vwap)}</td>
                <td style={{ ...td, fontSize: '.7rem' }}>{fmtNum(r.or_high)} / {fmtNum(r.or_low)}</td>
                <td style={td}>{fmtNum(r.value_cr, 0)} cr</td>
                <td style={{ ...td, textAlign: 'left', fontSize: '.7rem' }}>
                  {r.fno ? (
                    <span style={{ color: /Long build|Short cover/.test(r.buildup || '') ? 'var(--green)' : r.buildup ? 'var(--red)' : 'var(--muted)' }}>
                      {r.buildup || 'flat'} <span style={{ color: 'var(--muted)' }}>{fmtPct(r.oi_pct)}</span>
                    </span>
                  ) : <span style={{ color: 'var(--muted)' }}>–</span>}
                </td>
                <td style={{ ...td, textAlign: 'left', fontSize: '.68rem' }}>
                  {r.deals.length ? (
                    <span title={r.deals.map(d => `${d.kind} ${d.side} ${fmtNum(d.qty, 0)} @ ${d.price} — ${d.client}`).join('\n')}
                      className={r.deal_bias === 'BUY' ? 'badge badge-green' : 'badge badge-red'} style={{ fontSize: '.6rem', padding: '1px 6px' }}>
                      {r.deals.length} {r.deal_bias || 'deal'}
                    </span>
                  ) : <span style={{ color: 'var(--muted)' }}>–</span>}
                </td>
                <td style={{ ...td, textAlign: 'left' }}><NewsCell r={r} onNews={onNews} /></td>
                <td style={{ ...td, textAlign: 'left' }}><CheckDots checks={r.checks} /></td>
                <td style={{ ...td, textAlign: 'center' }}><SetupBadge setup={r.setup} /></td>
                {showPlan ? (
                  <>
                    <td style={{ ...td, fontWeight: 700 }} title={sz.capped ? 'Capped by capital × leverage' : undefined}>{sz.qty}{sz.capped ? '*' : ''}</td>
                    <td style={td}>{fmtNum(r.plan.entry)}</td>
                    <td style={{ ...td, color: 'var(--red)' }} title={`${r.plan.stop_pct}% — ${r.plan.stop_basis}`}>{fmtNum(r.plan.stop)}</td>
                    <td style={{ ...td, color: 'var(--green)' }}>{fmtNum(sz.target)}</td>
                    <td style={td} title={`Exposure ${fmtInr(sz.exposure)}`}>
                      <span style={{ color: 'var(--red)' }}>-{fmtInr(sz.risk)}</span> / <span style={{ color: 'var(--green)' }}>+{fmtInr(sz.reward)}</span>
                    </td>
                  </>
                ) : (
                  <td style={{ ...td, color: 'var(--muted)', textAlign: 'center' }} colSpan={5}>–</td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function SectorScanner() {
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState(null);
  const [group, setGroup] = useState('Sectoral');
  const [selected, setSelected] = useState(null);
  const [stocks, setStocks] = useState(null);
  const [stocksErr, setStocksErr] = useState(null);
  const [loadingStocks, setLoadingStocks] = useState(false);
  const [onlySetups, setOnlySetups] = useState(false);
  const [sizing, setSizing] = useState(loadSizing);
  const [newsFor, setNewsFor] = useState(null);
  const [boardNews, setBoardNews] = useState(null);

  useEffect(() => { localStorage.setItem(SIZING_KEY, JSON.stringify(sizing)); }, [sizing]);

  const loadOverview = useCallback(() => {
    api.get('/sector-scanner')
      .then(r => { setOverview(r.data); setError(null); })
      .catch(e => setError(e.response?.data?.error || e.message));
  }, []);

  const loadStocks = useCallback((index, quiet = false) => {
    if (!index) return;
    if (!quiet) { setLoadingStocks(true); setStocks(null); }
    api.get('/sector-scanner/stocks', { params: { index } })
      .then(r => { setStocks(r.data); setStocksErr(null); })
      .catch(e => setStocksErr(e.response?.data?.error || e.message))
      .finally(() => setLoadingStocks(false));
  }, []);

  useEffect(() => {
    loadOverview();
    const t = setInterval(loadOverview, REFRESH_MS);
    return () => clearInterval(t);
  }, [loadOverview]);

  useEffect(() => {
    if (!selected) return undefined;
    loadStocks(selected);
    const t = setInterval(() => loadStocks(selected, true), REFRESH_MS);
    return () => clearInterval(t);
  }, [selected, loadStocks]);

  const sectors = useMemo(() => (overview?.sectors || []).filter(s => s.group === group), [overview, group]);

  // Default to the strongest-moving sector (by absolute % change) once data arrives
  useEffect(() => {
    if (!selected && sectors.length) {
      const top = [...sectors].sort((a, b) => Math.abs(b.pchange) - Math.abs(a.pchange))[0];
      setSelected(top.index);
    }
  }, [sectors, selected]);

  const green = sectors.filter(s => s.pchange > 0).length;
  const mkt = overview?.market;

  return (
    <div className="container" style={{ maxWidth: 1400 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}>
        <div>
          <h1 className="page-title" style={{ marginBottom: 4 }}>🧭 Sector Scanner</h1>
          <div style={{ fontSize: '.76rem', color: 'var(--muted)' }}>
            NSE sectors ranked by today's move → stocks with signs of big-player buying / selling · auto-refresh 60s
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: '.76rem' }}>
          {mkt && (
            <span className={mkt.status === 'Open' ? 'badge badge-green' : 'badge badge-yellow'}>
              Market {mkt.status} · {mkt.trade_date}
            </span>
          )}
          <button type="button" className="btn btn-outline" style={{ padding: '6px 14px', fontSize: '.76rem' }}
            onClick={() => { loadOverview(); loadStocks(selected, true); }}>↻ Refresh</button>
        </div>
      </div>

      {error && <div className="card" style={{ color: 'var(--red)' }}>Could not load NSE data: {error}</div>}

      {overview && (
        <>
          {/* Market context */}
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
            {overview.broad.map(b => (
              <div key={b.index} className="card" style={{ padding: '8px 14px', marginBottom: 0 }}>
                <div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700 }}>{b.index}</div>
                <div style={{ fontWeight: 800 }}>
                  {fmtNum(b.last)} <span style={{ fontSize: '.78rem', color: b.index === 'INDIA VIX' ? 'var(--text)' : pctColor(b.pchange) }}>{fmtPct(b.pchange)}</span>
                </div>
              </div>
            ))}
            {overview.fii_dii.map(f => (
              <div key={f.category} className="card" style={{ padding: '8px 14px', marginBottom: 0 }} title={`Buy ₹${fmtNum(f.buy, 0)} cr / Sell ₹${fmtNum(f.sell, 0)} cr (cash market)`}>
                <div style={{ fontSize: '.66rem', color: 'var(--muted)', fontWeight: 700 }}>{f.category} net · {f.date}</div>
                <div style={{ fontWeight: 800, color: pctColor(f.net) }}>{f.net > 0 ? '+' : ''}₹{fmtNum(f.net, 0)} cr</div>
              </div>
            ))}
          </div>

          {/* Sector heat-map */}
          <div className="card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', gap: 8 }}>
              <div>
                <div style={{ fontWeight: 700 }}>{group} indices <span style={{ color: 'var(--muted)', fontWeight: 500, fontSize: '.78rem' }}>· {green} green / {sectors.length - green} red</span></div>
                <div style={{ fontSize: '.68rem', color: 'var(--muted)' }}>
                  BULLISH / BEARISH = |change| ≥ {overview.thresholds.bias_pct}% and breadth ≥ {overview.thresholds.bias_breadth * 100}% net advancers / decliners · as of {overview.timestamp}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 4 }} role="group" aria-label="Index group">
                {['Sectoral', 'Thematic'].map(g => (
                  <button key={g} type="button" onClick={() => setGroup(g)} aria-pressed={group === g} style={{
                    padding: '4px 12px', borderRadius: 6, fontSize: '.75rem', fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit',
                    border: group === g ? '1.5px solid var(--accent)' : '1.5px solid var(--border)',
                    background: group === g ? 'var(--accent)' : 'var(--bg)', color: group === g ? '#fff' : 'var(--text)',
                  }}>{g}</button>
                ))}
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(165px, 1fr))', gap: 8 }}>
              {sectors.map(s => <SectorCard key={s.index} s={s} active={s.index === selected} onClick={() => { setSelected(s.index); setNewsFor(null); }} />)}
            </div>
          </div>

          {/* Selected sector drill-down */}
          {selected && (
            <div className="card">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 12, marginBottom: 12 }}>
                <div>
                  <div style={{ fontWeight: 800, fontSize: '1.05rem' }}>
                    {selected}{' '}
                    {stocks && stocks.index === selected && (
                      <>
                        <span style={{ color: pctColor(stocks.pchange) }}>{fmtPct(stocks.pchange)}</span>{' '}
                        <span className={BIAS_STYLE[stocks.bias]}>{stocks.bias}</span>
                      </>
                    )}
                  </div>
                  {stocks && stocks.index === selected && (
                    <div style={{ fontSize: '.7rem', color: 'var(--muted)', marginTop: 2 }}>
                      ▲{stocks.advances} ▼{stocks.declines} · {stocks.stocks.filter(r => ['LONG', 'SHORT', 'LATE'].includes(r.setup)).length} setups · NSE {stocks.timestamp}
                      {stocks.intraday_missing > 0 && ` · ${stocks.intraday_missing} stock(s) without intraday candles`}
                    </div>
                  )}
                </div>
                <SizingInputs sizing={sizing} setSizing={setSizing} />
              </div>

              {newsFor && <StockNews symbol={newsFor.symbol} name={newsFor.name} onClose={() => setNewsFor(null)} />}

              {stocks?.late && (
                <div className="badge badge-yellow" style={{ marginBottom: 10 }}>
                  After {stocks.params.late_entry} IST — new entries are marked LATE; square off intraday positions by 15:15.
                </div>
              )}

              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '.76rem', marginBottom: 8, cursor: 'pointer' }}>
                <input type="checkbox" checked={onlySetups} onChange={e => setOnlySetups(e.target.checked)} />
                Show only setups / watch-list
              </label>

              {loadingStocks && <div style={{ padding: 20, color: 'var(--muted)', fontSize: '.85rem' }}>Loading constituents and intraday candles…</div>}
              {stocksErr && !loadingStocks && <div style={{ padding: 12, color: 'var(--red)', fontSize: '.85rem' }}>Unavailable: {stocksErr}</div>}
              {stocks && stocks.index === selected && !loadingStocks && <StocksTable data={stocks} sizing={sizing} onlySetups={onlySetups} onNews={r => setNewsFor({ symbol: r.symbol, name: r.name })} />}

              <div style={{ fontSize: '.66rem', color: 'var(--muted)', marginTop: 12, lineHeight: 1.6 }}>
                Direction follows the sector bias (MIXED sectors use each stock's own direction). Setup = RS + VWAP + RVOL all pass; WATCH = 2+ checks.
                Stop = VWAP, clamped to {stocks?.params.stop_min_pct ?? 0.4}–{stocks?.params.stop_max_pct ?? 1.5}% from entry. Qty = capital × risk% ÷ stop distance, capped at capital × leverage.
                Big-player activity is inferred (volume, VWAP, OI, last session's bulk/block deals) — not observed directly. Not yet backtested; treat as a screener, not a signal.
              </div>
            </div>
          )}
        </>
      )}

      {boardNews && <StockNews symbol={boardNews.symbol} name={boardNews.name} onClose={() => setBoardNews(null)} />}
      {overview && <NewsBoard onOpen={c => setBoardNews({ symbol: c.symbol, name: c.name })} />}

      {!overview && !error && <div className="card" style={{ color: 'var(--muted)' }}>Loading NSE sectors…</div>}
    </div>
  );
}
