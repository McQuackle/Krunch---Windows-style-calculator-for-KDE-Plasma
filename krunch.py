#!/usr/bin/env python3
"""
Krunch — Everyday Calculator: a friendly, themeable calculator for KDE Plasma.

Built on Python + PyQt6 (both from Fedora's official repos).
Settings:  ~/.config/krunch/settings.json
History:   ~/.local/share/krunch/history.json

The file is laid out top to bottom as:
  1. Math engine   — safe parser (no eval) with exact decimal arithmetic
  2. Themes        — color presets, KDE color mapping, stylesheet, icons
  3. Storage       — settings and history files
  4. Widgets       — display, history panel, settings dialog
  5. Main window
"""
import ast
import copy
import decimal
import json
import math
import os
import re
import sys
import time
import traceback
from decimal import Decimal as D, localcontext
from pathlib import Path

from PyQt6.QtCore import QByteArray, QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence,
    QPainter, QPainterPath, QPalette, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStyle,
    QStyledItemDelegate, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

APP_ID = "krunch"
APP_NAME = "Krunch"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
CONFIG_DIR = CONFIG_HOME / APP_ID
DATA_DIR = DATA_HOME / APP_ID
SETTINGS_FILE = CONFIG_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
HISTORY_LIMIT = 500


# ════════════════════════════════════════════════════════════════════
#  1. Math engine
# ════════════════════════════════════════════════════════════════════
#
#  Text → tokens → Python expression with numbers as placeholders → AST,
#  which is walked by hand (only arithmetic and our own functions are
#  allowed). All math is exact decimal, like the Windows calculator:
#  0.1 + 0.2 = 0.3, 1 ÷ 3 × 3 = 1, and 16-digit numbers keep every digit.

CTX = decimal.Context(prec=34, rounding=decimal.ROUND_HALF_EVEN, Emax=999999, Emin=-999999,
                      traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])
PI = D("3.141592653589793238462643383279503")
E = D("2.718281828459045235360287471352662")


class CalcError(Exception):
    """A problem with the user's input, worded for the user."""


FUNC_NAMES = {"sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "ln", "log",
              "abs", "fact", "round", "floor", "ceil", "exp", "pct"}
CONST_NAMES = {"pi", "e", "tau", "ans"}

TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_]\w*)"
    r"|(?P<op>\*\*|//|[-+*/%^()!]))"
)

# Pretty symbols, plus the junk people paste in (currency, thousands separators…)
CLEAN_MAP = {
    "×": "*", "✕": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "—": "-", "‐": "-",
    "π": " pi ", "ℯ": " e ", "√": " sqrt ", "²": "^2", "³": "^3", "＋": "+",
    "$": "", "€": "", "£": "", "¥": "", ",": "", "_": "", "=": "",
}
TIMES_X = re.compile(r"(?<=[\d.)\s])[xX](?=\s*[\d.(√π])")     # 5x3, 2 x 4 → multiply

# Exact values at the common angles (degrees), so sin(30) is exactly 0.5
EXACT_DEGREES = {
    "sin": {0: 0, 30: "0.5", 90: 1, 150: "0.5", 180: 0, 210: "-0.5", 270: -1, 330: "-0.5"},
    "cos": {0: 1, 60: "0.5", 90: 0, 120: "-0.5", 180: -1, 240: "-0.5", 270: 0, 300: "0.5"},
    "tan": {0: 0, 45: 1, 135: -1, 180: 0, 225: 1, 315: -1},
}


def clean_input(text: str) -> str:
    for a, b in CLEAN_MAP.items():
        text = text.replace(a, b)
    return TIMES_X.sub("*", text).strip()


def tokenize(text: str):
    s = clean_input(text)
    toks, pos = [], 0
    while pos < len(s):
        m = TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            if not s[pos:].strip():
                break
            raise CalcError(f"Don't understand “{s[pos:].strip()[0]}”")
        pos = m.end()
        if m.group("num"):
            if pos < len(s) and s[pos] == ".":
                raise CalcError("A number has two decimal points")
            toks.append(("num", m.group("num")))
        elif m.group("name"):
            name = m.group("name").lower()
            if name == "mod":
                toks.append(("op", "mod"))
            elif name in FUNC_NAMES:
                toks.append(("func", name))
            elif name in CONST_NAMES:
                toks.append(("const", name))
            else:
                raise CalcError(f"Unknown name “{m.group('name')}”")
        else:
            toks.append(("op", m.group("op")))
    return toks


def _operand_start(out, what):
    """Index where the operand just before a postfix operator (% or !) begins."""
    if not out:
        raise CalcError(f"{what} needs a number before it")
    last = out[-1]
    if last[0] in ("num", "const"):
        return len(out) - 1
    if last == ("op", ")"):
        depth = 0
        for j in range(len(out) - 1, -1, -1):
            if out[j] == ("op", ")"):
                depth += 1
            elif out[j] == ("op", "("):
                depth -= 1
                if depth == 0:
                    return j - 1 if j > 0 and out[j - 1][0] == "func" else j
        raise CalcError("Unbalanced parentheses")
    raise CalcError(f"{what} needs a number before it")


def _to_python(text: str):
    """Tokenize, add implicit multiplication, turn % and ! into function calls.
    Numbers become placeholders (_n0, _n1…) so they stay exact decimals."""
    out = []
    ends = lambda t: t[0] in ("num", "const") or t == ("op", ")")
    starts = lambda t: t[0] in ("num", "const", "func") or t == ("op", "(")
    for t in tokenize(text):
        if out and out[-1][0] == "func" and t != ("op", "("):
            if t[0] in ("num", "const"):
                out += [("op", "("), t, ("op", ")")]      # √9 → sqrt(9)
                continue
            raise CalcError(f"Put parentheses after {out[-1][1]}, like {out[-1][1]}(30)")
        if t in (("op", "%"), ("op", "!")):
            i = _operand_start(out, t[1])
            func = "pct" if t[1] == "%" else "fact"
            out[i:] = [("func", func), ("op", "(")] + out[i:] + [("op", ")")]
            continue
        if out and out[-1][0] == "num" and t[0] == "num":
            raise CalcError("Missing an operator between two numbers")
        if out and ends(out[-1]) and starts(t):           # 2π, 3(4), (1)(2)
            out.append(("op", "*"))
        out.append(t)
    if out and out[-1][0] == "func":
        raise CalcError(f"Put parentheses after {out[-1][1]}, like {out[-1][1]}(30)")
    missing = out.count(("op", "(")) - out.count(("op", ")"))
    out += [("op", ")")] * max(missing, 0)                # "sqrt(9" still works
    nums, parts = [], []
    for kind, val in out:
        if kind == "num":
            parts.append(f"_n{len(nums)}")
            nums.append(val)
        else:
            parts.append({"^": "**", "mod": "%"}.get(val, val) if kind == "op" else val)
    return " ".join(parts), nums


def _sin_cos(x: D):
    """sin and cos of x (radians) to full precision, by Taylor series."""
    with localcontext() as ctx:
        ctx.prec += 6
        x = x % (2 * PI)
        results = []
        for total, i in ((x, 1), (D(1), 0)):            # sin series, then cos series
            term = total
            while True:
                term = -term * x * x / ((i + 1) * (i + 2))
                i += 2
                if total + term == total:
                    break
                total += term
            results.append(total)
    tiny = D("1e-30")
    return [D(0) if abs(v) < tiny else +v for v in results]


def _from_float(x: float) -> D:
    if math.isinf(x) or math.isnan(x):
        raise CalcError("Result is too large" if math.isinf(x) else "Result isn't a real number")
    return D(0) if abs(x) < 1e-13 else D(f"{x:.15g}")   # floats are good to ~15 digits


