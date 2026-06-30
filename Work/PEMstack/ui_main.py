#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_main.py
==========
Redesigned PEMstack main window — a three-zone, lab-grade layout:

    ┌──────────────────────────────────────────────────────────┐
    │  TOOLBAR  (connection · experiment controls · save · ⚙)  │
    ├───────────────────┬──────────────────────────────────────┤
    │   LIVE PLOT AREA  │           SENSOR PANEL               │
    ├───────────────────┴──────────────────────────────────────┤
    │  STATUS BAR  (state · elapsed/samples · last save path)  │
    └──────────────────────────────────────────────────────────┘

The acquisition layer (main_gui0.SerialAcquisition / DataRecorder /
parse_payload) is reused unchanged; this module is presentation + control.
"""

import os
import queue
import datetime
from collections import deque
from time import monotonic
from typing import Dict, List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QTimer, QUrl, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QKeySequence

import pyqtgraph as pg
import serial
import serial.tools.list_ports
import threading
import dataclasses

import app_settings
import ui_widgets
import ui_settings
import main_gui0 as core

# ---------------------------------------------------------------------------
# Palette — dark chrome (toolbar/status bar), light content, 3 accents
# (primary blue, ok green, danger red; amber is a warning state colour only)
# ---------------------------------------------------------------------------
CHROME_BG = "#262b33"
CHROME_FG = "#e8eaed"
ACCENT_PRIMARY = ui_widgets.COLOUR_PRIMARY
ACCENT_OK = ui_widgets.COLOUR_OK
ACCENT_DANGER = ui_widgets.COLOUR_DANGER

# 17 visually distinct trace colours (one per SENSOR_DEFS entry)
TRACE_COLOURS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                 "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
                 "#3366cc", "#dc3912", "#ff9900", "#109618", "#990099",
                 "#0099c6", "#dd4477")

RECONNECT_INTERVAL_S = 3
DIMMED_ALPHA = 60               # alpha of non-highlighted traces
FADE_CLEAR_MS = 300


def build_stylesheet(settings: app_settings.AppSettings) -> str:
    """Build the application-wide QSS from the current display settings."""
    data_pt, heading_pt = settings.fonts()
    if settings.dark_mode:
        content_bg, card_bg = "#1d2025", "#262a31"
        text, border = "#e8eaed", "#3c4043"
    else:
        content_bg, card_bg = "#f4f5f7", "#ffffff"
        text, border = "#202124", "#dadce0"
    return f"""
QMainWindow, QDialog {{ background-color: {content_bg}; }}
QWidget {{ font-size: {data_pt}pt; color: {text}; }}
QToolBar {{ background-color: {CHROME_BG}; border: none;
            padding: 4px; spacing: 8px; }}
QToolBar QToolButton {{ color: {CHROME_FG}; padding: 4px 10px;
                        border-radius: 4px; background: transparent; }}
QToolBar QToolButton:hover {{ background-color: #3a414c; }}
QToolBar QToolButton:disabled {{ color: #6b7280; }}
QToolBar QComboBox {{ background-color: #3a414c; color: {CHROME_FG};
                      border-radius: 3px; padding: 2px 8px; }}
QToolBar QLabel {{ color: {CHROME_FG}; }}
QStatusBar {{ background-color: {CHROME_BG}; color: {CHROME_FG}; }}
QStatusBar QLabel, QStatusBar QToolButton {{ color: {CHROME_FG}; }}
QStatusBar QToolButton {{ border: none; }}
QStatusBar QToolButton:hover {{ text-decoration: underline; }}
QFrame#sensorCard {{ background-color: {card_bg};
                     border: 1px solid {border}; border-radius: 6px; }}
QFrame#sensorCard[selected="true"] {{ border: 2px solid {ACCENT_PRIMARY}; }}
QFrame#sensorCard[sensorOff="true"] {{ background-color: {content_bg}; }}
QLabel#cardName {{ font-size: {heading_pt}pt; font-weight: 600;
                   border: none; background: transparent; }}
QLabel#cardValue {{ font-size: {data_pt + 6}pt; font-weight: bold;
                    border: none; background: transparent; }}
QLabel#cardMinMax, QLabel#cardBadge, QLabel#cardDot {{
                    border: none; background: transparent; }}
QFrame#welcomeCard {{ background-color: {card_bg};
                      border: 2px solid {ACCENT_PRIMARY}; border-radius: 8px; }}
QLabel#welcomeTitle {{ font-size: {heading_pt + 2}pt; font-weight: bold; }}
QLabel#plotPlaceholder {{ color: #9aa0a6; font-size: {heading_pt}pt;
                          background: transparent; }}
