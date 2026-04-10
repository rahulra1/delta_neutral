// ── Strategy Builder JS ──

let legCounter = 0;
let condCounters = { entry: 1, exit: 1 };
let liveChain = null;   // cached chain data
let liveSpot = 0;
let liveTimer = null;
const CRYPTO_ASSETS = new Set(['BTC', 'ETH']);

// ── Leg Management ──

function addLeg(side='sell', type='CE', strike='ATM', offset=0) {
  const id = legCounter++;
  const html = `
    <div class="sb-leg" data-leg="${id}">
      <div class="sb-leg-row">
        <div>
          <div class="leg-label">Side</div>
          <select class="leg-side" onchange="styleSide(this);updateSummary();renderLiveData()">
            <option value="buy" ${side==='buy'?'selected':''}>BUY</option>
            <option value="sell" ${side==='sell'?'selected':''}>SELL</option>
          </select>
        </div>
        <div>
          <div class="leg-label">Type</div>
          <select class="leg-type" onchange="updateSummary();renderLiveData()">
            <option value="CE" ${type==='CE'?'selected':''}>CE (Call)</option>
            <option value="PE" ${type==='PE'?'selected':''}>PE (Put)</option>
            <option value="FUT">FUT</option>
          </select>
        </div>
        <div>
          <div class="leg-label">Strike</div>
          <select class="leg-strike" onchange="updateSummary();renderLiveData()">
            <option value="ATM" ${strike==='ATM'?'selected':''}>ATM</option>
            <option value="ITM1" ${strike==='ITM1'?'selected':''}>ITM +1</option>
            <option value="ITM2" ${strike==='ITM2'?'selected':''}>ITM +2</option>
            <option value="ITM3">ITM +3</option>
            <option value="OTM1" ${strike==='OTM1'?'selected':''}>OTM +1</option>
            <option value="OTM2" ${strike==='OTM2'?'selected':''}>OTM +2</option>
            <option value="OTM3" ${strike==='OTM3'?'selected':''}>OTM +3</option>
            <option value="OTM4">OTM +4</option>
            <option value="OTM5">OTM +5</option>
            <option value="OTM7">OTM +7</option>
            <option value="OTM10">OTM +10</option>
            <option value="custom" ${strike==='custom'?'selected':''}>Custom</option>
          </select>
        </div>
        <div>
          <div class="leg-label">Lots</div>
          <input type="number" class="leg-lots" value="1" min="1" onchange="updateSummary();renderLiveData()">
        </div>
        <div>
          <div class="leg-label">Leg SL%</div>
          <input type="number" class="leg-sl" value="0" min="0" placeholder="0">
        </div>
        <div style="padding-top:16px">
          <button class="btn-remove" onclick="removeLeg(this)" title="Remove leg">✕</button>
        </div>
      </div>
    </div>`;
  document.getElementById('legs_container').insertAdjacentHTML('beforeend', html);
  // Style the side select
  const legEl = document.querySelector(`.sb-leg[data-leg="${id}"] .leg-side`);
  styleSide(legEl);
  updateSummary();
}

function removeLeg(btn) {
  btn.closest('.sb-leg').remove();
  updateSummary();
  renderLiveData();
}

function styleSide(sel) {
  sel.classList.remove('side-buy', 'side-sell');
  sel.classList.add(sel.value === 'buy' ? 'side-buy' : 'side-sell');
}

// ── Presets ──

function loadPreset(name) {
  document.getElementById('legs_container').innerHTML = '';
  legCounter = 0;
  const presets = {
    straddle:    [['sell','CE','ATM'],['sell','PE','ATM']],
    strangle:    [['sell','CE','OTM2'],['sell','PE','OTM2']],
    iron_condor: [['buy','CE','OTM5'],['sell','CE','OTM2'],['sell','PE','OTM2'],['buy','PE','OTM5']],
    iron_fly:    [['buy','CE','OTM3'],['sell','CE','ATM'],['sell','PE','ATM'],['buy','PE','OTM3']],
    bull_spread: [['buy','CE','ATM'],['sell','CE','OTM3']],
    bear_spread: [['buy','PE','ATM'],['sell','PE','OTM3']],
  };
  (presets[name] || []).forEach(l => addLeg(l[0], l[1], l[2]));
}

