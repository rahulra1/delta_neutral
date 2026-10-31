"""
Draw trendline trades as candlestick SVGs in one HTML file (no dependencies), so the
trendlines can be checked by eye.

  python backtest/trendline_chart.py                 # improved config, last 6 signals
  python backtest/trendline_chart.py --live --n 8    # the live code's trendlines
Output: backtest/trendline_examples.html
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
import trendline_optimize as T  # noqa: E402

IMPROVED = dict(lb=5, ema=100, vol=0, stop='swing', minrisk=0.005, rr=2, win=6, tlq='any')
W, H, PAD = 900, 360, 40


def svg(cs, a, b, line, sig, ema=None, title=''):
    seg = cs[a:b + 1]
    lo = min(x['l'] for x in seg + [{'l': sig['sl']}, {'l': sig['tp']}])
    hi = max(x['h'] for x in seg + [{'h': sig['sl']}, {'h': sig['tp']}])
    sx = lambda i: PAD + (i - a) * (W - 2 * PAD) / max(1, b - a)  # noqa: E731
    sy = lambda p: PAD + (hi - p) * (H - 2 * PAD) / max(1e-9, hi - lo)  # noqa: E731
    bw = max(1.5, (W - 2 * PAD) / (b - a + 1) * 0.6)
    out = [f'<svg width="{W}" height="{H}" style="background:#0f172a;margin:6px 0">',
           f'<text x="{PAD}" y="20" fill="#e2e8f0" font-size="13" font-family="monospace">{title}</text>']
    for i in range(a, b + 1):
        x = cs[i]
        col = '#22c55e' if x['c'] >= x['o'] else '#ef4444'
        out.append(f'<line x1="{sx(i):.1f}" x2="{sx(i):.1f}" y1="{sy(x["h"]):.1f}" y2="{sy(x["l"]):.1f}" stroke="{col}"/>')
        y1, y2 = sy(max(x['o'], x['c'])), sy(min(x['o'], x['c']))
        out.append(f'<rect x="{sx(i) - bw / 2:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{max(1, y2 - y1):.1f}" fill="{col}"/>')
    if ema:
        pts = ' '.join(f'{sx(i):.1f},{sy(ema[i]):.1f}' for i in range(a, b + 1))
        out.append(f'<polyline points="{pts}" fill="none" stroke="#a78bfa" stroke-width="1.2" opacity=".8"/>')
    i1, p1, i2, p2, t = line
    slope = (p2 - p1) / (i2 - i1)
    out.append(f'<line x1="{sx(i1):.1f}" y1="{sy(p1):.1f}" x2="{sx(t):.1f}" y2="{sy(p2 + slope * (t - i2)):.1f}" '
               f'stroke="#facc15" stroke-width="2"/>')
    for i, p in ((i1, p1), (i2, p2)):
        out.append(f'<circle cx="{sx(i):.1f}" cy="{sy(p):.1f}" r="4" fill="none" stroke="#facc15" stroke-width="2"/>')
    ent = sig['entry_i']
    for lvl, col, lab in ((sig['sl'], '#ef4444', 'SL'), (sig['tp'], '#22c55e', 'TP'), (sig['entry'], '#38bdf8', 'entry')):
        out.append(f'<line x1="{sx(ent):.1f}" x2="{sx(b):.1f}" y1="{sy(lvl):.1f}" y2="{sy(lvl):.1f}" stroke="{col}" stroke-dasharray="4 3"/>')
        out.append(f'<text x="{sx(b) + 3:.1f}" y="{sy(lvl) + 4:.1f}" fill="{col}" font-size="11" font-family="monospace">{lab}</text>')
    out.append(f'<text x="{sx(t):.1f}" y="{sy(cs[t]["c"]) - 10:.1f}" fill="#38bdf8" font-size="16">{"▲" if sig["side"] == "buy" else "▼"}</text>')
    out.append('</svg>')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=6)
    ap.add_argument('--live', action='store_true', help="draw the live code's trendlines instead")
    a = ap.parse_args()
    cs = rep.fetch_delta('1h', 1100)
    ctx = T.Ctx(cs)
    items = []
    if a.live:
        from api.chart import calc_ema_trendline_breakout
        idx = {c['t']: i for i, c in enumerate(cs)}
        sigs = calc_ema_trendline_breakout(cs[-3000:])['signals'][-a.n:]
        for s in sigs:
            i1, i2, t = idx[s['tl_start_time']], idx[s['tl_end_time']], idx[s['time']]
            items.append(((i1, s['tl_start_price'], i2, s['tl_end_price'], t),
                          {'side': s['type'], 'sl': s['sl'], 'tp': s['tp1'], 'entry': s['price'], 'entry_i': t},
                          ctx.ema[200]))
        cfg_name = 'LIVE code (lb5, EMA200, vol>1.5x, stop = breakout bar, 2R)'
    else:
        cfg = IMPROVED
        trace = []
        sg = T.gen_signals(ctx, cfg['lb'], cfg['ema'], cfg['vol'], cfg['stop'], cfg['win'] * cfg['lb'], cfg['tlq'], trace)
        lines = {(t, side): (i1, p1, i2, p2, t) for t, side, i1, i2, p1, p2, _ in trace}
        for t, side, sl in sg[-a.n:]:
            ref = cs[t]['c']
            risk = abs(ref - sl)
            if risk / ref < cfg['minrisk']:
                continue
            tp = ref + cfg['rr'] * risk if side == 'buy' else ref - cfg['rr'] * risk
            items.append((lines[(t, side)], {'side': side, 'sl': sl, 'tp': tp, 'entry': ref, 'entry_i': t},
                          ctx.ema[cfg['ema']]))
        cfg_name = 'IMPROVED (' + T.name(cfg) + ')'
    html = ['<html><body style="background:#020617;color:#e2e8f0;font-family:sans-serif">',
            f'<h3>BTC 1h trendline trades — {cfg_name}</h3>',
            '<p>yellow = trendline through the two circled swing points; purple = EMA; '
            '▲/▼ = breakout close; dashed = entry / SL / TP</p>']
    for line, sig, e in items:
        i1, _, _, _, t = line
        a0, b0 = max(0, i1 - 10), min(len(cs) - 1, t + 40)
        html.append(svg(cs, a0, b0, line, sig, e, f'{sig["side"].upper()}  {rep.ts(cs[t]["t"])} UTC'))
    html.append('</body></html>')
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trendline_examples.html')
    open(path, 'w').write('\n'.join(html))
    print(f'wrote {path} ({len(items)} charts)')


if __name__ == '__main__':
    main()
