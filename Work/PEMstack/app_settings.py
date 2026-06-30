#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app_settings.py
===============
Persistent application settings for the PEMstack acquisition GUI.

Settings are stored in a `settings.json` file next to this module so they
survive between sessions. Only the Python standard library is used.

Auto-naming
-----------
Saved files are named from a user-editable template. Placeholders refer to
the *start* date/time of the experiment (case-sensitive, strftime-like):

    {HH}   hour, 2 digits (00-23)
    {mm}   minute, 2 digits          (lowercase = minutes!)
    {DD}   day of month, 2 digits
    {MM}   month, 2 digits           (uppercase = month!)
    {YYYY} year, 4 digits

Default template:  Experiment_{HH}h{mm}_{DD}-{MM}-{YYYY}
Example output:    Experiment_14h32_12-06-2026.csv

A free-text prefix can simply be typed into the template, e.g.
    MEA5_Experiment_{HH}h{mm}_{DD}-{MM}-{YYYY}

If the generated name already exists on disk, `_v2`, `_v3`, ... is appended
instead of silently overwriting.
"""

import dataclasses
import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

# settings.json lives next to the application files
SETTINGS_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "settings.json")

# Default save folder: ~/work/data  (the `data` folder next to PEMstack/)
DEFAULT_SAVE_FOLDER: str = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
)

DEFAULT_NAME_TEMPLATE: str = "Experiment_{HH}h{mm}_{DD}-{MM}-{YYYY}"

# File format choices shown in the Settings dialog dropdown
FILE_FORMATS = ("CSV", "HDF5", "both")

# Display font presets: name -> (data pt, heading pt). All data fonts are
# >= 11 pt and headings >= 13 pt so text stays readable at arm's length.
FONT_PRESETS: Dict[str, Tuple[int, int]] = {
    "Small": (11, 13),
    "Medium": (12, 14),
    "Large": (14, 17),
}

# Validation bounds (shared between the dialog and load-time sanitising)
SAMPLING_RATE_RANGE = (1, 10000)
BUFFER_SIZE_RANGE = (100, 1_000_000)
TIME_WINDOW_RANGE = (10, 600)
PLOT_REFRESH_RANGE = (1, 30)

# Characters Windows/Unix forbid in file names (template validation)
INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def _clamp(value: int, bounds: Tuple[int, int]) -> int:
    return max(bounds[0], min(bounds[1], value))


@dataclasses.dataclass
class AppSettings:
    """All persisted user settings (one attribute per Settings-panel row)."""
    # --- Save tab ---
    save_folder: str = DEFAULT_SAVE_FOLDER
    name_template: str = DEFAULT_NAME_TEMPLATE
    file_format: str = "CSV"            # one of FILE_FORMATS
    autosave_on_stop: bool = True
    include_header: bool = True

    # --- Acquisition tab ---
    sampling_rate_hz: int = 100         # requested board rate (informational
                                        # unless the firmware honours it)
    buffer_size: int = 10000            # live plot buffer length [samples]
    # Per-sensor maps keyed by SensorDef.key; a missing key means
    # "enabled" / "offset 0.0" / "no thresholds".
    sensor_enabled: Dict[str, bool] = dataclasses.field(default_factory=dict)
    calibration_offsets: Dict[str, float] = dataclasses.field(default_factory=dict)
    warning_thresholds: Dict[str, List[float]] = dataclasses.field(default_factory=dict)

    # --- Display tab ---
    time_window_s: int = 60
    plot_refresh_hz: int = 10
    dark_mode: bool = False
    show_minmax_on_cards: bool = True
    font_size: str = "Medium"           # one of FONT_PRESETS

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    # ------------------------------------------------------------------ #
    # per-sensor convenience accessors                                    #
    # ------------------------------------------------------------------ #
    def is_sensor_enabled(self, key: str) -> bool:
        return bool(self.sensor_enabled.get(key, True))

    def offset_for(self, key: str) -> float:
        try:
            return float(self.calibration_offsets.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    def thresholds_for(self, key: str) -> "Optional[Tuple[float, float]]":
        pair = self.warning_thresholds.get(key)
        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(isinstance(v, (int, float)) for v in pair)):
            return (float(pair[0]), float(pair[1]))
        return None

    def fonts(self) -> Tuple[int, int]:
        """Return (data_pt, heading_pt) for the selected font size."""
        return FONT_PRESETS.get(self.font_size, FONT_PRESETS["Medium"])


def _sanitise(settings: AppSettings) -> AppSettings:
    """Clamp/repair values that came from an edited or stale settings.json."""
    if settings.file_format not in FILE_FORMATS:
        settings.file_format = "CSV"
    if settings.font_size not in FONT_PRESETS:
        settings.font_size = "Medium"
    settings.sampling_rate_hz = _clamp(int(settings.sampling_rate_hz),
                                       SAMPLING_RATE_RANGE)
    settings.buffer_size = _clamp(int(settings.buffer_size), BUFFER_SIZE_RANGE)
    settings.time_window_s = _clamp(int(settings.time_window_s),
                                    TIME_WINDOW_RANGE)
    settings.plot_refresh_hz = _clamp(int(settings.plot_refresh_hz),
                                      PLOT_REFRESH_RANGE)
    if not settings.name_template.strip():
        settings.name_template = DEFAULT_NAME_TEMPLATE
    return settings


def load_settings(path: Optional[str] = None) -> AppSettings:
    """Load settings from JSON; fall back to defaults on any problem."""
    path = SETTINGS_PATH if path is None else path
    settings = AppSettings()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return settings           # missing or corrupt file -> defaults

    if not isinstance(raw, dict):
        return settings
    for field in dataclasses.fields(AppSettings):
        value = raw.get(field.name)
        if isinstance(value, type(getattr(settings, field.name))):
            setattr(settings, field.name, value)
    return _sanitise(settings)


def save_settings(settings: AppSettings, path: Optional[str] = None) -> None:
    """Write settings to JSON (raises OSError to the caller on failure)."""
    path = SETTINGS_PATH if path is None else path
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings.to_dict(), fh, indent=2)


def settings_file_exists(path: Optional[str] = None) -> bool:
    """True when settings.json already exists (used for first-run detection)."""
    path = SETTINGS_PATH if path is None else path
    return os.path.exists(path)


def build_filename(template: str, start_time: datetime.datetime) -> str:
    """Expand the auto-name template with the experiment *start* time."""
    return (template
            .replace("{HH}", f"{start_time.hour:02d}")
            .replace("{mm}", f"{start_time.minute:02d}")
            .replace("{DD}", f"{start_time.day:02d}")
            .replace("{MM}", f"{start_time.month:02d}")
            .replace("{YYYY}", f"{start_time.year:04d}"))


def template_error(template: str) -> Optional[str]:
    """Return a human-readable problem with the template, or None if valid."""
    if not template.strip():
        return "Template must not be empty."
    expanded = build_filename(template, datetime.datetime.now())
    bad = sorted({c for c in expanded if c in INVALID_FILENAME_CHARS})
    if bad:
        return f"Invalid filename character(s): {' '.join(bad)}"
    return None


def unique_path(folder: str, base_name: str, extension: str) -> str:
    """
    Return `folder/base_name.extension`; if it already exists, append
    `_v2`, `_v3`, ... until the name is free (never overwrite silently).
    """
    candidate = os.path.join(folder, f"{base_name}.{extension}")
    version = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base_name}_v{version}.{extension}")
        version += 1
    return candidate
