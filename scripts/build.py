#!/usr/bin/env python3
"""ダイニーの実績から、これから60日ぶんの売上の上振れ・下振れを予測して docs/data/ に書き出す。

モデルは掛け算式:
    売上 = 水準(店) × 曜日 × 月(季節) × 祝日 × イベント係数の積

係数はすべて実績から毎日学習し直す。実績が少ないイベントは events.json の prior（暫定値）に
引き戻して（縮小推定）、回数が増えるほど実測が効くようにしてある。だから毎日走らせるほど精度が上がる。

公開するのは指数だけで、円の実額は docs/ に一切書き出さない。
"""
import json, math, statistics as st, datetime as dt, pathlib, collections

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
OUT = ROOT / "docs" / "data"
OUT.mkdir(parents=True, exist_ok=True)

WD = "月火水木金土日"
HORIZON = 60          # 何日先まで予測するか
SHRINK = 3.0          # イベント係数を prior に引き戻す強さ（実績n回に対する重み）
RECENT_WEEKS = 12     # 「いまの水準」を測る直近期間

cfg = json.loads((ROOT / "scripts" / "events.json").read_text(encoding="utf-8"))
SHOPS = cfg["shops"]
HOLIDAYS = set(cfg["holidays"])
EVENTS = cfg["events"]

kpi = json.loads((CACHE / "kpi.json").read_text(encoding="utf-8"))
rows = [r for r in kpi["rows"] if r["code"] in SHOPS and r["sales"] > 0]
today = dt.date.fromisoformat(max(r["date"] for r in rows)) if rows else dt.date.today()
# 当日ぶんは営業中で数字が締まっていないので、実績としては前日までを使う
LAST = today

# ---------------------------------------------------------------- 暦とイベントの展開
def calendar_tags(d):
    """暦そのものの効果。年末と正月は普通の祝日とまったく別物なので分けて学習する。"""
    out = []
    md = d[5:]
    if md >= "12-29":
        out.append(("nenmatsu", "年末（12/29-31）", None, None))
    elif md <= "01-03":
        out.append(("nenshi", "年始（1/1-3）", None, None))
    elif "08-13" <= md <= "08-16":
        out.append(("obon", "お盆", None, None))
    elif d in HOLIDAYS:
        # 翌日も休みの祝日と、連休の最終日（翌日から仕事）はまるで別物。実測で2.13倍と1.60倍。
        if is_off((dt.date.fromisoformat(d) + dt.timedelta(1)).isoformat()):
            out.append(("holiday", "祝日（翌日も休み）", None, None))
        else:
            out.append(("holiday-last", "祝日（翌日から仕事）", None, None))
    else:
        nxt = (dt.date.fromisoformat(d) + dt.timedelta(1)).isoformat()
        if dt.date.fromisoformat(d).weekday() <= 3 and nxt in HOLIDAYS:
            out.append(("hol-eve", "翌日が祝日（休前日）", None, None))
    return out


def is_off(d):
    return d in HOLIDAYS or dt.date.fromisoformat(d).weekday() >= 5


DOW_CLASS = ["月〜木", "月〜木", "月〜木", "月〜木", "金", "土日", "土日"]
# 展示会の格。同じ日に何本開いていても「街に展示会客がいる」状態は1つなので、
# いちばん効く1本だけを採る（掛け算にすると3本重なった日が勝手に3倍になってしまう）
BS_RANK = {"bigsight-food": 4, "bigsight-b3": 3, "bigsight-b2": 2, "bigsight-c": 1}


def shop_events(code, d):
    """展示会は曜日で効き方がまるで違う（月〜木は効く／金は効かない／土日は無関係）ので、
    同じ展示会でも曜日の区分ごとに別の係数として学習させる。"""
    w = dt.date.fromisoformat(d).weekday()
    hits, bs = [], []
    for e in EVENTS:
        if e.get("shops") and code not in e["shops"]:
            continue
        if not (e["from"] <= d <= e["to"]):
            continue
        (bs if e["tag"] in BS_RANK else hits).append(e)
    if bs:
        best = max(bs, key=lambda e: BS_RANK[e["tag"]])
        same = [e["name"] for e in bs if BS_RANK[e["tag"]] == BS_RANK[best["tag"]]]
        hits.insert(0, {**best, "tag": f'{best["tag"]}@{DOW_CLASS[w]}',
                        "name": ("／".join(same))[:44] + ("…" if len("／".join(same)) > 44 else "")})
    return [(e["tag"], e["name"], e.get("prior"), e.get("note")) for e in hits]


