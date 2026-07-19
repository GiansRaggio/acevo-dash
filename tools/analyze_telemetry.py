#!/usr/bin/env python3
"""Analisis offline de la telemetria grabada por acevo_telemetry.py (AC EVO).

Lee una carpeta de sesion (telemetry/<...>/) con su summary.jsonl y, opcional, las
trazas L*.csv.gz. Portado del analizador de AMS2: reusa los algoritmos (delta por
distancia, deteccion de curvas, coasting, balance) que son agnosticos del sim.

Diferencias con la version AMS2 (limitaciones de EVO): la vuelta se identifica por
`uid` (EVO no da numero de vuelta); no hay sectores, wear, compound ni zonas de temp
de goma -> esos analisis se omiten. Gomas usa presion/nucleo/camber (canales que EVO
si da). Metadata (pista) sale de name_candidates del static.

Solo stdlib. Ejemplos:
    python tools/analyze_telemetry.py                 # ultima sesion (resumen)
    python tools/analyze_telemetry.py --list          # lista sesiones
    python tools/analyze_telemetry.py --vs 3          # vuelta 3 vs tu mejor (delta por distancia)
    python tools/analyze_telemetry.py --vs 3 5        # vuelta 3 vs 5
    python tools/analyze_telemetry.py --lap 3         # inspecciona la vuelta 3 canal por canal
    python tools/analyze_telemetry.py --balance       # sobre/subviraje por curva
    python tools/analyze_telemetry.py --tyres         # presion/temp/camber por rueda
    python tools/analyze_telemetry.py --insights      # 2-4 consejos accionables
"""
import argparse
import csv
import glob
import gzip
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEM = os.path.join(HERE, "telemetry")
CORNERS = ("FL", "FR", "RL", "RR")


def _fmt_t(s):
    if s is None:
        return "  --:--.---"
    m, sec = divmod(s, 60)
    return f"{int(m):>3d}:{sec:06.3f}"


