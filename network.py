import gi
gi.require_version("Gtk", "3.0")
gi.require_version('NM', '1.0')
from gi.repository import Gtk, Gdk, GLib, GObject, Gio, NM
import uuid
import subprocess

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.label import Label
from fabric.widgets.image import Image
from fabric.widgets.revealer import Revealer
from fabric.widgets.scrolledwindow import ScrolledWindow
from fabric.widgets.stack import Stack

from widgets import FakeEntry

def log(tag, message):
    print(f"[{tag}] {message}")

class NetworkService(GObject.Object):
    __gsignals__ = {
        'state-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'ap-list-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'connection-failed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    NM_STATE_MAP = { 0: 'UNKNOWN', 10: 'ASLEEP', 20: 'DISCONNECTED', 30: 'DISCONNECTING', 40: 'CONNECTING', 50: 'CONNECTED_LOCAL', 60: 'CONNECTED_SITE', 70: 'CONNECTED_GLOBAL', }
    NM_DEVICE_STATE_MAP = { 0: 'UNKNOWN', 10: 'UNMANAGED', 20: 'UNAVAILABLE', 30: 'DISCONNECTED', 40: 'PREPARE', 50: 'CONFIG', 60: 'NEED_AUTH', 70: 'IP_CONFIG', 80: 'IP_CHECK', 90: 'SECONDARIES', 100: 'ACTIVATED', 110: 'DEACTIVATING', 120: 'FAILED', }
    NM_DEVICE_TYPE_WIFI = 2
    NM_DEVICE_TYPE_ETHERNET = 1

    def __init__(self):
        super().__init__()
        # Internal state and list cache
        self.nm_state = 0
        self.active_ap_path = None
        self.activating_ap_path = None
        self.active_connection_type = None
        self.is_activating = False
        self.is_scanning = False
        self.access_points = []

        try:
            self.properties_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path='/org/freedesktop/NetworkManager', interface_name='org.freedesktop.DBus.Properties', cancellable=None)
            self.manager_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path='/org/freedesktop/NetworkManager', interface_name='org.freedesktop.NetworkManager', cancellable=None)
            self.settings_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path='/org/freedesktop/NetworkManager/Settings', interface_name='org.freedesktop.NetworkManager.Settings', cancellable=None)
            
            self.properties_proxy.connect('g-signal', self._on_dbus_signal)
            self._find_and_connect_wifi_device()
            
            # Perform a full initial cache population once the event loop is ready.
            GLib.idle_add(self.force_state_and_list_update)

        except GLib.Error as e:
            log("SVC:FATAL", f"Could not complete initialization: {e}")

    def force_state_and_list_update(self):
        """Updates BOTH state and the AP list cache, then notifies the UI."""
        log("SVC:UPDATE", "Forcing full state AND list cache update...")
        self._update_state_cache()
        self._update_ap_list_cache()
        log("SVC:EMIT", "Update complete. Emitting 'state-changed' AND 'ap-list-changed'.")
        self.emit('state-changed')
        self.emit('ap-list-changed')
        return False

    def _update_state_cache(self):
        """Silently updates only the connection state cache."""
        try:
            all_props = self.properties_proxy.GetAll('(s)', 'org.freedesktop.NetworkManager')
            self._process_property_changes(all_props)
        except GLib.Error as e:
            log("SVC:ERROR", f"Error updating state cache: {e}")

    def _on_dbus_signal(self, proxy, sender_name, signal_name, parameters):
        if signal_name == 'PropertiesChanged':
            interface_name, changed_properties, invalidated_properties = parameters.unpack()
            self._process_property_changes(changed_properties)
            
    def _process_property_changes(self, props):
        """Processes property changes and emits state-changed if needed."""
        state_was_updated = False
        if 'State' in props:
            new_state = props['State']
            if self.nm_state != new_state:
                self.nm_state = new_state
                state_was_updated = True
        if 'PrimaryConnection' in props:
            primary_conn_path = props['PrimaryConnection']
            new_active_ap_path = self._get_specific_object_path(primary_conn_path)
            if self.active_ap_path != new_active_ap_path:
                self.active_ap_path = new_active_ap_path
                self._update_device_type(primary_conn_path)
                state_was_updated = True
        if 'ActivatingConnection' in props:
            activating_conn_path = props['ActivatingConnection']
            is_now_activating = (activating_conn_path and activating_conn_path != '/')
            new_activating_ap_path = self._get_specific_object_path(activating_conn_path)
            if self.is_activating != is_now_activating or self.activating_ap_path != new_activating_ap_path:
                self.is_activating = is_now_activating
                self.activating_ap_path = new_activating_ap_path
                state_was_updated = True
        
        if state_was_updated:
            log("SVC:EMIT", "State has changed. Emitting 'state-changed'.")
            self.emit('state-changed')
            
    def _on_wifi_device_signal(self, proxy, sender_name, signal_name, parameters):
        if signal_name != 'PropertiesChanged': return
        try:
            interface_name, changed_properties, invalidated = parameters.unpack()
            if interface_name == 'org.freedesktop.NetworkManager.Device.Wireless':
                if 'LastScan' in changed_properties and self.is_scanning:
                    self.is_scanning = False
                    self._update_ap_list_cache()
                    self.emit('ap-list-changed')
            elif interface_name == 'org.freedesktop.NetworkManager.Device':
                if 'State' in changed_properties:
                    new_state_value = changed_properties['State']
                    if new_state_value == 120:
                        log("RFAIL", f"{self.activating_ap_path}")
                        self.emit('connection-failed')
                        log("RFAIL", "end")
                    elif new_state_value == 100: self._update_state_cache(); self.emit('state-changed')
        except Exception as e:
            log("SVC:ERROR", f"in wifi signal handler: {e}")
            
    def _update_ap_list_cache(self):
        """Performs a live D-Bus query to update the internal AP list cache."""
        new_ap_list = []
        if not self.manager_proxy:
            self.access_points = []
            return
        try:
            for path in self.manager_proxy.GetDevices():
                proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
                if proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Device', 'DeviceType') == self.NM_DEVICE_TYPE_WIFI:
                    wireless_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=path, interface_name='org.freedesktop.NetworkManager.Device.Wireless', cancellable=None)
                    for ap_path in wireless_proxy.GetAllAccessPoints():
                        ap_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=ap_path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
                        ssid_bytes = ap_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.AccessPoint', 'Ssid')
                        strength = ap_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.AccessPoint', 'Strength')
                        ssid = bytes(ssid_bytes).decode('utf-8', 'ignore') if ssid_bytes else None
                        if ssid: new_ap_list.append({'ssid': ssid, 'strength': strength, 'path': ap_path})
        except GLib.Error as e:
            log("SVC:ERROR", f"Error building AP cache: {e}")
        self.access_points = sorted(new_ap_list, key=lambda ap: ap['strength'], reverse=True)

    def get_wifi_access_points(self): return self.access_points

    def _get_specific_object_path(self, connection_path):
        if not connection_path or connection_path == '/': return None
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=connection_path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
            return proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Connection.Active', 'SpecificObject')
        except GLib.Error: return None

    def _update_device_type(self, connection_path):
        self.active_connection_type = None
        if not connection_path or connection_path == '/': return
        try:
            proxy1 = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=connection_path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
            device_path = proxy1.Get('(ss)', 'org.freedesktop.NetworkManager.Connection.Active', 'Devices')[0]
            proxy2 = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=device_path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
            self.active_connection_type = proxy2.Get('(ss)', 'org.freedesktop.NetworkManager.Device', 'DeviceType')
        except (GLib.Error, IndexError): self.active_connection_type = None

    def _find_and_connect_wifi_device(self):
        wifi_path = self._get_wifi_device_path()
        if not wifi_path: return
        try:
            self.wifi_device_wireless_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=wifi_path, interface_name='org.freedesktop.NetworkManager.Device.Wireless', cancellable=None)
            self.wifi_device_properties_proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=wifi_path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
            self.wifi_device_properties_proxy.connect('g-signal', self._on_wifi_device_signal)
        except GLib.Error as e:
            log("SVC:ERROR", f"Could not create wifi device proxies: {e}")

    def get_state(self): return self.nm_state
    def get_active_connection_type(self): return self.active_connection_type
    def get_active_ap_path(self): return self.active_ap_path
    def get_activating_ap_path(self): return self.activating_ap_path

    def activate_ap_connection(self, ap_path, password=""):
        """
        Activates a connection by first trying to find and use a saved profile.
        If a password is provided for an existing connection, it updates that connection
        by removing the old profile and creating a new one with the new password.
        If no saved profile is found, it creates a robust, temporary profile
        that is NOT saved, which prevents duplicate connections.
        It always enforces the 'no-retry' rule on new connections.
        """
        device_path = self._get_wifi_device_path()
        if not device_path:
            log("SVC:ERROR", "Cannot activate: Wi-Fi device not found.")
            return

        try:
            ap_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM,
                flags=Gio.DBusProxyFlags.NONE,
                info=None,
                name='org.freedesktop.NetworkManager',
                object_path=ap_path,
                interface_name='org.freedesktop.DBus.Properties',
                cancellable=None
            )

            ssid_bytes = ap_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.AccessPoint', 'Ssid')
            ssid = bytes(ssid_bytes).decode('utf-8', 'ignore')

            existing_connections = self.settings_proxy.ListConnections()
            for conn_path in existing_connections:
                conn_settings_proxy = Gio.DBusProxy.new_for_bus_sync(
                    bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                    name='org.freedesktop.NetworkManager', object_path=conn_path,
                    interface_name='org.freedesktop.NetworkManager.Settings.Connection',
                    cancellable=None
                )
                settings = conn_settings_proxy.GetSettings()

                wireless_settings = settings.get('802-11-wireless')
                if wireless_settings and wireless_settings.get('ssid') == ssid_bytes:
                    log("SVC:INFO", f"Found existing connection profile for SSID: {ssid}")
                    
                    if password:
                        log("SVC:INFO", "New password provided for existing connection. Removing old profile.")
                        conn_settings_proxy.Delete()
                        # Break the loop to proceed with creating a new connection with the new password.
                        break
                    else:
                        # If no new password is provided, activate the existing connection.
                        self.manager_proxy.ActivateConnection('(ooo)', conn_path, device_path, ap_path)
                        log("SVC:INFO", "Activation of existing connection successful.")
                        return

            log("SVC:INFO", "No existing profile found or old one removed. Creating a new temporary connection.")

            connection_profile = {
                'connection': {
                    'id': GLib.Variant('s', ssid),
                    'type': GLib.Variant('s', '802-11-wireless'),
                    'uuid': GLib.Variant('s', str(uuid.uuid4())),
                    'autoconnect-retries': GLib.Variant('i', 0)
                },
                '802-11-wireless': {
                    'ssid': GLib.Variant('ay', ssid_bytes),
                    'mode': GLib.Variant('s', 'infrastructure'),
                },
                'ipv4': {'method': GLib.Variant('s', 'auto')},
                'ipv6': {'method': GLib.Variant('s', 'auto')}
            }

            if password:
                log("SVC:INFO", f"Setting up new connection with password for SSID: {ssid}.")
                connection_profile['802-11-wireless']['security'] = GLib.Variant('s', '802-11-wireless-security')
                connection_profile['802-11-wireless-security'] = {
                    'key-mgmt': GLib.Variant('s', 'wpa-psk'),
                    'psk': GLib.Variant('s', password)
                }
            else:
                log("SVC:INFO", f"Setting up new open connection for SSID: {ssid}.")

            self.manager_proxy.AddAndActivateConnection(
                '(a{sa{sv}}oo)',
                connection_profile,
                device_path,
                ap_path
            )
            log("SVC:INFO", "Successfully activated a new temporary connection.")

        except GLib.Error as e:
            log("SVC:ERROR", f"Failed to activate connection: {e}")

    def _get_wifi_device_path(self):
        if not self.manager_proxy: return None
        try:
            for path in self.manager_proxy.GetDevices():
                proxy = Gio.DBusProxy.new_for_bus_sync(bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None, name='org.freedesktop.NetworkManager', object_path=path, interface_name='org.freedesktop.DBus.Properties', cancellable=None)
                if proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Device', 'DeviceType') == self.NM_DEVICE_TYPE_WIFI: return path
        except GLib.Error: return None
        return None

    def request_scan(self):
        if not self.wifi_device_wireless_proxy: return
        try:
            self.is_scanning = True
            self.emit('state-changed')
            self.wifi_device_wireless_proxy.RequestScan('(a{sv})', {})
        except GLib.Error as e:
            self.is_scanning = False
            self.emit('state-changed')
            log("SVC:ERROR", f"Failed to request scan: {e}")

    def deactivate_current_connection(self):
        try:
            active_conn_path = self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'PrimaryConnection')
            if active_conn_path and active_conn_path != '/': self.manager_proxy.DeactivateConnection('(o)', active_conn_path)
        except GLib.Error as e:
            log("SVC:ERROR", f"Failed to deactivate connection: {e}")

