import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GObject, Gio
import subprocess
import sys
import shlex
import signal
import os
import json

from popup_manager import PopupManager

from fabric import Application
from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.centerbox import CenterBox
from fabric.widgets.datetime import DateTime
from fabric.widgets.entry import Entry
from fabric.widgets.eventbox import EventBox
from fabric.widgets.label import Label
from fabric.widgets.image import Image
from fabric.widgets.revealer import Revealer
from fabric.widgets.stack import Stack
from fabric.widgets.wayland import WaylandWindow as Window
from fabric.widgets.scrolledwindow import ScrolledWindow
from fabric.utils import get_relative_path

from network import NetworkService, NetworkWidget
from widgets import FakeEntry

toplevel_monitor_process = None
PINNED_APPS_FILE = "pinned_apps.json"

def parse_parameters(param_string):
    params = {}
    parts = shlex.split(param_string)
    for part in parts:
        if '=' in part:
            key, value = part.split('=', 1)
            params[key.lower()] = value
    return params

def parse_actions(data):
    actions_list = []
    if "actions" in data and data["actions"]:
        action_strings = data["actions"].split(';')
        for action_str in action_strings:
            if not action_str: continue
            parts = action_str.split('|')
            if len(parts) == 2:
                actions_list.append({"name": parts[0], "value": parts[1]})
    return actions_list

def send_command(command: str):
    if toplevel_monitor_process and toplevel_monitor_process.stdin:
        try:
            toplevel_monitor_process.stdin.write(command + "\n")
            toplevel_monitor_process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            print(f"Error sending command: {e}", file=sys.stderr)


# ===================================================================
# === SERVICE =======================================================
# ===================================================================

