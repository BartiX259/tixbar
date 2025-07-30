import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GObject, Gio

from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.label import Label
from fabric.widgets.image import Image
from fabric.widgets.revealer import Revealer
from fabric.widgets.scrolledwindow import ScrolledWindow
from fabric.widgets.stack import Stack

class NetworkService(GObject.Object):
    __gsignals__ = {
        'state-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'ap-list-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    NM_STATE_MAP = {
        0: 'UNKNOWN', 10: 'ASLEEP', 20: 'DISCONNECTED', 30: 'DISCONNECTING',
        40: 'CONNECTING', 50: 'CONNECTED_LOCAL', 60: 'CONNECTED_SITE',
        70: 'CONNECTED_GLOBAL',
    }
    NM_DEVICE_TYPE_WIFI = 2
    NM_DEVICE_TYPE_ETHERNET = 1

    def __init__(self):
        super().__init__()
        self.properties_proxy = None
        self.manager_proxy = None
        self.settings_proxy = None
        self.wifi_device_wireless_proxy = None
        self.wifi_device_properties_proxy = None

        self.active_connection_type = None
        self.is_activating = False
        self.is_scanning = False

        try:
            self.properties_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path='/org/freedesktop/NetworkManager',
                interface_name='org.freedesktop.DBus.Properties', cancellable=None
            )
            self.manager_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path='/org/freedesktop/NetworkManager',
                interface_name='org.freedesktop.NetworkManager', cancellable=None
            )
            self.settings_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path='/org/freedesktop/NetworkManager/Settings',
                interface_name='org.freedesktop.NetworkManager.Settings', cancellable=None
            )

            self.properties_proxy.connect('g-signal', self._on_dbus_signal)
            
            self._update_activating_state()
            self._update_active_connection_info()
            self._find_and_connect_wifi_device()

        except GLib.Error as e:
            print(f"[NetworkService] FATAL: Could not complete initialization: {e}")

    def _on_dbus_signal(self, proxy, sender_name, signal_name, parameters):
        if signal_name != 'PropertiesChanged':
            return
        try:
            _, changed_properties, _ = parameters.unpack()

            GLib.idle_add(self._process_property_changes, changed_properties)
        except Exception as e:
             print(f"[NetworkService] Error unpacking PropertiesChanged signal: {e}")
    
    def _process_property_changes(self, changed_properties):
        """Processes property changes on the main thread to ensure UI safety."""
        if 'ActivatingConnection' in changed_properties:
            self._update_activating_state()
            self.emit('state-changed')

        if 'State' in changed_properties or 'PrimaryConnection' in changed_properties:
            self._update_active_connection_info()
            self.emit('state-changed')
        
        # Return False to tell GLib not to call this idle function again
        return False

    def _update_activating_state(self):
        """Reads and updates the is_activating flag from DBus."""
        try:
            activating_path = self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'ActivatingConnection')
            self.is_activating = (activating_path and activating_path != '/')
        except GLib.Error:
            self.is_activating = False

    def _update_active_connection_info(self):
        try:
            active_conn_path = self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'PrimaryConnection')
            self._update_device_type(active_conn_path)
        except GLib.Error:
            self.active_connection_type = None

    def _update_device_type(self, connection_path):
        self.active_connection_type = None
        if not connection_path or connection_path == '/':
            return
        try:
            conn_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=connection_path,
                interface_name='org.freedesktop.DBus.Properties', cancellable=None
            )
            device_path = conn_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Connection.Active', 'Devices')[0]
            device_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=device_path,
                interface_name='org.freedesktop.DBus.Properties', cancellable=None
            )
            self.active_connection_type = device_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Device', 'DeviceType')
        except (GLib.Error, IndexError):
            self.active_connection_type = None

    def _find_and_connect_wifi_device(self):
        wifi_path = self._get_wifi_device_path()
        if not wifi_path:
            return
        try:
            # FIX: Create the proxy for calling methods on the Wireless interface
            self.wifi_device_wireless_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=wifi_path,
                interface_name='org.freedesktop.NetworkManager.Device.Wireless',
                cancellable=None
            )
            # FIX: Create a SEPARATE proxy for listening to property changes on the same object
            self.wifi_device_properties_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=wifi_path,
                interface_name='org.freedesktop.DBus.Properties', # The standard properties interface
                cancellable=None
            )
            # FIX: Connect the signal handler to the PROPERTIES proxy
            self.wifi_device_properties_proxy.connect('g-signal', self._on_wifi_device_signal)
        except GLib.Error as e:
            self.wifi_device_wireless_proxy = None
            self.wifi_device_properties_proxy = None
            print(f"[NetworkService] Could not create wifi device proxies: {e}")

    def _on_wifi_device_signal(self, proxy, sender_name, signal_name, parameters):
        """Handles signals from the Wireless device's Properties interface."""
        if signal_name != 'PropertiesChanged':
            return
        
        try:
            # The first parameter is the interface name whose properties changed
            interface_name, changed_properties, invalidated = parameters.unpack()

            # We only care about changes on the Wireless interface
            if interface_name == 'org.freedesktop.NetworkManager.Device.Wireless':
                # The 'LastScan' property is updated when a scan finishes. This is our trigger.
                if 'LastScan' in changed_properties:
                    print("[NetworkService] Scan finished (LastScan updated). Emitting ap-list-changed signal.")
                    self.is_scanning = False
                    GLib.idle_add(self.emit, 'ap-list-changed')
        except Exception as e:
            print(f"[NetworkService] Error processing wifi device signal: {e}")


    def _get_wifi_device_path(self):
        if not self.manager_proxy: return None
        try:
            device_paths = self.manager_proxy.GetDevices()
            for path in device_paths:
                device_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                    bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                    name='org.freedesktop.NetworkManager', object_path=path,
                    interface_name='org.freedesktop.DBus.Properties', cancellable=None
                )
                if device_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Device', 'DeviceType') == self.NM_DEVICE_TYPE_WIFI:
                    return path
        except GLib.Error:
            return None
        return None

    def get_state(self):
        if not self.properties_proxy: return 0
        try:
            return self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'State')
        except GLib.Error:
            return 0
    
    def get_active_connection_type(self):
        return self.active_connection_type
    
    def get_active_ap_path(self):
        try:
            active_conn_path = self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'PrimaryConnection')
            if not active_conn_path or active_conn_path == '/': return None
            conn_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=active_conn_path,
                interface_name='org.freedesktop.DBus.Properties', cancellable=None
            )
            return conn_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Connection.Active', 'SpecificObject')
        except GLib.Error:
            return None

    def get_activating_ap_path(self):
        if not self.is_activating: return None
        try:
            activating_conn_path = self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'ActivatingConnection')
            if not activating_conn_path or activating_conn_path == '/': return None
            conn_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=activating_conn_path,
                interface_name='org.freedesktop.DBus.Properties', cancellable=None
            )
            return conn_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Connection.Active', 'SpecificObject')
        except GLib.Error:
            return None

    def get_wifi_access_points(self):
        if not self.manager_proxy: return []
        access_points = []
        try:
            device_paths = self.manager_proxy.GetDevices()
        except GLib.Error: return []
        
        wifi_device_paths = []
        for path in device_paths:
            try:
                device_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                    bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                    name='org.freedesktop.NetworkManager', object_path=path,
                    interface_name='org.freedesktop.DBus.Properties', cancellable=None
                )
                if device_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.Device', 'DeviceType') == self.NM_DEVICE_TYPE_WIFI:
                    wifi_device_paths.append(path)
            except GLib.Error: continue
        
        for wifi_path in wifi_device_paths:
            try:
                wireless_proxy = Gio.DBusProxy.new_for_bus_sync(
                    bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                    name='org.freedesktop.NetworkManager', object_path=wifi_path,
                    interface_name='org.freedesktop.NetworkManager.Device.Wireless', cancellable=None
                )
                ap_paths = wireless_proxy.GetAllAccessPoints()
                for ap_path in ap_paths:
                    ap_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                        bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                        name='org.freedesktop.NetworkManager', object_path=ap_path,
                        interface_name='org.freedesktop.DBus.Properties', cancellable=None
                    )
                    ssid_bytes = ap_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.AccessPoint', 'Ssid')
                    strength = ap_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.AccessPoint', 'Strength')
                    ssid = bytes(ssid_bytes).decode('utf-8', 'ignore') if ssid_bytes else None
                    if ssid:
                        access_points.append({'ssid': ssid, 'strength': strength, 'path': ap_path})
            except GLib.Error: continue
        
        return sorted(access_points, key=lambda ap: ap['strength'], reverse=True)

    def request_scan(self):
        """Tells NetworkManager to perform a new Wi-Fi scan."""
        # FIX: Use the WIRELESS proxy to call the method
        if not self.wifi_device_wireless_proxy: return
        try:
            print("[NetworkService] Requesting new Wi-Fi scan...")
            self.is_scanning = True
            self.wifi_device_wireless_proxy.RequestScan('(a{sv})', {})
            self.emit('ap-list-changed')
        except GLib.Error as e:
            print(f"[NetworkService] Failed to request scan: {e}")

    def activate_ap_connection(self, ap_path):
        device_path = self._get_wifi_device_path()
        if not device_path: return
        try:
            ap_props_proxy = Gio.DBusProxy.new_for_bus_sync(
                bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                name='org.freedesktop.NetworkManager', object_path=ap_path,
                interface_name='org.freedesktop.DBus.Properties', cancellable=None
            )
            ssid_bytes = ap_props_proxy.Get('(ss)', 'org.freedesktop.NetworkManager.AccessPoint', 'Ssid')
            
            saved_connections = self.settings_proxy.ListConnections()
            matching_connection_path = None
            for conn_path in saved_connections:
                conn_proxy = Gio.DBusProxy.new_for_bus_sync(
                    bus_type=Gio.BusType.SYSTEM, flags=Gio.DBusProxyFlags.NONE, info=None,
                    name='org.freedesktop.NetworkManager', object_path=conn_path,
                    interface_name='org.freedesktop.NetworkManager.Settings.Connection',
                    cancellable=None
                )
                settings = conn_proxy.GetSettings()
                if '802-11-wireless' in settings and 'ssid' in settings['802-11-wireless']:
                    if settings['802-11-wireless']['ssid'] == ssid_bytes:
                        matching_connection_path = conn_path
                        break
            
            if matching_connection_path:
                self.manager_proxy.ActivateConnection('(ooo)', matching_connection_path, device_path, ap_path)
            else:
                self.manager_proxy.AddAndActivateConnection('(a{sa{sv}}oo)', {}, device_path, ap_path)
        except GLib.Error as e:
            print(f"[NetworkService] Failed to activate connection: {e}")

    def deactivate_current_connection(self):
        try:
            active_conn_path = self.properties_proxy.Get('(ss)', 'org.freedesktop.NetworkManager', 'PrimaryConnection')
            if active_conn_path and active_conn_path != '/':
                self.manager_proxy.DeactivateConnection('(o)', active_conn_path)
        except GLib.Error as e:
            print(f"[NetworkService] Failed to deactivate connection: {e}")


