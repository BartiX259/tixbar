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


# ===================================================================
# === FAKE ENTRY (UTILITY WIDGET) ===================================
# ===================================================================

class FakeEntry(Gtk.Overlay):
    def __init__(self, placeholder="", on_text_changed=None):
        super().__init__(name="fake-entry")
        self.text_buffer = ""
        self.placeholder = placeholder
        self.on_text_changed = on_text_changed

        self._internal_box = Box(orientation="h")
        self.add(self._internal_box)
        self.label = Label(label=self.placeholder)
        self.label.get_style_context().add_class("placeholder")
        self.cursor = Label(label="|")
        self.cursor.get_style_context().add_class("cursor")
        self._internal_box.pack_start(self.cursor, False, False, 0)
        self._internal_box.pack_start(self.label, False, False, 0)
        self._setup_blinker()
        self.show_all()

    def _setup_blinker(self):
        def _toggle_cursor():
            if not self.get_window(): return GLib.SOURCE_REMOVE
            self.cursor.set_opacity(0.0 if self.cursor.get_opacity() == 1.0 else 1.0)
            return GLib.SOURCE_CONTINUE
        GLib.timeout_add(500, _toggle_cursor)

    def _update_label(self):
        if self.text_buffer == "":
            self.label.set_label(self.placeholder)
            self.label.get_style_context().remove_class("regular")
            self.label.get_style_context().add_class("placeholder")
            self._internal_box.reorder_child(self.cursor, 0)
        else:
            self.label.set_text(GLib.markup_escape_text(self.text_buffer))
            self.label.get_style_context().remove_class("placeholder")
            self.label.get_style_context().add_class("regular")
            self._internal_box.reorder_child(self.cursor, 1)

        if self.on_text_changed:
            self.on_text_changed(self.text_buffer)

    def handle_key_press(self, event_key):
        key_name = Gdk.keyval_name(event_key.keyval)
        text_changed = False

        if key_name == "BackSpace":
            if self.text_buffer:
                self.text_buffer = self.text_buffer[:-1]
                text_changed = True
        elif key_name == "Escape":
            toplevel = self.get_ancestor(Gtk.Window)
            if toplevel: toplevel.destroy()
            return

        char_code = Gdk.keyval_to_unicode(event_key.keyval)
        if char_code != 0 and chr(char_code).isprintable():
            self.text_buffer += chr(char_code)
            text_changed = True

        if text_changed:
            self._update_label()
        self.cursor.set_opacity(1.0)