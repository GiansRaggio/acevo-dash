#!/usr/bin/env python3
"""Logger de telemetria por vuelta de AC EVO (shared memory) para analisis offline.

Portado de ams2_telemetry.py. Corre su propio hilo + Reader (~50 Hz). Detecta cruces
de meta por RESET de `current_lap_time_ms` (graphics @188) y graba, por sesion:

  telemetry/<pista>__<fecha>/
      session.json      metadatos (auto/pista best-effort, largo de pista, canales)
      summary.jsonl     1 linea por vuelta guardada (con flag `clean`)
      L003_92.451s.csv.gz   traza completa de la vuelta (CSV gz)

Solo se guardan vueltas que ARRANCARON en meta (cronometro ~0 al empezar) y con tiempo
plausible de circuito (LAP_MIN_S..LAP_MAX_S) + suficientes muestras -> descarta out-laps,
free-roam (cronometro acumula sin cruzar) y menu. `clean`=True si el manejo fue continuo
(muestras ~= tiempo*rate); una vuelta con paradas se guarda pero con clean=False.

Diferencias con AMS2 (limitaciones de EVO, ver acevo-dash-viabilidad): sin numero de
vuelta ni sectores por SHM (uid propio); tyre_temp=nucleo (no hay zonas); wear no
disponible. Se agregan canales que EVO SI da (camber directo, slip ratio/angle).
Esquema (HEADER) usa nombres AMS2 donde el canal es equivalente -> maximiza reuso de
analyze_telemetry. append-only por-archivo -> sobrevive un taskkill.
Solo Windows (Reader usa la shared memory de AC EVO).
"""
import ctypes
import gzip
import json
import os
import threading
import time
from ctypes import wintypes
from datetime import datetime

import acevo_shm

CORNERS = ("FL", "FR", "RL", "RR")
RATE_HZ = 50
TELEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry")

MIN_SPEED = 5                 # km/h: "manejando"
MIN_LAP_SAMPLES = 200         # menos que esto = vuelta corta/parcial
LAP_MIN_S = 20.0              # tiempo de vuelta plausible minimo
LAP_MAX_S = 600.0             # maximo (descarta acumulacion de free-roam)
CLEAN_START_MS = 3000         # el cronometro debe arrancar bajo esto (vuelta empezada en meta)
CLEAN_FRAC = 0.75            # muestras >= esto * (tiempo*rate) -> manejo continuo (clean)
IDLE_TIMEOUT = 20.0           # s sin manejar -> se cierra la sesion

PSI_TO_BAR = 1.0 / 14.5038
RAD_TO_DEG = 57.29578

