"""SEGA MENU — elige un juego de Mega Drive y lo juega ahí mismo con el núcleo Genesis Plus GX.

Teclado:  A / D  moverse          W / S  saltar de a 10
          I / U  categoría         J / Enter  jugar
          H      favorito          Y  buscar      K  volver      Esc  salir
En el juego: Esc o PS = pausa (guardar, cargar, salir) · F5 o panel táctil = guardado rápido
"""
import ctypes
import hashlib
import json
import math
import os
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import numpy as np
import pygame

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import sistema  # noqa: E402
from sistema import AQUI, DATOS_DIR, USUARIO, VERSION  # noqa: E402
try:
    from nucleo import Nucleo
except Exception:          # sin núcleo el menú sigue andando con capturas fijas
    Nucleo = None

CARPETA_JUEGOS = Path(sistema.leer_config()["juegos"])      # en la casa: Z:\HECTOOL\ROMS\SEGA
CATALOGO = DATOS_DIR / "catalogo.json"
DATOS = USUARIO / "datos_usuario.json"
FUENTE_PIXEL = DATOS_DIR / "fuentes" / "PressStart2P-Regular.ttf"
NUCLEO_DLL = DATOS_DIR / "nucleo" / "genesis_plus_gx_libretro.dll"
CACHE_DEMOS = USUARIO / "demos"                              # puntos de demo ya encontrados
ESPERA_VIVO = 1.1          # segundos quieto sobre un juego antes de que arranque de fondo
VOLUMEN_VIVO = 0.45
VOLUMEN_JUEGO = 1.0
WIN_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

ROJO = (214, 34, 42)
AZUL = (47, 111, 214)
BLANCO = (242, 244, 248)
GRIS = (168, 176, 190)
GRIS_OSCURO = (92, 98, 112)
NEGRO = (8, 9, 12)
AMARILLO = (255, 210, 63)

COLOR_GENERO = {
    "Plataformas": (59, 111, 214), "Deportes": (47, 158, 91), "Naves": (125, 79, 209),
    "Beat 'em up": (208, 100, 58), "Disparos": (184, 67, 63), "Lucha": (194, 58, 107),
    "Rol": (165, 130, 47), "Estrategia": (77, 138, 138), "Carreras": (210, 161, 43),
    "Acción": (91, 107, 133), "Puzzle": (58, 160, 184), "Aventura": (106, 143, 58),
}


# ---------------------------------------------------------------- datos

def cargar_juegos():
    """Catálogo (rutas relativas) + ROMs nuevas que haya en la carpeta de juegos. Lista vacía si no está."""
    if not CARPETA_JUEGOS.is_dir():
        return []
    datos = json.loads(CATALOGO.read_text(encoding="utf-8"))
    juegos = []
    for j in datos:
        j["rom"] = str(CARPETA_JUEGOS / j["rom"])
        if Path(j["rom"]).exists():
            juegos.append(j)
    conocidos = {j["rom"].lower() for j in datos}
    for f in CARPETA_JUEGOS.rglob("*"):
        if f.suffix.lower() in (".smd", ".bin", ".md", ".gen") and str(f).lower() not in conocidos:
            juegos.append({"rom": str(f), "titulo": f.stem.replace("_", " "), "genero": "Sin catalogar"})
    juegos.sort(key=lambda j: j["titulo"].lower())
    return juegos


def _rel(ruta):
    try:
        return str(Path(ruta).relative_to(CARPETA_JUEGOS))
    except ValueError:
        return ruta


def _abs(ruta):
    return ruta if Path(ruta).is_absolute() else str(CARPETA_JUEGOS / ruta)


def leer_datos():
    try:
        d = json.loads(DATOS.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("favoritos", [])
    d.setdefault("stats", {})
    # en disco se guardan rutas relativas (así sirven aunque la carpeta de juegos cambie de lugar)
    d["favoritos"] = [_abs(r) for r in d["favoritos"]]
    d["stats"] = {_abs(r): v for r, v in d["stats"].items()}
    if d.get("rom"):
        d["rom"] = _abs(d["rom"])
    return d


def guardar_datos(d):
    try:
        tmp = DATOS.with_suffix(".tmp")
        d = dict(d, favoritos=[_rel(r) for r in d["favoritos"]], stats={_rel(r): v for r, v in d["stats"].items()},
                 rom=_rel(d["rom"]) if d.get("rom") else None)
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DATOS)
    except Exception:
        pass


PARTIDAS = USUARIO / "partidas"            # partidas de esta PC
NOMBRE_SLOT = {0: "Guardado rápido", 1: "Slot 1", 2: "Slot 2", 3: "Slot 3", 4: "Slot 4"}


def carpeta_partidas(juego):
    return PARTIDAS / Path(juego["rom"]).stem


def estados_de(juego):
    """Partidas guardadas desde el menú: slot0 (rápido, panel táctil) y slot1..slot4. Más nueva primero."""
    d = carpeta_partidas(juego)
    res = []
    for n in range(5):
        f = d / f"slot{n}.est"
        if f.exists():
            res.append((n, f.stat().st_mtime))
    res.sort(key=lambda x: -x[1])
    return res


