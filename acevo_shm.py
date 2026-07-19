#!/usr/bin/env python3
"""Lectura de la Shared Memory de Assetto Corsa EVO (solo lectura, Windows).

AC EVO publica tres mapeos nombrados (estilo Kunos, pero con infijo 'pmf' y
prefijo 'acevo_', NO los 'acpmf_' de AC1/ACC):
    Local\\acevo_pmf_physics   (~800 bytes, ~332 Hz)  <- fisica del auto
    Local\\acevo_pmf_graphics  (~8 KB, cadencia HUD)  <- tiempos/combustible/HUD
    Local\\acevo_pmf_static    (~210 bytes, 1x sesion)

Este modulo mapea PHYSICS completo (layout verificado en vivo contra el auto real,
16-jul-2026, build 0.8.0.1) y el PREFIJO de GRAPHICS hasta 'current_bhp' (offset
~216). NO modelamos la cola de graphics (tiempos mejor/ultima, posicion, standings,
max_fuel): el layout comunitario es internamente inconsistente ahi y vive detras de
bloques opacos de tamano no verificable -> se decodifica aparte en E2, con validacion
empirica. Aca solo usamos campos de graphics ANTES de la zona de neumaticos (offset
<220), cuyos offsets son deterministas desde el header limpio.

Fuente del layout: docs/SHARED_MEMORY.md de github.com/albertowd/live-telemetry-evo,
cruzado con lectura en vivo. Campos MUERTOS confirmados en EVO (leen 0): tyreWear,
tyreTempI/M/O (zonas), suspensionDamage; ride height lee basura. Ver acevo-dash-viabilidad.

Convencion de marcha: EVO usa gear 0=R, 1=N, 2+=1a...  (distinto de AMS2 -1=R,0=N).
"""
import ctypes
import os
from ctypes import wintypes

MAP_PHYSICS = "Local\\acevo_pmf_physics"
MAP_GRAPHICS = "Local\\acevo_pmf_graphics"
MAP_STATIC = "Local\\acevo_pmf_static"

_FILE_MAP_READ = 0x0004

F = ctypes.c_float
I = ctypes.c_int32
U = ctypes.c_uint32
F3 = F * 3
F4 = F * 4
F5 = F * 5
F2 = F * 2
F43 = (F * 3) * 4


class Physics(ctypes.Structure):
    """acevo_pmf_physics, 800 bytes, _pack_=4. Layout verificado en vivo."""
    _pack_ = 4
    _fields_ = [
        ("packetId", I), ("gas", F), ("brake", F), ("fuel", F), ("gear", I), ("rpms", I),
        ("steerAngle", F), ("speedKmh", F), ("velocity", F3), ("accG", F3),
        ("wheelSlip", F4), ("wheelLoad", F4), ("wheelsPressure", F4), ("wheelAngularSpeed", F4),
        ("tyreWear", F4), ("tyreDirtyLevel", F4), ("tyreCoreTemperature", F4), ("camberRAD", F4),
        ("suspensionTravel", F4), ("drs", F), ("tc", F), ("heading", F), ("pitch", F), ("roll", F),
        ("cgHeight", F), ("carDamage", F5), ("numberOfTyresOut", I), ("pitLimiterOn", I),
        ("abs", F), ("kersCharge", F), ("kersInput", F), ("autoShifterOn", I), ("rideHeight", F2),
        ("turboBoost", F), ("ballast", F), ("airDensity", F), ("airTemp", F), ("roadTemp", F),
        ("localAngularVel", F3), ("finalFF", F), ("performanceMeter", F), ("engineBrake", I),
        ("ersRecoveryLevel", I), ("ersPowerLevel", I), ("ersHeatCharging", I), ("ersIsCharging", I),
        ("kersCurrentKJ", F), ("drsAvailable", I), ("drsEnabled", I), ("brakeTemp", F4), ("clutch", F),
        ("tyreTempI", F4), ("tyreTempM", F4), ("tyreTempO", F4), ("isAIControlled", I),
        ("tyreContactPoint", F43), ("tyreContactNormal", F43), ("tyreContactHeading", F43),
        ("brakeBias", F), ("localVelocity", F3), ("P2PActivations", I), ("P2PStatus", I),
        ("currentMaxRpm", I), ("mz", F4), ("fx", F4), ("fy", F4), ("slipRatio", F4), ("slipAngle", F4),
        ("tcInAction", I), ("absInAction", I), ("suspensionDamage", F4), ("tyreTemp", F4),
        ("waterTemp", F), ("brakeTorque", F4), ("frontBrakeCompound", I), ("rearBrakeCompound", I),
        ("padLife", F4), ("discLife", F4), ("ignitionOn", I), ("starterEngineOn", I),
        ("isEngineRunning", I), ("kerbVibration", F), ("slipVibrations", F), ("roadVibrations", F),
        ("absVibrations", F),
    ]