// ── Conditions ──

function addCondition(group) {
  const id = condCounters[group]++;
  const container = document.getElementById(group + '_conditions');
  const isEntry = group === 'entry';
  const html = `
    <div class="sb-condition" data-id="${group}_${id}">
      <div class="sb-cond-row">
        <select class="cond-type" onchange="updateConditionUI(this)">
          <option value="time">⏰ Time Based</option>
          <option value="indicator">📊 Indicator Based</option>
          <option value="price">💰 Price Based</option>
          <option value="pnl">📈 P&L Based</option>
          <option value="premium">🏷 Premium Based</option>
        </select>
        <div class="cond-params">
          <label>${isEntry ? 'Entry' : 'Exit'} Time</label>
          <input type="time" value="${isEntry ? '09:20' : '15:15'}" class="cond-val">
        </div>
        <button class="btn-remove" onclick="removeCondition(this)">✕</button>
      </div>
    </div>`;
  container.insertAdjacentHTML('beforeend', html);
  updateSummary();
}

function removeCondition(btn) {
  btn.closest('.sb-condition').remove();
  updateSummary();
}

function updateConditionUI(sel) {
  const params = sel.closest('.sb-cond-row').querySelector('.cond-params');
  const type = sel.value;
  const templates = {
    time: `<label>Time</label><input type="time" value="09:20" class="cond-val">`,
    indicator: `
      <div class="ind-fields">
        <select class="ind-name">
          <option value="rsi">RSI</option>
          <option value="sma">SMA</option>
          <option value="ema">EMA</option>
          <option value="supertrend">Supertrend</option>
          <option value="vwap">VWAP</option>
          <option value="bb">Bollinger Bands</option>
          <option value="macd">MACD</option>
          <option value="atr">ATR</option>
        </select>
        <select class="ind-op">
          <option value="crosses_above">Crosses Above</option>
          <option value="crosses_below">Crosses Below</option>
          <option value="greater_than">Greater Than</option>
          <option value="less_than">Less Than</option>
        </select>
        <input type="number" value="70" class="ind-val" placeholder="Value">
        <label>Period</label>
        <input type="number" value="14" class="ind-period" style="width:55px">
        <label>TF</label>
        <select class="ind-tf">
          <option value="1m">1m</option>
          <option value="5m" selected>5m</option>
          <option value="15m">15m</option>
          <option value="1h">1h</option>
          <option value="1d">1d</option>
        </select>
      </div>`,
    price: `
      <div class="ind-fields">
        <label>Spot Price</label>
        <select class="price-op">
          <option value="above">Above</option>
          <option value="below">Below</option>
          <option value="between">Between</option>
          <option value="change_pct">% Change</option>
        </select>
        <input type="number" value="0" class="price-val" placeholder="Value">
      </div>`,
    pnl: `
      <div class="ind-fields">
        <label>P&L</label>
        <select class="pnl-op">
          <option value="profit_above">Profit Above</option>
          <option value="loss_above">Loss Above</option>
          <option value="profit_pct">Profit % Above</option>
          <option value="loss_pct">Loss % Above</option>
        </select>
        <input type="number" value="10" class="pnl-val" placeholder="Value">
      </div>`,
    premium: `
      <div class="ind-fields">
        <label>Premium</label>
        <select class="prem-op">
          <option value="increase_pct">Increases by %</option>
          <option value="decrease_pct">Decreases by %</option>
          <option value="above">Above Value</option>
          <option value="below">Below Value</option>
        </select>
        <input type="number" value="40" class="prem-val" placeholder="Value">
        <label>Leg</label>
        <select class="prem-leg">
          <option value="any">Any Leg</option>
          <option value="all">All Legs</option>
          <option value="combined">Combined</option>
        </select>
      </div>`,
  };
  params.innerHTML = templates[type] || templates.time;
  updateSummary();
}

// ── Summary ──