class AccessPointRow(Box):
    __gsignals__ = {
        'toggled': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }
    def __init__(self, network_service, ap_data, is_active, is_activating):
        super().__init__(orientation='v', spacing=0)
        self.network_service = network_service
        self.ap_data = ap_data
        self.is_active = is_active
        self.is_activating = is_activating

        top_row_button = Button(name="network-ap-button")
        top_row_button.connect('clicked', self._on_toggled)

        row_content = Box(orientation='h', spacing=10)
        icon_name = self.get_strength_icon(self.ap_data['strength'])
        print(icon_name)
        icon = Image(icon_name=icon_name, icon_size=16)
        
        label_box = Box(orientation='v', spacing=0)
        ssid_label = Label(label=self.ap_data['ssid'], h_align="start")
        label_box.pack_start(ssid_label, False, False, 0)
        
        # --- NEW CONSOLIDATED LOGIC ---
        # Display the correct sub-label and apply the background style.
        if self.is_activating:
            status_label = Label(label="Connecting...", h_align="start")
            status_label.get_style_context().add_class("dim-label")
            label_box.pack_start(status_label, False, False, 0)
            top_row_button.get_style_context().add_class("dim-bg")
        elif self.is_active:
            status_label = Label(label="Connected", h_align="start")
            status_label.get_style_context().add_class("dim-label")
            label_box.pack_start(status_label, False, False, 0)
            top_row_button.get_style_context().add_class("active-bg")

        row_content.pack_start(icon, False, False, 5)
        row_content.pack_start(label_box, True, True, 0)
        
        top_row_button.add(row_content)
        self.pack_start(top_row_button, False, False, 0)

        self.revealer = Revealer(transition_type='slide-down', transition_duration=200)
        self.action_area = Box(orientation='v', name="network-action-area")
        self.revealer.add(self.action_area)
        self.pack_start(self.revealer, False, False, 0)

        self._build_action_area()

    def _build_action_area(self):
        # This method is now much simpler.
        for child in self.action_area.get_children():
            self.action_area.remove(child)

        # The action area is empty if we are connecting.
        if self.is_activating:
            pass
        elif self.is_active:
            disconnect_button = Button(label="Disconnect")
            disconnect_button.get_style_context().add_class("destructive-action")
            disconnect_button.connect('clicked', self._on_disconnect_clicked)
            self.action_area.add(disconnect_button)
        else:
            connect_button = Button(label="Connect")
            connect_button.connect('clicked', self._on_connect_clicked)
            self.action_area.add(connect_button)
        
        self.action_area.show_all()
    
    def close_revealer(self):
        self.revealer.set_reveal_child(False)

    def _on_toggled(self, button):
        self.revealer.set_reveal_child(not self.revealer.get_reveal_child())
        self.emit('toggled')
    
    def _on_connect_clicked(self, button):
        print(f"UI: Connect clicked for {self.ap_data['ssid']}")
        self.network_service.activate_ap_connection(self.ap_data['path'])

    def _on_disconnect_clicked(self, button):
        print("UI: Disconnect clicked")
        self.network_service.deactivate_current_connection()

    def get_strength_icon(self, strength):
        """Returns a symbolic icon name based on Wi-Fi signal strength."""
        if strength > 80: return 'network-wireless-signal-good-symbolic'
        if strength > 55: return 'network-wireless-signal-ok-symbolic'
        if strength > 5:  return 'network-wireless-signal-weak-symbolic'
        return 'network-wireless-signal-none-symbolic'