class AccessPointRow(Box):
    __gsignals__ = {'toggled': (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_OBJECT,)),}
    def __init__(self, network_service, ap_data, is_active, is_activating, needs_password):
        super().__init__(orientation='v', spacing=0)
        self.network_service = network_service
        self.ap_data = ap_data
        self.password_entry = None
        top_row_button = Button(name="network-ap-button")
        top_row_button.connect('clicked', self._on_toggled)
        row_content = Box(orientation='h', spacing=10)
        icon = Image(icon_name=self.get_strength_icon(ap_data['strength']), icon_size=16)
        label_box = Box(orientation='v', spacing=0)
        label_box.pack_start(Label(label=ap_data['ssid'], h_align="start"), False, False, 0)
        if is_activating:
            status_label = Label(label="Connecting...", h_align="start")
            status_label.get_style_context().add_class("dim-label")
            label_box.pack_start(status_label, False, False, 0)
            top_row_button.get_style_context().add_class("dim-bg")
        elif is_active:
            status_label = Label(label="Connected", h_align="start")
            status_label.get_style_context().add_class("dim-label")
            label_box.pack_start(status_label, False, False, 0)
            top_row_button.get_style_context().add_class("active-bg")
        elif needs_password:
            status_label = Label(label="Needs password...", h_align="start")
            status_label.get_style_context().add_class("dim-label")
            label_box.pack_start(status_label, False, False, 0)
            self.get_style_context().add_class("dim-bg")   
        row_content.pack_start(icon, False, False, 5)
        row_content.pack_start(label_box, True, True, 0)
        top_row_button.add(row_content)
        self.pack_start(top_row_button, False, False, 0)
        self.revealer = Revealer(transition_type='slide-down', transition_duration=200)
        self.action_area = Box(orientation='v', name="network-action-area")
        self.revealer.add(self.action_area)
        self.pack_start(self.revealer, False, False, 0)
        self._build_action_area(is_active, is_activating, needs_password)
        if needs_password:
            self.revealer.set_reveal_child(True)

    def _build_action_area(self, is_active, is_activating, needs_password):
        for child in self.action_area.get_children(): child.destroy()
        if is_activating:
            pass
        elif is_active:
            btn = Button(label="Disconnect")
            btn.get_style_context().add_class("destructive-action")
            btn.connect('clicked', self._on_disconnect_clicked)
            self.action_area.add(btn)
        elif needs_password:
            self.password_entry = FakeEntry(placeholder="Enter password...")
            self.action_area.add(Box(name="wifi-entry", children=[self.password_entry]))
        else:
            btn = Button(label="Connect")
            btn.connect('clicked', self._on_connect_clicked)
            self.action_area.add(btn)
        self.action_area.show_all()
    
    def close_revealer(self): self.revealer.set_reveal_child(False)
    def _on_toggled(self, button):
        self.revealer.set_reveal_child(not self.revealer.get_reveal_child())
        self.emit('toggled', self)

    def _on_connect_clicked(self, button):
        self.network_service.activate_ap_connection(self.ap_data['path'])

    def _on_disconnect_clicked(self, button):
        self.network_service.deactivate_current_connection()

    def get_strength_icon(self, strength):
        if strength > 80: return 'network-wireless-signal-good-symbolic'
        if strength > 55: return 'network-wireless-signal-ok-symbolic'
        if strength > 30: return 'network-wireless-signal-weak-symbolic'
        return 'network-wireless-signal-none-symbolic'