def day_tags(code, d):
    """その店・その日に効くものを全部 [(tag, name, prior, note)] で返す"""
    return calendar_tags(d) + shop_events(code, d)


def shop_priors(code):
    """この店に効くイベントの暫定値だけを拾う（他店のイベントの prior を持ち込まない）"""
    out = {}
    for e in EVENTS:
        if e.get("prior") and (not e.get("shops") or code in e["shops"]):
            out[e["tag"]] = e["prior"]
    return out

by_shop = collections.defaultdict(dict)
for r in rows:
    by_shop[r["code"]][r["date"]] = r


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(1)


# ---------------------------------------------------------------- 月（季節）係数：全店共通
def month_factors():
    per = collections.defaultdict(list)
    for code, days in by_shop.items():
        vals = [x["sales"] for x in days.values()]
        if len(vals) < 60:
            continue
        base = st.median(vals)
        m_med = collections.defaultdict(list)
        for date, x in days.items():
            m_med[int(date[5:7])].append(x["sales"])
        for m, v in m_med.items():
            if len(v) >= 12:
                per[m].append(st.median(v) / base)
    out = {}
    for m in range(1, 13):
        out[m] = min(1.25, max(0.85, st.median(per[m]))) if per.get(m) else 1.0
    return out


MONTH = month_factors()

# ---------------------------------------------------------------- 店ごとに係数を学習
models = {}
for code in SHOPS:
    days = by_shop.get(code, {})
    if len(days) < 40:
        continue
    recent_from = (LAST - dt.timedelta(weeks=RECENT_WEEKS)).isoformat()

    dates = sorted(days)
    tags_of = {d: [t for t, *_ in day_tags(code, d)] for d in dates}
    priors = shop_priors(code)

    def shrink(obs, prior, k=SHRINK):
        return (len(obs) * st.median(obs) + k * prior) / (len(obs) + k) if obs else prior

    # 曜日・水準・イベント係数はお互いに絡んでいる（月島は9割の日に何かの展示会が当たっている
    # ので「イベントの無い日」だけを基準にすると水準が沈む）。順番に決めず、3周まわして
    # 互いに引き算しながら収束させる。
    coef, nobs = {}, {}
    dow = {w: 1.0 for w in range(7)}
    lvl_cache, level, base_of = {}, None, None

    for _ in range(3):
        def stripped(date):
            """暦・イベントの効果を取り除いた売上"""
            v = days[date]["sales"] / MONTH[dt.date.fromisoformat(date).month]
            for t in tags_of[date]:
                v /= coef.get(t, 1.0)
            return v

        # 曜日の形
        by_w = collections.defaultdict(list)
        for date in dates:
            by_w[dt.date.fromisoformat(date).weekday()].append(stripped(date))
        center = st.median([v for g in by_w.values() for v in g])
        dow = {w: (st.median(by_w[w]) / center if by_w.get(w) else 1.0) for w in range(7)}

        # その時期の水準（開店直後は高く、あとで落ち着く。前後6週の中央値で追う）
        adj = {d: stripped(d) / dow[dt.date.fromisoformat(d).weekday()] for d in dates}
        fallback = st.median(adj.values())
        lvl_cache = {}

        def level_at(date, _adj=adj, _fb=fallback):
            if date in lvl_cache:
                return lvl_cache[date]
            d0 = dt.date.fromisoformat(date)
            for span in (42, 84, 180):
                lo, hi = (d0 - dt.timedelta(span)).isoformat(), (d0 + dt.timedelta(span)).isoformat()
                w = [_adj[k] for k in dates if lo <= k <= hi]
                if len(w) >= 12:
                    lvl_cache[date] = st.median(w)
                    return lvl_cache[date]
            lvl_cache[date] = _fb
            return _fb

        recent = [adj[k] for k in dates if k >= recent_from]
        level = st.median(recent) if len(recent) >= 8 else fallback

        def base_of(date, _lv=level_at):
            d0 = dt.date.fromisoformat(date)
            return _lv(date) * dow[d0.weekday()] * MONTH[d0.month]

        # 暦とイベントの係数を残差から学習し直す
        tag_r = collections.defaultdict(list)
        for date in dates:
            if not tags_of[date]:
                continue
            r = days[date]["sales"] / base_of(date)
            share = 1.0 / len(tags_of[date])   # 複数の要因が重なる日は等分に割り当てる
            for t in tags_of[date]:
                tag_r[t].append(r ** share)

        # 展示会は「展示会ぜんぶ → 格 → 格×曜日区分」と段階的に絞る。
        # いきなり細かく割ると実績3件の枠が暴れるので、上の階層へ引き戻す。
        c_all = shrink([v for t, g in tag_r.items() if t.startswith("bigsight-") for v in g], 1.0, 12)
        c_fam = {fam: shrink([v for t, g in tag_r.items() if t.startswith(fam + "@") for v in g], c_all, 12)
                 for fam in BS_RANK}

        coef, nobs = {}, {}
        for tag in set(list(tag_r.keys()) + list(priors.keys())):
            obs = tag_r.get(tag, [])
            fam = tag.split("@")[0]
            prior = c_fam[fam] if fam in BS_RANK else priors.get(tag, 1.0)
            coef[tag] = min(2.5, max(0.4, shrink(obs, prior, 12 if fam in BS_RANK else SHRINK)))
            nobs[tag] = len(obs)

    models[code] = {
        "level": level, "dow": dow, "coef": coef, "nobs": nobs, "base_of": base_of,
        "median": st.median([x["sales"] for x in days.values()]),
        "days": len(days),
    }


