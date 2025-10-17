from typing import List, Dict, Callable, Optional, override
from pyqtgraph.Qt import QtWidgets, QtCore, QtGui
from zeroconf import Zeroconf, ServiceListener, ServiceBrowser
from enum import Enum
import socket
import time


class MDNSWorker(QtCore.QObject, ServiceListener):
    '''Worker for mDNS queries.'''
    info_list_ready_signal = QtCore.pyqtSignal(list)
    error_signal = QtCore.pyqtSignal(str)

    def __init__(self, service_type, timeout_s: float = 2.0):
        super().__init__()
        self._service_type = service_type
        self._timeout_s = timeout_s
        self._zc: Zeroconf = Zeroconf()
        self._names_set = set()
        self._lock = QtCore.QMutex()
        self._browser = ServiceBrowser(self._zc, self._service_type, listener=self)

    @override
    def add_service(self, zc, type_, name):
        with QtCore.QMutexLocker(self._lock): self._names_set.add(name)

    @override
    def update_service(self, zc, type_, name):
        with QtCore.QMutexLocker(self._lock): self._names_set.add(name)

    @override
    def remove_service(self, zc, type_, name):
        with QtCore.QMutexLocker(self._lock):
            if name in self._names_set: self._names_set.remove(name)

    def refresh(self):
        """Extract the information from the names set and emit a signal which includes a post processed list"""
        try:
            # We need a local copy of the names set to make sure it does not change while it is itrated upon.
            with QtCore.QMutexLocker(self._lock):
                names_set_local = self._names_set

            # Fill out a list of dicts with the information retrieved from ZeroConf().
            info_list = []
            for name in sorted(names_set_local):
                info = self._zc.get_service_info(self._service_type, name, timeout=int(self._timeout_s) * 1000)
                if not info: continue
                addrs: List[str] = []
                for raw in getattr(info, "addresses", []) or []:
                    try: addrs.append(socket.inet_ntop(socket.AF_INET if len(raw) == 4 else socket.AF_INET6, raw))
                    except Exception: pass

                # Decode info.properties into a dictionary.
                props: Dict[str, str] = {}
                for k, v in (info.properties or {}).items():
                    ks = k.decode("utf-8", "replace") if isinstance(k, (bytes, bytearray)) else str(k)
                    if isinstance(v, (bytes, bytearray)):
                        try: vs = v.decode("utf-8")
                        except Exception: vs = v.decode("utf-8", "replace")
                    else:
                        vs = str(v)
                    props[ks] = vs

                # Add to the final info_list.
                # For some reason zeroconf has '.' at the end of the name and server.
                for addr in addrs:
                    info_list.append({
                        "name": name[:-1],
                        "host": info.server[:-1] if info.server else "",
                        "ip": addr,
                        "port": info.port or 0,
                        "txt": " ".join(f"{k}={v}" for k, v in props.items() if v != "None")
                    })

            # Signal that the info list is ready.
            self.info_list_ready_signal.emit(info_list)

        except Exception as e:
            self.error_signal.emit(str(e))

    def stop(self):
        self._browser.cancel()
        self._zc.close()