QGroupBox {{ font-size: {heading_pt}pt; font-weight: 600; }}
QPushButton {{ padding: 4px 12px; }}
"""


# ===========================================================================
# Plot interaction: wheel = Y zoom, drag = X pan (Task 3)
# ===========================================================================
class PlotViewBox(pg.ViewBox):
    """ViewBox with Y-only wheel zoom and X-only left-drag pan."""

    user_interacted = pyqtSignal()

    def wheelEvent(self, ev, axis=None):
        scale = 1.001 ** ev.delta()
        self.scaleBy(y=scale)
        ev.accept()
        self.user_interacted.emit()

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() == Qt.LeftButton:
            ev.accept()
            shift = self.mapToView(ev.lastPos()) - self.mapToView(ev.pos())
            self.translateBy(x=shift.x())
            self.user_interacted.emit()
        else:
            super().mouseDragEvent(ev, axis=axis)


# ===========================================================================
# Live plot manager (Task 3)
# ===========================================================================
class PlotManager(QtCore.QObject):
    """Owns the multi-trace live plot: dual axes, legend, auto-scroll,
    start/stop markers and the animated clear."""

    def __init__(self, settings: app_settings.AppSettings,
                 parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.autoscroll = True
        self.time_window_s = settings.time_window_s

        self.view_box = PlotViewBox()
        self.widget = pg.PlotWidget(viewBox=self.view_box)
        self.plot_item = self.widget.getPlotItem()
        self.plot_item.showGrid(x=True, y=True, alpha=0.25)   # light gridlines
        self.plot_item.setLabel("left", "Cell voltage", units="mV")
        self.plot_item.setLabel("bottom", "Time", units="s")
        self.legend = self.plot_item.addLegend(offset=(10, 10))

        # second Y axis (right) for every non-voltage unit group
        self.right_vb = pg.ViewBox()
        self.plot_item.showAxis("right")
        self.plot_item.scene().addItem(self.right_vb)
        self.plot_item.getAxis("right").linkToView(self.right_vb)
        self.right_vb.setXLink(self.view_box)
        self.view_box.sigResized.connect(self._sync_right_geometry)

        self.curves: Dict[str, pg.PlotDataItem] = {}
        self._base_colours: Dict[str, str] = {}
        for index, sensor in enumerate(core.SENSOR_DEFS):
            colour = TRACE_COLOURS[index % len(TRACE_COLOURS)]
            curve = pg.PlotDataItem(
                pen=pg.mkPen(colour, width=1.5),
                name=f"{sensor.name} [{sensor.unit}]")
            curve.setDownsampling(auto=True)
            curve.setClipToView(True)
            if sensor.group == "voltage":
                self.plot_item.addItem(curve)        # left axis (mV)
            else:
                self.right_vb.addItem(curve)         # right axis
                self.legend.addItem(curve, f"{sensor.name} [{sensor.unit}]")
            self.curves[sensor.key] = curve
            self._base_colours[sensor.key] = colour

        # data buffers (bounded by the buffer-size setting)
        size = settings.buffer_size
        self.times: deque = deque(maxlen=size)
        self.values: Dict[str, deque] = {
            sensor.key: deque(maxlen=size) for sensor in core.SENSOR_DEFS}

        self._markers: List[pg.InfiniteLine] = []
        self._fade_anim: Optional[QVariantAnimation] = None
        self.set_dark(settings.dark_mode)

    # ------------------------------------------------------------------ #
    def _sync_right_geometry(self) -> None:
        self.right_vb.setGeometry(self.view_box.sceneBoundingRect())
        self.right_vb.linkedViewChanged(self.view_box, self.right_vb.XAxis)

    def set_dark(self, dark: bool) -> None:
        self.widget.setBackground("#1d2025" if dark else "w")
        axis_colour = "#e8eaed" if dark else "#202124"
        for name in ("left", "bottom", "right"):
            axis = self.plot_item.getAxis(name)
            axis.setPen(pg.mkPen(axis_colour))
            axis.setTextPen(pg.mkPen(axis_colour))
        self.legend.setLabelTextColor(axis_colour)
        # setLabelTextColor only stores the colour; the label HTML is rebuilt
        # on setText, so re-set each existing label to apply it
        for _sample, label in self.legend.items:
            label.setText(label.text)

    def set_buffer_size(self, size: int) -> None:
        """Resize buffers, keeping the most recent samples."""
        self.times = deque(self.times, maxlen=size)
        for key in self.values:
            self.values[key] = deque(self.values[key], maxlen=size)

    # ------------------------------------------------------------------ #
    def append(self, x: float, values: Dict[str, float]) -> None:
        self.times.append(x)
        for key, value in values.items():
            self.values[key].append(value)

    def has_data(self) -> bool:
        return len(self.times) > 0

    def refresh(self) -> None:
        """Push buffered data to the curves and apply auto-scroll."""
        if not self.times:
            return
        xs = list(self.times)
        for key, curve in self.curves.items():
            if curve.isVisible():
                ys = list(self.values[key])
                if len(ys) == len(xs):
                    curve.setData(xs, ys)
        if self.autoscroll:
            latest = xs[-1]
            self.view_box.setXRange(max(0.0, latest - self.time_window_s),
                                    max(latest, self.time_window_s),
                                    padding=0)
            self.view_box.enableAutoRange(axis=pg.ViewBox.YAxis)
        self.right_vb.enableAutoRange(axis=pg.ViewBox.YAxis)
        self._update_right_axis_label()

    def _update_right_axis_label(self) -> None:
        units = {sensor.unit for sensor in core.SENSOR_DEFS
                 if sensor.group != "voltage"
                 and self.curves[sensor.key].isVisible()}
        label = units.pop() if len(units) == 1 else "mixed units"
        self.plot_item.setLabel("right", label)

    def reset_view(self) -> None:
        """Snap back to auto-scroll mode (Task 3 Reset view)."""
        self.autoscroll = True
        self.refresh()

    def set_sensor_visible(self, key: str, visible: bool) -> None:
        self.curves[key].setVisible(visible)

    def highlight(self, key: Optional[str]) -> None:
        """Bold one trace and dim the others; None restores all."""
        for curve_key, curve in self.curves.items():
            colour = QtGui.QColor(self._base_colours[curve_key])
            if key is None:
                curve.setPen(pg.mkPen(colour, width=1.5))
            elif curve_key == key:
                curve.setPen(pg.mkPen(colour, width=3))
            else:
                colour.setAlpha(DIMMED_ALPHA)
                curve.setPen(pg.mkPen(colour, width=1))

    # ------------------------------------------------------------------ #
    def add_marker(self, x: float, kind: str) -> None:
        """Vertical dashed marker: kind 'start' (green) or 'stop' (red)."""
        colour = ACCENT_OK if kind == "start" else ACCENT_DANGER
        label = "Start" if kind == "start" else "Stop"
        line = pg.InfiniteLine(
            pos=x, angle=90, movable=False,
            pen=pg.mkPen(colour, width=2, style=Qt.DashLine),
            label=label,
            labelOpts={"position": 0.92, "color": colour,
                       "fill": (255, 255, 255, 30)})
        self.plot_item.addItem(line)
        self._markers.append(line)

    def clear_markers(self) -> None:
        for line in self._markers:
            self.plot_item.removeItem(line)
        self._markers = []

    # ------------------------------------------------------------------ #
    def clear_animated(self, on_done=None) -> None:
        """Fade traces out over 0.3 s, then clear all data (Task 8)."""
        anim = QVariantAnimation(self)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setDuration(FADE_CLEAR_MS)
        anim.valueChanged.connect(self._set_traces_opacity)
        anim.finished.connect(lambda: self._finish_clear(on_done))
        self._fade_anim = anim          # keep a reference while running
        anim.start()

    def _set_traces_opacity(self, value: float) -> None:
        for curve in self.curves.values():
            curve.setOpacity(float(value))

    def _finish_clear(self, on_done) -> None:
        self.times.clear()
        for buffer in self.values.values():
            buffer.clear()
        for curve in self.curves.values():
            curve.setData([], [])
            curve.setOpacity(1.0)
        self.clear_markers()
        self.autoscroll = True
        self._fade_anim = None
        if on_done is not None:
            on_done()


# ===========================================================================
# Main window
# ===========================================================================
class PEMstackMainWindow(QtWidgets.QMainWindow):
    """Three-zone PEMstack main window (toolbar / plot + cards / status)."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PEMstack — PEM fuel cell test bench")
        self.setMinimumSize(900, 600)
        self.resize(1280, 800)

        # ---------------- state ----------------
        self.first_run = not app_settings.settings_file_exists()
        self.settings = app_settings.load_settings()
        if self.first_run:                       # silently create settings.json
            try:
                app_settings.save_settings(self.settings)
            except OSError:
                pass

        self.ser: Optional[serial.Serial] = None
        self.serial_lock = threading.Lock()
        self.acquisition: Optional[core.SerialAcquisition] = None
        self.sample_queue: "queue.Queue" = queue.Queue(maxsize=core.ACQ_QUEUE_SIZE)
        self.recorder = core.DataRecorder()

        self.connected = False
        self.running = False
        self.experiment_start: Optional[datetime.datetime] = None
        self.current_log_path: Optional[str] = None
        self.run_started: Optional[float] = None
        self.elapsed_accum = 0.0
        self.time_base = 0.0
        self.last_x: Optional[float] = None
        self.selected_key: Optional[str] = None
        self.last_saved_path: Optional[str] = None
        self.last_dropped = 0
        self.drops_changed_at: Optional[float] = None
        self.reconnect_active = False
        self.reconnect_countdown = 0
        self.reconnect_port: Optional[str] = None
        self.reconnect_baud = core.SERIAL_BAUDRATE

        # ---------------- UI ----------------
        self.plot = PlotManager(self.settings, self)
        self._build_toolbar()
        self._build_menu()
        self._build_central()
        self._build_status_bar()
        self._apply_stylesheet()
        self._update_actions()

        # ---------------- timers ----------------
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._drain_and_refresh)
        self.refresh_timer.start(max(33, 1000 // self.settings.plot_refresh_hz))

        self.second_timer = QTimer(self)
        self.second_timer.timeout.connect(self._on_second_tick)
        self.second_timer.start(1000)

    # ================================================================== #
    # UI construction                                                     #
    # ================================================================== #
    def _build_toolbar(self) -> None:
        toolbar = QtWidgets.QToolBar("Main toolbar", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        # --- Group 1: Connection ---
        self.conn_dot = QtWidgets.QLabel("●")
        self.conn_dot.setToolTip("Connection status: grey = disconnected, "
                                 "green = connected, red = error")
        toolbar.addWidget(self.conn_dot)

        self.port_combo = ui_widgets.PortComboBox(self._list_ports)
        self.port_combo.addItems(self._list_ports())
        toolbar.addWidget(self.port_combo)

        self.baud_combo = QtWidgets.QComboBox()
        self.baud_combo.addItems(["9600", "115200", "250000"])
        self.baud_combo.setCurrentText(str(core.SERIAL_BAUDRATE))
        self.baud_combo.setToolTip("Serial baud rate "
                                   "(must match the board firmware)")
        toolbar.addWidget(self.baud_combo)

        self.act_connect = QtWidgets.QAction("🔌 Connect", self)
        self.act_connect.setToolTip("Open the selected serial port "
                                    "and start streaming")
        self.act_connect.triggered.connect(self.toggle_connection)
        toolbar.addAction(self.act_connect)

        toolbar.addSeparator()

        # --- Group 2: Experiment ---
        self.act_start = QtWidgets.QAction("▶ Start", self)
        self.act_start.setShortcut(QKeySequence("Ctrl+R"))
        self.act_start.setToolTip("Begin recording the experiment (Ctrl+R)")
        self.act_start.triggered.connect(self.start_run)
        toolbar.addAction(self.act_start)

        self.act_stop = QtWidgets.QAction("■ Stop", self)
        self.act_stop.setShortcut(QKeySequence("Ctrl+T"))
        self.act_stop.setToolTip("Stop recording the experiment (Ctrl+T)")
        self.act_stop.triggered.connect(self.stop_run)
        toolbar.addAction(self.act_stop)

        self.act_new = QtWidgets.QAction("↺ New Experiment", self)
        self.act_new.setShortcut(QKeySequence("Ctrl+N"))
        self.act_new.setToolTip("Clear all data and start fresh — "
                                "asks for confirmation (Ctrl+N)")
        self.act_new.triggered.connect(self.new_experiment)
        toolbar.addAction(self.act_new)

        toolbar.addSeparator()

        # --- Group 3: Save ---
        self.act_save = QtWidgets.QAction("💾 Save", self)
        self.act_save.setShortcut(QKeySequence("Ctrl+S"))
        self.act_save.setToolTip("Save the recorded data with the "
                                 "auto-generated name (Ctrl+S)")
        self.act_save.triggered.connect(self.save_data)
        toolbar.addAction(self.act_save)

        self.format_badge = QtWidgets.QLabel()
        self.format_badge.setToolTip("Current file format "
                                     "(change it in Settings)")
        self.format_badge.setStyleSheet(
            f"QLabel {{ background-color: {ACCENT_PRIMARY}; color: white;"
            f" border-radius: 3px; padding: 2px 8px; font-weight: bold; }}")
        toolbar.addWidget(self.format_badge)
        self._update_format_badge()

        toolbar.addSeparator()

        # --- Group 4: Settings ---
        self.act_settings = QtWidgets.QAction("⚙ Settings", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_settings.setToolTip("Open the settings panel (Ctrl+,)")
        self.act_settings.triggered.connect(self.open_settings)
        toolbar.addAction(self.act_settings)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        replay = file_menu.addAction("Open experiment log…")
        replay.setToolTip("Re-plot a previously recorded raw .txt log")
        replay.triggered.connect(self.open_log_replay)
        file_menu.addSeparator()
        file_menu.addAction(self.act_new)
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_settings)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

    def _build_central(self) -> None:
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 4, 8, 4)

        self.banners = ui_widgets.BannerStack(central)
        root.addWidget(self.banners)

        splitter = QtWidgets.QSplitter(Qt.Horizontal)

        # ----- left: plot zone (plot + overlays + window slider) -----
        plot_zone = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_zone)
        plot_layout.setContentsMargins(0, 0, 0, 0)

        overlay_grid = QtWidgets.QGridLayout()
        overlay_grid.addWidget(self.plot.widget, 0, 0)

        self.placeholder = QtWidgets.QLabel(
            "No data yet — press ▶ Start to begin")
        self.placeholder.setObjectName("plotPlaceholder")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setAttribute(Qt.WA_TransparentForMouseEvents)
        overlay_grid.addWidget(self.placeholder, 0, 0, Qt.AlignCenter)

        self.welcome = self._build_welcome_card()
        overlay_grid.addWidget(self.welcome, 0, 0, Qt.AlignCenter)
        self.welcome.setVisible(self.first_run)
        plot_layout.addLayout(overlay_grid, 1)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Time window:"))
        self.window_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.window_slider.setRange(*app_settings.TIME_WINDOW_RANGE)
        self.window_slider.setValue(self.settings.time_window_s)
        self.window_slider.setToolTip("Width of the auto-scrolling time "
                                      "window on the X axis")
        self.window_slider.valueChanged.connect(self._on_window_slider)
        controls.addWidget(self.window_slider, 1)
        self.window_label = QtWidgets.QLabel(
            f"{self.settings.time_window_s} s")
        controls.addWidget(self.window_label)
        reset_btn = QtWidgets.QPushButton("Reset view")
        reset_btn.setToolTip("Snap back to auto-scroll after zooming "
                             "or panning")
        reset_btn.clicked.connect(self.plot.reset_view)
        controls.addWidget(reset_btn)
        plot_layout.addLayout(controls)

        self.plot.view_box.user_interacted.connect(self._on_plot_interaction)
        splitter.addWidget(plot_zone)

        # ----- right: sensor panel (cards + stimulus group) -----
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)

        cards_holder = QtWidgets.QWidget()
        cards_grid = QtWidgets.QGridLayout(cards_holder)
        cards_grid.setSpacing(6)
        self.cards: Dict[str, ui_widgets.SensorCard] = {}
        for index, sensor in enumerate(core.SENSOR_DEFS):
            card = ui_widgets.SensorCard(sensor.key, sensor.name, sensor.unit)
            card.selected.connect(self._on_card_selected)
            card.disable_requested.connect(self._on_card_toggle_enabled)
            card.set_thresholds(self.settings.thresholds_for(sensor.key))
            card.set_show_minmax(self.settings.show_minmax_on_cards)
            card.set_sensor_enabled(self.settings.is_sensor_enabled(sensor.key))
            cards_grid.addWidget(card, index // 2, index % 2)
            self.cards[sensor.key] = card
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_holder)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        panel_layout.addWidget(scroll, 1)

        stimulus = QtWidgets.QGroupBox("Stimulus (solenoid valve)")
        stim_layout = QtWidgets.QHBoxLayout(stimulus)
        self.btn_step = QtWidgets.QPushButton("Step")
        self.btn_step.setToolTip("Apply the step input to the solenoid valve")
        self.btn_step.clicked.connect(lambda: self._send_command(b"s"))
        self.btn_pyramid = QtWidgets.QPushButton("Pyramid")
        self.btn_pyramid.setToolTip("Apply the pyramid (PWM) input "
                                    "to the solenoid valve")
        self.btn_pyramid.clicked.connect(lambda: self._send_command(b"w"))
        self.btn_release = QtWidgets.QPushButton("Release")
        self.btn_release.setToolTip("Release the solenoid valve")
        self.btn_release.clicked.connect(lambda: self._send_command(b"r"))
        for button in (self.btn_step, self.btn_pyramid, self.btn_release):
            stim_layout.addWidget(button)
        panel_layout.addWidget(stimulus)

        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 2)     # plot keeps >= ~60 % of the width
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([820, 440])
        root.addWidget(splitter, 1)

        self.setCentralWidget(central)

    def _build_welcome_card(self) -> QtWidgets.QFrame:
        """One-time first-run welcome card (Task 8)."""
        card = QtWidgets.QFrame()
        card.setObjectName("welcomeCard")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(28, 20, 28, 20)
        title = QtWidgets.QLabel("Welcome to PEMstack")
        title.setObjectName("welcomeTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        for step in ("1.  Connect your device",
                     "2.  Press ▶ Start",
                     "3.  Press 💾 Save"):
            label = QtWidgets.QLabel(step)
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)
        dismiss = QtWidgets.QPushButton("Got it")
        dismiss.clicked.connect(lambda: self.welcome.hide())
        layout.addWidget(dismiss, alignment=Qt.AlignCenter)
        return card

    def _build_status_bar(self) -> None:
        bar = self.statusBar()

        self.state_badge = ui_widgets.StateBadge()
        bar.addWidget(self.state_badge)

        self.centre_label = QtWidgets.QLabel("⏱ 00:00:00 · 0 samples")
        self.centre_label.setAlignment(Qt.AlignCenter)
        bar.addWidget(self.centre_label, 1)

        self.save_path_btn = QtWidgets.QToolButton()
        self.save_path_btn.setText("📁 no file saved yet")
        self.save_path_btn.setToolTip("Click to open the save folder")
        self.save_path_btn.setCursor(Qt.PointingHandCursor)
        self.save_path_btn.clicked.connect(self._open_save_folder)
        bar.addPermanentWidget(self.save_path_btn)

    # ================================================================== #
    # appearance                                                          #
    # ================================================================== #
    def _apply_stylesheet(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self.settings))
        self.plot.set_dark(self.settings.dark_mode)
        self._update_conn_dot()

    def _update_conn_dot(self, error: bool = False) -> None:
        if error:
            colour = ACCENT_DANGER
        elif self.connected:
            colour = ACCENT_OK
        else:
            colour = ui_widgets.COLOUR_MUTED
        self.conn_dot.setStyleSheet(
            f"color: {colour}; font-size: 16pt; padding: 0 4px;")

    def _update_format_badge(self) -> None:
        text = {"CSV": "CSV", "HDF5": "HDF5",
                "both": "CSV+H5"}[self.settings.file_format]
        self.format_badge.setText(text)

    def _update_actions(self) -> None:
        """Grey out (never hide) every action that is not available now."""
        self.act_connect.setText(
            "⏏ Disconnect" if (self.connected or self.reconnect_active)
            else "🔌 Connect")
        self.act_connect.setToolTip(
            "Close the serial connection"
            if (self.connected or self.reconnect_active)
            else "Open the selected serial port and start streaming")
        self.act_start.setEnabled(self.connected and not self.running)
        self.act_stop.setEnabled(self.running)
        self.act_new.setEnabled(not self.running)
        self.act_save.setEnabled(True)
        for button in (self.btn_step, self.btn_pyramid, self.btn_release):
            button.setEnabled(self.connected)
        self.port_combo.setEnabled(not self.connected
                                   and not self.reconnect_active)
        self.baud_combo.setEnabled(not self.connected
                                   and not self.reconnect_active)

    # ================================================================== #
    # serial connection                                                   #
    # ================================================================== #
    @staticmethod
    def _list_ports() -> List[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    def toggle_connection(self) -> None:
        if self.connected or self.reconnect_active:
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self) -> None:
        if not self.port_combo.count():
            self.port_combo.addItems(self._list_ports())
        port = self.port_combo.currentText()
        if not port:
            self.banners.show_banner(
                "no-ports", "warning",
                "No serial ports found. Check USB connection.",
                action_text="Refresh",
                action_callback=self._refresh_ports_banner)
            return

        baud = int(self.baud_combo.currentText())
        try:
            self.ser = serial.Serial(
                port=port, baudrate=baud, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS,
                timeout=core.SERIAL_TIMEOUT_S)
        except (serial.SerialException, OSError) as exc:
            self._update_conn_dot(error=True)
            self.banners.show_banner(
                "connect-failed", "error",
                f"⚠ Could not open {port}: {exc}")
            return

        self._start_acquisition_thread()
        self.connected = True
        self.reconnect_port = port
        self.reconnect_baud = baud
        self.welcome.hide()                      # dismiss on first connection
        self.banners.dismiss("no-ports")
        self.banners.dismiss("connect-failed")
        self.banners.show_banner(
            "connected", "info",
            f"Connected to {port} — waiting for board data…")
        self._update_conn_dot()
        self._update_actions()

    def _refresh_ports_banner(self) -> None:
        self.port_combo.clear()
        self.port_combo.addItems(self._list_ports())
        if self.port_combo.count():
            self.banners.dismiss("no-ports")

    def _start_acquisition_thread(self) -> None:
        """Create and start a reader thread on the open port."""
        if self.last_x is not None:
            self.time_base = self.last_x + 1.0   # keep the timeline monotonic
        self.acquisition = core.SerialAcquisition(
            self.ser, None, self.sample_queue, self.serial_lock)
        if self.running and self.current_log_path:
            self.acquisition.enable_logging(self.current_log_path)
        self.last_dropped = 0
        self.acquisition.start()

    def disconnect_serial(self) -> None:
        self.reconnect_active = False
        self.banners.dismiss("serial-lost")
        if self.running:
            self.stop_run()
        if self.acquisition is not None:
            self.acquisition.stop_event.set()
            self.acquisition.join(timeout=2.0)
            self.acquisition = None
        if self.ser is not None:
            try:
                with self.serial_lock:
                    if self.ser.is_open:
                        self.ser.write(b"r")     # release solenoid valve
                        self.ser.write(b"C")     # close communication
                        self.ser.close()
            except (serial.SerialException, OSError):
                pass
            self.ser = None
        self.connected = False
        self._update_conn_dot()
        self._update_actions()

    def _send_command(self, command: bytes) -> None:
        if self.ser is None:
            return
        try:
            with self.serial_lock:
                if self.ser.is_open:
                    self.ser.write(command)
        except (serial.SerialException, OSError) as exc:
            self.banners.show_banner("write-failed", "error",
                                     f"⚠ Serial write failed: {exc}")

    # ================================================================== #
    # experiment control                                                  #
    # ================================================================== #
    def start_run(self) -> None:
        if not self.connected or self.running:
            return
        if self.experiment_start is None:
            self.experiment_start = datetime.datetime.now()

        # raw .txt log goes to the save folder under the auto-name
        base = app_settings.build_filename(self.settings.name_template,
                                           self.experiment_start)
        try:
            os.makedirs(self.settings.save_folder, exist_ok=True)
            self.current_log_path = app_settings.unique_path(
                self.settings.save_folder, base, "txt")
        except OSError:
            self.current_log_path = base + ".txt"      # fall back to CWD
        if self.acquisition is not None:
            self.acquisition.enable_logging(self.current_log_path)

        self.running = True
        self.run_started = monotonic()
        marker_x = self.last_x if self.last_x is not None else self.time_base
        self.plot.add_marker(marker_x, "start")
        self.state_badge.set_state("RUNNING")
        self._update_actions()

    def stop_run(self) -> None:
        if not self.running:
            return
        if self.acquisition is not None:
            self.acquisition.disable_logging()
        self.running = False
        if self.run_started is not None:
            self.elapsed_accum += monotonic() - self.run_started
            self.run_started = None
        marker_x = self.last_x if self.last_x is not None else self.time_base
        self.plot.add_marker(marker_x, "stop")
        self.state_badge.set_state("STOPPED")
        self._update_actions()
        if self.settings.autosave_on_stop and len(self.recorder) > 0:
            self.save_data()

    def new_experiment(self) -> None:
        if self.running:
            return                                # greyed out anyway
        result = QtWidgets.QMessageBox.question(
            self, "New Experiment",
            "Start new experiment? Unsaved data will be lost.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if result != QtWidgets.QMessageBox.Yes:
            return
        self.plot.clear_animated(on_done=self._finish_new_experiment)

    def _finish_new_experiment(self) -> None:
        while True:
            try:
                self.sample_queue.get_nowait()
            except queue.Empty:
                break
        self.recorder.clear()
        for card in self.cards.values():
            card.reset()
        self.experiment_start = None
        self.current_log_path = None
        self.elapsed_accum = 0.0
        self.run_started = None
        self.time_base = 0.0
        self.last_x = None
        self.selected_key = None
        self.plot.highlight(None)
        self.state_badge.set_state("IDLE")
        self.centre_label.setText("⏱ 00:00:00 · 0 samples")
        self.placeholder.show()
        self._update_actions()

    # ================================================================== #
    # data flow (consumer side)                                           #
    # ================================================================== #
    def _apply_offsets(self, sample: "core.Sample") -> "core.Sample":
        """Apply per-sensor calibration offsets from Settings."""
        offsets = self.settings.calibration_offsets
        if not offsets:
            return sample
        get = self.settings.offset_for
        return dataclasses.replace(
            sample,
            cells_mv=[v + get(f"cell{i}") for i, v in enumerate(sample.cells_mv)],
            pressures_kpa=[sample.pressures_kpa[0] + get("p10"),
                           sample.pressures_kpa[1] + get("p11"),
                           sample.pressures_kpa[2] + get("p12")],
            current_ma=sample.current_ma + get("i13"),
            massflow_sccm=[sample.massflow_sccm[0] + get("mf14"),
                           sample.massflow_sccm[1] + get("mf15")],
        )

    def _drain_and_refresh(self) -> None:
        drained = 0
        while True:
            try:
                sample, true_time = self.sample_queue.get_nowait()
            except queue.Empty:
                break
            sample = self._apply_offsets(sample)
            x = self.time_base + true_time
            self.last_x = x
            values = core.sample_values(sample)
            self.plot.append(x, values)
            for key, value in values.items():
                self.cards[key].update_value(value)
            if self.running:
                self.recorder.append(sample, int(x))
            drained += 1

        if drained:
            if self.placeholder.isVisible():
                self.placeholder.hide()
            self.banners.dismiss("connected")    # data confirmed flowing
            self.plot.refresh()

        self._check_thread_health()

    def _check_thread_health(self) -> None:
        acq = self.acquisition
        if acq is None:
            return
        # buffer overflow -> yellow banner with live drop count (Task 7)
        dropped = acq.dropped_samples
        if dropped > self.last_dropped:
            self.last_dropped = dropped
            self.drops_changed_at = monotonic()
            self.banners.show_banner(
                "overflow", "warning",
                f"⚠ Buffer overflow — samples are being dropped "
                f"({dropped} so far). The raw .txt log is not affected.",
                persistent=True)
        # reader thread died -> automatic reconnection (Task 7)
        if acq.error and not acq.is_alive() and not self.reconnect_active:
            self.acquisition = None
            if self.ser is not None:
                try:
                    with self.serial_lock:
                        if self.ser.is_open:
                            self.ser.close()
                except (serial.SerialException, OSError):
                    pass
                self.ser = None
            self.connected = False
            self.reconnect_active = True
            self.reconnect_countdown = RECONNECT_INTERVAL_S
            self._update_conn_dot(error=True)
            self._update_actions()
            self.banners.show_banner(
                "serial-lost", "error",
                f"⚠ Serial connection lost — reconnect or check cable · "
                f"retrying in {self.reconnect_countdown} s",
                persistent=True)

    def _attempt_reconnect(self) -> None:
        try:
            self.ser = serial.Serial(
                port=self.reconnect_port, baudrate=self.reconnect_baud,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS, timeout=core.SERIAL_TIMEOUT_S)
        except (serial.SerialException, OSError):
            self.ser = None
            self.reconnect_countdown = RECONNECT_INTERVAL_S
            return
        self._start_acquisition_thread()
        self.connected = True
        self.reconnect_active = False
        self.banners.dismiss("serial-lost")
        self.banners.show_banner(
            "reconnected", "success",
            f"Reconnected to {self.reconnect_port}.")
        self._update_conn_dot()
        self._update_actions()

    # ================================================================== #
    # periodic 1 s housekeeping                                           #
    # ================================================================== #
    def _on_second_tick(self) -> None:
        # elapsed time + sample count (status bar centre)
        elapsed = self.elapsed_accum
        if self.run_started is not None:
            elapsed += monotonic() - self.run_started
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        count = f"{len(self.recorder):,}".replace(",", " ")
        self.centre_label.setText(
            f"⏱ {hours:02d}:{minutes:02d}:{seconds:02d} · {count} samples")

        # stale-value detection on every card (Task 5)
        for card in self.cards.values():
            card.check_stale()

        # reconnect countdown (Task 7)
        if self.reconnect_active:
            self.reconnect_countdown -= 1
            if self.reconnect_countdown <= 0:
                self._attempt_reconnect()
            if self.reconnect_active:
                self.banners.show_banner(
                    "serial-lost", "error",
                    f"⚠ Serial connection lost — reconnect or check cable · "
                    f"retrying in {max(self.reconnect_countdown, 0)} s",
                    persistent=True)

        # overflow banner: release it once drops stopped for 10 s
        if (self.banners.has_banner("overflow")
                and self.drops_changed_at is not None
                and monotonic() - self.drops_changed_at > 10):
            self.banners.dismiss("overflow")

    # ================================================================== #
    # plot / card interactions                                            #
    # ================================================================== #
    def _on_plot_interaction(self) -> None:
        self.plot.autoscroll = False

    def _on_window_slider(self, value: int) -> None:
        self.window_label.setText(f"{value} s")
        self.plot.time_window_s = value
        self.settings.time_window_s = value      # persisted on shutdown

    def _on_card_selected(self, key: str) -> None:
        if self.selected_key == key:
            self.selected_key = None             # click again -> unselect
        else:
            self.selected_key = key
        for card_key, card in self.cards.items():
            card.set_selected(card_key == self.selected_key)
        self.plot.highlight(self.selected_key)

    def _on_card_toggle_enabled(self, key: str, enable: bool) -> None:
        self.settings.sensor_enabled[key] = enable
        self.cards[key].set_sensor_enabled(enable)
        self.plot.set_sensor_visible(key, enable)
        try:
            app_settings.save_settings(self.settings)
        except OSError:
            pass

    # ================================================================== #
    # saving (Task 7: failures must be actionable)                        #
    # ================================================================== #
    def save_data(self, folder_override: Optional[str] = None) -> None:
        if len(self.recorder) == 0:
            self.banners.show_banner("nothing-to-save", "info",
                                     "Nothing to save yet — no samples "
                                     "have been recorded.")
            return

        previous_state = "RUNNING" if self.running else (
            "STOPPED" if self.experiment_start else "IDLE")
        self.state_badge.set_state("SAVING")
        QtWidgets.QApplication.processEvents()

        folder = folder_override or self.settings.save_folder
        start = self.experiment_start or datetime.datetime.now()
        base = app_settings.build_filename(self.settings.name_template, start)
        fmt = self.settings.file_format
        saved: List[str] = []
        try:
            os.makedirs(folder, exist_ok=True)
            if fmt in ("CSV", "both"):
                path = app_settings.unique_path(folder, base, "csv")
                self.recorder.save_csv(path, self.settings.include_header)
                saved.append(path)
            if fmt in ("HDF5", "both"):
                path = app_settings.unique_path(folder, base, "h5")
                try:
                    self.recorder.save_hdf5(path)
                    saved.append(path)
                except ImportError:
                    self.banners.show_banner(
                        "hdf5-missing", "warning",
                        "HDF5 needs the optional h5py package "
                        "(pip install h5py) — saved as CSV instead.")
                    if fmt == "HDF5":
                        path = app_settings.unique_path(folder, base, "csv")
                        self.recorder.save_csv(path,
                                               self.settings.include_header)
                        saved.append(path)
        except OSError as exc:
            self.state_badge.set_state(previous_state)
            self._save_failed_dialog(exc)
            return

        self.state_badge.set_state(previous_state)
        if saved:
            self.last_saved_path = saved[-1]
            shown = self._elide_path(saved[-1])
            self.save_path_btn.setText(f"📁 {shown}")
            self.save_path_btn.setToolTip(
                f"{saved[-1]}\nClick to open the containing folder")
            self.banners.show_banner(
                "saved", "success",
                f"Saved: {', '.join(os.path.basename(p) for p in saved)}")

    def _save_failed_dialog(self, exc: OSError) -> None:
        """Modal Retry / Save elsewhere… / Cancel dialog with the OS error."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Critical)
        box.setWindowTitle("Save failed")
        box.setText("The data could not be saved.")
        box.setInformativeText(str(exc))
        retry = box.addButton("Retry", QtWidgets.QMessageBox.AcceptRole)
        elsewhere = box.addButton("Save elsewhere…",
                                  QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec_()
        clicked = box.clickedButton()
        if clicked == retry:
            self.save_data()
        elif clicked == elsewhere:
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Choose another folder", os.path.expanduser("~"))
            if folder:
                self.save_data(folder_override=folder)

    @staticmethod
    def _elide_path(path: str, max_length: int = 60) -> str:
        if len(path) <= max_length:
            return path
        return f"{path[:20]}…{path[-(max_length - 21):]}"

    def _open_save_folder(self) -> None:
        folder = (os.path.dirname(self.last_saved_path)
                  if self.last_saved_path else self.settings.save_folder)
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ================================================================== #
    # settings                                                            #
    # ================================================================== #
    def open_settings(self) -> None:
        dialog = ui_settings.SettingsDialog(self.settings, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        self.settings = dialog.result_settings()
        try:
            app_settings.save_settings(self.settings)
        except OSError as exc:
            self.banners.show_banner("settings-save", "error",
                                     f"⚠ Could not write settings.json: {exc}")
        self._apply_settings()

    def _apply_settings(self) -> None:
        self._apply_stylesheet()
        self._update_format_badge()
        self.refresh_timer.setInterval(
            max(33, 1000 // self.settings.plot_refresh_hz))
        self.plot.set_buffer_size(self.settings.buffer_size)
        self.plot.time_window_s = self.settings.time_window_s
        self.window_slider.blockSignals(True)
        self.window_slider.setValue(self.settings.time_window_s)
        self.window_slider.blockSignals(False)
        self.window_label.setText(f"{self.settings.time_window_s} s")
        for sensor in core.SENSOR_DEFS:
            card = self.cards[sensor.key]
            enabled = self.settings.is_sensor_enabled(sensor.key)
            card.set_thresholds(self.settings.thresholds_for(sensor.key))
            card.set_show_minmax(self.settings.show_minmax_on_cards)
            card.set_sensor_enabled(enabled)
            self.plot.set_sensor_visible(sensor.key, enabled)

    # ================================================================== #
    # log replay (the historical "Plot file" feature)                     #
    # ================================================================== #
    def open_log_replay(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open experiment log", self.settings.save_folder,
            "Experiment logs (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError as exc:
            self.banners.show_banner("replay-failed", "error",
                                     f"⚠ Could not open log: {exc}")
            return

        samples = []
        for line in lines:
            payload = line.split("data: ", 1)[-1].strip()
            sample = core.parse_payload(payload)
            if sample is not None:
                samples.append(sample)
        if not samples:
            self.banners.show_banner(
                "replay-failed", "warning",
                f"No valid data lines found in "
                f"{os.path.basename(path)}.")
            return

        offset = samples[0].time_s
        self.plot._finish_clear(None)            # instant clear, no fade
        for sample in samples:
            self.plot.append(sample.time_s - offset, core.sample_values(sample))
        self.placeholder.hide()
        self.plot.autoscroll = False             # show the whole file
        self.plot.refresh()
        self.plot.view_box.autoRange()
        self.banners.show_banner(
            "replay-done", "info",
            f"Replayed {len(samples)} samples from "
            f"{os.path.basename(path)} (display only, not recorded).")

    # ================================================================== #
    # shutdown                                                            #
    # ================================================================== #
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        result = QtWidgets.QMessageBox.question(
            self, "Confirm Exit...",
            "Are you sure you want to exit ?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if result == QtWidgets.QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()

    def shutdown(self) -> None:
        """Stop threads, close the port and persist the settings."""
        self.reconnect_active = False
        if self.acquisition is not None:
            self.acquisition.stop_event.set()
            self.acquisition.join(timeout=2.0)
            self.acquisition = None
        if self.ser is not None:
            try:
                with self.serial_lock:
                    if self.ser.is_open:
                        self.ser.write(b"r")
                        self.ser.write(b"C")
                        self.ser.close()
            except (serial.SerialException, OSError):
                pass
            self.ser = None
        try:
            app_settings.save_settings(self.settings)
        except OSError:
            pass