def _read_jsonl(path):
    """Lee un .jsonl tolerando una ULTIMA linea truncada (append-only no atomico: un
    taskkill a mitad deja la ultima linea partida). [] si no existe."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                continue
            raise
    return out


def _sessions():
    if not os.path.isdir(TELEM):
        return []
    subs = [d for d in glob.glob(os.path.join(TELEM, "*")) if os.path.isdir(d)]
    return sorted(subs, key=os.path.getmtime)


def _load(folder):
    laps = _read_jsonl(os.path.join(folder, "summary.jsonl"))
    meta = {}
    mf = os.path.join(folder, "session.json")
    if os.path.exists(mf):
        meta = json.load(open(mf, encoding="utf-8"))
    return meta, laps


def _track_name(meta):
    """Nombre de pista best-effort desde name_candidates (el mas largo suele ser la pista)."""
    names = [s for s in meta.get("name_candidates", []) if any(c.isalpha() for c in s)]
    return max(names, key=len) if names else "?"


def _slope(ys):
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def _read_trace(path):
    if not path or not os.path.exists(path):
        return None
    data = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for c in rdr.fieldnames:
            data[c] = []
        for row in rdr:
            for c in rdr.fieldnames:
                try:
                    data[c].append(float(row[c]))
                except (ValueError, TypeError):
                    data[c].append(0.0)
    # lap_dist de EVO (current_km) es ACUMULADO de sesion Y de resolucion gruesa (~50m/paso) ->
    # _mono lo colapsa a ~138 puntos y mata la deteccion de curvas. Se REEMPLAZA integrando la
    # velocidad (distancia por-muestra, fina, 0-based por vuelta): dist += v*dt. Estandar de
    # telemetria cuando no hay buen canal de distancia. Arregla trazas ya grabadas y futuras.
    sp, tt = data.get("speed_kmh"), data.get("t")
    if sp and tt and len(sp) == len(tt) and len(sp) > 1:
        dist = [0.0]
        for i in range(1, len(sp)):
            dt = tt[i] - tt[i - 1]
            if dt <= 0:                    # jitter del reloj de vuelta (t hacia atras) -> sin distancia
                dt = 0.0
            elif dt > 1.0:                 # gap real del reader -> periodo nominal 50Hz
                dt = 0.02
            dist.append(dist[-1] + sp[i] / 3.6 * dt)
        data["lap_dist"] = dist
    return data


def load_trace(folder, uid, laps=None):
    """Traza de una vuelta por UID (identidad unica del recorder EVO)."""
    if laps is None:
        _, laps = _load(folder)
    m = [l for l in laps if l.get("uid") == uid and l.get("trace")]
    if not m:
        return None
    return _read_trace(os.path.join(folder, m[0]["trace"]))


def _lap_trace(folder, rec):
    return _read_trace(os.path.join(folder, rec["trace"])) if rec and rec.get("trace") else None


def _mono(dist, *cols):
    """Recorta a la porcion de distancia estrictamente creciente (evita el wrap de meta)."""
    out_d, out = [], [[] for _ in cols]
    last = -1e9
    for i, d in enumerate(dist):
        if d > last:
            out_d.append(d)
            for k, c in enumerate(cols):
                out[k].append(c[i])
            last = d
    return (out_d, *out)


def _interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    f = (x - xs[lo]) / span if span else 0
    return ys[lo] + f * (ys[hi] - ys[lo])


def _smooth(v, w=15):
    if len(v) < w or w < 2:
        return list(v)
    half = w // 2
    out = []
    for i in range(len(v)):
        a, b = max(0, i - half), min(len(v), i + half + 1)
        out.append(sum(v[a:b]) / (b - a))
    return out


def _rescale(da, target_len):
    """Reescala el eje de distancia da para que termine en target_len (alinea dos vueltas cuya
    distancia integrada deriva un poco entre si)."""
    if not da or da[-1] <= 0:
        return da
    k = target_len / da[-1]
    return [x * k for x in da]


def _corners(dist, spd, min_prom=15.0, min_gap_m=80.0):
    """Detecta apex de curvas como minimos locales prominentes de velocidad."""
    s = _smooth(spd, 21)
    n = len(s)
    cand = []
    for i in range(2, n - 2):
        if s[i] <= s[i - 1] and s[i] <= s[i + 1]:
            lmax = s[i]
            j = i
            while j > 0 and s[j - 1] >= s[j]:
                lmax = max(lmax, s[j - 1]); j -= 1
            rmax = s[i]
            j = i
            while j < n - 1 and s[j + 1] >= s[j]:
                rmax = max(rmax, s[j + 1]); j += 1
            prom = min(lmax, rmax) - s[i]
            if prom >= min_prom:
                cand.append((dist[i], s[i], prom))
    cand.sort()
    merged = []
    for d, v, p in cand:
        if merged and d - merged[-1][0] < min_gap_m:
            if v < merged[-1][1]:
                merged[-1] = (d, v, p)
        else:
            merged.append((d, v, p))
    return [{"n": i + 1, "apex": round(d), "vmin": round(v, 1)}
            for i, (d, v, p) in enumerate(merged)]


def _coasting(dist, thr, brk, spd, min_m=8.0):
    """Tramos de coasting (gas~0 y freno~0 en movimiento)."""
    zones = []
    i, n = 0, len(dist)
    while i < n:
        if thr[i] < 0.05 and brk[i] < 0.05 and spd[i] > 30:
            j = i
            while j < n and thr[j] < 0.05 and brk[j] < 0.05 and spd[j] > 30:
                j += 1
            length = dist[j - 1] - dist[i]
            if length >= min_m:
                zones.append((dist[i], round(length)))
            i = j
        else:
            i += 1
    return zones


def clean_laps(folder):
    """Vueltas representativas: prefiere clean=True (manejo continuo) y lap_time <= mejor*1.03.
    Devuelve los REGISTROS (con uid+trace). Cae a todas si ninguna trae clean=True."""
    _, laps = _load(folder)
    timed = [l for l in laps if l.get("lap_time")]
    if not timed:
        return {"best_lap_time": None, "n_clean": 0, "clean": [], "ref": None}
    cont = [l for l in timed if l.get("clean", True)]   # clean ausente (sesiones viejas) -> incluir
    pool = cont if cont else timed
    best = min(l["lap_time"] for l in pool)
    clean = sorted((l for l in pool if l["lap_time"] <= best * 1.03), key=lambda l: l["lap_time"])
    return {"best_lap_time": best, "n_clean": len(clean), "clean": clean, "ref": clean[0] if clean else None}


# ---------------- reportes ----------------
def report_session(folder):
    meta, laps = _load(folder)
    print(f"\n=== {os.path.basename(folder)} ===")
    print(f"  {_track_name(meta)} · {meta.get('rate_hz', '?')}Hz"
          + (f" · pista ~{meta['track_length_m']:.0f}m" if meta.get("track_length_m") else ""))
    if not laps:
        print("  (sin vueltas validas registradas todavia — maneja hotlaps en circuito)")
        return
    print(f"\n  {'UID':>3} {'TIME':>11} {'CLEAN':>6} {'MUES':>5} {'FUEL':>6} {'TYRE°avg':>9} {'BRK°max':>8}")
    for l in laps:
        tavg = max(l["tyre_temp_avg"]) if l.get("tyre_temp_avg") else 0.0
        bmax = max(l["brake_temp_max"]) if l.get("brake_temp_max") else 0.0
        cl = "si" if l.get("clean", True) else "NO"
        uid = l.get("uid"); uid = str(uid) if uid is not None else "?"
        smp = l.get("samples") or 0
        print(f"  {uid:>3} {_fmt_t(l.get('lap_time'))} {cl:>6} {smp:>5} "
              f"{l.get('fuel_start', 0) or 0:>5.1f}L {tavg:>7.0f}° {bmax:>7.0f}°")
    times = [l["lap_time"] for l in laps if l.get("lap_time") and l.get("clean", True)]
    if not times:
        times = [l["lap_time"] for l in laps if l.get("lap_time")]
    print("\n  --- resumen (vueltas clean) ---")
    if times:
        print(f"  vueltas         : {len(laps)} guardadas · {len(times)} clean")
        print(f"  mejor / mediana : {_fmt_t(min(times)).strip()} / {_fmt_t(st.median(times)).strip()}")
        if len(times) >= 2:
            sd = st.pstdev(times)
            tag = "excelente" if sd < 0.5 else "buena" if sd < 1.0 else "regular" if sd < 2.0 else "dispersa"
            print(f"  consistencia    : sigma {sd:.2f}s ({tag})  ·  tendencia {_slope(times):+.2f}s/vuelta")
    print("\n  (sectores/wear/compound no disponibles en EVO por SHM; ver --vs y --balance)")


def report_vs(folder, lap_a, lap_b):
    _, laps = _load(folder)
    times = {l.get("uid"): l.get("lap_time") for l in laps}
    if lap_b is None:                       # default: contra la mejor CON traza (evita ref rota)
        valid = [(l["uid"], l["lap_time"]) for l in laps
                 if l.get("lap_time") and l.get("uid") is not None and l.get("trace")]
        lap_b = min(valid, key=lambda x: x[1])[0] if valid else None
    ta, tb = load_trace(folder, lap_a, laps), load_trace(folder, lap_b, laps)
    if not ta or not tb or not ta.get("speed_kmh") or not tb.get("speed_kmh"):
        print(f"  faltan trazas (uids con traza: {[l['uid'] for l in laps if l.get('trace')]})")
        return
    da, sa = _mono(ta["lap_dist"], ta["speed_kmh"])[:2]
    db, sb = _mono(tb["lap_dist"], tb["speed_kmh"])[:2]
    da2, t_a = _mono(ta["lap_dist"], ta["t"])
    db2, t_b = _mono(tb["lap_dist"], tb["t"])
    if not da or not db or not da2 or not db2:
        print("  traza vacia o corrupta (sin muestras utiles)")
        return
    L = db2[-1]                             # alinear el eje de A al largo de B (la distancia integrada
    da, da2 = _rescale(da, L), _rescale(da2, L)   # deriva ~3% entre vueltas -> sin esto el delta se desalinea)
    print(f"\n=== vuelta {lap_a} vs {lap_b} (ref) · {os.path.basename(folder)} ===")
    print(f"  lap-time: {_fmt_t(times.get(lap_a)).strip()} vs {_fmt_t(times.get(lap_b)).strip()}"
          f"  (delta {(times.get(lap_a, 0) - times.get(lap_b, 0)):+.3f}s)")
    end = min(da2[-1], db2[-1])
    grid = list(range(0, int(end), 50))
    deltas = [_interp(da2, t_a, x) - _interp(db2, t_b, x) for x in grid]
    segs = [(grid[i], deltas[i] - deltas[i - 1]) for i in range(1, len(deltas))]
    worst = sorted(segs, key=lambda s: s[1], reverse=True)[:4]
    best = sorted(segs, key=lambda s: s[1])[:3]
    corners = _corners(db, sb)

    def near(d):
        c = min(corners, key=lambda c: abs(c["apex"] - d)) if corners else None
        return f"~T{c['n']}" if c and abs(c["apex"] - d) < 150 else f"{d}m"
    print("  donde PIERDES tiempo:")
    for d, dv in worst:
        if dv > 0.02:
            print(f"   {near(d):>6} (dist {d}m): +{dv:.2f}s")
    print("  donde GANAS:")
    for d, dv in best:
        if dv < -0.02:
            print(f"   {near(d):>6} (dist {d}m): {dv:.2f}s")
    print("  velocidad de apex por curva (A vs ref):")
    ca = _corners(da, sa)
    for c in corners:
        ma = min((x for x in ca if abs(x["apex"] - c["apex"]) < 120),
                 key=lambda x: abs(x["apex"] - c["apex"]), default=None)
        if ma:
            dv = ma["vmin"] - c["vmin"]
            flag = "" if abs(dv) < 2 else ("  <= mas lento" if dv < 0 else "  (mas rapido)")
            print(f"   T{c['n']:<2} apex {c['apex']:>5}m: {ma['vmin']:>5.1f} vs {c['vmin']:>5.1f} km/h ({dv:+.1f}){flag}")


def report_lap(folder, uid):
    meta, laps = _load(folder)
    match = [l for l in laps if l.get("uid") == uid]
    if not match:
        print(f"  no hay vuelta uid {uid}. Uids: {[l.get('uid') for l in laps]}")
        return
    data = load_trace(folder, uid, laps)
    if not data:
        print(f"  falta la traza de la vuelta {uid}")
        return
    n = len(data.get("t", []))
    print(f"\n=== vuelta uid {uid} · {match[0].get('trace')} · {n} muestras "
          f"· {_fmt_t(match[0].get('lap_time')).strip()} ===")
    key = ["speed_kmh", "rpm", "throttle", "brake", "steer", "accel_lat", "accel_long",
           "fuel_l", "tyre_temp_FL", "tyre_temp_RR", "tyre_press_FL", "camber_FL", "brake_temp_FL"]
    print(f"  {'canal':>14} {'min':>9} {'avg':>9} {'max':>9}")
    for c in key:
        v = data.get(c)
        if v:
            print(f"  {c:>14} {min(v):>9.2f} {sum(v) / len(v):>9.2f} {max(v):>9.2f}")
    if data.get("speed_kmh"):
        thr = data.get("throttle", [])
        brk = data.get("brake", [])
        print(f"\n  vel. maxima: {max(data['speed_kmh']):.1f} km/h  ·  "
              f"% a fondo: {100 * sum(1 for x in thr if x > 0.98) / max(1, len(thr)):.0f}%  ·  "
              f"% frenando: {100 * sum(1 for x in brk if x > 0.05) / max(1, len(brk)):.0f}%")
    if data.get("lap_dist") and data.get("speed_kmh"):
        z = [0.0] * len(data["speed_kmh"])
        d, s, th, br = _mono(data["lap_dist"], data["speed_kmh"],
                             data.get("throttle", z), data.get("brake", z))
        cs = _corners(d, s)
        if cs:
            print(f"\n  curvas detectadas: {len(cs)}")
            for c in cs:
                print(f"   T{c['n']:<2} apex {c['apex']:>5}m  vmin {c['vmin']:>5.1f} km/h")
        zs = _coasting(d, th, br, s)
        if zs:
            print(f"  coasting: {len(zs)} tramos · {sum(z[1] for z in zs)}m total · mayor {max(z[1] for z in zs)}m")


def balance_struct(folder):
    """Balance por curva (slip trasero vs delantero -> sobre/subviraje) sobre las vueltas clean."""
    cl = clean_laps(folder)
    if cl["n_clean"] < 2:
        return None
    traces = [(rec, _lap_trace(folder, rec)) for rec in cl["clean"]]
    traces = [(rec, t) for rec, t in traces if t and "tyre_slip_RL" in t and t.get("lap_dist")]
    if len(traces) < 2:
        return None
    rt = traces[0][1]
    if not rt.get("lap_dist") or rt["lap_dist"][-1] <= 0:
        return None
    ref_len = rt["lap_dist"][-1]           # normalizar cada traza a este largo (alinea las ventanas ±60m)
    for _, t in traces:
        ld = t.get("lap_dist")
        if ld and ld[-1] > 0:
            t["lap_dist"] = _rescale(ld, ref_len)
    d, s = _mono(rt["lap_dist"], rt["speed_kmh"])[:2]
    corners = []
    for c in _corners(d, s):
        fr, re = [], []
        for _, t in traces:
            idx = [i for i, x in enumerate(t["lap_dist"]) if c["apex"] - 60 <= x <= c["apex"] + 60]
            if idx:
                fr.append(st.median([(abs(t["tyre_slip_FL"][i]) + abs(t["tyre_slip_FR"][i])) / 2 for i in idx]))
                re.append(st.median([(abs(t["tyre_slip_RL"][i]) + abs(t["tyre_slip_RR"][i])) / 2 for i in idx]))
        if len(fr) < 1:
            continue
        f, r = st.median(fr), st.median(re)
        ratio = round(r / f, 2) if f > 0.003 else 1.0
        corners.append({"n": c["n"], "apex": c["apex"], "vmin": round(c["vmin"]),
                        "front": round(f, 4), "rear": round(r, 4), "ratio": ratio,
                        "bal": "sobreviraje" if ratio >= 1.25 else "subviraje" if ratio <= 0.8 else "neutro"})
    return {"corners": corners} if corners else None


def report_balance(folder):
    meta, _ = _load(folder)
    bal = balance_struct(folder)
    print(f"\n=== Balance sobre/subviraje · {_track_name(meta)} ===")
    if not bal:
        print("  faltan >=2 vueltas clean con canal de slip por rueda.")
        return
    print("  slip lateral por rueda (trasero vs delantero) en el apex; R/F >1.25 = sobreviraje, <0.8 = subviraje.")
    print(f"\n  {'curva':6} {'apex':>6} {'vmin':>5} {'slipF':>7} {'slipR':>7} {'R/F':>5}  balance")
    for c in bal["corners"]:
        print(f"  T{c['n']:<5} {c['apex']:>6} {c['vmin']:>5} {c['front']:>7.4f} {c['rear']:>7.4f} "
              f"{c['ratio']:>5.2f}  {c['bal']}")


def tyres_struct(folder):
    """Gomas por rueda sobre las vueltas clean: presion (bar), temp de nucleo (C), camber (deg).
    EVO no da zonas de temp -> sin veredicto termico; valores crudos para que el piloto juzgue."""
    clean = clean_laps(folder)["clean"]
    if not clean:
        return None
    acc = {c: {"temp": [], "press": [], "camber": []} for c in CORNERS}
    for rec in clean:
        d = _lap_trace(folder, rec)
        if not d:
            continue
        for c in CORNERS:
            acc[c]["temp"] += d.get(f"tyre_temp_{c}", [])
            acc[c]["press"] += d.get(f"tyre_press_{c}", [])
            acc[c]["camber"] += d.get(f"camber_{c}", [])
    wheels = {}
    for c in CORNERS:
        a = acc[c]
        if not a["temp"]:
            continue
        wheels[c] = {"temp": round(sum(a["temp"]) / len(a["temp"]), 1),
                     "press": round(st.median(a["press"]), 2) if a["press"] else None,
                     "camber": round(sum(a["camber"]) / len(a["camber"]), 1) if a["camber"] else None}
    return {"wheels": wheels, "n_clean": len(clean)} if wheels else None


def report_tyres(folder):
    meta, _ = _load(folder)
    ts = tyres_struct(folder)
    print(f"\n=== Gomas · {_track_name(meta)} ===")
    if not ts:
        print("  sin vueltas clean con traza todavia.")
        return
    print("  promedios sobre las vueltas clean. EVO no da zonas de goma -> sin veredicto termico;")
    print("  camber es DINAMICO (cambia con carga), no el estatico del garage; signo L/R espejado.")
    print(f"\n  {'rueda':5} {'presion':>9} {'temp nucleo':>13} {'camber':>9}")
    for c, w in ts["wheels"].items():
        pr = f"{w['press']:.2f} bar" if w["press"] is not None else "--"
        cam = f"{w['camber']:+.1f}°" if w["camber"] is not None else "--"
        print(f"  {c:5} {pr:>9} {w['temp']:>11.1f}°C {cam:>9}")


def _corners_vs(ta, tb):
    if not ta or not tb or not ta.get("speed_kmh") or not tb.get("speed_kmh"):
        return []
    da, sa = _mono(ta["lap_dist"], ta["speed_kmh"])[:2]
    db, sb = _mono(tb["lap_dist"], tb["speed_kmh"])[:2]
    da2, t_a = _mono(ta["lap_dist"], ta["t"])
    db2, t_b = _mono(tb["lap_dist"], tb["t"])
    if not da or not db or not da2 or not db2:
        return []
    L = db2[-1]                             # alinear A al largo de B (ver report_vs)
    da, da2 = _rescale(da, L), _rescale(da2, L)
    ca = _corners(da, sa)
    out = []
    for c in _corners(db, sb):
        ma = min((x for x in ca if abs(x["apex"] - c["apex"]) < 120),
                 key=lambda x: abs(x["apex"] - c["apex"]), default=None)
        if not ma:
            continue
        x0, x1 = c["apex"] - 50, c["apex"] + 150
        seg = ((_interp(da2, t_a, x1) - _interp(db2, t_b, x1)) -
               (_interp(da2, t_a, x0) - _interp(db2, t_b, x0)))
        out.append({"n": c["n"], "apex": c["apex"], "vmin_a": ma["vmin"], "vmin_ref": c["vmin"],
                    "deficit": round(c["vmin"] - ma["vmin"], 1), "t_perdido_s": round(seg, 3)})
    return out


def build_insights(folder):
    """Consejos accionables para EVO: deficit de vmin por curva (R1) + coasting (R3) + balance (R6).
    Sin reglas de sector (EVO no da sectores). Referencia = tu mejor vuelta clean de la sesion."""
    cl = clean_laps(folder)
    n = cl["n_clean"]
    out = []
    if n >= 3:
        ref_rec = cl["ref"]
        ref_trace = _lap_trace(folder, ref_rec)
        agg = {}
        for rec in cl["clean"]:
            if rec is ref_rec:
                continue
            for c in _corners_vs(_lap_trace(folder, rec), ref_trace):
                if c["deficit"] >= 3.0:
                    a = agg.setdefault(c["n"], {"defs": [], "tp": [], "apex": c["apex"], "vref": c["vmin_ref"]})
                    a["defs"].append(c["deficit"])
                    a["tp"].append(max(0.0, c["t_perdido_s"]))
        r1_corners = set()
        for cn, a in agg.items():
            if len(a["defs"]) >= 2:
                md, tp = st.median(a["defs"]), st.median(a["tp"])
                if md >= 3.0 and tp >= 0.05:
                    r1_corners.add(cn)
                    out.append({"t": tp, "proc": "estimado",
                                "msg": f"T{cn} (apex {a['apex']}m): vmin {a['vref'] - md:.0f} km/h, {md:.0f} bajo "
                                       f"tu mejor ({a['vref']:.0f}) — ~{tp:.2f}s. Gira antes y carga mas velocidad de paso."})
        # coasting en la entrada
        coast = {}
        for rec in cl["clean"]:
            t = _lap_trace(folder, rec)
            if not t or not t.get("lap_dist"):
                continue
            d, s, th, br = _mono(t["lap_dist"], t["speed_kmh"], t["throttle"], t["brake"])
            cs = _corners(d, s)
            for dist_ini, largo in _coasting(d, th, br, s):
                end = dist_ini + largo
                ap = min(cs, key=lambda c: abs(c["apex"] - end), default=None) if cs else None
                if ap and largo >= 25 and (ap["apex"] - 80) <= end <= ap["apex"]:
                    coast.setdefault(ap["n"], []).append(largo)
        for cn, largos in coast.items():
            if len(largos) >= 2 and cn not in r1_corners:
                largo = st.median(largos)
                acortar = min(largo - 10, 15)
                if acortar >= 3:
                    out.append({"t": 0.0, "proc": "metros",
                                "msg": f"T{cn}: coasting {largo:.0f}m antes del apex (flotando). "
                                       f"Frena ~{acortar:.0f}m mas tarde y mantente en el freno hasta soltar el volante."})
    # balance
    bal = balance_struct(folder)
    if bal:
        cor = [c for c in bal["corners"] if c["bal"] != "neutro"]
        w = max(cor, key=lambda c: abs(c["ratio"] - 1.0)) if cor else None
        if w and abs(w["ratio"] - 1.0) >= 0.3:
            tip = ("estabiliza atras (ARB tras. mas blanda / diff coast / mas ala)" if w["bal"] == "sobreviraje"
                   else "ayuda la rotacion (ARB del. mas blanda / mas camber del. / diff power)")
            out.append({"t": 0.0, "proc": "medido",
                        "msg": f"T{w['n']} (apex {w['apex']}m): {w['bal']} marcado (slip R/F {w['ratio']}) — {tip}."})
    out.sort(key=lambda x: x["t"], reverse=True)
    return {"n_clean": n, "best": cl["best_lap_time"]}, out[:4]


def report_insights(folder):
    meta, _ = _load(folder)
    header, insights = build_insights(folder)
    print(f"\n=== Insights · {_track_name(meta)} · {header['n_clean']} vueltas clean "
          f"· mejor {_fmt_t(header['best']).strip()} ===")
    if header["n_clean"] < 3 and not insights:
        print("  N insuficiente: maneja >=3 vueltas clean en circuito para el analisis de curvas/coasting.")
        report_session(folder)
        return
    if not insights:
        print("  Tanda pareja, sin deficit sobre el ruido — sube tu ritmo de referencia.")
        return
    for i, ins in enumerate(insights, 1):
        head = f"~{ins['t']:.2f}s" if ins["t"] > 0 else "magnitud"
        print(f"  [{i}] {head} · {ins['msg']} ({ins['proc']})")


def main():
    ap = argparse.ArgumentParser(description="Analisis de telemetria AC EVO")
    ap.add_argument("folder", nargs="?", help="carpeta de sesion (default: la ultima)")
    ap.add_argument("--list", action="store_true", help="lista las sesiones")
    ap.add_argument("--lap", type=int, metavar="UID", help="inspecciona la traza de esa vuelta (uid)")
    ap.add_argument("--vs", type=int, nargs="+", metavar="UID",
                    help="compara vuelta A [B] por uid (B por defecto: la mejor) — delta por distancia")
    ap.add_argument("--balance", action="store_true", help="balance sobre/subviraje por curva")
    ap.add_argument("--tyres", action="store_true", help="presion/temp/camber por rueda (vueltas clean)")
    ap.add_argument("--insights", action="store_true", help="2-4 consejos accionables (>=3 vueltas clean)")
    a = ap.parse_args()

    if a.list:
        ss = _sessions()
        if not ss:
            print(f"sin sesiones en {TELEM}")
            return
        print(f"sesiones en {TELEM}:")
        for d in reversed(ss):
            meta, laps = _load(d)
            clean = sum(1 for l in laps if l.get("clean", True))
            print(f"  {os.path.basename(d):45} {_track_name(meta):30} {len(laps)} vueltas ({clean} clean)")
        return

    folder = a.folder or (_sessions()[-1] if _sessions() else None)
    if not folder or not os.path.isdir(folder):
        print(f"No hay sesiones en {TELEM}. Maneja con el dash grabando (hotlaps en circuito) y volve.")
        return
    if a.vs:
        report_vs(folder, a.vs[0], a.vs[1] if len(a.vs) > 1 else None)
    elif a.lap is not None:
        report_lap(folder, a.lap)
    elif a.balance:
        report_balance(folder)
    elif a.tyres:
        report_tyres(folder)
    elif a.insights:
        report_insights(folder)
    else:
        report_session(folder)


if __name__ == "__main__":
    main()
