"""OhNo Launcher - PyQt6 keyword launcher.

- Ctrl+Space toggles a centered, borderless popup.
- Clicking anywhere else (focus loss) closes the popup.
- Keywords are managed inside the popup itself; stored in a GLOBAL config
  location so a built .exe keeps working without a local config.json.
- Inline math: typing e.g. 5*3-2 shows the answer live, Enter copies it.
- prettycode opens a side-by-side prettifier dialog.
"""
import ast
import json
import math
import operator
import os
import re
import subprocess
import sys
import webbrowser

from PyQt6.QtCore import QEvent, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_DIR_NAME = "ohno-launcher"
APP_CONFIG_FILE = "config.json"
SHOW_HOTKEY = "ctrl+space"

WINDOW_WIDTH = 640
INPUT_HEIGHT = 56
ITEM_H = 58
VISIBLE_ROWS = 6
WINDOW_MIN_HEIGHT = 150
WINDOW_MAX_HEIGHT = 480
MAX_SUGGESTIONS = 50
DEFAULT_PLACEHOLDER = "Search keywords or descriptions…  (try: keys, 2+2, add)"


# ---------------------------------------------------------------- config ---

def get_config_path() -> str:
    """Global, per-user config path that survives .exe builds."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, APP_DIR_NAME)
    else:
        folder = os.path.join(os.path.expanduser("~"), ".config", APP_DIR_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, APP_CONFIG_FILE)


def _legacy_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


DEFAULT_CONFIG = {
    "mappings": {
        "timesheet": {
            "target": "https://example.com/timesheet",
            "description": "Open timesheet portal",
        },
        "idea": {
            "target": "",
            "description": "Open IntelliJ IDEA",
        },
    }
}


def norm_key(key: str) -> str:
    """Logic keys are always lowercase; display is Title Case."""
    return (key or "").strip().lower()


def display_key(key: str) -> str:
    """User-facing form: First Letter Caps for every word."""
    return re.sub(r"[_.\-]+", " ", (key or "").strip()).title() or "?"


def nat_key(s: str):
    """Natural sort: alphabetical + numerical (item2 < item10)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def get_target(entry) -> str:
    if isinstance(entry, dict):
        t = entry.get("target", "")
        return t if isinstance(t, str) else str(t)
    return entry if isinstance(entry, str) else str(entry or "")


def get_desc(entry) -> str:
    if isinstance(entry, dict):
        d = entry.get("description", "")
        return d if isinstance(d, str) else str(d or "")
    return ""


def normalize_mappings(raw: dict) -> dict:
    """Legacy plain-string values -> {target, description}; keys -> lowercase."""
    out: dict = {}
    for k, v in (raw or {}).items():
        nk = norm_key(k)
        if not nk:
            continue
        if isinstance(v, dict):
            out[nk] = {
                "target": get_target(v),
                "description": get_desc(v),
            }
        else:
            out[nk] = {"target": v if isinstance(v, str) else str(v or ""),
                       "description": ""}
    return out


def _merge_sections(data: dict) -> dict:
    data.setdefault("mappings", {})
    for section in ("directories", "websites"):
        extra = data.pop(section, None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                nk = norm_key(k)
                if nk and nk not in {norm_key(x) for x in data["mappings"]}:
                    data["mappings"][k] = v
    data["mappings"] = normalize_mappings(data.get("mappings", {}))
    # backfill default descriptions where the user never set one
    for k, v in normalize_mappings(DEFAULT_CONFIG["mappings"]).items():
        if k in data["mappings"] and not data["mappings"][k]["description"]:
            data["mappings"][k]["description"] = v["description"]
    return data


def _load_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_config() -> dict:
    path = get_config_path()
    data = _load_file(path) if os.path.exists(path) else None
    if data is None:
        # migrate from legacy local config.json if present
        legacy = _legacy_config_path()
        if os.path.exists(legacy) and os.path.abspath(legacy) != os.path.abspath(path):
            data = _load_file(legacy)
    if data is None:
        cfg = {"mappings": normalize_mappings(DEFAULT_CONFIG["mappings"])}
        save_config(cfg)
        return cfg
    before = json.dumps(data, sort_keys=True, default=str)
    data = _merge_sections(data)
    # only touch the disk when normalization/migration actually changed something
    if json.dumps(data, sort_keys=True, default=str) != before:
        save_config(data)
    return data


def save_config(config: dict) -> None:
    path = get_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ------------------------------------------------------------ math eval ---

_MATH_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_MATH_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MATH_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "pow": pow, "min": min, "max": max,
}
_MATH_NAMES = {"pi": math.pi, "e": math.e}
_MATH_RE = re.compile(r"^[\d\s\.\+\-\*\/\%\(\)\^,eE_]+$")
_WORD_RE = re.compile(r"[A-Za-z_]+")


