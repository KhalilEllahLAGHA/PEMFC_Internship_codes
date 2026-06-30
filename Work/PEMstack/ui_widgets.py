#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_widgets.py
=============
Reusable widgets for the redesigned PEMstack interface: sensor cards,
notification banners, the experiment-state badge and a self-refreshing
serial-port selector. Pure PyQt5, no new dependencies.
"""

from time import monotonic
from typing import Callable, List, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

# Semantic state colours, shared across cards / badges / banners.
# Accent palette is limited to blue (primary), green (ok) and red (danger);
# amber is reserved strictly for WARNING states.
COLOUR_OK = "#27ae60"
COLOUR_WARN = "#f2c94c"
COLOUR_DANGER = "#eb5757"
COLOUR_PRIMARY = "#2f80ed"
COLOUR_MUTED = "#9aa0a6"

BANNER_STYLES = {
    "error":   ("#fdecea", "#b3261e", COLOUR_DANGER),
    "warning": ("#fff8e1", "#7a5c00", COLOUR_WARN),
    "info":    ("#e8f0fe", "#174ea6", COLOUR_PRIMARY),
    "success": ("#e6f4ea", "#137333", COLOUR_OK),
}

BANNER_AUTODISMISS_MS = 10_000
STALE_AFTER_S = 2.0
# A value within this fraction of the threshold span from a limit -> WARNING
WARN_MARGIN_FRACTION = 0.10


def _repolish(widget: QtWidgets.QWidget) -> None:
    """Force a stylesheet re-evaluation after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ===========================================================================
# Experiment state badge (status bar, left)
# ===========================================================================
class StateBadge(QtWidgets.QLabel):
    """Coloured experiment-state badge: IDLE / RUNNING / STOPPED / SAVING."""

    _STYLES = {
        "IDLE":    (COLOUR_MUTED, COLOUR_MUTED),
        "RUNNING": (COLOUR_OK, "#6fcf97"),       # two greens -> pulsing
        "STOPPED": (COLOUR_DANGER, COLOUR_DANGER),
        "SAVING":  (COLOUR_PRIMARY, COLOUR_PRIMARY),
    }

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("stateBadge")
        self._state = "IDLE"
        self._pulse_on = False
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse)
        self.set_state("IDLE")

    def set_state(self, state: str) -> None:
        self._state = state if state in self._STYLES else "IDLE"
        self.setText(f" {self._state} ")
        if self._state == "RUNNING":
            self._pulse_timer.start(600)
        else:
            self._pulse_timer.stop()
        self._apply(self._STYLES[self._state][0])

    def _pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        self._apply(self._STYLES["RUNNING"][1 if self._pulse_on else 0])

    def _apply(self, colour: str) -> None:
        self.setStyleSheet(
            f"QLabel#stateBadge {{ background-color: {colour}; color: white;"
            f" border-radius: 4px; padding: 2px 8px; font-weight: bold; }}")


# ===========================================================================
# Notification banners (Task 7)
# ===========================================================================
class Banner(QtWidgets.QFrame):
    """One dismissible notification banner with optional action button."""

    closed = pyqtSignal(str)        # emits the banner id

    def __init__(self, banner_id: str, kind: str, message: str,
                 action_text: Optional[str] = None,
                 action_callback: Optional[Callable[[], None]] = None,
                 persistent: bool = False,
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.banner_id = banner_id
        self.kind = kind if kind in BANNER_STYLES else "info"
        self.persistent = persistent

        background, foreground, border = BANNER_STYLES[self.kind]
        self.setStyleSheet(
            f"QFrame {{ background-color: {background}; color: {foreground};"
            f" border: 1px solid {border}; border-radius: 4px; }}"
            f"QLabel {{ border: none; background: transparent; }}"
            f"QPushButton {{ border: none; background: transparent;"
            f" color: {foreground}; font-weight: bold; }}")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 6, 4)
        self._label = QtWidgets.QLabel(message)
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)

        if action_text and action_callback:
            action_btn = QtWidgets.QPushButton(action_text)
            action_btn.setCursor(Qt.PointingHandCursor)
            action_btn.setStyleSheet(
                f"QPushButton {{ border: 1px solid {border}; border-radius: 3px;"
                f" padding: 2px 10px; background: transparent;"
                f" color: {foreground}; font-weight: bold; }}")
            action_btn.clicked.connect(action_callback)
            layout.addWidget(action_btn)

        close_btn = QtWidgets.QPushButton("✕")     # ✕
        close_btn.setFixedWidth(24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Dismiss this message")
        close_btn.clicked.connect(self.dismiss)
        layout.addWidget(close_btn)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self.dismiss)
        if not persistent:
            self._auto_timer.start(BANNER_AUTODISMISS_MS)

    def set_message(self, message: str) -> None:
        self._label.setText(message)

    def set_persistent(self, persistent: bool) -> None:
        """A persistent banner stays until the problem clears."""
        self.persistent = persistent
        if persistent:
            self._auto_timer.stop()
        elif not self._auto_timer.isActive():
            self._auto_timer.start(BANNER_AUTODISMISS_MS)

    def dismiss(self) -> None:
        self.closed.emit(self.banner_id)
        self.hide()
        self.deleteLater()


