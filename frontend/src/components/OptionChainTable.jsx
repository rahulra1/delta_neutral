import React, { useState, useRef, useEffect } from 'react';

/**
 * Reusable Option Chain table component.
 * Props:
 *   chain       - array of {strike, call: {product_id, symbol, mark_price, delta, iv, oi, bid, ask, volume}, put: {...}}
 *   spot        - current spot price
 *   sym         - currency symbol ('$' or '₹')
 *   lot         - lot size multiplier
 *   isCrypto    - whether to divide OI by lot size
 *   legs        - currently selected legs [{symbol, side, ...}]
 *   onAddLeg    - (row, type:'call'|'put', side:'buy'|'sell') => void
 */
export default function OptionChainTable({ chain = [], spot = 0, sym = '$', lot = 1, isCrypto = false, legs = [], onAddLeg }) {
  const [hoveredRow, setHoveredRow] = useState(null);
  const ref = useRef(null);

  const selectedSymbols = new Set(legs.map(l => l.symbol));
  const oiMax = Math.max(...chain.map(r => Math.max(parseFloat(r.call?.oi || 0), parseFloat(r.put?.oi || 0))), 1);
  const atmIdx = chain.length && spot ? chain.reduce((best, row, i, arr) =>
    Math.abs(parseFloat(row.strike) - spot) < Math.abs(parseFloat(arr[best].strike) - spot) ? i : best, 0) : -1;

  useEffect(() => {
    if (atmIdx >= 0 && ref.current) {
      const rows = ref.current.querySelectorAll('tbody tr');
      if (rows[atmIdx]) setTimeout(() => rows[atmIdx].scrollIntoView({ block: 'center', behavior: 'smooth' }), 200);
    }
  }, [chain, atmIdx]);

  const f2 = v => typeof v === 'number' ? v.toFixed(2) : '—';
  const f4 = v => typeof v === 'number' ? v.toFixed(4) : '—';

  return (
    <div className="at-chain" ref={ref}>
      <table>
        <thead>
          <tr>
            <th>Delta</th>
            <th>Call LTP</th>
            <th>OI</th>
            <th className="at-strike-hd">Strike</th>
            <th>IV</th>
            <th>OI</th>
            <th>Put LTP</th>
            <th>Delta</th>
          </tr>
        </thead>
        <tbody>
          {chain.map((row, i) => {
            const s = parseFloat(row.strike);
            const c = row.call || {}, p = row.put || {};
            const cITM = spot && s < spot, pITM = spot && s > spot;
            const isAtm = i === atmIdx;
            const isHovered = hoveredRow === i;
            const cSel = selectedSymbols.has(c.symbol), pSel = selectedSymbols.has(p.symbol);
            const cOI = isCrypto ? Math.round((parseFloat(c.oi) || 0) / lot) : Math.round(parseFloat(c.oi) || 0);
            const pOI = isCrypto ? Math.round((parseFloat(p.oi) || 0) / lot) : Math.round(parseFloat(p.oi) || 0);
            const cOIpct = (parseFloat(c.oi || 0) / oiMax) * 100;
            const pOIpct = (parseFloat(p.oi || 0) / oiMax) * 100;

            return (
              <tr key={row.strike}
                className={`${isAtm ? 'at-atm-row' : ''} ${cITM ? 'at-call-itm' : ''} ${pITM ? 'at-put-itm' : ''}`}
                onMouseEnter={() => setHoveredRow(i)} onMouseLeave={() => setHoveredRow(null)}>
                <td className="at-call">{f4(c.delta)}</td>
                <td className="at-call at-ltp-cell">
                  {(isHovered || cSel) && (
                    <div className="at-bs-row">
                      <span className="at-bs-btn buy" onClick={() => onAddLeg(row, 'call', 'buy')}>B</span>
                      <span className="at-bs-btn sell" onClick={() => onAddLeg(row, 'call', 'sell')}>S</span>
                    </div>
                  )}
                  <span className={`at-ltp ${cSel ? 'selected' : ''}`}>{f2(c.mark_price)}</span>
                </td>
                <td className="at-call at-oi-cell">
                  <div className="at-oi-bar-wrap"><div className="at-oi-bar call" style={{ width: `${cOIpct}%` }} /></div>
                </td>
                <td className="at-strike-cell">
                  <span className="at-strike-num">{Number(row.strike).toLocaleString()}</span>
                  {isAtm && <div className="at-synth-label">Spot {sym}{spot?.toLocaleString(undefined, { maximumFractionDigits: 2 })}</div>}
                </td>
                <td className="at-iv">{c.iv ? (c.iv * 100).toFixed(1) : '—'}</td>
                <td className="at-put at-oi-cell">
                  <div className="at-oi-bar-wrap"><div className="at-oi-bar put" style={{ width: `${pOIpct}%` }} /></div>
                </td>
                <td className="at-put at-ltp-cell">
                  {(isHovered || pSel) && (
                    <div className="at-bs-row right">
                      <span className="at-bs-btn buy" onClick={() => onAddLeg(row, 'put', 'buy')}>B</span>
                      <span className="at-bs-btn sell" onClick={() => onAddLeg(row, 'put', 'sell')}>S</span>
                    </div>
                  )}
                  <span className={`at-ltp ${pSel ? 'selected' : ''}`}>{f2(p.mark_price)}</span>
                </td>
                <td className="at-put">{f4(p.delta)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
