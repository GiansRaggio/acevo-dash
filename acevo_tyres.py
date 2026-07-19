#!/usr/bin/env python3
"""Analizador de gomas + balance para AC EVO (sincronico, alimentado del bridge).

Dos cosas, ambas de canales VALIDADOS en vivo de physics:
1. GOMAS por esquina: presion (psi->bar), temp de nucleo (C), camber (rad->grados).
   EVO NO da temp por zonas (interior/medio/exterior estan MUERTAS) -> no hay veredicto
   termico de presion como en AMS2; se muestran los valores crudos (el piloto juzga).
   Camber SI es canal directo (mejor que en AMS2, que lo infiere del spread de temp).
2. BALANCE sobre/subviraje: durante curva (|G lateral| alto), compara slipAngle trasero
   vs delantero. ratio = reb/del: >1.25 sobrevirador, <0.8 subvirador (umbrales portados
   de analyze_telemetry.balance_struct de AMS2). Acumula tendencia de la sesion.

No corre hilo propio: el bridge llama update(phys) por frame (~30Hz) y payload() al emitir.
"""
import math

CORNERS = ("FL", "FR", "RL", "RR")
PSI_TO_BAR = 1.0 / 14.5038
RAD_TO_DEG = 57.29578

MIN_SPEED = 15          # km/h: bajo esto no acumulamos (parado/pits)
CORNER_G = 0.45         # |G lateral| para considerar "en curva" (balance)
OVERSTEER = 1.25        # ratio reb/del > esto = sobrevirador
UNDERSTEER = 0.80       # < esto = subvirador
EMA = 0.08             # suavizado de presion/temp/camber (valor "actual")


class TyreAnalyzer:
    def __init__(self):
        # valores actuales suavizados (EMA) por esquina
        self._press = [None] * 4     # bar
        self._temp = [None] * 4      # C
        self._camber = [None] * 4    # grados
        # balance acumulado (solo en curva)
        self._under = 0
        self._over = 0
        self._neutral = 0
        self._ratio_sum = 0.0
        self._ratio_n = 0
        self._live = False

    def reset(self):
        self._under = self._over = self._neutral = 0
        self._ratio_sum = 0.0
        self._ratio_n = 0

    def update(self, phys):
        if phys is None:
            self._live = False
            return
        self._live = phys.speedKmh >= MIN_SPEED
        # ---- valores por esquina (EMA): lecturas ABSOLUTAS, validas a cualquier velocidad
        # (incluido parado -> refleja enfriamiento). Cada canal se actualiza por separado:
        # un NaN puntual en uno no descarta las otras dos lecturas buenas de la esquina.
        for c in range(4):
            pb = phys.wheelsPressure[c] * PSI_TO_BAR
            tc = phys.tyreCoreTemperature[c]
            cam = phys.camberRAD[c] * RAD_TO_DEG
            if math.isfinite(pb):
                self._press[c] = pb if self._press[c] is None else self._press[c] + EMA * (pb - self._press[c])
            if math.isfinite(tc):
                self._temp[c] = tc if self._temp[c] is None else self._temp[c] + EMA * (tc - self._temp[c])
            if math.isfinite(cam):
                self._camber[c] = cam if self._camber[c] is None else self._camber[c] + EMA * (cam - self._camber[c])
        # ---- balance sobre/subviraje: usa slip (derivado de movimiento) -> solo manejando ----
        if phys.speedKmh < MIN_SPEED:
            return
        lat_g = abs(phys.accG[0])
        if lat_g >= CORNER_G:
            fa = (abs(phys.slipAngle[0]) + abs(phys.slipAngle[1])) / 2.0
            ra = (abs(phys.slipAngle[2]) + abs(phys.slipAngle[3])) / 2.0
            if fa > 0.003 and math.isfinite(fa) and math.isfinite(ra):
                ratio = ra / fa
                self._ratio_sum += ratio
                self._ratio_n += 1
                if ratio > OVERSTEER:
                    self._over += 1
                elif ratio < UNDERSTEER:
                    self._under += 1
                else:
                    self._neutral += 1

    def payload(self):
        corners = []
        for c in range(4):
            corners.append({
                "name": CORNERS[c],
                "press": round(self._press[c], 2) if self._press[c] is not None else None,
                "temp": round(self._temp[c]) if self._temp[c] is not None else None,
                "camber": round(self._camber[c], 1) if self._camber[c] is not None else None,
            })
        n = self._under + self._over + self._neutral
        bal = {"samples": n, "live": self._live}
        if n > 0:
            up = round(100 * self._under / n)
            op = round(100 * self._over / n)
            np_ = 100 - up - op
            avg = self._ratio_sum / self._ratio_n if self._ratio_n else 1.0
            if op - up >= 15:
                verdict = "sobrevirador"
            elif up - op >= 15:
                verdict = "subvirador"
            else:
                verdict = "neutro"
            bal.update({"understeer_pct": up, "oversteer_pct": op, "neutral_pct": np_,
                        "avg_ratio": round(avg, 2), "verdict": verdict})
        return {"corners": corners, "balance": bal}