class BannerStack(QtWidgets.QWidget):
    """Vertical stack of banners pinned to the top of the main window."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._banners: dict = {}

    def show_banner(self, banner_id: str, kind: str, message: str,
                    action_text: Optional[str] = None,
                    action_callback: Optional[Callable[[], None]] = None,
                    persistent: bool = False) -> Banner:
        """Create a banner, or update the message of an existing one."""
        banner = self._banners.get(banner_id)
        if banner is not None:
            banner.set_message(message)
            banner.set_persistent(persistent)
            return banner
        banner = Banner(banner_id, kind, message, action_text,
                        action_callback, persistent, self)
        banner.closed.connect(self._on_closed)
        self._banners[banner_id] = banner
        self._layout.addWidget(banner)
        return banner

    def dismiss(self, banner_id: str) -> None:
        banner = self._banners.get(banner_id)
        if banner is not None:
            banner.dismiss()

    def has_banner(self, banner_id: str) -> bool:
        return banner_id in self._banners

    def _on_closed(self, banner_id: str) -> None:
        self._banners.pop(banner_id, None)


# ===========================================================================
# Serial-port selector that refreshes its list when opened
# ===========================================================================
class PortComboBox(QtWidgets.QComboBox):
    """Port dropdown that re-scans available serial ports on every click."""

    def __init__(self, refresh_callback: Callable[[], List[str]],
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._refresh_callback = refresh_callback
        self.setToolTip("Serial port of the acquisition board "
                        "(list refreshes when opened)")

    def showPopup(self) -> None:  # noqa: N802 (Qt naming)
        current = self.currentText()
        ports = self._refresh_callback()
        self.blockSignals(True)
        self.clear()
        self.addItems(ports)
        index = self.findText(current)
        if index >= 0:
            self.setCurrentIndex(index)
        self.blockSignals(False)
        super().showPopup()


# ===========================================================================
# Sensor card (Task 5)
# ===========================================================================
class SensorCard(QtWidgets.QFrame):
    """Live readout card for one sensor: value, unit, min/max, state badge."""

    selected = pyqtSignal(str)              # card clicked -> highlight trace
    disable_requested = pyqtSignal(str, bool)   # (key, enable)

    def __init__(self, key: str, name: str, unit: str,
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self.sensor_name = name
        self.unit = unit

        self.setObjectName("sensorCard")
        self.setProperty("selected", False)
        self.setProperty("sensorOff", False)
        self.setCursor(Qt.PointingHandCursor)

        self._value: Optional[float] = None
        self._minimum: Optional[float] = None
        self._maximum: Optional[float] = None
        self._thresholds: Optional[Tuple[float, float]] = None
        self._last_update: Optional[float] = None
        self._stale = True
        self._show_minmax = True
        self._sensor_enabled = True

        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(10, 6, 10, 6)
        grid.setVerticalSpacing(2)

        self._dot = QtWidgets.QLabel("●")          # ●
        self._dot.setObjectName("cardDot")
        self._name_label = QtWidgets.QLabel(name)
        self._name_label.setObjectName("cardName")
        self._badge = QtWidgets.QLabel("[NOMINAL]")
        self._badge.setObjectName("cardBadge")
        self._badge.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self._dot)
        header.addWidget(self._name_label, 1)
        header.addWidget(self._badge)
        grid.addLayout(header, 0, 0)

        self._value_label = QtWidgets.QLabel("--- (no data)")
        self._value_label.setObjectName("cardValue")
        self._value_label.setAlignment(Qt.AlignCenter)
        grid.addWidget(self._value_label, 1, 0)

        self._minmax_label = QtWidgets.QLabel("Min ---   Max ---")
        self._minmax_label.setObjectName("cardMinMax")
        self._minmax_label.setAlignment(Qt.AlignCenter)
        grid.addWidget(self._minmax_label, 2, 0)

        self._render()

    # ------------------------------------------------------------------ #
    # data updates                                                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt(value: float) -> str:
        if abs(value) >= 100:
            return f"{value:.1f}"
        if abs(value) >= 10:
            return f"{value:.2f}"
        return f"{value:.3f}"

    def update_value(self, value: float) -> None:
        self._value = value
        self._last_update = monotonic()
        self._stale = False
        if self._minimum is None or value < self._minimum:
            self._minimum = value
        if self._maximum is None or value > self._maximum:
            self._maximum = value
        self._render()

    def check_stale(self) -> None:
        """Grey the value out if nothing arrived in the last 2 seconds."""
        if (not self._stale and self._last_update is not None
                and monotonic() - self._last_update > STALE_AFTER_S):
            self._stale = True
            self._render()

    def set_thresholds(self, thresholds: Optional[Tuple[float, float]]) -> None:
        self._thresholds = thresholds
        self._render()

    def set_show_minmax(self, show: bool) -> None:
        self._show_minmax = show
        self._minmax_label.setVisible(show)

    def set_sensor_enabled(self, enabled: bool) -> None:
        """Grey the whole card when the sensor is disabled in Settings."""
        self._sensor_enabled = enabled
        self.setProperty("sensorOff", not enabled)
        _repolish(self)
        self._render()

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        _repolish(self)

    def reset_minmax(self) -> None:
        self._minimum = self._value
        self._maximum = self._value
        self._render()

    def reset(self) -> None:
        """Full reset to the no-data state (used by New Experiment)."""
        self._value = None
        self._minimum = None
        self._maximum = None
        self._last_update = None
        self._stale = True
        self._render()

    def current_value(self) -> Optional[float]:
        return self._value

    # ------------------------------------------------------------------ #
    # rendering                                                           #
    # ------------------------------------------------------------------ #
    def _status(self) -> Tuple[str, str]:
        """Return (badge text, colour) from the thresholds and value."""
        if not self._sensor_enabled:
            return "[DISABLED]", COLOUR_MUTED
        if self._stale or self._value is None or self._thresholds is None:
            return "[NOMINAL]", COLOUR_OK if not self._stale else COLOUR_MUTED
        low, high = self._thresholds
        if self._value < low or self._value > high:
            return "[OUT OF RANGE]", COLOUR_DANGER
        margin = (high - low) * WARN_MARGIN_FRACTION
        if self._value < low + margin or self._value > high - margin:
            return "[WARNING]", COLOUR_WARN
        return "[NOMINAL]", COLOUR_OK

    def _render(self) -> None:
        badge_text, colour = self._status()
        self._dot.setStyleSheet(f"color: {colour}; border: none;")
        self._badge.setText(badge_text)
        self._badge.setStyleSheet(
            f"color: {colour}; font-weight: bold; border: none;")
        if self._stale or self._value is None:
            self._value_label.setText("--- (no data)")
            self._value_label.setStyleSheet(f"color: {COLOUR_MUTED};")
        else:
            self._value_label.setText(f"{self._fmt(self._value)}  {self.unit}")
            self._value_label.setStyleSheet("")
        if self._minimum is None or self._maximum is None:
            self._minmax_label.setText("Min ---   Max ---")
        else:
            self._minmax_label.setText(
                f"Min {self._fmt(self._minimum)}   "
                f"Max {self._fmt(self._maximum)}")

    # ------------------------------------------------------------------ #
    # interaction                                                         #
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.selected.emit(self.key)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        menu = QtWidgets.QMenu(self)
        toggle_text = ("Enable sensor" if not self._sensor_enabled
                       else "Disable sensor")
        toggle_action = menu.addAction(toggle_text)
        reset_action = menu.addAction("Reset min/max")
        copy_action = menu.addAction("Copy current value")
        copy_action.setEnabled(self._value is not None and not self._stale)
        chosen = menu.exec_(event.globalPos())
        if chosen == toggle_action:
            self.disable_requested.emit(self.key, not self._sensor_enabled)
        elif chosen == reset_action:
            self.reset_minmax()
        elif chosen == copy_action and self._value is not None:
            QtWidgets.QApplication.clipboard().setText(self._fmt(self._value))