function updateSummary() {
  const legs = document.querySelectorAll('.sb-leg');
  const legCount = legs.length;
  document.getElementById('sum_leg_count').textContent = legCount;
  document.getElementById('sum_legs').textContent = legCount;
  document.getElementById('sum_under').textContent = document.getElementById('sb_underlying').value;

  // Determine strategy type from legs
  let sells = 0, buys = 0, ces = 0, pes = 0;
  legs.forEach(l => {
    if (l.querySelector('.leg-side').value === 'sell') sells++; else buys++;
    if (l.querySelector('.leg-type').value === 'CE') ces++; else pes++;
  });
  let type = '—';
  if (legCount === 2 && sells === 2 && ces === 1 && pes === 1) {
    const s1 = legs[0].querySelector('.leg-strike').value;
    const s2 = legs[1].querySelector('.leg-strike').value;
    type = (s1 === 'ATM' && s2 === 'ATM') ? 'Short Straddle' : 'Short Strangle';
  } else if (legCount === 4 && sells === 2 && buys === 2) {
    type = 'Iron Condor / Iron Fly';
  } else if (legCount === 2 && sells === 1 && buys === 1) {
    type = 'Spread';
  } else if (legCount > 0) {
    type = 'Custom (' + legCount + ' legs)';
  }
  document.getElementById('sum_type').textContent = type;

  // Entry/exit summary
  const entryConds = document.querySelectorAll('#entry_conditions .cond-type');
  const exitConds = document.querySelectorAll('#exit_conditions .cond-type');
  document.getElementById('sum_entry').textContent = summarizeConds(entryConds);
  document.getElementById('sum_exit').textContent = summarizeConds(exitConds);

  const sl = document.getElementById('sb_sl').value;
  const tgt = document.getElementById('sb_target').value;
  document.getElementById('sum_sl_tgt').textContent = sl + '% / ' + tgt + '%';

  document.getElementById('leg_summary').style.display = legCount > 0 ? 'flex' : 'none';

  // Update premium from live data if available
  if (liveChain && legCount > 0) {
    const resolved = getResolvedLegs();
    let net = 0;
    resolved.forEach(l => { if (l.opt) net += (l.side === 'sell' ? 1 : -1) * l.opt.mark_price * l.lots; });
    document.getElementById('sum_premium').textContent = fmtPrice(net);
  }
}

function summarizeConds(conds) {
  const labels = { time: 'Time', indicator: 'Indicator', price: 'Price', pnl: 'P&L', premium: 'Premium' };
  const types = [];
  conds.forEach(c => { const l = labels[c.value]; if (!types.includes(l)) types.push(l); });
  return types.join(' + ') || '—';
}

// ── Collect Strategy Data ──