# --- esquema: header y fila del MISMO spec. g = graphics, p = physics ---
_SCALAR = [
    ("t",          lambda p, g: round(g.current_lap_time_ms / 1000.0, 3)),
    ("lap_dist",   lambda p, g: round(g.current_km * 1000.0, 2)),
    ("speed_kmh",  lambda p, g: round(p.speedKmh, 2)),
    ("rpm",        lambda p, g: p.rpms),
    ("gear",       lambda p, g: acevo_shm.gear_to_ams2(g.gear_int)),
    ("throttle",   lambda p, g: round(p.gas, 4)),
    ("brake",      lambda p, g: round(p.brake, 4)),
    ("clutch",     lambda p, g: round(p.clutch, 4)),
    ("steer",      lambda p, g: round(p.steerAngle, 4)),
    ("brake_bias", lambda p, g: round(p.brakeBias, 4)),
    ("accel_lat",  lambda p, g: round(p.accG[0], 3)),
    ("accel_vert", lambda p, g: round(p.accG[1], 3)),
    ("accel_long", lambda p, g: round(p.accG[2], 3)),
    ("yaw",        lambda p, g: round(p.heading, 4)),
    ("pitch",      lambda p, g: round(p.pitch, 4)),
    ("roll",       lambda p, g: round(p.roll, 4)),
    ("water_t",    lambda p, g: round(p.waterTemp, 1)),
    ("fuel_l",     lambda p, g: round(g.fuel_liter_current_quantity, 2)),
    ("max_rpm",    lambda p, g: p.currentMaxRpm),
    ("tc_action",  lambda p, g: int(p.tcInAction)),
    ("abs_action", lambda p, g: int(p.absInAction)),
    ("drs",        lambda p, g: round(p.drs, 3)),
]
_CORNER = [
    ("tyre_temp",   lambda p, i: round(p.tyreCoreTemperature[i], 1)),        # nucleo (EVO no da zonas)
    ("brake_temp",  lambda p, i: round(p.brakeTemp[i], 1)),
    ("susp_travel", lambda p, i: round(p.suspensionTravel[i] * 1000.0, 3)),  # mm
    ("tyre_slip",   lambda p, i: round(p.slipAngle[i], 4)),                  # slip lateral (rad)
    ("tyre_slipr",  lambda p, i: round(p.slipRatio[i], 4)),                  # slip longitudinal
    ("camber",      lambda p, i: round(p.camberRAD[i] * RAD_TO_DEG, 2)),     # grados (canal directo de EVO)
    ("tyre_press",  lambda p, i: round(p.wheelsPressure[i] * PSI_TO_BAR, 3)),  # bar
    ("wheel_load",  lambda p, i: round(p.wheelLoad[i], 1)),                  # N
]
HEADER = [n for n, _ in _SCALAR] + [f"{n}_{c}" for n, _ in _CORNER for c in CORNERS]


def _row(p, g):
    r = [fn(p, g) for _, fn in _SCALAR]
    for _, fn in _CORNER:
        for i in range(4):
            r.append(fn(p, i))
    return ",".join(map(str, r))


def _read_static_strings():
    """Best-effort: cadenas legibles del bloque static (auto/pista). Cierra handle+view
    en todos los caminos (sin leak). Offsets de EVO no verificados -> son candidatos."""
    k = ctypes.windll.kernel32
    k.OpenFileMappingW.restype = wintypes.HANDLE
    k.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.MapViewOfFile.restype = ctypes.c_void_p
    k.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, ctypes.c_size_t]
    k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = p = None
    try:
        h = k.OpenFileMappingW(0x4, False, "Local\\acevo_pmf_static")
        if not h:
            return []
        p = k.MapViewOfFile(h, 0x4, 0, 0, 512)
        if not p:
            return []
        buf = ctypes.string_at(p, 512)
    except Exception:
        return []
    finally:
        if p:
            k.UnmapViewOfFile(ctypes.c_void_p(p))
        if h:
            k.CloseHandle(h)
    out, cur = [], []
    for b in buf:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= 3:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 3:
        out.append("".join(cur))
    return out


def _safe(s):
    return "".join(c if c.isalnum() else "_" for c in s).strip("_") or "x"


