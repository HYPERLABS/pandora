import os
import re
import click
import grpc
from generated.nitrogen_public_pb2_grpc import NitrogenStub
import generated.nitrogen_public_pb2 as nitrogen_public_pb2
from generated.radium_public_pb2_grpc import RadiumStub
import generated.radium_public_pb2 as radium_public_pb2
import pyqtgraph as pg
from pyqtgraph.Qt import QtWidgets, QtCore, QtGui
from typing import Callable, Optional, override
from service_browser import ServiceBrowserDialog
import numpy as np
from enum import Enum
import datetime
import logging
import time
import threading
import queue
import sys


try:
    # On windows, set process user mode in order to get our icon on the taskbar.
    from ctypes import windll
    myappid = 'com.hyperlabs.PyMTDR._._'
    windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except ImportError:
    pass


def make_label(text: str, bold: bool = True, tooltip: Optional[str] = None):
    '''Helper to make labels.'''
    label = QtWidgets.QLabel(text)
    if bold:
        font = label.font()
        font.setBold(True)
        label.setFont(font)
    if tooltip:
        label.setToolTip(tooltip)
    return label


class PlotViewBox(pg.ViewBox):
    '''Custom viewbox for the plot for customizations.'''
    def __init__(self, parent=None,
                 on_get_zoom_axis: Optional[Callable[[], str]] = None,
                 on_move_cursors: Optional[Callable[[QtCore.QPointF], None]] = None,
                 on_is_cursor_selected: Optional[Callable[[], bool]] = None,
                 on_is_connected: Optional[Callable[[], bool]] = None,
                 on_import: Optional[Callable[[], None]] = None
                 ):
        super().__init__(parent)
        self._on_get_zoom_axis = on_get_zoom_axis
        self._on_move_cursors = on_move_cursors
        self._on_is_cursor_selected = on_is_cursor_selected
        self._on_is_connected = on_is_connected
        self._on_import = on_import

    @override
    def wheelEvent(self, ev: QtGui.QWheelEvent, axis: Optional[str] = None):
        '''Override the default wheelevent to take into account the zoom axis'''
        if self._on_get_zoom_axis is None:
            ev.accept()
            return
        
        # Get axis to apply zoom on.
        axis = self._on_get_zoom_axis()

        # Determine scale factor.
        zoom_in = ev.delta() > 0
        factor = 0.9 if zoom_in else 1.1

        # Get mouse position in data coordinates
        mouse_point = self.mapSceneToView(ev.scenePos())
        x, y = mouse_point.x(), mouse_point.y()

        # Apply scale.
        if axis == "none": pass
        elif axis == "x": self.scaleBy((factor, 1))
        elif axis == "y": self.scaleBy((1, factor))
        else: self.scaleBy((factor, factor))

        # After scaling, apply offset so mouse stays at the same data point.
        mouse_point_after = self.mapSceneToView(ev.scenePos())
        dx = x - mouse_point_after.x()
        dy = y - mouse_point_after.y()
        self.translateBy((dx, dy))

        # Done
        ev.accept()

    @override
    def raiseContextMenu(self, ev):
        '''Override the default context menu for customizations'''
        menu = self.getMenu(ev)
        if menu is not None:
            # Remove and rename some actions.
            move_selected_cursors_action_text = "Move Selected Cursors Here"
            import_action_text = "Import..."
            add_actions_before_this_action_in_context_menu = QtGui.QAction()
            for action in menu.actions():
                if action.text() == "Mouse Mode": menu.removeAction(action)
                if action.text() == "View All": action.setText("Auto Range")
                if action.text() == "Auto Range": action.setText("Auto Range")
                if action.text() == "X axis": add_actions_before_this_action_in_context_menu = action
                if action.text() == move_selected_cursors_action_text: menu.removeAction(action)
                if action.text() == import_action_text: menu.removeAction(action)

            # Add a custom action to move the selected cursors.
            if self._on_is_cursor_selected is not None and self._on_is_cursor_selected():
                if self._on_move_cursors is not None:
                    action = QtGui.QAction(move_selected_cursors_action_text, menu)
                    mouse_point: QtCore.QPointF = self.mapSceneToView(ev.scenePos())
                    if mouse_point is not None:
                        action.triggered.connect(lambda: self._on_move_cursors(mouse_point))  # type: ignore
                        menu.insertAction(add_actions_before_this_action_in_context_menu, action)

            # Add a custom action to import a waveform.
            if self._on_is_connected is not None and self._on_import is not None:
                if not self._on_is_connected():
                    action = QtGui.QAction(import_action_text, menu)
                    action.triggered.connect(self._on_import)
                    menu.insertAction(add_actions_before_this_action_in_context_menu, action)

            # Popup the menu.
            scene = self.scene()
            assert scene is not None
            scene.addParentContextMenus(self, menu, ev)
            menu.popup(ev.screenPos().toPoint())


class PlotGraphicsLayoutWidget(pg.GraphicsLayoutWidget):
    '''Custom graphics layout widget with a label positioned at the bottom right.'''
    def __init__(self, br_label: QtWidgets.QLabel, *args, **kwargs):
        self._br_label = br_label
        super().__init__(*args, **kwargs)
        self._br_label.setParent(self)
        self._br_label.show()
        self._br_label.raise_()

    @override
    def resizeEvent(self, ev):
        '''Override resizeEvent() method to move the label.'''
        x = self.width() - self._br_label.width() - 12
        y = self.height() - self._br_label.height() - 12
        self._br_label.move(x, y)
        super().resizeEvent(ev)


