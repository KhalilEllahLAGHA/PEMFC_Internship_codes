#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_settings.py
==============
Tabbed Settings dialog for the redesigned PEMstack interface:
  Tab 1 — Acquisition : sampling rate, buffer size, per-sensor table
                        (enable / calibration offset / warning thresholds)
  Tab 2 — Save        : folder, auto-name template, format, auto-save, header
  Tab 3 — Display     : time window, refresh rate, dark mode, cards, fonts

OK validates every field (inline red messages, the dialog never closes with
invalid values), then the caller persists the result to settings.json.
"""

import datetime
from typing import Dict, List, Optional

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

import app_settings
from main_gui0 import SENSOR_DEFS

_ERROR_STYLE = "color: #b3261e; font-weight: bold;"

# sensor-table column indices
COL_SENSOR, COL_OFFSET, COL_WMIN, COL_WMAX = range(4)


def _make_error_label() -> QtWidgets.QLabel:
    """Create a hidden inline error label (shown red on invalid input)."""
    label = QtWidgets.QLabel()
    label.setStyleSheet(_ERROR_STYLE)
    label.setWordWrap(True)
    label.hide()
    return label


class SettingsDialog(QtWidgets.QDialog):
    """Modal three-tab settings dialog with validation and restore-defaults."""

    def __init__(self, settings: app_settings.AppSettings,
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._tabs = QtWidgets.QTabWidget(self)
        self._tabs.addTab(self._build_acquisition_tab(settings), "Acquisition")
        self._tabs.addTab(self._build_save_tab(settings), "Save")
        self._tabs.addTab(self._build_display_tab(settings), "Display")

        restore_btn = QtWidgets.QPushButton("Restore defaults")
        restore_btn.setToolTip("Reset the fields of the current tab "
                               "to their default values")
        restore_btn.clicked.connect(self._restore_defaults)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(restore_btn)
        bottom.addStretch(1)
        bottom.addWidget(buttons)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(self._tabs)
        root.addLayout(bottom)

    # ================================================================== #
    # Tab 1 — Acquisition                                                 #
    # ================================================================== #
    def _build_acquisition_tab(self,
                               settings: app_settings.AppSettings) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)

        self.sampling_spin = QtWidgets.QSpinBox()
        self.sampling_spin.setRange(*app_settings.SAMPLING_RATE_RANGE)
        self.sampling_spin.setValue(settings.sampling_rate_hz)
        self.sampling_spin.setSuffix(" Hz")
        self.sampling_spin.setToolTip(
            "Requested board sampling rate (applied if the firmware "
            "supports rate commands)")
        form.addRow("Sampling rate:", self.sampling_spin)

        self.buffer_spin = QtWidgets.QSpinBox()
        self.buffer_spin.setRange(*app_settings.BUFFER_SIZE_RANGE)
        self.buffer_spin.setValue(settings.buffer_size)
        self.buffer_spin.setSuffix(" samples")
        self.buffer_spin.setToolTip("Number of samples kept in the live plot")
        form.addRow("Buffer size:", self.buffer_spin)

        # one row per sensor: enable checkbox + offset + warning thresholds
        self.sensor_table = QtWidgets.QTableWidget(len(SENSOR_DEFS), 4)
        self.sensor_table.setHorizontalHeaderLabels(
            ["Sensor (✓ = enabled)", "Offset", "Warn min", "Warn max"])
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_table.horizontalHeader().setSectionResizeMode(
            COL_SENSOR, QtWidgets.QHeaderView.Stretch)
        self.sensor_table.setMinimumHeight(260)

        for row, sensor in enumerate(SENSOR_DEFS):
            name_item = QtWidgets.QTableWidgetItem(
                f"{sensor.name} [{sensor.unit}]")
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            name_item.setCheckState(
                Qt.Checked if settings.is_sensor_enabled(sensor.key)
                else Qt.Unchecked)
            name_item.setData(Qt.UserRole, sensor.key)
            self.sensor_table.setItem(row, COL_SENSOR, name_item)

            offset = settings.calibration_offsets.get(sensor.key, 0.0)
            self.sensor_table.setItem(
                row, COL_OFFSET, QtWidgets.QTableWidgetItem(str(offset)))

            thresholds = settings.thresholds_for(sensor.key)
            low_text = str(thresholds[0]) if thresholds else ""
            high_text = str(thresholds[1]) if thresholds else ""
            self.sensor_table.setItem(
                row, COL_WMIN, QtWidgets.QTableWidgetItem(low_text))
            self.sensor_table.setItem(
                row, COL_WMAX, QtWidgets.QTableWidgetItem(high_text))

        form.addRow(self.sensor_table)
        self.acq_error = _make_error_label()
        form.addRow(self.acq_error)
        return tab

    # ================================================================== #
    # Tab 2 — Save                                                        #
    # ================================================================== #
    def _build_save_tab(self,
                        settings: app_settings.AppSettings) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)

        folder_row = QtWidgets.QHBoxLayout()
        browse_btn = QtWidgets.QPushButton("Choose folder…")
        browse_btn.setToolTip("Pick the folder where experiments are saved")
        browse_btn.clicked.connect(self._browse_folder)
        self.folder_label = QtWidgets.QLabel(settings.save_folder)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.folder_label.setWordWrap(True)
        folder_row.addWidget(browse_btn)
        folder_row.addWidget(self.folder_label, 1)
        form.addRow("Save folder:", folder_row)
        self.folder_error = _make_error_label()
        form.addRow(self.folder_error)

        self.template_edit = QtWidgets.QLineEdit(settings.name_template)
        self.template_edit.setToolTip(
            "Placeholders (experiment START time):\n"
            "{HH}=hour  {mm}=minutes  {DD}=day  {MM}=month  {YYYY}=year\n"
            "Free text is kept as-is, e.g. MEA5_Experiment_{HH}h{mm}_{DD}-{MM}-{YYYY}")
        self.template_edit.textChanged.connect(self._update_template_preview)
        form.addRow("Filename template:", self.template_edit)
        self.template_preview = QtWidgets.QLabel()
        self.template_preview.setStyleSheet("color: #5f6368;")
        form.addRow("", self.template_preview)
        self.template_error = _make_error_label()
        form.addRow(self.template_error)

        self.format_csv = QtWidgets.QRadioButton("CSV")
        self.format_hdf5 = QtWidgets.QRadioButton("HDF5")
        self.format_both = QtWidgets.QRadioButton("Both")
        self.format_hdf5.setToolTip(
            "Requires the optional h5py package (pip install h5py); "
            "falls back to CSV when missing")
        {"CSV": self.format_csv, "HDF5": self.format_hdf5,
         "both": self.format_both}[settings.file_format].setChecked(True)
        format_row = QtWidgets.QHBoxLayout()
        for radio in (self.format_csv, self.format_hdf5, self.format_both):
            format_row.addWidget(radio)
        format_row.addStretch(1)
        form.addRow("File format:", format_row)

        self.autosave_check = QtWidgets.QCheckBox(
            "Save automatically when acquisition stops")
        self.autosave_check.setChecked(settings.autosave_on_stop)
        form.addRow("Auto-save on Stop:", self.autosave_check)

        self.header_check = QtWidgets.QCheckBox(
            "Write the column-name header row in CSV files")
        self.header_check.setChecked(settings.include_header)
        form.addRow("Include header row:", self.header_check)

        self._update_template_preview()
        return tab

    # ================================================================== #
    # Tab 3 — Display                                                     #
    # ================================================================== #
    def _build_display_tab(self,
                           settings: app_settings.AppSettings) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)

        window_row = QtWidgets.QHBoxLayout()
        self.window_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.window_slider.setRange(*app_settings.TIME_WINDOW_RANGE)
        self.window_slider.setValue(settings.time_window_s)
        self.window_value = QtWidgets.QLabel(f"{settings.time_window_s} s")
        self.window_slider.valueChanged.connect(
            lambda v: self.window_value.setText(f"{v} s"))
        window_row.addWidget(self.window_slider, 1)
        window_row.addWidget(self.window_value)
        form.addRow("Time window:", window_row)

        self.refresh_spin = QtWidgets.QSpinBox()
        self.refresh_spin.setRange(*app_settings.PLOT_REFRESH_RANGE)
        self.refresh_spin.setValue(settings.plot_refresh_hz)
        self.refresh_spin.setSuffix(" Hz")
        form.addRow("Plot refresh rate:", self.refresh_spin)

        self.dark_check = QtWidgets.QCheckBox("Use a dark interface theme")
        self.dark_check.setChecked(settings.dark_mode)
        form.addRow("Dark mode:", self.dark_check)

        self.minmax_check = QtWidgets.QCheckBox(
            "Show min/max values on sensor cards")
        self.minmax_check.setChecked(settings.show_minmax_on_cards)
        form.addRow("Min/max on cards:", self.minmax_check)

        font_row = QtWidgets.QHBoxLayout()
        self.font_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.font_slider.setRange(0, 2)
        self.font_slider.setPageStep(1)
        self.font_slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self._font_names = list(app_settings.FONT_PRESETS)   # Small/Medium/Large
        self.font_slider.setValue(self._font_names.index(
            settings.font_size if settings.font_size in self._font_names
            else "Medium"))
        self.font_value = QtWidgets.QLabel(settings.font_size)
        self.font_slider.valueChanged.connect(
            lambda v: self.font_value.setText(self._font_names[v]))
        font_row.addWidget(self.font_slider, 1)
        font_row.addWidget(self.font_value)
        form.addRow("Font size:", font_row)
        return tab

    # ================================================================== #
    # helpers                                                             #
    # ================================================================== #
    def _browse_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose save folder", self.folder_label.text())
        if folder:
            self.folder_label.setText(folder)
            self.folder_error.hide()

    def _update_template_preview(self) -> None:
        name = app_settings.build_filename(self.template_edit.text(),
                                           datetime.datetime.now())
        self.template_preview.setText(f"Preview: {name}.csv")

    def _restore_defaults(self) -> None:
        """Reset only the fields of the currently visible tab."""
        defaults = app_settings.AppSettings()
        tab = self._tabs.currentIndex()
        if tab == 0:
            self.sampling_spin.setValue(defaults.sampling_rate_hz)
            self.buffer_spin.setValue(defaults.buffer_size)
            for row in range(self.sensor_table.rowCount()):
                self.sensor_table.item(row, COL_SENSOR).setCheckState(Qt.Checked)
                self.sensor_table.item(row, COL_OFFSET).setText("0.0")
                self.sensor_table.item(row, COL_WMIN).setText("")
                self.sensor_table.item(row, COL_WMAX).setText("")
            self.acq_error.hide()
        elif tab == 1:
            self.folder_label.setText(defaults.save_folder)
            self.template_edit.setText(defaults.name_template)
            self.format_csv.setChecked(True)
            self.autosave_check.setChecked(defaults.autosave_on_stop)
            self.header_check.setChecked(defaults.include_header)
            self.folder_error.hide()
            self.template_error.hide()
        else:
            self.window_slider.setValue(defaults.time_window_s)
            self.refresh_spin.setValue(defaults.plot_refresh_hz)
            self.dark_check.setChecked(defaults.dark_mode)
            self.minmax_check.setChecked(defaults.show_minmax_on_cards)
            self.font_slider.setValue(
                self._font_names.index(defaults.font_size))

    # ================================================================== #
    # validation                                                          #
    # ================================================================== #
    def _cell_text(self, row: int, col: int) -> str:
        item = self.sensor_table.item(row, col)
        return item.text().strip() if item is not None else ""

    def _validate_sensor_table(self) -> List[str]:
        problems: List[str] = []
        for row, sensor in enumerate(SENSOR_DEFS):
            offset_text = self._cell_text(row, COL_OFFSET)
            if offset_text:
                try:
                    float(offset_text)
                except ValueError:
                    problems.append(f"{sensor.name}: offset must be a number")
            low_text = self._cell_text(row, COL_WMIN)
            high_text = self._cell_text(row, COL_WMAX)
            if bool(low_text) != bool(high_text):
                problems.append(
                    f"{sensor.name}: set both warn min and warn max "
                    f"(or leave both empty)")
                continue
            if low_text:
                try:
                    low, high = float(low_text), float(high_text)
                except ValueError:
                    problems.append(
                        f"{sensor.name}: thresholds must be numbers")
                    continue
                if low >= high:
                    problems.append(
                        f"{sensor.name}: warn min must be below warn max")
        return problems

    def _try_accept(self) -> None:
        """Validate every tab; never close while a field is invalid."""
        table_problems = self._validate_sensor_table()
        if table_problems:
            self.acq_error.setText(" · ".join(table_problems[:3]))
            self.acq_error.show()
            self._tabs.setCurrentIndex(0)
            return
        self.acq_error.hide()

        if not self.folder_label.text().strip():
            self.folder_error.setText("Choose a save folder.")
            self.folder_error.show()
            self._tabs.setCurrentIndex(1)
            return
        self.folder_error.hide()

        template_problem = app_settings.template_error(self.template_edit.text())
        if template_problem:
            self.template_error.setText(template_problem)
            self.template_error.show()
            self._tabs.setCurrentIndex(1)
            return
        self.template_error.hide()

        self.accept()

    # ================================================================== #
    # result                                                              #
    # ================================================================== #
    def result_settings(self) -> app_settings.AppSettings:
        """Build an AppSettings from the (validated) dialog fields."""
        enabled: Dict[str, bool] = {}
        offsets: Dict[str, float] = {}
        thresholds: Dict[str, List[float]] = {}
        for row, sensor in enumerate(SENSOR_DEFS):
            item = self.sensor_table.item(row, COL_SENSOR)
            enabled[sensor.key] = item.checkState() == Qt.Checked
            offset_text = self._cell_text(row, COL_OFFSET)
            offset = float(offset_text) if offset_text else 0.0
            if offset != 0.0:
                offsets[sensor.key] = offset
            low_text = self._cell_text(row, COL_WMIN)
            if low_text:
                thresholds[sensor.key] = [
                    float(low_text), float(self._cell_text(row, COL_WMAX))]

        if self.format_hdf5.isChecked():
            file_format = "HDF5"
        elif self.format_both.isChecked():
            file_format = "both"
        else:
            file_format = "CSV"

        return app_settings.AppSettings(
            save_folder=self.folder_label.text().strip(),
            name_template=self.template_edit.text().strip(),
            file_format=file_format,
            autosave_on_stop=self.autosave_check.isChecked(),
            include_header=self.header_check.isChecked(),
            sampling_rate_hz=self.sampling_spin.value(),
            buffer_size=self.buffer_spin.value(),
            sensor_enabled=enabled,
            calibration_offsets=offsets,
            warning_thresholds=thresholds,
            time_window_s=self.window_slider.value(),
            plot_refresh_hz=self.refresh_spin.value(),
            dark_mode=self.dark_check.isChecked(),
            show_minmax_on_cards=self.minmax_check.isChecked(),
            font_size=self._font_names[self.font_slider.value()],
        )
