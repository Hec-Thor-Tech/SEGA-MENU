"""Conexión mínima con un núcleo libretro (Genesis Plus GX) para correr juegos de fondo en el menú.

Solo video y audio, sin controles: el juego queda solo y muestra su presentación y su demo.
No escribe partidas ni nada en disco.
"""
import ctypes
import tempfile
import zlib
from ctypes import (CFUNCTYPE, POINTER, Structure, c_bool, c_char_p, c_double, c_float, c_int16,
                    c_size_t, c_uint, c_void_p)
from pathlib import Path

import numpy as np


class SystemInfo(Structure):
    _fields_ = [("library_name", c_char_p), ("library_version", c_char_p), ("valid_extensions", c_char_p),
                ("need_fullpath", c_bool), ("block_extract", c_bool)]


class GameGeometry(Structure):
    _fields_ = [("base_width", c_uint), ("base_height", c_uint), ("max_width", c_uint),
                ("max_height", c_uint), ("aspect_ratio", c_float)]


class SystemTiming(Structure):
    _fields_ = [("fps", c_double), ("sample_rate", c_double)]


class AVInfo(Structure):
    _fields_ = [("geometry", GameGeometry), ("timing", SystemTiming)]


class GameInfo(Structure):
    _fields_ = [("path", c_char_p), ("data", c_void_p), ("size", c_size_t), ("meta", c_char_p)]


class Variable(Structure):
    _fields_ = [("key", c_char_p), ("value", c_char_p)]


ENV_T = CFUNCTYPE(c_bool, c_uint, c_void_p)
VIDEO_T = CFUNCTYPE(None, c_void_p, c_uint, c_uint, c_size_t)
SAMPLE_T = CFUNCTYPE(None, c_int16, c_int16)
BATCH_T = CFUNCTYPE(c_size_t, POINTER(c_int16), c_size_t)
POLL_T = CFUNCTYPE(None)
STATE_T = CFUNCTYPE(c_int16, c_uint, c_uint, c_uint, c_uint)

ENV_GET_CAN_DUPE = 3
ENV_GET_SYSTEM_DIRECTORY = 9
ENV_SET_PIXEL_FORMAT = 10
ENV_GET_VARIABLE = 15
ENV_GET_VARIABLE_UPDATE = 17
ENV_GET_SAVE_DIRECTORY = 31

# opciones del núcleo: vacías a propósito. Con opciones propias (filtro de audio) el núcleo
# 1.7.4 tira una división por cero; con sus valores de fábrica anda bien.
OPCIONES = {}


def a_bin(data):
    """.smd entrelazado -> ROM plana (el núcleo también lo detecta, pero así es seguro)."""
    if len(data) % 16384 == 512 and data[8:10] == b"\xaa\xbb":
        body = np.frombuffer(data[512:], dtype=np.uint8).reshape(-1, 2, 8192)
        out = np.empty((body.shape[0], 8192, 2), dtype=np.uint8)
        out[:, :, 1] = body[:, 0, :]
        out[:, :, 0] = body[:, 1, :]
        return out.tobytes()
    if len(data) % 16384 == 512:
        return data[512:]
    return data


