#!/usr/bin/env python3
"""Analizador de dampers para AC EVO — histograma de velocidad de amortiguador.

AC EVO NO expone velocidad de suspension (a diferencia de AMS2). La DERIVAMOS de
`physics.suspensionTravel[4]` (m -> mm) diferenciando en el tiempo a la tasa nativa
(~332 Hz). La matematica de binning / metricas / recomendaciones 4-way esta portada
de ams2_dampers.py (es agnostica del sim).

SIGNO DE COMPRESION (critico): en EVO el travel es negativo y su signo varia por auto,
asi que "que direccion es compresion (bump)" NO se sabe a priori -> se AUTO-CALIBRA en
vivo: al frenar las delanteras comprimen, asi que corr(brake, travel_delantero) da la
direccion. Hasta calibrar se usa la hipotesis por defecto (compresion = travel decrece,
porque las traseras —mas cargadas— miden mas negativas). El histograma se acumula en
convencion cruda (travel-creciente = +) y se ORIENTA a bump-positivo recien al emitir,
para que un flip de calibracion no corrompa lo acumulado.

Corre su propio hilo + Reader, independiente del bridge. Recomendaciones HEURISTICAS.
El bottoming/travel-recs de AMS2 NO se portan (EVO no da un cero de travel confiable).
"""
import math
import threading
import time

import acevo_shm

CORNERS = ("FL", "FR", "RL", "RR")

BIN_W = 25                       # mm/s por bin
BIN_MAX = 400                    # mm/s (borde)
N_BINS = (2 * BIN_MAX) // BIN_W   # 32
LOW_HIGH = 50                    # mm/s: umbral low/high speed
CENTERS = [(-BIN_MAX + BIN_W * (i + 0.5)) for i in range(N_BINS)]

DT_MAX = 0.03                    # s: si el gap entre frames supera esto, no derivar (pausa/salto)
MIN_SPEED = 10                   # km/h: bajo esto no acumulamos (parado/pits)

# Auto-calibracion del signo de compresion
CALIB_MIN_SAMPLES = 400          # muestras manejando antes de fijar signo
CALIB_MIN_ABSCORR = 0.12         # |corr(brake, frontTravel)| minimo para confiar
CALIB_MIN_BRAKEVAR = 0.02        # varianza minima de brake (que hayas frenado de verdad)


