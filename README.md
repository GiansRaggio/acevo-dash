# AC EVO Dash

Dashboard para el celular + grabador de telemetría para **Assetto Corsa EVO**,
alimentado por la memoria compartida del juego.

Port del [dash de AMS2](https://github.com/lucianograndim/ams2-dash) (fork propio):
se reescribió solo la capa de lectura (`acevo_shm.py`); el bridge, la UI y los
motores de análisis se reusaron casi tal cual.

> **Estado**: funciona y está verificado en vivo (build **0.8.0.1**, carreras online
> y vueltas en Spa). AC EVO está en early access — EA ya rompió el layout de memoria
> una vez (update 0.6), así que espera tener que re-verificar tras cada parche.

## Qué hace

**En vivo, en el celular** (`http://IP_DEL_PC:8080`, horizontal):
- LEDs de cambio, marcha, velocidad, RPM, combustible (litros + %), temp de agua,
  pedales, pit limiter, indicadores TC/ABS.
- Cronómetro de vuelta en curso.
- Página **GOMAS + BALANCE**: presión, temperatura de núcleo y **camber dinámico**
  por esquina, más un veredicto de **sobreviraje/subviraje** con barra de tendencia
  de sesión.
- Página **DAMPERS**: histograma 4-way de velocidad de amortiguador + recomendación
  de clicks (slow/fast bump/rebound).

**Grabando en el disco** (`telemetry/<pista>__<fecha>/`, automático):
- `session.json` — pista, largo inferido, cabecera de 54 canales.
- `summary.jsonl` — una línea por vuelta, con flag `clean`.
- `L###_<tiempo>s.csv.gz` — la traza completa de la vuelta a ~50 Hz.

**Análisis offline** (`tools/analyze_telemetry.py`, solo stdlib):

```bash
python tools/analyze_telemetry.py --list          # sesiones grabadas
python tools/analyze_telemetry.py                 # informe: tiempos, consistencia, tendencia, fuel, temps
python tools/analyze_telemetry.py --insights      # 2-4 consejos accionables (necesita >=3 vueltas limpias)
python tools/analyze_telemetry.py --vs 3 7        # delta por distancia + velocidad de apex entre 2 vueltas
python tools/analyze_telemetry.py --lap 5         # canal por canal de una vuelta
python tools/analyze_telemetry.py --balance       # sobre/subviraje curva por curva
python tools/analyze_telemetry.py --tyres         # presión/temp/camber por rueda
```

## Arranque

Requiere **Python 3** con `websockets`, el celular en la misma WiFi, y el firewall
del PC permitiendo los puertos **8080** (HTTP) y **8765** (WebSocket) en la LAN.

```bash
python acevo_bridge.py
```

O doble clic a `start-acevo.bat`. **Ojo**: ese `.bat` apunta al venv del dash de
AMS2 (`C:\Users\gians\sim\ams2-dash\.venv\...`) porque comparten dependencias —
si clonas esto en otra máquina, edita esa ruta o arma tu propio venv.

No hace falta configurar nada en el juego: la memoria compartida siempre está
escrita. Se puede arrancar el bridge antes que AC EVO; reintenta solo hasta
encontrar el juego.

## Archivos

- `acevo_shm.py` — el reader. Mapea `Local\acevo_pmf_physics` (800 B, ~332 Hz),
  `_graphics` y `_static` con `ctypes`. **Solo lectura**, con guard anti-tearing
  (relee el `packetId` vivo después de copiar).
- `acevo_bridge.py` — WebSocket :8765 + HTTP :8080. Emite el mismo JSON de estado
  que el bridge de AMS2, así que `index.html` es casi el mismo archivo.
- `acevo_dampers.py` — deriva velocidad de amortiguador e histograma 4-way.
- `acevo_tyres.py` — gomas + balance sobre/subviraje.
- `acevo_telemetry.py` — el grabador histórico (hilo propio, ~50 Hz, siempre activo).
- `tools/analyze_telemetry.py` — el analizador offline.
- `index.html` — el dashboard.

## Lo que EVO sí da (y lo que no)

Esto se midió **en vivo contra el auto real**, no se sacó de la documentación —
la doc de la comunidad tiene offsets internamente inconsistentes en la "cola" del
bloque de graphics.

**Vivos y confiables**: presión, temperatura de núcleo y `camberRAD` por esquina
(camber directo, ¡mejor que en AMS2 donde hay que inferirlo!), `suspensionTravel`,
slipRatio/slipAngle, velocidad/RPM/marcha/combustible/brake bias, temperatura de
aire y pista, nombre de la pista.

**Muertos — leen 0.0 constante**:
- `tyreTempI/M/O` (zonas interior/medio/exterior de la goma) → **no hay veredicto
  térmico de presión**. Esta es la pérdida más dolorosa respecto al dash de AMS2.
- `tyreWear` → **no hay horizonte de desgaste ni planificación de stint**.
- Ride height lee basura (44 m).
- No hay array de participantes con nombres → **el leaderboard es imposible**
  (por eso el botón de estrategia se reemplazó por el de gomas).

**Trampas conocidas** (por si portas algo más):
- Las marchas van 0=R, 1=N, 2+ — al revés de la convención de AMS2 (-1=R, 0=N).
- `physics.gear` se glitchea a 1 durante los cambios; usa `graphics.gear_int`.
- `physics.fuel` da basura (4.7 L con el estanque en 65); usa
  `graphics.fuel_liter_current_quantity`.
- No existe velocidad de amortiguador nativa (en ningún sim): hay que derivarla
  de `suspensionTravel`. El **signo** de compresión se auto-calibra correlacionando
  el freno contra el recorrido delantero (frenar comprime la delantera).
- `current_km` es **acumulado de sesión y grueso** (~50 m por paso), no sirve como
  distancia de vuelta. El analizador la recomputa integrando velocidad (`dist += v·dt`)
  y reescala entre vueltas antes de comparar, porque la integración deriva ~3%.

**Pendiente**: el tiempo exacto de última/mejor vuelta vive en la cola no decodificada
de graphics. Necesita una captura con 2+ vueltas cronometradas limpias en circuito
(en mundo abierto esos campos leen el centinela 65535). Mientras tanto el grabador
mide la vuelta con el cronómetro, lo que la deja ~1 frame corta.

## Overlay y juego online

Leer memoria compartida es el mismo patrón que usa SimHub hace 10+ años, y EVO no
trae anti-cheat de kernel. El juego corre borderless, así que un overlay always-on-top
funcionaría — pero la pantalla secundaria vía LAN (el patrón de AMS2) es mejor para
análisis y no le roba frames al juego.
