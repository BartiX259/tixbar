import gi
import os
import sys

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

class Popup(Gtk.Window):
    """A simple, undecorated popup window for holding content."""
    def __init__(self, parent, content_widget):
        super().__init__(type=Gtk.WindowType.POPUP, transient_for=parent)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_keep_above(True)
        self.add(content_widget)
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)

class PopupManager:
    """
    Manages all popups for a parent window using a single, robust named pipe (FIFO).
    It can use a pre-existing FIFO or create its own.
    """
    def __init__(self, parent_window):
        self.parent_window = parent_window
        self.active_popup = None
        self.active_parent = None
        self.is_mouse_inside_popup = False
        
        self.fifo_path = "/tmp/taskbar-commands.fifo" # Changed from taskbar.fifo for clarity
        self.fifo_fd = None
        
        # --- MODIFICATION: Add a flag to track ownership of the FIFO ---
        self.created_fifo = False
        
        self.command_map = {}
        self._setup_fifo()

    def _setup_fifo(self):
        """
        Connects to the FIFO. If it doesn't exist, it creates it and takes ownership.
        """
        # --- MODIFICATION: Only create the FIFO if it doesn't already exist ---
        if not os.path.exists(self.fifo_path):
            print(f"FIFO not found at {self.fifo_path}. Creating it.", file=sys.stderr)
            os.mkfifo(self.fifo_path)
            self.created_fifo = True # Mark that we are the owner
        
        try:
            self.fifo_fd = os.open(self.fifo_path, os.O_RDWR | os.O_NONBLOCK)
            GLib.io_add_watch(self.fifo_fd, GLib.IO_IN | GLib.IO_HUP, self._on_fifo_ready)
            print(f"Unified FIFO manager listening on {self.fifo_path}.", file=sys.stderr)
        except Exception as e:
            print(f"Failed to open unified FIFO: {e}", file=sys.stderr)

    def _on_fifo_ready(self, fd, condition):
        # ... (This method is unchanged from the previous version) ...
        if condition & GLib.IO_HUP:
            return False

        line_bytes = os.read(fd, 1024)
        if not line_bytes:
            return True

        line = line_bytes.decode('utf-8').strip()

        if line.startswith("CMD:"):
            command = line.split(":", 1)[1]
            if command in self.command_map:
                widget, content_factory = self.command_map[command]
                if self.active_parent == widget:
                    self.close_active_popup()
                else:
                    self.show_popup(widget, content_factory())
        else:
            if not self.is_mouse_inside_popup:
                if self.active_parent and self.active_parent.get_state_flags() & Gtk.StateFlags.PRELIGHT:
                    return True
                self.close_active_popup()

        return True
    
    def _on_global_key_press(self, widget, event_key):
        """Delegates key presses to the active popup handler if one exists."""
        if self.active_popup and hasattr(self.active_popup, 'handle_key_press'):
            # The popup's key handler returns True if it consumed the event
            if self.active_popup.handle_key_press(widget, event_key):
                return Gdk.EVENT_STOP
        return Gdk.EVENT_PROPAGATE

    def attach(self, widget, content_factory, event_type='right-click', command=None):
        # ... (This method is unchanged) ...
        event_name = "button-release-event"
        button = 3 if event_type == 'right-click' else 1
        widget.connect(event_name, self._on_widget_click, content_factory, button)

        if command:
            self.command_map[command] = (widget, content_factory)

    def _on_widget_click(self, widget, event, content_factory, button):
        # ... (This method is unchanged) ...
        if event.button != button:
            self.close_active_popup()
            return False
        
        if self.active_parent == widget:
            self.close_active_popup()
        else:
            self.show_popup(widget, content_factory())
        return True

    def show_popup(self, clicked_widget, content_widget):
        # ... (This method is unchanged) ...
        self.close_active_popup()
        parent_gdk_window = self.parent_window.get_window()
        if not parent_gdk_window: return
        widget_alloc = clicked_widget.get_allocation()
        taskbar_x, taskbar_y = self.parent_window.get_position()
        widget_screen_x = taskbar_x + widget_alloc.x
        widget_screen_y = taskbar_y + widget_alloc.y
        content_widget.show_all()
        req_width, req_height = content_widget.get_size_request()
        popup_height = req_height if req_height != -1 else content_widget.get_preferred_height()[1]
        popup_width = req_width if req_width != -1 else content_widget.get_preferred_width()[1]
        widget_width = clicked_widget.get_allocated_width()
        ideal_x = widget_screen_x - (popup_width / 2) + (widget_width / 2)
        screen_width = parent_gdk_window.get_screen().get_width()
        popup_x = max(0, min(screen_width - popup_width, ideal_x))
        popup_y = widget_screen_y - popup_height
        popup = Popup(self.parent_window, content_widget)
        popup.resize(int(popup_width), int(popup_height))
        popup.connect("enter-notify-event", self._on_popup_mouse_enter)
        popup.connect("leave-notify-event", self._on_popup_mouse_leave)
        popup.connect("destroy", self._on_popup_destroyed)
        if hasattr(content_widget, 'handle_key_press'):
            popup.handle_key_press = content_widget.handle_key_press
        self.active_popup = popup
        self.active_parent = clicked_widget
        popup.move(int(popup_x), int(popup_y))
        popup.show()
        popup.grab_focus()
        self.parent_window.set_keyboard_mode("exclusive")

    def _on_popup_mouse_enter(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR: self.is_mouse_inside_popup = True

    def _on_popup_mouse_leave(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR: self.is_mouse_inside_popup = False
    
    def close_active_popup(self):
        self.parent_window.set_keyboard_mode("on_demand")
        if self.active_popup:
            self.active_popup.destroy()

    def _on_popup_destroyed(self, widget):
        self.is_mouse_inside_popup = False
        self.active_popup = None
        self.active_parent = None

    def cleanup(self):
        """Cleans up the FIFO file and descriptor on application exit."""
        try:
            if self.fifo_fd:
                os.close(self.fifo_fd)
            # --- MODIFICATION: Only remove the FIFO if we created it ---
            if self.created_fifo and os.path.exists(self.fifo_path):
                print(f"Removing FIFO {self.fifo_path} that this process created.", file=sys.stderr)
                os.remove(self.fifo_path)
        except Exception as e:
            print(f"Error during FIFO cleanup: {e}", file=sys.stderr)