class Calculator:
    def __init__(self):
        self.degrees = True
        self.ans = D(0)

    def evaluate(self, text: str) -> D:
        if not text.strip():
            raise CalcError("Nothing to calculate")
        expr, nums = _to_python(text)
        if not expr:
            raise CalcError("Nothing to calculate")
        try:
            tree = ast.parse(expr, mode="eval")
            with localcontext(CTX):
                value = +self._ev(tree.body, nums)
        except CalcError:
            raise
        except SyntaxError as err:
            raise CalcError("Too many parentheses" if "nest" in str(err) else "Incomplete expression")
        except decimal.DivisionByZero:
            raise CalcError("Can't divide by zero")
        except (decimal.Overflow, OverflowError):
            raise CalcError("Result is too large")
        except (decimal.InvalidOperation, ValueError, ArithmeticError):
            raise CalcError("Outside what that function allows")
        except (RecursionError, MemoryError):
            raise CalcError("Too complicated to calculate")
        except Exception:
            raise CalcError("Can't calculate that")
        if not value.is_finite():
            raise CalcError("Result is too large")
        return value

    @staticmethod
    def _is_pct(node):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "pct"

    def _ev(self, n, nums):
        if isinstance(n, ast.Name):
            if n.id.startswith("_n"):
                return D(nums[int(n.id[2:])])
            consts = {"pi": PI, "e": E, "tau": 2 * PI, "ans": self.ans}
            if n.id in consts:
                return consts[n.id]
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = self._ev(n.operand, nums)
            return -v if isinstance(n.op, ast.USub) else v
        elif isinstance(n, ast.BinOp):
            return self._binop(n, nums)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNC_NAMES
              and len(n.args) == 1 and not n.keywords):
            return self._call(n.func.id, self._ev(n.args[0], nums))
        raise CalcError("Incomplete expression")

    def _binop(self, n, nums):
        left = self._ev(n.left, nums)
        op = type(n.op)
        # "200 + 15%" → 230 and "200 − 15%" → 170, like a phone calculator
        if op in (ast.Add, ast.Sub) and self._is_pct(n.right):
            change = left * self._ev(n.right.args[0], nums) / 100
            return left + change if op is ast.Add else left - change
        right = self._ev(n.right, nums)
        if op is ast.Add:
            return left + right
        if op is ast.Sub:
            return left - right
        if op is ast.Mult:
            return left * right
        if op in (ast.Div, ast.Mod) and right == 0:
            raise CalcError("Can't divide by zero")
        if op is ast.Div:
            return left / right
        if op is ast.Mod:
            r = left % right                     # result takes the divisor's sign, like Excel's MOD
            return r + right if r and (r < 0) != (right < 0) else r
        if op is ast.Pow:
            return self._pow(left, right)
        raise CalcError("Incomplete expression")

    @staticmethod
    def _pow(a, b):
        if a == 0:
            if b < 0:
                raise CalcError("Can't divide by zero")
            return D(1) if b == 0 else D(0)
        if b == b.to_integral_value():
            if abs(b) > 10 ** 9:
                raise CalcError("Result is too large" if abs(a) > 1 else "Exponent is too large")
            return a ** int(b)
        if a < 0:
            raise CalcError("Result isn't a real number")
        return a ** b

    def _call(self, f, x):
        if f in ("sin", "cos", "tan"):
            return self._trig(f, x)
        if f in ("asin", "acos", "atan"):
            if f != "atan" and abs(x) > 1:
                raise CalcError(f"{f} needs a number from −1 to 1")
            r = getattr(math, f)(float(x))
            return _from_float(math.degrees(r) if self.degrees else r)
        if f == "sqrt":
            if x < 0:
                raise CalcError("Can't take √ of a negative number")
            return x.sqrt()
        if f in ("ln", "log"):
            if x <= 0:
                raise CalcError("Log needs a number above 0")
            return x.ln() if f == "ln" else x.log10()
        if f == "exp":
            return x.exp()
        if f == "fact":
            if x < 0 or x != x.to_integral_value():
                raise CalcError("n! needs a whole number, 0 or more")
            if x > 3000:
                raise CalcError("Result is too large")
            return D(math.factorial(int(x)))
        if f == "pct":
            return x / 100
        if f == "abs":
            return abs(x)
        rounding = {"round": decimal.ROUND_HALF_UP, "floor": decimal.ROUND_FLOOR,
                    "ceil": decimal.ROUND_CEILING}[f]
        return x.to_integral_value(rounding=rounding)

    def _trig(self, f, x):
        if self.degrees:
            k = x % 360
            if f == "tan" and k in (90, 270):
                raise CalcError("tan is undefined at 90°")
            if k in EXACT_DEGREES[f]:
                return D(EXACT_DEGREES[f][k])
            x = x * PI / 180
        sin, cos = _sin_cos(x)
        if f == "sin":
            return sin
        if f == "cos":
            return cos
        if cos == 0:
            raise CalcError("tan is undefined there")
        return sin / cos


def format_number(v, separators=False, sig=16) -> str:
    """Round to `sig` significant digits and write it the way people expect.
    Uses an ASCII minus, so the text is safe to paste anywhere."""
    d = v if isinstance(v, D) else D(str(v))
    if not d.is_finite():
        return "Error"
    if d.is_zero():
        return "0"
    with localcontext(CTX):
        exp = d.adjusted()
        if exp >= sig or exp < -9:                  # very large / very small → scientific
            m = d.scaleb(-exp).quantize(D(1).scaleb(-(sig - 1)), rounding=decimal.ROUND_HALF_UP)
            if abs(m) >= 10:
                m, exp = m / 10, exp + 1
            ms = format(m, "f")
            if "." in ms:
                ms = ms.rstrip("0").rstrip(".")
            return f"{ms}e{exp}"
        q = d.quantize(D(1).scaleb(exp - sig + 1), rounding=decimal.ROUND_HALF_UP)
        if q.adjusted() >= sig:                     # rounding carried into the next power
            return format_number(q, separators, sig)
        s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        return "0"
    if separators:
        neg = s.startswith("-")
        ip, dot, fp = s.lstrip("-").partition(".")
        s = ("-" if neg else "") + f"{int(ip):,}" + dot + fp
    return s


# Regexes used by the display for editing (they accept both − and -)
NUMBER_RE = re.compile(r"[−-]?(?:\d[\d,]*\.?\d*|\.\d+)(?:e[+\-−]?\d+)?")
LAST_NUMBER = re.compile(r"(?:\d[\d,]*\.?\d*|\.\d+)(?:e[+\-−]?\d+)?$")
NUMBERISH = re.compile(r"[\s$€£¥]*\(?[-−]?[\s$€£¥]*[\d,]*\.?\d+\)?%?\s*")


def is_plain_number(text: str) -> bool:
    return bool(NUMBER_RE.fullmatch(text.strip()))


def smart_paste_text(text: str) -> str:
    """Tidy pasted text. Accounting negatives like (50.00) become −50.00, and a
    column or row of numbers (e.g. copied from a spreadsheet) becomes a sum."""
    parts = [p.strip() for p in re.split(r"[\r\n\t]+", text) if p.strip()]
    if parts and all(NUMBERISH.fullmatch(p) for p in parts):
        fixed = []
        for p in parts:
            q = re.sub(r"[\s$€£¥]", "", p)
            if q.startswith("(") and q.endswith(")"):
                q = "−" + q[1:-1]
            fixed.append(q.replace("-", "−"))
        return " + ".join(fixed).replace("+ −", "− ")
    return " ".join(parts)


# ════════════════════════════════════════════════════════════════════
#  2. Themes
# ════════════════════════════════════════════════════════════════════

COLOR_KEYS = [
    ("window", "Window background"), ("window_text", "Window text & icons"),
    ("display_bg", "Display background"), ("display_text", "Display text"),
    ("preview_text", "Small text above the result"),
    ("num_bg", "Number buttons"), ("num_text", "Number button text"),
    ("op_bg", "Operator buttons (+ − × ÷)"), ("op_text", "Operator button text"),
    ("fn_bg", "Other buttons (C, %, √ …)"), ("fn_text", "Other button text"),
    ("eq_bg", "Equals button"), ("eq_text", "Equals button text"),
    ("panel_bg", "History background"), ("panel_text", "History text"),
    ("accent", "Accent / selection"),
]
TEXT_AND_BG_KEYS = ["window", "window_text", "display_bg", "display_text",
                    "preview_text", "panel_bg", "panel_text", "accent"]


def _preset(*hex_colors):
    return dict(zip([k for k, _ in COLOR_KEYS], hex_colors))