def predict(code, date):
    """(予測売上, 平常比, 効いた要因) を返す。平常比は「その時期のその曜日の平常＝1.00」"""
    m = models[code]
    d = dt.date.fromisoformat(date)
    base = m["level"] * m["dow"][d.weekday()] * MONTH[d.month]
    idx, why = 1.0, []
    for tag, name, prior, note in day_tags(code, date):
        c = m["coef"].get(tag, prior or 1.0)
        idx *= c
        why.append({"tag": tag, "name": name, "coef": round(c, 2), "n": m["nobs"].get(tag, 0), "note": note})
    return base * idx, idx, why


def rank_of(idx):
    if idx >= 1.30: return "up2"
    if idx >= 1.12: return "up1"
    if idx <= 0.80: return "down2"
    if idx <= 0.92: return "down1"
    return "flat"


# ---------------------------------------------------------------- 予測を出す
start = LAST + dt.timedelta(1)
days_out = []
for d in daterange(start, start + dt.timedelta(HORIZON - 1)):
    date = d.isoformat()
    entry = {"date": date, "w": d.weekday(), "hol": date in HOLIDAYS, "shops": {}}
    for code in models:
        pred, idx, why = predict(code, date)
        entry["shops"][code] = {
            "lvl": round(pred / models[code]["median"] * 100),
            "idx": round(idx, 3),
            "rank": rank_of(idx),
            "why": why,
        }
    days_out.append(entry)

# ---------------------------------------------------------------- 過去90日の実績（指数のみ）
hist = []
for d in daterange(LAST - dt.timedelta(89), LAST):
    date = d.isoformat()
    e = {"date": date, "w": d.weekday(), "hol": date in HOLIDAYS, "shops": {}}
    for code in models:
        x = by_shop[code].get(date)
        if not x:
            continue
        m = models[code]
        base = m["base_of"](date)
        e["shops"][code] = {"lvl": round(x["sales"] / m["median"] * 100), "idx": round(x["sales"] / base, 3)}
    if e["shops"]:
        hist.append(e)

# ---------------------------------------------------------------- 予測ログと精度
LOG = CACHE / "prediction_log.json"
log = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else {}
run_date = LAST.isoformat()
log[run_date] = {
    code: {d["date"]: round(predict(code, d["date"])[0]) for d in days_out[:21]}
    for code in models
}
# 実績が出そろった古いログは残しつつ、200日より前は捨てる
cutoff = (LAST - dt.timedelta(200)).isoformat()
log = {k: v for k, v in log.items() if k >= cutoff}
LOG.write_text(json.dumps(log), encoding="utf-8")

