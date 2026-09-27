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

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QApplication,
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
ROW_HEIGHT = 46
WINDOW_MIN_HEIGHT = 150
WINDOW_MAX_HEIGHT = 480
MAX_SUGGESTIONS = 6


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


def load_config() -> dict:
    path = get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = _merge_sections(data)
                save_config(data)  # persist normalized form + backfilled descriptions
                return data
        except (OSError, json.JSONDecodeError):
            pass
    # migrate from legacy local config.json if present
    legacy = _legacy_config_path()
    if os.path.exists(legacy) and os.path.abspath(legacy) != os.path.abspath(path):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = _merge_sections(data)
                save_config(data)
                return data
        except (OSError, json.JSONDecodeError):
            pass
    cfg = {"mappings": normalize_mappings(DEFAULT_CONFIG["mappings"])}
    save_config(cfg)
    return cfg


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
                padding: 10px 12px;
                margin: 2px;
                border-radius: 9px;
            }
            QListWidget#Results::item:selected {
                background: #3a70b8;
                color: #ffffff;
            }
            QListWidget#Results::item:hover:!selected {
                background: #353548;
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
        self.entry.setPlaceholderText("Type keyword, math, or `add key value`…")
        self.entry.setFixedHeight(INPUT_HEIGHT)
        self.entry.setClearButtonEnabled(True)
        self.entry.textChanged.connect(self.on_text_changed)
        self.entry.returnPressed.connect(self.on_enter)
        layout.addWidget(self.entry)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("Results")
        self.list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_widget.setUniformItemSizes(False)
        self.list_widget.setSpacing(2)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.list_widget.itemActivated.connect(self.on_item_clicked)
        layout.addWidget(self.list_widget)

        self.setFixedWidth(WINDOW_WIDTH)

    # -- config -----------------------------------------------------
    def refresh_keywords(self):
        self.keywords = sorted(self.config.get("mappings", {}).keys(), key=str.lower)

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
        size = self.sizeHint()
        x = geo.x() + (geo.width() - WINDOW_WIDTH) // 2
        y = geo.y() + (geo.height() - self.height()) // 2
        self.move(x, max(geo.y() + 40, y))
        _ = size

    # -- suggestions --------------------------------------------------
    def _item(self, text: str, sub: str = "", payload: dict | None = None):
        label = text if not sub else f"{text}  —  {sub}"
        item = QListWidgetItem(label)
        item.setToolTip(label)
        item.setData(Qt.ItemDataRole.UserRole, payload or {})
        return item

    def _key_row(self, key: str):
        """One row for a stored keyword: Title Case + description (never the raw value)."""
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
                for k in sorted(maps, key=str.lower)[:MAX_SUGGESTIONS]:
                    self.list_widget.addItem(self._key_row(k))
                if len(maps) > MAX_SUGGESTIONS:
                    self.list_widget.addItem(
                        self._item(f"+{len(maps) - MAX_SUGGESTIONS} more…",
                                   "keep typing to filter, click a row to edit",
                                   {"kind": "hint"})
                    )
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
                    self._item("Usage  —  add <keyword> <url-or-path> | description",
                               "example: add docs D:\\docs | Project docs",
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
                    self._item("Usage  —  edit <keyword>",
                               "loads it back into the box for editing",
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
                    self._item("Usage  —  desc <keyword> <text>",
                               "example: desc docs Project documentation",
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
                    self._item("Usage  —  del <keyword>", "", {"kind": "hint"})
                )
            self._after_list()
            return

        # empty query: top keywords + discoverable helpers
        if not raw:
            for k in sorted(maps, key=str.lower)[:MAX_SUGGESTIONS]:
                self.list_widget.addItem(self._key_row(k))
            # pad so the popup never collapses to nothing
            helpers = [
                ("Prettycode", "Paste code, get it prettified",
                 {"kind": "launch", "key": "prettycode"}),
                ("Keys", "Manage all keywords",
                 {"kind": "manage"}),
            ]
            for t, s, p in helpers:
                if self.list_widget.count() < MAX_SUGGESTIONS:
                    self.list_widget.addItem(self._item(t, s, p))
            self._after_list()
            return

        # filtered keyword matches (logic stays lowercase)
        for k in [k for k in self.keywords if low in k][:MAX_SUGGESTIONS]:
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
                           f"type:  add {raw.lower()} <url-or-path>",
                           {"kind": "hint"})
            )

        self._after_list()

    def _after_list(self):
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        self.resize_to_fit()

    def resize_to_fit(self):
        # Stable popup: fixed width, height derived from rows with sane
        # minimum so it never shrinks "beyond the height of the text".
        count = self.list_widget.count()
        rows = min(max(count, 1), MAX_SUGGESTIONS)
        # measure one row; fall back to ROW_HEIGHT constant
        row_h = ROW_HEIGHT
        try:
            if count:
                h = self.list_widget.sizeHintForIndex(
                    self.list_widget.model().index(0, 0)).height()
                if h and 20 <= h <= 80:
                    row_h = h
        except Exception:
            pass
        list_h = rows * (row_h + 4) + 16
        total = 14 + INPUT_HEIGHT + 10 + list_h + 14
        total = max(WINDOW_MIN_HEIGHT, min(WINDOW_MAX_HEIGHT, total))
        self.setFixedSize(WINDOW_WIDTH, total)

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
        self.entry.setPlaceholderText(f"No match — type: add {keyword} <url-or-path>")
        self.show_suggestions(keyword)

    def open_target(self, value: str):
        try:
            v = value.strip().strip("'\"")
            if v.startswith(("http://", "https://")):
                webbrowser.open(v)
                return
            if os.path.exists(v):
                if os.name == "nt":
                    os.startfile(v)  # noqa: PGH121
                else:
                    subprocess.Popen(["xdg-open", v])
                return
            # allow `explorer D:\dir` style or bare exe names on PATH
            try:
                if os.name == "nt":
                    os.startfile(v)  # noqa: PGH121
                else:
                    subprocess.Popen(v, shell=True)
            except Exception:
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

        from PyQt6.QtWidgets import QComboBox

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