PRESETS = {
    "Graphite": _preset("#202020", "#ffffff", "#202020", "#ffffff", "#9d9d9d",
                        "#3b3b3b", "#ffffff", "#323232", "#ffffff", "#323232", "#ffffff",
                        "#4cc2ff", "#00263a", "#272727", "#ffffff", "#4cc2ff"),
    "Match KDE": {k: "kde" for k, _ in COLOR_KEYS},
    "Midnight": _preset("#14161c", "#d8dce6", "#14161c", "#f2f4f8", "#8b93a7", "#262a35", "#e8ebf2",
                        "#1f232c", "#9fc2ff", "#1f232c", "#c4c9d6", "#4c8dff", "#ffffff",
                        "#1a1d24", "#d8dce6", "#4c8dff"),
    "Paper": _preset("#f3f3f3", "#1b1b1b", "#f3f3f3", "#1b1b1b", "#6b6b6b", "#ffffff", "#1b1b1b",
                     "#f9f9f9", "#1b1b1b", "#f9f9f9", "#1b1b1b", "#005fb8", "#ffffff",
                     "#ebebeb", "#1b1b1b", "#005fb8"),
    "Nord": _preset("#2e3440", "#e5e9f0", "#2e3440", "#eceff4", "#9aa3b5", "#434c5e", "#eceff4",
                    "#3b4252", "#88c0d0", "#3b4252", "#d8dee9", "#88c0d0", "#2e3440",
                    "#343b48", "#e5e9f0", "#81a1c1"),
    "Forest": _preset("#1b2420", "#dfe8e1", "#1b2420", "#e9f0ea", "#8fa596", "#2f3d36", "#e9f0ea",
                      "#25302b", "#a8e0b5", "#25302b", "#c3d1c7", "#5cc982", "#0e1a13",
                      "#202b26", "#dfe8e1", "#5cc982"),
    "Sunset": _preset("#241b1f", "#f3e3dc", "#241b1f", "#fbeee6", "#b89a9a", "#3d2d34", "#fbeee6",
                      "#30242a", "#ffb38a", "#30242a", "#e3c9c3", "#ff7a59", "#2a1f24",
                      "#2b2025", "#f3e3dc", "#ff7a59"),
    "High Contrast": _preset("#000000", "#ffffff", "#000000", "#ffffff", "#ffff00", "#1c1c1c",
                             "#ffffff", "#000000", "#ffff00", "#000000", "#00ffff", "#ffff00",
                             "#000000", "#000000", "#ffffff", "#ffff00"),
}
DEFAULT_PRESET = "Graphite"


def blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(*(x * (1 - t) + y * t for x, y in
                             ((a.redF(), b.redF()), (a.greenF(), b.greenF()), (a.blueF(), b.blueF()))))


def rgba(c: QColor, alpha: float) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def kde_color(key: str, pal: QPalette) -> QColor:
    """Map one of our color slots onto the current Plasma (Qt) palette."""
    R = QPalette.ColorRole
    btn, win, wtext = pal.color(R.Button), pal.color(R.Window), pal.color(R.WindowText)
    mapping = {
        "window": win, "window_text": wtext,
        "display_bg": win, "display_text": wtext, "preview_text": blend(wtext, win, 0.4),
        "num_bg": btn, "num_text": pal.color(R.ButtonText),
        "op_bg": blend(btn, win, 0.5), "op_text": pal.color(R.ButtonText),
        "fn_bg": blend(btn, win, 0.5), "fn_text": pal.color(R.ButtonText),
        "eq_bg": pal.color(R.Highlight), "eq_text": pal.color(R.HighlightedText),
        "panel_bg": pal.color(R.Base), "panel_text": pal.color(R.Text),
        "accent": pal.color(R.Highlight),
    }
    return QColor(mapping[key])


def effective_colors(s: dict) -> dict:
    """With "Match KDE colors" on, every color (text included) comes from Plasma."""
    return PRESETS["Match KDE"] if s.get("follow_kde") else s["colors"]


def resolve_colors(colors: dict) -> dict:
    pal = QApplication.palette()
    return {k: kde_color(k, pal) if colors.get(k, "kde") == "kde" else QColor(colors[k])
            for k, _ in COLOR_KEYS}


def build_stylesheet(c: dict, s: dict) -> str:
    r, fs = s["radius"], s["font_size"]
    small_r = max(r - 1, 3)
    wt, pt = c["window_text"], c["panel_text"]
    css = [f"""
QWidget#Root {{ background: {c['window'].name()}; }}
QFrame#Display {{ background: {c['display_bg'].name()}; border-radius: {r + 2}px; }}
QLineEdit#Expr {{ background: transparent; color: {c['display_text'].name()}; border: none; padding: 0 10px;
    selection-background-color: {rgba(c['accent'], 0.40)}; selection-color: {c['display_text'].name()}; }}
QLabel#Preview {{ background: transparent; color: {c['preview_text'].name()}; padding: 0 12px 0 4px;
    font-size: {fs + 1}pt; }}
QLabel#Preview[error="true"] {{ color: #ff6b61; }}
QLabel#ModeTitle {{ color: {wt.name()}; font-size: {fs + 4}pt; font-weight: 600; padding-left: 4px; }}
QToolButton#Bar, QToolButton#Icon {{ color: {wt.name()}; background: transparent; border: none;
    border-radius: {small_r}px; padding: 6px; font-size: {max(fs - 3, 8)}pt; font-weight: 600; }}
QToolButton#Bar:hover, QToolButton#Icon:hover {{ background: {rgba(wt, 0.09)}; }}
QToolButton#Bar:pressed, QToolButton#Icon:pressed {{ background: {rgba(wt, 0.05)}; }}
QToolButton#Icon:checked {{ background: {rgba(c['accent'], 0.22)}; }}
QWidget#History {{ background: {c['panel_bg'].name()}; border-radius: {r + 2}px; }}
QWidget#History QToolButton#Icon {{ color: {pt.name()}; }}
QWidget#History QToolButton#Icon:hover {{ background: {rgba(pt, 0.09)}; }}
QLabel#HistTitle {{ color: {pt.name()}; font-weight: 600; font-size: {fs + 1}pt; }}
QLabel#HistEmpty {{ color: {rgba(pt, 0.55)}; font-size: {fs - 1}pt; }}
QLineEdit#Search {{ background: {rgba(pt, 0.06)}; color: {pt.name()};
    border: 1px solid {rgba(pt, 0.08)}; border-radius: {small_r}px; padding: 5px 8px; }}
QLineEdit#Search:focus {{ border-bottom: 2px solid {c['accent'].name()}; }}
QListWidget#HistList {{ background: transparent; border: none; outline: none; }}
QSplitter::handle {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {rgba(pt, 0.25)}; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
"""]
    for role in ("num", "op", "fn", "eq"):
        bg, fg = c[f"{role}_bg"], c[f"{role}_text"]
        size = fs + 4 if role != "fn" else fs + 1
        weight = 600 if role in ("num", "eq") else 400
        edge = QColor("#ffffff" if bg.lightness() < 128 else "#000000")
        css.append(f"""
QPushButton[role="{role}"] {{ background: {bg.name()}; color: {fg.name()};
    border: 1px solid {rgba(edge, 0.06)}; border-bottom-color: {rgba(QColor('#000000'), 0.18)};
    border-radius: {r}px; font-size: {size}pt; font-weight: {weight}; padding: 2px; }}
QPushButton[role="{role}"]:hover {{ background: {blend(bg, fg, 0.07).name()}; }}
QPushButton[role="{role}"]:pressed {{ background: {blend(bg, c['window'], 0.45).name()};
    color: {blend(fg, bg, 0.30).name()}; }}""")
    return "\n".join(css)