class Nucleo:
    def __init__(self, ruta_dll):
        self.dll = ctypes.CDLL(str(ruta_dll))
        d = self.dll
        d.retro_load_game.restype = c_bool
        d.retro_load_game.argtypes = [POINTER(GameInfo)]
        d.retro_get_system_av_info.argtypes = [POINTER(AVInfo)]
        d.retro_get_system_info.argtypes = [POINTER(SystemInfo)]

        self.formato = 0                 # 0 = 0RGB1555, 1 = XRGB8888, 2 = RGB565
        self.convertir = True            # False = no armar la imagen (más rápido al adelantar)
        self.cuadro = None               # último cuadro como array (alto, ancho, 3)
        self.cuadro_nuevo = False
        self.audio = []
        self.cargado = False
        self._datos = None
        self._dir = c_char_p(str(Path(tempfile.gettempdir()) / "menu_sega_nucleo").encode("mbcs"))
        Path(self._dir.value.decode("mbcs")).mkdir(exist_ok=True)
        self._valores = {k: c_char_p(v) for k, v in OPCIONES.items()}

        # las funciones de retorno se guardan en el objeto para que Python no las borre
        self._env = ENV_T(self._entorno)
        self._video = VIDEO_T(self._video_cb)
        self._sample = SAMPLE_T(lambda l, r: None)
        self._batch = BATCH_T(self._audio_cb)
        self._poll = POLL_T(lambda: None)
        self.botones = set()             # botones apretados por el programa en el joystick 1 (ids de libretro)
        self.mascaras = [0, 0]           # botones de los jugadores 1 y 2 (bit = id de libretro)
        self._state = STATE_T(self._entrada)

        d.retro_set_environment(self._env)
        d.retro_init()
        d.retro_set_video_refresh(self._video)
        d.retro_set_audio_sample(self._sample)
        d.retro_set_audio_sample_batch(self._batch)
        d.retro_set_input_poll(self._poll)
        d.retro_set_input_state(self._state)
        info = SystemInfo()
        d.retro_get_system_info(ctypes.byref(info))
        self.ruta_completa = bool(info.need_fullpath)
        self.nombre = (info.library_name or b"").decode() + " " + (info.library_version or b"").decode()
        self.frecuencia = 44100.0
        self.fps = 60.0

    # ---------------- retornos del núcleo
    def _entorno(self, cmd, data):
        cmd &= 0xFFFF
        try:
            if cmd == ENV_SET_PIXEL_FORMAT:
                self.formato = ctypes.cast(data, POINTER(ctypes.c_int)).contents.value
                return self.formato in (0, 1, 2)
            if cmd in (ENV_GET_SYSTEM_DIRECTORY, ENV_GET_SAVE_DIRECTORY):
                ctypes.cast(data, POINTER(c_char_p))[0] = self._dir.value
                return True
            if cmd == ENV_GET_CAN_DUPE:
                ctypes.cast(data, POINTER(c_bool))[0] = True
                return True
            if cmd == ENV_GET_VARIABLE:
                var = ctypes.cast(data, POINTER(Variable)).contents
                v = self._valores.get(var.key)
                if v is None:
                    return False
                var.value = v.value
                return True
            if cmd == ENV_GET_VARIABLE_UPDATE:
                ctypes.cast(data, POINTER(c_bool))[0] = False
                return True
        except Exception:
            return False
        return False

    def _video_cb(self, data, ancho, alto, pitch):
        if not data or not self.convertir:
            return                               # cuadro repetido, o adelantando sin mirar
        crudo = ctypes.string_at(data, pitch * alto)
        if self.formato == 1:
            a = np.frombuffer(crudo, dtype=np.uint32).reshape(alto, pitch // 4)[:, :ancho]
            rgb = np.empty((alto, ancho, 3), dtype=np.uint8)
            rgb[..., 0] = (a >> 16) & 255
            rgb[..., 1] = (a >> 8) & 255
            rgb[..., 2] = a & 255
        else:
            a = np.frombuffer(crudo, dtype=np.uint16).reshape(alto, pitch // 2)[:, :ancho].astype(np.uint32)
            rgb = np.empty((alto, ancho, 3), dtype=np.uint8)
            if self.formato == 2:                # RGB565
                rgb[..., 0] = ((a >> 11) & 31) * 255 // 31
                rgb[..., 1] = ((a >> 5) & 63) * 255 // 63
                rgb[..., 2] = (a & 31) * 255 // 31
            else:                                # 0RGB1555
                rgb[..., 0] = ((a >> 10) & 31) * 255 // 31
                rgb[..., 1] = ((a >> 5) & 31) * 255 // 31
                rgb[..., 2] = (a & 31) * 255 // 31
        self.cuadro = rgb
        self.cuadro_nuevo = True

    def _entrada(self, puerto, disp, ind, id_):
        if puerto == 0 and id_ in self.botones:
            return 1
        if puerto < 2 and id_ < 16:
            return (self.mascaras[puerto] >> id_) & 1
        return 0

    def reiniciar(self):
        if self.cargado:
            self.dll.retro_reset()

    def sram(self):
        """Memoria de batería del cartucho (partidas guardadas por el propio juego): bytes o None."""
        d = self.dll
        d.retro_get_memory_data.restype = c_void_p
        d.retro_get_memory_size.restype = c_size_t
        n = d.retro_get_memory_size(0)                    # RETRO_MEMORY_SAVE_RAM
        p = d.retro_get_memory_data(0)
        return ctypes.string_at(p, n) if n and p else None

    def poner_sram(self, datos):
        d = self.dll
        d.retro_get_memory_data.restype = c_void_p
        d.retro_get_memory_size.restype = c_size_t
        n = d.retro_get_memory_size(0)
        p = d.retro_get_memory_data(0)
        if n and p and datos:
            ctypes.memmove(p, datos, min(n, len(datos)))

    def _audio_cb(self, datos, cuadros):
        self.audio.append(np.ctypeslib.as_array(datos, shape=(cuadros * 2,)).copy())
        return cuadros

    # ---------------- uso
    def cargar(self, ruta_rom):
        self.descargar()
        if self.ruta_completa:
            # este núcleo lee el archivo él mismo (y entiende .smd entrelazado)
            # los núcleos libretro esperan rutas en UTF-8 (con "mbcs" fallaban los nombres con tilde)
            self._ruta = c_char_p(str(Path(ruta_rom)).encode("utf-8"))
            info = GameInfo(path=self._ruta.value, data=None, size=0, meta=None)
        else:
            datos = a_bin(Path(ruta_rom).read_bytes())
            self._datos = ctypes.create_string_buffer(datos, len(datos))
            info = GameInfo(path=(Path(ruta_rom).with_suffix(".bin").name).encode("mbcs", "replace"),
                            data=ctypes.cast(self._datos, c_void_p), size=len(datos), meta=None)
        if not self.dll.retro_load_game(ctypes.byref(info)):
            self._datos = None
            return False
        av = AVInfo()
        self.dll.retro_get_system_av_info(ctypes.byref(av))
        self.frecuencia = av.timing.sample_rate or 44100.0
        self.fps = av.timing.fps or 60.0
        self.cuadro = None
        self.audio = []
        self.cargado = True
        return True

    def correr(self, cuadros=1):
        if self.cargado:
            for _ in range(cuadros):
                self.dll.retro_run()

    def tomar_audio(self):
        """Devuelve el audio acumulado como array (n, 2) int16 y vacía el búfer."""
        if not self.audio:
            return None
        a = np.concatenate(self.audio).reshape(-1, 2)
        self.audio = []
        return a

    def descargar(self):
        if self.cargado:
            self.dll.retro_unload_game()
            self.cargado = False
        self._datos = None
        self.cuadro = None
        self.audio = []

    def serializar(self):
        """Foto completa del estado del juego (para volver justo a la demo la próxima vez)."""
        d = self.dll
        d.retro_serialize_size.restype = c_size_t
        n = d.retro_serialize_size()
        if not n:
            return None
        buf = ctypes.create_string_buffer(n)
        d.retro_serialize.restype = c_bool
        d.retro_serialize.argtypes = [c_void_p, c_size_t]
        return buf.raw if d.retro_serialize(buf, n) else None

    def deserializar(self, datos):
        d = self.dll
        d.retro_unserialize.restype = c_bool
        d.retro_unserialize.argtypes = [c_void_p, c_size_t]
        buf = ctypes.create_string_buffer(datos, len(datos))
        return bool(d.retro_unserialize(buf, len(datos)))

    def cerrar(self):
        self.descargar()
        self.dll.retro_deinit()


# ===================================================================== proceso aparte
# El emulador corre en otro proceso, con su propio sonido marcando el ritmo. Así el dibujo
# del menú no lo puede frenar. El último cuadro se deja en una memoria compartida.

ANCHO_MAX, ALTO_MAX = 512, 512
CAB = 8                                   # enteros de 64 bits al principio de la memoria
TAM_MEMORIA = CAB * 8 + ANCHO_MAX * ALTO_MAX * 3
# cabecera: 0 contador de cuadros, 1 ancho, 2 alto, 3 cuadros/s del juego, 4 cortes de sonido,
#           5 estado (0 nada, 1 fondo, 2 buscando la demo, 3 jugando), 6 y 7 botones de los jugadores 1 y 2


def huella(rgb):
    """Imagen chiquita (32×24) para comparar pantallas sin importar su tamaño."""
    from PIL import Image
    return np.asarray(Image.fromarray(rgb).resize((32, 24), Image.BILINEAR), dtype=np.float32)


class BuscadorDemo:
    """Adelanta la presentación hasta pasar la pantalla de título y deja guardado ese punto.

    Reconoce el título comparando con la imagen de título que ya tiene el menú. Cuando el título
    se va (y la pantalla no está negra), empieza la demo. Si nunca lo reconoce, salta los logos
    del principio (12 s) y arranca ahí.
    """
    CADA = 10                 # cada cuántos cuadros se mira la pantalla
    TOPE = 150 * 60           # 2,5 minutos de juego como mucho
    PLAN_B = 12 * 60

    APRETAR = {0, 1, 3, 8}    # B, C, Start y A de libretro: cada cracktro espera uno distinto
    TRABADO = 15              # segundos con la pantalla quieta antes de apretar Start

    def __init__(self, ref, archivo):
        self.ref = None if ref is None else np.frombuffer(ref, dtype=np.float32).reshape(24, 32, 3)
        self.archivo = archivo
        self.cuadro = 0
        self.vio_titulo = False
        self.fuera = 0
        self.plan_b = None
        self.huellas = []         # una por segundo, para darse cuenta si la pantalla quedó trabada
        self.quieto = 0
        self.apretadas = 0
        self.soltar_en = None

    def _trabado(self, nucleo, h):
        """Pantallas que esperan Start para siempre (las "cracktros" de las copias piratas de los 90):
        si la imagen casi no cambia durante un rato y todavía no apareció el título, aprieta Start."""
        self.huellas.append(h)
        if len(self.huellas) < 4:
            return
        cambio = float(np.abs(h - self.huellas[-4]).mean())
        self.quieto = self.quieto + 1 if cambio < 5 else 0
        if self.quieto >= self.TRABADO and self.apretadas < 3 and not self.vio_titulo:
            nucleo.botones.update(self.APRETAR)
            self.soltar_en = self.cuadro + 8
            self.apretadas += 1
            self.quieto = 0
            self.plan_b = None            # lo de antes de apretar no sirve como plan B

    def _guardar(self, datos):
        try:
            Path(self.archivo).parent.mkdir(parents=True, exist_ok=True)
            Path(self.archivo).write_bytes(zlib.compress(datos, 6))
        except Exception:
            pass

    def tanda(self, nucleo):
        """Corre unos cuadros. Devuelve True cuando ya está en la demo (o se rindió)."""
        if not nucleo.cargado or self.cuadro > self.TOPE + 600:     # salida de seguridad
            nucleo.botones.clear()
            return True
        for _ in range(60):
            mirar = self.cuadro % self.CADA == 0
            nucleo.convertir = mirar
            nucleo.correr(1)
            nucleo.audio = []
            self.cuadro += 1
            if self.soltar_en is not None and self.cuadro >= self.soltar_en:
                nucleo.botones.clear()
                self.soltar_en = None
                self.plan_b_en = self.cuadro + self.PLAN_B     # el plan B se cuenta desde que arrancó el juego
            if self.cuadro >= getattr(self, "plan_b_en", self.PLAN_B) and self.plan_b is None \
                    and self.soltar_en is None and self.quieto < 8:
                self.plan_b = nucleo.serializar()
                if self.ref is None:                 # sin imagen de título: alcanza con saltear logos
                    if self.plan_b:
                        self._guardar(self.plan_b)
                    return True
            if not mirar or nucleo.cuadro is None:
                continue
            c = nucleo.cuadro
            h = huella(c)
            if (self.cuadro - 1) % 60 == 0:
                self._trabado(nucleo, h)
            if self.ref is not None:
                d = float(np.abs(h - self.ref).mean())
                if d < 15:
                    self.vio_titulo = True
                    self.fuera = 0
                elif self.vio_titulo and d > 25 and c[::8, ::8].mean() > 15:
                    self.fuera += 1
                    if self.fuera >= 6:              # 1 segundo lejos del título: es la demo
                        estado = nucleo.serializar()
                        if estado:
                            self._guardar(estado)
                        return True
            if self.cuadro >= self.TOPE:
                nucleo.botones.clear()
                if self.vio_titulo:                  # juego sin demo: se queda en el título, con su música
                    estado = nucleo.serializar()
                    if estado:
                        self._guardar(estado)
                elif self.plan_b and nucleo.deserializar(self.plan_b):
                    self._guardar(self.plan_b)
                return True
        return False


def proceso_fondo(conn, nombre_memoria, ruta_dll, volumen, archivo_error=None):
    """Envoltorio: si el emulador falla, deja el error anotado y el proceso se cierra limpio
    (en el .exe un error sin atrapar abre un cartel invisible y el menú se quedaría esperando)."""
    import os
    try:
        _proceso_fondo(conn, nombre_memoria, ruta_dll, volumen)
    except BaseException:
        import traceback
        if archivo_error:
            try:
                with open(archivo_error, "a", encoding="utf-8") as f:
                    f.write(traceback.format_exc() + "\n")
            except OSError:
                pass
        try:
            conn.close()
        finally:
            os._exit(1)


def _proceso_fondo(conn, nombre_memoria, ruta_dll, volumen):
    import os
    import threading
    import time
    from multiprocessing import shared_memory
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_JOYSTICK_HIDAPI"] = "0"      # no tocar el mando: lo usa Fusion
    import pygame
    pygame.init()
    from pygame._sdl2.audio import AudioDevice, AUDIO_S16

    mem = shared_memory.SharedMemory(name=nombre_memoria)
    cab = np.ndarray((CAB,), dtype=np.int64, buffer=mem.buf)
    pix = np.ndarray((ANCHO_MAX * ALTO_MAX * 3,), dtype=np.uint8, buffer=mem.buf, offset=CAB * 8)
    nucleo = Nucleo(ruta_dll)
    estado = {"buf": np.zeros((0, 2), dtype=np.int16), "silencio": False, "fade": 0.0, "cortes": 0,
              "activo": False, "juego": False, "pausa": False, "vol": volumen}
    lock = threading.Lock()
    ADELANTO_FONDO = 4096                 # ~90 ms de sonido listos (juego de fondo)
    ADELANTO_JUEGO = 2048                 # ~45 ms jugando: menos demora entre el botón y lo que pasa

    def escribir(ruta, datos):
        ruta = Path(ruta)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(ruta.suffix + ".tmp")
        tmp.write_bytes(datos)
        tmp.replace(ruta)

    def llenar(dev, memoria):
        n = len(memoria) // 4
        with lock:
            tomar = estado["buf"][:n]
            estado["buf"] = estado["buf"][n:]
        if estado["activo"] and busca is None and not estado["pausa"] and len(tomar) < n:   # silencio a propósito no cuenta
            estado["cortes"] += 1
        salida = np.zeros((n, 2), dtype=np.int16)
        if len(tomar) and not (estado["silencio"] and not estado["juego"]):
            salida[:len(tomar)] = (tomar.astype(np.float32) * (estado["vol"] * estado["fade"])).astype(np.int16)
            estado["fade"] = min(1.0, estado["fade"] + 0.06)
        memoria[:] = salida.tobytes()

    try:
        dev = AudioDevice(devicename=None, iscapture=False, frequency=int(nucleo.frecuencia), audioformat=AUDIO_S16,
                          numchannels=2, chunksize=1024, allowed_changes=0, callback=llenar)
        dev.pause(0)
    except Exception:
        dev = None                     # sin placa de sonido: el reloj marca el ritmo y no suena nada
    proximo = time.perf_counter()

    busca = None
    contados, t0 = 0, time.perf_counter()
    while True:
        # órdenes del menú
        while conn.poll():
            orden = conn.recv()
            if orden[0] == "jugar":                      # ("jugar", rom, archivo_sram, volumen)
                _, rom, archivo_sram, vol = orden
                estado["activo"] = False
                nucleo.descargar()
                with lock:
                    estado["buf"] = np.zeros((0, 2), dtype=np.int16)
                busca = None
                nucleo.botones.clear()
                nucleo.mascaras = [0, 0]
                nucleo.convertir = True
                estado.update(fade=0.0, cortes=0, juego=True, pausa=False, vol=vol)
                try:
                    estado["activo"] = nucleo.cargar(rom)
                except Exception:
                    estado["activo"] = False
                if estado["activo"] and archivo_sram and Path(archivo_sram).exists():
                    nucleo.poner_sram(Path(archivo_sram).read_bytes())
                conn.send(("jugando", estado["activo"]))
            elif orden[0] == "pausa":
                estado["pausa"] = bool(orden[1])
                with lock:
                    estado["buf"] = np.zeros((0, 2), dtype=np.int16)
                estado["fade"] = 0.0
            elif orden[0] == "guardar":                  # ("guardar", archivo_estado)
                datos = nucleo.serializar() if estado["activo"] else None
                if datos:
                    escribir(orden[1], zlib.compress(datos, 6))
                conn.send(("guardado", bool(datos), orden[1]))
            elif orden[0] == "cargar_estado":
                ok = False
                try:
                    ok = nucleo.deserializar(zlib.decompress(Path(orden[1]).read_bytes()))
                except Exception:
                    pass
                with lock:
                    estado["buf"] = np.zeros((0, 2), dtype=np.int16)
                conn.send(("cargado", ok, orden[1]))
            elif orden[0] == "guardar_sram":
                datos = nucleo.sram() if estado["activo"] else None
                if datos and any(datos):
                    escribir(orden[1], datos)
            elif orden[0] == "reiniciar":
                nucleo.reiniciar()
            elif orden[0] == "volumen":
                estado["vol"] = float(orden[1])
            elif orden[0] == "cargar":
                estado["juego"] = False
                estado["vol"] = volumen
                nucleo.mascaras = [0, 0]
                estado["activo"] = False
                nucleo.descargar()
                with lock:
                    estado["buf"] = np.zeros((0, 2), dtype=np.int16)
                estado["fade"] = 0.0
                estado["cortes"] = 0
                busca = None
                nucleo.botones.clear()
                try:
                    estado["activo"] = nucleo.cargar(orden[1])
                except Exception:
                    estado["activo"] = False
                if estado["activo"]:
                    _, _, ref, archivo = orden
                    guardado = None
                    try:
                        guardado = zlib.decompress(Path(archivo).read_bytes())
                    except Exception:
                        pass
                    if not (guardado and nucleo.deserializar(guardado)):
                        busca = BuscadorDemo(ref, archivo)
            elif orden[0] == "parar":
                estado["activo"] = False
                estado["juego"] = False
                estado["vol"] = volumen
                nucleo.mascaras = [0, 0]
                nucleo.descargar()
                with lock:
                    estado["buf"] = np.zeros((0, 2), dtype=np.int16)
            elif orden[0] == "silencio":
                estado["silencio"] = bool(orden[1])
            elif orden[0] == "salir":
                if dev is not None:
                    dev.pause(1)
                    dev.close()
                nucleo.cerrar()
                del cab, pix
                mem.close()
                return
        cab[5] = (3 if estado["juego"] else 2 if busca else 1) if estado["activo"] else 0
        cab[4] = estado["cortes"]
        if not estado["activo"] or estado["pausa"]:
            time.sleep(0.01)
            continue
        if estado["juego"]:
            nucleo.mascaras = [int(cab[6]), int(cab[7])]     # botones que manda el menú
        if busca:
            # adelantar a toda velocidad, en silencio y sin mostrar, de a tandas cortas
            # (entre tanda y tanda se atienden las órdenes del menú)
            try:
                if busca.tanda(nucleo):
                    busca = None
                    nucleo.botones.clear()
                    nucleo.convertir = True
                    estado["cortes"] = 0
                    with lock:
                        estado["buf"] = np.zeros((0, 2), dtype=np.int16)
            except OSError:
                estado["activo"] = False
                nucleo.descargar()
                busca = None
            continue
        with lock:
            lleno = len(estado["buf"])
        if dev is None:
            ahora = time.perf_counter()
            if ahora < proximo:
                time.sleep(min(0.004, proximo - ahora))
                continue
            proximo = max(proximo + 1 / nucleo.fps, ahora - 0.1)
            with lock:
                estado["buf"] = np.zeros((0, 2), dtype=np.int16)
        elif lleno >= (ADELANTO_JUEGO if estado["juego"] else ADELANTO_FONDO):
            time.sleep(0.002)
            continue
        try:
            nucleo.correr(1)
        except OSError:
            estado["activo"] = False
            nucleo.descargar()
            continue
        audio = nucleo.tomar_audio()
        if audio is not None:
            with lock:
                estado["buf"] = np.concatenate([estado["buf"], audio])
        if nucleo.cuadro is not None and nucleo.cuadro_nuevo:
            nucleo.cuadro_nuevo = False
            c = nucleo.cuadro
            h, w = c.shape[:2]
            if w <= ANCHO_MAX and h <= ALTO_MAX:
                pix[:h * w * 3] = c.reshape(-1)
                cab[1], cab[2] = w, h
                cab[0] += 1
        contados += 1
        if time.perf_counter() - t0 >= 1.0:
            cab[3] = contados
            contados, t0 = 0, time.perf_counter()
