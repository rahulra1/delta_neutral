import React, { useState, useRef, useEffect } from 'react';

export default function OptionChainTable({ chain = [], spot = 0, sym = '$', lot = 1, isCrypto = false, legs = [], onAddLeg }) {
  const [hoveredRow, setHoveredRow] = useState(null);
  const ref = useRef(null);

  const selectedSymbols = new Set(legs.map(l => l.symbol));
  const selectedSides = {};
  legs.forEach(l => { selectedSides[l.symbol] = l.side; });
  const oiMax = Math.max(...chain.map(r => Math.max(parseFloat(r.call?.oi || 0), parseFloat(r.put?.oi || 0))), 1);
  const volMax = Math.max(...chain.map(r => Math.max(parseFloat(r.call?.volume || 0), parseFloat(r.put?.volume || 0))), 1);
  const atmIdx = chain.length && spot ? chain.reduce((best, row, i, arr) =>
    Math.abs(parseFloat(row.strike) - spot) < Math.abs(parseFloat(arr[best].strike) - spot) ? i : best, 0) : -1;

  useEffect(() => {
    if (atmIdx >= 0 && ref.current) {
      const rows = ref.current.querySelectorAll('tbody tr');
      if (rows[atmIdx]) setTimeout(() => rows[atmIdx].scrollIntoView({ block: 'center', behavior: 'smooth' }), 200);
      // Horizontal scroll to center the strike column
      setTimeout(() => {
        if (ref.current) {
          const table = ref.current.querySelector('table');
          if (table) ref.current.scrollLeft = (table.scrollWidth - ref.current.clientWidth) / 2;
        }
      }, 300);
    }
  }, [chain, atmIdx]);

  const f = (v, d = 2) => typeof v === 'number' && v ? v.toFixed(d) : '—';
  const fk = v => { const n = parseFloat(v) || 0; return n >= 100000 ? (n / 100000).toFixed(2) + 'L' : n >= 1000 ? (n / 1000).toFixed(2) + 'K' : n ? n.toFixed(2) : '—'; };

  return (
    <div className="oc2-wrap" ref={ref}>
      <table className="oc2">
        <thead>
          <tr>
            <th colSpan={6} className="oc2-hdr-call">CALLS</th>
            <th className="oc2-hdr-strike">STRIKE</th>
            <th colSpan={6} className="oc2-hdr-put">PUTS</th>
          </tr>
          <tr>
            <th className="oc2-th c">OI</th>
            <th className="oc2-th c">Vol</th>
            <th className="oc2-th c">IV</th>
            <th className="oc2-th c">Δ</th>
            <th className="oc2-th c">Bid / Ask</th>
            <th className="oc2-th c ltp-h">LTP</th>
            <th className="oc2-th strike-h" />
            <th className="oc2-th p ltp-h">LTP</th>
            <th className="oc2-th p">Bid / Ask</th>
            <th className="oc2-th p">Δ</th>
            <th className="oc2-th p">IV</th>
            <th className="oc2-th p">Vol</th>
            <th className="oc2-th p">OI</th>
          </tr>
        </thead>
        <tbody>
          {chain.map((row, i) => {
            const s = parseFloat(row.strike);
            const c = row.call || {}, p = row.put || {};
            const cITM = spot && s < spot, pITM = spot && s > spot;
            const isAtm = i === atmIdx;
            const hov = hoveredRow === i;
            const cSel = selectedSymbols.has(c.symbol), pSel = selectedSymbols.has(p.symbol);
            const cSide = selectedSides[c.symbol], pSide = selectedSides[p.symbol];
            const cOIpct = (parseFloat(c.oi || 0) / oiMax) * 100;
            const pOIpct = (parseFloat(p.oi || 0) / oiMax) * 100;
            const cVolPct = (parseFloat(c.volume || 0) / volMax) * 100;
            const pVolPct = (parseFloat(p.volume || 0) / volMax) * 100;
            const cChg = parseFloat(c.change || 0), pChg = parseFloat(p.change || 0);

            return (
              <tr key={row.strike}
                className={`oc2-row ${isAtm ? 'atm' : ''} ${cITM ? 'c-itm' : ''} ${pITM ? 'p-itm' : ''}`}
                onMouseEnter={() => setHoveredRow(i)} onMouseLeave={() => setHoveredRow(null)}>

                {/* ── CALL SIDE ── */}
                <td className="oc2-td c oi-cell">
                  <div className="oc2-oi-bg c" style={{ width: `${cOIpct}%` }} />
                  <span className="oc2-oi-val">{fk(c.oi)}</span>
                </td>
                <td className="oc2-td c vol-cell">
                  <div className="oc2-vol-bg c" style={{ width: `${cVolPct}%` }} />
                  <span className="oc2-vol-val">{fk(c.volume)}</span>
                </td>
                <td className="oc2-td c iv">{c.iv ? (c.iv * 100).toFixed(1) : '—'}</td>
                <td className="oc2-td c delta">{f(c.delta, 2)}</td>
                <td className="oc2-td c bidask">
                  <span className="bid">{f(c.bid)}</span>
                  <span className="sep">/</span>
                  <span className="ask">{f(c.ask)}</span>
                </td>
                <td className="oc2-td c ltp-cell">
                  {(hov || cSel) && (
                    <div className="oc2-bs">
                      <span className={`oc2-b ${cSide === 'buy' ? 'on' : ''}`} onClick={() => onAddLeg(row, 'call', 'buy')}>B</span>
                      <span className={`oc2-s ${cSide === 'sell' ? 'on' : ''}`} onClick={() => onAddLeg(row, 'call', 'sell')}>S</span>
                    </div>
                  )}
                  <div className="oc2-ltp-wrap">
                    <span className={`oc2-ltp ${cSel ? 'sel' : ''}`}>{f(c.mark_price)}</span>
                    {cChg !== 0 && <span className={`oc2-chg ${cChg >= 0 ? 'up' : 'dn'}`}>{cChg >= 0 ? '▲' : '▼'}{Math.abs(cChg).toFixed(2)}</span>}
                  </div>
                </td>

                {/* ── STRIKE ── */}
                <td className={`oc2-td strike ${isAtm ? 'atm-strike' : ''}`}>
                  <span className="oc2-strike-num">{Number(row.strike).toLocaleString()}</span>
                  {isAtm && <div className="oc2-atm-badge">ATM</div>}
                </td>

                {/* ── PUT SIDE ── */}
                <td className="oc2-td p ltp-cell">
                  {(hov || pSel) && (
                    <div className="oc2-bs right">
                      <span className={`oc2-b ${pSide === 'buy' ? 'on' : ''}`} onClick={() => onAddLeg(row, 'put', 'buy')}>B</span>
                      <span className={`oc2-s ${pSide === 'sell' ? 'on' : ''}`} onClick={() => onAddLeg(row, 'put', 'sell')}>S</span>
                    </div>
                  )}
                  <div className="oc2-ltp-wrap">
                    <span className={`oc2-ltp ${pSel ? 'sel' : ''}`}>{f(p.mark_price)}</span>
                    {pChg !== 0 && <span className={`oc2-chg ${pChg >= 0 ? 'up' : 'dn'}`}>{pChg >= 0 ? '▲' : '▼'}{Math.abs(pChg).toFixed(2)}</span>}
                  </div>
                </td>
                <td className="oc2-td p bidask">
                  <span className="bid">{f(p.bid)}</span>
                  <span className="sep">/</span>
                  <span className="ask">{f(p.ask)}</span>
                </td>
                <td className="oc2-td p delta">{f(p.delta, 2)}</td>
                <td className="oc2-td p iv">{p.iv ? (p.iv * 100).toFixed(1) : '—'}</td>
                <td className="oc2-td p vol-cell">
                  <div className="oc2-vol-bg p" style={{ width: `${pVolPct}%` }} />
                  <span className="oc2-vol-val">{fk(p.volume)}</span>
                </td>
                <td className="oc2-td p oi-cell">
                  <div className="oc2-oi-bg p" style={{ width: `${pOIpct}%` }} />
                  <span className="oc2-oi-val">{fk(p.oi)}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
