"""Fast backtest of Next Move Prediction Engine."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.chart import get_candles

def ema(data, p):
    k = 2/(p+1); e=[data[0]]
    for v in data[1:]: e.append(v*k+e[-1]*(1-k))
    return e

def predict_score(closes, highs, lows, i):
    if i < 55: return 0, 0
    e14 = ema(closes[:i+1], 14)
    e50 = ema(closes[:i+1], 50)
    trs = [highs[0]-lows[0]]
    for j in range(1, i+1):
        trs.append(max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1])))
    atr = sum(trs[-14:])/14
    
    price = closes[i]
    score = 0
    if price > e14[-1]: score += 20
    else: score -= 20
    if price > e50[-1]: score += 15
    else: score -= 15
    if len(e14)>3 and e14[-1] > e14[-3]: score += 10
    else: score -= 10
    
    # RSI
    deltas = [closes[j]-closes[j-1] for j in range(max(1,i-13), i+1)]
    ag = sum(max(d,0) for d in deltas)/max(len(deltas),1)
    al = sum(max(-d,0) for d in deltas)/max(len(deltas),1)
    rsi = 100 - 100/(1+(ag/al if al>0 else 100))
    if rsi > 60: score += 10
    elif rsi < 40: score -= 10
    
    if i > 5:
        rc = (closes[i]-closes[i-5])/closes[i-5]*100
        if rc > 0.5: score += 15
        elif rc < -0.5: score -= 15
    if highs[i]>highs[i-1] and lows[i]>lows[i-1]: score += 10
    elif highs[i]<highs[i-1] and lows[i]<lows[i-1]: score -= 10
    
    return max(-100, min(100, score)), atr

ASSETS = ['BTC','ETH','NIFTY','BANKNIFTY','SENSEX']
TFS = ['15m','1h','1d']
THRESHOLDS = [0, 25, 40, 50, 65]
LOOKAHEADS = [3, 5, 10]

results = []
for sym in ASSETS:
    for tf in TFS:
        candles = get_candles(sym, tf)
        if not candles or len(candles) < 100: continue
        closes = [c['c'] for c in candles]
        highs = [c['h'] for c in candles]
        lows = [c['l'] for c in candles]
        lotSize = 0.001 if sym=='BTC' else 0.01 if sym=='ETH' else 1
        lots = 10

        for ms in THRESHOLDS:
            for la in LOOKAHEADS:
                w=l=pnl=0
                step = max(1, la)  # don't overlap trades
                i = 55
                while i < len(candles) - la - 1:
                    score, atr = predict_score(closes, highs, lows, i)
                    if abs(score) < ms or atr <= 0:
                        i += 1; continue
                    bull = score > 0
                    tp_d = atr; sl_d = atr * 0.5
                    price = closes[i]
                    hit = None
                    for c in candles[i+1:i+1+la]:
                        if bull:
                            if c['l'] <= price - sl_d: hit='sl'; break
                            if c['h'] >= price + tp_d: hit='tp'; break
                        else:
                            if c['h'] >= price + sl_d: hit='sl'; break
                            if c['l'] <= price - tp_d: hit='tp'; break
                    if hit=='tp': w+=1; pnl+=tp_d*lots*lotSize
                    elif hit=='sl': l+=1; pnl-=sl_d*lots*lotSize
                    i += step
                
                t = w+l
                if t < 5: continue
                results.append({'sym':sym,'tf':tf,'ms':ms,'la':la,'w':w,'l':l,'t':t,
                               'wr':round(w/t*100,1),'pnl':round(pnl,2)})

# Find best combos
print("="*100)
print("BEST CONFIGURATIONS (sorted by PnL)")
print("="*100)
print(f"{'Sym':>10} {'TF':<4} {'MinScore':<9} {'Lookahead':<10} {'Trades':<7} {'W':<5} {'L':<5} {'WR%':<7} {'PnL':<14}")
print("-"*80)
for r in sorted(results, key=lambda x: -x['pnl'])[:25]:
    s = '✅' if r['pnl']>0 else '❌'
    print(f"{r['sym']:>10} {r['tf']:<4} >={r['ms']:<7} {r['la']:<10} {r['t']:<7} {r['w']:<5} {r['l']:<5} {r['wr']:<7} {r['pnl']:<+14.2f} {s}")

# Summary by threshold
print("\n" + "="*100)
print("SUMMARY BY MIN SCORE THRESHOLD")
print("="*100)
for ms in THRESHOLDS:
    rows = [r for r in results if r['ms']==ms]
    if not rows: continue
    avg_wr = sum(r['wr'] for r in rows)/len(rows)
    total_pnl = sum(r['pnl'] for r in rows)
    prof = sum(1 for r in rows if r['pnl']>0)
    print(f"  Score >= {ms:<3}  AvgWR: {avg_wr:5.1f}% | Combos: {prof}/{len(rows)} profitable | TotalPnL: {total_pnl:>+12.2f}")

# Summary by timeframe
print("\n" + "="*100)
print("SUMMARY BY TIMEFRAME")
print("="*100)
for tf in TFS:
    rows = [r for r in results if r['tf']==tf]
    if not rows: continue
    avg_wr = sum(r['wr'] for r in rows)/len(rows)
    total_pnl = sum(r['pnl'] for r in rows)
    prof = sum(1 for r in rows if r['pnl']>0)
    print(f"  {tf:<4}  AvgWR: {avg_wr:5.1f}% | Combos: {prof}/{len(rows)} profitable | TotalPnL: {total_pnl:>+12.2f}")

# Summary by lookahead
print("\n" + "="*100)
print("SUMMARY BY LOOKAHEAD (candles to hold)")
print("="*100)
for la in LOOKAHEADS:
    rows = [r for r in results if r['la']==la]
    if not rows: continue
    avg_wr = sum(r['wr'] for r in rows)/len(rows)
    total_pnl = sum(r['pnl'] for r in rows)
    prof = sum(1 for r in rows if r['pnl']>0)
    print(f"  {la} candles  AvgWR: {avg_wr:5.1f}% | Combos: {prof}/{len(rows)} profitable | TotalPnL: {total_pnl:>+12.2f}")