class NetworkPopup(Box):
    def __init__(self, network_service, popup_manager):
        super().__init__(orientation='v', spacing=4, name="popup-menu-box")
        self.network_service = network_service
        self.popup_manager = popup_manager
        self.debounce_timer_id = None
        
        # --- NEW: Add a header for title and rescan button ---
        header = Box(orientation='h', spacing=6)
        title = Label(label="Wireless Networks", h_align="start", name="heading")
        self.rescan_stack = Stack(transition_type="slide-up")
        rescan_button = Button(child=Image(icon_name="view-refresh", icon_size=14))
        rescan_button.connect('clicked', lambda b: self.network_service.request_scan())
        rescan_spinner = Label("...")
        self.rescan_stack.add_named(rescan_button, "button")
        self.rescan_stack.add_named(rescan_spinner, "spinner")
        
        self.set_size_request(300, 300)
        
        header.pack_start(title, True, True, 0)
        header.pack_end(self.rescan_stack, False, False, 0)
        self.pack_start(header, False, False, 5)
        
        scrolled_window = ScrolledWindow(h_policy="never", v_policy="automatic")
        self.results_box = Box(orientation='v', spacing=4)
        scrolled_window.add(self.results_box)
        
        self.pack_start(scrolled_window, True, True, 0)
        
        self.network_service.connect('ap-list-changed', self.build_network_list)
        self.network_service.connect('state-changed', self.build_network_list)

        self.network_service.request_scan()
        
        self.build_network_list_real()
        self.show_all()

    def build_network_list(self, *args):
        # If there's an existing timer, remove it
        if self.debounce_timer_id:
            GLib.source_remove(self.debounce_timer_id)
        
        # Start a new timer to call the build function after 150ms
        self.debounce_timer_id = GLib.timeout_add(150, self.build_network_list_real)
    
    def build_network_list_real(self):
        print("build")
        self.debounce_timer_id = None
        self.row_widgets = []
        
        for child in self.results_box.get_children():
            self.results_box.remove(child)

        active_ap_path = self.network_service.get_active_ap_path()
        access_points = self.network_service.get_wifi_access_points()
        is_activating = self.network_service.is_activating
        activating_ap_path = self.network_service.get_activating_ap_path()
        is_scanning = self.network_service.is_scanning

        # --- NEW: Update the rescan button's state ---
        if is_scanning:
            self.rescan_stack.set_visible_child_name("spinner")
        else:
            self.rescan_stack.set_visible_child_name("button")

        if not access_points:
            self.results_box.add(Label(label="No Wi-Fi networks found."))
            self.results_box.show_all()
            return

        seen_ssids = set()
        for ap in access_points:
            if ap['ssid'] in seen_ssids:
                continue
            seen_ssids.add(ap['ssid'])
            
            is_active = (ap['path'] == active_ap_path)
            is_activating_ap = (ap['path'] == activating_ap_path)
            
            row_widget = AccessPointRow(self.network_service, ap, is_active, is_activating_ap)
            row_widget.connect('toggled', self._on_row_toggled, row_widget)
            
            # The only logic needed here is to disable other rows during activation.
            if is_activating and not is_activating_ap:
                row_widget.set_sensitive(False)
            else:
                row_widget.set_sensitive(True)

            self.results_box.add(row_widget)
            self.row_widgets.append(row_widget)
        
        self.results_box.show_all()

    # NEW: Add the handler for the 'toggled' signal
    def _on_row_toggled(self, toggled_row):
        # First, find out if the revealer is about to open or close
        is_opening = not toggled_row.revealer.get_reveal_child()

        # Close all other revealers
        for row in self.row_widgets:
            if row != toggled_row:
                row.close_revealer()