def hace(ts):
    seg = time.time() - ts
    if seg < 90:
        return "recién"
    if seg < 3600:
        return f"hace {int(seg // 60)} min"
    if seg < 86400:
        h = int(seg // 3600)
        return f"hace {h} hora" + ("s" if h > 1 else "")
    d = int(seg // 86400)
    if d < 30:
        return "ayer" if d == 1 else f"hace {d} días"
    return datetime.fromtimestamp(ts).strftime("el %d/%m/%Y")


def duracion(seg):
    m = int(seg // 60)
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


# ---------------------------------------------------------------- Windows

user32 = ctypes.windll.user32


def traer_al_frente(hwnd):
    """Poner la ventana delante con foco, aunque el programa lo haya abierto otro desde atrás
    (Windows lo bloquea): un instante "siempre visible", Alt suelto y tomar el foco."""
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    planos = 0x0001 | 0x0002 | 0x0040   # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, planos)     # HWND_TOPMOST
    user32.keybd_event(0x12, 0, 0, 0)   # Alt suelto: permite SetForegroundWindow
    user32.keybd_event(0x12, 0, 2, 0)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, planos)     # HWND_NOTOPMOST


# ---------------------------------------------------------------- sonido (sintetizado, propio)

def crear_sonidos():
    try:
        pygame.mixer.init(44100, -16, 2, 512)
    except pygame.error:
        return {}
    sr = 44100

    def tono(notas, dur, onda="cuadrada", vol=0.18):
        partes = []
        for f in notas:
            t = np.arange(int(sr * dur)) / sr
            if onda == "cuadrada":
                w = np.sign(np.sin(2 * np.pi * f * t)) * 0.6
            else:
                w = np.sin(2 * np.pi * f * t)
            env = np.exp(-t * 18) * np.minimum(1, t * 400)
            partes.append(w * env)
        s = np.concatenate(partes) * vol
        s = (np.clip(s, -1, 1) * 32767).astype(np.int16)
        return pygame.sndarray.make_sound(np.column_stack([s, s]).copy())

    return {
        "mover": tono([1318], 0.045, vol=0.10),
        "saltar": tono([988, 1318], 0.04, vol=0.10),
        "categoria": tono([659, 880], 0.05, onda="seno", vol=0.25),
        "elegir": tono([523, 659, 784, 1046], 0.06, vol=0.14),
        "volver": tono([784, 523], 0.05, vol=0.12),
        "favorito": tono([1046, 1568], 0.06, onda="seno", vol=0.25),
    }


# ---------------------------------------------------------------- mando (DualSense u otro)

BOTON_PANEL = getattr(pygame, "CONTROLLER_BUTTON_TOUCHPAD", 20)

COLOR_PS = {"cruz": (124, 178, 232), "circulo": (255, 102, 102), "triangulo": (64, 224, 176),
            "cuadrado": (232, 138, 216)}


class Mando:
    """Lee el mando con la interfaz de SDL (que entiende el DualSense) y lo traduce a las teclas del menú."""
    INICIO, CADA = 0.30, 0.075            # repetición al mantener una dirección

    def __init__(self):
        self.c = None
        self.dir = None
        self.t_prox = 0.0
        try:
            from pygame._sdl2 import controller
            controller.init()
            self.mod = controller
        except Exception:
            self.mod = None
        self.abrir()

    def abrir(self):
        """Abre todos los mandos conectados: el primero maneja el menú y es el jugador 1."""
        if self.mod is None:
            return
        self.lista = []
        for i in range(self.mod.get_count()):
            try:
                if self.mod.is_controller(i):
                    self.lista.append(self.mod.Controller(i))
            except Exception:
                pass
        self.c = self.lista[0] if self.lista else None

    def conectados(self):
        res = []
        for c in getattr(self, "lista", []):
            try:
                if c.attached():
                    res.append(c)
            except Exception:
                pass
        return res

    @staticmethod
    def mascara(c):
        """Botones del mando como los lee el núcleo (bits = ids de libretro), con el reparto de siempre:
        □ ✕ ○ = A B C · L1 △ R1 = X Y Z · OPTIONS = Start · R2 = Mode · cruceta o palanca = direcciones."""
        m = 0
        try:
            b = c.get_button
            x = c.get_axis(pygame.CONTROLLER_AXIS_LEFTX)
            y = c.get_axis(pygame.CONTROLLER_AXIS_LEFTY)
            pares = ((b(pygame.CONTROLLER_BUTTON_DPAD_UP) or y < -16000, 4),
                     (b(pygame.CONTROLLER_BUTTON_DPAD_DOWN) or y > 16000, 5),
                     (b(pygame.CONTROLLER_BUTTON_DPAD_LEFT) or x < -16000, 6),
                     (b(pygame.CONTROLLER_BUTTON_DPAD_RIGHT) or x > 16000, 7),
                     (b(pygame.CONTROLLER_BUTTON_X), 1),               # □ -> A
                     (b(pygame.CONTROLLER_BUTTON_A), 0),               # ✕ -> B
                     (b(pygame.CONTROLLER_BUTTON_B), 8),               # ○ -> C
                     (b(pygame.CONTROLLER_BUTTON_LEFTSHOULDER), 10),   # L1 -> X
                     (b(pygame.CONTROLLER_BUTTON_Y), 9),               # △ -> Y
                     (b(pygame.CONTROLLER_BUTTON_RIGHTSHOULDER), 11),  # R1 -> Z
                     (b(pygame.CONTROLLER_BUTTON_START), 3),           # OPTIONS -> Start
                     (c.get_axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT) > 16000, 2))  # R2 -> Mode
            for apretado, bit in pares:
                if apretado:
                    m |= 1 << bit
        except Exception:
            pass
        return m

    @property
    def conectado(self):
        try:
            return self.c is not None and self.c.attached()
        except Exception:
            return False

    def tecla_de(self, boton):
        return {
            pygame.CONTROLLER_BUTTON_A: pygame.K_j,             # ✕ jugar / aceptar
            pygame.CONTROLLER_BUTTON_START: pygame.K_j,         # OPTIONS también juega
            pygame.CONTROLLER_BUTTON_B: pygame.K_k,             # ○ volver / cancelar
            pygame.CONTROLLER_BUTTON_Y: pygame.K_h,             # △ favorito
            pygame.CONTROLLER_BUTTON_X: pygame.K_m,             # □ música
            pygame.CONTROLLER_BUTTON_LEFTSHOULDER: pygame.K_u,  # L1 categoría anterior
            pygame.CONTROLLER_BUTTON_RIGHTSHOULDER: pygame.K_i, # R1 categoría siguiente
            pygame.CONTROLLER_BUTTON_BACK: pygame.K_ESCAPE,     # CREATE salir
        }.get(boton)

    def direccion(self):
        """Tecla de dirección que se debe aplicar ahora (cruceta o palanca izquierda, con repetición)."""
        if not self.conectado:
            return None
        c = self.c
        d = None
        try:
            x = c.get_axis(pygame.CONTROLLER_AXIS_LEFTX)
            y = c.get_axis(pygame.CONTROLLER_AXIS_LEFTY)
            if c.get_button(pygame.CONTROLLER_BUTTON_DPAD_LEFT) or x < -20000:
                d = pygame.K_a
            elif c.get_button(pygame.CONTROLLER_BUTTON_DPAD_RIGHT) or x > 20000:
                d = pygame.K_d
            elif c.get_button(pygame.CONTROLLER_BUTTON_DPAD_UP) or y < -20000:
                d = pygame.K_w
            elif c.get_button(pygame.CONTROLLER_BUTTON_DPAD_DOWN) or y > 20000:
                d = pygame.K_s
        except Exception:
            return None
        ahora = time.time()
        if d != self.dir:
            self.dir = d
            self.t_prox = ahora + self.INICIO
            return d
        if d is not None and ahora >= self.t_prox:
            self.t_prox = ahora + self.CADA
            return d
        return None


class TeclaFalsa:
    def __init__(self, key):
        self.key = key


# ---------------------------------------------------------------- juego corriendo de fondo

class FondoVivo:
    """El juego corriendo de fondo. El emulador vive en otro proceso (ver nucleo.proceso_fondo)
    con su propio sonido marcando el ritmo; acá solo se manda qué juego correr y se levanta el
    último cuadro de una memoria compartida."""

    def __init__(self):
        self.rom = None
        self.alfa = 0.0
        self._silencio = False
        self._visto = 0
        self._grande = None
        self._pos = (0, 0)
        self._mascara = None
        self.proc = None
        self.mem = None
        if Nucleo is None or not NUCLEO_DLL.exists():
            return
        try:
            import multiprocessing as mp
            from multiprocessing import shared_memory
            from nucleo import proceso_fondo, TAM_MEMORIA, CAB, ANCHO_MAX, ALTO_MAX
            self.mem = shared_memory.SharedMemory(create=True, size=TAM_MEMORIA)
            self.cab = np.ndarray((CAB,), dtype=np.int64, buffer=self.mem.buf)
            self.cab[:] = 0
            self.pix = np.ndarray((ANCHO_MAX * ALTO_MAX * 3,), dtype=np.uint8, buffer=self.mem.buf, offset=CAB * 8)
            self.conn, hijo = mp.Pipe()
            self.proc = mp.Process(target=proceso_fondo, args=(hijo, self.mem.name, str(NUCLEO_DLL), VOLUMEN_VIVO,
                                                               str(USUARIO / "error.log")), daemon=True)
            self.proc.start()
        except Exception:
            self.proc = None

    @property
    def disponible(self):
        return self.proc is not None and self.proc.is_alive()

    @property
    def silencio(self):
        return self._silencio

    @silencio.setter
    def silencio(self, v):
        self._silencio = bool(v)
        self._mandar(("silencio", self._silencio))

    @property
    def emu_fps(self):
        return int(self.cab[3]) if self.proc else 0

    @property
    def cortes(self):
        return int(self.cab[4]) if self.proc else 0

    def _mandar(self, orden):
        if self.disponible:
            try:
                self.conn.send(orden)
            except (OSError, EOFError):
                pass

    @property
    def buscando(self):
        return bool(self.rom) and self.proc is not None and int(self.cab[5]) == 2

    def cargar(self, juego):
        rom = juego["rom"]
        ref = None
        if juego.get("caja") and (DATOS_DIR / "titulos" / juego["caja"]).exists():
            try:
                from nucleo import huella
                from PIL import Image
                ref = huella(np.asarray(Image.open(DATOS_DIR / "titulos" / juego["caja"]).convert("RGB"))).tobytes()
            except Exception:
                ref = None
        # "v2": los puntos guardados antes del arreglo de las cracktros se descartan
        clave = hashlib.md5(("v2|" + rom.lower()).encode("utf-8")).hexdigest()[:20]
        archivo = str(CACHE_DEMOS / f"{clave}.est")
        self._mandar(("cargar", rom, ref, archivo))
        self.rom = rom
        self._visto = int(self.cab[0]) if self.proc else 0
        self.alfa = 0.0
        self._grande = None

    def parar(self):
        if self.rom:
            self._mandar(("parar",))
        self.rom = None
        self.alfa = 0.0
        self._grande = None

    def imagen(self, W, H, alfa_velo):
        """Último cuadro con el velo del menú y las rayas de tele ya aplicados (a 320×224, que es barato),
        agrandado para cubrir W×H. Devuelve (superficie, posición) o (None, None)."""
        if not self.rom or not self.proc:
            return None, None
        n = int(self.cab[0])
        if n != self._visto:
            self._visto = n
            w, h = int(self.cab[1]), int(self.cab[2])
            if w <= 0 or h <= 0:
                return self._grande, self._pos
            c = self.pix[:w * h * 3].reshape(h, w, 3).astype(np.float32)
            k = max(W / w, H / h)
            sw, sh = int(w * k), int(h * k)
            ox, oy = (W - sw) // 2, (H - sh) // 2
            if self._mascara is None or self._mascara[0] != (w, h, W, H):
                xs = np.clip((ox + (np.arange(w) + 0.5) * k).astype(int), 0, W - 1)
                ys = np.clip((oy + (np.arange(h) + 0.5) * k).astype(int), 0, H - 1)
                factor = (1 - alfa_velo[np.ix_(xs, ys)].T.astype(np.float32) / 255) * 0.62
                self._mascara = ((w, h, W, H), factor[..., None])
            c *= self._mascara[1]
            # solo las líneas que entran en pantalla (el resto quedaría afuera igual)
            f0 = max(0, int(-oy / k))
            f1 = min(h, int(np.ceil((H - oy) / k)))
            c = c[f0:f1]
            doble = np.repeat(c, 2, axis=0)
            doble[1::2] *= 0.55                     # rayas de tele: una línea oscura por línea del juego
            doble = np.ascontiguousarray(doble.astype(np.uint8))
            chica = pygame.image.frombuffer(doble.tobytes(), (w, doble.shape[0]), "RGB").convert()
            self._grande = pygame.transform.scale(chica, (sw, int((f1 - f0) * k)))
            self._pos = (ox, oy + int(f0 * k))
            brillo = float(self.pix[:w * h * 3:97].mean())
            objetivo = 1.0 if brillo > 10 else 0.0
            self.alfa += (objetivo - self.alfa) * 0.08
        return self._grande, self._pos

    # ---- jugar de verdad (mismo proceso del emulador, en "modo juego")
    def jugar(self, rom, archivo_sram):
        self.rom = None
        self._grande = None
        self._visto = int(self.cab[0]) if self.proc else 0
        self.cab[6] = self.cab[7] = 0
        self._mandar(("jugar", rom, archivo_sram, VOLUMEN_JUEGO))

    def botones(self, j1, j2):
        if self.proc is not None:
            self.cab[6], self.cab[7] = j1, j2

    def pausa(self, si):
        self._mandar(("pausa", bool(si)))

    def guardar(self, archivo):
        self._mandar(("guardar", str(archivo)))

    def cargar_estado(self, archivo):
        self._mandar(("cargar_estado", str(archivo)))

    def guardar_sram(self, archivo):
        self._mandar(("guardar_sram", str(archivo)))

    def reiniciar(self):
        self._mandar(("reiniciar",))

    def respuestas(self):
        """Mensajes del emulador: ("jugando", ok), ("guardado", ok, ruta), ("cargado", ok, ruta)."""
        res = []
        try:
            while self.proc is not None and self.conn.poll():
                res.append(self.conn.recv())
        except (OSError, EOFError):
            pass
        return res

    def cuadro_juego(self, W, H, rayas):
        """Último cuadro del juego en 4:3 a todo el alto, con rayas de tele opcionales.
        Devuelve (superficie, posición, cuadro_crudo) o (None, None, None)."""
        if self.proc is None:
            return None, None, None
        n = int(self.cab[0])
        w, h = int(self.cab[1]), int(self.cab[2])
        if n != self._visto and w > 0 and h > 0:
            self._visto = n
            c = self.pix[:w * h * 3].reshape(h, w, 3)
            self._crudo = c.copy()
            alto = H
            ancho = min(W, H * 4 // 3)
            if rayas:
                doble = np.repeat(c, 2, axis=0)
                doble[1::2] = (doble[1::2].astype(np.uint16) * 160 // 255).astype(np.uint8)
                chica = pygame.image.frombuffer(np.ascontiguousarray(doble).tobytes(), (w, h * 2), "RGB").convert()
                self._grande = pygame.transform.scale(chica, (ancho, alto))
            else:
                chica = pygame.image.frombuffer(np.ascontiguousarray(c).tobytes(), (w, h), "RGB").convert()
                self._grande = pygame.transform.scale(chica, (ancho, alto))
            self._pos = ((W - ancho) // 2, (H - alto) // 2)
        return self._grande, self._pos, getattr(self, "_crudo", None)

    def cerrar(self):
        if self.proc is not None:
            self._mandar(("salir",))
            self.proc.join(2)
            if self.proc.is_alive():
                self.proc.terminate()
        if self.mem is not None:
            try:
                del self.cab, self.pix
                self.mem.close()
                self.mem.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------- menú

class Menu:
    def __init__(self):
        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
        # leer el mando por DirectInput: con HIDAPI (lo normal de SDL) el DualSense por Bluetooth pasa a
        # "modo extendido" y los programas viejos dejan de recibir sus botones. Así anda probado.
        os.environ["SDL_JOYSTICK_HIDAPI"] = "0"
        pygame.init()
        self.mando = Mando()
        pygame.display.set_caption("Menú SEGA")
        if os.environ.get("SDL_VIDEODRIVER") == "dummy":
            self.pantalla = pygame.display.set_mode((1920, 1080))
        else:
            info = pygame.display.Info()
            self.pantalla = pygame.display.set_mode((info.current_w, info.current_h), pygame.NOFRAME)
        self.W, self.H = self.pantalla.get_size()
        self.S = min(self.W / 1920, self.H / 1080)
        pygame.key.set_repeat(260, 55)
        self.reloj = pygame.time.Clock()
        self.sonidos = crear_sonidos()

        self.juegos = cargar_juegos()
        self.datos = leer_datos()
        self.cache_cajas = {}
        self.cache_img = {}
        self._fuentes()
        self._decorados()

        self.busqueda = ""
        self.buscando = False
        self.popup = None            # ("jugar", opciones, sel) | ("salir",)
        self.partida = None          # juego corriendo adentro del menú
        self.aviso = ("", 0)
        self.armar_categorias()
        self.cat = self.datos.get("categoria") if self.datos.get("categoria") in self.categorias else "Todos"
        self.filtrar(conservar=self.datos.get("rom"))
        self.pos = float(self.sel)
        self.fondo_actual = None
        self.fondo_previo = None
        self.fundido = 1.0
        self.rom_fondo = None
        self.actualizacion = None
        threading.Thread(target=self._buscar_actualizacion, daemon=True).start()
        self.vivo = FondoVivo()
        self.vivo.silencio = bool(self.datos.get("silencio"))
        self.t_sel = time.time()

    def _buscar_actualizacion(self):
        if sistema.CONGELADO:
            r = sistema.buscar_actualizacion()
            if r:
                self.actualizacion = r
                self.avisar(f"Versión nueva {r[0]}: CREATE o Esc → Actualizar")

    # ---------- recursos
    def _fuentes(self):
        S = self.S

        def win(nombre, tam):
            p = WIN_FONTS / nombre
            return pygame.font.Font(str(p), max(8, int(tam * S))) if p.exists() else pygame.font.SysFont("segoeui", int(tam * S))

        def pix(tam):
            return pygame.font.Font(str(FUENTE_PIXEL), max(6, int(tam * S))) if FUENTE_PIXEL.exists() \
                else pygame.font.SysFont("consolas", int(tam * S), bold=True)
        self.f_titulo = win("seguisb.ttf", 58)
        self.f_texto = win("segoeui.ttf", 25)
        self.f_dato = win("segoeui.ttf", 21)
        self.f_dato_b = win("seguisb.ttf", 21)
        self.f_chip = win("seguisb.ttf", 20)
        self.f_px = pix(13)
        self.f_px_chico = pix(10)
        self.f_px_grande = pix(22)
        self.f_reloj = win("seguisb.ttf", 26)

    def _decorados(self):
        W, H = self.W, self.H
        # rayas de tele de tubo
        self.rayas = pygame.Surface((W, H), pygame.SRCALPHA)
        paso = max(2, int(3 * self.S))
        for y in range(0, H, paso):
            pygame.draw.line(self.rayas, (0, 0, 0, 46), (0, y), (W, y))
        # degradado oscuro a la izquierda y abajo para leer el texto
        tira = pygame.Surface((256, 1), pygame.SRCALPHA)
        for x in range(256):
            tira.set_at((x, 0), (8, 9, 12, int(235 * (1 - x / 255) ** 1.4)))
        self.degr_izq = pygame.transform.smoothscale(tira, (int(W * 0.78), H))
        tira = pygame.Surface((1, 256), pygame.SRCALPHA)
        for y in range(256):
            tira.set_at((0, y), (8, 9, 12, int(250 * (y / 255) ** 1.6)))
        self.degr_abajo = pygame.transform.smoothscale(tira, (W, int(H * 0.55)))
        # los dos degradados juntos en una sola capa: una pasada menos por cuadro
        self.velo = pygame.Surface((W, H), pygame.SRCALPHA)
        self.velo.blit(self.degr_izq, (0, 0))
        self.velo.blit(self.degr_abajo, (0, H - self.degr_abajo.get_height()), special_flags=pygame.BLEND_RGBA_MAX)
        self.alfa_degr = pygame.surfarray.array_alpha(self.velo)
        a_velo = pygame.surfarray.pixels_alpha(self.velo)
        a_rayas = pygame.surfarray.pixels_alpha(self.rayas)
        a_velo[:] = (255 - (255 - a_velo.astype(np.uint16)) * (255 - a_rayas.astype(np.uint16)) // 255).astype(np.uint8)
        del a_velo, a_rayas

    def img(self, carpeta, nombre, tam, modo="contener"):
        clave = (carpeta, nombre, tam, modo)
        if clave in self.cache_img:
            return self.cache_img[clave]
        sup = None
        p = DATOS_DIR / carpeta / nombre
        if nombre and p.exists():
            try:
                bruto = pygame.image.load(str(p)).convert()
                w, h = bruto.get_size()
                if modo == "cubrir":
                    k = max(tam[0] / w, tam[1] / h)
                else:
                    k = min(tam[0] / w, tam[1] / h)
                sup = pygame.transform.smoothscale(bruto, (max(1, int(w * k)), max(1, int(h * k))))
            except pygame.error:
                sup = None
        self.cache_img[clave] = sup
        if len(self.cache_img) > 600:
            for k in list(self.cache_img)[:200]:
                del self.cache_img[k]
        return sup

    def caja(self, juego, alto):
        alto = int(alto)
        if juego.get("caja"):
            s = self.img("caratulas", juego["caja"], (int(alto * 0.78), alto))
            if s:
                return s
        # tarjeta para los que no tienen caja
        clave = ("tarjeta", juego["rom"], alto)
        if clave not in self.cache_img:
            w = int(alto * 0.72)
            t = pygame.Surface((w, alto))
            col = COLOR_GENERO.get(juego["genero"], (70, 76, 90))
            t.fill(col)
            pygame.draw.rect(t, NEGRO, (0, 0, w, int(alto * 0.12)))
            et = self.f_px_chico.render("MEGA DRIVE", True, BLANCO)
            t.blit(et, ((w - et.get_width()) // 2, int(alto * 0.06) - et.get_height() // 2))
            self._texto_envuelto(t, juego["titulo"], self.f_chip, BLANCO, (10, int(alto * 0.35)), w - 20, 5, centrar=True)
            self.cache_img[clave] = t
        return self.cache_img[clave]

    # ---------- categorías y filtro
    def armar_categorias(self):
        cuenta = {}
        for j in self.juegos:
            cuenta[j["genero"]] = cuenta.get(j["genero"], 0) + 1
        orden = sorted(cuenta, key=lambda g: (g in ("Otros", "Sin catalogar"), -cuenta[g]))
        especiales = []
        if self.datos["favoritos"]:
            especiales.append("Favoritos")
        if self.datos["stats"]:
            especiales += ["Recientes", "Más jugados"]
        self.categorias = ["Todos"] + especiales + orden

    def filtrar(self, conservar=None):
        q = self.busqueda.lower()
        st = self.datos["stats"]
        if self.cat == "Favoritos":
            lista = [j for j in self.juegos if j["rom"] in self.datos["favoritos"]]
        elif self.cat == "Recientes":
            lista = sorted([j for j in self.juegos if j["rom"] in st], key=lambda j: -st[j["rom"]].get("ultima", 0))
        elif self.cat == "Más jugados":
            lista = sorted([j for j in self.juegos if j["rom"] in st], key=lambda j: -st[j["rom"]].get("segundos", 0))
        elif self.cat == "Todos":
            lista = list(self.juegos)
        else:
            lista = [j for j in self.juegos if j["genero"] == self.cat]
        if q:
            lista = [j for j in lista if q in j["titulo"].lower() or q in (j.get("saga") or "").lower()]
        self.lista = lista
        self.sel = 0
        if conservar:
            for i, j in enumerate(lista):
                if j["rom"] == conservar:
                    self.sel = i
                    break
        self.pos = float(self.sel)

    # ---------- acciones
    def sonar(self, n):
        s = self.sonidos.get(n)
        if s:
            s.play()

    def mover(self, d):
        if not self.lista:
            return
        nuevo = max(0, min(len(self.lista) - 1, self.sel + d))
        if nuevo != self.sel:
            self.sel = nuevo
            self.sonar("mover" if abs(d) == 1 else "saltar")

    def cambiar_cat(self, paso):
        i = self.categorias.index(self.cat)
        self.cat = self.categorias[(i + paso) % len(self.categorias)]
        self.filtrar()
        self.sonar("categoria")

    def favorito(self):
        if not self.lista:
            return
        rom = self.lista[self.sel]["rom"]
        favs = self.datos["favoritos"]
        if rom in favs:
            favs.remove(rom)
            self.avisar("Quitado de favoritos")
        else:
            favs.append(rom)
            self.avisar("Agregado a favoritos")
        guardar_datos(self.datos)
        self.armar_categorias()
        if self.cat not in self.categorias:
            self.cat = "Todos"
            self.filtrar(conservar=rom)
        elif self.cat == "Favoritos":
            self.filtrar(conservar=rom)
        self.sonar("favorito")

    def avisar(self, txt):
        self.aviso = (txt, time.time())

    def pedir_jugar(self):
        if not self.lista:
            return
        juego = self.lista[self.sel]
        slots = estados_de(juego)
        self.sonar("elegir")
        if not slots:
            self.empezar_partida(juego, None)
            return
        n0, ts0 = slots[0]
        opciones = [("Continuar", ("estado", n0), f"{NOMBRE_SLOT[n0].lower()}  ·  {hace(ts0)}"),
                    ("Empezar de cero", ("nuevo",), "sin cargar nada")]
        for n, ts in slots[1:]:
            opciones.append((f"Cargar {NOMBRE_SLOT[n].lower()}", ("estado", n),
                             datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")))
        self.popup = ["jugar", opciones, 0, juego]

    # ---------- jugar adentro del menú (núcleo Genesis Plus GX)
    TECLAS_JUEGO = ((pygame.K_w, 4), (pygame.K_UP, 4), (pygame.K_s, 5), (pygame.K_DOWN, 5),
                    (pygame.K_a, 6), (pygame.K_LEFT, 6), (pygame.K_d, 7), (pygame.K_RIGHT, 7),
                    (pygame.K_h, 1), (pygame.K_j, 0), (pygame.K_k, 8),       # A B C
                    (pygame.K_y, 10), (pygame.K_u, 9), (pygame.K_i, 11),     # X Y Z
                    (pygame.K_RETURN, 3), (pygame.K_o, 2))                   # Start, Mode
    OPCIONES_PAUSA = ("Continuar", "Guardar partida", "Cargar partida", "Rayas de tele", "Reiniciar",
                      "Salir al menú")

    def empezar_partida(self, juego, slot):
        if not self.vivo.disponible:
            self.avisar("Falta el núcleo del emulador (carpeta nucleo)")
            return
        self.datos["categoria"] = self.cat
        self.datos["rom"] = juego["rom"]
        guardar_datos(self.datos)
        carpeta = carpeta_partidas(juego)
        sram = carpeta / "bateria.srm"
        origen = sram if sram.exists() else None
        self.vivo.parar()
        self.vivo.jugar(juego["rom"], str(origen) if origen else None)
        if slot is not None:
            self.vivo.cargar_estado(carpeta / f"slot{slot}.est")
        self.partida = {"juego": juego, "t0": time.time(), "pausa": None,
                        "rayas": bool(self.datos.get("rayas_juego", False))}

    def botones_partida(self):
        j = [0, 0]
        mandos = self.mando.conectados()
        if mandos:
            j[0] = Mando.mascara(mandos[0])
        if len(mandos) > 1:
            j[1] = Mando.mascara(mandos[1])
        t = pygame.key.get_pressed()
        m = 0
        for tecla, bit in self.TECLAS_JUEGO:
            if t[tecla]:
                m |= 1 << bit
        j[1 if mandos else 0] |= m                 # con mando, el teclado es el jugador 2
        return j

    def guardar_slot(self, n):
        p = self.partida
        carpeta = carpeta_partidas(p["juego"])
        carpeta.mkdir(parents=True, exist_ok=True)
        self.vivo.guardar(carpeta / f"slot{n}.est")
        crudo = getattr(self.vivo, "_crudo", None)
        if crudo is not None:
            foto = pygame.image.frombuffer(np.ascontiguousarray(crudo).tobytes(), (crudo.shape[1], crudo.shape[0]), "RGB")
            pygame.image.save(pygame.transform.smoothscale(foto, (240, 180)), str(carpeta / f"slot{n}.png"))
        self._slots_cache = None
        self.cache_img = {k: v for k, v in self.cache_img.items() if not (isinstance(k, tuple) and k[0] == "slot")}

    def abrir_pausa(self):
        p = self.partida
        if p and p["pausa"] is None:
            p["pausa"] = {"nivel": "principal", "sel": 0}
            self.vivo.botones(0, 0)
            self.vivo.pausa(True)
            self.sonar("volver")

    def cerrar_pausa(self):
        self.partida["pausa"] = None
        self.vivo.pausa(False)

    def _ops_pausa(self):
        pz, p = self.partida["pausa"], self.partida
        if pz["nivel"] == "guardar":
            existentes = dict(estados_de(p["juego"]))
            return [(n, NOMBRE_SLOT[n], f"usado {hace(existentes[n])}" if n in existentes else "libre")
                    for n in (1, 2, 3, 4)]
        if pz["nivel"] == "cargar":
            return [(n, NOMBRE_SLOT[n], hace(ts)) for n, ts in estados_de(p["juego"])]
        return [(o, o if o != "Rayas de tele" else f"Rayas de tele: {'sí' if p['rayas'] else 'no'}", "")
                for o in self.OPCIONES_PAUSA]

    def tecla_pausa(self, k):
        p = self.partida
        pz = p["pausa"]
        ops = self._ops_pausa()
        if k in (pygame.K_w, pygame.K_UP) and ops:
            pz["sel"] = (pz["sel"] - 1) % len(ops)
            self.sonar("mover")
        elif k in (pygame.K_s, pygame.K_DOWN) and ops:
            pz["sel"] = (pz["sel"] + 1) % len(ops)
            self.sonar("mover")
        elif k in (pygame.K_k, pygame.K_ESCAPE):
            if pz["nivel"] == "principal":
                self.cerrar_pausa()
            else:
                p["pausa"] = {"nivel": "principal", "sel": 1 if pz["nivel"] == "guardar" else 2}
            self.sonar("volver")
        elif k in (pygame.K_j, pygame.K_RETURN) and ops:
            valor = ops[pz["sel"]][0]
            self.sonar("elegir")
            if pz["nivel"] == "guardar":
                self.guardar_slot(valor)
                self.cerrar_pausa()
            elif pz["nivel"] == "cargar":
                self.vivo.cargar_estado(carpeta_partidas(p["juego"]) / f"slot{valor}.est")
                self.cerrar_pausa()
            elif valor == "Continuar":
                self.cerrar_pausa()
            elif valor == "Guardar partida":
                p["pausa"] = {"nivel": "guardar", "sel": 0}
            elif valor == "Cargar partida":
                if estados_de(p["juego"]):
                    p["pausa"] = {"nivel": "cargar", "sel": 0}
                else:
                    self.avisar("Todavía no hay partidas guardadas")
            elif valor == "Rayas de tele":
                p["rayas"] = not p["rayas"]
                self.datos["rayas_juego"] = p["rayas"]
                guardar_datos(self.datos)
                self.vivo._visto = -1                 # rearmar la imagen con o sin rayas
            elif valor == "Reiniciar":
                self.vivo.reiniciar()
                self.cerrar_pausa()
            elif valor == "Salir al menú":
                self.salir_partida()

    def salir_partida(self):
        p = self.partida
        if not p:
            return
        self.vivo.guardar_sram(carpeta_partidas(p["juego"]) / "bateria.srm")
        self.vivo.parar()
        self.partida = None
        seg = time.time() - p["t0"]
        if seg > 15:
            st = self.datos["stats"].setdefault(p["juego"]["rom"], {"veces": 0, "segundos": 0})
            st["veces"] += 1
            st["segundos"] += int(seg)
            st["ultima"] = time.time()
            guardar_datos(self.datos)
            self.armar_categorias()
        self._slots_cache = None
        self.t_sel = time.time()
        self.rom_fondo = None
        self.sonar("volver")

    def paso_partida(self):
        """Un cuadro de la partida: eventos, botones, dibujo. Devuelve False si hay que cerrar el menú."""
        p = self.partida
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                self.salir_partida()
                return False
            if e.type == pygame.CONTROLLERDEVICEADDED:
                self.mando.abrir()
            elif e.type == pygame.CONTROLLERBUTTONDOWN:
                if e.button == pygame.CONTROLLER_BUTTON_GUIDE:
                    self.cerrar_pausa() if p["pausa"] else self.abrir_pausa()
                elif p["pausa"]:
                    k = self.mando.tecla_de(e.button)
                    if k is not None:
                        self.tecla_pausa(k)
            elif e.type == pygame.JOYBUTTONDOWN and e.button == 13 and not p["pausa"]:
                self.guardar_slot(0)                      # panel táctil: guardado rápido
            elif e.type == pygame.KEYDOWN:
                if p["pausa"]:
                    self.tecla_pausa(e.key)
                elif e.key == pygame.K_ESCAPE:
                    self.abrir_pausa()
                elif e.key == pygame.K_F5:
                    self.guardar_slot(0)
            if self.partida is None:
                return True
        if p["pausa"]:
            d = self.mando.direccion()
            if d is not None:
                self.tecla_pausa(d)
                if self.partida is None:
                    return True
        else:
            self.vivo.botones(*self.botones_partida())
        for r in self.vivo.respuestas():
            if r[0] == "jugando" and not r[1]:
                self.salir_partida()
                self.avisar("Este juego no arranca")
                return True
            if r[0] == "guardado":
                self.avisar("Partida guardada" if r[1] else "No se pudo guardar")
            elif r[0] == "cargado":
                self.avisar("Partida cargada" if r[1] else "No se pudo cargar esa partida")
        self.dibujar_partida()
        pygame.display.flip()
        self.reloj.tick(60)
        return True

    def dibujar_partida(self):
        P, S, W, H = self.pantalla, self.S, self.W, self.H
        P.fill(NEGRO)
        sup, pos, crudo = self.vivo.cuadro_juego(W, H, self.partida["rayas"])
        if sup is not None:
            P.blit(sup, pos)
        else:
            t = self.f_px.render("CARGANDO...", True, GRIS_OSCURO)
            P.blit(t, t.get_rect(center=(W // 2, H // 2)))
        if self.partida["pausa"]:
            self._dibujar_pausa()
        self._aviso()

    def _foto_slot(self, n):
        f = carpeta_partidas(self.partida["juego"]) / f"slot{n}.png"
        if not f.exists():
            return None
        clave = ("slot", str(f), f.stat().st_mtime)
        if clave not in self.cache_img:
            try:
                self.cache_img[clave] = pygame.transform.smoothscale(pygame.image.load(str(f)).convert(),
                                                                     (int(160 * self.S), int(120 * self.S)))
            except pygame.error:
                self.cache_img[clave] = None
        return self.cache_img[clave]

    def _dibujar_pausa(self):
        P, S, W, H = self.pantalla, self.S, self.W, self.H
        if getattr(self, "_velo_pausa", None) is None:
            self._velo_pausa = pygame.Surface((W, H))
            self._velo_pausa.set_alpha(175)          # transparencia de superficie: mucho más rápida
        P.blit(self._velo_pausa, (0, 0))
        pz = self.partida["pausa"]
        ops = self._ops_pausa()
        con_fotos = pz["nivel"] in ("guardar", "cargar")
        alto_op = int((132 if con_fotos else 62) * S)
        ancho = int((820 if con_fotos else 640) * S)
        caja = pygame.Rect(0, 0, ancho, int(150 * S) + alto_op * max(1, len(ops)))
        caja.center = (W // 2, H // 2)
        pygame.draw.rect(P, (24, 27, 34), caja, border_radius=int(14 * S))
        pygame.draw.rect(P, ROJO, (caja.x, caja.y, caja.width, int(8 * S)),
                         border_top_left_radius=int(14 * S), border_top_right_radius=int(14 * S))
        titulo = {"principal": "PAUSA", "guardar": "GUARDAR EN...", "cargar": "CARGAR..."}[pz["nivel"]]
        t = self.f_px_grande.render(titulo, True, BLANCO)
        P.blit(t, (caja.x + int(36 * S), caja.y + int(34 * S)))
        self._texto_envuelto(P, self.partida["juego"]["titulo"], self.f_dato, GRIS,
                             (caja.x + int(36 * S), caja.y + int(74 * S)), ancho - int(72 * S), 1)
        y = caja.y + int(116 * S)
        for i, (valor, txt, sub) in enumerate(ops):
            fila = pygame.Rect(caja.x + int(24 * S), y, ancho - int(48 * S), alto_op - int(10 * S))
            if i == pz["sel"]:
                pygame.draw.rect(P, AZUL, fila, border_radius=int(8 * S))
            x = fila.x + int(18 * S)
            if con_fotos:
                foto = self._foto_slot(valor)
                marco = pygame.Rect(x, fila.y + int(4 * S), int(160 * S), int(114 * S))
                pygame.draw.rect(P, (12, 13, 16), marco)
                if foto:
                    P.blit(foto, foto.get_rect(center=marco.center))
                x = marco.right + int(20 * S)
                P.blit(self.f_texto.render(txt, True, BLANCO), (x, fila.y + int(28 * S)))
                P.blit(self.f_dato.render(sub, True, BLANCO if i == pz["sel"] else GRIS), (x, fila.y + int(64 * S)))
            else:
                P.blit(self.f_texto.render(txt, True, BLANCO), (x, fila.y + int(10 * S)))
            y += alto_op
        if self.mando.conectado:
            self._pistas([(["cruceta"], "elegir"), (["cruz"], "aceptar"), (["circulo"], "volver"),
                          (["PS"], "seguir jugando")], 0, caja.bottom - int(46 * S), centro=caja.centerx)
        else:
            t = self.f_px_chico.render("W S ELEGIR    J ACEPTAR    K VOLVER    ESC SEGUIR", True, GRIS)
            P.blit(t, t.get_rect(center=(caja.centerx, caja.bottom - int(28 * S))))

    # ---------- menú del sistema (CREATE / Esc)
    def opciones_sistema(self):
        cfg = sistema.leer_config()
        if self.actualizacion:
            act = ("actualizar", f"Actualizar a la versión {self.actualizacion[0]}", f"tenés la {VERSION}")
        else:
            act = ("buscar", "Buscar actualizaciones", f"versión {VERSION}")
        return [("volver", "Volver al menú", ""),
                ("ps", f"Abrir con el botón PS al prender la PC: {'sí' if cfg.get('boton_ps') else 'no'}",
                 "el botón PS del mando abre el menú"),
                ("carpeta", "Carpeta de juegos", str(CARPETA_JUEGOS)),
                act,
                ("salir", "Salir del menú", "")]

    def abrir_sistema(self):
        self.popup = ["sistema", 0]
        self.sonar("volver")

    def tecla_sistema(self, k):
        ops = self.opciones_sistema()
        if k in (pygame.K_w, pygame.K_UP):
            self.popup[1] = (self.popup[1] - 1) % len(ops)
            self.sonar("mover")
        elif k in (pygame.K_s, pygame.K_DOWN):
            self.popup[1] = (self.popup[1] + 1) % len(ops)
            self.sonar("mover")
        elif k in (pygame.K_k, pygame.K_ESCAPE):
            self.popup = None
            self.sonar("volver")
        elif k in (pygame.K_j, pygame.K_RETURN):
            que = ops[self.popup[1]][0]
            self.sonar("elegir")
            if que == "volver":
                self.popup = None
            elif que == "salir":
                return False
            elif que == "ps":
                si = not sistema.leer_config().get("boton_ps")
                try:
                    sistema.activar_boton_ps(si)
                    self.avisar("Botón PS activado" if si else "Botón PS desactivado")
                except Exception as e:
                    self.avisar(f"No se pudo: {e}")
            elif que == "carpeta":
                self.elegir_carpeta_juegos()
            elif que == "buscar":
                self.actualizacion = sistema.buscar_actualizacion()
                self.avisar(f"Hay una versión nueva: {self.actualizacion[0]}" if self.actualizacion
                            else "Ya tenés la última versión")
            elif que == "actualizar":
                if not sistema.CONGELADO:
                    self.avisar("Esta es la copia de desarrollo: se actualiza con git")
                else:
                    self.avisar("Bajando la actualización...")
                    self.dibujar()
                    pygame.display.flip()
                    try:
                        if sistema.instalar_actualizacion(self.actualizacion[1]):
                            return False          # al cerrarse, se copia la versión nueva y se vuelve a abrir
                        self.avisar("La actualización vino incompleta")
                    except Exception as e:
                        self.avisar(f"No se pudo actualizar: {e}")
        return True

    def elegir_carpeta_juegos(self):
        global CARPETA_JUEGOS
        import tkinter as tk
        from tkinter import filedialog
        pygame.display.iconify()
        raiz = tk.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)
        elegida = filedialog.askdirectory(title="¿Dónde están los juegos de Mega Drive?",
                                          initialdir=str(CARPETA_JUEGOS) if CARPETA_JUEGOS.exists() else None)
        raiz.destroy()
        try:
            traer_al_frente(pygame.display.get_wm_info()["window"])
        except Exception:
            pass
        if not elegida:
            return
        cfg = sistema.leer_config()
        cfg["juegos"] = str(Path(elegida))
        sistema.guardar_config(cfg)
        favs, stats = [_rel(r) for r in self.datos["favoritos"]], {_rel(r): v for r, v in self.datos["stats"].items()}
        CARPETA_JUEGOS = Path(elegida)
        self.datos["favoritos"], self.datos["stats"] = [_abs(r) for r in favs], {_abs(r): v for r, v in stats.items()}
        self.vivo.parar()
        self.juegos = cargar_juegos()
        self.armar_categorias()
        self.cat = "Todos"
        self.filtrar()
        self.popup = None
        self.avisar(f"{len(self.juegos)} juegos encontrados" if self.juegos else "En esa carpeta no hay juegos")

    def _dibujar_sistema(self):
        P, S, W, H = self.pantalla, self.S, self.W, self.H
        ops = self.opciones_sistema()
        sel = self.popup[1]
        ancho, alto_op = int(900 * S), int(84 * S)
        caja = pygame.Rect(0, 0, ancho, int(150 * S) + alto_op * len(ops))
        caja.center = (W // 2, H // 2)
        pygame.draw.rect(P, (24, 27, 34), caja, border_radius=int(14 * S))
        pygame.draw.rect(P, ROJO, (caja.x, caja.y, caja.width, int(8 * S)),
                         border_top_left_radius=int(14 * S), border_top_right_radius=int(14 * S))
        P.blit(self.f_px_grande.render("SEGA MENU", True, BLANCO), (caja.x + int(36 * S), caja.y + int(34 * S)))
        v = self.f_px_chico.render(f"V{VERSION}", True, GRIS)
        P.blit(v, (caja.right - v.get_width() - int(36 * S), caja.y + int(42 * S)))
        y = caja.y + int(90 * S)
        for i, (_, txt, sub) in enumerate(ops):
            fila = pygame.Rect(caja.x + int(24 * S), y, ancho - int(48 * S), alto_op - int(10 * S))
            if i == sel:
                pygame.draw.rect(P, AZUL, fila, border_radius=int(8 * S))
            P.blit(self.f_texto.render(txt, True, BLANCO), (fila.x + int(20 * S), fila.y + int(6 * S)))
            if sub:
                self._texto_envuelto(P, sub, self.f_dato, BLANCO if i == sel else GRIS,
                                     (fila.x + int(20 * S), fila.y + int(38 * S)), fila.width - int(40 * S), 1)
            y += alto_op
        if self.mando.conectado:
            self._pistas([(["cruceta"], "elegir"), (["cruz"], "aceptar"), (["circulo"], "volver")], 0,
                         caja.bottom - int(46 * S), centro=caja.centerx)
        else:
            t = self.f_px_chico.render("W S ELEGIR    J ACEPTAR    K VOLVER", True, GRIS)
            P.blit(t, t.get_rect(center=(caja.centerx, caja.bottom - int(28 * S))))

    # ---------- eventos
    def tecla(self, e):
        k = e.key
        if self.popup:
            tipo = self.popup[0]
            if tipo == "sistema":
                return self.tecla_sistema(k)
            else:
                ops = self.popup[1]
                if k in (pygame.K_w, pygame.K_UP):
                    self.popup[2] = (self.popup[2] - 1) % len(ops)
                    self.sonar("mover")
                elif k in (pygame.K_s, pygame.K_DOWN):
                    self.popup[2] = (self.popup[2] + 1) % len(ops)
                    self.sonar("mover")
                elif k in (pygame.K_j, pygame.K_RETURN):
                    juego, valor = self.popup[3], ops[self.popup[2]][1]
                    self.popup = None
                    self.sonar("elegir")
                    self.empezar_partida(juego, valor[1] if valor[0] == "estado" else None)
                elif k in (pygame.K_k, pygame.K_ESCAPE):
                    self.popup = None
                    self.sonar("volver")
            return True

        if self.buscando:
            if k == pygame.K_RETURN:
                self.buscando = False
                pygame.key.stop_text_input()
            elif k == pygame.K_ESCAPE:
                self.buscando = False
                pygame.key.stop_text_input()
                self.busqueda = ""
                self.filtrar()
            elif k == pygame.K_BACKSPACE:
                self.busqueda = self.busqueda[:-1]
                self.filtrar()
            elif k in (pygame.K_LEFT, pygame.K_RIGHT):
                self.mover(-1 if k == pygame.K_LEFT else 1)
            return True

        if k in (pygame.K_a, pygame.K_LEFT):
            self.mover(-1)
        elif k in (pygame.K_d, pygame.K_RIGHT):
            self.mover(1)
        elif k in (pygame.K_w, pygame.K_UP):
            self.mover(-10)
        elif k in (pygame.K_s, pygame.K_DOWN):
            self.mover(10)
        elif k == pygame.K_HOME:
            self.mover(-10000)
        elif k == pygame.K_END:
            self.mover(10000)
        elif k == pygame.K_i:
            self.cambiar_cat(1)
        elif k == pygame.K_u:
            self.cambiar_cat(-1)
        elif k in (pygame.K_j, pygame.K_RETURN):
            self.pedir_jugar()
        elif k == pygame.K_f:
            self.medidor = not getattr(self, "medidor", False)
        elif k == pygame.K_m:
            self.vivo.silencio = not self.vivo.silencio
            self.datos["silencio"] = self.vivo.silencio
            guardar_datos(self.datos)
            self.avisar("Música de fondo apagada" if self.vivo.silencio else "Música de fondo prendida")
        elif k == pygame.K_h:
            self.favorito()
        elif k == pygame.K_y:
            self.buscando = True
            pygame.key.start_text_input()
            self.sonar("categoria")
        elif k == pygame.K_k:
            if self.busqueda:
                self.busqueda = ""
                self.filtrar()
                self.sonar("volver")
            elif self.cat != "Todos":
                rom = self.lista[self.sel]["rom"] if self.lista else None
                self.cat = "Todos"
                self.filtrar(conservar=rom)
                self.sonar("volver")
        elif k == pygame.K_ESCAPE:
            self.abrir_sistema()
            self.sonar("volver")
        return True

    # ---------- dibujo
    def _texto_envuelto(self, sup, texto, fuente, color, pos, ancho, max_lineas, centrar=False, interlinea=1.25):
        palabras = texto.split()
        lineas, actual = [], ""
        for p in palabras:
            prueba = (actual + " " + p).strip()
            if fuente.size(prueba)[0] <= ancho:
                actual = prueba
            else:
                if actual:
                    lineas.append(actual)
                actual = p
        if actual:
            lineas.append(actual)
        if len(lineas) > max_lineas:
            lineas = lineas[:max_lineas]
            while fuente.size(lineas[-1] + "…")[0] > ancho and " " in lineas[-1]:
                lineas[-1] = lineas[-1].rsplit(" ", 1)[0]
            lineas[-1] += "…"
        x, y = pos
        alto = int(fuente.get_linesize() * interlinea)
        for ln in lineas:
            r = fuente.render(ln, True, color)
            sup.blit(r, (x + (ancho - r.get_width()) // 2 if centrar else x, y))
            y += alto
        return y

    def _estrella(self, cx, cy, radio, color, borde=None):
        """Estrella de 5 puntas dibujada (las fuentes del menú no traen el símbolo ★)."""
        puntos = []
        for i in range(10):
            r = radio if i % 2 == 0 else radio * 0.45
            a = -math.pi / 2 + i * math.pi / 5
            puntos.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        if borde:
            pygame.draw.polygon(self.pantalla, borde, puntos, max(2, int(radio * 0.35)))
        pygame.draw.polygon(self.pantalla, color, puntos)

    def _chip(self, x, y, texto, fondo, fuente=None, color=BLANCO):
        fuente = fuente or self.f_chip
        r = fuente.render(texto, True, color)
        pad = int(12 * self.S)
        rect = pygame.Rect(x, y, r.get_width() + pad * 2, r.get_height() + int(8 * self.S))
        pygame.draw.rect(self.pantalla, fondo, rect, border_radius=int(6 * self.S))
        self.pantalla.blit(r, (x + pad, y + int(4 * self.S)))
        return rect.right + int(10 * self.S)

    def _fondo(self, juego):
        if juego is None:
            return None
        nombre = juego.get("caja")
        s = self.img("capturas", nombre, (self.W, self.H), "cubrir") if nombre else None
        if s is None:
            s = self.img("titulos", nombre, (self.W, self.H), "cubrir") if nombre else None
        if s is None:
            s = pygame.Surface((self.W, self.H))
            s.fill(tuple(int(c * 0.35) for c in COLOR_GENERO.get(juego["genero"], (60, 66, 80))))
        else:
            # suavizado: achicar y agrandar
            chica = pygame.transform.smoothscale(s, (max(1, s.get_width() // 6), max(1, s.get_height() // 6)))
            s = pygame.transform.smoothscale(chica, s.get_size())
        return s

    def dibujar(self):
        if self.partida:
            # con una partida en curso, el menú no dibuja ni toca el juego de fondo
            # (antes pedía la demo y pisaba la partida: arrancaba en la demo y sin botones)
            return
        P, S, W, H = self.pantalla, self.S, self.W, self.H
        juego = self.lista[self.sel] if self.lista else None

        # fondo con fundido al cambiar de juego
        rom = juego["rom"] if juego else None
        if rom != self.rom_fondo:
            self.fondo_previo = self.fondo_actual
            self.fondo_actual = self._fondo(juego)
            self.rom_fondo = rom
            self.fundido = 0.0
            self.vivo.parar()
            self.t_sel = time.time()
        self.fundido = min(1.0, self.fundido + 0.12)
        if self.fundido >= 1.0:
            self.fondo_previo = None

        # el juego corriendo de fondo (arranca tras unos segundos quieto sobre él)
        if rom and self.vivo.disponible and not self.vivo.rom and time.time() - self.t_sel > ESPERA_VIVO:
            self.vivo.cargar(juego)
        grande, pos_vivo = self.vivo.imagen(W, H, self.alfa_degr)
        vivo_alfa = self.vivo.alfa if grande is not None else 0.0

        P.fill(NEGRO)
        if vivo_alfa < 0.97:          # la captura fija solo hace falta mientras el juego no la tapa
            for sup, alfa in ((self.fondo_previo, 1.0), (self.fondo_actual, self.fundido)):
                if sup:
                    sup.set_alpha(int(255 * alfa * 0.55 * (1 - vivo_alfa)))
                    P.blit(sup, ((W - sup.get_width()) // 2, (H - sup.get_height()) // 2))
        if vivo_alfa < 0.97:
            P.blit(self.velo, (0, 0))
        if vivo_alfa > 0.02:          # el cuadro del juego ya trae el velo y las rayas aplicados
            grande.set_alpha(None if vivo_alfa >= 0.97 else int(255 * vivo_alfa))
            P.blit(grande, pos_vivo)

        margen = int(70 * S)
        # barra superior: logo + categorías + reloj
        y_barra = int(34 * S)
        logo = self.f_px_grande.render("MEGA DRIVE", True, BLANCO)
        pygame.draw.rect(P, ROJO, (margen, y_barra - int(6 * S), int(10 * S), logo.get_height() + int(12 * S)))
        P.blit(logo, (margen + int(22 * S), y_barra))
        reloj = self.f_reloj.render(datetime.now().strftime("%H:%M"), True, BLANCO)
        P.blit(reloj, (W - margen - reloj.get_width(), y_barra - int(4 * S)))

        x = margen
        y_cat = int(92 * S)
        i0 = self.categorias.index(self.cat)
        visibles = self.categorias[max(0, i0 - 2):] if i0 > 4 else self.categorias
        if visibles is not self.categorias:
            flecha = self.f_chip.render("‹", True, GRIS)
            P.blit(flecha, (x - int(24 * S), y_cat + int(2 * S)))
        for cat in visibles:
            activo = cat == self.cat
            n = len([j for j in self.lista]) if activo else None
            txt = f"{cat}  {n}" if activo else cat
            ancho = self.f_chip.size(txt)[0] + int(24 * S)
            if x + ancho > W - margen:
                P.blit(self.f_chip.render("›", True, GRIS), (W - margen + int(8 * S), y_cat + int(2 * S)))
                break
            if activo:
                x = self._chip(x, y_cat, txt, AZUL)
            else:
                r = self.f_chip.render(txt, True, GRIS)
                P.blit(r, (x + int(12 * S), y_cat + int(4 * S)))
                x += ancho + int(10 * S)

        # ficha del juego
        if juego:
            self._ficha(juego, margen)
        else:
            if self.juegos:
                P.blit(self.f_texto.render("No hay juegos acá todavía", True, GRIS), (margen, int(220 * S)))
            else:
                P.blit(self.f_titulo.render("No encuentro los juegos", True, BLANCO), (margen, int(170 * S)))
                self._texto_envuelto(P, f"Los busco en {CARPETA_JUEGOS}. Si es el disco de la familia, fijate que "
                                        "esté prendido. Para elegir otra carpeta: CREATE (o Esc) → Carpeta de juegos.",
                                     self.f_texto, GRIS, (margen, int(260 * S)), int(1100 * S), 4)

        # carrusel
        self._carrusel()

        # búsqueda
        if self.buscando or self.busqueda:
            txt = f"Buscar: {self.busqueda}" + ("_" if self.buscando and int(time.time() * 2) % 2 else "")
            self._chip(W - margen - self.f_chip.size(txt)[0] - int(24 * S), y_cat, txt,
                       (40, 44, 54) if not self.buscando else ROJO)

        self._ayuda(margen)
        if getattr(self, "medidor", False):
            txt = (f"MENU {self.reloj.get_fps():.0f} FPS   JUEGO {self.vivo.emu_fps} FPS   "
                   f"CORTES DE SONIDO {self.vivo.cortes}")
            r = self.f_px_chico.render(txt, True, AMARILLO)
            P.blit(r, (W - margen - r.get_width(), int(64 * S)))
        self._aviso()
        if self.popup:
            self._popup()

    def _ficha(self, j, margen):
        P, S = self.pantalla, self.S
        y = int(160 * S)
        ancho_texto = int(980 * S)
        y = self._texto_envuelto(P, j["titulo"], self.f_titulo, BLANCO, (margen, y), ancho_texto, 2, interlinea=1.0)
        y += int(14 * S)
        x = margen
        if j.get("anio"):
            x = self._chip(x, y, str(j["anio"]), ROJO)
        x = self._chip(x, y, j["genero"], COLOR_GENERO.get(j["genero"], GRIS_OSCURO))
        if j.get("jugadores"):
            x = self._chip(x, y, "1 jugador" if j["jugadores"] == 1 else f"{j['jugadores']} jugadores", (52, 58, 72))
        if j.get("botones") == 6:
            x = self._chip(x, y, "6 botones", (52, 58, 72))
        if j["rom"] in self.datos["favoritos"]:
            x0 = x
            x = self._chip(x, y, "     favorito", AMARILLO, color=NEGRO)
            alto_chip = self.f_chip.get_height() + int(8 * S)
            self._estrella(x0 + int(24 * S), y + alto_chip / 2, 9 * S, NEGRO)
        y += int(56 * S)

        partes = []
        if j.get("desarrolladora"):
            partes.append(("Desarrolla", j["desarrolladora"]))
        if j.get("editora") and j.get("editora") != j.get("desarrolladora"):
            partes.append(("Edita", j["editora"]))
        if j.get("saga"):
            partes.append(("Saga", j["saga"]))
        if j.get("mes") and j.get("anio"):
            partes.append(("Salió", f"{j['mes']} de {j['anio']}"))
        x = margen
        for etiqueta, valor in partes:
            a = self.f_dato_b.render(etiqueta.upper(), True, GRIS)
            b = self.f_dato.render(valor, True, BLANCO)
            if x + a.get_width() + b.get_width() > margen + ancho_texto + int(200 * S):
                x = margen
                y += int(34 * S)
            P.blit(a, (x, y))
            P.blit(b, (x + a.get_width() + int(8 * S), y))
            x += a.get_width() + b.get_width() + int(36 * S)
        y += int(46 * S)

        if j.get("sinopsis"):
            y = self._texto_envuelto(P, j["sinopsis"], self.f_texto, (222, 226, 234), (margen, y), ancho_texto, 5)
            y += int(10 * S)

        # partidas guardadas y estadísticas
        linea = []
        cache = getattr(self, "_slots_cache", None)
        if not cache or cache[0] != j["rom"] or time.time() - cache[1] > 3:
            self._slots_cache = cache = (j["rom"], time.time(), estados_de(j))
        slots = cache[2]
        if slots:
            boton = "cruz" if self.mando.conectado else "J"
            linea.append(f"Partida guardada {hace(slots[0][1])}  ({boton} para continuar)")
        st = self.datos["stats"].get(j["rom"])
        if st:
            linea.append(f"Jugado {st['veces']} " + ("vez" if st["veces"] == 1 else "veces") +
                         f"  ·  {duracion(st['segundos'])}")
        if linea:
            P.blit(self.f_px_chico.render("   ·   ".join(linea).upper(), True, AMARILLO), (margen, y + int(6 * S)))

        # pantalla de título a la derecha
        tit = self.img("titulos", j.get("caja"), (int(520 * S), int(390 * S))) if j.get("caja") else None
        if tit:
            tx = self.W - margen - tit.get_width()
            ty = int(160 * S)
            borde = int(8 * S)
            pygame.draw.rect(P, (20, 22, 28), (tx - borde, ty - borde, tit.get_width() + borde * 2,
                                               tit.get_height() + borde * 2 + int(26 * S)), border_radius=int(10 * S))
            P.blit(tit, (tx, ty))
            texto = "PANTALLA DE TITULO"
            if self.vivo.buscando:
                texto = "BUSCANDO LA DEMO" + "." * (int(time.time() * 3) % 4)
            et = self.f_px_chico.render(texto, True, AMARILLO if self.vivo.buscando else GRIS)
            P.blit(et, (tx, ty + tit.get_height() + int(8 * S)))

    def _carrusel(self):
        P, S, W, H = self.pantalla, self.S, self.W, self.H
        if not self.lista:
            return
        self.pos += (self.sel - self.pos) * 0.22
        if abs(self.sel - self.pos) < 0.002:
            self.pos = float(self.sel)
        base_y = H - int(118 * S)               # línea donde apoyan las cajas
        alto_n, alto_g = 250 * S, 340 * S
        paso = 205 * S
        cx = int(W * 0.5)
        dibujar = []
        i0 = max(0, int(self.pos) - 8)
        i1 = min(len(self.lista), int(self.pos) + 9)
        for i in range(i0, i1):
            d = i - self.pos
            foco = max(0.0, 1 - abs(d))
            alto = alto_n + (alto_g - alto_n) * foco
            x = cx + d * paso + math.copysign(min(abs(d), 1) * 60 * S, d)
            dibujar.append((foco, i, x, alto))
        dibujar.sort()
        for foco, i, x, alto in dibujar:
            s = self.caja(self.lista[i], alto)
            r = s.get_rect(midbottom=(int(x), int(base_y)))
            if r.right < -50 or r.left > W + 50:
                continue
            clave_sombra = ("sombra", r.width, r.height)
            sombra = self.cache_img.get(clave_sombra)
            if sombra is None:
                sombra = pygame.Surface((r.width, r.height))
                sombra.set_alpha(120)
                self.cache_img[clave_sombra] = sombra
            P.blit(sombra, r.move(int(8 * S), int(10 * S)))
            s.set_alpha(int(150 + 105 * foco) if foco < 0.99 else None)
            P.blit(s, r)
            if i == self.sel:
                grosor = max(3, int(5 * S))
                pygame.draw.rect(P, BLANCO, r.inflate(grosor * 3, grosor * 3), grosor, border_radius=int(6 * S))
                if self.lista[i]["rom"] in self.datos["favoritos"]:
                    self._estrella(r.right - int(20 * S), r.top + int(20 * S), 15 * S, AMARILLO, borde=NEGRO)
        # contador
        cont = self.f_px_chico.render(f"{self.sel + 1} / {len(self.lista)}", True, GRIS)
        P.blit(cont, (cx - cont.get_width() // 2, base_y + int(18 * S)))

    def _glifo(self, x, y, tipo, dibujar=True):
        """Botón del mando dibujado: cruz, circulo, triangulo, cuadrado, cruceta o una etiqueta (L1, PS...).
        Devuelve el ancho que ocupa."""
        P, S = self.pantalla, self.S
        t = int(30 * S)
        if tipo in COLOR_PS:
            if dibujar:
                c = (x + t // 2, y + t // 2)
                pygame.draw.circle(P, (36, 40, 50), c, t // 2)
                col, r, g = COLOR_PS[tipo], t * 0.26, max(2, int(2.6 * S))
                if tipo == "cruz":
                    pygame.draw.line(P, col, (c[0] - r, c[1] - r), (c[0] + r, c[1] + r), g)
                    pygame.draw.line(P, col, (c[0] - r, c[1] + r), (c[0] + r, c[1] - r), g)
                elif tipo == "circulo":
                    pygame.draw.circle(P, col, c, int(r * 1.05), g)
                elif tipo == "triangulo":
                    pygame.draw.polygon(P, col, [(c[0], c[1] - r * 1.1), (c[0] + r * 1.1, c[1] + r * 0.8),
                                                 (c[0] - r * 1.1, c[1] + r * 0.8)], g)
                else:
                    pygame.draw.rect(P, col, (c[0] - r, c[1] - r, 2 * r, 2 * r), g)
            return t
        if tipo == "cruceta":
            if dibujar:
                c, b, l = (x + t // 2, y + t // 2), max(4, int(8 * S)), t // 2
                pygame.draw.rect(P, BLANCO, (c[0] - b // 2, c[1] - l, b, 2 * l), border_radius=2)
                pygame.draw.rect(P, BLANCO, (c[0] - l, c[1] - b // 2, 2 * l, b), border_radius=2)
            return t
        r = self.f_px_chico.render(tipo, True, NEGRO)
        caja = pygame.Rect(x, y, max(r.get_width() + int(14 * S), t), t)
        if dibujar:
            pygame.draw.rect(P, BLANCO, caja, border_radius=int(5 * S))
            P.blit(r, r.get_rect(center=caja.center))
        return caja.width

    def _pistas(self, items, x, y, fuente=None, color=GRIS, centro=None):
        """Fila de ayuda: [(["cruz"], "jugar"), (["L1", "R1"], "categoría")...]. Con centro=x la centra."""
        fuente = fuente or self.f_dato
        S = self.S

        def recorrer(dibujar, x):
            for botones, que in items:
                for b in botones:
                    x += self._glifo(x, y, b, dibujar) + int(5 * S)
                r = fuente.render(que, True, color)
                if dibujar:
                    self.pantalla.blit(r, (x + int(4 * S), y + int(2 * S)))
                x += r.get_width() + int(30 * S)
            return x
        if centro is not None:
            ancho = recorrer(False, 0) - int(30 * S)
            x = centro - ancho // 2
        return recorrer(True, x)

    def _ayuda(self, margen):
        P, S = self.pantalla, self.S
        y = self.H - int(52 * S)
        x = margen
        if self.mando.conectado:
            self._pistas([(["cruceta"], "mover"), (["L1", "R1"], "categoría"), (["cruz"], "jugar"),
                          (["circulo"], "volver"), (["triangulo"], "favorito"), (["cuadrado"], "música"),
                          (["CREATE"], "salir")], x, y)
            r = self.f_px_chico.render("EN EL JUEGO:  PS = PAUSA   PANEL TACTIL = GUARDADO RAPIDO", True, GRIS_OSCURO)
            P.blit(r, (self.W - margen - r.get_width(), y + int(9 * S)))
            return
        for teclas, que in (("A D", "mover"), ("W S", "saltar"), ("I U", "categoría"), ("J", "jugar"),
                            ("H", "favorito"), ("Y", "buscar"), ("M", "música"), ("K", "volver"), ("ESC", "salir")):
            for t in teclas.split():
                r = self.f_px_chico.render(t, True, NEGRO)
                caja = pygame.Rect(x, y, max(r.get_width() + int(14 * S), int(30 * S)), int(30 * S))
                pygame.draw.rect(P, BLANCO, caja, border_radius=int(5 * S))
                P.blit(r, r.get_rect(center=caja.center))
                x = caja.right + int(5 * S)
            r = self.f_dato.render(que, True, GRIS)
            P.blit(r, (x + int(4 * S), y + int(2 * S)))
            x += r.get_width() + int(30 * S)
        r = self.f_px_chico.render("EN EL JUEGO:  ESC = PAUSA   F5 = GUARDADO RAPIDO", True, GRIS_OSCURO)
        P.blit(r, (self.W - margen - r.get_width(), y + int(9 * S)))

    def _aviso(self):
        txt, t = self.aviso
        if txt and time.time() - t < 1.8:
            r = self.f_px.render(txt.upper(), True, NEGRO)
            caja = r.get_rect(center=(self.W // 2, int(180 * self.S))).inflate(int(40 * self.S), int(24 * self.S))
            pygame.draw.rect(self.pantalla, AMARILLO, caja, border_radius=int(8 * self.S))
            self.pantalla.blit(r, r.get_rect(center=caja.center))

    def _popup(self):
        P, S, W, H = self.pantalla, self.S, self.W, self.H
        velo = pygame.Surface((W, H), pygame.SRCALPHA)
        velo.fill((0, 0, 0, 170))
        P.blit(velo, (0, 0))
        ancho = int(760 * S)
        if self.popup[0] == "sistema":
            self._dibujar_sistema()
            return
        if self.popup[0] == "salir":
            caja = pygame.Rect(0, 0, ancho, int(230 * S))
            caja.center = (W // 2, H // 2)
            pygame.draw.rect(P, (24, 27, 34), caja, border_radius=int(14 * S))
            pygame.draw.rect(P, ROJO, (caja.x, caja.y, caja.width, int(8 * S)),
                             border_top_left_radius=int(14 * S), border_top_right_radius=int(14 * S))
            t = self.f_px_grande.render("¿SALIR DEL MENU?", True, BLANCO)
            P.blit(t, t.get_rect(center=(caja.centerx, caja.y + int(85 * S))))
            if self.mando.conectado:
                self._pistas([(["cruz"], "sí, salir"), (["circulo"], "no, seguir")], 0, caja.y + int(145 * S),
                             centro=caja.centerx)
            else:
                t = self.f_dato.render("J  sí, salir        K  no, seguir", True, GRIS)
                P.blit(t, t.get_rect(center=(caja.centerx, caja.y + int(160 * S))))
            return
        _, ops, sel, juego = self.popup
        alto_op = int(74 * S)
        caja = pygame.Rect(0, 0, ancho, int(140 * S) + alto_op * len(ops))
        caja.center = (W // 2, H // 2)
        pygame.draw.rect(P, (24, 27, 34), caja, border_radius=int(14 * S))
        pygame.draw.rect(P, AZUL, (caja.x, caja.y, caja.width, int(8 * S)),
                         border_top_left_radius=int(14 * S), border_top_right_radius=int(14 * S))
        self._texto_envuelto(P, juego["titulo"], self.f_chip, GRIS, (caja.x + int(36 * S), caja.y + int(34 * S)),
                             ancho - int(72 * S), 1)
        y = caja.y + int(84 * S)
        for i, (txt, _, sub) in enumerate(ops):
            fila = pygame.Rect(caja.x + int(24 * S), y, ancho - int(48 * S), alto_op - int(10 * S))
            if i == sel:
                pygame.draw.rect(P, AZUL, fila, border_radius=int(8 * S))
            P.blit(self.f_texto.render(txt, True, BLANCO), (fila.x + int(20 * S), fila.y + int(4 * S)))
            P.blit(self.f_dato.render(sub, True, BLANCO if i == sel else GRIS), (fila.x + int(20 * S), fila.y + int(34 * S)))
            y += alto_op
        if self.mando.conectado:
            self._pistas([(["cruceta"], "elegir"), (["cruz"], "aceptar"), (["circulo"], "cancelar")], 0,
                         caja.bottom - int(46 * S), centro=caja.centerx)
        else:
            t = self.f_px_chico.render("W S ELEGIR    J ACEPTAR    K CANCELAR", True, GRIS)
            P.blit(t, t.get_rect(center=(caja.centerx, caja.bottom - int(28 * S))))

    # ---------- bucle
    def correr(self):
        seguir = True
        cuadro = 0
        while seguir:
            # al abrirse (por ejemplo con el botón PS) tomar el frente; se insiste unas veces porque
            # la primera puede llegar antes de que la ventana termine de aparecer
            if cuadro in (2, 20, 60):
                try:
                    traer_al_frente(pygame.display.get_wm_info()["window"])
                except Exception:
                    pass
            cuadro += 1
            if self.partida:
                seguir = self.paso_partida()
                continue
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    seguir = False
                elif e.type == pygame.CONTROLLERDEVICEADDED and not self.mando.conectado:
                    self.mando.abrir()
                    self.avisar("Mando conectado")
                elif e.type == pygame.KEYDOWN:
                    seguir = self.tecla(e)
                elif e.type == pygame.CONTROLLERBUTTONDOWN:
                    k = self.mando.tecla_de(e.button)
                    if k == pygame.K_k and self.buscando:
                        k = pygame.K_ESCAPE
                    if k is not None:
                        seguir = self.tecla(TeclaFalsa(k))
                elif e.type == pygame.TEXTINPUT and self.buscando:
                    self.busqueda += e.text
                    self.filtrar()
                elif e.type == pygame.MOUSEWHEEL and not self.popup:
                    self.mover(-e.y)
            if self.partida:              # la partida recién empezó en este cuadro: la maneja paso_partida
                continue
            d = self.mando.direccion()
            if d is not None and seguir:
                if self.buscando:
                    if d in (pygame.K_a, pygame.K_d):
                        self.mover(-1 if d == pygame.K_a else 1)
                else:
                    seguir = self.tecla(TeclaFalsa(d))
            self.dibujar()
            pygame.display.flip()
            self.reloj.tick(60)
        self.datos["categoria"] = self.cat
        if self.lista:
            self.datos["rom"] = self.lista[self.sel]["rom"]
        guardar_datos(self.datos)
        self.vivo.cerrar()
        pygame.quit()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()          # el emulador corre en otro proceso (también en el .exe)

    def _anotar_error(tipo, valor, tb):
        import traceback
        try:
            with open(USUARIO / "error.log", "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ")
                        + "".join(traceback.format_exception(tipo, valor, tb)) + "\n")
        except OSError:
            pass
    sys.excepthook = _anotar_error            # en el .exe no hay consola: los errores quedan en usuario\error.log
    if "--boton-ps" in sys.argv:
        sistema.escuchar_boton_ps()
        sys.exit(0)
    m = Menu()
    if len(sys.argv) > 2 and sys.argv[1] == "--captura":
        # prueba sin ventana: SDL_VIDEODRIVER=dummy, mueve y guarda una imagen
        for paso in sys.argv[3:]:
            if paso.startswith("cat="):
                m.cat = paso[4:]
                m.filtrar()
            elif paso.startswith("ir="):
                m.filtrar(conservar=next((j["rom"] for j in m.juegos if paso[3:].lower() in j["titulo"].lower()), None))
            elif paso.startswith("vivo="):
                m.dibujar()
                m.t_sel = 0
                for _ in range(int(paso[5:])):
                    m.dibujar()
            elif paso == "popup":
                m.popup = ["jugar", [("Continuar", 0, "slot 0  ·  hace 2 días"), ("Empezar de cero", None, "sin cargar nada")], 0, m.lista[m.sel]]
        for _ in range(40):
            m.dibujar()
        pygame.image.save(m.pantalla, sys.argv[2])
        pygame.quit()
    else:
        m.correr()