class GraphicsHead(ctypes.Structure):
    """PREFIJO de acevo_pmf_graphics hasta current_bhp (offset ~216). Solo campos
    antes de la zona de neumaticos (offset 220) -> offsets deterministas, sin
    riesgo de deriva por bloques opacos. Mapeamos un prefijo del bloque real de ~8KB."""
    _pack_ = 4
    _fields_ = [
        ("packetId", I), ("status", I),
        ("focused_car_id_a", ctypes.c_uint64), ("focused_car_id_b", ctypes.c_uint64),
        ("player_car_id_a", ctypes.c_uint64), ("player_car_id_b", ctypes.c_uint64),
        ("rpm", ctypes.c_uint16),
        ("is_rpm_limiter_on", ctypes.c_bool), ("is_change_up_rpm", ctypes.c_bool),
        ("is_change_down_rpm", ctypes.c_bool), ("tc_active", ctypes.c_bool),
        ("abs_active", ctypes.c_bool), ("esc_active", ctypes.c_bool),
        ("launch_active", ctypes.c_bool), ("is_ignition_on", ctypes.c_bool),
        ("is_engine_running", ctypes.c_bool), ("kers_is_charging", ctypes.c_bool),
        ("is_wrong_way", ctypes.c_bool), ("is_drs_available", ctypes.c_bool),
        ("battery_is_charging", ctypes.c_bool), ("is_max_kj_per_lap_reached", ctypes.c_bool),
        ("is_max_charge_kj_per_lap_reached", ctypes.c_bool),
        ("display_speed_kmh", ctypes.c_int16), ("display_speed_mph", ctypes.c_int16),
        ("display_speed_ms", ctypes.c_int16),
        ("pitspeeding_delta", F), ("gear_int", ctypes.c_int16),
        ("rpm_percent", F), ("gas_percent", F), ("brake_percent", F), ("handbrake_percent", F),
        ("clutch_percent", F), ("steering_percent", F), ("ffb_strength", F), ("car_ffb_multiplier", F),
        ("water_temperature_percent", F), ("water_pressure_bar", F), ("fuel_pressure_bar", F),
        ("water_temperature_c", ctypes.c_int8), ("air_temperature_c", ctypes.c_int8),
        ("oil_temperature_c", F), ("oil_pressure_bar", F), ("exhaust_temperature_c", F),
        ("g_forces_x", F), ("g_forces_y", F), ("g_forces_z", F),
        ("turbo_boost", F), ("turbo_boost_level", F), ("turbo_boost_perc", F), ("steer_degrees", I),
        ("current_km", F), ("total_km", U), ("total_driving_time_s", U),
        ("time_of_day_hours", I), ("time_of_day_minutes", I), ("time_of_day_seconds", I),
        ("delta_time_ms", I), ("current_lap_time_ms", I), ("predicted_lap_time_ms", I),
        ("fuel_liter_current_quantity", F), ("fuel_liter_current_quantity_percent", F),
        ("fuel_liter_per_km", F), ("km_per_fuel_liter", F),
        ("current_torque", F), ("current_bhp", I),
    ]


# Fail-fast si un parche de AC EVO cambia el layout (EA activo; el 0.6 ya lo rompio una vez).
assert ctypes.sizeof(Physics) == 800, f"Layout de Physics cambio: {ctypes.sizeof(Physics)} != 800"
assert ctypes.sizeof(GraphicsHead) == 220, f"Layout de GraphicsHead cambio: {ctypes.sizeof(GraphicsHead)} != 220"


class SharedMemoryUnavailable(Exception):
    """No se pudo abrir un mapeo de AC EVO (juego cerrado / no en sesion)."""


def gear_to_ams2(evo_gear):
    """EVO 0=R,1=N,2+=1a  ->  convencion del dash (-1=R, 0=N, 1+=marcha). index.html
    pinta gear===-1 como 'R', ===0 como 'N', el resto como numero."""
    return evo_gear - 1


