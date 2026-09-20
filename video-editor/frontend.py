import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .backend import ExportCancelled, MediaInfo, Operation, OperationKind, Pipeline, dependencies_available, operation_label, probe


APP_ID = "dev.mudkip.VideoEditor"


class VideoEditorApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
        window.present()

    def do_open(self, files, _count, _hint):
        self.do_activate()
        path = files[0].get_path() if files else None
        if path:
            self.props.active_window.load_video(Path(path))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Video Editor")
        self.set_default_size(1120, 720)
        self.set_size_request(760, 560)
        self.source: Path | None = None
        self.info: MediaInfo | None = None
        self.operations: list[Operation] = []
        self.export_cancelled = False

        self.toast_overlay = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(self._build_editor(), "editor")
        self.toast_overlay.set_child(self.stack)
        self.set_content(self.toast_overlay)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        self._install_css()

        if not dependencies_available():
            GLib.idle_add(self._toast, "Install FFmpeg to inspect and export videos")

    def _build_welcome(self):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        clamp = Adw.Clamp(maximum_size=560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_start(32)
        box.set_margin_end(32)
        box.set_margin_top(32)
        box.set_margin_bottom(64)
        icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
        icon.set_pixel_size(96)
        icon.add_css_class("welcome-icon")
        title = Gtk.Label(label="Make a video recipe")
        title.add_css_class("title-1")
        description = Gtk.Label(label="Drop a video here, or choose one to build a simple sequence of edits.")
        description.set_wrap(True)
        description.set_justify(Gtk.Justification.CENTER)
        description.add_css_class("dim-label")
        open_button = Gtk.Button(label="Open Video…")
        open_button.add_css_class("suggested-action")
        open_button.add_css_class("pill")
        open_button.set_halign(Gtk.Align.CENTER)
        open_button.connect("clicked", lambda *_: self._choose_video())
        for child in (icon, title, description, open_button):
            box.append(child)
        clamp.set_child(box)
        toolbar.set_content(clamp)
        return toolbar

    def _build_editor(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        open_button = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open another video")
        open_button.connect("clicked", lambda *_: self._choose_video())
        header.pack_start(open_button)
        self.export_button = Gtk.Button(label="Export…")
        self.export_button.add_css_class("suggested-action")
        self.export_button.connect("clicked", lambda *_: self._choose_export())
        header.pack_end(self.export_button)
        toolbar.add_top_bar(header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_position(380)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_start_child(self._build_source_panel())
        paned.set_end_child(self._build_recipe_panel())
        toolbar.set_content(paned)
        return toolbar

    def _build_source_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("source-panel")
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        heading = Gtk.Label(label="Source")
        heading.add_css_class("heading")
        heading.set_halign(Gtk.Align.START)
        self.video = Gtk.Video(hexpand=True, vexpand=True, autoplay=False)
        self.video.set_size_request(280, 220)
        self.video.add_css_class("video-preview")
        self.source_name = Gtk.Label(xalign=0, ellipsize=3)
        self.source_name.add_css_class("title-4")
        self.source_details = Gtk.Label(xalign=0)
        self.source_details.add_css_class("dim-label")
        box.append(heading)
        box.append(self.video)
        box.append(self.source_name)
        box.append(self.source_details)
        return box

    def _build_recipe_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(22)
        box.set_margin_end(22)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        top = Gtk.Box(spacing=12)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label="Recipe", xalign=0)
        title.add_css_class("title-2")
        subtitle = Gtk.Label(label="Steps run from top to bottom", xalign=0)
        subtitle.add_css_class("dim-label")
        title_box.append(title)
        title_box.append(subtitle)
        title_box.set_hexpand(True)
        menu = Gtk.MenuButton(label="Add Step")
        menu.add_css_class("suggested-action")
        menu.set_popover(self._operation_popover(menu))
        top.append(title_box)
        top.append(menu)
        self.recipe_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.recipe_list.add_css_class("boxed-list")
        self.recipe_list.set_placeholder(self._empty_recipe())
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.recipe_list)
        box.append(top)
        box.append(scroll)
        return box

    def _empty_recipe(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        icon = Gtk.Image.new_from_icon_name("view-list-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        label = Gtk.Label(label="No steps yet")
        label.add_css_class("title-4")
        hint = Gtk.Label(label="Add a step, or export the original as MP4.")
        hint.add_css_class("dim-label")
        box.append(icon)
        box.append(label)
        box.append(hint)
        return box

    def _operation_popover(self, menu):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        icons = {
            OperationKind.TRIM: "edit-cut-symbolic",
            OperationKind.CUT: "edit-delete-symbolic",
            OperationKind.COMPRESS: "package-x-generic-symbolic",
            OperationKind.AUDIO: "audio-x-generic-symbolic",
            OperationKind.MUTE: "audio-volume-muted-symbolic",
            OperationKind.RESIZE: "view-fullscreen-symbolic",
        }
        for kind in OperationKind:
            button = Gtk.Button()
            button.add_css_class("flat")
            content = Adw.ButtonContent(label=operation_label(kind), icon_name=icons[kind])
            button.set_child(content)
            button.connect("clicked", self._add_operation, kind, menu)
            box.append(button)
        popover.set_child(box)
        return popover

    def _add_operation(self, _button, kind, menu):
        self.operations.append(Operation.with_defaults(kind))
        menu.popdown()
        self._rebuild_recipe()

    def _rebuild_recipe(self):
        while child := self.recipe_list.get_first_child():
            self.recipe_list.remove(child)
        for index, operation in enumerate(self.operations):
            self.recipe_list.append(self._operation_row(index, operation))

    def _operation_row(self, index, operation):
        row = Gtk.ListBoxRow(activatable=False)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_start(14)
        outer.set_margin_end(8)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        heading = Gtk.Box(spacing=8)
        number = Gtk.Label(label=str(index + 1))
        number.add_css_class("step-number")
        title = Gtk.Label(label=operation_label(operation.kind), xalign=0, hexpand=True)
        title.add_css_class("heading")
        up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Move up")
        down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Move down")
        remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove step")
        for button in (up, down, remove):
            button.add_css_class("flat")
        up.set_sensitive(index > 0)
        down.set_sensitive(index < len(self.operations) - 1)
        up.connect("clicked", lambda *_: self._move(operation, -1))
        down.connect("clicked", lambda *_: self._move(operation, 1))
        remove.connect("clicked", lambda *_: self._remove(operation))
        for child in (number, title, up, down, remove):
            heading.append(child)
        outer.append(heading)
        controls = self._operation_controls(operation)
        if controls:
            outer.append(controls)
        row.set_child(outer)
        return row

    def _operation_controls(self, operation):
        if operation.kind == OperationKind.MUTE:
            return None
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        if operation.kind in (OperationKind.TRIM, OperationKind.CUT):
            labels = ("Start (seconds)", "End trim (seconds)") if operation.kind == OperationKind.TRIM else ("From (seconds)", "To (seconds)")
            for column, (key, label) in enumerate(zip(("start", "end"), labels)):
                field = self._spin(float(operation.values[key]), 0, 86400, 0.1)
                field.connect("value-changed", lambda widget, name=key: operation.values.__setitem__(name, widget.get_value()))
                grid.attach(Gtk.Label(label=label, xalign=0), column, 0, 1, 1)
                grid.attach(field, column, 1, 1, 1)
        elif operation.kind == OperationKind.COMPRESS:
            field = self._spin(float(operation.values["size_mib"]), 0.1, 100000, 1)
            field.connect("value-changed", lambda widget: operation.values.__setitem__("size_mib", widget.get_value()))
            grid.attach(Gtk.Label(label="Maximum size (MiB)", xalign=0), 0, 0, 1, 1)
            grid.attach(field, 0, 1, 1, 1)
        elif operation.kind == OperationKind.RESIZE:
            field = self._spin(float(operation.values["height"]), 144, 4320, 2)
            field.set_digits(0)
            field.connect("value-changed", lambda widget: operation.values.__setitem__("height", widget.get_value_as_int()))
            grid.attach(Gtk.Label(label="Height (pixels)", xalign=0), 0, 0, 1, 1)
            grid.attach(field, 0, 1, 1, 1)
        elif operation.kind == OperationKind.AUDIO:
            choose = Gtk.Button(label=Path(str(operation.values["path"])).name or "Choose Audio…")
            choose.connect("clicked", lambda *_: self._choose_audio(operation))
            modes = Gtk.StringList.new(["Replace existing audio", "Mix with existing audio"])
            dropdown = Gtk.DropDown(model=modes, selected=1 if operation.values["mode"] == "mix" else 0)
            dropdown.connect("notify::selected", lambda widget, _param: operation.values.__setitem__("mode", "mix" if widget.get_selected() == 1 else "replace"))
            grid.attach(Gtk.Label(label="Audio file", xalign=0), 0, 0, 1, 1)
            grid.attach(Gtk.Label(label="Mode", xalign=0), 1, 0, 1, 1)
            grid.attach(choose, 0, 1, 1, 1)
            grid.attach(dropdown, 1, 1, 1, 1)
        return grid

    @staticmethod
    def _spin(value, lower, upper, step):
        spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=value, lower=lower, upper=upper, step_increment=step, page_increment=step * 10), digits=1)
        spin.set_hexpand(True)
        return spin

    def _move(self, operation, offset):
        index = self.operations.index(operation)
        target = index + offset
        if 0 <= target < len(self.operations):
            self.operations[index], self.operations[target] = self.operations[target], self.operations[index]
            self._rebuild_recipe()

    def _remove(self, operation):
        self.operations.remove(operation)
        self._rebuild_recipe()

    def _choose_video(self):
        dialog = self._file_chooser("Open Video", Gtk.FileChooserAction.OPEN, "_Open")
        file_filter = Gtk.FileFilter(name="Video files")
        file_filter.add_mime_type("video/*")
        dialog.add_filter(file_filter)
        dialog.connect("response", self._video_chosen)
        dialog.show()

    def _video_chosen(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file and file.get_path():
                self.load_video(Path(file.get_path()))
        dialog.destroy()

    def _choose_audio(self, operation):
        dialog = self._file_chooser("Choose Audio", Gtk.FileChooserAction.OPEN, "_Choose")
        file_filter = Gtk.FileFilter(name="Audio files")
        file_filter.add_mime_type("audio/*")
        dialog.add_filter(file_filter)
        dialog.connect("response", self._audio_chosen, operation)
        dialog.show()

    def _audio_chosen(self, dialog, response, operation):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file and file.get_path():
                operation.values["path"] = file.get_path()
                self._rebuild_recipe()
        dialog.destroy()

    def _choose_export(self):
        if not self.source:
            return
        dialog = self._file_chooser("Export Video", Gtk.FileChooserAction.SAVE, "_Export")
        dialog.set_current_name(f"{self.source.stem}-edited.mp4")
        file_filter = Gtk.FileFilter(name="MPEG-4 video")
        file_filter.add_pattern("*.mp4")
        dialog.add_filter(file_filter)
        dialog.connect("response", self._export_chosen)
        dialog.show()

    def _export_chosen(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file and file.get_path():
                destination = Path(file.get_path())
                if destination.suffix.lower() != ".mp4":
                    destination = destination.with_suffix(".mp4")
                if self.source and destination.absolute() == self.source.absolute():
                    self._toast("Choose a different file so the source stays unchanged")
                else:
                    self._start_export(destination)
        dialog.destroy()

    def _file_chooser(self, title, action, accept):
        return Gtk.FileChooserNative(title=title, transient_for=self, modal=True, action=action, accept_label=accept, cancel_label="_Cancel")

    def load_video(self, path: Path):
        if not path.is_file():
            self._toast("That file could not be opened")
            return
        self.source = path
        self.info = None
        self.source_name.set_label(path.name)
        self.source_details.set_label("Reading video…")
        self.video.set_file(Gio.File.new_for_path(str(path)))
        self.stack.set_visible_child_name("editor")
        threading.Thread(target=self._probe_source, args=(path,), daemon=True).start()

    def _probe_source(self, path):
        try:
            info = probe(path)
            GLib.idle_add(self._source_probed, path, info)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            GLib.idle_add(self._probe_failed, path, str(error))

    def _source_probed(self, path, info):
        if path != self.source:
            return GLib.SOURCE_REMOVE
        self.info = info
        audio = "with audio" if info.has_audio else "no audio"
        self.source_details.set_label(f"{info.duration_label}  ·  {info.dimensions}  ·  {info.size_label}  ·  {audio}")
        return GLib.SOURCE_REMOVE

    def _probe_failed(self, path, _detail):
        if path == self.source:
            self.source_details.set_label("Could not read video details")
            self._toast("FFprobe could not read this file")
        return GLib.SOURCE_REMOVE

    def _on_drop(self, _target, value, _x, _y):
        files = value.get_files()
        if files and files[0].get_path():
            self.load_video(Path(files[0].get_path()))
            return True
        return False

    def _start_export(self, destination):
        assert self.source is not None
        self.export_cancelled = False
        self.export_dialog = Adw.Dialog(title="Exporting Video")
        self.export_dialog.set_can_close(False)
        self.export_dialog.set_content_width(440)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        self.export_status = Gtk.Label(label="Preparing export", xalign=0)
        self.export_progress = Gtk.ProgressBar(show_text=False)
        cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
        cancel.connect("clicked", lambda *_: setattr(self, "export_cancelled", True))
        content.append(self.export_status)
        content.append(self.export_progress)
        content.append(cancel)
        self.export_dialog.set_child(content)
        self.export_dialog.present(self)
        self.export_button.set_sensitive(False)
        source = self.source
        operations = [Operation(op.kind, op.values.copy(), op.id) for op in self.operations]
        threading.Thread(target=self._export_worker, args=(source, destination, operations), daemon=True).start()

    def _export_worker(self, source, destination, operations):
        pipeline = Pipeline(
            lambda fraction, label: GLib.idle_add(self._export_progressed, fraction, label),
            lambda: self.export_cancelled,
        )
        try:
            pipeline.export(source, destination, operations)
            GLib.idle_add(self._export_finished, destination)
        except ExportCancelled:
            GLib.idle_add(self._export_stopped, "Export canceled")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            GLib.idle_add(self._export_stopped, str(error))

    def _export_progressed(self, fraction, label):
        self.export_progress.set_fraction(fraction)
        self.export_status.set_label(label)
        return GLib.SOURCE_REMOVE

    def _export_finished(self, destination):
        self.export_dialog.force_close()
        self.export_button.set_sensitive(True)
        self._toast(f"Exported {destination.name}")
        return GLib.SOURCE_REMOVE

    def _export_stopped(self, message):
        self.export_dialog.force_close()
        self.export_button.set_sensitive(True)
        self._toast(message)
        return GLib.SOURCE_REMOVE

    def _toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=5))
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _install_css():
        provider = Gtk.CssProvider()
        provider.load_from_string("""
            .welcome-icon { color: @accent_color; }
            .video-preview { background: #111; border-radius: 12px; }
            .step-number {
                min-width: 26px; min-height: 26px; border-radius: 99px;
                background: alpha(@accent_color, 0.18); color: @accent_color; font-weight: bold;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main() -> int:
    return VideoEditorApplication().run(sys.argv)