class ServiceBrowserDialog(QtWidgets.QDialog):
    '''Dialog which periodically refreshes an mDNS service table.

       on_connect is a user callback which takes two arguments - ip: str, name: str
       it should return True if the user wants to accept the connection and False otherwise.
    '''
    class _Column(Enum):
        NAME = 0
        HOST = 1
        IP = 2
        PORT = 3
        TXT = 4

    def __init__(self, parent=None, on_connect: Optional[Callable[[str, str], bool]] = None, service_type: str = "_workstation._tcp.local.", refresh_ms: int = 3000):
        super().__init__(parent)
        self._service_type = service_type
        self._refresh_ms = refresh_ms
        self._prev_auto_discovered_services = []
        self._manual_entry_services = []
        self._block_status_updates = False
        self._external_selected = False
        self._user_on_connect = on_connect
        self.setWindowTitle(f"mDNS Services: {service_type}")
        self.resize(1100, 420)

        # Table widget.
        header_labels = ["Name", "Host", "IP", "Port", "TXT"]
        self._table = QtWidgets.QTableWidget(0, len(header_labels), self)
        self._table.setHorizontalHeaderLabels(header_labels)
        horizontal_header = self._table.horizontalHeader()
        assert horizontal_header is not None
        horizontal_header.setStretchLastSection(True)
        horizontal_header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._table.itemDoubleClicked.connect(self._on_connect)

        # Widgets at the bottom of the dialog.
        self._spin = QtWidgets.QSpinBox()
        self._spin.setRange(500, 60000)
        self._spin.setValue(refresh_ms)
        self._spin.setSuffix(" ms")
        self._refresh_btn = QtWidgets.QPushButton("Refresh Now")
        self._connect_btn = QtWidgets.QPushButton("Connect")
        self._disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self._disconnect_btn.setDisabled(True)
        self._add_entry_btn = QtWidgets.QPushButton("Add Entry")
        self._connect_btn.setEnabled(False)
        self._add_entry_btn.clicked.connect(self._on_add_entry)
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        hbox_layout = QtWidgets.QHBoxLayout()
        hbox_layout.addWidget(QtWidgets.QLabel("Update every:"))
        hbox_layout.addWidget(self._spin)
        hbox_layout.addWidget(self._refresh_btn)
        hbox_layout.addWidget(self._add_entry_btn)
        hbox_layout.addStretch(1)
        hbox_layout.addWidget(self._disconnect_btn)
        hbox_layout.addWidget(self._connect_btn)

        # Final vertical layout.
        vbox_layout = QtWidgets.QVBoxLayout(self)
        vbox_layout.addWidget(self._table)
        vbox_layout.addLayout(hbox_layout)
        self._status = QtWidgets.QLabel("Scanning...")
        vbox_layout.addWidget(self._status)

    def _init_worker_and_timer(self):
        '''Instantiate the mdns_worker and refresh timer.'''
        # MDNSWorker instance.
        self._mdns_worker = MDNSWorker(service_type=self._service_type)
        self._mdns_worker.info_list_ready_signal.connect(self._populate)
        self._mdns_worker.error_signal.connect(self._on_error)

        # Timer driven refresh.
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setInterval(self._refresh_ms)
        self._refresh_timer.timeout.connect(self._mdns_worker.refresh)
        self._refresh_timer.start()

        # Connect the timer and the worker to the UI.
        self._spin.valueChanged.connect(self._refresh_timer.setInterval)
        self._refresh_btn.clicked.connect(self._mdns_worker.refresh)
        self._table.itemSelectionChanged.connect(lambda: self._connect_btn.setEnabled(bool(self._table.selectedItems())))
        self._status.setText("Scanning...")

    def _trim_service_list(self, service_list: list, trim_by_key: str) -> list:
        """Merges entries in the list if they have a matching key."""
        merged = {}
        for entry in service_list:
            key = entry[trim_by_key]
            if key in merged:
                if entry["name"]: merged[key]["name"] += ' | ' + entry["name"]
            else:
                merged[key] = entry.copy()
        return list(merged.values())

    def _populate(self, auto_discovered_services: list):
        """Populate the service table."""

        # Store the service list that zconf found.
        self._prev_auto_discovered_services = auto_discovered_services

        # Build a single list with a combination of the auto discovered and manual entered services.
        all_services = auto_discovered_services.copy()
        for ip in self._manual_entry_services:
            all_services.append({
                "name": "",
                "host": "",
                "ip": ip,
                "port": "",
                "txt": ""
            })

        # Trim the list (merge duplicates).
        all_services = self._trim_service_list(all_services, "ip")

        # Build the table widget.
        self._table.setRowCount(len(all_services))
        for r, service in enumerate(all_services):
            self._table.setItem(r,self._Column.NAME.value, QtWidgets.QTableWidgetItem(service["name"]))
            self._table.setItem(r, self._Column.HOST.value, QtWidgets.QTableWidgetItem(service["host"]))
            self._table.setItem(r, self._Column.IP.value, QtWidgets.QTableWidgetItem(service["ip"]))
            self._table.setItem(r, self._Column.PORT.value, QtWidgets.QTableWidgetItem(str(service["port"])))
            self._table.setItem(r, self._Column.TXT.value, QtWidgets.QTableWidgetItem(service["txt"]))

        # Use the status to indicate what's happenning.
        if not self._block_status_updates:
            if not all_services: self._status.setText("Scanning...")
            else: self._status.setText(f"{len(all_services)} service(s) — updated")

    def _on_disconnect(self):
        """Disconnect button logic."""
        if not self._external_selected: return
        if self._user_on_connect:
            self._user_on_connect("", "")
            self._block_status_updates = True
            self._refresh_timer.stop()
            self._status.setText(f"Disconnected from {self._external_selected_ip}")
            self._disconnect_btn.setDisabled(True)
            self._disconnect_btn.setToolTip(None)
            QtWidgets.QApplication.processEvents()
            time.sleep(1)
            self._block_status_updates = False
            self._refresh_timer.start()

    def _on_refresh_btn_clicked(self):
        """Refresh button logic."""
        # When refresh is clicked, stop the refresh timer since the refresh method of the mdns_worker is not reentrant.
        self._refresh_timer.stop()
        QtCore.QTimer.singleShot(0, self._mdns_worker.refresh)
        self._refresh_timer.start()

    def _on_error(self, msg: str):
        """Set the status label to a given error message."""
        self._status.setText(f"Error: {msg}")

    def _on_add_entry(self):
        """Add manual entries to the mDNS service table using an input dialog."""
        ip, ok = QtWidgets.QInputDialog.getText(self, "Add Entry", "Enter IP address:")
        if ok and ip:
            self._manual_entry_services.append(ip)
            self._populate(self._prev_auto_discovered_services)

    def _on_connect(self):
        """Connect logic."""
        # Get the selected entry from the table.
        selected = self.get_selected()
        if selected is None or not selected["IP"]: return

        # Stop the refresh timer.
        self._block_status_updates = True
        self._refresh_timer.stop()

        # Use the status to indicate the connection address. Prefer Host (mDNS name) over IP.
        if selected["Host"]:
            self._status.setText(f"Connecting to {selected["Host"]}...")
        else:
            self._status.setText(f"Connecting to {selected["IP"]}...")

        # Note use of processEvents() to update the UI before and after _user_on_connect() which can be a long-running function.
        # This is also true for other long block calls like sleep()
        QtWidgets.QApplication.processEvents()

        # Call the user_on_connect with the IP and host mdns name. The user is expected to return True if it wants to accept the selected.
        # If _user_on_connect returned true, we can accept and the dialog will be closed.
        if self._user_on_connect is None: pass
        else:
            connected = self._user_on_connect(selected["IP"], selected["Host"])
            if connected:
                self._status.setText("Connected")
                QtWidgets.QApplication.processEvents()
                time.sleep(1)
                self._block_status_updates = False
            else:
                self._status.setText("Connection refused")
                QtWidgets.QApplication.processEvents()
                time.sleep(1)
                self._block_status_updates = False
                self._refresh_timer.start()
                return

        # Close the dialog, we're done.
        self.accept()

    @override
    def exec(self):
        """Override parent class method in order to initialize MDNSWorker related stuff upon execution."""
        self._init_worker_and_timer()
        self._disconnect_btn.setEnabled(self._external_selected)
        if self._external_selected: self._disconnect_btn.setToolTip(f"Disconnect from {self._external_selected_ip}")
        else: self._disconnect_btn.setToolTip(None)
        return super().exec()

    @override
    def closeEvent(self, a0: Optional[QtGui.QCloseEvent]):
        """Override parent class method in order to clean MDNSWorker related stuff upon close."""
        self._refresh_timer.stop()
        self._mdns_worker.stop()
        self._external_selected = False
        super().closeEvent(a0)

    def set_selected(self, ip: str):
        """Selects an entry in the table based on the given IP, if the IP is not present it will be added and then selected"""
        self._external_selected = True
        self._external_selected_ip = ip

    def get_selected(self) -> Optional[dict[str, str]]:
        """Returns the IP address of the currently selected service in the table or None if nothing is selected."""
        selected = self._table.selectedItems()
        if not selected: return None
        row = self._table.currentRow()
        name_item = self._table.item(row, self._Column.NAME.value)
        host_item = self._table.item(row, self._Column.HOST.value)
        ip_item = self._table.item(row, self._Column.IP.value)
        port_item = self._table.item(row, self._Column.PORT.value)
        txt_item = self._table.item(row, self._Column.TXT.value)
        name = name_item.text() if name_item is not None else ""
        host = host_item.text() if host_item is not None else ""
        ip = ip_item.text() if ip_item is not None else ""
        port = port_item.text() if port_item is not None else ""
        txt = txt_item.text() if txt_item is not None else ""
        selected_dict = {"Name": name, "Host": host, "IP": ip, "Port": port, "TXT": txt}
        return selected_dict