class AppService(GObject.GObject):
    __gsignals__ = {
        'data-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.db = {}
        self.windows = []
        self.pinned_app_ids = []
        self.real_active_window_id = None
        self._idle_update_source_id = None
        self.load_pinned_apps()

    def load_pinned_apps(self):
        if os.path.exists(PINNED_APPS_FILE):
            try:
                with open(PINNED_APPS_FILE, 'r') as f:
                    self.pinned_app_ids = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.pinned_app_ids = []

    def save_pinned_apps(self):
        try:
            with open(PINNED_APPS_FILE, 'w') as f:
                json.dump(self.pinned_app_ids, f, indent=4)
        except IOError as e:
            print(f"Error saving pinned apps: {e}", file=sys.stderr)

    def toggle_pin(self, app_id):
        if app_id in self.pinned_app_ids:
            self.pinned_app_ids.remove(app_id)
        else:
            self.pinned_app_ids.append(app_id)
        self.save_pinned_apps()
        self.emit('data-changed')
    
    def _emit_data_changed_idle(self):
        self.emit('data-changed')
        # Reset the source ID so a new update can be scheduled in the future.
        self._idle_update_source_id = None
        # Return False to tell GLib not to run this function again automatically.
        return False

    def update_from_daemon_line(self, line):
        line = line.strip()
        parts = line.split(" ", 1)
        command = parts[0]
        if command == "DAEMON_READY": return

        data = parts[1] if len(parts) > 1 else ""
        params = parse_parameters(data)
        appid = params.get("appid")

        if command == "DB":
            if not appid: return
            params["actions"] = parse_actions(params)
            self.db[appid] = params
            # Instead of emitting directly, schedule an idle update.
            # If one is already scheduled, this does nothing.
            if not self._idle_update_source_id:
                self._idle_update_source_id = GLib.idle_add(self._emit_data_changed_idle)
            return

        id_str = params.get("id")
        if id_str is None: return
        try:
            id = int(id_str)
        except ValueError:
            return

        if command == "NEW":
            self.windows.append({"id": id})
        elif command == "CLOSED":
            self.windows[:] = [w for w in self.windows if w.get("id") != id]
        elif command == "UPDATE":
            for w in self.windows:
                if w.get("id") == id:
                    w.update(appid=appid, icon=params.get("icon"), state=params.get("state"), title=params.get("title"), bin=self.db.get(appid, {}).get("bin"))
                    break

        self.emit('data-changed')


# ===================================================================
# === POPUPS ========================================================
# ===================================================================

class LeftClickMenuPopup(Box):
    def __init__(self, bar, app_service, popup_manager, app_windows, real_active_window_id):
        super().__init__(orientation='v', spacing=4, name="popup-menu-box")
        self.bar = bar
        self.app_service = app_service
        self.popup_manager = popup_manager
        self.real_active_window_id = real_active_window_id
        self.window_was_clicked_in_popup = False

        self.connect("destroy", self.on_popup_destroy)

        for window in app_windows:
            title = window.get("title") or f"Untitled Window ({window['id']})"
            win_button = Button(label=title, h_expand=True)
            close_button = Button(label="X")
            close_button.get_style_context().add_class("destructive-action")
            row_box = Box(h_expand=True, spacing=4)
            row_box.pack_start(win_button, True, True, 0)
            row_box.pack_start(close_button, False, False, 0)
            box = EventBox()
            box.add(row_box)
            win_button.connect("clicked", self.on_click, window['id'])
            close_button.connect("clicked", self.on_close, window['id'], box)
            box.connect("enter-notify-event", self.on_hover, window['id'])
            box.connect("leave-notify-event", self.on_hover_lost)
            self.pack_start(box, False, False, 0)
        self.show_all()

    def on_hover(self, widget, event, window_id):
        if event.detail != Gdk.NotifyType.INFERIOR:
            send_command(f"ACTIVATE {window_id}")

    def on_hover_lost(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR and self.real_active_window_id:
            send_command(f"ACTIVATE {self.real_active_window_id}")

    def on_click(self, button, window_id):
        self.window_was_clicked_in_popup = True
        self.app_service.real_active_window_id = None
        send_command(f"ACTIVATE {window_id}")
        self.popup_manager.close_active_popup()

    def on_close(self, button, window_id, box):
        send_command(f"CLOSE {window_id}")
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Window): return
        old_width, old_height = toplevel.get_size()
        old_x, old_y = toplevel.get_position()
        self.remove(box)
        self.show_all()
        toplevel.resize(1, 1)

        def do_adjust_position():
            new_width, new_height = toplevel.get_size()
            if (new_width, new_height) == (old_width, old_height): return True
            dx = old_width - new_width
            dy = old_height - new_height
            toplevel.move(old_x + dx // 2, old_y + dy)
            return False
        GLib.timeout_add(30, lambda: not do_adjust_position())

    def on_popup_destroy(self, widget):
        if not self.window_was_clicked_in_popup and self.real_active_window_id:
            send_command(f"ACTIVATE {self.real_active_window_id}")
        self.app_service.real_active_window_id = None
        self.app_service.emit('data-changed')

class RightClickMenuPopup(Box):
    def __init__(self, app_service, popup_manager, app_id, app_info, app_windows):
        super().__init__(orientation='v', spacing=4, name="popup-menu-box")
        self.app_service = app_service
        self.popup_manager = popup_manager
        self.app_id = app_id
        self.app_info = app_info
        self.app_windows = app_windows

        if "bin" in self.app_info:
            new_window_button = Button(label="New Window", on_clicked=self.on_new_window)
            self.add(new_window_button)

            pin_label = "Unpin from taskbar" if self.app_id in self.app_service.pinned_app_ids else "Pin to taskbar"
            toggle_pin_button = Button(label=pin_label, on_clicked=self.on_toggle_pin)
            self.add(toggle_pin_button)

        if self.app_windows:
            close_all_button = Button(label="Close all", on_clicked=self.on_close_all)
            self.add(close_all_button)

        self.show_all()

    def on_new_window(self, button):
        subprocess.Popen(shlex.split(self.app_info["bin"]))
        self.popup_manager.close_active_popup()

    def on_toggle_pin(self, button):
        self.app_service.toggle_pin(self.app_id)
        self.popup_manager.close_active_popup()

    def on_close_all(self, button):
        for w in self.app_windows:
            send_command(f"CLOSE {w['id']}")
        self.popup_manager.close_active_popup()

class StartMenuPopup(Box):
    def __init__(self, app_service, popup_manager):
        super().__init__(orientation='v', spacing=4, name="start-menu")
        self.app_service = app_service
        self.popup_manager = popup_manager
        self.selected_widget = None
        self.set_size_request(500, 550)
        self.set_can_focus(True)

        # --- NEW: Keep track of our background loading task ---
        self._idle_load_source_id = None

        search_box_container = Box(orientation='v', spacing=4, name="search-box")
        self.fake_entry = FakeEntry(placeholder="Search for apps...", on_text_changed=self.on_search_text_changed)
        search_box_container.add(self.fake_entry)

        self.search_results_box = Box(orientation='v', spacing=4, name="search-results-box")
        self.scrolled_window = ScrolledWindow(h_policy="never", v_policy="automatic")
        self.scrolled_window.add(self.search_results_box)

        power_box = Box(orientation='h', spacing=4, name="power-box", h_align="end")
        power_box.add(Button(label="Restart"))
        power_box.add(Button(label="Shutdown"))

        self.pack_start(search_box_container, False, False, 5)
        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, name="search-separator"), False, False, 5)
        self.pack_start(self.scrolled_window, True, True, 5)
        self.pack_end(power_box, False, False, 5)

        self.show_all()
        self._update_search_results("")

    # --- NEW: A generator to load apps in chunks ---
    def _load_remaining_apps_incrementally(self, apps_to_load):
        chunk_size = 20
        for i in range(0, len(apps_to_load), chunk_size):
            chunk = apps_to_load[i:i + chunk_size]
            for appid, info in chunk:
                self._add_app_button(info, appid)
            # Yield control back to the UI thread to keep it responsive
            yield True
        
        # All done
        self._idle_load_source_id = None
        yield False

    # --- NEW: A helper to cancel any existing background loading ---
    def _cancel_idle_load(self):
        if self._idle_load_source_id:
            GLib.source_remove(self._idle_load_source_id)
            self._idle_load_source_id = None

    def _update_search_results(self, search_text):
        # --- NEW: Cancel any pending background loads before redrawing ---
        self._cancel_idle_load()
        
        for child in self.search_results_box.get_children():
            self.search_results_box.remove(child)

        search_text_lower = search_text.strip().lower()

        if not search_text_lower:
            # --- MODIFIED: Load a small batch now, schedule the rest ---
            sorted_apps = sorted(self.app_service.db.items(), key=lambda item: item[1].get("name", item[0]).lower())
            
            initial_load_size = 40
            initial_apps = sorted_apps[:initial_load_size]
            remaining_apps = sorted_apps[initial_load_size:]
            
            current_letter = None
            for appid, info in initial_apps:
                # This logic remains the same, but only runs for the initial batch
                app_name = info.get("name", appid)
                if not app_name: continue
                first_letter = app_name[0].upper()
                if first_letter != current_letter:
                    if current_letter is not None:
                        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, name="letter-separator")
                        self.search_results_box.pack_start(separator, False, False, 0)
                    current_letter = first_letter
                    header_label = Label(label=current_letter, h_align="start", name="letter-header", h_expand=True)
                    self.search_results_box.pack_start(header_label, False, False, 5)
                self._add_app_button(info, appid)
            
            self.selected_widget = None

            # Schedule the rest of the apps to be loaded when idle
            if remaining_apps:
                generator = self._load_remaining_apps_incrementally(remaining_apps)
                self._idle_load_source_id = GLib.idle_add(lambda: next(generator, False))

        else:
            # Search logic remains the same, as it's filtered and should be fast
            scored_matches = []
            for appid, info in self.app_service.db.items():
                name_lower = info.get("name", appid).lower()
                generic_name_lower = info.get("generic_name", "").lower()
                best_score = float('inf')
                if search_text_lower in name_lower:
                    if name_lower == search_text_lower: best_score = min(best_score, 0)
                    elif name_lower.startswith(search_text_lower): best_score = min(best_score, 2)
                    elif any(w.startswith(search_text_lower) for w in name_lower.split()): best_score = min(best_score, 4)
                    else: best_score = min(best_score, 6)
                if generic_name_lower and search_text_lower in generic_name_lower:
                    if generic_name_lower == search_text_lower: best_score = min(best_score, 1)
                    elif generic_name_lower.startswith(search_text_lower): best_score = min(best_score, 3)
                    elif any(w.startswith(search_text_lower) for w in generic_name_lower.split()): best_score = min(best_score, 5)
                    else: best_score = min(best_score, 7)
                if best_score != float('inf'):
                    scored_matches.append((best_score, len(name_lower), name_lower, appid, info))
            scored_matches.sort()
            matches = [(appid, info) for _, _, _, appid, info in scored_matches]

            buttons_in_results = [self._add_app_button(info, appid) for appid, info in matches]
            self.selected_widget = buttons_in_results[0] if buttons_in_results else None
            self._update_selection_visuals()

        self.search_results_box.show_all()

    def handle_key_press(self, widget, event_key):
        key_name = Gdk.keyval_name(event_key.keyval)
        if key_name in ("Up", "Down", "Tab", "ISO_Left_Tab", "Return", "KP_Enter"):
            buttons = [child for child in self.search_results_box.get_children() if isinstance(child, Gtk.Button)]
            if not buttons:
                return Gdk.EVENT_STOP

            if key_name in ("Return", "KP_Enter"):
                if self.selected_widget:
                    self.selected_widget.emit("clicked")
                return Gdk.EVENT_STOP

            try:
                current_idx = buttons.index(self.selected_widget)
            except ValueError:
                current_idx = -1

            num_buttons = len(buttons)
            if key_name == "Down" or key_name == "Tab":
                new_idx = (current_idx + 1) % num_buttons
            else:
                new_idx = (current_idx - 1 + num_buttons) % num_buttons

            self.selected_widget = buttons[new_idx]
            self.selected_widget.grab_focus()
            self._update_selection_visuals()
            return Gdk.EVENT_STOP

        self.fake_entry.handle_key_press(event_key)
        return Gdk.EVENT_STOP

    def on_search_text_changed(self, text):
        self._update_search_results(text)

    def _update_selection_visuals(self):
        for child in self.search_results_box.get_children():
            context = child.get_style_context()
            if child == self.selected_widget:
                context.add_class("selected")
            else:
                context.remove_class("selected")

    def _add_app_button(self, info, appid):
        icon = Image(icon_name=info.get("icon", "dialog-question"), icon_size=16, v_align="center")
        text_vbox = Box(orientation='v')
        name_label = Label(label=info.get("name", appid), h_align="start")
        text_vbox.pack_start(name_label, False, False, 0)
        generic_name = info.get("generic_name")
        if generic_name:
            generic_name_label = Label(label=generic_name, h_align="start")
            generic_name_label.get_style_context().add_class("dim-label")
            text_vbox.pack_start(generic_name_label, False, False, 0)
        button_content = Box(orientation='h', spacing=10)
        button_content.pack_start(icon, False, False, 0)
        button_content.pack_start(text_vbox, True, True, 0)
        launch_command = shlex.split(info["bin"]) if "bin" in info else None
        button = Button(child=button_content, name="search-result-button")
        if launch_command:
            button.connect("clicked", lambda _, cmd=launch_command: (subprocess.Popen(cmd), self.popup_manager.close_active_popup()))
        self.search_results_box.pack_start(button, False, False, 0)
        return button

