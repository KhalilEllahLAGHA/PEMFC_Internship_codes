#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from PyQt5 import QtGui, QtCore, uic, QtWidgets
from PyQt5.QtWidgets import QMainWindow, QApplication, QGraphicsScene
from PyQt5.QtCore import *
import gui0 as ihm
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui
import time
import numpy as np

import app_settings

pg.setConfigOption('background', (200,200,200)) # couleur de fond du graphique
pg.setConfigOption('foreground', 'k')

# couleur des axes

#pg.setConfigOptions(antialias=True)


class SettingsDialog(QtWidgets.QDialog):
    """
    Settings panel (File saving): save folder, auto-name template,
    file format and auto-save on Stop. Persisted via app_settings.
    """
    def __init__(self, settings: "app_settings.AppSettings", parent=None) -> None:
        super(SettingsDialog, self).__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)

        form = QtWidgets.QFormLayout(self)

        # --- Save folder (line edit + Browse button) ---
        folder_row = QtWidgets.QHBoxLayout()
        self.folder_edit = QtWidgets.QLineEdit(settings.save_folder)
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(browse_btn)
        form.addRow("Save folder:", folder_row)

        # --- Auto-name template ---
        self.template_edit = QtWidgets.QLineEdit(settings.name_template)
        self.template_edit.setToolTip(
            "Placeholders (experiment START time):\n"
            "{HH}=hour  {mm}=minutes  {DD}=day  {MM}=month  {YYYY}=year\n"
            "Free text is kept as-is, e.g. MEA5_Experiment_{HH}h{mm}_{DD}-{MM}-{YYYY}"
        )
        form.addRow("Auto-name template:", self.template_edit)

        # --- File format ---
        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(list(app_settings.FILE_FORMATS))
        index = self.format_combo.findText(settings.file_format)
        if index >= 0:
            self.format_combo.setCurrentIndex(index)
        form.addRow("File format:", self.format_combo)

        # --- Auto-save on Stop ---
        self.autosave_check = QtWidgets.QCheckBox("Save automatically when acquisition stops")
        self.autosave_check.setChecked(settings.autosave_on_stop)
        form.addRow("Auto-save on Stop:", self.autosave_check)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose save folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def result_settings(self) -> "app_settings.AppSettings":
        """Return an AppSettings reflecting the dialog fields."""
        return app_settings.AppSettings(
            save_folder=self.folder_edit.text().strip(),
            name_template=self.template_edit.text().strip()
                          or app_settings.DEFAULT_NAME_TEMPLATE,
            file_format=self.format_combo.currentText(),
            autosave_on_stop=self.autosave_check.isChecked(),
        )


