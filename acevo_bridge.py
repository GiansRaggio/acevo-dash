#!/usr/bin/env python3
"""Bridge Assetto Corsa EVO (Shared Memory) -> Dashboard web.

Hermano de bridge_shm.py del dash de AMS2, pero lee la shared memory de AC EVO
(acevo_pmf_physics + acevo_pmf_graphics). Emite el MISMO contrato JSON por
WebSocket (:8765) y sirve el mismo index.html por HTTP (:8080) -> el dash del
telefono/tablet es identico.

E1 (dash basico): velocidad, marcha, rpm+shift LEDs, pedales, TC/ABS, pit limiter,
agua, combustible (litros + % + barra). Tiempos de vuelta / posicion / leaderboard /
estrategia / dampers quedan para E2 (requieren decodificar la 'cola' de graphics y
derivar la velocidad de damper del recorrido de suspension).

Fuentes (ver acevo_shm): graphics para el HUD (no se blanquea en menu), physics para
pedales/pit/TC-ABS-in-action. Combustible y max_rpm se derivan de graphics.

Uso (Windows):
    .venv\\Scripts\\python.exe acevo_bridge.py
Requiere AC EVO abierto y en una sesion (los mapeos existen al entrar a pista).
"""
import asyncio
import http.server
import json
import math
import os
import socket
import threading
import time

import websockets

import acevo_shm
import acevo_dampers
import acevo_tyres
import acevo_telemetry

WS_PORT = 8765
HTTP_PORT = 8080
POLL_HZ = 30
STALE_S = 1.5   # sin avance de packetId por mas de esto -> no "conectado"

state = {
    "connected": False,
    "speed_kmh": 0,
    "rpm": 0,
    "max_rpm": 8000,
    "gear": 0,
    "throttle": 0,
    "brake": 0,
    "fuel_liters": 0.0,
    "fuel_capacity": 0,
    "split_ahead": None,
    "split_behind": None,
    "event_remaining": None,
    "position": 0,
    "num_participants": 0,
    "current_lap": 0,
    "current_time": None,
    "last_lap": None,
    "best_lap": None,
    "water_temp": None,
    "oil_temp": None,
    "pit_limiter": False,
    "abs_active": False,
    "tc_active": False,
    "engine_warning": False,
    "fuel_per_lap": None,
    "fuel_laps_left": None,
    "leaderboard": [],            # E2: EVO no expone nombres de rivales por SHM
    "strategy": {"calibrating": True, "live": False, "mode": "none"},   # E2
}

_last_packet = -1
_last_packet_change = 0.0
_max_rpm_cache = 8000
_fuel_cap_cache = 0


def update_state(phys, graf):
    """Vuelca (physics, graphics) al dict global `state`. graphics = fuente del HUD;
    physics = pedales/pit (precisos, leen 0 en menu)."""
    global _last_packet, _last_packet_change, _max_rpm_cache, _fuel_cap_cache
    now = time.monotonic()

    pk = phys.packetId
    if pk != _last_packet:
        _last_packet = pk
        _last_packet_change = now
    fresh = (now - _last_packet_change) < STALE_S
    state["connected"] = bool(fresh and graf is not None)

    # gomas + balance: canales de physics, independiente de graphics
    tyres_analyzer.update(phys)
    state["tyres"] = tyres_analyzer.payload()
    if telemetry is not None:
        state["telemetry"] = telemetry.status()

    if graf is None:
        # sin graphics no hay HUD: solo lo minimo de physics
        state["speed_kmh"] = round(phys.speedKmh)
        state["rpm"] = max(phys.rpms, 0)
        state["gear"] = acevo_shm.gear_to_ams2(phys.gear)
        return

    # ---- HUD desde graphics (estable, no se blanquea en menu) ----
    state["speed_kmh"] = max(graf.display_speed_kmh, 0)
    state["rpm"] = max(graf.rpm, 0)
    if graf.rpm_percent > 0.02 and graf.rpm > 0:
        _max_rpm_cache = max(round(graf.rpm / graf.rpm_percent), 1)
    state["max_rpm"] = _max_rpm_cache
    state["gear"] = acevo_shm.gear_to_ams2(graf.gear_int)

    liters = graf.fuel_liter_current_quantity
    pct = graf.fuel_liter_current_quantity_percent      # fraccion 0..1
    if math.isfinite(liters):
        state["fuel_liters"] = round(liters, 1)
        if math.isfinite(pct) and pct > 0.02:
            _fuel_cap_cache = round(liters / pct)        # capacidad estatica del auto (cachea ultimo bueno)
        state["fuel_capacity"] = _fuel_cap_cache

    state["water_temp"] = graf.water_temperature_c

    # cronometro de vuelta EN CURSO (graphics offset 188, confirmado por byte-scan en vivo).
    # Se descarta si acumulo demasiado (auto parado en pista sin cruzar meta -> no es una vuelta).
    lt = graf.current_lap_time_ms
    state["current_time"] = lt / 1000.0 if 0 < lt < 1_200_000 else None

    # ---- señales de "manejo" desde physics (0/off en menu, correcto) ----
    state["throttle"] = round(phys.gas * 100)
    state["brake"] = round(phys.brake * 100)
    state["pit_limiter"] = bool(phys.pitLimiterOn)
    # TC/ABS "actuando ahora": physics tiene el flag dedicado in-action
    state["tc_active"] = bool(phys.tcInAction)
    state["abs_active"] = bool(phys.absInAction)