class NetworkPopup(Box):
    def __init__(self, network_service, popup_manager):
        super().__init__(orientation='v', spacing=4, name="popup-menu-box")
        log("UI:INIT", "NetworkPopup creating...")
        self.network_service = network_service
        self.popup_manager = popup_manager
        self.row_widgets = []
        self.needs_password_ap = None
        self.password_entry = None
        header = Box(orientation='h', spacing=0)
        title = Label(label="Wireless Networks", h_align="start", name="heading")
        self.rescan_stack = Stack(transition_type="slide-up-down")
        rescan_button = Button(child=Image(icon_name="view-refresh-symbolic", icon_size=14))
        rescan_spinner = Label("...")
        self.rescan_stack.add_named(rescan_button, "button")
        self.rescan_stack.add_named(rescan_spinner, "spinner")
        self.set_size_request(300, 300)
        edit_button = Button(on_clicked=lambda: (subprocess.Popen("nm-connection-editor"), self.popup_manager.close_active_popup()))
        edit_button.add(Image(icon_name="document-edit", icon_size=14))
        header.pack_start(title, True, True, 0)
        header.pack_end(self.rescan_stack, False, False, 0)
        header.pack_end(edit_button, False, False, 0)
        self.pack_start(header, False, False, 5)
        scrolled_window = ScrolledWindow(h_policy="never", v_policy="automatic")
        self.results_box = Box(orientation='v', spacing=4)
        scrolled_window.add(self.results_box)
        self.pack_start(scrolled_window, True, True, 0)
        
        log("UI:INIT", "Connecting to service signals...")
        rescan_button.connect('clicked', lambda b: self.network_service.request_scan())
        self.ap_list_changed_handler_id = self.network_service.connect('ap-list-changed', self.build_network_list)
        self.state_changed_handler_id = self.network_service.connect('state-changed', self.build_network_list)
        self.connection_failed_handler_id = self.network_service.connect('connection-failed', self.on_connection_failed)
        self.connect('destroy', self._on_destroy)

        log("UI:INIT", "Performing initial build and requesting background scan.")
        # self.build_network_list()
        self.network_service.request_scan()
        
        self.show_all()
    
    def build_network_list(self, *args):
        log("UI:BUILD", "Rebuilding network list.")
        self.row_widgets = []
        for child in self.results_box.get_children(): self.results_box.remove(child)
        
        active_ap_path = self.network_service.get_active_ap_path()
        access_points = self.network_service.get_wifi_access_points()
        is_activating = self.network_service.is_activating
        activating_ap_path = self.network_service.get_activating_ap_path()
        is_scanning = self.network_service.is_scanning
        
        log("UI:BUILD", f"  -> Building with state: active='{active_ap_path}', activating='{activating_ap_path}', scanning={is_scanning}, num_aps={len(access_points)}, aps={access_points}")

        self.rescan_stack.set_visible_child_name("spinner" if is_scanning else "button")
        if active_ap_path == self.needs_password_ap:
            self.needs_password_ap = None

        if not access_points and not is_scanning:
            self.results_box.add(Label(label="No networks found. Scanning..."))
            self.results_box.show_all()
            return

        for ap in access_points:
            is_active = (ap['path'] == active_ap_path)
            is_activating_ap = (is_activating and ap['path'] == activating_ap_path)
            needs_password = self.needs_password_ap == ap['path']
            row_widget = AccessPointRow(self.network_service, ap, is_active, is_activating_ap, needs_password)
            row_widget.connect('toggled', self._on_row_toggled)
            row_widget.set_sensitive(not (is_activating and not is_activating_ap))
            if row_widget.password_entry:
                self.password_entry = row_widget.password_entry
            self.results_box.add(row_widget)
            self.row_widgets.append(row_widget)
        self.results_box.show_all()
    
    def on_connection_failed(self):
        self.needs_password_ap = self.network_service.get_activating_ap_path()
        print(f"FAIL {self.needs_password_ap}")
    
    def handle_key_press(self, widget, event_key):
        if not self.password_entry:
            return Gdk.EVENT_STOP
        key_name = Gdk.keyval_name(event_key.keyval)

        if key_name in ("Return", "KP_Enter"):
            self.network_service.activate_ap_connection(self.needs_password_ap, password=self.password_entry.text_buffer)
            return Gdk.EVENT_STOP

        self.password_entry.handle_key_press(event_key)
        return Gdk.EVENT_STOP
    
    def _on_destroy(self, *args):
        if self.ap_list_changed_handler_id: self.network_service.disconnect(self.ap_list_changed_handler_id)
        if self.state_changed_handler_id: self.network_service.disconnect(self.state_changed_handler_id)
        if self.connection_failed_handler_id: self.network_service.disconnect(self.connection_failed_handler_id)

    def _on_row_toggled(self, toggled_row, emitting_row):
        for row in self.row_widgets:
            if row != emitting_row:
                row.close_revealer()

class NetworkWidget(Box):
    def __init__(self, network_service, popup_manager):
        super().__init__()
        self.network_service = network_service
        self.popup_manager = popup_manager
        self.icon = Image(icon_size=24)
        button = Button(name="task-button", child=Box(name="task-button-inner", children=[self.icon]))
        self.add(button)
        self.network_service.connect('state-changed', self._update_icon)
        self._update_icon()
        self.popup_manager.attach(button, self._create_network_popup, 'left-click')

    def _create_network_popup(self):
        return NetworkPopup(self.network_service, self.popup_manager)

    def _update_icon(self, *args):
        state = self.network_service.get_state()
        icon_name = 'network-wireless-disconnected-symbolic'
        if state >= 70: # CONNECTED_GLOBAL
            conn_type = self.network_service.get_active_connection_type()
            if conn_type == self.network_service.NM_DEVICE_TYPE_WIFI:
                icon_name = 'network-wireless-signal-good-symbolic'
            elif conn_type == self.network_service.NM_DEVICE_TYPE_ETHERNET:
                icon_name = 'network-wired-symbolic'
        elif self.network_service.is_activating:
            icon_name = 'network-cellular-acquiring-symbolic'
        self.icon.set_from_icon_name(icon_name, 16)