class MonInterface(QtWidgets.QMainWindow, ihm.Ui_Form):
    """
    Dessine l'interface graphique
    """
    def __init__(self):
        super(MonInterface, self).__init__()
        self.setupUi(self)

        #self.showFullScreen()
        #self.displayGraph()
        self.graphicsView.addLegend()
        self.graphicsView_2.addLegend()
        self.graphicsView_3.addLegend()
        self.graphicsView_4.addLegend()
        self.graphicsView_5.addLegend()

        self.graphicsView.showGrid(x=True, y=True)
        self.graphicsView_2.showGrid(x=True, y=True)
        self.graphicsView_3.showGrid(x=True, y=True)
        self.graphicsView_4.showGrid(x=True, y=True)
        self.graphicsView_5.showGrid(x=True, y=True)


        self.graphicsView.setLabel('left', "Voltage", units = 'mV' )
        self.graphicsView.setLabel('bottom', "Time", units='s')

        self.graphicsView_2.setLabel('left', "Pressure", units = 'kPa' )
        self.graphicsView_2.setLabel('bottom', "Time", units='s')

        self.graphicsView_3.setLabel('left', "Mass flow", units = 'SCCM' )
        self.graphicsView_3.setLabel('bottom', "Time", units='s')

        self.graphicsView_4.setLabel('left', "Current", units = 'A' )
        self.graphicsView_4.setLabel('bottom', "Time", units='s')

        self.graphicsView_5.setLabel('left', "Amplitud", units = 'V' )
        self.graphicsView_5.setLabel('bottom', "Time", units='s')


        #self.array_name = np.array(['Cell0','Cell1','Cell2','Cell3','Cell4','Cell5','Cell6','Cell7','Cell8','Cell9','Psensor10','Psensor11','Psensor12','Isensor13','M_Fsensor14','M_Fsensor15'])
        #self.array_color= np.array(['b','g','r','c','m','y','k','w']) #,'','','','','','',''])

        self.curve_Cell=np.array([self.graphicsView.plot(pen='b',name='Cell0'),self.graphicsView.plot(pen='g',name='Cell1'),self.graphicsView.plot(pen='r',name='Cell2'),self.graphicsView.plot(pen='c',name='Cell3'),self.graphicsView.plot(pen='m',name='Cell4'),self.graphicsView.plot(pen='y',name='Cell5'),self.graphicsView.plot(pen='k',name='Cell6'),self.graphicsView.plot(pen='w',name='Cell7'),self.graphicsView.plot(pen='b', symbol='o', symbolSize = 3 ,name='Cell8'),self.graphicsView.plot(pen='g', symbol='o', symbolSize = 3,name='Cell9')])

        self.curve_Psensor=np.array([self.graphicsView_2.plot(pen='b',name='Psensor10'),self.graphicsView_2.plot(pen='m',name='Psensor11'),self.graphicsView_2.plot(pen='r',name='Psensor12')])
        self.curve_MFsensor=np.array([self.graphicsView_3.plot(pen='b',name='M_Fsensor14'),self.graphicsView_3.plot(pen='r',name='M_Fsensor15')])
        self.curve_Isensor=self.graphicsView_4.plot(pen='b',name='Isensor13')

        self.curve_U=self.graphicsView_5.plot(pen='r',name='Step')

        # ------------------------------------------------------------------
        # Extra widgets added in code (gui0.py is generated by pyuic5 and
        # must not be edited by hand): New Experiment + Settings buttons in
        # the bottom button row, status + elapsed-time labels next to the
        # experiment-name field.
        # ------------------------------------------------------------------
        self.layoutWidget.setGeometry(QtCore.QRect(30, 839, 1310, 27))
        self.pB_new_experiment = QtWidgets.QPushButton("New Experiment", self.layoutWidget)
        self.pB_new_experiment.setObjectName("pB_new_experiment")
        self.horizontalLayout.addWidget(self.pB_new_experiment)
        self.pB_save_data = QtWidgets.QPushButton("Save data", self.layoutWidget)
        self.pB_save_data.setObjectName("pB_save_data")
        self.horizontalLayout.addWidget(self.pB_save_data)
        self.pB_settings = QtWidgets.QPushButton("⚙ Settings", self.layoutWidget)
        self.pB_settings.setObjectName("pB_settings")
        self.horizontalLayout.addWidget(self.pB_settings)

        self.layoutWidget1.setGeometry(QtCore.QRect(40, 800, 900, 27))
        self.label_status = QtWidgets.QLabel("Status: Idle", self.layoutWidget1)
        self.label_status.setObjectName("label_status")
        self.horizontalLayout_2.addWidget(self.label_status)
        self.label_elapsed = QtWidgets.QLabel("Elapsed: 0 s", self.layoutWidget1)
        self.label_elapsed.setObjectName("label_elapsed")
        self.horizontalLayout_2.addWidget(self.label_elapsed)

        #self.curve= self.graphicsView.plot(pen='r')
        self.show()
        #self.setMouseMode(self.RectMode)

    # ---------------------------------------------------------------------
    # Status indicators
    # ---------------------------------------------------------------------
    def set_status(self, text: str, colour: str = "black") -> None:
        self.label_status.setText(f"Status: {text}")
        self.label_status.setStyleSheet(f"color: {colour};")

    def set_elapsed(self, seconds: float) -> None:
        self.label_elapsed.setText(f"Elapsed: {int(seconds)} s")

    def reset_indicators(self) -> None:
        """Back to the initial state (used by New Experiment)."""
        self.set_status("Idle")
        self.set_elapsed(0)


    def clean_g(self):
        for i in range(10):
            self.curve_Cell[i].update()
            self.curve_Cell[i].clear()
            #self.curve_Cell[i].items().clear()
        for i in range(3):
            self.curve_Psensor[i].update()
            self.curve_Psensor[i].clear()
            #self.curve_Psensor[i].items().clear()
        for i in range(2):
            self.curve_MFsensor[i].update()
            self.curve_MFsensor[i].clear()
            #self.curve_MFsensor[i].items().clear()

        self.curve_Isensor.update()
        self.curve_Isensor.clear()
        #self.curve_Isensor.items().clear()

        self.curve_U.update()
        self.curve_U.clear()


    ## reimplement right-click to zoom out
    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.RightButton:
            self.autoRange()

    ## reimplement mouseDragEvent to disable continuous axis zoom
    def mouseDragEvent(self, ev, axis=None):
        if axis is not None and ev.button() == QtCore.Qt.MouseButton.RightButton:
            ev.ignore()
        else:
            pg.ViewBox.mouseDragEvent(self, ev, axis=axis)



    def closeEvent(self, event): # ajoute une boite de dialogue pour confirmation de fermeture
        result = QtWidgets.QMessageBox.question(self,
        "Confirm Exit...",
        "Are you sure you want to exit ?",
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if result == QtWidgets.QMessageBox.Yes:
           event.accept()
        else:
           event.ignore()