def draw_icon(kind: str, color: QColor, size: int = 18) -> QIcon:
    """Small line icons drawn in code, so they always match the theme colors."""
    scale = 3
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(scale, scale)
    pen = QPen(color, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    s = size
    pt = lambda x, y: QPointF(s * x, s * y)
    if kind == "menu":
        for y in (0.3, 0.5, 0.7):
            p.drawLine(pt(0.18, y), pt(0.82, y))
    elif kind == "history":
        p.drawEllipse(pt(0.5, 0.5), s * 0.34, s * 0.34)
        p.drawLine(pt(0.5, 0.5), pt(0.5, 0.30))
        p.drawLine(pt(0.5, 0.5), pt(0.64, 0.60))
    elif kind == "copy":
        p.drawRoundedRect(QRectF(s * 0.34, s * 0.30, s * 0.44, s * 0.52), 2, 2)
        path = QPainterPath(pt(0.22, 0.66))
        path.lineTo(pt(0.22, 0.22))
        path.quadTo(pt(0.22, 0.18), pt(0.26, 0.18))
        path.lineTo(pt(0.62, 0.18))
        p.drawPath(path)
    elif kind == "trash":
        p.drawLine(pt(0.20, 0.28), pt(0.80, 0.28))
        lid = QPainterPath(pt(0.40, 0.28))
        for x, y in ((0.42, 0.18), (0.58, 0.18), (0.60, 0.28)):
            lid.lineTo(pt(x, y))
        p.drawPath(lid)
        body = QPainterPath(pt(0.28, 0.28))
        for x, y in ((0.32, 0.82), (0.68, 0.82), (0.72, 0.28)):
            body.lineTo(pt(x, y))
        p.drawPath(body)
        p.drawLine(pt(0.44, 0.42), pt(0.45, 0.68))
        p.drawLine(pt(0.56, 0.42), pt(0.55, 0.68))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


# ════════════════════════════════════════════════════════════════════
#  3. Storage
# ════════════════════════════════════════════════════════════════════

SETTINGS_VERSION = 3
DEFAULTS = {
    "preset": DEFAULT_PRESET,
    "colors": dict(PRESETS[DEFAULT_PRESET]),
    "follow_kde": False,
    "font_size": 13, "radius": 6, "spacing": 3,
    "mode": "standard",
    "degrees": True, "sig_digits": 16,
    "separators": True, "copy_separators": False,
    "keep_history": True, "show_history": False,
    "geometry": "", "split": "",
}
RANGES = {"font_size": (10, 22), "radius": (0, 24), "spacing": (0, 16), "sig_digits": (6, 16)}
WINDOW_STATE_KEYS = ("geometry", "split", "show_history")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except OSError:
        pass


def _valid(value, default):
    """Return value if it has the same type as default (ints are coerced), else default."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return value if isinstance(value, type(default)) else default


def load_settings() -> dict:
    """Read settings, ignoring anything missing, mistyped or out of range."""
    s = copy.deepcopy(DEFAULTS)
    saved = load_json(SETTINGS_FILE, {})
    if not isinstance(saved, dict):
        return s
    for key, default in DEFAULTS.items():
        if key != "colors" and key in saved:
            s[key] = _valid(saved[key], default)
    colors = saved.get("colors")
    if isinstance(colors, dict):
        s["colors"].update({k: v for k, v in colors.items() if k in s["colors"] and isinstance(v, str)
                            and (v == "kde" or QColor.isValidColorName(v))})
    if saved.get("version", 1) < SETTINGS_VERSION:
        s["sig_digits"] = DEFAULTS["sig_digits"]          # older versions defaulted to 12 digits
    for key, (lo, hi) in RANGES.items():
        s[key] = max(lo, min(hi, s[key]))
    if s["mode"] not in ("standard", "scientific"):
        s["mode"] = "standard"
    return s


def save_settings_file(s: dict):
    data = {k: v for k, v in s.items() if k in DEFAULTS}
    data["version"] = SETTINGS_VERSION
    save_json(SETTINGS_FILE, data)


def migrate_old_files():
    """Carry settings and history over from the app's earlier name, Tally."""
    for old, new in ((CONFIG_HOME / "tally" / "settings.json", SETTINGS_FILE),
                     (DATA_HOME / "tally" / "history.json", HISTORY_FILE)):
        if old.exists() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                new.write_bytes(old.read_bytes())
            except OSError:
                pass


def load_history() -> list:
    data = load_json(HISTORY_FILE, [])
    if not isinstance(data, list):
        return []
    entries = [e for e in data if isinstance(e, dict)
               and isinstance(e.get("expr"), str) and isinstance(e.get("result"), str)]
    return entries[:HISTORY_LIMIT]


# ════════════════════════════════════════════════════════════════════
#  4. Widgets
# ════════════════════════════════════════════════════════════════════

CONTINUE_CHARS = set("+-*/^%!)×÷−²xX")     # typed after "=", these continue from the result
PRETTY_KEYS = {"*": "×", "/": "÷", "-": "−"}
NAV_KEYS = {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End}


def icon_button(tip: str, size: int = 18, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setObjectName("Icon")
    b.setToolTip(tip)
    b.setIconSize(QSize(size, size))
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class ElidedLabel(QLabel):
    """A label that trims long text from the left with “…”, keeping the end visible."""
    def __init__(self):
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str):
        self._full = text
        self._elide()

    def full_text(self) -> str:
        return self._full

    def _elide(self):
        avail = max(self.contentsRect().width() - 4, 20)
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideLeft, avail)
        self.setText(shown)
        self.setToolTip(self._full if shown != self._full else "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()


class ExprEdit(QLineEdit):
    """The big display. Editable, selectable, and shrinks its font to fit."""
    MAX_PT, MIN_PT = 40, 15

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("Expr")
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setFrame(False)
        self.setPlaceholderText("0")
        self.textChanged.connect(self.fit_font)

    def fit_font(self):
        f = QFont(self.font())
        f.setWeight(QFont.Weight.DemiBold)
        avail = max(self.width() - 30, 50)
        text = self.text() or "0"
        size = self.MAX_PT * self.win.active["font_size"] / 13
        while size > self.MIN_PT:
            f.setPointSizeF(size)
            if QFontMetrics(f).horizontalAdvance(text) <= avail:
                break
            size -= 1
        f.setPointSizeF(size)
        self.setFont(f)
        self.setMinimumHeight(QFontMetrics(f).height() + 8)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_font()

    def mousePressEvent(self, e):
        self.win.leave_fresh_start()        # clicking into a result means "edit it"
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        w, key, text = self.win, e.key(), e.text()
        command = e.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                   | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) or text == "=":
            w.evaluate()
        elif key == Qt.Key.Key_Escape:
            w.clear_all()
        elif e.matches(QKeySequence.StandardKey.Paste):
            self.smart_paste()
        elif e.matches(QKeySequence.StandardKey.Copy) and not self.hasSelectedText():
            w.copy_result()
        elif (key == Qt.Key.Key_Delete and not self.hasSelectedText()
              and self.cursorPosition() == len(self.text())):
            w.clear_entry()
        elif text and text.isprintable() and not command:
            w.prepare_input(continues=text in CONTINUE_CHARS)
            if text in PRETTY_KEYS:
                self.insert(PRETTY_KEYS[text])
            else:
                super().keyPressEvent(e)
        else:
            if key in NAV_KEYS:
                w.leave_fresh_start()
            super().keyPressEvent(e)

    def smart_paste(self):
        text = smart_paste_text(QGuiApplication.clipboard().text())
        if text:
            self.win.prepare_input(continues=text[0] in CONTINUE_CHARS)
            self.insert(text)

    def contextMenuEvent(self, e):
        m = self.createStandardContextMenu()
        for act in m.actions():
            if act.objectName() == "edit-paste":   # route menu paste through the tidy-up too
                act.triggered.disconnect()
                act.triggered.connect(self.smart_paste)
        m.addSeparator()
        m.addAction(self.win.act_copy)
        m.exec(e.globalPos())
        m.deleteLater()


