# SEGA MENU

Menú para jugar a la Mega Drive en la PC, hecho para la familia.

- Carátulas, capturas, pantalla de título, sinopsis y datos de cada juego.
- El juego elegido corre de fondo con su demo y su música.
- Se juega adentro del menú, con joystick (DualSense o cualquier mando) o teclado.
- Partidas guardadas con foto, guardado rápido y pausa.
- Se actualiza solo desde esta página.

**Los juegos no vienen incluidos.** El menú los busca en una carpeta (de fábrica, `Z:\HECTOOL\ROMS\SEGA`, el disco de la familia).
Se puede elegir otra desde el menú del sistema.

## Cómo instalarlo

1. Bajar `SEGA-MENU-vX.Y.Z.zip` de la sección **Releases**.
2. Descomprimirlo donde quieras (por ejemplo `D:\Juegos`).
3. Abrir `SEGA MENU\SEGA MENU.exe`.

Cuando hay una versión nueva, el menú avisa: **CREATE** (o **Esc**) → **Actualizar**. Los favoritos y las partidas no se pierden.

## Controles

| | Mando (DualSense) | Teclado |
|---|---|---|
| Moverse | cruceta o palanca | W A S D |
| Categoría | L1 / R1 | U / I |
| Jugar | ✕ u OPTIONS | J o Enter |
| Volver | ○ | K |
| Favorito | △ | H |
| Música de fondo | □ | M |
| Menú del sistema | CREATE | Esc |

**En el juego:** □ ✕ ○ = A B C · L1 △ R1 = X Y Z · OPTIONS = Start · R2 = Mode.
**PS** (o Esc) = pausa: guardar, cargar, rayas de tele, reiniciar, salir. **Panel táctil** (o F5) = guardado rápido.
Con el teclado y un mando a la vez, el teclado es el jugador 2 (W A S D, H J K, Y U I, Enter).

En el menú del sistema se puede activar **"Abrir con el botón PS al prender la PC"** (viene apagado).

## Para el que lo programa

- `SEGA MENU.pyw` — el menú (pygame-ce, numpy, Pillow).
- `nucleo.py` — conexión con el núcleo libretro Genesis Plus GX (proceso aparte, el sonido marca el ritmo).
- `sistema.py` — configuración de cada PC, botón PS y actualizaciones (`VERSION` y `REPO`).
- `datos/` — catálogo, carátulas, capturas, títulos, fuente y núcleo.
- `compilar.bat` — arma el `.exe` (PyInstaller) y el zip para el release.

Para publicar una versión: subir `VERSION` en `sistema.py`, correr `compilar.bat` y crear un release
con el tag `vX.Y.Z` y el zip de `release\`.

Ver [LICENCIAS.md](LICENCIAS.md) para los créditos de lo que no es propio.