function collectStrategy() {
  const legs = [];
  document.querySelectorAll('.sb-leg').forEach(el => {
    legs.push({
      side: el.querySelector('.leg-side').value,
      type: el.querySelector('.leg-type').value,
      strike: el.querySelector('.leg-strike').value,
      lots: parseInt(el.querySelector('.leg-lots').value),
      sl_pct: parseFloat(el.querySelector('.leg-sl').value) || 0,
    });
  });

  const collectConds = group => {
    const conds = [];
    document.querySelectorAll(`#${group}_conditions .sb-condition`).forEach(el => {
      const type = el.querySelector('.cond-type').value;
      const data = { type };
      if (type === 'time') {
        const inp = el.querySelector('input[type="time"]');
        data.time = inp ? inp.value : '';
      } else if (type === 'indicator') {
        data.indicator = el.querySelector('.ind-name')?.value;
        data.operator = el.querySelector('.ind-op')?.value;
        data.value = parseFloat(el.querySelector('.ind-val')?.value) || 0;
        data.period = parseInt(el.querySelector('.ind-period')?.value) || 14;
        data.timeframe = el.querySelector('.ind-tf')?.value || '5m';
      } else if (type === 'price') {
        data.operator = el.querySelector('.price-op')?.value;
        data.value = parseFloat(el.querySelector('.price-val')?.value) || 0;
      } else if (type === 'pnl') {
        data.operator = el.querySelector('.pnl-op')?.value;
        data.value = parseFloat(el.querySelector('.pnl-val')?.value) || 0;
      } else if (type === 'premium') {
        data.operator = el.querySelector('.prem-op')?.value;
        data.value = parseFloat(el.querySelector('.prem-val')?.value) || 0;
        data.leg = el.querySelector('.prem-leg')?.value || 'any';
      }
      conds.push(data);
    });
    return conds;
  };

  return {
    name: document.getElementById('sb_name').value,
    underlying: document.getElementById('sb_underlying').value,
    expiry: document.getElementById('sb_expiry').value,
    profile_id: document.getElementById('sb_profile').value,
    legs,
    entry_conditions: collectConds('entry'),
    exit_conditions: collectConds('exit'),
    risk: {
      sl_pct: parseFloat(document.getElementById('sb_sl').value) || 0,
      target_pct: parseFloat(document.getElementById('sb_target').value) || 0,
      leg_sl_pct: parseFloat(document.getElementById('sb_leg_sl').value) || 0,
      trail_pct: parseFloat(document.getElementById('sb_trail').value) || 0,
      max_loss: parseFloat(document.getElementById('sb_max_loss').value) || 0,
      reentry: document.getElementById('sb_reentry').value,
    },
    execution: {
      order_type: document.getElementById('sb_order_type').value,
      product: document.getElementById('sb_product').value,
      lots: parseInt(document.getElementById('sb_lots').value) || 1,
      sqoff_time: document.getElementById('sb_sqoff').value,
      days: document.getElementById('sb_days').value,
    },
    adjustment: {
      trigger: document.getElementById('sb_adj_trigger').value,
      value: parseFloat(document.getElementById('sb_adj_value').value) || 0,
      action: document.getElementById('sb_adj_action').value,
      max: parseInt(document.getElementById('sb_max_adj').value) || 0,
    },
  };
}

// ── Actions ──

function showToast(msg, isError) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (isError ? ' error' : '');
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