class HistoryDelegate(QStyledItemDelegate):
    """Windows-style rows: small grey calculation above a big result, right-aligned."""
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel

    def _fonts(self, option):
        fs = self.panel.win.active["font_size"]
        small, big = QFont(option.font), QFont(option.font)
        small.setPointSizeF(max(fs - 2, 8))
        big.setPointSizeF(fs + 6)
        big.setWeight(QFont.Weight.DemiBold)
        return small, big

    def sizeHint(self, option, index):
        small, big = self._fonts(option)
        return QSize(100, QFontMetrics(small).height() + QFontMetrics(big).height() + 22)

    def paint(self, p, option, index):
        colors = self.panel.win.colors
        text_c = colors["panel_text"]
        expr = index.data(HistoryPanel.EXPR_ROLE)
        result = index.data(HistoryPanel.RESULT_ROLE)
        small, big = self._fonts(option)
        rect = QRectF(option.rect).adjusted(2, 2, -2, -2)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = option.state & QStyle.StateFlag.State_Selected
        if selected or option.state & QStyle.StateFlag.State_MouseOver:
            bg = QColor(colors["accent"] if selected else text_c)
            bg.setAlphaF(0.22 if selected else 0.07)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            r = self.panel.win.active["radius"]
            p.drawRoundedRect(rect, r, r)
        inner = rect.adjusted(12, 8, -12, -8)
        small_h = QFontMetrics(small).height()
        muted = QColor(text_c)
        muted.setAlphaF(0.62)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        p.setPen(muted)
        p.setFont(small)
        p.drawText(QRectF(inner.left(), inner.top(), inner.width(), small_h), right,
                   QFontMetrics(small).elidedText(f"{expr} =", Qt.TextElideMode.ElideLeft, int(inner.width())))
        # Long results shrink to fit; if they still don't, trim the end — never the leading digits
        while big.pointSizeF() > small.pointSizeF() and QFontMetrics(big).horizontalAdvance(result) > inner.width():
            big.setPointSizeF(big.pointSizeF() - 1)
        p.setPen(text_c)
        p.setFont(big)
        p.drawText(QRectF(inner.left(), inner.top() + small_h + 2, inner.width(), inner.height() - small_h - 2),
                   right, QFontMetrics(big).elidedText(result, Qt.TextElideMode.ElideRight, int(inner.width())))
        p.restore()


class HistoryList(QListWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setObjectName("HistList")
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setSpacing(1)
        self.setItemDelegate(HistoryDelegate(panel))

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Copy):
            self.panel.copy_selected()
        elif e.key() == Qt.Key.Key_Delete:
            self.panel.delete_selected()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.panel.use_current("result")
        else:
            super().keyPressEvent(e)


class HistoryPanel(QWidget):
    INDEX_ROLE = Qt.ItemDataRole.UserRole
    EXPR_ROLE = Qt.ItemDataRole.UserRole + 1
    RESULT_ROLE = Qt.ItemDataRole.UserRole + 2

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("History")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(230)
        self.entries = load_history() if win.settings["keep_history"] else []
        self._format_sig = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 12, 10, 8)
        lay.setSpacing(8)
        title = QLabel("History")
        title.setObjectName("HistTitle")
        lay.addWidget(title)
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        lay.addWidget(self.search)
        self.empty = QLabel()
        self.empty.setObjectName("HistEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.empty.setContentsMargins(4, 6, 4, 0)
        lay.addWidget(self.empty)
        self.list = HistoryList(self)
        self.list.itemDoubleClicked.connect(lambda _: self.use_current("result"))
        self.list.customContextMenuRequested.connect(self.context_menu)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_copy = icon_button("Copy all history")
        self.btn_copy.clicked.connect(self.copy_all)
        self.btn_clear = icon_button("Clear history")
        self.btn_clear.clicked.connect(self.clear_all)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_clear)
        lay.addLayout(row)

    def apply_theme(self, colors, settings):
        self.btn_copy.setIcon(draw_icon("copy", colors["panel_text"]))
        self.btn_clear.setIcon(draw_icon("trash", colors["panel_text"]))
        sig = (settings["font_size"], settings["separators"], settings["sig_digits"])
        if sig != self._format_sig:          # row text or height changes → rebuild rows
            self._format_sig = sig
            self.refresh()
        self.list.viewport().update()

    # --- data
    def _fmt(self, raw, for_copy=False):
        try:
            return self.win.format_value(D(raw), for_copy)
        except decimal.InvalidOperation:
            return raw

    def _line(self, e):
        return f"{e['expr']} = {self._fmt(e['result'], True)}"

    def _entry(self, item):
        return self.entries[item.data(self.INDEX_ROLE)]

    def _selected_items(self):
        return sorted((i for i in self.list.selectedItems() if not i.isHidden()), key=self.list.row)

    def save(self):
        if self.win.settings["keep_history"]:
            save_json(HISTORY_FILE, self.entries)
        else:
            HISTORY_FILE.unlink(missing_ok=True)

    def add(self, expr, raw):
        if self.entries and self.entries[0]["expr"] == expr and self.entries[0]["result"] == raw:
            return
        self.entries.insert(0, {"expr": expr, "result": raw, "t": int(time.time())})
        del self.entries[HISTORY_LIMIT:]
        self.save()
        self.refresh()

    def refresh(self):
        self.list.clear()
        for i, e in enumerate(self.entries):
            shown = self._fmt(e["result"])
            item = QListWidgetItem(f"{e['expr']} = {shown}")
            item.setData(self.INDEX_ROLE, i)
            item.setData(self.EXPR_ROLE, e["expr"])
            item.setData(self.RESULT_ROLE, shown)
            if isinstance(e.get("t"), int):
                item.setToolTip(time.strftime("%a %b %d, %I:%M %p", time.localtime(e["t"])))
            self.list.addItem(item)
        self.apply_filter()

    def apply_filter(self):
        q = self.search.text().strip().lower()
        shown = 0
        for i in range(self.list.count()):
            item = self.list.item(i)
            text = item.text().lower()
            hide = bool(q) and q not in text and q not in text.replace(",", "")
            item.setHidden(hide)
            shown += not hide
        self.empty.setText("No matches" if self.entries else "There's no history yet")
        self.empty.setVisible(shown == 0)
        self.search.setVisible(bool(self.entries))

    # --- actions
    def use_current(self, what):
        item = self.list.currentItem()
        if not item:
            return
        e = self._entry(item)
        if what == "result":
            self.win.insert_value(self._fmt(e["result"]))
        else:
            self.win.set_expression(e["expr"])

    def copy_selected(self):
        items = self._selected_items()
        if items:
            self.win.to_clipboard("\n".join(self._line(self._entry(i)) for i in items))

    def copy_all(self):
        if self.entries:
            self.win.to_clipboard("\n".join(self._line(e) for e in self.entries))

    def delete_selected(self):
        doomed = {i.data(self.INDEX_ROLE) for i in self._selected_items()}
        if doomed:
            self.entries = [e for i, e in enumerate(self.entries) if i not in doomed]
            self.save()
            self.refresh()

    def clear_all(self):
        if self.entries and QMessageBox.question(
                self.win, "Clear history", "Delete all history entries?") == QMessageBox.StandardButton.Yes:
            self.entries = []
            self.save()
            self.refresh()

    def context_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item:
            return
        if not item.isSelected():
            self.list.setCurrentItem(item)
        e = self._entry(item)
        n = len(self._selected_items())
        m = QMenu(self)
        m.addAction("Use this result", lambda: self.win.insert_value(self._fmt(e["result"])))
        m.addAction("Edit this calculation", lambda: self.win.set_expression(e["expr"]))
        m.addSeparator()
        m.addAction("Copy result", lambda: self.win.to_clipboard(self._fmt(e["result"], True)))
        m.addAction("Copy calculation", lambda: self.win.to_clipboard(e["expr"]))
        m.addAction("Copy selected lines" if n > 1 else "Copy calculation and result", self.copy_selected)
        m.addSeparator()
        m.addAction(f"Delete {n} entries" if n > 1 else "Delete", self.delete_selected)
        m.exec(self.list.viewport().mapToGlobal(pos))
        m.deleteLater()