class _Map:
    """Un mapeo nombrado abierto (solo lectura) + copia barata a un buffer ctypes."""

    def __init__(self, k, name, struct_type):
        self._k = k
        self._name = name
        self._struct = struct_type
        self._size = ctypes.sizeof(struct_type)
        self._buf = struct_type()
        self._handle = None
        self._addr = None

    def open(self):
        h = self._k.OpenFileMappingW(_FILE_MAP_READ, False, self._name)
        if not h:
            raise SharedMemoryUnavailable(
                f"No existe el mapeo '{self._name}'. Abri AC EVO y entra a una sesion."
            )
        addr = self._k.MapViewOfFile(h, _FILE_MAP_READ, 0, 0, 0)
        if not addr:
            err = ctypes.get_last_error()
            self._k.CloseHandle(h)
            raise SharedMemoryUnavailable(f"MapViewOfFile fallo en '{self._name}' (err={err})")
        self._handle = h
        self._addr = addr
        return self

    def copy(self):
        ctypes.memmove(ctypes.byref(self._buf), self._addr, self._size)
        return self._buf

    def close(self):
        if self._addr:
            self._k.UnmapViewOfFile(ctypes.c_void_p(self._addr))
            self._addr = None
        if self._handle:
            self._k.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


class Reader:
    """Abre physics + graphics de AC EVO y entrega snapshots consistentes."""

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenFileMappingW.restype = ctypes.c_void_p
        k.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k = k
        self._phys = _Map(k, MAP_PHYSICS, Physics)
        self._graf = _Map(k, MAP_GRAPHICS, GraphicsHead)

    def open(self):
        # physics es obligatorio; graphics es best-effort (no tumbar si falta)
        self._phys.open()
        try:
            self._graf.open()
        except SharedMemoryUnavailable:
            self._graf = None
        return self

    def snapshot(self):
        """(physics, graphics_o_None). Guard anti-frame-roto en physics: reintenta si
        packetId cambia durante la copia (a 332 Hz el memmove es ~us, el tear es raro)."""
        p = self._phys
        live_pk = ctypes.cast(p._addr, ctypes.POINTER(I))   # packetId VIVO (offset 0)
        for _ in range(8):
            seq1 = live_pk.contents.value
            phys = p.copy()
            if live_pk.contents.value == seq1:   # packetId no cambio DURANTE el copy -> sin tearing
                break
        graf = None
        if self._graf is not None:
            try:
                graf = self._graf.copy()
            except OSError:
                graf = None
        return phys, graf

    def close(self):
        self._phys.close()
        if self._graf is not None:
            self._graf.close()


if __name__ == "__main__":
    # Sonda de validacion: vuelca los campos que usa E1 + chequea que graphics calce
    # (display_speed vs physics.speedKmh, gear_int vs physics.gear).
    import time
    print(f"sizeof(Physics)={ctypes.sizeof(Physics)} (esp 800)  "
          f"sizeof(GraphicsHead)={ctypes.sizeof(GraphicsHead)}")
    r = Reader().open()
    try:
        for _ in range(5):
            phys, graf = r.snapshot()
            g_ok = graf is not None
            line = (f"seq={phys.packetId}  speed={phys.speedKmh:6.1f}km/h  rpm={phys.rpms}/"
                    f"{phys.currentMaxRpm}  gear={phys.gear}(->{gear_to_ams2(phys.gear)})  "
                    f"gas={phys.gas*100:3.0f}% brk={phys.brake*100:3.0f}%  "
                    f"fuel={phys.fuel:.1f}L  water={phys.waterTemp:.0f}C  "
                    f"pit={phys.pitLimiterOn} tc={phys.tcInAction} abs={phys.absInAction}")
            print(line)
            if g_ok:
                dspeed = graf.display_speed_kmh
                match = abs(dspeed - phys.speedKmh) < 5 and (graf.gear_int == phys.gear)
                print(f"   GRAPHICS: display_speed={dspeed}km/h gear_int={graf.gear_int} "
                      f"lap_time={graf.current_lap_time_ms}ms fuel={graf.fuel_liter_current_quantity:.1f}L "
                      f"-> {'CALZA' if match else 'NO CALZA (revisar offsets)'}")
            else:
                print("   GRAPHICS: no disponible")
            time.sleep(0.4)
    finally:
        r.close()