function saveStrategy() {
  const data = collectStrategy();
  if (!data.legs.length) { showToast('Add at least one leg', true); return; }
  fetch('/api/strategy-builder/save', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(r => r.json()).then(d => {
    showToast('Strategy saved! ID: ' + d.sid);
  }).catch(() => showToast('Save failed', true));
}

function deployStrategy() {
  const data = collectStrategy();
  if (!data.legs.length) { showToast('Add at least one leg', true); return; }

  // Build modal body
  const resolved = getResolvedLegs();
  let legsHtml = resolved.map(l => {
    const strike = l.opt ? l.opt.strike : l.strikeKey;
    const price = l.opt ? '$' + l.opt.mark_price.toFixed(2) : '—';
    return `<tr><td class="side-${l.side}">${l.side.toUpperCase()}</td><td>${l.type}</td><td>${strike}</td><td>${price}</td><td>${l.lots}</td></tr>`;
  }).join('');

  const body = document.getElementById('deployModalBody');
  body.innerHTML = `
    <div class="modal-section">
      <div class="modal-section-title">Strategy</div>
      <div class="modal-kv">
        <span class="k">Name</span><span class="v">${data.name}</span>
        <span class="k">Underlying</span><span class="v">${data.underlying}</span>
        <span class="k">Expiry</span><span class="v">${data.expiry.replace('_',' ')}</span>
        <span class="k">Order Type</span><span class="v">${data.execution.order_type}</span>
        <span class="k">Product</span><span class="v">${data.execution.product}</span>
        <span class="k">Lots/Leg</span><span class="v">${data.execution.lots}</span>
      </div>
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Legs (${data.legs.length})</div>
      <table class="modal-table">
        <tr><th>Side</th><th>Type</th><th>Strike</th><th>LTP</th><th>Lots</th></tr>
        ${legsHtml}
      </table>
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Risk</div>
      <div class="modal-kv">
        <span class="k">Stop Loss</span><span class="v">${data.risk.sl_pct}%</span>
        <span class="k">Target</span><span class="v">${data.risk.target_pct}%</span>
        <span class="k">Trailing SL</span><span class="v">${data.risk.trail_pct ? data.risk.trail_pct + '%' : 'Off'}</span>
        <span class="k">Re-entry</span><span class="v">${data.risk.reentry}</span>
      </div>
    </div>
    <div class="modal-warn">⚠️ This will place <strong>real orders</strong> with real money. Please verify all details before proceeding.</div>`;

  document.getElementById('deployModal').classList.add('active');
  // Reset footer in case it was replaced by a previous deploy
  document.querySelector('#deployModal .modal-footer').innerHTML = `
    <button class="btn btn-outline" onclick="closeDeployModal()">Cancel</button>
    <button class="btn btn-green" id="deployRunBtn" onclick="confirmDeploy()">▶ Run Strategy</button>`;
}

function closeDeployModal() {
  document.getElementById('deployModal').classList.remove('active');
}

function confirmDeploy() {
  const btn = document.getElementById('deployRunBtn');
  btn.disabled = true;
  btn.textContent = 'Deploying…';
  const data = collectStrategy();
  fetch('/api/strategy-builder/deploy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(r => r.json().then(d => ({ok: r.ok, data: d}))).then(({ok, data: d}) => {
    if (!ok || d.error) {
      showToast(d.error || 'Deploy failed', true);
      btn.disabled = false;
      btn.textContent = '▶ Run Strategy';
      return;
    }
    // Show results in modal
    const body = document.getElementById('deployModalBody');
    const res = d.results || [];
    const rows = res.map(r =>
      `<tr><td>${r.symbol||'—'}</td><td>${r.side||'—'}</td><td>${r.size||'—'}</td><td>${r.success ? '<span class="val-green">✓</span>' : '<span class="val-red">✗</span>'}</td></tr>`
    ).join('');
    body.innerHTML = `
      <div style="text-align:center;margin-bottom:16px;font-size:1.5rem">🚀</div>
      <div style="text-align:center;font-weight:700;margin-bottom:4px">Strategy Deployed — ID: ${d.sid}</div>
      <div style="text-align:center;font-size:.8rem;color:var(--muted);margin-bottom:16px">${d.monitor_id ? 'Monitor active' : 'No auto-monitor'}</div>
      <table class="modal-table"><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Status</th></tr>${rows}</table>`;
    document.querySelector('.modal-footer').innerHTML = `<button class="btn btn-outline" onclick="closeDeployModal()">Close</button>`;
    showToast('🚀 Strategy deployed! ID: ' + d.sid);
  }).catch(() => {
    showToast('Deploy failed', true);
    btn.disabled = false;
    btn.textContent = '▶ Run Strategy';
  });
}

function paperTrade() {
  const data = collectStrategy();
  if (!data.legs.length) { showToast('Add at least one leg', true); return; }
  fetch('/api/strategy-builder/paper-trade', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(r => r.json()).then(d => {
    showToast('📝 Paper trade started! ID: ' + d.sid);
  }).catch(() => showToast('Paper trade failed', true));
}

// ── Expiry & Live Data ──

function loadExpiries() {
  const asset = document.getElementById('sb_underlying').value;
  const sel = document.getElementById('sb_expiry');
  sel.innerHTML = '<option value="">Loading…</option>';
  fetch(`/api/expiries?asset=${asset}`).then(r => r.json()).then(d => {
    sel.innerHTML = '';
    (d.expiries || []).forEach(e => {
      const o = document.createElement('option');
      o.value = e; o.textContent = e; sel.appendChild(o);
    });
    if (!sel.options.length) sel.innerHTML = '<option value="">No expiries</option>';
    fetchLiveData();
  }).catch(() => { sel.innerHTML = '<option value="">Error</option>'; });
}

function fetchLiveData() {
  const asset = document.getElementById('sb_underlying').value;
  const expiry = document.getElementById('sb_expiry').value;
  if (!expiry) return;
  fetch(`/api/chain?asset=${asset}&expiry=${expiry}`).then(r => r.json()).then(d => {
    if (d.error) return;
    liveChain = d.chain || [];
    liveSpot = d.spot_price || 0;
    renderLiveData();
  }).catch(() => {});
}

function resolveStrikeIndex(strikeKey, optType) {
  if (!liveChain || !liveSpot) return -1;
  const strikes = liveChain.map(r => parseFloat(r.strike));
  const atmIdx = strikes.reduce((best, s, i) => Math.abs(s - liveSpot) < Math.abs(strikes[best] - liveSpot) ? i : best, 0);
  const m = strikeKey.match(/^(ATM|OTM|ITM)(\d*)$/);
  if (!m) return atmIdx;
  let offset = parseInt(m[2]) || 0;
  if (m[1] === 'OTM') offset = optType === 'CE' ? offset : -offset;
  else if (m[1] === 'ITM') offset = optType === 'CE' ? -offset : offset;
  return Math.max(0, Math.min(atmIdx + offset, liveChain.length - 1));
}

function getResolvedLegs() {
  const legs = [];
  document.querySelectorAll('.sb-leg').forEach(el => {
    const side = el.querySelector('.leg-side').value;
    const type = el.querySelector('.leg-type').value;
    const strikeKey = el.querySelector('.leg-strike').value;
    const lots = parseInt(el.querySelector('.leg-lots').value) || 1;
    if (!liveChain || type === 'FUT') { legs.push({side, type, strikeKey, lots, opt: null}); return; }
    const idx = resolveStrikeIndex(strikeKey, type);
    const optKey = type === 'CE' ? 'call' : 'put';
    const opt = idx >= 0 ? liveChain[idx]?.[optKey] : null;
    legs.push({side, type, strikeKey, lots, opt});
  });
  return legs;
}

function fmtPrice(v) {
  const asset = document.getElementById('sb_underlying').value;
  const sym = CRYPTO_ASSETS.has(asset) ? '$' : '₹';
  return sym + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2});
}