# ===================================================================
# === WIDGETS =======================================================
# ===================================================================

class ClockWidget(DateTime):
    def __init__(self):
        super().__init__(name="date-time-label", formatters=["%H:%M"])

class StartWidget(Box):
    def __init__(self, app_service, popup_manager):
        super().__init__()
        self.app_service = app_service
        self.popup_manager = popup_manager

        icon = Image(icon_name="fedora-logo-icon", icon_size=24)
        inner = Box(name="task-button-inner", children=[icon])
        start_button = Button(name="task-button", child=inner)

        self.popup_manager.attach(
            start_button,
            self._create_start_menu_popup,
            'left-click',
            command='toggle-menu'
        )
        self.add(start_button)

    def _create_start_menu_popup(self):
        return StartMenuPopup(self.app_service, self.popup_manager)

class TaskListWidget(Box):
    def __init__(self, app_service, popup_manager):
        super().__init__(name="center-container", v_align="center", h_align="center", spacing=4, orientation="h")
        self.app_service = app_service
        self.popup_manager = popup_manager
        self.app_service.connect('data-changed', self._redraw_widget)
        self._redraw_widget()

    def _on_task_button_clicked(self, button, app_id):
        app_windows = [w for w in self.app_service.windows if w.get("appid") == app_id]

        if not app_windows:
            if app_id in self.app_service.db and "bin" in self.app_service.db[app_id]:
                subprocess.Popen(shlex.split(self.app_service.db[app_id]["bin"]))
            return

        window_to_toggle = app_windows[0]
        is_active = window_to_toggle.get("state", "").startswith("Active")

        if is_active:
            send_command(f"MINIMIZE {window_to_toggle['id']}")
        else:
            send_command(f"ACTIVATE {window_to_toggle['id']}")

    def _create_left_click_menu_popup(self, bar, app_windows):
        active_window = next((w for w in self.app_service.windows if w.get("state", "").startswith("Active")), None)
        self.app_service.real_active_window_id = active_window['id'] if active_window else None
        return LeftClickMenuPopup(bar, self.app_service, self.popup_manager, app_windows, self.app_service.real_active_window_id)

    def _create_right_click_menu_popup(self, app_id, app_info, app_windows):
        return RightClickMenuPopup(self.app_service, self.popup_manager, app_id, app_info, app_windows)

    def _redraw_widget(self, *args):
        for child in self.get_children():
            self.remove(child)

        grouped_windows = {}
        for window in self.app_service.windows:
            app_id = window.get("appid")
            if not app_id: continue
            if app_id not in grouped_windows: grouped_windows[app_id] = []
            grouped_windows[app_id].append(window)

        open_unpinned_apps = sorted([app_id for app_id in grouped_windows.keys() if app_id not in self.app_service.pinned_app_ids])
        all_app_ids = self.app_service.pinned_app_ids + open_unpinned_apps

        DEFAULT_ICON_NAME = "dialog-question"

        for app_id in all_app_ids:
            # Use a default app_info if missing instead of skipping
            if app_id not in self.app_service.db:
                print(f"Missing app {app_id}, using default info")
                app_info = {"icon": DEFAULT_ICON_NAME, "name": f"App {app_id}"}
            else:
                app_info = self.app_service.db[app_id]

            app_windows = grouped_windows.get(app_id, [])
            is_open = len(app_windows) > 0
            has_multiple_windows = len(app_windows) > 1

            # Always use app_info["icon"], falling back to default if empty
            icon_name = app_info.get("icon") or DEFAULT_ICON_NAME
            icon = Image(icon_name=icon_name, icon_size=24)

            inner = Box(name="task-button-inner", children=[icon])
            icon_button = Button(name="task-button", child=inner)
            style_context = icon_button.get_style_context()

            if is_open:
                style_context.add_class("open")
            if has_multiple_windows:
                style_context.add_class("multiple")

            if self.app_service.real_active_window_id:
                if any(w.get("id", -1) == self.app_service.real_active_window_id for w in app_windows):
                    style_context.add_class("active")
            elif any(w.get("state", "").startswith("Active") for w in app_windows):
                style_context.add_class("active")

            bar = self.get_ancestor(Bar)
            if has_multiple_windows:
                self.popup_manager.attach(
                    icon_button,
                    lambda b=bar, wins=app_windows: self._create_left_click_menu_popup(b, wins),
                    'left-click'
                )
            else:
                icon_button.connect("clicked", self._on_task_button_clicked, app_id)

            # Use simple right-click menu instead of full one
            self.popup_manager.attach(
                icon_button,
                lambda app_id=app_id, info=app_info, win_list=app_windows:
                    self._create_right_click_menu_popup(app_id, info, win_list),
                'right-click'
            )

            self.add(icon_button)

        self.show_all()

