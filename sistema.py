"""SEGA MENU — configuración de cada PC, botón PS opcional y actualizaciones desde GitHub."""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.0.0"
REPO = "Hec-Thor-Tech/SEGA-MENU"
JUEGOS_DEFECTO = r"Z:\HECTOOL\ROMS\SEGA"
TITULO_VENTANA = "Menú SEGA"

CONGELADO = getattr(sys, "frozen", False)          # True cuando corre como SEGA MENU.exe
AQUI = Path(sys.executable).resolve().parent if CONGELADO else Path(__file__).resolve().parent
DATOS_DIR = AQUI / "datos"                          # catálogo, carátulas, núcleo... (viene con el programa)
USUARIO = AQUI / "usuario"                          # favoritos, partidas y configuración de ESTA PC
USUARIO.mkdir(exist_ok=True)
CONFIG = USUARIO / "config.json"
EXE = AQUI / "SEGA MENU.exe"


# ---------------------------------------------------------------- configuración

def leer_config():
    c = {"juegos": JUEGOS_DEFECTO, "boton_ps": False}
    try:
        c.update(json.loads(CONFIG.read_text(encoding="utf-8-sig")))     # -sig: tolera la marca BOM
    except Exception:
        pass
    return c


def guardar_config(c):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG)


def entorno_limpio():
    """Variables de entorno sin las de "sin pantalla" (si el menú las hereda, arranca invisible)."""
    return {k: v for k, v in os.environ.items() if k not in ("SDL_VIDEODRIVER", "SDL_AUDIODRIVER")}


def comando_menu(extra=()):
    if CONGELADO:
        return [sys.executable, *extra]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else sys.executable), str(AQUI / "SEGA MENU.pyw"), *extra]


# ---------------------------------------------------------------- botón PS (opcional, apagado de fábrica)

INICIO_WINDOWS = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
ACCESO_PS = INICIO_WINDOWS / "SEGA MENU - Boton PS.lnk"


def _crear_acceso(lnk, destino, argumentos, carpeta):
    q = lambda s: str(s).replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
          f"$s.TargetPath='{q(destino)}';$s.Arguments='{q(argumentos)}';$s.WorkingDirectory='{q(carpeta)}';"
          f"$s.Description='Abre SEGA MENU con el boton PS del mando';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                   creationflags=0x08000000, timeout=30)          # CREATE_NO_WINDOW


def activar_boton_ps(si):
    c = leer_config()
    c["boton_ps"] = bool(si)
    guardar_config(c)
    if si:
        cmd = comando_menu(["--boton-ps"])
        args = " ".join(f'"{a}"' for a in cmd[1:])
        _crear_acceso(ACCESO_PS, cmd[0], args, AQUI)
        subprocess.Popen(cmd, cwd=str(AQUI), env=entorno_limpio(), creationflags=0x00000008)  # DETACHED
    else:
        try:
            ACCESO_PS.unlink()
        except FileNotFoundError:
            pass
        # el escuchador ve la configuración y se cierra solo


def escuchar_boton_ps():
    """Sin ventana: al apretar PS abre el menú (o lo trae al frente). Se cierra si se apaga la opción."""
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    _mutex = kernel32.CreateMutexW(None, False, "SEGA_MENU_BotonPS")
    if kernel32.GetLastError() == 183:                       # ya hay uno corriendo
        return
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    os.environ["SDL_JOYSTICK_HIDAPI"] = "0"
    os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
    import pygame
    pygame.init()
    from pygame._sdl2 import controller
    controller.init()
    mandos = []

    def conectar():
        for i in range(controller.get_count()):
            try:
                if controller.is_controller(i):
                    mandos.append(controller.Controller(i))
            except Exception:
                pass

    def abrir_o_traer():
        hwnd = user32.FindWindowW(None, TITULO_VENTANA)
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x43)
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x43)
        else:
            subprocess.Popen(comando_menu(), cwd=str(AQUI), env=entorno_limpio())

    conectar()
    ultimo, revisado = 0.0, time.time()
    while True:
        for e in pygame.event.get():
            if e.type == pygame.CONTROLLERDEVICEADDED:
                conectar()
            elif e.type == pygame.CONTROLLERBUTTONDOWN and e.button == pygame.CONTROLLER_BUTTON_GUIDE:
                if time.time() - ultimo > 2.5:
                    ultimo = time.time()
                    abrir_o_traer()
        if time.time() - revisado > 5:
            revisado = time.time()
            if not leer_config().get("boton_ps"):
                return
        time.sleep(0.05)


# ---------------------------------------------------------------- actualizaciones (releases de GitHub)

def _tupla(v):
    partes = []
    for p in str(v).lstrip("vV").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        partes.append(int(num) if num else 0)
    return tuple(partes + [0] * (3 - len(partes)))


def buscar_actualizacion():
    """(version, url_del_zip) si en GitHub hay una versión más nueva; si no, None."""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": "SEGA-MENU", "Accept": "application/vnd.github+json"})
        r = json.loads(urllib.request.urlopen(req, timeout=6).read())
    except Exception:
        return None
    tag = r.get("tag_name", "")
    zips = [a["browser_download_url"] for a in r.get("assets", []) if a.get("name", "").lower().endswith(".zip")]
    if zips and _tupla(tag) > _tupla(VERSION):
        return tag.lstrip("vV"), zips[0]
    return None


def instalar_actualizacion(url, progreso=None):
    """Baja el zip de la versión nueva y deja preparado el reemplazo, que se hace cuando el menú se cierra.
    Devuelve True si hay que cerrar el menú para terminar. Solo funciona en el .exe."""
    if not CONGELADO:
        return False
    carpeta = USUARIO / "actualizacion"
    if carpeta.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(carpeta)], creationflags=0x08000000)
    carpeta.mkdir(parents=True, exist_ok=True)
    zip_local = carpeta / "paquete.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "SEGA-MENU"})
    with urllib.request.urlopen(req, timeout=30) as r, open(zip_local, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        bajado = 0
        while True:
            trozo = r.read(1 << 16)
            if not trozo:
                break
            f.write(trozo)
            bajado += len(trozo)
            if progreso and total:
                progreso(bajado / total)
    nuevo = carpeta / "nuevo"
    with zipfile.ZipFile(zip_local) as z:
        z.extractall(nuevo)
    raiz = next((p.parent for p in nuevo.rglob("SEGA MENU.exe")), None)
    if raiz is None:
        return False
    bat = USUARIO / "terminar_actualizacion.bat"
    bat.write_text(
        "@echo off\r\n"
        f":espera\r\ntasklist /FI \"PID eq {os.getpid()}\" | find \"{os.getpid()}\" >nul && "
        "(timeout /t 1 /nobreak >nul & goto espera)\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        f"robocopy \"{raiz}\" \"{AQUI}\" /E /XD usuario /R:5 /W:1 >nul\r\n"
        f"start \"\" \"{EXE}\"\r\n"
        f"rmdir /s /q \"{carpeta}\"\r\n", encoding="mbcs")
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(USUARIO), creationflags=0x08000000 | 0x00000008)
    return True