function renderLiveData() {
  const resolved = getResolvedLegs();
  const card = document.getElementById('live_data_card');
  const payoffCard = document.getElementById('payoff_card');
  if (!resolved.length || !liveChain) { card.style.display = 'none'; payoffCard.style.display = 'none'; return; }
  card.style.display = '';
  payoffCard.style.display = '';

  document.getElementById('live_spot').textContent = fmtPrice(liveSpot);
  document.getElementById('live_expiry_label').textContent = document.getElementById('sb_expiry').value;

  const isCrypto = CRYPTO_ASSETS.has(document.getElementById('sb_underlying').value);
  const tbody = document.getElementById('live_legs_body');
  let netPrem = 0;
  tbody.innerHTML = resolved.map(l => {
    if (!l.opt) return `<tr><td class="side-${l.side}">${l.side.toUpperCase()} ${l.type}</td><td colspan="5">—</td></tr>`;
    const dir = l.side === 'sell' ? 1 : -1;
    netPrem += dir * l.opt.mark_price * l.lots;
    const deltaCell = l.opt.delta ? l.opt.delta.toFixed(3) : '—';
    const thetaCell = l.opt.theta ? l.opt.theta.toFixed(4) : '—';
    return `<tr>
      <td class="side-${l.side}">${l.side.toUpperCase()} ${l.type}</td>
      <td>${l.opt.strike}</td>
      <td>${fmtPrice(l.opt.mark_price)}</td>
      <td>${(l.opt.iv * 100).toFixed(1)}%</td>
      <td>${deltaCell}</td>
      <td>${thetaCell}</td>
    </tr>`;
  }).join('');

  document.getElementById('live_net_prem').textContent = fmtPrice(netPrem);

  // Compute max profit / max loss for display
  const payoff = computePayoff(resolved);
  if (payoff) {
    const maxP = Math.max(...payoff.pnls);
    const maxL = Math.min(...payoff.pnls);
    document.getElementById('live_max_profit').textContent = maxP >= 9999 ? 'Unlimited' : fmtPrice(maxP);
    document.getElementById('live_max_loss').textContent = maxL <= -9999 ? 'Unlimited' : fmtPrice(maxL);
  }

  drawPayoff(resolved);
  updateSummary();
}