def try_eval_math(text: str):
    """Return (ok, result_str). Only pure math, no code execution."""
    s = text.strip().replace("^", "**").replace("x", "*").replace("X", "*")
    if len(s) < 2 or not any(c.isdigit() for c in s):
        return False, ""
    if not any(op in s for op in ("+", "-", "*", "/", "%", "(", "**")):
        return False, ""
    if not _MATH_RE.match(s):
        # allow whitelisted function/constant names
        words = set(_WORD_RE.findall(s))
        if not words or not words <= (set(_MATH_FUNCS) | set(_MATH_NAMES)):
            return False, ""
    try:
        node = ast.parse(s, mode="eval")
        value = _eval_node(node.body)
    except Exception:
        return False, ""
    if isinstance(value, bool):
        return False, ""
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return False, ""
    if isinstance(value, float) and value.is_integer():
        return True, str(int(value))
    return True, str(round(value, 10) if isinstance(value, float) else value)


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _MATH_BINOPS:
        return _MATH_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _MATH_UNARYOPS:
        return _MATH_UNARYOPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MATH_FUNCS:
        if node.keywords:
            raise ValueError("no kwargs")
        return _MATH_FUNCS[node.func.id](*[_eval_node(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in _MATH_NAMES:
        return _MATH_NAMES[node.id]
    raise ValueError("not a math expression")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].strip()
    return s


def parse_add_command(text: str):
    """Parse 'add <key> <target> [| <description>]', incl. key=value / key -> value."""
    m = re.match(r"^(add|set|save)\s+(.+)$", text.strip(), re.IGNORECASE)
    if not m:
        return None, None, None, False
    rest = m.group(2).strip()
    desc = ""
    if "|" in rest:
        rest, _, desc = rest.partition("|")
        rest, desc = rest.strip(), _strip_quotes(desc.strip())
    # `key=value` / `key -> value` glued forms take priority
    for sep in ("->", "=", ":"):
        if sep in rest:
            key, _, value = rest.partition(sep)
            key, value = norm_key(key), _strip_quotes(value)
            if key and value and " " not in key:
                return key, value, desc, True
    parts = rest.split(None, 1)
    if len(parts) < 2:
        return (norm_key(parts[0]) if parts else None), "", desc, True
    return norm_key(parts[0]), _strip_quotes(parts[1]), desc, True


def parse_desc_command(text: str):
    """Parse 'desc <key> <new description>'."""
    m = re.match(r"^desc\s+(\S+)\s+(.+)$", text.strip(), re.IGNORECASE)
    if not m:
        return None, None, False
    return norm_key(m.group(1)), _strip_quotes(m.group(2).strip()), True


_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:.*$")
_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def classify_target(value: str):
    """Decide how a stored target should be opened.

    Returns (kind, arg) with kind in {"url", "path", "scheme", "app", "shell"}:
    - url: http(s) link -> default browser
    - path: existing file/folder -> associated app / explorer
    - scheme: ms-teams:, mailto:, ... -> OS handler
    - app: bare app name (chrome, teams, notepad, ...) -> `start` on
      Windows (resolves App Paths registry + PATH), shell elsewhere
    - shell: full commands / exe paths with args -> run via shell
    """
    v = (value or "").strip().strip("'\"")
    if not v:
        return ("empty", v)
    if v.startswith(("http://", "https://")):
        return ("url", v)
    if os.path.exists(v):
        return ("path", v)
    if _DRIVE_RE.match(v) or v.startswith("\\\\"):
        # drive/UNC path that doesn't exist (yet) -> let the shell try
        return ("shell", v)
    if _URI_RE.match(v):
        return ("scheme", v)
    if re.search(r"[\\/]", v) or v.lower().endswith((".exe", ".bat", ".cmd", ".lnk")):
        return ("shell", v)
    return ("app", v)


def parse_del_command(text: str):
    m = re.match(r"^(del|delete|rm|remove)\s+(\S+)\s*$", text.strip(), re.IGNORECASE)
    if not m:
        return None, False
    return m.group(2).strip(), True


# -------------------------------------------------------------- launcher ---

class ModernLauncher(QWidget):
    toggle_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.shown = False
        self.config = load_config()
        self.keywords = []
        self.manage_mode = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setStyleSheet(
            """
            QLineEdit#SearchBox {
                background: #2d2d44;
                border: none;
                border-radius: 14px;
                padding: 0px 18px;
                font-size: 16px;
                color: #e0e0e0;
                selection-background-color: #3a70b8;
            }
            QListWidget#Results {
                background: #252635;
                border: none;
                border-radius: 14px;
                font-size: 14px;
                color: #cccccc;
                outline: none;
                padding: 6px;
            }
            QListWidget#Results::item {
                padding: 0px;
                margin: 2px 4px 2px 2px;
                border: none;
                border-radius: 10px;
                background: transparent;
            }
            QListWidget#Results::item:selected {
                background: #3a70b8;
                color: #ffffff;
            }
            QListWidget#Results::item:hover:!selected {
                background: #353548;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 4px 2px 4px 0px;
            }
            QScrollBar::handle:vertical {
                background: #4a4a63;
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: #5a5a78;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )

        self._build_ui()
        self.refresh_keywords()
        self.show_suggestions("")
        self._wire_focus_close()
        self._setup_hotkey()
        self.center_window()
        self.hide()

    # -- ui ---------------------------------------------------------
    def _build_ui(self):
        self.shell = QFrame(self)
        self.shell.setObjectName("Shell")
        self.shell.setStyleSheet(
            "QFrame#Shell { background: #1e1e2f; border: 1px solid #34344a;"
            " border-radius: 18px; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.shell)

        layout = QVBoxLayout(self.shell)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.entry = QLineEdit()
        self.entry.setObjectName("SearchBox")
        self.entry.setPlaceholderText(DEFAULT_PLACEHOLDER)
        self.entry.setFixedHeight(INPUT_HEIGHT)
        self.entry.setClearButtonEnabled(True)
        self.entry.textChanged.connect(self.on_text_changed)
        self.entry.returnPressed.connect(self.on_enter)
        self.entry.installEventFilter(self)
        layout.addWidget(self.entry)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("Results")
        self.list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setSpacing(2)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.list_widget.itemActivated.connect(self.on_item_clicked)
        layout.addWidget(self.list_widget)

        self.setFixedWidth(WINDOW_WIDTH)

    # -- config -----------------------------------------------------
    def refresh_keywords(self):
        self.keywords = sorted(self.config.get("mappings", {}).keys(), key=nat_key)

    def persist(self):
        save_config(self.config)
        self.refresh_keywords()

    # -- hotkey / focus ---------------------------------------------
    def _setup_hotkey(self):
        self.toggle_requested.connect(self._toggle_slot)
        try:
            import keyboard

            keyboard.add_hotkey(SHOW_HOTKEY, self.toggle_requested.emit)
        except Exception as exc:  # keep app usable even if hotkey fails
            print(f"[launcher] hotkey '{SHOW_HOTKEY}' unavailable: {exc}")

    def _wire_focus_close(self):
        app = QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.connect(self._on_focus_changed)
            except Exception:
                pass

    def _on_focus_changed(self, _old, _new):
        # Organic close: once the popup (or its dialogs) lose the active
        # window, vanish. No polling, just Qt's own focus signal.
        if not self.shown or self.isHidden():
            return
        if self.isActiveWindow():
            return
        # don't hide while the prettifier dialog (child) is open
        for child in self.findChildren(QDialog):
            if child.isVisible():
                return
        self.hide_popup()

    def changeEvent(self, event):  # noqa: N802 (Qt naming)
        if event.type() == event.Type.ActivationChange and not self.isActiveWindow():
            if self.shown and not self.isHidden():
                self.hide_popup()
        super().changeEvent(event)

    def toggle(self):
        self.toggle_requested.emit()

    def _toggle_slot(self):
        if self.shown and not self.isHidden():
            self.hide_popup()
        else:
            self.show_popup()

    def show_popup(self):
        self.config = load_config()
        self.refresh_keywords()
        self.entry.clear()
        # fresh open: empty box with the default hint, never stale text
        self.entry.setPlaceholderText(DEFAULT_PLACEHOLDER)
        self.show_suggestions("")
        self.center_window()
        self.show()
        self.raise_()
        self.activateWindow()
        self.entry.setFocus()
        self.shown = True

    def hide_popup(self):
        self.shown = False
        self.hide()
        self.entry.clear()

    def center_window(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - WINDOW_WIDTH) // 2
        y = geo.y() + (geo.height() - self.height()) // 2
        self.move(x, max(geo.y() + 40, y))

    # -- suggestions --------------------------------------------------
    # Display strings live in UserRole+1; the item text itself stays EMPTY.
    # (If the item holds text, Qt's delegate paints it underneath our custom
    # widget and every row renders twice — the "double fonts" bug.)
    TEXT_ROLE = Qt.ItemDataRole.UserRole + 1

    def _item(self, title: str, sub: str = "", payload: dict | None = None):
        item = QListWidgetItem()
        item.setToolTip(f"{title}\n{sub}" if sub else title)
        item.setData(Qt.ItemDataRole.UserRole, payload or {})
        item.setData(self.TEXT_ROLE, (title, sub))
        item.setSizeHint(QSize(0, ITEM_H))
        return item

    def _render_rows(self):
        """Paint every row as title-over-description: bold key on top,
        smaller dimmer description below. No separator characters.
        In the manager view each stored keyword also gets a delete button,
        so keys can be added, edited AND deleted without leaving the popup."""
        for r in range(self.list_widget.count()):
            item = self.list_widget.item(r)
            stored = item.data(self.TEXT_ROLE) or ("", "")
            title, sub = stored[0], (stored[1] if len(stored) > 1 else "")
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            is_hint = data.get("kind") == "hint"

            wrap = QFrame()
            wrap.setStyleSheet("QFrame { background: transparent; border: none; }")
            row = QHBoxLayout(wrap)
            row.setContentsMargins(12, 7, 6, 7)
            row.setSpacing(8)

            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(1)
            t = QLabel(title)
            if is_hint:
                t.setStyleSheet("color: #c9c9d6; font-size: 13px; background: transparent; border: none;")
            else:
                t.setStyleSheet("color: #f2f2f7; font-size: 14px; font-weight: 700;"
                                " background: transparent; border: none;")
            text_col.addWidget(t)
            if sub:
                d = QLabel(sub)
                d.setStyleSheet("color: #9a9ab0; font-size: 12px;"
                                " background: transparent; border: none;")
                text_col.addWidget(d)
            text_col.addStretch(1)
            row.addLayout(text_col, 1)

            key = data.get("key", "")
            if (data.get("kind") == "launch"
                    and key in self.config.get("mappings", {})):
                del_btn = QPushButton("\u2715")
                del_btn.setToolTip(f"Delete {display_key(key)}")
                del_btn.setFixedSize(28, 28)
                del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                del_btn.setStyleSheet(
                    "QPushButton { color: #6b6b85; font-size: 13px; font-weight: 700;"
                    " background: transparent; border: none; border-radius: 8px; min-width: 0px; }"
                    "QPushButton:hover { color: #ff7b72; background: #3a2a35; }"
                )
                del_btn.clicked.connect(lambda _=False, k=key: self.delete_mapping(k))
                row.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignVCenter)

            self.list_widget.setItemWidget(item, wrap)

    def _key_row(self, key: str):
        """One row for a stored keyword: bold Title Case + dim description."""
        entry = self.config.get("mappings", {}).get(key, {})
        desc = get_desc(entry).strip()
        sub = desc if desc else "No description yet"
        return self._item(display_key(key), sub, {"kind": "launch", "key": key})

    def show_suggestions(self, text: str):
        raw = text.strip()
        low = raw.lower()
        self.list_widget.clear()
        self.manage_mode = False
        maps = self.config.get("mappings", {})

        # manage view: `keys` / `list` / `manage` — click a row to EDIT it
        if low in ("keys", "list", "manage", "config", "key"):
            self.manage_mode = True
            if not maps:
                self.list_widget.addItem(
                    self._item("No keywords yet",
                               "type:  add timesheet https://… | My portal",
                               {"kind": "hint"})
                )
            else:
                for k in sorted(maps, key=nat_key)[:MAX_SUGGESTIONS]:
                    self.list_widget.addItem(self._key_row(k))
            self._after_list()
            return

        # add / set command (optional `| description`)
        if re.match(r"^(add|set|save)\b", raw, re.IGNORECASE):
            key, value, desc, parsed = parse_add_command(raw)
            if parsed and key and value:
                exists = key in maps
                verb = "Update" if exists else "Add"
                sub = desc if desc else (get_desc(maps.get(key, {})).strip()
                                         or "Enter to save")
                self.list_widget.addItem(
                    self._item(f"{verb} {display_key(key)}", sub,
                               {"kind": "save", "key": key,
                                "value": value, "desc": desc})
                )
            else:
                self.list_widget.addItem(
                    self._item("Add a keyword",
                               "add <keyword> <url, path, or app> | description",
                               {"kind": "hint"})
                )
            self._after_list()
            return

        # edit command: loads the stored entry into the box for editing
        if re.match(r"^edit\b", raw, re.IGNORECASE):
            parts = raw.split(None, 1)
            key = norm_key(parts[1]) if len(parts) > 1 else ""
            if key and key in maps:
                self.list_widget.addItem(
                    self._item(f"Edit {display_key(key)}",
                               get_desc(maps[key]).strip() or "Enter to load editor",
                               {"kind": "edit_load", "key": key})
                )
            elif key:
                self.list_widget.addItem(
                    self._item(f"No keyword named '{key}'", "",
                               {"kind": "hint"})
                )
            else:
                self.list_widget.addItem(
                    self._item("Edit a keyword",
                               "edit <keyword> loads it back into the box",
                               {"kind": "hint"})
                )
            self._after_list()
            return

        # desc command: set/replace a description without touching the target
        if re.match(r"^desc\b", raw, re.IGNORECASE):
            key, new_desc, parsed = parse_desc_command(raw)
            if parsed and key and new_desc:
                if key in maps:
                    self.list_widget.addItem(
                        self._item(f"Describe {display_key(key)}", new_desc[:70],
                                   {"kind": "desc_save", "key": key,
                                    "desc": new_desc})
                    )
                else:
                    self.list_widget.addItem(
                        self._item(f"No keyword named '{key}'", "",
                                   {"kind": "hint"})
                    )
            else:
                self.list_widget.addItem(
                    self._item("Describe a keyword",
                               "desc <keyword> <text>",
                               {"kind": "hint"})
                )
            self._after_list()
            return

        # delete command
        if re.match(r"^(del|delete|rm|remove)\b", raw, re.IGNORECASE):
            key, parsed = parse_del_command(raw)
            key = norm_key(key)
            if parsed and key:
                if key in maps:
                    self.list_widget.addItem(
                        self._item(f"Delete {display_key(key)}",
                                   get_desc(maps[key]).strip() or "Enter to confirm",
                                   {"kind": "delete", "key": key})
                    )
                else:
                    self.list_widget.addItem(
                        self._item(f"No keyword named '{key}'", "",
                                   {"kind": "hint"})
                    )
            else:
                self.list_widget.addItem(
                    self._item("Delete a keyword", "del <keyword>", {"kind": "hint"})
                )
            self._after_list()
            return

        # empty query: stored keywords, if any, plus the built-ins —
        # Manage Keys and Prettycode — always visible.
        if not raw:
            for k in sorted(maps, key=nat_key)[:MAX_SUGGESTIONS]:
                self.list_widget.addItem(self._key_row(k))
            if self.list_widget.count() < MAX_SUGGESTIONS:
                self.list_widget.addItem(
                    self._item("Manage Keys", "Add, edit, delete keywords",
                               {"kind": "manage"}))
            if self.list_widget.count() < MAX_SUGGESTIONS:
                self.list_widget.addItem(
                    self._item("Prettycode", "Paste code, get it prettified",
                               {"kind": "launch", "key": "prettycode"}))
            self._after_list()
            return

        # filtered matches: search keys AND descriptions (logic stays lowercase)
        for k in sorted(
            (k for k in self.keywords
             if low in k or low in get_desc(maps.get(k, {})).lower()),
            key=nat_key,
        )[:MAX_SUGGESTIONS]:
            if k in maps:
                self.list_widget.addItem(self._key_row(k))

        # prettycode discoverability
        if "pretty" in low and self.list_widget.count() < MAX_SUGGESTIONS:
            if not any((i.data(Qt.ItemDataRole.UserRole) or {}).get("key") == "prettycode"
                       for i in (self.list_widget.item(r) for r in range(self.list_widget.count()))):
                self.list_widget.addItem(
                    self._item("Prettycode", "Paste code, get it prettified",
                               {"kind": "launch", "key": "prettycode"})
                )

        # quit/exit (the popup has no chrome: this is the only clean way out,
        # unless shadowed by a user mapping of the same name)
        if low in ("quit", "exit") and "quit" not in maps and "exit" not in maps:
            self.list_widget.insertItem(
                0, self._item("Quit", "Close the launcher",
                              {"kind": "quit"})
            )

        # inline math answer (no separate calc window)
        ok, result = try_eval_math(raw)
        if ok and self.list_widget.count() < MAX_SUGGESTIONS + 1:
            self.list_widget.insertItem(
                0, self._item(f"= {result}", "Enter to copy",
                              {"kind": "calc_copy", "value": result})
            )

        # no-match fallback guides the user to the in-popup add flow
        if self.list_widget.count() == 0:
            self.list_widget.addItem(
                self._item(f"No match for '{raw}'",
                           f"type:  add {raw.lower()} <url, path, or app>",
                           {"kind": "hint"})
            )

        self._after_list()

    def _after_list(self):
        self._render_rows()
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
            self.list_widget.scrollToTop()
        self.resize_to_fit()

    def resize_to_fit(self):
        # Stable popup: fixed width; list shows at most VISIBLE_ROWS rows
        # and scrolls beyond that. Never shrinks below the minimum height.
        count = self.list_widget.count()
        visible = min(max(count, 1), VISIBLE_ROWS)
        list_h = visible * (ITEM_H + 2) + 14
        total = 14 + INPUT_HEIGHT + 10 + list_h + 14
        total = max(WINDOW_MIN_HEIGHT, min(WINDOW_MAX_HEIGHT, total))
        self.setFixedSize(WINDOW_WIDTH, total)
        self.list_widget.setFixedHeight(list_h)

    # -- events ---------------------------------------------------------
    def on_text_changed(self):
        self.show_suggestions(self.entry.text())

    def _selected_payload(self):
        row = self.list_widget.currentRow()
        if 0 <= row < self.list_widget.count():
            item = self.list_widget.item(row)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if isinstance(data, dict) and data.get("kind"):
                return data
        return {}

    def on_enter(self):
        raw = self.entry.text().strip()
        # exact quit/exit always wins (a hint row must never swallow it)
        if norm_key(raw) in ("quit", "exit"):
            maps = self.config.get("mappings", {})
            if norm_key(raw) not in maps:
                self.quit_app()
                return
        payload = self._selected_payload()
        if payload:
            self.dispatch(payload, raw)
            return
        # fallback: treat raw text as keyword / math
        if not raw:
            return
        ok, result = try_eval_math(raw)
        if ok:
            self.copy_to_clipboard(result)
            self.hide_popup()
            return
        self.dispatch({"kind": "launch", "key": raw}, raw)

    def on_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if not isinstance(data, dict) or not data.get("kind"):
            return
        # In the manager view, clicking a row loads it for editing
        # instead of launching it.
        if getattr(self, "manage_mode", False) and data.get("kind") == "launch":
            if data.get("key", "") in self.config.get("mappings", {}):
                self.load_edit_form(data.get("key", ""))
            return
        self.dispatch(data, self.entry.text().strip())

    def dispatch(self, payload: dict, raw: str):
        kind = payload.get("kind")
        if kind == "calc_copy":
            self.copy_to_clipboard(payload.get("value", ""))
            self.hide_popup()
        elif kind == "save":
            self.save_mapping(payload.get("key", ""),
                              payload.get("value", ""),
                              payload.get("desc", ""))
        elif kind == "delete":
            self.delete_mapping(payload.get("key", ""))
        elif kind == "edit_load":
            self.load_edit_form(payload.get("key", ""))
        elif kind == "desc_save":
            self.set_description(payload.get("key", ""),
                                 payload.get("desc", ""))
        elif kind == "manage":
            self.entry.setText("keys")
        elif kind == "quit":
            self.quit_app()
        elif kind == "launch":
            self.execute_keyword(payload.get("key", raw))
        elif kind == "hint":
            pass  # hints are informational; keep popup open

    # -- actions ----------------------------------------------------------
    def save_mapping(self, key: str, target: str, desc: str = ""):
        key, target = norm_key(key), (target or "").strip()
        if not key or not target:
            return
        maps = self.config.setdefault("mappings", {})
        old_desc = get_desc(maps.get(key, {})).strip()
        maps[key] = {"target": target, "description": desc.strip() or old_desc}
        self.persist()
        self.entry.clear()
        self.entry.setPlaceholderText(f"Saved {display_key(key)}")
        self.show_suggestions("keys")

    def delete_mapping(self, key: str):
        key = norm_key(key)
        maps = self.config.get("mappings", {})
        if key in maps:
            del maps[key]
            self.persist()
        self.entry.clear()
        self.entry.setPlaceholderText(f"Deleted {display_key(key)}")
        self.show_suggestions("keys")

    def set_description(self, key: str, desc: str):
        key = norm_key(key)
        maps = self.config.get("mappings", {})
        if key not in maps or not (desc or "").strip():
            return
        maps[key]["description"] = desc.strip()
        self.persist()
        self.entry.clear()
        self.entry.setPlaceholderText(f"Described {display_key(key)}")
        self.show_suggestions("keys")

    def load_edit_form(self, key: str):
        """Put `add <key> <target> | <desc>` back in the box for editing."""
        key = norm_key(key)
        entry = self.config.get("mappings", {}).get(key, {})
        target, desc = get_target(entry), get_desc(entry)
        form = f"add {key} {target}"
        if desc.strip():
            form += f" | {desc}"
        self.entry.setText(form)
        self.entry.setFocus()

    def copy_to_clipboard(self, text: str):
        QApplication.clipboard().setText(text)

    def quit_app(self):
        self.hide_popup()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def execute_keyword(self, keyword: str):
        keyword = norm_key(keyword)
        if not keyword:
            return
        # math typed + Enter without selecting the answer row still copies it
        ok, result = try_eval_math(keyword)
        maps = self.config.get("mappings", {})
        if ok and keyword not in maps and keyword != "prettycode":
            self.copy_to_clipboard(result)
            self.hide_popup()
            return
        if keyword == "prettycode":
            self.hide_popup()
            self.show_prettycode()
            return
        if keyword in maps:
            target = get_target(maps[keyword]).strip()
            if not target:
                self.entry.setText(f"add {keyword} ")
                self.entry.setPlaceholderText(
                    f"'{display_key(keyword)}' has no target yet — type it after the space")
                return
            self.hide_popup()
            self.open_target(target)
            self.entry.clear()
            return
        # unknown keyword: stay open and guide toward in-popup add flow
        self.entry.setPlaceholderText(f"No match — type: add {keyword} <url, path, or app>")
        self.show_suggestions(keyword)

    def open_target(self, value: str):
        try:
            kind, v = classify_target(value)
            if kind == "url":
                webbrowser.open(v)
            elif kind == "path":
                if os.name == "nt":
                    os.startfile(v)  # noqa: PGH121
                else:
                    subprocess.Popen(["xdg-open", v])
            elif kind == "scheme":
                if os.name == "nt":
                    os.startfile(v)  # noqa: PGH121
                else:
                    subprocess.Popen(["xdg-open", v])
            elif kind == "app":
                # Bare app name: `start` resolves App Paths registry
                # (chrome, msedge, ...) + PATH (notepad, ...).
                if os.name == "nt":
                    subprocess.Popen(["cmd", "/c", "start", "", v])
                else:
                    subprocess.Popen(v, shell=True)
            elif kind == "empty":
                raise ValueError("empty target")
            else:  # shell: exe paths with args, `explorer ...`, UNC, ...
                subprocess.Popen(v, shell=True)
        except Exception as exc:
            self.show_popup()
            self.entry.setPlaceholderText(f"Couldn't open: {exc}"[:90])

    # -- prettycode ---------------------------------------------------------
    def show_prettycode(self):
        dlg = QDialog(None)
        dlg.setWindowTitle("PrettyCode")
        dlg.setModal(True)
        dlg.setMinimumSize(760, 520)
        dlg.setStyleSheet(
            "QDialog { background: #1e1e2f; border: 1px solid #34344a; border-radius: 18px; }"
            "QTextEdit { background: #14141f; color: #d7d7e5; border: 1px solid #34344a;"
            " border-radius: 12px; padding: 10px; font-family: 'JetBrains Mono', Consolas, monospace;"
            " font-size: 13px; selection-background-color: #3a70b8; }"
            "QPushButton { background: #3a70b8; color: white; border: none; border-radius: 10px;"
            " padding: 9px 18px; font-weight: 600; font-size: 13px; }"
            "QPushButton:hover { background: #2c5a98; }"
            "QLabel { color: #8b949e; font-size: 12px; font-weight: 600; }"
        )
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        lang_box = QComboBox()
        lang_box.addItems(["Auto", "JSON", "CSV", "Python", "Java", "YAML", "Text"])
        lang_box.setStyleSheet(
            "QComboBox { background: #2d2d44; color: #e0e0e0; border: none;"
            " border-radius: 10px; padding: 7px 12px; font-size: 13px; }"
        )
        detected = QLabel("detected: —")
        detected.setStyleSheet("color: #5b5b73; font-weight: 400;")
        top.addWidget(QLabel("Language:"))
        top.addWidget(lang_box)
        top.addWidget(detected, 1)
        layout.addLayout(top)

        panes = QHBoxLayout()
        panes.setSpacing(12)

        left = QVBoxLayout()
        left.addWidget(QLabel("RAW — paste code here"))
        raw = QTextEdit()
        raw.setPlaceholderText("Paste raw code… (JSON auto-formats)")
        left.addWidget(raw, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("PRETTIFIED — ready to copy"))
        pretty = QTextEdit()
        pretty.setReadOnly(True)
        pretty.setPlaceholderText("Prettified output appears as you type…")
        right.addWidget(pretty, 1)

        panes.addLayout(left, 1)
        panes.addLayout(right, 1)
        layout.addLayout(panes, 1)

        btns = QHBoxLayout()
        status = QLabel("Live prettify on • Esc closes")
        status.setStyleSheet("color: #5b5b73; font-weight: 400;")
        copy_btn = QPushButton("Copy output")
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            "QPushButton { background: #2d2d44; } QPushButton:hover { background: #3a3a56; }"
        )
        btns.addWidget(status, 1)
        btns.addWidget(copy_btn)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        def do_prettify():
            lang = lang_box.currentText().lower()
            if lang == "auto":
                detected.setText(f"detected: {detect_language(raw.toPlainText())}")
            else:
                detected.setText(f"detected: {lang} (manual)")
            pretty.setPlainText(prettify_code(raw.toPlainText(), lang))

        def do_copy():
            QApplication.clipboard().setText(pretty.toPlainText())

        raw.textChanged.connect(do_prettify)
        lang_box.currentIndexChanged.connect(lambda *_: do_prettify())
        copy_btn.clicked.connect(do_copy)
        close_btn.clicked.connect(dlg.accept)
        raw.setPlainText("")
        dlg.exec()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        # Up/Down moves through the results while the search box keeps focus,
        # so Enter always runs the highlighted row.
        if obj is self.entry and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                count = self.list_widget.count()
                if count:
                    row = self.list_widget.currentRow()
                    step = -1 if event.key() == Qt.Key.Key_Up else 1
                    self.list_widget.setCurrentRow((row + step) % count)
                    self.list_widget.scrollToItem(
                        self.list_widget.currentItem())
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if event.key() == Qt.Key.Key_Escape:
            self.hide_popup()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------- prettify ---

LANGS = ("auto", "json", "csv", "python", "java", "yaml", "text")


def detect_language(code: str) -> str:
    t = (code or "").strip()
    if not t:
        return "text"
    if t[0] in "{[":
        try:
            json.loads(t)
            return "json"
        except (json.JSONDecodeError, ValueError):
            return "java" if (";" in t or "}" in t) else "text"
    lines = [ln for ln in t.split("\n") if ln.strip()]
    if len(lines) >= 2 and _csv_delimiter(lines):
        return "csv"
    if re.search(r"^(def |class |import |from |if __name__)", t, re.M):
        return "python"
    if re.search(r"\b(public|private|class|void|int |String |package |import java)", t):
        return "java"
    if re.search(r"^[A-Za-z0-9_.\-\"']+\s*:\s*\S?", t, re.M) and "{" not in t and ";" not in t:
        return "yaml"
    return "text"


def _csv_delimiter(lines) -> str:
    import csv as _csv

    sample = "\n".join(lines[:8])
    for d in (",", ";", "\t", "|"):
        try:
            rows = list(_csv.reader(sample.split("\n"), delimiter=d))
        except Exception:
            continue
        widths = [len(r) for r in rows if r]
        if len(widths) >= 2 and min(widths) >= 2 and max(widths) == min(widths):
            return d
    return ""


def prettify_json(text: str) -> str:
    return json.dumps(json.loads(text), indent=2, ensure_ascii=False)


def prettify_csv(text: str) -> str:
    import csv as _csv

    lines = [ln for ln in text.split("\n") if ln.strip()]
    delim = _csv_delimiter(lines) or ","
    rows = list(_csv.reader(lines, delimiter=delim))
    rows = [[c.strip() for c in r] for r in rows]
    width = [0] * max(len(r) for r in rows)
    for r in rows:
        for i, c in enumerate(r):
            width[i] = max(width[i], len(c))
    sep = f" {delim} " if delim.strip() else "  "
    out = []
    for r in rows:
        cells = [c.ljust(width[i]) for i, c in enumerate(r)]
        out.append(sep.join(cells).rstrip())
    return "\n".join(out) + "\n"


def prettify_python(text: str) -> str:
    text = text.replace("\t", "    ").replace("\r\n", "\n")
    lines = [ln.rstrip() for ln in text.strip("\n").split("\n")]
    # drop trailing blank-only lines inside, collapse 3+ blanks
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (cleaned + "\n") if cleaned else ""


def prettify_brace(text: str) -> str:
    """Simple brace-aware reindent for Java / C-like code (4 spaces)."""
    text = text.replace("\t", "    ").replace("\r\n", "\n")
    out, depth, buf = [], 0, ""
    i, n = 0, len(text)
    in_str, str_ch, in_lc, in_bc = False, "", False, False

    def flush():
        nonlocal buf
        s = buf.strip()
        buf = ""
        if s:
            out.append("    " * depth + s)

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_lc:
            buf += c
            if c == "\n":
                in_lc = False
                flush()
        elif in_bc:
            buf += c
            if c == "*" and nxt == "/":
                buf += nxt
                i += 1
                in_bc = False
        elif in_str:
            buf += c
            if c == "\\" and nxt:
                buf += nxt
                i += 1
            elif c == str_ch:
                in_str = False
        else:
            if c == "/" and nxt == "/":
                in_lc = True
                buf += c
            elif c == "/" and nxt == "*":
                in_bc = True
                buf += c
            elif c in ("'", '"'):
                in_str, str_ch = True, c
                buf += c
            elif c == "{":
                buf += c
                flush()
                depth += 1
            elif c == "}":
                flush()
                depth = max(0, depth - 1)
                buf = c
            elif c == ";":
                buf += c
                flush()
            elif c == "\n":
                flush()
            else:
                buf += c
        i += 1
    flush()
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return (cleaned + "\n") if cleaned else ""


def prettify_yaml(text: str) -> str:
    text = text.replace("\t", "  ").replace("\r\n", "\n")
    lines = [ln.rstrip() for ln in text.strip("\n").split("\n")]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (cleaned + "\n") if cleaned else ""


def prettify_generic(text: str) -> str:
    text = (text or "").replace("\t", "    ").replace("\r\n", "\n")
    lines = [ln.rstrip() for ln in text.strip("\n").split("\n")]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (cleaned + "\n") if cleaned else ""


def prettify_code(code: str, lang: str = "auto") -> str:
    text = (code or "").replace("\r\n", "\n")
    if not text.strip():
        return ""
    lang = (lang or "auto").lower()
    if lang == "auto":
        lang = detect_language(text)
    try:
        if lang == "json":
            return prettify_json(text.strip())
        if lang == "csv":
            return prettify_csv(text.strip())
        if lang == "python":
            return prettify_python(text)
        if lang == "java":
            return prettify_brace(text)
        if lang == "yaml":
            return prettify_yaml(text)
    except Exception:
        pass
    if lang == "auto" and text.strip()[:1] in "{[":
        try:
            return prettify_json(text.strip())
        except Exception:
            pass
    return prettify_generic(text)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("fusion")
    app.setQuitOnLastWindowClosed(False)
    launcher = ModernLauncher()
    sys.exit(app.exec())