class NetworkWidget(Box):
    def __init__(self, network_service, popup_manager):
        # We now inherit from Button directly
        super().__init__()
        self.network_service = network_service
        self.popup_manager = popup_manager

        # The icon will be the child of the button
        self.icon = Image(icon_name="fedora-logo-icon", icon_size=24)
        inner = Box(name="task-button-inner", children=[self.icon])
        button = Button(name="task-button", child=inner)
        self.add(button)

        # Connect to our custom signal from the service to update the icon
        self.network_service.connect('state-changed', self._update_icon)
        # Set the initial icon state
        self._update_icon()

        # Use the standard popup manager attachment
        self.popup_manager.attach(
            self, # The widget to attach to is this button itself
            self._create_network_popup,
            'left-click',
        )

    def _create_network_popup(self):
        """A factory function to create the popup."""
        return NetworkPopup(self.network_service, self.popup_manager)

    def _update_icon(self, *args):
        state = self.network_service.get_state()
        icon_name = 'network-offline-symbolic'

        if state >= 70: # CONNECTED_GLOBAL
            conn_type = self.network_service.get_active_connection_type()
            if conn_type == self.network_service.NM_DEVICE_TYPE_WIFI:
                # TODO: Get actual signal strength for a more accurate icon
                icon_name = 'network-wireless-signal-good-symbolic'
            elif conn_type == self.network_service.NM_DEVICE_TYPE_ETHERNET:
                icon_name = 'network-wired-symbolic'
        elif state >= 40 and state < 70:
            icon_name = 'network-cellular-acquiring-symbolic'
        
        self.icon.set_from_icon_name(icon_name, 16)