# ===================================================================
# === BAR (MAIN WINDOW) =============================================
# ===================================================================

class Bar(Window):
    def __init__(self, app_service, network_service):
        super().__init__(name="bar", layer="top", anchor="left bottom right", margin="0px", v_align="end", exclusivity="auto")
        self.app_service = app_service
        self.network_service = network_service
        self.popup_manager = PopupManager(self)
        self.connect("destroy", self._on_destroy)
        self.set_keyboard_mode("on_demand")
        self.connect("key-press-event", self.popup_manager._on_global_key_press)

        start_widget = StartWidget(app_service, self.popup_manager)
        tasklist_widget = TaskListWidget(app_service, self.popup_manager)
        network_widget = NetworkWidget(network_service, self.popup_manager)
        clock_widget = ClockWidget()
        minimize_widget = Button(name="minimize-button", label="", on_clicked=lambda _: send_command("MINIMIZEALL"))

        start_container = Box(name="start-container", children=[])
        center_container = Box(name="center-container", spacing=4, children=[start_widget, tasklist_widget])
        end_container = Box(name="end-container", h_align="end", spacing=0, children=[network_widget, clock_widget, minimize_widget])

        self.add(CenterBox(start_children=start_container, center_children=center_container, end_children=end_container))
        self.show_all()

    def _on_destroy(self, widget):
        self.popup_manager.cleanup()