def _bin_index(v_mmps):
    idx = int((v_mmps + BIN_MAX) // BIN_W)
    return 0 if idx < 0 else (N_BINS - 1 if idx >= N_BINS else idx)


def _zeros():
    return [[0] * N_BINS for _ in range(4)]


class DamperAnalyzer:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._acc = _zeros()          # histograma crudo (travel-creciente = +)
        self._n = 0
        self._rate = 0.0
        # derivacion
        self._prev_t = None           # travel[4] mm del frame anterior
        self._prev_time = None
        self._prev_pk = None
        # auto-calibracion de signo (correlacion incremental brake vs frontTravel)
        self._comp_sign = -1          # hipotesis: compresion = travel decrece
        self._sign_locked = False
        self._cw = _CorrAcc()         # corr(brake, frontTravel)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

    def reset(self):
        with self._lock:
            self._acc = _zeros()
            self._n = 0
            # el signo calibrado NO se borra (es propiedad del auto): reset limpia solo el histograma

    def _run(self):
        reader = None
        frames = 0
        t0 = time.perf_counter()
        while not self._stop:
            if reader is None:
                try:
                    reader = acevo_shm.Reader().open()
                except acevo_shm.SharedMemoryUnavailable:
                    time.sleep(1.0)
                    continue
            try:
                phys, _ = reader.snapshot()
            except OSError:
                reader.close()
                reader = None
                time.sleep(0.5)
                continue
            pk = phys.packetId
            if pk == self._prev_pk:
                time.sleep(0.0015)
                continue
            self._ingest(phys, pk)
            frames += 1
            now = time.perf_counter()
            if now - t0 >= 1.0:
                with self._lock:
                    self._rate = frames / (now - t0)
                frames = 0
                t0 = now

    def _ingest(self, phys, pk):
        now = time.perf_counter()
        travel = [phys.suspensionTravel[i] * 1000.0 for i in range(4)]   # mm
        prev_t, prev_time, prev_pk = self._prev_t, self._prev_time, self._prev_pk
        self._prev_t, self._prev_time, self._prev_pk = travel, now, pk
        if prev_t is None:
            return
        dt = now - prev_time
        if dt <= 0 or dt > DT_MAX:        # pausa/salto de frames -> no derivar (velocidad basura)
            return
        if phys.speedKmh < MIN_SPEED:     # parado/pits
            return
        # velocidad cruda por esquina (convencion travel-creciente = +)
        vel = [(travel[c] - prev_t[c]) / dt for c in range(4)]
        if not all(math.isfinite(v) for v in vel):
            return
        with self._lock:
            for c in range(4):
                self._acc[c][_bin_index(vel[c])] += 1
            self._n += 1
            # auto-calibracion: corr(brake, travel medio delantero)
            if not self._sign_locked:
                self._cw.add(phys.brake, (travel[0] + travel[1]) / 2.0)
                if self._n >= CALIB_MIN_SAMPLES and self._cw.brake_var() >= CALIB_MIN_BRAKEVAR:
                    r = self._cw.corr()
                    if abs(r) >= CALIB_MIN_ABSCORR:
                        # comp_sign = sign(corr(brake, frontTravel)): al frenar la delantera comprime,
                        # asi que el signo de la correlacion ES la direccion de compresion del travel.
                        self._comp_sign = 1 if r > 0 else -1
                        self._sign_locked = True

    def payload(self):
        with self._lock:
            comp = self._comp_sign
            # orientar a bump-positivo: si compresion = travel decrece, espejar el histograma
            hist = []
            for c in range(4):
                h = list(self._acc[c])
                hist.append(h if comp > 0 else h[::-1])
            n, rate, locked = self._n, self._rate, self._sign_locked
        corners = [self._corner_metrics(CORNERS[c], hist[c]) for c in range(4)]
        sign_txt = ("signo confirmado" if locked else "signo por calibrar (frena fuerte unas veces)")
        return {
            "binW": BIN_W, "binMax": BIN_MAX, "lowHigh": LOW_HIGH, "centers": CENTERS,
            "corners": corners,
            "validLaps": 0,
            "showingCurrent": True,
            "curSamples": n, "accSamples": n,
            "rateHz": round(rate),
            "recommendations": self._recommend(corners, locked),
            "springRecs": [f"Bump/rebound del histograma ({sign_txt}). "
                           "Bottoming/travel no portado a EVO todavia (falta cero de travel confiable)."],
        }

    @staticmethod
    def _corner_metrics(name, hist):
        total = sum(hist)
        if total == 0:
            return {"name": name, "hist": hist, "pctLow": 0, "pctHigh": 0,
                    "pctBump": 0, "pctRebound": 0, "medAbs": 0, "samples": 0,
                    "pctSB": 0, "pctFB": 0, "pctSR": 0, "pctFR": 0, "tBottom": 0}
        sb = fb = sr = fr = 0
        for h, cc in zip(hist, CENTERS):
            if cc > 0:
                if cc <= LOW_HIGH: sb += h
                else: fb += h
            elif cc < 0:
                if cc >= -LOW_HIGH: sr += h
                else: fr += h
        bump, rebound, low = sb + fb, sr + fr, sb + sr
        order = sorted(zip((abs(cc) for cc in CENTERS), hist))
        cum, med = 0, 0
        for av, h in order:
            cum += h
            if cum >= total / 2:
                med = av
                break
        return {
            "name": name, "hist": hist,
            "pctLow": round(100 * low / total),
            "pctHigh": round(100 * (fb + fr) / total),
            "pctBump": round(100 * bump / total),
            "pctRebound": round(100 * rebound / total),
            "pctSB": round(100 * sb / total), "pctFB": round(100 * fb / total),
            "pctSR": round(100 * sr / total), "pctFR": round(100 * fr / total),
            "medAbs": round(med), "samples": total, "tBottom": 0,
        }

    @staticmethod
    def _recommend(corners, locked):
        def clicks(dev):
            return 0 if dev < 8 else max(1, min(3, round(dev / 8)))

        head = ("Clicks orientativos (- = ablandar, + = endurecer). Iterar 1-2 por vez y re-medir; "
                "el histograma guia balance/simetria, no el click exacto.")
        recs = [head]
        if not locked:
            recs.append("⚠ Signo bump/rebound sin confirmar: frena fuerte unas veces para calibrar antes "
                        "de aplicar clicks (las recomendaciones podrian estar invertidas).")

        def axle(a, b, label):
            c1, c2 = corners[a], corners[b]
            if min(c1["samples"], c2["samples"]) == 0:
                return f"{label}: sin datos todavia."
            SB = (c1["pctSB"] + c2["pctSB"]) / 2
            FB = (c1["pctFB"] + c2["pctFB"]) / 2
            SR = (c1["pctSR"] + c2["pctSR"]) / 2
            FR = (c1["pctFR"] + c2["pctFR"]) / 2
            high = FB + FR
            adj = {"slow bump": 0, "fast bump": 0, "slow reb": 0, "fast reb": 0}
            aS = SB - SR
            if clicks(abs(aS)):
                adj["slow bump" if aS > 0 else "slow reb"] -= clicks(abs(aS))
            aF = FB - FR
            if clicks(abs(aF)):
                adj["fast bump" if aF > 0 else "fast reb"] -= clicks(abs(aF))
            if high >= 28:
                adj["fast bump"] -= max(1, min(3, round((high - 22) / 6)))
            tips = [f"{k} {'+' if dv > 0 else ''}{dv}"
                    for k, dv in ((k, max(-3, min(3, v))) for k, v in adj.items()) if dv]
            body = " · ".join(tips) if tips else "balanceado, sin cambios"
            return f"{label} [SB{SB:.0f}/FB{FB:.0f}/SR{SR:.0f}/FR{FR:.0f}%]: {body}"

        recs.append(axle(0, 1, "DELANTERO"))
        recs.append(axle(2, 3, "TRASERO"))
        for a, b, lbl in ((0, 1, "Delantero"), (2, 3, "Trasero")):
            ca, cb = corners[a], corners[b]
            if ca["samples"] and cb["samples"] and abs(ca["medAbs"] - cb["medAbs"]) > 12:
                hi, lo = (CORNERS[a], CORNERS[b]) if ca["medAbs"] > cb["medAbs"] else (CORNERS[b], CORNERS[a])
                recs.append(f"{lbl}: {hi} trabaja mas que {lo} (asimetria izq/der: peso/alturas/presiones)")
        return recs


class _CorrAcc:
    """Correlacion de Pearson incremental (para calibrar el signo sin guardar todo)."""
    def __init__(self):
        self.n = 0
        self.sx = self.sy = self.sxx = self.syy = self.sxy = 0.0

    def add(self, x, y):
        self.n += 1
        self.sx += x; self.sy += y
        self.sxx += x * x; self.syy += y * y; self.sxy += x * y

    def brake_var(self):
        if self.n < 2:
            return 0.0
        return max(0.0, self.sxx / self.n - (self.sx / self.n) ** 2)

    def corr(self):
        n = self.n
        if n < 2:
            return 0.0
        cov = self.sxy - self.sx * self.sy / n
        vx = self.sxx - self.sx * self.sx / n
        vy = self.syy - self.sy * self.sy / n
        if vx <= 0 or vy <= 0:
            return 0.0
        return cov / math.sqrt(vx * vy)