# ---------------- WebSocket + HTTP (identico a bridge_shm) ----------------
CLIENTS = set()
_shutdown = False
analyzer = None   # DamperAnalyzer (se crea en main; su hilo muestrea aparte a ~332Hz)
telemetry = None  # TelemetryLogger (se crea en main; su hilo graba vueltas a disco)
tyres_analyzer = acevo_tyres.TyreAnalyzer()   # gomas+balance (sincronico, del snapshot del bridge)


async def ws_handler(ws):
    global _shutdown
    CLIENTS.add(ws)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            cmd = msg.get("cmd")
            if cmd == "stop_server":
                print("[acevo] detenido por el usuario (boton del dash)")
                _shutdown = True
            elif cmd == "reset_dampers" and analyzer is not None:
                analyzer.reset()
            elif cmd == "reset_tyres":
                tyres_analyzer.reset()
            elif cmd == "set_telemetry" and telemetry is not None:
                m = msg.get("mode")
                if m in ("off", "summary", "full"):
                    telemetry.set_mode(m)
            # otros comandos (set_race/set_telemetry) son no-op en EVO por ahora
    finally:
        CLIENTS.discard(ws)


async def _send_all(msg):
    for ws in list(CLIENTS):
        try:
            await ws.send(msg)
        except websockets.ConnectionClosed:
            CLIENTS.discard(ws)


async def _broadcast():
    if CLIENTS:
        await _send_all(json.dumps(state))


async def pump():
    global _last_packet, _last_packet_change
    period = 1.0 / POLL_HZ
    reader = None
    next_retry = 0.0
    next_damper = 0.0
    while not _shutdown:
        await asyncio.sleep(period)
        now = time.monotonic()
        # histograma de dampers (hilo propio del analizador, ~2 Hz de emision)
        if analyzer is not None and now >= next_damper:
            next_damper = now + 0.5
            await _send_all(json.dumps({"dampers": analyzer.payload()}))
        if reader is None:
            if now < next_retry:
                await _broadcast()
                continue
            try:
                reader = acevo_shm.Reader().open()
                _last_packet = -1              # no arrastrar el packetId de la sesion previa
                _last_packet_change = now
                print("[acevo] shared memory conectada (acevo_pmf_physics/graphics)")
            except acevo_shm.SharedMemoryUnavailable:
                state["connected"] = False
                next_retry = now + 1.0
                await _broadcast()
                continue
        try:
            phys, graf = reader.snapshot()
        except OSError:                        # el mapeo murio (EVO cerrado) -> reabrir
            reader.close()
            reader = None
            state["connected"] = False
            next_retry = now + 1.0
            await _broadcast()
            continue
        try:
            update_state(phys, graf)
        except (ValueError, OverflowError):    # frame con NaN/Inf (tear raro): conservar ultimo bueno
            pass
        # recuperacion: si packetId lleva >3s congelado (EVO cerrado/menu), reabrir
        if (now - _last_packet_change) > 3.0:
            reader.close()
            reader = None
            next_retry = now + 1.0
        await _broadcast()
    if reader is not None:
        reader.close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, *a):
        pass


def make_httpd():
    here = os.path.dirname(os.path.abspath(__file__))
    handler = lambda *a, **kw: _NoCacheHandler(*a, directory=here, **kw)
    # bindea ACA (no dentro del hilo): si el puerto esta tomado por un zombi, falla ruidoso
    return http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler)


async def main():
    global analyzer, telemetry
    analyzer = acevo_dampers.DamperAnalyzer().start()
    telemetry = acevo_telemetry.TelemetryLogger().start()
    httpd = make_httpd()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ip = lan_ip()
    print(f"[acevo] Fuente: AC EVO Shared Memory (acevo_pmf_physics + graphics)")
    print(f"[acevo] WS   : ws://{ip}:{WS_PORT}")
    print(f"[acevo] Dash : http://{ip}:{HTTP_PORT}  <- abrir en el celular")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await pump()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[acevo] detenido")