class SettingsDialog(QDialog):
    """Edits a copy of the settings and previews it live; Cancel puts things back."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.orig = copy.deepcopy(win.settings)
        self.work = copy.deepcopy(win.settings)
        self._loading = False
        self.resize(520, 640)

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._colors_tab(), "Colors && Look")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
        self.load_into_widgets()

    # --- layout
    def _general_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.mode = QComboBox()
        self.mode.addItems(["Standard — everyday buttons", "Scientific — adds sin, cos, log…"])
        self.mode.currentIndexChanged.connect(
            lambda i: self.set("mode", "standard" if i == 0 else "scientific"))
        form.addRow("Buttons:", self.mode)
        self.sep = self._check("Show thousands separators (1,234,567)", "separators")
        self.copysep = self._check("Include separators when copying results", "copy_separators")
        self.copysep.setToolTip("Leave off for clean pasting into spreadsheets and forms.")
        self.keep = self._check("Keep history after closing", "keep_history")
        for cb in (self.sep, self.copysep, self.keep):
            form.addRow(cb)
        self.sig = self._spin(6, 16, "sig_digits", " digits")
        form.addRow("Precision:", self.sig)
        self.angle = QComboBox()
        self.angle.addItems(["Degrees", "Radians"])
        self.angle.currentIndexChanged.connect(lambda i: self.set("degrees", i == 0))
        form.addRow("Angles (Scientific):", self.angle)
        tips = QLabel(
            "<br><b>Handy tricks</b><br>"
            "• Type or paste anything — “$1,250.00 × 3” just works.<br>"
            "• Paste a column of numbers from a spreadsheet to add them up.<br>"
            "• 200 + 15% = 230 &nbsp;·&nbsp; 200 × 15% = 30<br>"
            "• Click anywhere in the display to fix a typo.<br><br>"
            "<b>Keyboard</b><br>"
            "Enter or = &nbsp;calculate<br>Esc &nbsp;clear all &nbsp;·&nbsp; Delete &nbsp;clear entry<br>"
            "Ctrl+C &nbsp;copy result &nbsp;·&nbsp; Ctrl+V &nbsp;paste<br>"
            "Ctrl+H &nbsp;history &nbsp;·&nbsp; Ctrl+Z &nbsp;undo &nbsp;·&nbsp; Alt+1 / Alt+2 &nbsp;Standard / Scientific")
        tips.setWordWrap(True)
        tips.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(tips)
        return w

    def _colors_tab(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        self.follow = QCheckBox("Match KDE colors (including text)")
        bold = self.follow.font()
        bold.setBold(True)
        self.follow.setFont(bold)
        self.follow.setToolTip("Use your Plasma color scheme for every part of the calculator.\n"
                               "Updates live when you change it in System Settings.")
        self.follow.toggled.connect(self.follow_toggled)
        outer.addWidget(self.follow)

        self.custom_box = QWidget()             # greyed out while matching KDE
        lay = QVBoxLayout(self.custom_box)
        lay.setContentsMargins(0, 4, 0, 0)
        form = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS) + ["Custom"])
        self.preset.currentTextChanged.connect(self.preset_changed)
        form.addRow("Theme:", self.preset)
        lay.addLayout(form)
        mix = QPushButton("Use KDE for text && backgrounds, keep my button colors")
        mix.clicked.connect(self.follow_kde_text_and_backgrounds)
        lay.addWidget(mix)
        note = QLabel("Click a color to change it. “Follow KDE” takes that one color from your "
                      "Plasma color scheme.")
        note.setWordWrap(True)
        note.setEnabled(False)
        lay.addWidget(note)

        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setColumnStretch(0, 1)
        self.rows = {}
        for i, (key, label) in enumerate(COLOR_KEYS):
            swatch = QPushButton()
            swatch.setFixedSize(70, 26)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            swatch.clicked.connect(lambda _=False, k=key, lab=label: self.pick_color(k, lab))
            follow = QCheckBox("Follow KDE")
            follow.toggled.connect(lambda on, k=key: self.color_follow_toggled(k, on))
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(swatch, i, 1)
            grid.addWidget(follow, i, 2)
            self.rows[key] = (swatch, follow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_w)
        lay.addWidget(scroll, 1)
        outer.addWidget(self.custom_box, 1)

        sizes = QFormLayout()
        self.font_size = self._spin(*RANGES["font_size"], "font_size", " pt")
        self.radius = self._spin(*RANGES["radius"], "radius", " px")
        self.spacing = self._spin(*RANGES["spacing"], "spacing", " px")
        sizes.addRow("Text size:", self.font_size)
        sizes.addRow("Button roundness:", self.radius)
        sizes.addRow("Button spacing:", self.spacing)
        outer.addLayout(sizes)
        return w

    def _spin(self, lo, hi, key, suffix):
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setSuffix(suffix)
        sp.valueChanged.connect(lambda v: self.set(key, v))
        return sp

    def _check(self, text, key):
        cb = QCheckBox(text)
        cb.toggled.connect(lambda v: self.set(key, v))
        return cb

    # --- state
    def set(self, key, value):
        if not self._loading:
            self.work[key] = value
            self._preview()

    def _preview(self):
        self.win.apply_settings(self.work)

    def load_into_widgets(self):
        self._loading = True
        w = self.work
        self.mode.setCurrentIndex(0 if w["mode"] == "standard" else 1)
        self.preset.setCurrentText(w["preset"] if w["preset"] in PRESETS else "Custom")
        for spin, key in ((self.font_size, "font_size"), (self.radius, "radius"),
                          (self.spacing, "spacing"), (self.sig, "sig_digits")):
            spin.setValue(w[key])
        for cb, key in ((self.sep, "separators"), (self.copysep, "copy_separators"),
                        (self.keep, "keep_history"), (self.follow, "follow_kde")):
            cb.setChecked(w[key])
        self.angle.setCurrentIndex(0 if w["degrees"] else 1)
        self.custom_box.setEnabled(not w["follow_kde"])
        self.refresh_swatches()
        self._loading = False

    def refresh_swatches(self):
        resolved = resolve_colors(self.work["colors"])
        was_loading, self._loading = self._loading, True
        for key, (swatch, follow) in self.rows.items():
            kde = self.work["colors"][key] == "kde"
            follow.setChecked(kde)
            swatch.setEnabled(not kde)
            swatch.setToolTip("Following KDE" if kde else resolved[key].name())
            swatch.setStyleSheet(f"background: {resolved[key].name()}; border: 1px solid #888; border-radius: 4px;")
        self._loading = was_loading

    def _colors_changed(self, preset_name="Custom"):
        self.work["preset"] = preset_name
        self.preset.blockSignals(True)
        self.preset.setCurrentText(preset_name)
        self.preset.blockSignals(False)
        self.refresh_swatches()
        self._preview()

    def follow_toggled(self, on):
        self.custom_box.setEnabled(not on)
        self.set("follow_kde", on)

    def preset_changed(self, name):
        if not self._loading and name in PRESETS:
            self.work["colors"] = dict(PRESETS[name])
            self._colors_changed(name)

    def pick_color(self, key, label):
        current = resolve_colors(self.work["colors"])[key]
        chosen = QColorDialog.getColor(current, self, f"Choose: {label}")
        if chosen.isValid():
            self.work["colors"][key] = chosen.name()
            self._colors_changed()

    def color_follow_toggled(self, key, on):
        if not self._loading:
            self.work["colors"][key] = "kde" if on else resolve_colors(self.work["colors"])[key].name()
            self._colors_changed()

    def follow_kde_text_and_backgrounds(self):
        for key in TEXT_AND_BG_KEYS:
            self.work["colors"][key] = "kde"
        self._colors_changed()

    def restore_defaults(self):
        keep = {k: self.work[k] for k in WINDOW_STATE_KEYS}
        self.work = copy.deepcopy(DEFAULTS)
        self.work.update(keep)
        self.load_into_widgets()
        self._preview()

    def accept(self):
        self.win.settings = copy.deepcopy(self.work)
        self.win.apply_settings(self.win.settings)
        self.win.save_settings()
        self.win.history.save()             # writes, or deletes the file if history is now off
        super().accept()

    def reject(self):
        self.win.apply_settings(self.orig)
        super().reject()


# ════════════════════════════════════════════════════════════════════
#  5. Main window
# ════════════════════════════════════════════════════════════════════

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        migrate_old_files()
        self.settings = load_settings()
        self.active = self.settings          # the settings on screen (a dialog may preview others)
        self.colors = resolve_colors(effective_colors(self.settings))
        self.calc = Calculator()
        self.just_evaluated = False          # True right after "=": next number starts fresh
        self._programmatic = False
        self._last_expr = ""
        self._exact = None                   # (text shown, full-precision value) of the last result
        self._flash_timer = QTimer(self, singleShot=True, interval=1200)
        self._flash_timer.timeout.connect(self.update_preview)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon.fromTheme("accessories-calculator")))
        self.setMinimumSize(300, 460)

        self._build_actions()
        self._build_ui()

        s = self.settings
        self.btn_hist.blockSignals(True)
        self.btn_hist.setChecked(s["show_history"])
        self.btn_hist.blockSignals(False)
        self.act_hist.blockSignals(True)
        self.act_hist.setChecked(s["show_history"])
        self.act_hist.blockSignals(False)
        self.history.setVisible(s["show_history"])
        if s["geometry"]:
            self.restoreGeometry(QByteArray.fromHex(s["geometry"].encode()))
        else:
            self.resize(620 if s["show_history"] else 340, 560)
        if s["split"]:
            self.split.restoreState(QByteArray.fromHex(s["split"].encode()))

        self.apply_settings(self.settings)
        self._pal_sig = self._palette_signature()
        QApplication.instance().installEventFilter(self)
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: QTimer.singleShot(50, self._check_palette))
        self.expr.setFocus()

    # --- construction
    def _action(self, text, shortcut=None, slot=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        if slot:
            (a.toggled if checkable else a.triggered).connect(slot)
        self.addAction(a)                    # shortcuts work whether or not the menu is open
        return a

    def _build_actions(self):
        modes = QActionGroup(self)
        self.act_std = self._action("Standard", "Alt+1", lambda: self.change_setting("mode", "standard"))
        self.act_sci = self._action("Scientific", "Alt+2", lambda: self.change_setting("mode", "scientific"))
        for a in (self.act_std, self.act_sci):
            a.setCheckable(True)
            modes.addAction(a)
        self.act_kde = self._action("Match KDE colors", slot=lambda on: self.change_setting("follow_kde", on),
                                    checkable=True)
        self.act_hist = self._action("Show history", "Ctrl+H", checkable=True,
                                     slot=lambda on: self.btn_hist.setChecked(on))
        self.act_settings = self._action("Settings && colors…", "Ctrl+,", self.open_settings)
        self.act_copy = self._action("Copy result", "Ctrl+Shift+C", self.copy_result)
        self._action("Quit", "Ctrl+Q", self.close)
        self.menu = QMenu(self)
        for item in (self.act_std, self.act_sci, None, self.act_kde, self.act_hist, None, self.act_settings):
            self.menu.addAction(item) if item else self.menu.addSeparator()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.root = QWidget()
        self.root.setObjectName("Root")
        self.root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.root)
        lay = QVBoxLayout(self.root)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # top bar:  ☰ Standard ............ DEG  🕘
        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.btn_menu = icon_button("Menu", 20)
        self.btn_menu.clicked.connect(
            lambda: self.menu.exec(self.btn_menu.mapToGlobal(self.btn_menu.rect().bottomLeft())))
        self.mode_title = QLabel()
        self.mode_title.setObjectName("ModeTitle")
        self.btn_angle = QToolButton()
        self.btn_angle.setObjectName("Bar")
        self.btn_angle.setToolTip("Angle unit for sin/cos/tan — click to switch")
        self.btn_angle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_angle.clicked.connect(lambda: self.change_setting("degrees", not self.active["degrees"]))
        self.btn_hist = icon_button("History (Ctrl+H)", 20, checkable=True)
        self.btn_hist.toggled.connect(self.toggle_history)
        for widget in (self.btn_menu, self.mode_title):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self.btn_angle)
        bar.addWidget(self.btn_hist)
        lay.addLayout(bar)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        calc_w = QWidget()
        cl = QVBoxLayout(calc_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        cl.addWidget(self._build_display())
        cl.addSpacing(4)

        I, U = self.ins, self.apply_unary
        self.sci_panel = QWidget()
        self.sci_grid = self._button_grid(self.sci_panel, "fn", [
            [("sin", I("sin(")), ("cos", I("cos(")), ("tan", I("tan(")), ("π", I("π"), "Pi")],
            [("ln", I("ln("), "Natural log"), ("log", I("log("), "Log base 10"),
             ("xʸ", I("^", True), "Power"), ("n!", I("!", True), "Factorial")],
            [("(", I("(")), (")", I(")", True)), ("|x|", I("abs("), "Absolute value"),
             ("ℯ", I("ℯ"), "Euler's number")],
        ])
        cl.addWidget(self.sci_panel, 3)
        main_panel = QWidget()
        self.main_grid = self._button_grid(main_panel, "num", [
            [("%", I("%", True), "Percent — 200 + 15% = 230", "fn"),
             ("CE", self.clear_entry, "Clear entry (Delete)", "fn"),
             ("C", self.clear_all, "Clear all (Esc)", "fn"),
             ("⌫", self.backspace, "Backspace", "fn")],
            [("¹⁄ₓ", U("1/({})"), "One over x", "fn"), ("x²", U("{}²"), "Square", "fn"),
             ("²√x", U("√({})"), "Square root", "fn"), ("÷", I("÷", True), "Divide  ( / )", "op")],
            [("7", I("7")), ("8", I("8")), ("9", I("9")), ("×", I("×", True), "Multiply  ( * )", "op")],
            [("4", I("4")), ("5", I("5")), ("6", I("6")), ("−", I("−", True), "Subtract  ( - )", "op")],
            [("1", I("1")), ("2", I("2")), ("3", I("3")), ("+", I("+", True), "Add  ( + )", "op")],
            [("+/−", self.negate, "Change sign"), ("0", I("0")), (".", I(".")),
             ("=", self.evaluate, "Calculate  (Enter)", "eq")],
        ])
        cl.addWidget(main_panel, 6)

        self.split.addWidget(calc_w)
        self.history = HistoryPanel(self)
        self.split.addWidget(self.history)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        lay.addWidget(self.split, 1)

    def _build_display(self):
        disp = QFrame()
        disp.setObjectName("Display")
        dl = QVBoxLayout(disp)
        dl.setContentsMargins(0, 8, 0, 6)
        dl.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(6, 0, 0, 0)
        self.btn_copy = icon_button("Copy result (Ctrl+C)", 16)
        self.btn_copy.clicked.connect(self.copy_result)
        self.preview = ElidedLabel()
        self.preview.setObjectName("Preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.btn_copy)
        row.addWidget(self.preview, 1)
        dl.addLayout(row)
        self.expr = ExprEdit(self)
        self.expr.textChanged.connect(self.on_text_changed)
        dl.addWidget(self.expr)
        return disp

    def _button_grid(self, parent, default_role, rows):
        """rows: (label, handler[, tooltip[, role]]) per button."""
        grid = QGridLayout(parent)
        grid.setContentsMargins(0, 0, 0, 0)
        for r, row in enumerate(rows):
            grid.setRowStretch(r, 1)
            for c, (label, handler, *extra) in enumerate(row):
                b = QPushButton(label)
                b.setProperty("role", extra[1] if len(extra) > 1 else default_role)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                b.setMinimumSize(40, 34)
                if extra:
                    b.setToolTip(extra[0])
                b.clicked.connect(handler)
                grid.addWidget(b, r, c)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        return grid

    def ins(self, text, continues=False):
        return lambda: self.insert_text(text, continues)

    # --- settings and theming
    def apply_settings(self, s):
        """Show settings `s` (the saved ones, or a dialog's live preview)."""
        self.active = s
        self.colors = resolve_colors(effective_colors(s))
        self.root.setStyleSheet(build_stylesheet(self.colors, s))
        for grid in (self.main_grid, self.sci_grid):
            grid.setSpacing(s["spacing"])
        self.btn_menu.setIcon(draw_icon("menu", self.colors["window_text"]))
        self.btn_hist.setIcon(draw_icon("history", self.colors["window_text"]))
        self.btn_copy.setIcon(draw_icon("copy", self.colors["preview_text"]))
        self.history.apply_theme(self.colors, s)
        scientific = s["mode"] == "scientific"
        self.sci_panel.setVisible(scientific)
        self.btn_angle.setVisible(scientific)
        self.mode_title.setText("Scientific" if scientific else "Standard")
        (self.act_sci if scientific else self.act_std).setChecked(True)
        self.act_kde.blockSignals(True)
        self.act_kde.setChecked(bool(s["follow_kde"]))
        self.act_kde.blockSignals(False)
        self.calc.degrees = s["degrees"]
        self.btn_angle.setText("DEG" if s["degrees"] else "RAD")
        self.expr.fit_font()
        self.update_preview()

    def change_setting(self, key, value):
        """Change one setting from the menu or toolbar, show it, and save it."""
        self.settings[key] = value
        self.active[key] = value
        self.apply_settings(self.active)
        self.save_settings()

    def save_settings(self):
        self.settings["geometry"] = bytes(self.saveGeometry().toHex()).decode()
        self.settings["split"] = bytes(self.split.saveState().toHex()).decode()
        save_settings_file(self.settings)

    # KDE color-scheme changes arrive as palette events; reapply only if colors really changed.
    def _palette_signature(self):
        pal = QApplication.palette()
        R = QPalette.ColorRole
        return tuple(pal.color(r).rgba() for r in (R.Window, R.WindowText, R.Base, R.Text, R.Button,
                                                   R.ButtonText, R.Highlight, R.HighlightedText))

    def _check_palette(self):
        sig = self._palette_signature()
        if sig != self._pal_sig:
            self._pal_sig = sig
            self.apply_settings(self.active)

    def eventFilter(self, obj, e):
        if obj is QApplication.instance() and e.type() == QEvent.Type.ApplicationPaletteChange:
            QTimer.singleShot(0, self._check_palette)
        return False

    def changeEvent(self, e):
        if e.type() in (QEvent.Type.ApplicationPaletteChange, QEvent.Type.PaletteChange):
            QTimer.singleShot(0, self._check_palette)
        super().changeEvent(e)

    # --- editing the display
    def leave_fresh_start(self):
        """The user is editing the shown result rather than starting a new calculation."""
        if self.just_evaluated:
            self.just_evaluated = False
            self.update_preview()

    def prepare_input(self, continues):
        """Right after "=", an operator continues from the result; anything else starts over."""
        if self.just_evaluated:
            if continues:
                self.expr.end(False)
            else:
                self.expr.clear()

    def insert_text(self, text, continues=False):
        self.prepare_input(continues)
        e = self.expr
        if text == ".":
            current = re.search(r"[\d,]*\.?\d*$", e.text()[:e.cursorPosition()]).group()
            if "." in current:
                e.setFocus()
                return                       # this number already has a decimal point
            if not current:
                text = "0."
        e.insert(text)
        e.setFocus()

    def insert_value(self, value):
        """Put a number (e.g. from history) in, replacing the number being typed, like Windows."""
        e = self.expr
        if self.just_evaluated or not e.text().strip():
            self.set_expression(value)
            return
        left, right = e.text()[:e.cursorPosition()], e.text()[e.cursorPosition():]
        m = LAST_NUMBER.search(left)
        if m:
            left = left[:m.start()]
        elif left.rstrip()[-1:] in tuple(")πℯ²!%"):
            left += "×"
        self.set_expression(left + value + right, cursor=len(left) + len(value))

    def set_expression(self, text, cursor=None):
        self._exact = None
        self.expr.setText(text)
        self.expr.setCursorPosition(len(text) if cursor is None else cursor)
        self.expr.setFocus()

    def clear_all(self):
        self._exact = None
        self.just_evaluated = False
        self.expr.clear()
        self._set_preview("")
        self.expr.setFocus()

    def clear_entry(self):
        """CE: remove just the number being typed and keep the rest of the calculation."""
        if self.just_evaluated:
            self.clear_all()
            return
        text = self.expr.text().rstrip()
        m = LAST_NUMBER.search(text)
        if m:
            self.set_expression(text[:m.start()])

    def backspace(self):
        self.leave_fresh_start()
        self.expr.backspace()
        self.expr.setFocus()

    def _last_operand(self, t):
        """(start, end) of the last number, constant or (…) group in t, or None."""
        end = len(t.rstrip())
        if end == 0:
            return None
        if t[end - 1] == ")":
            depth = 0
            for j in range(end - 1, -1, -1):
                depth += {")": 1, "(": -1}.get(t[j], 0)
                if depth == 0:
                    while j > 0 and (t[j - 1].isalpha() or t[j - 1] == "√"):
                        j -= 1               # include a function name: sin(…), √(…)
                    return j, end
            return None
        m = LAST_NUMBER.search(t[:end]) or re.search(r"(π|ℯ|ans)$", t[:end])
        return (m.start(), end) if m else None

    def _operand_span(self, t):
        """The whole display after "=" or when it's one number; otherwise the last operand."""
        if self.just_evaluated or is_plain_number(t):
            return (0, len(t)) if t else None
        return self._last_operand(t)

    def apply_unary(self, template):
        """1/x, x² and √x act on the current number, like Windows."""
        def run():
            t = self.expr.text().strip()
            span = self._operand_span(t)
            if not span:
                return
            a, b = span
            inner = t[a:b]
            if template == "{}²" and not re.fullmatch(r"[\d.,]+|\(.*\)|[πℯ]", inner):
                inner = f"({inner})"
            self.just_evaluated = False
            self.set_expression(t[:a] + template.format(inner) + t[b:])
        return run

    def negate(self):
        t = self.expr.text().strip()
        if not t:
            self.set_expression("−")
            return
        span = self._operand_span(t)
        if not span:
            return
        a, b = span
        inner = t[a:b]
        if a == 0 and b == len(t) and is_plain_number(t):
            new = t[1:] if t[0] in "−-" else "−" + t
        else:
            m = re.fullmatch(r"\([−-](.*)\)", inner)
            new = t[:a] + (m.group(1) if m else f"(−{inner})") + t[b:]
        self.just_evaluated = False
        self.set_expression(new)

    def on_text_changed(self):
        if not self._programmatic:
            self.just_evaluated = False
            self.update_preview()

    # --- results
    def format_value(self, v, for_copy=False):
        s = self.active
        text = format_number(v, s["copy_separators"] if for_copy else s["separators"], s["sig_digits"])
        return text if for_copy else text.replace("-", "−")

    def _calc(self, text):
        """Evaluate, using the full-precision last result when continuing from it
        (so 1 ÷ 3 = then × 3 gives exactly 1, like the Windows calculator)."""
        if self._exact:
            shown, value = self._exact
            nxt = text[len(shown):len(shown) + 1]
            if text.startswith(shown) and (not nxt or nxt not in "0123456789.,eE"):
                text = f"({value})" + text[len(shown):]
        return self.calc.evaluate(text)

    def _set_preview(self, text, error=False):
        self.preview.set_full_text(text)
        if self.preview.property("error") != error:
            self.preview.setProperty("error", error)
            self.preview.style().unpolish(self.preview)
            self.preview.style().polish(self.preview)

    def update_preview(self):
        if self.just_evaluated:
            self._set_preview(f"{self._last_expr} =")
            return
        text = self.expr.text()
        if not text.strip() or is_plain_number(text):
            self._set_preview("")
            return
        try:
            self._set_preview("= " + self.format_value(self._calc(text)))
        except CalcError:
            self._set_preview("")

    def evaluate(self):
        text = self.expr.text().strip()
        if not text or self.just_evaluated:
            return
        try:
            v = self._calc(text)
        except CalcError as err:
            self._set_preview(str(err), error=True)
            return
        self.calc.ans = v
        self._last_expr = text
        if not is_plain_number(text):
            self.history.add(text, format_number(v, False, self.active["sig_digits"]))
        shown = self.format_value(v)
        self._programmatic = True
        self.expr.setText(shown)
        self._programmatic = False
        self.expr.end(False)
        self._exact = (shown, v)
        self.just_evaluated = True
        self.update_preview()

    def copy_result(self):
        try:
            v = self._calc(self.expr.text())
        except CalcError:
            self.flash("Nothing to copy")
            return
        self.to_clipboard(self.format_value(v, True))

    def to_clipboard(self, text):
        QGuiApplication.clipboard().setText(text)
        self.flash("Copied ✓")

    def flash(self, message):
        self._set_preview(message)
        self._flash_timer.start()

    # --- panels and dialogs
    def toggle_history(self, on):
        width = self.history.width() if self.history.isVisible() and self.history.width() > 50 else 270
        self.history.setVisible(on)
        self.act_hist.setChecked(on)
        self.settings["show_history"] = on
        if not (self.isMaximized() or self.isFullScreen()):
            self.resize(self.width() + (width + 8 if on else -(width + 8)), self.height())

    def open_settings(self):
        SettingsDialog(self).exec()
        self.expr.setFocus()

    def closeEvent(self, e):
        self.save_settings()
        super().closeEvent(e)


def _log_unexpected(exc_type, exc, tb):
    traceback.print_exception(exc_type, exc, tb)    # report it, but keep the app running


def main():
    sys.excepthook = _log_unexpected
    QApplication.setApplicationName(APP_NAME)
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