function computePayoff(resolved) {
  if (!resolved.length || !liveSpot) return null;
  const strikes = resolved.filter(l => l.opt).map(l => parseFloat(l.opt.strike));
  if (!strikes.length) return null;
  const lo = Math.min(liveSpot, ...strikes) * 0.9;
  const hi = Math.max(liveSpot, ...strikes) * 1.1;
  const step = (hi - lo) / 200;
  const xs = [], pnls = [];
  for (let s = lo; s <= hi; s += step) {
    let pnl = 0;
    resolved.forEach(l => {
      if (!l.opt) return;
      const K = parseFloat(l.opt.strike);
      const prem = l.opt.mark_price;
      const dir = l.side === 'sell' ? -1 : 1;
      let intrinsic = l.type === 'CE' ? Math.max(s - K, 0) : Math.max(K - s, 0);
      pnl += dir * (intrinsic - prem) * l.lots;
    });
    xs.push(s);
    pnls.push(pnl);
  }
  return {xs, pnls};
}

function drawPayoff(resolved) {
  const canvas = document.getElementById('payoff_canvas');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const data = computePayoff(resolved);
  if (!data) return;
  const {xs, pnls} = data;
  const minX = xs[0], maxX = xs[xs.length-1];
  const minY = Math.min(...pnls), maxY = Math.max(...pnls);
  const pad = {l:50, r:16, t:16, b:28};
  const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
  const sx = i => pad.l + (xs[i] - minX) / (maxX - minX) * cw;
  const sy = v => pad.t + (1 - (v - minY) / ((maxY - minY) || 1)) * ch;

  // Zero line
  const zeroY = sy(0);
  ctx.strokeStyle = '#ccc'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(w - pad.r, zeroY); ctx.stroke();
  ctx.setLineDash([]);

  // Spot line
  const spotX = pad.l + (liveSpot - minX) / (maxX - minX) * cw;
  ctx.strokeStyle = '#6366f1'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(spotX, pad.t); ctx.lineTo(spotX, h - pad.b); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#6366f1'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('Spot', spotX, h - pad.b + 12);

  // Fill profit/loss areas
  ctx.beginPath(); ctx.moveTo(sx(0), zeroY);
  for (let i = 0; i < xs.length; i++) ctx.lineTo(sx(i), sy(pnls[i]));
  ctx.lineTo(sx(xs.length-1), zeroY); ctx.closePath();
  ctx.save(); ctx.clip();
  // Green above zero
  ctx.fillStyle = 'rgba(34,197,94,0.15)';
  ctx.fillRect(pad.l, pad.t, cw, zeroY - pad.t);
  // Red below zero
  ctx.fillStyle = 'rgba(239,68,68,0.15)';
  ctx.fillRect(pad.l, zeroY, cw, h - pad.b - zeroY);
  ctx.restore();

  // Payoff line
  ctx.strokeStyle = '#1a1a2e'; ctx.lineWidth = 2;
  ctx.beginPath();
  for (let i = 0; i < xs.length; i++) { i === 0 ? ctx.moveTo(sx(i), sy(pnls[i])) : ctx.lineTo(sx(i), sy(pnls[i])); }
  ctx.stroke();

  // Y-axis labels
  ctx.fillStyle = '#9ca3af'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
  [maxY, 0, minY].forEach(v => { ctx.fillText(v.toFixed(1), pad.l - 6, sy(v) + 3); });
}

// ── Init ──

document.addEventListener('DOMContentLoaded', () => {
  // Load profiles
  fetch('/api/profiles').then(r => r.json()).then(d => {
    const sel = document.getElementById('sb_profile');
    (d.profiles || []).forEach(p => {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name; sel.appendChild(o);
    });
  });

  // Underlying change → reload expiries
  document.getElementById('sb_underlying').addEventListener('change', () => { loadExpiries(); updateSummary(); });
  // Expiry change → reload chain
  document.getElementById('sb_expiry').addEventListener('change', () => { fetchLiveData(); updateSummary(); });

  ['sb_sl','sb_target'].forEach(id => {
    document.getElementById(id).addEventListener('change', updateSummary);
  });

  // Start with a default short strangle
  loadPreset('strangle');
  loadExpiries();
  updateSummary();

  // Auto-refresh live data every 15s
  liveTimer = setInterval(fetchLiveData, 15000);

  // Close deploy modal on overlay click
  document.getElementById('deployModal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeDeployModal();
  });
});