# ===================================================================
# === MAIN EXECUTION ================================================
# ===================================================================

if __name__ == "__main__":
    app_service = AppService()
    network_service = NetworkService()
    bar = Bar(app_service, network_service)
    app = Application("taskbar", bar)
    app.set_stylesheet_from_file(get_relative_path("style.css"))

    try:
        toplevel_monitor_process = subprocess.Popen(["./bin/toplevel_monitor"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

        def on_monitor_error(channel, cond):
            if cond & (GLib.IO_HUP | GLib.IO_ERR): return False
            status, line, _, _ = channel.read_line()
            if status == GLib.IOStatus.NORMAL and line: print(f"[MONITOR-ERROR] {line.strip()}", file=sys.stderr)
            return True
        
        def on_monitor_output(channel, condition):
            if condition & (GLib.IO_HUP | GLib.IO_ERR): return False
            while True:
                status, line, _, _ = channel.read_line()
                if status != GLib.IOStatus.NORMAL: break
                if not line: continue
                bar.app_service.update_from_daemon_line(line)
            return True

        stdout_channel = GLib.IOChannel(toplevel_monitor_process.stdout.fileno())
        stdout_channel.set_flags(stdout_channel.get_flags() | GLib.IO_FLAG_NONBLOCK)
        GLib.io_add_watch(stdout_channel, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_monitor_output)
        stderr_channel = GLib.IOChannel(toplevel_monitor_process.stderr.fileno())
        stderr_channel.set_flags(stderr_channel.get_flags() | GLib.IO_FLAG_NONBLOCK)
        GLib.io_add_watch(stderr_channel, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_monitor_error)
    except FileNotFoundError:
        print("Error: 'toplevel_monitor' executable not found.", file=sys.stderr)
        sys.exit(1)

    send_command("QUERY")

    signal.signal(signal.SIGINT, lambda s, f: app.quit())
    app.run()

    if toplevel_monitor_process:
        toplevel_monitor_process.terminate()
        try:
            toplevel_monitor_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            toplevel_monitor_process.kill()