# pyright: reportAttributeAccessIssue=false
class AppWindow(QtWidgets.QMainWindow):
    '''Main Application class.'''
    class _DisplayMode(Enum):
        S_N = 0
        IMPEDANCE = 1
        IMPEDANCE_LOG_SCALE = 2
        RHO = 3

    class _DisplayValue:
        def __init__(self, mode: 'AppWindow._DisplayMode', label: str):
            self.mode = mode
            self.label = label

    class _CursorBehaviorWhenClipped(Enum):
        USE_MIN = 0
        USE_MAX = 1

    _CSV_RECORD_HEADER = "Time[sec],Sn"
    _radium_disconnect_signal = QtCore.pyqtSignal()
    _radium_state_signal = QtCore.pyqtSignal(radium_public_pb2.GetStateReply)
    _radium_led_id_state_signal = QtCore.pyqtSignal(radium_public_pb2.LedState)
    _radium_sample_stream_signal = QtCore.pyqtSignal()

    def __init__(self, logger: logging.Logger, connection: Optional[str] = None):
        super().__init__()

        # Connect signals to slots.
        self._radium_disconnect_signal.connect(self._ui_on_disconnected)
        self._radium_state_signal.connect(self._update_based_on_radium_state)
        self._radium_led_id_state_signal.connect(self._update_led_id_state)
        self._radium_sample_stream_signal.connect(self._pop_sample_stream_queue)

        # Connection independent state initialization.
        self._radium_sample_stream_lock = threading.Lock()
        self._radium_state = None
        self._paused = False
        self._connected = False
        self._last_displayed_sample_stream = None
        self._zoom_axis = "both"
        self._zoom_axis_store = self._zoom_axis
        self._y_axis_is_in_log_scale = False
        self._y_display = self._DisplayValue(self._DisplayMode.S_N, "Sn")
        self._logger = logger
        self._service_browser_dialog = ServiceBrowserDialog(self, on_connect=self._on_connect, service_type="_workstation._tcp.local.", refresh_ms=1000)
        self._red_led_state = None
        self._last_x_as_array = None
        self._last_y_as_array = None

        # Window.
        self.setWindowTitle("PyMTDR")
        self.resize(1680, 720)
        pg.setConfigOptions(useOpenGL=True, antialias=True)

        # Central widget which uses a custom graphicsLayout with a PlotItem and the refresh rate label used in a custom view box.
        self._refresh_rate_label = QtWidgets.QLabel("--Hz")
        self._refresh_rate_label.setStyleSheet("background: transparent; color: white; font-weight: bold;")
        self._refresh_rate_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignBottom)
        self._refresh_rate_label.setFixedHeight(24)
        self._refresh_rate_label.setFixedWidth(84)
        self._glw = PlotGraphicsLayoutWidget(self._refresh_rate_label)
        self.setCentralWidget(self._glw)
        self._main_plot_viewbox = PlotViewBox(
            on_get_zoom_axis=lambda: self._zoom_axis,
            on_move_cursors=self._move_cursor_to_intersection,
            on_is_cursor_selected=self._is_cursor_selected,
            on_is_connected=lambda: self._connected,
            on_import=self._import
        )
        self._main_plot: pg.PlotItem = self._glw.addPlot(viewBox=self._main_plot_viewbox)
        self._axis_map = {"bottom": pg.ViewBox.XAxis, "left": pg.ViewBox.YAxis}
        self._main_plot.getAxis("left").enableAutoSIPrefix(False)
        self._main_plot.getAxis("bottom").enableAutoSIPrefix(False)
        self._main_plot.setLabels(left=(self._y_display.label, ""), bottom=("Time", "ps"))
        self._main_plot.showGrid(x=True, y=True, alpha=0.3)
        self._main_plot.setContextMenuActionVisible(name="Transforms", visible=False)
        self._main_plot.setContextMenuActionVisible(name="Downsample", visible=False)
        self._main_plot.setContextMenuActionVisible(name="Average", visible=False)
        self._main_plot.setContextMenuActionVisible(name="Alpha", visible=False)
        self._main_plot.setContextMenuActionVisible(name="Points", visible=False)
        self._main_plot.setContextMenuActionVisible(name="Grid", visible=True)
        self._main_plot.ctrl.logXCheck.hide()
        self._main_plot.ctrl.fftCheck.hide()
        self._main_plot.ctrl.derivativeCheck.hide()
        self._main_plot.ctrl.phasemapCheck.hide()
        self._main_plot.ctrl.logYCheck.setDisabled(True)
        self._main_plot.ctrl.logYCheck.toggled.disconnect()
        self._main_plot.ctrl.logYCheck.toggled.connect(self._on_plot_update_y_log_mode)
        self._main_plot.updateButtons = (lambda: None)
        self._main_plot.setYRange(0, 1)
        self._main_plot.setXRange(0, 16000, padding=0)

        # Override autorange 'A' button behavior for both axes.
        auto_btn = self._main_plot.autoBtn
        assert auto_btn is not None
        auto_btn.clicked.disconnect()
        auto_btn.clicked.connect(self._one_time_autorange)
        auto_btn.setVisible(True)

        # Toolbar widget.
        toolbar = QtWidgets.QToolBar("Controls", self)
        toolbar.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.PreventContextMenu)
        toolbar_group_label_alignment = QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignHCenter
        self.addToolBar(toolbar)

        # Device group - service browser and identify buttons.
        self._service_browser_btn = QtWidgets.QPushButton("✅") if self._connected else QtWidgets.QPushButton("🚫")
        self._service_browser_btn.setFixedWidth(40)
        self._service_browser_btn.clicked.connect(lambda: self._init_connection())
        self._identify_device_btn = QtWidgets.QPushButton("⚫")
        self._identify_device_btn_tooltip = {
            "no_connect": "Device identification is not possible without a connection",
            "toggle": "Toggle device LED"
        }
        self._identify_device_btn.setFixedWidth(40)
        self._identify_device_btn.clicked.connect(self._on_toggle_radium_led_id)
        group_widget = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_widget)
        group_layout.addWidget(make_label(text="Device", bold=True, tooltip="Device connection and identification"), alignment=toolbar_group_label_alignment)
        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.addWidget(self._service_browser_btn)
        controls_layout.addWidget(self._identify_device_btn)
        controls_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        group_layout.addLayout(controls_layout)
        toolbar.addWidget(group_widget)
        toolbar.addSeparator()

        # Acquisition group - start_stop, play/pause and record buttons.
        self._start_stop_acquisition_tooltip = {
            "no_connect": "Acquisition cannot be started without a connection",
            "start": "Not acquiring, click to start acquisition",
            "stop": "Acquiring, click to stop acquisition",
            "start/stop": "Start/Stop acquisition",
            "no_tdr_preset": "This function is not available when the TDR preset is not selected",
            "display_paused": "This function is not available when the display plot is paused"
        }
        self._start_stop_btn = QtWidgets.QPushButton("🎬")
        self._start_stop_btn.setFixedWidth(50)
        self._start_stop_btn.clicked.connect(self._on_start_stop_clicked)
        self._play_pause_plot_updates_btn = QtWidgets.QPushButton("▶️")
        self._play_pause_plot_updates_btn_tooltip = {
            "no_connect": "Display plot update control function is not available without a connection",
            "pause": "Display plot updates are enabled, click to pause display plot updates",
            "resume": "Display plot updates are disabled, click to pesume display plot updates",
        }
        self._play_pause_plot_updates_btn.setFixedWidth(40)
        self._play_pause_plot_updates_btn.clicked.connect(self._on_play_pause_plot_updates)
        self._record_btn = QtWidgets.QPushButton("⏺️")
        self._record_btn_tooltip = {
            "no_connect": "The record function is not available without a connection",
            "record": "Click to record data",
            "not_acquiring": "This function is not available when acquisition has not been started"
        }
        self._record_btn.setFixedWidth(40)
        self._record_btn.clicked.connect(self._on_record_state_change)
        group_widget = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_widget)
        group_layout.addWidget(make_label(text="Acquisition", bold=True, tooltip="Acquisition controls"), alignment=toolbar_group_label_alignment)
        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.addWidget(self._start_stop_btn)
        controls_layout.addWidget(self._play_pause_plot_updates_btn)
        controls_layout.addWidget(self._record_btn)
        controls_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        group_layout.addLayout(controls_layout)
        toolbar.addWidget(group_widget)
        toolbar.addSeparator()

        # Configuration group - TDR preset dropdown.
        # Note that updates will be pushed to the widget upon radium state events.
        # If the current radium state is not using a preset, the text in self._tdr_cfg_preset_none_txt will be display.
        # This text is otherwise hidden and will not be displayed.
        self._tdr_cfg_preset_tooltip = {
            "preset": "TDR Preset (Pulse Period/Sample Spacing)",
            "display_paused" : "This function is not available when the display plot is paused"
        }
        self._tdr_cfg_preset_combo = QtWidgets.QComboBox()
        self._tdr_cfg_preset_combo.setToolTip(self._tdr_cfg_preset_tooltip["preset"])
        self._tdr_cfg_preset_none_txt = "--"
        self._tdr_cfg_preset_custom_txt = "Custom"
        self._tdr_cfg_preset_map = {
            self._tdr_cfg_preset_none_txt: None,
            self._tdr_cfg_preset_custom_txt: None,
            "3.2ns/0.2ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_3P2_NS_SAMPLE_SPACING_0P2_PS,
            "6.4ns/0.4ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_6P4_NS_SAMPLE_SPACING_0P4_PS,
            "12.8ns/0.8ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_12P8_NS_SAMPLE_SPACING_0P8_PS,
            "16.0ns/1.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_16P0_NS_SAMPLE_SPACING_1P0_PS,
            "32.0ns/2.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_32P0_NS_SAMPLE_SPACING_2P0_PS,
            "64.0ns/4.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_64P0_NS_SAMPLE_SPACING_4P0_PS,
            "80.0ns/5.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_80P0_NS_SAMPLE_SPACING_5P0_PS,
            "128.0ns/8.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_128P0_NS_SAMPLE_SPACING_8P0_PS,
            "160.0ns/10.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_160P0_NS_SAMPLE_SPACING_10P0_PS,
            "16.0ns/50.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_16P0_NS_SAMPLE_SPACING_50P0_PS,
            "16.0ns/100.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_16P0_NS_SAMPLE_SPACING_100P0_PS
        }
        self._tdr_cfg_preset_combo.addItems(list(self._tdr_cfg_preset_map.keys()))
        self._tdr_cfg_preset_combo.setCurrentIndex(list(self._tdr_cfg_preset_map).index(self._tdr_cfg_preset_none_txt))
        self._tdr_cfg_preset_combo.view().setRowHidden(0, True)  # type: ignore
        self._tdr_cfg_preset_combo.view().setRowHidden(1, True)  # type: ignore
        def on_tdr_cfg_preset_combo_index_changed(idx):
            self._tdr_cfg_preset_combo.view().setRowHidden(0, idx != 0)  # type: ignore
            self._tdr_cfg_preset_combo.view().setRowHidden(1, idx != 1)  # type: ignore
        self._tdr_cfg_preset_combo.currentIndexChanged.connect(on_tdr_cfg_preset_combo_index_changed)
        self._tdr_cfg_preset_combo.currentTextChanged.connect(self._on_tdr_preset_changed)
        group_widget = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_widget)
        group_layout.addWidget(make_label(text="TDR Preset", bold=True, tooltip=self._tdr_cfg_preset_tooltip["preset"]), alignment=toolbar_group_label_alignment)
        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.addWidget(self._tdr_cfg_preset_combo)
        controls_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        group_layout.addLayout(controls_layout)
        toolbar.addWidget(group_widget)
        toolbar.addSeparator()

        # Display group - display select and average dropdowns + clear display button.
        self._display_select_tooltip = {
            "select": "Display select",
        }
        self._display_select_combo = QtWidgets.QComboBox()
        self._display_select_combo.setToolTip(self._display_select_tooltip["select"])
        self._display_select_map = {
            "Normalized Raw Samples": self._DisplayValue(self._DisplayMode.S_N, "Sn"),
            "Impedance": self._DisplayValue(self._DisplayMode.IMPEDANCE, "Ω"),
            "Impedance (Log Scale)": self._DisplayValue(self._DisplayMode.IMPEDANCE_LOG_SCALE, "Ω"),
            "Reflection Coefficient": self._DisplayValue(self._DisplayMode.RHO, "Γ")
        }
        self._display_select_combo.addItems(list(self._display_select_map.keys()))
        self._display_select_combo.setCurrentIndex(list(self._display_select_map).index("Normalized Raw Samples"))
        self._display_select_combo.currentTextChanged.connect(self._on_display_select_changed)
        self._waveform_average_tooltip = {
            "average": "Waveform Averaging",
        }
        self._waveform_average_combo = QtWidgets.QComboBox()
        self._waveform_average_combo.setToolTip(self._waveform_average_tooltip["average"])
        self._waveform_average_map = {
            "No Averaging": 1,
            "x2 Averaging": 2,
            "x4 Averaging": 4,
            "x8 Averaging": 8,
            "x16 Averaging": 16,
            "x32 Averaging": 32,
            "x64 Averaging": 64,
            "x128 Averaging": 128
        }
        self._waveform_average_combo.addItems(list(self._waveform_average_map.keys()))
        self._waveform_average_combo.setCurrentIndex(0)
        self._waveform_average_count = self._waveform_average_map[self._waveform_average_combo.currentText()]
        self._waveform_average_combo.currentTextChanged.connect(self._on_waveform_average_changed)
        self._clear_display_btn = QtWidgets.QPushButton("🆑")
        self._clear_display_btn_tooltip = {
            "clear": "Click to clear the display",
        }
        self._clear_display_btn.setFixedWidth(40)
        self._clear_display_btn.setToolTip(self._clear_display_btn_tooltip["clear"])
        self._clear_display_btn.clicked.connect(self._on_clear_display)
        group_widget = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_widget)
        group_layout.addWidget(make_label(text="Display", bold=True, tooltip="Display controls"), alignment=toolbar_group_label_alignment)
        controls_layout1 = QtWidgets.QHBoxLayout()
        controls_layout1.addWidget(self._display_select_combo)
        controls_layout2 = QtWidgets.QHBoxLayout()
        controls_layout2.addWidget(self._waveform_average_combo)
        controls_layout2.addWidget(self._clear_display_btn)
        group_layout.addLayout(controls_layout1)
        group_layout.addLayout(controls_layout2)
        toolbar.addWidget(group_widget)
        toolbar.addSeparator()

        # Cursors group.
        cursor_label_min_width = 100
        self._vcursor_label_widget = QtWidgets.QLabel("Vertical:")
        self._show_vcursor1_checkbox = QtWidgets.QCheckBox("")
        self._show_vcursor1_checkbox.setChecked(False)
        vcursor1_label_widget = QtWidgets.QLabel("C1:")
        vcursor1_base_color = (255, 80, 255, 200)  # Purple.
        vcursor1_label_widget.setStyleSheet(f"color: rgba({vcursor1_base_color[0]},{vcursor1_base_color[1]},{vcursor1_base_color[2]},{vcursor1_base_color[3]/255});")
        self._vcursor1_label = QtWidgets.QLabel("--")
        self._vcursor1_label.setMinimumWidth(cursor_label_min_width)
        self._show_vcursor2_checkbox = QtWidgets.QCheckBox("")
        self._show_vcursor2_checkbox.setChecked(False)
        vcursor2_label_widget = QtWidgets.QLabel("C2:")
        vcursor2_base_color = (80, 220, 120, 200)  # Green.
        vcursor2_label_widget.setStyleSheet(f"color: rgba({vcursor2_base_color[0]},{vcursor2_base_color[1]},{vcursor2_base_color[2]},{vcursor2_base_color[3]/255});")
        self._vcursor2_label = QtWidgets.QLabel("--")
        self._vcursor2_label.setMinimumWidth(cursor_label_min_width)
        self._vcursor_delta_label = QtWidgets.QLabel("--")
        self._vcursor_delta_label.setMinimumWidth(cursor_label_min_width)

        self._hcursor_label_widget = QtWidgets.QLabel("Horizontal:")
        self._show_hcursor1_checkbox = QtWidgets.QCheckBox("")
        self._show_hcursor1_checkbox.setChecked(False)
        hcursor1_label_widget = QtWidgets.QLabel("C1:")
        hcursor1_base_color = (225, 190, 25, 200)  # Yellow.
        hcursor1_label_widget.setStyleSheet(f"color: rgba({hcursor1_base_color[0]},{hcursor1_base_color[1]},{hcursor1_base_color[2]},{hcursor1_base_color[3]/255});")
        self._hcursor1_label = QtWidgets.QLabel("--")
        self._hcursor1_label.setMinimumWidth(cursor_label_min_width)
        self._show_hcursor2_checkbox = QtWidgets.QCheckBox("")
        self._show_hcursor2_checkbox.setChecked(False)
        hcursor2_label_widget = QtWidgets.QLabel("C2:")
        hcursor2_base_color = (86, 60, 13, 200)  # Brown.
        hcursor2_label_widget.setStyleSheet(f"color: rgba({hcursor2_base_color[0]},{hcursor2_base_color[1]},{hcursor2_base_color[2]},{hcursor2_base_color[3]/255});")
        self._hcursor2_label = QtWidgets.QLabel("--")
        self._hcursor2_label.setMinimumWidth(cursor_label_min_width)
        self._hcursor_delta_label = QtWidgets.QLabel("--")
        self._hcursor_delta_label.setMinimumWidth(cursor_label_min_width)

        group_widget = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_widget)
        group_layout.addWidget(make_label(text="Cursors", bold=True), alignment=toolbar_group_label_alignment)
        controls_hlayout = QtWidgets.QHBoxLayout()
        controls_vlayout0 = QtWidgets.QVBoxLayout()
        controls_vlayout1 = QtWidgets.QVBoxLayout()
        controls_vlayout2 = QtWidgets.QVBoxLayout()
        controls_vlayout3 = QtWidgets.QVBoxLayout()
        controls_vlayout4 = QtWidgets.QVBoxLayout()
        controls_vlayout5 = QtWidgets.QVBoxLayout()
        controls_vlayout6 = QtWidgets.QVBoxLayout()
        controls_vlayout7 = QtWidgets.QVBoxLayout()
        controls_vlayout8 = QtWidgets.QVBoxLayout()
        controls_vlayout0.addWidget(self._vcursor_label_widget)
        controls_vlayout0.addWidget(self._hcursor_label_widget)
        controls_vlayout1.addWidget(self._show_vcursor1_checkbox)
        controls_vlayout1.addWidget(self._show_hcursor1_checkbox)
        controls_vlayout2.addWidget(vcursor1_label_widget)
        controls_vlayout2.addWidget(hcursor1_label_widget)
        controls_vlayout3.addWidget(self._vcursor1_label)
        controls_vlayout3.addWidget(self._hcursor1_label)
        controls_vlayout4.addWidget(self._show_vcursor2_checkbox)
        controls_vlayout4.addWidget(self._show_hcursor2_checkbox)
        controls_vlayout5.addWidget(vcursor2_label_widget)
        controls_vlayout5.addWidget(hcursor2_label_widget)
        controls_vlayout6.addWidget(self._vcursor2_label)
        controls_vlayout6.addWidget(self._hcursor2_label)
        controls_vlayout7.addWidget(QtWidgets.QLabel("Δx:"))
        controls_vlayout7.addWidget(QtWidgets.QLabel("Δy:"))
        controls_vlayout8.addWidget(self._vcursor_delta_label)
        controls_vlayout8.addWidget(self._hcursor_delta_label)
        controls_hlayout.addLayout(controls_vlayout0)
        controls_hlayout.addLayout(controls_vlayout1)
        controls_hlayout.addLayout(controls_vlayout2)
        controls_hlayout.addLayout(controls_vlayout3)
        controls_hlayout.addLayout(controls_vlayout4)
        controls_hlayout.addLayout(controls_vlayout5)
        controls_hlayout.addLayout(controls_vlayout6)
        controls_hlayout.addLayout(controls_vlayout7)
        controls_hlayout.addLayout(controls_vlayout8)
        controls_hlayout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        group_layout.addLayout(controls_hlayout)
        toolbar.addWidget(group_widget)
        toolbar.addSeparator()

        # Axes zoom control group - zoom checkboxes.
        self._zoom_tooltip = {
            "enable": "Enable zoom on axis",
            "disable": "Disable zoom on axis",
            "control": "Axes zoom control"
        }
        self._zoom_x_checkbox = QtWidgets.QCheckBox("X")
        self._zoom_y_checkbox = QtWidgets.QCheckBox("Y")
        self._zoom_x_checkbox.setChecked(True)
        self._zoom_y_checkbox.setChecked(True)
        self._update_zoom_checkbox_tooltip()
        self._zoom_x_checkbox.stateChanged.connect(self._update_zoom_mode)
        self._zoom_y_checkbox.stateChanged.connect(self._update_zoom_mode)
        group_widget = QtWidgets.QWidget()
        group_layout = QtWidgets.QVBoxLayout(group_widget)
        group_layout.addWidget(make_label(text="Zoom", bold=True, tooltip=self._zoom_tooltip["control"]), alignment=toolbar_group_label_alignment)
        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.addWidget(self._zoom_x_checkbox)
        controls_layout.addWidget(self._zoom_y_checkbox)
        controls_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        group_layout.addLayout(controls_layout)
        toolbar.addWidget(group_widget)
        toolbar.addSeparator()

        # Add a scatter item to main_plot.
        self._sample_stream_count = 0
        self._current_brush = pg.mkBrush(0, 122, 255, 160)
        self._scatter = pg.ScatterPlotItem(size=5, pen=None, brush=self._current_brush)
        self._main_plot.addItem(self._scatter)

        # Vertical draggable cursors.
        self._vcursor1 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(color=vcursor1_base_color, width=5))
        self._vcursor2 = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(color=vcursor2_base_color, width=5))
        self._vcursor1.setValue(1000)
        self._vcursor2.setValue(2000)
        self._main_plot.addItem(self._vcursor1, ignoreBounds=True)
        self._main_plot.addItem(self._vcursor2, ignoreBounds=True)
        self._vcursor1.sigPositionChanged.connect(self._update_vcursor_readouts)
        self._vcursor2.sigPositionChanged.connect(self._update_vcursor_readouts)
        self._show_vcursor1_checkbox.stateChanged.connect(lambda state: self._toggle_cursor(self._vcursor1, True, state, self._CursorBehaviorWhenClipped.USE_MIN))
        self._show_vcursor2_checkbox.stateChanged.connect(lambda state: self._toggle_cursor(self._vcursor2, True, state, self._CursorBehaviorWhenClipped.USE_MAX))
        self._toggle_cursor(self._vcursor1, True, self._show_vcursor1_checkbox.isChecked(), self._CursorBehaviorWhenClipped.USE_MIN)
        self._toggle_cursor(self._vcursor2, True, self._show_vcursor2_checkbox.isChecked(), self._CursorBehaviorWhenClipped.USE_MAX)
        self._update_vcursor_readouts()

        # Horizonal draggable cursors.
        self._hcursor1 = pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen(color=hcursor1_base_color, width=5))
        self._hcursor2 = pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen(color=hcursor2_base_color, width=5))
        self._hcursor1.setValue(0.71)
        self._hcursor2.setValue(0.6)
        self._hcursor1.sigPositionChanged.connect(self._update_hcursor_readouts)
        self._hcursor2.sigPositionChanged.connect(self._update_hcursor_readouts)
        self._show_hcursor1_checkbox.stateChanged.connect(lambda state: self._toggle_cursor(self._hcursor1, False, state, self._CursorBehaviorWhenClipped.USE_MIN))
        self._show_hcursor2_checkbox.stateChanged.connect(lambda state: self._toggle_cursor(self._hcursor2, False, state, self._CursorBehaviorWhenClipped.USE_MAX))
        self._main_plot.addItem(self._hcursor1, ignoreBounds=True)
        self._main_plot.addItem(self._hcursor2, ignoreBounds=True)
        self._toggle_cursor(self._hcursor1, False, self._show_hcursor1_checkbox.isChecked(), self._CursorBehaviorWhenClipped.USE_MIN)
        self._toggle_cursor(self._hcursor2, False, self._show_hcursor2_checkbox.isChecked(), self._CursorBehaviorWhenClipped.USE_MAX)
        self._update_hcursor_readouts()

        # Trigger refresh rate update at a fixed interval.
        update_refresh_rate_interval_ms = 1000
        self._update_refresh_rate_timer = QtCore.QTimer(self)
        self._update_refresh_rate_timer.timeout.connect(self._update_refresh_rate)
        self._update_refresh_rate_timer.start(update_refresh_rate_interval_ms)

        # Initial UI state should correspond to disconnected. 
        self._ui_on_disconnected()

        # Trigger non blocking _init_connection() or _on_connect().
        if connection is None: QtCore.QTimer.singleShot(0, self._init_connection)
        else: QtCore.QTimer.singleShot(0, lambda: self._on_connect(connection, ""))

    def _init_connection(self):
        """Pop the service browser and initiate a connection."""
        if self._connected: self._service_browser_dialog.set_selected(self._ip)
        self._service_browser_dialog.exec()

    def _clear_sample_stream_queue(self):
        '''Clear the sample stream queue'''
        while not self._sample_stream_queue.empty():
            try: self._sample_stream_queue.get_nowait()
            except queue.Empty: break

    def _on_connect(self, ip: str, host_name: str) -> bool:
        '''New connection logic'''
        # Clean up previous threads and listeners if needed.
        self._running = False
        try:
            if self._connected:
                self._radium_state_event_listener_it.cancel()
                self._sample_stream_listener_it.cancel()
                self._radium_led_id_state_listener_it.cancel()
                self._radium_state_work_thread.join()
                self._sample_stream_work_thread.join()
                self._radium_led_id_state_work_thread.join()
                self._logger.info(f"Disconnected from {self._host_txt}")
        except Exception:
            pass

        # Connection dependent state initialization.
        # If recording is in process _on_record_state_change() will end it.
        self._ui_on_disconnected()
        self._connected = False
        self._paused = False
        self._buffered_sample_stream = None
        self._sample_stream_queue = queue.Queue(maxsize=10)
        self._sample_stream_count = 0
        self._prev_update_refresh_rate_time = time.time()
        self._prev_displayed_pulse_period_ns = None
        self._prev_displayed_sample_spacing_ps = None
        self._prev_displayed_mode = None

        # If the ip address is empty, finish here.
        if not ip: return False

        # Create stubs, run initial checks and activate radium's listeners.
        # Use the ip address and not the mdns host name to speed up channel creation and additional avoid mdns lookups.
        self._ip = ip
        self._nitrogen_stub = NitrogenStub(grpc.insecure_channel(f"{self._ip}:50051"))
        self._radium_stub = RadiumStub(grpc.insecure_channel(f"{self._ip}:50052"))
        if host_name: self._host_txt = host_name + f" - {self._ip}"
        else: self._host_txt = self._ip
        try:
            self._nitrogen_stub.IsReady(nitrogen_public_pb2.IsReadyRequest(), timeout=0.5)
            version = self._nitrogen_stub.GetVersionId(nitrogen_public_pb2.GetVersionIdRequest())
            version_parts = version.id.split('.')
            version_dict = {
                "major": version_parts[0] if len(version_parts) > 0 else "",
                "minor": version_parts[1] if len(version_parts) > 1 else "",
                "patch": version_parts[2] if len(version_parts) > 2 else "",
                "reserved": version_parts[3] if len(version_parts) > 3 else ""
            }
            if version_dict["major"] != "0" and version_dict["reserved"] != "dev":
                self._logger.error(f"Device version is {version.id}, only versions with major == 0 are supported")
                raise ValueError("Device version mismatch")
            self._radium_stub.IsReady(radium_public_pb2.IsReadyRequest(), timeout=0.5)
            self._radium_state_event_listener_it = self._radium_stub.ListenToStateEvent(radium_public_pb2.ListenToStateEventRequest())
            self._sample_stream_listener_it = self._radium_stub.ListenToSampleStream(radium_public_pb2.ListenToSampleStreamRequest())
            self._radium_led_id_state_listener_it = self._radium_stub.ListenToLedState(radium_public_pb2.ListenToLedStateRequest(led_id_red=True, led_id_green=False, led_id_blue=False))
            self._connected = True
            self._service_browser_btn.setText("✅")
            str = f"Connected to {self._host_txt}, device software version is {version.id}"
            self._logger.info(str)
            self._service_browser_btn.setToolTip(str)
        except:
            self._logger.error(f"Failed connecting to device at {self._host_txt}")
            return self._connected

        # Update UI.
        if self._connected:
            self._identify_device_btn.setDisabled(False)
            self._identify_device_btn.setToolTip(self._identify_device_btn_tooltip["toggle"])
            self._start_stop_btn.setDisabled(False)
            self._paused = False
            self._play_pause_plot_updates_btn.setText("▶️")
            self._play_pause_plot_updates_btn.setToolTip(self._play_pause_plot_updates_btn_tooltip["pause"])
            self._play_pause_plot_updates_btn.setDisabled(False)
            self._record_btn.setDisabled(False)
            self._tdr_cfg_preset_combo.setDisabled(False)
            self._waveform_average_combo.setDisabled(False)

        # Start work threads.
        self._running = True
        self._sample_stream_work_thread = threading.Thread(target=self._radium_sample_stream)
        self._sample_stream_work_thread.name = "sample_stream_work_thread"
        self._sample_stream_work_thread.start()
        self._radium_led_id_state_work_thread = threading.Thread(target=self._radium_led_id_state_updates)
        self._radium_led_id_state_work_thread.name = "radium_led_id_state_work_thread"
        self._radium_led_id_state_work_thread.start()
        self._radium_state_work_thread = threading.Thread(target=self._radium_state_updates)
        self._radium_state_work_thread.name = "radium_state_work_thread"
        self._radium_state_work_thread.start()
        return self._connected

    def _ui_on_disconnected(self):
        '''Set the UI to a deterministic state upon disconnection'''
        self._service_browser_btn.setText("🚫")
        self._service_browser_btn.setToolTip("Disconnected")
        self._identify_device_btn.setText("⚫")
        self._identify_device_btn.setToolTip(self._identify_device_btn_tooltip["no_connect"])
        self._identify_device_btn.setDisabled(True)
        self._start_stop_btn.setText("🎬")
        self._start_stop_btn.setToolTip(self._start_stop_acquisition_tooltip["no_connect"])
        self._start_stop_btn.setDisabled(True)
        self._play_pause_plot_updates_btn.setToolTip(self._play_pause_plot_updates_btn_tooltip["no_connect"])
        self._play_pause_plot_updates_btn.setDisabled(True)
        self._record_btn.setToolTip(self._record_btn_tooltip["no_connect"])
        self._record_btn.setDisabled(True)
        self._tdr_cfg_preset_combo.setDisabled(True)
        self._waveform_average_combo.setDisabled(True)

    def _radium_state_updates(self):
        """Receive radium state updates."""
        try:
            while self._connected:
                radium_state = self._radium_stub.GetState(radium_public_pb2.GetStateRequest())
                if radium_state.acquisition_stalled: self._logger.error("Acquisition stalled")
                if not self._paused: self._radium_state_signal.emit(radium_state)
                next(self._radium_state_event_listener_it)
        except grpc.RpcError as e:
            # Upon error, if the link was connected, consider it now disconnected.
            if self._running:
                self._logger.error(f"gRPC error: {e.code()} - {e.details()}")
                if self._connected:
                    self._radium_disconnect_signal.emit()

    def _radium_led_id_state_updates(self):
        """Receive radium led state updates."""
        try:
            while self._connected:
                led_state = next(self._radium_led_id_state_listener_it)
                self._radium_led_id_state_signal.emit(led_state)
        except grpc.RpcError as e:
            if self._running:
                self._logger.error(f"gRPC error: {e.code()} - {e.details()}")

    def _radium_sample_stream(self):
        """Receive the gRPC sample stream from radium and buffer it in a queue. If needed, also record to memory."""
        try:
            rx_count_for_input_rate_calc = 0
            rx_count_for_avg_calc = 0
            prev_log_rx_stream_rate_time = time.time()
            while self._connected:
                next_msg = next(self._sample_stream_listener_it)
                rx_count_for_input_rate_calc += 1

                # Compute and log input sample stream rate.
                now = time.time()
                log_rx_stream_rate_interval_sec = 5
                if (now - prev_log_rx_stream_rate_time) > log_rx_stream_rate_interval_sec:
                    rate = rx_count_for_input_rate_calc / (now - prev_log_rx_stream_rate_time)
                    self._logger.info(f"RX sample stream rate is:{rate:.2f}Hz")
                    if rx_count_for_input_rate_calc != 0:
                        prev_log_rx_stream_rate_time = now
                        rx_count_for_input_rate_calc = 0

                try:
                    if not self._paused:
                        # Accumulate sample streams based on the average setting (as long as they belong to the same TDR configuration)
                        # Send the average sample stream to the UI thread using a queue.
                        self._radium_sample_stream_lock.acquire_lock()
                        if self._buffered_sample_stream is None or rx_count_for_avg_calc == 0:
                            self._buffered_sample_stream = next_msg
                            rx_count_for_avg_calc = 1
                        else:
                            if self._buffered_sample_stream.sample_spacing_ps == next_msg.sample_spacing_ps and self._buffered_sample_stream.pulse_period_ns == next_msg.pulse_period_ns:
                                self._buffered_sample_stream.sample[:] = (np.array(self._buffered_sample_stream.sample) + np.array(next_msg.sample)).tolist()
                                rx_count_for_avg_calc += 1
                            else:
                                self._buffered_sample_stream = next_msg
                                rx_count_for_avg_calc = 0

                        if rx_count_for_avg_calc == self._waveform_average_count:
                            self._buffered_sample_stream.sample[:] = (np.array(self._buffered_sample_stream.sample)/rx_count_for_avg_calc).tolist()
                            self._radium_sample_stream_lock.release_lock()
                            rx_count_for_avg_calc = 0
                            self._sample_stream_queue.put(self._buffered_sample_stream, block=False)
                            self._radium_sample_stream_signal.emit()
                        else:
                            self._radium_sample_stream_lock.release_lock()
                except queue.Full: pass
        except grpc.RpcError as e:
            if self._running: self._logger.error(f"gRPC error: {e.code()} - {e.details()}")

    def _on_tdr_preset_changed(self, preset_name: str):
        """Handle TDR preset changes."""
        if not self._connected: return
        preset_enum = self._tdr_cfg_preset_map.get(preset_name)
        if preset_enum is not None:
            try:
                self._logger.info(f"Configuring TDR preset: {preset_name}")
                self._radium_stub.ConfigureTDRPreset(radium_public_pb2.ConfigureTDRPresetRequest(preset=preset_enum))
            except grpc.RpcError as e:
                self._logger.error(f"Failed to communicate with radium: {e}")

    def _convert_y_to(self, sample_array: np.ndarray, in_mode: _DisplayMode, out_mode: _DisplayMode, ref_50ohm: np.float64, ref_unit_amp: np.float64) -> np.ndarray:
        """Convert the given array from the given in_mode to the given out_mode."""
        if in_mode == self._DisplayMode.S_N:
            if out_mode == self._DisplayMode.RHO:
                return (sample_array - ref_50ohm)/ref_unit_amp
            elif out_mode == self._DisplayMode.IMPEDANCE or out_mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
                reflection_coeff = (sample_array - ref_50ohm)/ref_unit_amp
                impedance = 50*(1 + reflection_coeff)/(1 - reflection_coeff)
                if out_mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
                    if np.isscalar(impedance):
                        impedance = impedance.astype(float)
                        if impedance <= 0: impedance = np.nan
                    else:
                        impedance[impedance <= 0] = np.nan
                    impedance = np.log10(impedance)
                return impedance
            else:
                return sample_array

        if in_mode == self._DisplayMode.RHO:
            if out_mode == self._DisplayMode.S_N:
                return sample_array*ref_unit_amp + ref_50ohm
            elif out_mode == self._DisplayMode.IMPEDANCE or out_mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
                sample_array_sn = sample_array*ref_unit_amp + ref_50ohm
                reflection_coeff = (sample_array_sn - ref_50ohm)/ref_unit_amp
                impedance = 50*(1 + reflection_coeff)/(1 - reflection_coeff)
                if out_mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
                    if np.isscalar(impedance):
                        impedance = impedance.astype(float)
                        if impedance <= 0: impedance = np.nan
                    else:
                        impedance[impedance <= 0] = np.nan
                    impedance = np.log10(impedance)
                return impedance
            else:
                return sample_array

        if in_mode == self._DisplayMode.IMPEDANCE:
            if out_mode == self._DisplayMode.S_N:
                reflection_coeff = (sample_array - 50)/(sample_array + 50)
                return reflection_coeff*ref_unit_amp + ref_50ohm
            elif out_mode == self._DisplayMode.RHO:
                reflection_coeff = (sample_array - 50)/(sample_array + 50)
                return reflection_coeff
            elif out_mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
                impedance = sample_array
                if np.isscalar(impedance):
                    impedance = impedance.astype(float)
                    if impedance <= 0: impedance = np.nan
                else:
                    impedance[impedance <= 0] = np.nan
                impedance = np.log10(impedance)
                return impedance
            else:
                return sample_array

        if in_mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
            if out_mode == self._DisplayMode.S_N:
                sample_array = 10 ** sample_array
                reflection_coeff = (sample_array - 50)/(sample_array + 50)
                return reflection_coeff*ref_unit_amp + ref_50ohm
            elif out_mode == self._DisplayMode.RHO:
                sample_array = 10 ** sample_array
                reflection_coeff = (sample_array - 50)/(sample_array + 50)
                return reflection_coeff
            elif out_mode == self._DisplayMode.IMPEDANCE:
                return 10 ** sample_array
            else:
                return sample_array

    def _on_display_select_changed(self, display_name: str):
        """Handle display select changes."""
        self._y_display = self._display_select_map[self._display_select_combo.currentText()]
        if self._y_display.mode == self._DisplayMode.IMPEDANCE:
            self._main_plot.ctrl.logYCheck.setDisabled(False)
            self._main_plot.ctrl.logYCheck.setChecked(False)
        elif self._y_display.mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
            self._main_plot.ctrl.logYCheck.setDisabled(False)
            self._main_plot.ctrl.logYCheck.setChecked(True)
        else:
            self._main_plot.ctrl.logYCheck.setChecked(False)
            self._main_plot.ctrl.logYCheck.setDisabled(True) 
        self._on_plot_update_y_log_mode()

    def _on_waveform_average_changed(self, average_name: str):
        """Handle waveform average preset changes."""
        self._radium_sample_stream_lock.acquire_lock()
        self._waveform_average_count = self._waveform_average_map[self._waveform_average_combo.currentText()]
        self._buffered_sample_stream = None
        self._radium_sample_stream_lock.release_lock()

    def _on_clear_display(self):
        """Clear display logic."""
        self._radium_sample_stream_lock.acquire_lock()
        self._clear_sample_stream_queue()
        self._scatter.clear()
        self._last_displayed_sample_stream = None
        self._buffered_sample_stream = None
        self._last_x_as_array = None
        self._last_y_as_array = None
        axis_flag = self._axis_map.get("left", pg.ViewBox.YAxis)
        self._main_plot.enableAutoRange(axis=axis_flag, enable=True)
        self._main_plot.enableAutoRange(axis=axis_flag, enable=False)
        self._update_vcursor_readouts()
        self._radium_sample_stream_lock.release_lock()

    def _on_start_stop_clicked(self):
        """Start\\stop acquisition button logic."""
        if not self._connected or self._radium_state is None: return
        try:
            if self._radium_state.HasField("pulse_period_ns") and self._radium_state.HasField("sample_spacing_ps") and not self._radium_state.acquiring:
                self._logger.info(f"Enabling TDR")
                self._radium_stub.EnableTDR(radium_public_pb2.EnableTDRRequest(enable=True))
            elif self._radium_state.acquiring:
                self._logger.info(f"Disabling TDR")
                self._radium_stub.EnableTDR(radium_public_pb2.EnableTDRRequest(enable=False))
        except grpc.RpcError as e:
            self._logger.error(f"Failed to communicate with radium: {e}")

    def _on_play_pause_plot_updates(self):
        """Play\\pause button logic."""
        if not self._connected: return
        self._paused = not self._paused
        self._play_pause_plot_updates_btn.setText("⏸️" if self._paused else "▶️")
        self._play_pause_plot_updates_btn.setToolTip(self._play_pause_plot_updates_btn_tooltip["resume"] if self._paused else self._play_pause_plot_updates_btn_tooltip["pause"])
        if self._paused:
            # Upon pause disable some controls.
            self._start_stop_btn.setToolTip(self._start_stop_acquisition_tooltip["display_paused"])
            self._start_stop_btn.setDisabled(True)
            self._tdr_cfg_preset_combo.setToolTip(self._tdr_cfg_preset_tooltip["display_paused"])
            self._tdr_cfg_preset_combo.setDisabled(True)

            # Clear the queue so we don't see the past when we resume.
            self._clear_sample_stream_queue()
        else:
            try:
                # Since we blocked radium state based updates when paused, upon resume get radium's latest state and trigger state based updates.
                radium_state = self._radium_stub.GetState(radium_public_pb2.GetStateRequest())
                self._tdr_cfg_preset_combo.setToolTip(self._tdr_cfg_preset_tooltip["preset"])
                self._tdr_cfg_preset_combo.setDisabled(False)
                self._radium_state_signal.emit(radium_state)
            except grpc.RpcError as e:
                self._logger.error(f"Failed to communicate with radium: {e}")

    def _on_record_state_change(self):
        """Record logic."""
        if not self._connected or self._last_displayed_sample_stream is None: return
        self._record_btn.setText("🔴")
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        default_filename = f"pymtdr_{timestamp}.csv"
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Data As", default_filename, "CSV Files (*.csv);;All Files (*)")
        if file_path:
            try:
                ref_50ohm = self._last_displayed_sample_stream.ref_50ohm
                ref_unit_amp = self._last_displayed_sample_stream.ref_unit_amp
                arr_y = np.asarray(self._last_displayed_sample_stream.sample, dtype=np.float64).reshape(-1, 1)
                arr_x = np.asarray(self._last_x_as_array, dtype=np.float64).reshape(-1, 1) * 1e-12
                arr = np.concatenate([arr_x, arr_y], axis=1)
                comments=f"#ref_50ohm = {ref_50ohm}\n#ref_unit_amp = {ref_unit_amp}\n"
                np.savetxt(file_path, arr, fmt=["%.12e", "%.6e"], delimiter=",", header=self._CSV_RECORD_HEADER, comments=f"{comments}")
                self._logger.info(f"Recorded data saved to {file_path}")
            except Exception as e:
                self._logger.error(f"Failed to save recorded data: {e}")
        else:
            self._logger.info("Discarding recorded data")
        self._record_btn.setText("⏺️")

    def _on_toggle_radium_led_id(self):
        """Toggle led, can be used to identify a device."""
        if not self._connected: return
        try:
            if self._led_id_state is not None and self._led_id_state.brightness != 255:
                self._radium_stub.SetLed(radium_public_pb2.SetLedRequest(led_id=radium_public_pb2.LED_ID_RED, set=True))
            else:
                self._radium_stub.SetLed(radium_public_pb2.SetLedRequest(led_id=radium_public_pb2.LED_ID_RED, set=False))
        except grpc.RpcError as e:
            self._logger.error(f"Failed to communicate with radium: {e}")

    def _on_plot_update_y_log_mode(self):
        """Custom plot log mode handler."""
        # Update the label and state.
        _prev_y_axis_is_in_log_mode = self._y_axis_is_in_log_scale
        self._main_plot.setLabel("left", text=self._y_display.label)
        self._y_axis_is_in_log_scale = self._main_plot.ctrl.logYCheck.isChecked()

        # Run a single scatter update 
        # This is important in order to remove invalid values before setting the log mode.
        # Since we also get here from _on_display_select_changed a call to _update_scatter_plot() is critical in order to get the new display mode correctly set.
        self._update_scatter_plot()
        
        # Set the axis to use log mode.
        pg.PlotItem.updateLogMode(self._main_plot)

        # Disable auto range to avoid the plot from moving all the time.
        for axis_name in self._axis_map:
            axis_flag = self._axis_map.get(axis_name, pg.ViewBox.XYAxes)
            self._main_plot.enableAutoRange(axis=axis_flag, enable=False)
            if self._last_x_as_array is not None and len(self._last_x_as_array) > 0: self._main_plot.setXRange(np.min(self._last_x_as_array), np.max(self._last_x_as_array), padding=0)

        # If log mode was entered or exited using the context menu of the plot, update the display selector.
        if self._y_axis_is_in_log_scale and self._y_display.mode == self._DisplayMode.IMPEDANCE:
            self._y_display = self._DisplayValue(self._DisplayMode.IMPEDANCE_LOG_SCALE, "Ω")
            self._display_select_combo.setCurrentIndex(list(self._display_select_map).index("Impedance (Log Scale)"))
        if not self._y_axis_is_in_log_scale and self._y_display.mode == self._DisplayMode.IMPEDANCE_LOG_SCALE:
            self._y_display = self._DisplayValue(self._DisplayMode.IMPEDANCE, "Ω")
            self._display_select_combo.setCurrentIndex(list(self._display_select_map).index("Impedance"))

    def _one_time_autorange(self):
        """Perform autorange only on the y axis and only once. The x axis is tight with the data"""
        if self._last_x_as_array is not None and len(self._last_x_as_array) > 0: self._main_plot.setXRange(np.min(self._last_x_as_array), np.max(self._last_x_as_array), padding=0)
        axis_flag = self._axis_map.get("left", pg.ViewBox.XYAxes)
        self._main_plot.enableAutoRange(axis=axis_flag, enable=True)
        self._main_plot.enableAutoRange(axis=axis_flag, enable=False)

    def _toggle_cursor(self, cursor: pg.InfiniteLine, vertical: bool, state: bool, when_clipped: _CursorBehaviorWhenClipped):
        """Toggle a given cursor on/off"""
        if state:
            cursor.setVisible(True)
        else:
            cursor.setVisible(False)
        if vertical:
            if state and self._last_x_as_array is not None:
                min_x = np.min(self._last_x_as_array)
                max_x = np.max(self._last_x_as_array)
                pos = cursor.value()
                if pos < min_x or pos > max_x:
                    if when_clipped == self._CursorBehaviorWhenClipped.USE_MIN:
                        cursor.setValue(min_x)
                    if when_clipped == self._CursorBehaviorWhenClipped.USE_MAX:
                        cursor.setValue(max_x)
            self._update_vcursor_readouts()
        else:
            if state and self._last_y_as_array is not None:
                min_y = np.nanmin(self._last_y_as_array)
                max_y = np.nanmax(self._last_y_as_array)
                pos = cursor.value()
                if pos < min_y or pos > max_y or np.isnan(pos):
                    if when_clipped == self._CursorBehaviorWhenClipped.USE_MIN:
                        cursor.setValue(min_y)
                    if when_clipped == self._CursorBehaviorWhenClipped.USE_MAX:
                        cursor.setValue(max_y)
            self._update_hcursor_readouts()

    def _is_cursor_selected(self) -> bool:
        '''Returns True if a cursor is selected'''
        if self._show_vcursor1_checkbox.isChecked(): return True
        if self._show_vcursor2_checkbox.isChecked(): return True
        if self._show_hcursor1_checkbox.isChecked(): return True
        if self._show_hcursor2_checkbox.isChecked(): return True
        return False

    def _move_cursor_to_intersection(self, point: QtCore.QPointF):
        '''Move a cursor to intersect with the given point if it is checked'''
        if self._show_vcursor1_checkbox.isChecked(): self._vcursor1.setValue(point.x())
        if self._show_vcursor2_checkbox.isChecked(): self._vcursor2.setValue(point.x())
        if self._show_hcursor1_checkbox.isChecked(): self._hcursor1.setValue(point.y())
        if self._show_hcursor2_checkbox.isChecked(): self._hcursor2.setValue(point.y())

    def _update_vcursor_readouts(self):
        """Update the vertical cursor readouts."""
        def y_at_x(xq: float):
            '''Helper to get the waveform y value at a give x value'''
            if self._last_x_as_array is None or self._last_y_as_array is None or len(self._last_x_as_array) == 0: return None
            idx = np.searchsorted(self._last_x_as_array, xq)
            if idx <= 0: return float(self._last_y_as_array[0])
            if idx >= len(self._last_x_as_array): return float(self._last_y_as_array[-1])
            x0, x1 = self._last_x_as_array[idx-1], self._last_x_as_array[idx]
            y0, y1 = self._last_y_as_array[idx-1], self._last_y_as_array[idx]
            if x1 == x0: return float(y0)
            t = (xq - x0) / (x1 - x0)
            if self._y_axis_is_in_log_scale: return 10 ** float(y0 + t * (y1 - y0))
            else: return float(y0 + t * (y1 - y0))

        # Update the vertical cursors.
        vx1, vx2 = float(self._vcursor1.value()), float(self._vcursor2.value())  # type: ignore
        if self._vcursor1.isVisible():
            vy1 = y_at_x(vx1)
            self._vcursor1_label.setText(f"{vx1:.1f}ps, {"--" if vy1 is None else f'{vy1:.2f}'}")
        else:
            self._vcursor1_label.setText(f"--ps, --")
        if self._vcursor2.isVisible():
            vy2 = y_at_x(vx2)
            self._vcursor2_label.setText(f"{vx2:.1f}ps, {"--" if vy2 is None else f'{vy2:.2f}'}")
        else:
            self._vcursor2_label.setText(f"--ps, --")
        if self._vcursor1.isVisible() and self._vcursor2.isVisible():
            self._vcursor_delta_label.setText(f"{abs(vx2 - vx1):.2f}ps")
        else:
            self._vcursor_delta_label.setText(f"--ps")

    def _update_hcursor_readouts(self):
        """Update the horizontal cursor readouts."""
        hy1, hy2 = float(self._hcursor1.value()), float(self._hcursor2.value())  # type: ignore
        if self._y_axis_is_in_log_scale:
            hy1, hy2 = 10 ** hy1, 10 ** hy2
        if self._hcursor1.isVisible():
            self._hcursor1_label.setText(f"{hy1:.2f}")
        else:
            self._hcursor1_label.setText(f"--")
        if self._hcursor2.isVisible():
            self._hcursor2_label.setText(f"{hy2:.2f}")
        else:
            self._hcursor2_label.setText(f"--")
        if self._hcursor1.isVisible() and self._hcursor2.isVisible():
            self._hcursor_delta_label.setText(f"{abs(hy1 - hy2):.2f}")
        else:
            self._hcursor_delta_label.setText(f"--")

    def _update_based_on_radium_state(self, radium_state: radium_public_pb2.GetStateReply):
        """Handles all UI updates based on a radium state change."""
        # Save the latest radium state.
        self._radium_state = radium_state

        # Update preset dropdown if present in radium_state (only update if different to avoid unnecessary signals).
        if radium_state.HasField("configuration_preset"):
            for name, enum_val in self._tdr_cfg_preset_map.items():
                if enum_val == radium_state.configuration_preset:
                    if self._tdr_cfg_preset_combo.currentText() != name:
                        self._tdr_cfg_preset_combo.blockSignals(True)
                        self._tdr_cfg_preset_combo.setCurrentText(name)
                        self._tdr_cfg_preset_combo.blockSignals(False)
                    break
        else:
            # If no configuration_preset, set to self._tdr_cfg_preset_none_txt
            if radium_state.HasField("pulse_period_ns") and radium_state.HasField("sample_spacing_ps"):
                self._tdr_cfg_preset_combo.blockSignals(True)
                self._tdr_cfg_preset_combo.setCurrentText(self._tdr_cfg_preset_custom_txt)
                self._tdr_cfg_preset_combo.blockSignals(False)
            elif self._tdr_cfg_preset_combo.currentText() != self._tdr_cfg_preset_none_txt:
                self._tdr_cfg_preset_combo.blockSignals(True)
                self._tdr_cfg_preset_combo.setCurrentText(self._tdr_cfg_preset_none_txt)
                self._tdr_cfg_preset_combo.blockSignals(False)

        # Update start_stop button and record button attributes based on the acquisition attribute in the state..
        if radium_state.acquiring:
            self._start_stop_btn.setText("🟢")
            self._start_stop_btn.setToolTip(self._start_stop_acquisition_tooltip["stop"])
            self._record_btn.setToolTip(self._record_btn_tooltip["record"])
            self._record_btn.setDisabled(False)
        else:
            self._start_stop_btn.setText("🎬")
            self._start_stop_btn.setToolTip(self._start_stop_acquisition_tooltip["start"])
            self._record_btn.setToolTip(self._record_btn_tooltip["not_acquiring"])
            self._record_btn.setDisabled(True)

        # When the start_stop button displays 🎬 and the tdr configuration is invalid, disable it.
        if self._start_stop_btn.text() == "🎬" and not radium_state.HasField("configuration_preset") and (not radium_state.HasField("pulse_period_ns") or not radium_state.HasField("sample_spacing_ps")):
            self._start_stop_btn.setToolTip(self._start_stop_acquisition_tooltip["no_tdr_preset"])
            self._start_stop_btn.setDisabled(True)
        else:
            self._start_stop_btn.setDisabled(False)

    def _update_scatter_plot(self):
        """Update the scatter plot title and data from last_display_sample_stream."""
        if self._last_displayed_sample_stream is None: return
        sample_spacing_ps = self._last_displayed_sample_stream.sample_spacing_ps
        pulse_period_ns = self._last_displayed_sample_stream.pulse_period_ns
        display_mode = self._y_display.mode

        # Update plot title.
        if sample_spacing_ps != self._prev_displayed_sample_spacing_ps or pulse_period_ns != self._prev_displayed_pulse_period_ns:
            self._main_plot.setTitle(f"TDR: Pulse Period:{pulse_period_ns:.2f}ns, Sample Spacing:{sample_spacing_ps}ps")

        # Update xlimits if needed.
        if sample_spacing_ps != self._prev_displayed_sample_spacing_ps:
            xlim_max = len(self._last_displayed_sample_stream.sample) * sample_spacing_ps
            self._main_plot.setXRange(0, xlim_max, padding=0)
            self._prev_displayed_sample_spacing_ps = sample_spacing_ps

        #  Convert x & y to numpy arrays (convert y as needed using the metadata) and store for analysis external to the plot.
        self._last_x_as_array = np.arange(len(self._last_displayed_sample_stream.sample), dtype=np.float64) * sample_spacing_ps
        self._last_y_as_array = np.asarray(self._last_displayed_sample_stream.sample, dtype=np.float64)
        self._last_y_as_array = self._convert_y_to(self._last_y_as_array, self._DisplayMode.S_N, display_mode, self._last_displayed_sample_stream.ref_50ohm, self._last_displayed_sample_stream.ref_unit_amp)

        # Update the scatter plot.
        self._scatter.setData(x=self._last_x_as_array, y=self._last_y_as_array, pxMode=True, brush=self._current_brush)

        # Restrict vertical cursor movement to x data range and update the readout.
        if self._last_x_as_array is not None and len(self._last_x_as_array) > 0:
            min_x = 0
            max_x = self._last_x_as_array[-1]
            self._vcursor1.setBounds([min_x, max_x])
            self._vcursor2.setBounds([min_x, max_x])
            if self._vcursor1.value() < min_x: self._vcursor1.setValue(min_x)  # type: ignore
            elif self._vcursor1.value() > max_x: self._vcursor1.setValue(max_x)
            if self._vcursor2.value() < min_x: self._vcursor2.setValue(min_x)  # type: ignore
            elif self._vcursor2.value() > max_x: self._vcursor2.setValue(max_x)
        self._update_vcursor_readouts()

        # If the display mode changed apply autorange and update the value of the horizontal cursors using the metadata.
        if self._prev_displayed_mode != display_mode:
            axis_flag = self._axis_map.get("left", pg.ViewBox.XYAxes)
            self._main_plot.enableAutoRange(axis=axis_flag, enable=True)
            self._main_plot.enableAutoRange(axis=axis_flag, enable=False)
            if self._prev_displayed_mode is not None:
                hcursor1_as_np = np.asarray(self._hcursor1.value(), dtype=np.float64)
                hcursor2_as_np = np.asarray(self._hcursor2.value(), dtype=np.float64)
                hcursor1_updated_value = self._convert_y_to(hcursor1_as_np, self._prev_displayed_mode, display_mode, self._last_displayed_sample_stream.ref_50ohm, self._last_displayed_sample_stream.ref_unit_amp)
                hcursor2_updated_value = self._convert_y_to(hcursor2_as_np, self._prev_displayed_mode, display_mode, self._last_displayed_sample_stream.ref_50ohm, self._last_displayed_sample_stream.ref_unit_amp)
                self._hcursor1.setValue(hcursor1_updated_value)
                self._hcursor2.setValue(hcursor2_updated_value)
                self._update_hcursor_readouts()

        # Update previous displayed state.
        self._prev_displayed_sample_spacing_ps = sample_spacing_ps
        self._prev_displayed_pulse_period_ns = pulse_period_ns
        self._prev_displayed_mode = display_mode

    def _pop_sample_stream_queue(self):
        '''Keep popping sample streams from the queue.'''
        while True:
            if self._paused: return
            try:
                self._last_displayed_sample_stream = self._sample_stream_queue.get_nowait()
                self._sample_stream_count += 1
                self._update_scatter_plot()
            except queue.Empty: return

    def _update_refresh_rate(self):
        """Update the refresh rate label."""
        # Restart the counter and the previous refresh rate timestamp only if we have something. Otherwise, refresh the display of slow refresh rates will reset to 0 between updates.
        if not self._connected or self._radium_state is None or not self._radium_state.acquiring:
            rate = 0
            self._refresh_rate_label.setText(f"{rate:.2f}Hz")
            return
        now = time.time()
        rate = self._sample_stream_count / (now - self._prev_update_refresh_rate_time)
        if self._sample_stream_count != 0:
            self._refresh_rate_label.setText(f"{rate:.2f}Hz")
            self._prev_update_refresh_rate_time = now
            self._sample_stream_count = 0

    def _update_led_id_state(self, led_state: radium_public_pb2.LedState):
        """Update the identify device button."""
        self._led_id_state = led_state
        if led_state.brightness != 255: self._identify_device_btn.setText("⚫")
        else: self._identify_device_btn.setText("💡")

    def _update_zoom_mode(self):
        """Update the zoom mode."""
        if self._zoom_x_checkbox.isChecked() and not self._zoom_y_checkbox.isChecked(): self._zoom_axis = "x"
        elif self._zoom_y_checkbox.isChecked() and not self._zoom_x_checkbox.isChecked(): self._zoom_axis = "y"
        elif self._zoom_y_checkbox.isChecked() and self._zoom_x_checkbox.isChecked(): self._zoom_axis = "both"
        else: self._zoom_axis = "none"
        self._update_zoom_checkbox_tooltip()

    def _update_zoom_checkbox_tooltip(self):
        """Update the zoom checkboxes tooltip."""
        if self._zoom_axis == "none": self._zoom_x_checkbox.setToolTip(self._zoom_tooltip["enable"]); self._zoom_y_checkbox.setToolTip(self._zoom_tooltip["enable"])
        elif self._zoom_axis == "x": self._zoom_x_checkbox.setToolTip(self._zoom_tooltip["disable"]); self._zoom_y_checkbox.setToolTip(self._zoom_tooltip["enable"])
        elif self._zoom_axis == "y": self._zoom_x_checkbox.setToolTip(self._zoom_tooltip["enable"]); self._zoom_y_checkbox.setToolTip(self._zoom_tooltip["disable"])
        else: self._zoom_x_checkbox.setToolTip(self._zoom_tooltip["disable"]); self._zoom_y_checkbox.setToolTip(self._zoom_tooltip["disable"])

    def _import(self):
        """Import a CSV file and update the display if valid."""
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import Data", "", "CSV Files (*.csv);;All Files (*)")
        if not file_path: return
        try:
            # Read file and parse.
            # Find the header line and start data after it.
            with open(file_path, 'r') as f: lines = f.readlines()
            comments = [l for l in lines if l.startswith('#')]
            header_idx = None
            for i, l in enumerate(lines):
                if l.strip() == self._CSV_RECORD_HEADER:
                    header_idx = i
                    break
            if header_idx is None:
                QtWidgets.QMessageBox.warning(self, "Import Error", f"CSV missing header: {self._CSV_RECORD_HEADER}")
                return
            data_lines = [l for l in lines[header_idx+1:] if l.strip()]

            # Parse reference values from comments.
            ref_50ohm = None
            ref_unit_amp = None
            for c in comments:
                m1 = re.match(r'#ref_50ohm\s*=\s*([\d.eE+-]+)', c)
                m2 = re.match(r'#ref_unit_amp\s*=\s*([\d.eE+-]+)', c)
                if m1: ref_50ohm = float(m1.group(1))
                if m2: ref_unit_amp = float(m2.group(1))
            if ref_50ohm is None or ref_unit_amp is None:
                QtWidgets.QMessageBox.warning(self, "Import Error", "CSV missing ref_50ohm or ref_unit_amp in comments.")
                return

            # Parse data.
            arr = np.genfromtxt(data_lines, delimiter=',')
            if arr.ndim == 1: arr = arr.reshape(-1, 2)
            if arr.shape[1] != 2:
                QtWidgets.QMessageBox.warning(self, "Import Error", "CSV data is invalid")
                return
            arr_x = arr[:, 0]
            arr_y = arr[:, 1]
            if len(arr_x) < 2:
                QtWidgets.QMessageBox.warning(self, "Import Error", "CSV data is invalid")
                return
            sample_spacing_ps = (arr_x[1] - arr_x[0]) * 1e12
            pulse_period_ns = len(arr_x)*sample_spacing_ps/1000.0

            # Build a sample stream.
            class DummySampleStream: pass
            s = DummySampleStream()
            s.sample_spacing_ps = sample_spacing_ps
            s.pulse_period_ns = pulse_period_ns
            s.ref_50ohm = ref_50ohm
            s.ref_unit_amp = ref_unit_amp
            s.sample = arr_y.tolist()
            self._last_displayed_sample_stream = s
            self._last_x_as_array = arr_x
            self._last_y_as_array = arr_y
            self._prev_displayed_pulse_period_ns = None
            self._prev_displayed_sample_spacing_ps = None
            self._update_scatter_plot()
            self._logger.info(f"Imported data from {file_path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Import Error", f"Failed to import CSV: {e}")

    @override
    def closeEvent(self, a0: Optional[QtGui.QCloseEvent]):
        """Override parent class method in order to perform a clean exit."""
        self._running = False
        if self._connected:
            self._radium_state_event_listener_it.cancel()
            self._sample_stream_listener_it.cancel()
            self._radium_led_id_state_listener_it.cancel()
            self._radium_state_work_thread.join()
            self._sample_stream_work_thread.join()
            self._radium_led_id_state_work_thread.join()
        super().closeEvent(a0)

    @override
    def keyPressEvent(self, a0: Optional[QtGui.QKeyEvent]):
        """Override parent class method in order to handle special keys for zoom."""
        assert a0 is not None
        if a0.key() == QtCore.Qt.Key.Key_X:
            self._zoom_axis_store = self._zoom_axis
            self._zoom_axis = "x"
            self._zoom_x_checkbox.setChecked(True)
            self._zoom_y_checkbox.setChecked(False)
            self._update_zoom_checkbox_tooltip()
        elif a0.key() == QtCore.Qt.Key.Key_Y:
            self._zoom_axis_store = self._zoom_axis
            self._zoom_axis = "y"
            self._zoom_x_checkbox.setChecked(False)
            self._zoom_y_checkbox.setChecked(True)
            self._update_zoom_checkbox_tooltip()
        elif a0.key() == QtCore.Qt.Key.Key_Shift:
            self._zoom_axis_store = self._zoom_axis
            self._zoom_axis = "both"
            self._zoom_x_checkbox.setChecked(True)
            self._zoom_y_checkbox.setChecked(True)
            self._update_zoom_checkbox_tooltip()
            self._main_plot.getViewBox().setMouseMode(pg.ViewBox.RectMode)  # type: ignore
        else:
            super().keyPressEvent(a0)

    @override
    def keyReleaseEvent(self, a0: Optional[QtGui.QKeyEvent]):
        """Override parent class method in order to handle special keys for zoom."""
        assert a0 is not None
        if a0.key() in (QtCore.Qt.Key.Key_X, QtCore.Qt.Key.Key_Y, QtCore.Qt.Key.Key_Shift):
            self._zoom_axis = self._zoom_axis_store
            if self._zoom_axis == "none": self._zoom_x_checkbox.setChecked(False); self._zoom_y_checkbox.setChecked(False)
            elif self._zoom_axis == "x": self._zoom_x_checkbox.setChecked(True); self._zoom_y_checkbox.setChecked(False)
            elif self._zoom_axis == "y": self._zoom_x_checkbox.setChecked(False); self._zoom_y_checkbox.setChecked(True)
            else: self._zoom_x_checkbox.setChecked(True); self._zoom_y_checkbox.setChecked(True)
            self._update_zoom_checkbox_tooltip()
            if a0.key() == QtCore.Qt.Key.Key_Shift:
                self._main_plot.getViewBox().setMouseMode(pg.ViewBox.PanMode)  # type: ignore
        else:
            super().keyReleaseEvent(a0)


# Entry.
def main(address: Optional[str] = None):
    # Create a QApplication and start the window.
    logger = logging.getLogger("app")
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s', handlers=[logging.StreamHandler()])
    app = QtWidgets.QApplication(sys.argv)
    basedir = os.path.dirname(__file__)
    app.setWindowIcon(QtGui.QIcon(os.path.join(basedir, 'favicon.ico')))
    app_win = AppWindow(logger, address)
    app_win.show()
    sys.exit(app.exec())


@click.command()
@click.option('--address', '-a', help='Address to connect to')
def cli(address):
    if address: main(address)
    else: main()


if __name__ == "__main__":
    cli()
