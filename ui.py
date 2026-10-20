"""Interface terminal de Sylvie : couleurs, bannière, spinner, effet machine à écrire.

Les animations se désactivent seules si la sortie n'est pas un terminal,
ou avec SYLVIE_ANIM=0 ; les couleurs avec NO_COLOR=1.
"""
import os, shutil, sys, threading, time

TTY = sys.stdout.isatty()
COLOR = TTY and not os.environ.get("NO_COLOR")
ANIM = TTY and os.environ.get("SYLVIE_ANIM", "1") != "0"


def _c(code):
    return (lambda s: f"\033[{code}m{s}\033[0m") if COLOR else (lambda s: str(s))


bold, dim, italic = _c("1"), _c("2"), _c("3")
red, green, yellow, blue, magenta, cyan = (_c(f"38;5;{n}") for n in (203, 114, 221, 75, 177, 80))
violet = _c("38;5;141")

_GRADIENT = [177, 171, 141, 105, 75, 80]  # magenta -> cyan

BANNER = [
    r"  ____  _   _ _     __     __ ___  _____ ",
    r" / ___|| | | | |    \ \   / /|_ _|| ____|",
    r" \___ \| |_| | |     \ \ / /  | | |  _|  ",
    r"  ___) |\__, | |___   \ V /   | | | |___ ",
    r" |____/ |___/|_____|   \_/   |___||_____|",
]


def width():
    return shutil.get_terminal_size((60, 20)).columns


def out(s="", end="\n"):
    sys.stdout.write(s + end)
    sys.stdout.flush()


def banner(subtitle=""):
    w = width()
    lines = BANNER if w >= 44 else ["  S Y L V I E"]
    for i, line in enumerate(lines):
        code = _GRADIENT[min(i, len(_GRADIENT) - 1)]
        out(f"\033[1;38;5;{code}m{line}\033[0m" if COLOR else line)
        if ANIM:
            time.sleep(0.06)
    if subtitle:
        out(dim("  " + subtitle))
    out()


class Spinner:
    """Animation pendant une attente. Usage : with Spinner("Sylvie réfléchit"): ..."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    PULSE = ["●", "◉", "○", "◉"]

    def __init__(self, label, style="dots", color=None):
        self.label, self.style = label, style
        self.color = color or violet
        self._stop = threading.Event()
        self._t = None

    def _run(self):
        frames = self.PULSE if self.style == "pulse" else self.FRAMES
        t0, i = time.time(), 0
        while not self._stop.is_set():
            f = frames[i % len(frames)]
            sys.stdout.write(f"\r  {self.color(f)} {self.label} {dim(f'{time.time() - t0:4.1f}s')}\033[K")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.25 if self.style == "pulse" else 0.08)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def __enter__(self):
        if ANIM:
            self._t = threading.Thread(target=self._run, daemon=True)
            self._t.start()
        else:
            out(dim(f"  … {self.label}"))
        return self

    def __exit__(self, *exc):
        if self._t:
            self._stop.set()
            self._t.join()
        return False


def typewrite(text, prefix=""):
    """Affiche le texte progressivement (durée totale plafonnée à ~2 s)."""
    if not ANIM:
        out(prefix + text)
        return
    sys.stdout.write(prefix)
    delay = min(0.012, 2.0 / max(len(text), 1))
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        if ch not in " \n":
            time.sleep(delay)
    out()


def sylvie(text):
    out()
    typewrite(text, prefix=f"{bold(magenta('Sylvie'))} {dim('›')} ")


def prompt():
    return input(f"\n{bold(cyan('Toi'))} {dim('›')} ")


def info(msg):
    out(f"  {blue('ℹ')} {msg}")


def ok(msg):
    out(f"  {green('✔')} {msg}")


def warn(msg):
    out(f"  {yellow('⚠')} {msg}")


def error(msg):
    out(f"  {red('✖')} {msg}")


def tool(name, risk, args=""):
    icon = {"auto": green("⚙"), "confirm": yellow("⚙"), "sensitive": red("⚙")}.get(risk, "⚙")
    out(f"  {icon} {dim(name)} {dim(args)}")


def box(title, lines, color=yellow):
    """Encadré pour les demandes de confirmation dans le terminal."""
    w = min(max([len(title)] + [len(l) for l in lines]) + 4, width() - 2)
    out(color("  ╭" + "─" * (w - 2) + "╮"))
    out(color("  │ ") + bold(title.ljust(w - 4)[: w - 4]) + color(" │"))
    out(color("  ├" + "─" * (w - 2) + "┤"))
    for l in lines:
        out(color("  │ ") + l.ljust(w - 4)[: w - 4] + color(" │"))
    out(color("  ╰" + "─" * (w - 2) + "╯"))