# 誤差を集計（イベント込みモデル vs 曜日だけのベンチマーク）
errs = collections.defaultdict(list)          # horizon -> [誤差率]
per_shop = collections.defaultdict(list)
recent_pairs = []
for made_on, shops_pred in log.items():
    for code, dates in shops_pred.items():
        if code not in models:
            continue
        for date, pred in dates.items():
            actual = by_shop[code].get(date)
            if not actual or date > LAST.isoformat():
                continue
            h = (dt.date.fromisoformat(date) - dt.date.fromisoformat(made_on)).days
            if h < 1:
                continue
            err = abs(pred - actual["sales"]) / actual["sales"]
            bucket = "1-3日前" if h <= 3 else ("4-7日前" if h <= 7 else "8日以上前")
            errs[bucket].append(err)
            if h <= 7:
                per_shop[code].append(err)
                recent_pairs.append({"date": date, "code": code, "h": h, "err": round(err * 100, 1)})

accuracy = {
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "byHorizon": {k: {"n": len(v), "mape": round(st.mean(v) * 100, 1)} for k, v in sorted(errs.items())},
    "byShop": {k: {"n": len(v), "mape": round(st.mean(v) * 100, 1)} for k, v in per_shop.items()},
    "recent": sorted(recent_pairs, key=lambda x: x["date"], reverse=True)[:120],
    "note": "誤差率＝|予測−実績|÷実績。予測ログが貯まるほどイベント係数が実測に寄り、数字が下がっていく想定。",
}

# ---------------------------------------------------------------- 書き出し
meta_shops = [{"code": c, "name": SHOPS[c]["name"], "area": SHOPS[c]["area"],
               "days": models[c]["days"],
               "dow": [round(models[c]["dow"][w], 2) for w in range(7)]}
              for c in models]

upcoming = []
for e in EVENTS:
    if e["to"] < start.isoformat() or e["from"] > (start + dt.timedelta(HORIZON)).isoformat():
        continue
    tgt = e.get("shops") or list(models)
    learned = {}
    for c in tgt:
        if c not in models:
            continue
        # 会期の初日に実際に付くタグ（展示会は曜日区分ごとに別係数）を代表として見せる
        rep = next((t for t, *_ in shop_events(c, max(e["from"], start.isoformat()) if e["to"] >= start.isoformat() else e["from"])
                    if t.startswith(e["tag"])), e["tag"])
        learned[c] = {"coef": round(models[c]["coef"].get(rep, e.get("prior") or 1.0), 2),
                      "n": models[c]["nobs"].get(rep, 0)}
    upcoming.append({**{k: e[k] for k in ("from", "to", "tag", "name") if k in e},
                     "shops": [c for c in tgt if c in models],
                     "note": e.get("note"), "learned": learned})
upcoming.sort(key=lambda x: x["from"])

(OUT / "forecast.json").write_text(json.dumps({
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "actualThrough": LAST.isoformat(),
    "shops": meta_shops,
    "month": {str(k): round(v, 3) for k, v in MONTH.items()},
    "days": days_out,
    "history": hist,
    "events": upcoming,
}, ensure_ascii=False), encoding="utf-8")

(OUT / "accuracy.json").write_text(json.dumps(accuracy, ensure_ascii=False), encoding="utf-8")

print(f"[build] 実績 {LAST} まで / {len(models)}店 / 予測 {len(days_out)}日 / ログ {len(log)}回ぶん")
for c in models:
    m = models[c]
    top = sorted(m["coef"].items(), key=lambda kv: -abs(kv[1] - 1))[:3]
    print(f"  {c} {SHOPS[c]['name']:<12} 実績{m['days']:>3}日  " +
          " ".join(f"{t}={v:.2f}(n={m['nobs'].get(t,0)})" for t, v in top))
if accuracy["byHorizon"]:
    print("  誤差率:", ", ".join(f"{k} {v['mape']}%(n={v['n']})" for k, v in accuracy["byHorizon"].items()))
