#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wend Dec 8 14:14:50 2021

@author: Elfrich

Refactored 2026-06-12:
 - acquisition runs in a producer thread; samples flow through a bounded
   queue.Queue and are consumed by a QTimer in the GUI thread
   (Qt widgets are only ever touched from the GUI thread)
 - all busy-wait `while True` polling loops replaced by Qt signals/timers
 - calibration values centralised as named constants below
 - "New Experiment" resets the in-app state without restarting the app
 - Settings panel (folder, auto-name template, format, auto-save) persisted
   in settings.json next to the application
"""

from PyQt5 import QtGui, QtCore, uic, QtWidgets
from PyQt5.QtWidgets import QMainWindow, QApplication, QVBoxLayout, QMessageBox
from PyQt5.QtCore import QObject, QTimer

import os
import csv
import queue
import dataclasses
import class_gui0

import sys

import datetime
import threading

#---------------------------------
import class_serial_plot as sPEMstack
import app_settings
import serial

from time import monotonic

import numpy as np

# import complex math module
import math

import serial.tools.list_ports

# ===========================================================================
# Calibration / configuration constants
# (were magic numbers spread through read_sensors / read_file — both paths
#  now share the same constants, removing the old 0.9775 vs 0.9051 mismatch)
# ===========================================================================
#constant_volt = 0.977517107 #(5000mv/1023*5) ; 5000mv=1000mV*5(gain of AD628; Rext1=499kohm,Rext2=10.2kohm)
VOLT_PER_COUNT_MV: float = 0.905108432  # (5000mv/1023*5.4) ; 5000mv=1000mV*5(gain of AD628; Rext1=499kohm,Rext2=10.2kohm)

P10_OFFSET_COUNTS: float = 36.0         # Psensor10 zero offset [counts]
P11_OFFSET_COUNTS: float = 27.5         # Psensor11 zero offset [counts]
PRESSURE_COUNTS_PER_KPA: float = 3.77487
P12_KPA_PER_COUNT: float = 0.09775      # kPa 100/1023

MASS_FLOW_PER_COUNT: float = 0.0488     # V 50/1023 gain= 300

# Current-sensor calibration: counts -> sensor voltage (linear fit), then
# voltage -> current through the inverse of a quadratic calibration curve.
ISENSOR_VOLT_P1: float = 202.9233
ISENSOR_VOLT_P2: float = -2.6951
ISENSOR_QUAD_A: float = -0.1183
ISENSOR_QUAD_B: float = 0.9293
ISENSOR_QUAD_C0: float = 2.5479
ISENSOR_ROOT_SPLIT: float = 1.7         # separates the physical quadratic root

# Solenoid-valve drive code -> voltage look-up
U_STACK_CODE_TO_VOLT = {0: 0, 50: 1, 76: 2, 97: 3, 116: 4, 133: 5, 148: 6,
                        163: 7, 176: 8, 184: 9, 189: 10, 195: 11, 200: 12,
                        206: 13, 211: 14, 216: 15, 222: 16, 227: 17, 233: 18,
                        238: 19, 244: 20, 249: 21, 255: 22}

SERIAL_BAUDRATE: int = 115200
SERIAL_TIMEOUT_S: float = 0.125  # 1  # 0,125#L/Fe#round(L/Fe)+1
SYNC_TIMEOUT_S: float = 10.0     # max wait for the board time counter restart
MIN_PAYLOAD_FIELDS: int = 19     # 'mydata' + 18 comma-separated values

GUI_REFRESH_MS: int = 100        # plot/queue-drain period (GUI thread)
PORT_REFRESH_MS: int = 2000      # COM-port list refresh period
ELAPSED_REFRESH_MS: int = 500    # elapsed-time label refresh period
ACQ_QUEUE_SIZE: int = 5000       # bounded queue between reader and GUI

N_CELLS: int = 10
N_PRESSURE: int = 3
N_MASSFLOW: int = 2


# ===========================================================================
# Conversion helpers
# ===========================================================================
def curve_fitter_voltage_isensor(val_conv_arduino: float) -> float:
	p1 = ISENSOR_VOLT_P1
	p2 = ISENSOR_VOLT_P2
	x = (val_conv_arduino - p2) / p1
	return x
	'''
	if x >= 2.4: #2.55:
		return x
	else:
		return 2.5
    '''


def curve_fitter_current_isensor(val_conv_arduino: float) -> float:
    """Invert the quadratic calibration curve; returns 0.0 when the value is
    outside the calibrated range (the old code returned None there, which
    crashed the plotting and the log writer)."""
    a = ISENSOR_QUAD_A
    b = ISENSOR_QUAD_B
    c = ISENSOR_QUAD_C0 - val_conv_arduino
    dis = (b ** 2) - (4 * a * c)  # calculating  the discriminant
    if dis < 0:
        # no real root -> outside the calibrated range (old code hid this
        # with sqrt(abs(dis)), producing a wrong but plausible number)
        return 0.0
    sqrt_val = math.sqrt(dis)  # find two results
    ans1 = (-b - sqrt_val) / (2 * a)
    ans2 = (-b + sqrt_val) / (2 * a)
    if ans1 > ISENSOR_ROOT_SPLIT > ans2:
        return ans2 if ans2 >= 0 else 0.0
    elif ans2 > ISENSOR_ROOT_SPLIT > ans1:
        return ans1 if ans1 >= 0 else 0.0
    return 0.0


def u_stack_code_to_volt(code: int) -> int:
    """Valve drive code -> voltage. Unknown codes snap to the nearest known
    entry (the old dict lookup raised KeyError and killed the read loop)."""
    if code in U_STACK_CODE_TO_VOLT:
        return U_STACK_CODE_TO_VOLT[code]
    nearest = min(U_STACK_CODE_TO_VOLT, key=lambda k: abs(k - code))
    return U_STACK_CODE_TO_VOLT[nearest]


# ===========================================================================
# Sample parsing — shared by the live serial reader and the file re-plotter
# ===========================================================================
@dataclasses.dataclass
class Sample:
    """One fully converted acquisition row (physical units)."""
    time_s: int                      # board timestamp [s], not yet re-zeroed
    cells_mv: list                   # 10 cell voltages [mV]
    pressures_kpa: list              # 3 pressures [kPa]
    current_ma: float                # stack current [mA]
    massflow_sccm: list              # 2 mass-flow values [SCCM]
    u_stack_v: int                   # valve drive voltage [V]
    raw: str                         # raw payload (kept for the .txt log)


def parse_payload(payload: str) -> "Sample | None":
    """Parse one 'mydata,...' payload into a converted Sample.

    Returns None for malformed/incomplete lines instead of raising, so a
    corrupted serial line can never kill the acquisition loop.
    """
    if not payload.startswith('mydata'):
        return None
    fields = payload.split(',')
    if len(fields) < MIN_PAYLOAD_FIELDS:
        return None
    try:
        values = [int(f.strip()) for f in fields[1:MIN_PAYLOAD_FIELDS]]
    except ValueError:
        return None

    cells_mv = [v * VOLT_PER_COUNT_MV for v in values[0:N_CELLS]]  # mV  (gain of AD628; Rext1=499kohm,Rext2=10.2kohm)
    pressures_kpa = [
        (values[10] - P10_OFFSET_COUNTS) / PRESSURE_COUNTS_PER_KPA,  # kPa
        (values[11] - P11_OFFSET_COUNTS) / PRESSURE_COUNTS_PER_KPA,  # kPa
        values[12] * P12_KPA_PER_COUNT,                              # kPa 100/1023
    ]
    voltage_isensor = curve_fitter_voltage_isensor(values[13])
    current_ma = curve_fitter_current_isensor(voltage_isensor)       # mA datasheet
    massflow_sccm = [values[14] * MASS_FLOW_PER_COUNT,               # V 50/1023 gain= 300
                     values[15] * MASS_FLOW_PER_COUNT]
    u_stack_v = u_stack_code_to_volt(values[16])
    time_s = values[17]

    return Sample(time_s, cells_mv, pressures_kpa, current_ma,
                  massflow_sccm, u_stack_v, payload)


@dataclasses.dataclass(frozen=True)
class SensorDef:
    """Static description of one acquisition channel (id, label, unit)."""
    key: str        # stable identifier used in settings.json and the UI
    name: str       # human-readable display name
    unit: str       # physical unit shown on cards and axes
    group: str      # unit group used for Y-axis assignment


# One entry per channel of a Sample, in display order.
SENSOR_DEFS = tuple(
    [SensorDef(f"cell{i}", f"Cell {i}", "mV", "voltage") for i in range(N_CELLS)]
    + [SensorDef("p10", "Pressure 10", "kPa", "pressure"),
       SensorDef("p11", "Pressure 11", "kPa", "pressure"),
       SensorDef("p12", "Pressure 12", "kPa", "pressure"),
       SensorDef("i13", "Current", "mA", "current"),
       SensorDef("mf14", "Mass flow 14", "SCCM", "massflow"),
       SensorDef("mf15", "Mass flow 15", "SCCM", "massflow"),
       SensorDef("u_stack", "U stack (input)", "V", "input")]
)


def sample_values(sample: Sample) -> dict:
    """Map a Sample onto {SensorDef.key: converted value} for the UI layer."""
    values = {f"cell{i}": sample.cells_mv[i] for i in range(N_CELLS)}
    values["p10"] = sample.pressures_kpa[0]
    values["p11"] = sample.pressures_kpa[1]
    values["p12"] = sample.pressures_kpa[2]
    values["i13"] = sample.current_ma
    values["mf14"] = sample.massflow_sccm[0]
    values["mf15"] = sample.massflow_sccm[1]
    values["u_stack"] = sample.u_stack_v
    return values


def format_log_line(sample: Sample, true_time: int) -> str:
    """Reproduce the historical .txt log line format exactly (the regexes in
    plot_experiments.py and the 'Plot file' button both rely on it)."""
    parts = [f'Cell{i}[V]: {int(sample.cells_mv[i])}' for i in range(N_CELLS)]
    parts += [f'PressureS{10 + i}[kPa]: {int(sample.pressures_kpa[i])}'
              for i in range(N_PRESSURE)]
    parts.append(f'CurrentS13[mA]: {int(sample.current_ma)}')
    parts += [f'MassFlow{14 + i}[SCCM]: {int(sample.massflow_sccm[i])}'
              for i in range(N_MASSFLOW)]
    parts.append(f'U_Stack[V]: {int(sample.u_stack_v)}')
    parts.append(f'time[s]: {int(true_time)}')
    parts.append(f'data: {sample.raw}')
    return ' '.join(parts)


# ===========================================================================
# Full-history recorder (for CSV/HDF5 export — display uses rolling deques)
# ===========================================================================
class DataRecorder:
    """Thread-safe, unbounded store of every converted sample of the
    current experiment. The rolling display buffers only keep the last
    `DISPLAY_BUFFER_LEN` points; this keeps everything for saving."""

    HEADER = (['time_s'] + [f'Cell{i}_mV' for i in range(N_CELLS)]
              + ['P10_kPa', 'P11_kPa', 'P12_kPa', 'Current_mA',
                 'MassFlow14_SCCM', 'MassFlow15_SCCM', 'U_stack_V'])

    def __init__(self) -> None:
        self._rows: list = []
        self._lock = threading.Lock()

    def append(self, sample: Sample, true_time: int) -> None:
        row = ([true_time] + list(sample.cells_mv) + list(sample.pressures_kpa)
               + [sample.current_ma] + list(sample.massflow_sccm)
               + [sample.u_stack_v])
        with self._lock:
            self._rows.append(row)

    def clear(self) -> None:
        with self._lock:
            self._rows = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def save_csv(self, path: str, include_header: bool = True) -> None:
        with self._lock:
            rows = list(self._rows)
        with open(path, 'w', newline='', encoding='utf-8') as fh:
            writer = csv.writer(fh)
            if include_header:
                writer.writerow(self.HEADER)
            writer.writerows(rows)

    def save_hdf5(self, path: str) -> None:
        """Requires the optional h5py package (ImportError bubbles up so the
        caller can warn the user and fall back to CSV)."""
        import h5py
        with self._lock:
            data = np.array(self._rows, dtype=float)
        with h5py.File(path, 'w') as fh:
            dset = fh.create_dataset('experiment', data=data)
            dset.attrs['columns'] = ','.join(self.HEADER)


# ===========================================================================
# Serial acquisition thread (producer)
# Reads lines, converts them, logs them to the raw .txt file and pushes
# Samples into a bounded queue. Never touches any Qt object.
# ===========================================================================
class SerialAcquisition(threading.Thread):

    def __init__(self, ser: serial.Serial, log_path: "str | None",
                 sample_queue: "queue.Queue", serial_lock: threading.Lock) -> None:
        super().__init__(name='read_sensors', daemon=True)
        self.ser = ser
        self.log_path = log_path
        self.sample_queue = sample_queue
        self.serial_lock = serial_lock          # guards writes vs. GUI thread
        self.stop_event = threading.Event()
        self.error: "str | None" = None
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        # Raw-log writing can be toggled at runtime (Connect vs. Start).
        # Constructing with a log_path keeps the old always-logging behaviour.
        self._log_lock = threading.Lock()
        self._log_enabled = threading.Event()
        if log_path:
            self._log_enabled.set()

    def enable_logging(self, log_path: str) -> None:
        """Begin (or switch) raw .txt logging to `log_path`."""
        with self._log_lock:
            self.log_path = log_path
        self._log_enabled.set()

    def disable_logging(self) -> None:
        """Stop raw .txt logging (the file is closed by the run loop)."""
        self._log_enabled.clear()

    @property
    def dropped_samples(self) -> int:
        with self._dropped_lock:
            return self._dropped

    def _read_line(self) -> str:
        raw = self.ser.readline()               # bounded by SERIAL_TIMEOUT_S
        return raw.decode('utf8', errors='ignore').strip()

    def _wait_for_board_sync(self) -> None:
        """Opening the port resets the Arduino, which restarts its time
        counter. Wait (bounded!) for a sample with time <= 2 s so logging
        starts at the beginning of the board's timeline. The old code waited
        forever when the board was already mid-stream."""
        deadline = monotonic() + SYNC_TIMEOUT_S
        while not self.stop_event.is_set() and monotonic() < deadline:
            sample = parse_payload(self._read_line())
            if sample is not None and sample.time_s <= 2:
                return

    def run(self) -> None:
        log_file = None
        try:
            self._wait_for_board_sync()
            time_offset: "int | None" = None
            while not self.stop_event.is_set():
                if not self.ser.is_open:
                    break
                sample = parse_payload(self._read_line())

                # open/close the raw .txt log lazily so logging can be
                # toggled while the stream keeps running
                if self._log_enabled.is_set():
                    if log_file is None:
                        with self._log_lock:
                            path = self.log_path
                        if path:
                            log_file = open(path, 'a', buffering=1,
                                            encoding='utf-8')
                elif log_file is not None:
                    log_file.close()
                    log_file = None

                if sample is None:
                    continue                    # timeout or malformed line
                if time_offset is None:
                    time_offset = sample.time_s
                true_time = sample.time_s - time_offset

                if log_file is not None:
                    log_file.write(format_log_line(sample, true_time) + '\n')
                try:
                    self.sample_queue.put_nowait((sample, true_time))
                except queue.Full:
                    # never drop silently: the GUI surfaces this counter
                    with self._dropped_lock:
                        self._dropped += 1
        except (serial.SerialException, OSError) as exc:
            self.error = str(exc)
        finally:
            if log_file is not None:
                log_file.close()


# ===========================================================================
# Application controller — owns the GUI, the sensors, the recorder and the
# acquisition thread. All methods run in the GUI thread.
# ===========================================================================
class PEMstackApp(QObject):

    def __init__(self, interface: class_gui0.MonInterface) -> None:
        super().__init__()
        self.ui = interface

        # --- sensor display buffers (rolling deques, see class_serial_plot) ---
        self.sensors = [
            sPEMstack.Sensor_PEMstack("Cell0", 0, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell1", 1, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell2", 2, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell3", 3, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell4", 4, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell5", 5, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell6", 6, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell7", 7, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell8", 8, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Cell9", 9, "voltage", 0),
            sPEMstack.Sensor_PEMstack("Psensor10", 10, "Pressure", 0),
            sPEMstack.Sensor_PEMstack("Psensor11", 11, "Pressure", 0),
            sPEMstack.Sensor_PEMstack("Psensor12", 12, "Pressure", 0),
            sPEMstack.Sensor_PEMstack("Isensor13", 13, "Current", 0),
            sPEMstack.Sensor_PEMstack("M_Fsensor14", 14, "Mass flow", 0),
            sPEMstack.Sensor_PEMstack("M_Fsensor15", 15, "Mass flow", 0),
        ]
        #timeStack = sPEMstack.Sensor_PEMstack("time",00,"time",0)
        self.u_stack = sPEMstack.Sensor_PEMstack("SignalU", 100, "U", 0)

        self.recorder = DataRecorder()
        self.settings = app_settings.load_settings()

        self.ser: "serial.Serial | None" = None
        self.serial_lock = threading.Lock()
        self.acquisition: "SerialAcquisition | None" = None
        self.sample_queue: "queue.Queue" = queue.Queue(maxsize=ACQ_QUEUE_SIZE)
        self.experiment_start: "datetime.datetime | None" = None
        self.acq_start_monotonic: "float | None" = None
        self.overflow_warned = False
        self.known_ports: list = []

        self._connect_signals()
        self._start_timers()
        self.refresh_ports()

    # ------------------------------------------------------------------ #
    # wiring                                                              #
    # ------------------------------------------------------------------ #
    def _connect_signals(self) -> None:
        self.ui.pB_read.clicked.connect(self.start_acquisition)
        self.ui.pB_close.clicked.connect(self.stop_acquisition)
        self.ui.pB_read_file.clicked.connect(self.read_file)
        self.ui.pB_U.pressed.connect(self.step_)
        self.ui.pB_pwm.pressed.connect(self.pyramid_)
        self.ui.pB_release.pressed.connect(self.release_)
        self.ui.pB_new_experiment.clicked.connect(self.new_experiment)
        self.ui.pB_settings.clicked.connect(self.open_settings)
        self.ui.pB_save_data.clicked.connect(self.save_recorder)

    def _start_timers(self) -> None:
        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.drain_queue_and_plot)
        self.plot_timer.start(GUI_REFRESH_MS)

        self.port_timer = QTimer(self)
        self.port_timer.timeout.connect(self.refresh_ports)
        self.port_timer.start(PORT_REFRESH_MS)

        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self.update_elapsed)
        self.elapsed_timer.start(ELAPSED_REFRESH_MS)

    # ------------------------------------------------------------------ #
    # COM-port handling                                                   #
    # ------------------------------------------------------------------ #
    def refresh_ports(self) -> None:
        # http://lense.institutoptique.fr/mine/python-pyserial-premier-script/
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports != self.known_ports:
            current = self.ui.comboBox_port_com.currentText()
            self.ui.comboBox_port_com.clear()
            for device in ports:
                self.ui.comboBox_port_com.addItem(device, device)
            index = self.ui.comboBox_port_com.findText(current)
            if index >= 0:
                self.ui.comboBox_port_com.setCurrentIndex(index)
            self.known_ports = ports

    # ------------------------------------------------------------------ #
    # acquisition control                                                 #
    # ------------------------------------------------------------------ #
    def is_acquiring(self) -> bool:
        return self.acquisition is not None and self.acquisition.is_alive()

    def start_acquisition(self) -> None:
        if self.is_acquiring():
            self.ui.set_status("Already acquiring", "darkorange")
            return
        if self.ui.comboBox_port_com.currentIndex() < 0:
            QMessageBox.warning(self.ui, "No serial port",
                                "No COM port selected — connect the "
                                "acquisition board and try again.")
            return

        port_name = self.ui.comboBox_port_com.currentData() \
            or self.ui.comboBox_port_com.currentText()
        try:
            self.ser = serial.Serial(port=port_name,
                                     baudrate=SERIAL_BAUDRATE,
                                     parity=serial.PARITY_NONE,
                                     stopbits=serial.STOPBITS_ONE,
                                     bytesize=serial.EIGHTBITS,
                                     timeout=SERIAL_TIMEOUT_S)
        except (serial.SerialException, OSError) as exc:
            QMessageBox.critical(self.ui, "Serial port error",
                                 f"Could not open {port_name}:\n{exc}")
            self.ui.set_status("Port open failed", "red")
            return

        str_experiment = self.ui.lineEdit.text().strip() or "experiment_1"
        log_path = str_experiment + '.txt'

        if self.experiment_start is None:
            self.experiment_start = datetime.datetime.now()
        self.acq_start_monotonic = monotonic()
        self.overflow_warned = False

        self.acquisition = SerialAcquisition(self.ser, log_path,
                                             self.sample_queue,
                                             self.serial_lock)
        self.acquisition.start()
        self.ui.set_status("Acquiring…", "green")

    def stop_acquisition(self) -> None:
        if not self.is_acquiring() and self.ser is None:
            self.ui.set_status("Idle")
            return

        if self.acquisition is not None:
            self.acquisition.stop_event.set()
            self.acquisition.join(timeout=2.0)

        # release solenoid valve + close communication, like the old close_()
        if self.ser is not None:
            try:
                with self.serial_lock:
                    if self.ser.is_open:
                        self.ser.write(b'r')  # release solenoid valve
                        self.ser.write(b'C')  # close and release
                        self.ser.close()
            except (serial.SerialException, OSError) as exc:
                QMessageBox.warning(self.ui, "Serial port error",
                                    f"Problem while closing the port:\n{exc}")
            self.ser = None

        self.acquisition = None
        self.acq_start_monotonic = None
        self.ui.set_status("Stopped")

        if self.settings.autosave_on_stop and len(self.recorder) > 0:
            self.save_recorder()

    def send_command(self, command: bytes) -> None:
        """Write one command byte to the board (guarded + error-reported)."""
        if self.ser is None:
            return
        try:
            with self.serial_lock:
                if self.ser.is_open:
                    self.ser.write(command)
        except (serial.SerialException, OSError) as exc:
            self.ui.set_status(f"Serial write failed: {exc}", "red")

    def step_(self) -> None:
        # print("case 2 : step form input to solenoid valve")
        self.send_command(b's')  # set to 255 = 22v in the solenoid valve

    def pyramid_(self) -> None:
        # print("case 5 pyramid form input to solenoid valve")
        self.send_command(b'w')  # active the pyramid form output

    def release_(self) -> None:
        # print("case 3 release solenoid valve")
        self.send_command(b'r')  # release solenoid valve

    # ------------------------------------------------------------------ #
    # consumer: queue -> buffers -> plots (GUI thread only)               #
    # ------------------------------------------------------------------ #
    def drain_queue_and_plot(self) -> None:
        got_data = False
        while True:
            try:
                sample, true_time = self.sample_queue.get_nowait()
            except queue.Empty:
                break
            got_data = True
            for i in range(N_CELLS):
                self.sensors[i].set_array_convertion(sample.cells_mv[i])
            for i in range(N_PRESSURE):
                self.sensors[10 + i].set_array_convertion(sample.pressures_kpa[i])
            self.sensors[13].set_array_convertion(sample.current_ma)
            for i in range(N_MASSFLOW):
                self.sensors[14 + i].set_array_convertion(sample.massflow_sccm[i])
            self.u_stack.set_array_convertion(sample.u_stack_v)
            self.sensors[0].setV_X(true_time)
            self.recorder.append(sample, true_time)

        if got_data:
            self.plot_data()

        if self.acquisition is not None:
            if self.acquisition.error and not self.acquisition.is_alive():
                error_text = self.acquisition.error
                self.acquisition = None
                # free the port so a new acquisition can be started
                if self.ser is not None:
                    try:
                        with self.serial_lock:
                            if self.ser.is_open:
                                self.ser.close()
                    except (serial.SerialException, OSError):
                        pass
                    self.ser = None
                self.acq_start_monotonic = None
                self.ui.set_status("Serial error", "red")
                QMessageBox.critical(self.ui, "Acquisition stopped",
                                     f"Serial communication failed:\n{error_text}")
            elif self.acquisition.dropped_samples > 0 and not self.overflow_warned:
                self.overflow_warned = True
                dropped = self.acquisition.dropped_samples
                self.ui.set_status(f"Buffer overflow! {dropped} samples dropped",
                                   "red")
                QMessageBox.warning(self.ui, "Acquisition buffer overflow",
                                    f"{dropped} samples were dropped because "
                                    "the display could not keep up. The raw "
                                    ".txt log still contains every sample.")

    def update_elapsed(self) -> None:
        if self.acq_start_monotonic is not None:
            self.ui.set_elapsed(monotonic() - self.acq_start_monotonic)

    # ------------------------------------------------------------------ #
    # plotting                                                            #
    # ------------------------------------------------------------------ #
    def plot_data(self) -> None:
        xs = np.fromiter(self.sensors[0].abscisses, dtype=float)

        def set_curve(curve, sensor) -> None:
            ys = np.fromiter(sensor.array_conv_v, dtype=float)
            if len(xs) == len(ys):
                curve.setData(xs, ys)

        for i in range(N_CELLS):
            set_curve(self.ui.curve_Cell[i], self.sensors[i])
        for i in range(N_PRESSURE):
            set_curve(self.ui.curve_Psensor[i], self.sensors[10 + i])
        set_curve(self.ui.curve_Isensor, self.sensors[13])
        for i in range(N_MASSFLOW):
            set_curve(self.ui.curve_MFsensor[i], self.sensors[14 + i])
        ys = np.fromiter(self.u_stack.array_conv_v, dtype=float)
        if len(xs) == len(ys):
            self.ui.curve_U.setData(xs, ys)

    # ------------------------------------------------------------------ #
    # "Plot file" — re-plot a finished experiment from its .txt log       #
    # ------------------------------------------------------------------ #
    def read_file(self) -> None:
        str_experiment = self.ui.lineEdit.text().strip() or "experiment_1"
        logfile = str_experiment + '.txt'
        try:
            with open(logfile, 'r', encoding='utf-8', errors='ignore') as fh:
                lines = fh.readlines()
        except OSError as exc:
            QMessageBox.warning(self.ui, "Plot file",
                                f"Could not open '{logfile}':\n{exc}")
            return

        samples: list = []
        for line in lines:
            # log lines embed the raw payload after 'data: '
            payload = line.split('data: ', 1)[-1].strip()
            sample = parse_payload(payload)
            if sample is not None:
                samples.append(sample)

        if not samples:
            QMessageBox.information(self.ui, "Plot file",
                                    f"No valid data lines found in '{logfile}'.")
            return

        time_offset = samples[0].time_s
        xs = np.array([s.time_s - time_offset for s in samples], dtype=float)

        # full-history plot: write straight to the curves (the rolling live
        # buffers stay untouched)
        for i in range(N_CELLS):
            self.ui.curve_Cell[i].setData(
                xs, np.array([s.cells_mv[i] for s in samples]))
        for i in range(N_PRESSURE):
            self.ui.curve_Psensor[i].setData(
                xs, np.array([s.pressures_kpa[i] for s in samples]))
        self.ui.curve_Isensor.setData(
            xs, np.array([s.current_ma for s in samples]))
        for i in range(N_MASSFLOW):
            self.ui.curve_MFsensor[i].setData(
                xs, np.array([s.massflow_sccm[i] for s in samples]))
        self.ui.curve_U.setData(
            xs, np.array([s.u_stack_v for s in samples], dtype=float))
        self.ui.set_status(f"Plotted file '{logfile}' ({len(samples)} samples)")

    # ------------------------------------------------------------------ #
    # New Experiment — reset in-app state without restarting the app      #
    # ------------------------------------------------------------------ #
    def new_experiment(self) -> None:
        result = QMessageBox.question(
            self.ui, "New Experiment",
            "Start new experiment? Unsaved data will be lost.",
            QMessageBox.Yes | QMessageBox.No)
        if result != QMessageBox.Yes:
            return

        # 1. stop any running acquisition (without auto-saving: the user
        #    just confirmed the data may be discarded)
        autosave = self.settings.autosave_on_stop
        self.settings.autosave_on_stop = False
        try:
            self.stop_acquisition()
        finally:
            self.settings.autosave_on_stop = autosave

        # 2. clear in-memory buffers and plot traces (files on disk untouched)
        while True:
            try:
                self.sample_queue.get_nowait()
            except queue.Empty:
                break
        for sensor in self.sensors:
            sensor.clear_all()
        self.u_stack.clear_all()
        self.recorder.clear()
        self.ui.clean_g()

        # 3. reset elapsed time and status indicators
        self.experiment_start = None
        self.acq_start_monotonic = None
        self.overflow_warned = False
        self.ui.reset_indicators()

    # ------------------------------------------------------------------ #
    # Settings + saving                                                   #
    # ------------------------------------------------------------------ #
    def open_settings(self) -> None:
        dialog = class_gui0.SettingsDialog(self.settings, self.ui)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        self.settings = dialog.result_settings()
        try:
            app_settings.save_settings(self.settings)
        except OSError as exc:
            QMessageBox.warning(self.ui, "Settings",
                                f"Could not write settings.json:\n{exc}")

    def save_recorder(self) -> None:
        """Save the recorded experiment using the auto-naming settings."""
        if len(self.recorder) == 0:
            self.ui.set_status("Nothing to save (no recorded data)", "darkorange")
            return

        folder = self.settings.save_folder
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self.ui, "Save failed",
                                 f"Cannot create save folder '{folder}':\n{exc}")
            return

        start = self.experiment_start or datetime.datetime.now()
        base_name = app_settings.build_filename(self.settings.name_template, start)
        fmt = self.settings.file_format
        saved: list = []

        if fmt in ("CSV", "both"):
            path = app_settings.unique_path(folder, base_name, "csv")
            try:
                self.recorder.save_csv(path)
                saved.append(os.path.basename(path))
            except OSError as exc:
                QMessageBox.critical(self.ui, "Save failed",
                                     f"Could not write CSV:\n{exc}")

        if fmt in ("HDF5", "both"):
            path = app_settings.unique_path(folder, base_name, "h5")
            try:
                self.recorder.save_hdf5(path)
                saved.append(os.path.basename(path))
            except ImportError:
                QMessageBox.warning(
                    self.ui, "HDF5 not available",
                    "The optional 'h5py' package is not installed "
                    "(pip install h5py).\nSaving as CSV instead.")
                if fmt == "HDF5":     # don't double-save in 'both' mode
                    path = app_settings.unique_path(folder, base_name, "csv")
                    self.recorder.save_csv(path)
                    saved.append(os.path.basename(path))
            except OSError as exc:
                QMessageBox.critical(self.ui, "Save failed",
                                     f"Could not write HDF5:\n{exc}")

        if saved:
            self.ui.set_status(f"Saved: {', '.join(saved)}", "green")

    # ------------------------------------------------------------------ #
    # shutdown                                                            #
    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        if self.acquisition is not None:
            self.acquisition.stop_event.set()
            self.acquisition.join(timeout=2.0)
            self.acquisition = None
        if self.ser is not None:
            try:
                with self.serial_lock:
                    if self.ser.is_open:
                        self.ser.write(b'r')   # release solenoid valve
                        self.ser.write(b'C')   # write a close communication  command code ascii 67 = C
                        self.ser.close()
            except (serial.SerialException, OSError):
                pass
            self.ser = None


#-------------------------------- main
def main_() -> int:
    """Launch the redesigned three-zone main window (ui_main.py)."""
    # imported here to avoid a circular import (ui_main imports this module)
    import ui_main

    app = QApplication(sys.argv)
    window = ui_main.PEMstackMainWindow()
    window.show()
    exit_code = app.exec_()
    window.shutdown()
    sys.stdout.flush()
    return exit_code


def legacy_main_() -> int:
    """Launch the pre-redesign interface (MonInterface + PEMstackApp),
    kept intact as a fallback during the UI transition."""
    app = QApplication(sys.argv)
    mon_interface = class_gui0.MonInterface()
    controller = PEMstackApp(mon_interface)
    exit_code = app.exec_()
    controller.shutdown()
    sys.stdout.flush()
    return exit_code


if __name__ == '__main__':
    sys.exit(main_())