class TelemetryLogger:
    """Graba la traza de cada vuelta valida de circuito (CSV gz + resumen)."""

    def __init__(self, base_dir=TELEM_DIR, rate_hz=RATE_HZ):
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._mode = "full"       # "off" | "summary" | "full"
        self._base = base_dir
        self._rate = rate_hz
        self._period = 1.0 / rate_hz
        self._laps_logged = 0
        self._last_file = None
        self._recording = False
        self._reset_session()

    def _reset_session(self):
        self._in_session = False
        self._idle_since = None
        self._sess_dir = None         # ruta real (se crea recien en la 1ra vuelta valida)
        self._sess_name = None        # nombre de carpeta calculado al arrancar
        self._sess_label = None
        self._names = []              # candidatos de nombre (auto/pista) del static
        self._dir_created = False
        self._buf = []
        self._prev_lap_ms = None
        self._lap_max_ms = 0
        self._lap_max_dist = 0.0
        self._lap_clean_start = False
        self._track_len = 0.0
        self._lap_uid = 0
        self._agg = None
        self._start_fuel = None

    # ---------------- API ----------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

    def set_mode(self, mode):
        if mode in ("off", "summary", "full"):
            with self._lock:
                self._mode = mode

    def set_enabled(self, on):
        self.set_mode("full" if on else "off")

    def status(self):
        with self._lock:
            return {
                "mode": self._mode, "enabled": self._mode != "off",
                "recording": self._recording, "laps_logged": self._laps_logged,
                "session": self._sess_label, "last_file": self._last_file,
            }

    # ---------------- hilo ----------------
    def _run(self):
        reader = None
        while not self._stop:
            if self._mode == "off":
                self._recording = False
                time.sleep(0.5)
                continue
            if reader is None:
                try:
                    reader = acevo_shm.Reader().open()
                except acevo_shm.SharedMemoryUnavailable:
                    time.sleep(1.0)
                    continue
            try:
                phys, graf = reader.snapshot()
            except OSError:
                reader.close()
                reader = None
                time.sleep(1.0)
                continue
            try:
                self._ingest(phys, graf)
            except Exception:
                pass   # nunca tumbar el hilo por I/O
            time.sleep(self._period if self._mode == "full" else 0.1)
        if reader is not None:
            reader.close()

    def _ingest(self, phys, graf):
        if graf is None:
            return
        driving = phys.speedKmh > MIN_SPEED
        now = time.monotonic()
        pending = None                     # vuelta a escribir FUERA del lock (evita stall del broadcast)

        with self._lock:
            if driving:
                self._idle_since = None
                if not self._in_session:
                    self._start_session(graf)
            else:
                if self._in_session:
                    if self._idle_since is None:
                        self._idle_since = now
                    elif now - self._idle_since > IDLE_TIMEOUT:
                        self._in_session = False
                self._recording = False
                return
            if not self._in_session:
                return

            lap_ms = graf.current_lap_time_ms
            dist = graf.current_km * 1000.0

            # cruce de meta: el cronometro RESETEA a ~0 desde un valor plausible
            if self._prev_lap_ms is not None:
                dropped = self._prev_lap_ms - lap_ms
                if dropped > 10000 and self._prev_lap_ms >= LAP_MIN_S * 1000:
                    pending = self._collect_lap()      # copia rapida en memoria (I/O afuera)
                    self._begin_lap(graf)
            self._prev_lap_ms = lap_ms

            if lap_ms > self._lap_max_ms:
                self._lap_max_ms = lap_ms
            if dist > self._lap_max_dist:
                self._lap_max_dist = dist
            self._update_agg(phys)
            if self._mode == "full":
                self._buf.append(_row(phys, graf))
            self._recording = True

        if pending is not None:
            self._write_lap(pending)       # gzip + append a disco, SIN el lock

    # ---------------- sesion / vuelta ----------------
    def _start_session(self, graf):
        """Arranca la sesion (SIN crear carpeta todavia: se difiere a la 1ra vuelta valida)."""
        self._in_session = True
        self._prev_lap_ms = None
        self._lap_uid = 0
        self._track_len = 0.0
        self._dir_created = False
        self._sess_dir = None
        self._names = _read_static_strings()
        names = [s for s in self._names if any(c.isalpha() for c in s) and 3 <= len(s) <= 32]
        label = _safe(max(names, key=len)) if names else "evo"   # el mas largo suele ser la pista
        self._sess_label = label
        self._sess_name = f"{label}__{datetime.now():%Y%m%d_%H%M%S}"
        self._begin_lap(graf)

    def _ensure_dir(self):
        """Crea la carpeta + session.json recien cuando hay una vuelta valida que guardar."""
        if self._dir_created:
            return self._sess_dir
        d = os.path.join(self._base, self._sess_name)
        try:
            os.makedirs(d, exist_ok=True)
            meta = {
                "sim": "assetto_corsa_evo",
                "name_candidates": self._names[:8],
                "track_length_m": round(self._track_len, 1) if self._track_len else None,
                "started": datetime.now().isoformat(timespec="seconds"),
                "rate_hz": self._rate,
                "channels": HEADER,
                "note": "EVO: sin numero de vuelta/sectores por SHM; vuelta por reset de cronometro. "
                        "tyre_temp=nucleo (no hay zonas); wear no disponible.",
            }
            with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            self._sess_dir = d
            self._dir_created = True
        except OSError:
            self._sess_dir = None
        return self._sess_dir

    def _begin_lap(self, graf):
        self._buf = []
        self._lap_max_ms = graf.current_lap_time_ms
        self._lap_max_dist = graf.current_km * 1000.0
        self._lap_clean_start = graf.current_lap_time_ms < CLEAN_START_MS   # empezo en meta
        self._agg = {"tmin": [9e9] * 4, "tmax": [-9e9] * 4, "tsum": [0.0] * 4,
                     "bmax": [-9e9] * 4, "n": 0}
        self._start_fuel = graf.fuel_liter_current_quantity

    def _update_agg(self, phys):
        a = self._agg
        if a is None:
            return
        for i in range(4):
            t = phys.tyreCoreTemperature[i]
            if t < a["tmin"][i]:
                a["tmin"][i] = t
            if t > a["tmax"][i]:
                a["tmax"][i] = t
            a["tsum"][i] += t
            b = phys.brakeTemp[i]
            if b > a["bmax"][i]:
                a["bmax"][i] = b
        a["n"] += 1

    def _collect_lap(self):
        """Copia (bajo lock, rapido) el estado de la vuelta recien cerrada para escribirla afuera."""
        return {
            "buf": self._buf, "agg": self._agg, "lap_ms": self._lap_max_ms,
            "dist": self._lap_max_dist, "start_fuel": self._start_fuel,
            "clean_start": self._lap_clean_start, "mode": self._mode,
        }

    def _write_lap(self, p):
        """Escribe la vuelta a disco FUERA del lock. Solo si arranco en meta, tiempo
        plausible y suficientes muestras. `clean`=manejo continuo (sin paradas)."""
        a = p["agg"]
        n_samples = a["n"] if a else 0
        lap_s = p["lap_ms"] / 1000.0
        if not (p["clean_start"] and LAP_MIN_S <= lap_s <= LAP_MAX_S
                and n_samples >= MIN_LAP_SAMPLES):
            return                          # out-lap / free-roam / corta -> descartar
        clean = n_samples >= CLEAN_FRAC * lap_s * self._rate   # muestras ~= tiempo*rate?
        with self._lock:                    # actualizar contadores/track_len (corto)
            if p["dist"] > self._track_len:
                self._track_len = p["dist"]
            self._lap_uid += 1
            uid = self._lap_uid
        sess = self._ensure_dir()
        if sess is None:
            return
        n = max(1, n_samples)
        try:
            tname = None
            if p["mode"] == "full":
                tname = f"L{uid:03d}_{lap_s:.3f}s.csv.gz"
                with gzip.open(os.path.join(sess, tname), "wt",
                               newline="", encoding="utf-8") as f:
                    f.write(",".join(HEADER) + "\n")
                    f.write("\n".join(p["buf"]))
                    f.write("\n")
            summary = {
                "uid": uid, "lap_time": round(lap_s, 3), "valid": True, "clean": bool(clean),
                "samples": n_samples, "lap_dist_m": round(p["dist"], 1),
                "fuel_start": round(p["start_fuel"], 2) if p["start_fuel"] is not None else None,
                "tyre_temp_min": [round(x, 1) for x in a["tmin"]] if a else None,
                "tyre_temp_max": [round(x, 1) for x in a["tmax"]] if a else None,
                "tyre_temp_avg": [round(a["tsum"][i] / n, 1) for i in range(4)] if a else None,
                "brake_temp_max": [round(x, 1) for x in a["bmax"]] if a else None,
                "trace": tname, "ts": datetime.now().isoformat(timespec="seconds"),
            }
            with open(os.path.join(sess, "summary.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            with self._lock:
                self._laps_logged += 1
                self._last_file = tname or f"L{uid:03d} (resumen)"
        except OSError:
            pass
