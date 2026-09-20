import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk

from .backend import FORMATS, MODELS, Model, Settings, UpscaleCancelled, Upscaler, data_dir


APP_ID = "dev.mudkip.ImageUpscaler"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


class CompareView(Gtk.Widget):
    def __init__(self):
        super().__init__()
        self.before: Gdk.Texture | None = None
        self.after: Gdk.Texture | None = None
        self.fraction = 0.5
        self.start = 0.0
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_began)
        drag.connect("drag-update", self._dragged)
        self.add_controller(drag)

    def set_images(self, before: Gdk.Texture | None, after: Gdk.Texture | None = None):
        self.before = before
        self.after = after
        self.fraction = 0.5
        self.queue_draw()

    def _drag_began(self, _gesture, x, _y):
        self.start = x
        self._move_to(x)

    def _dragged(self, _gesture, offset_x, _offset_y):
        self._move_to(self.start + offset_x)

    def _move_to(self, x):
        width = self.get_width()
        if width:
            self.fraction = min(max(x / width, 0.0), 1.0)
            self.queue_draw()

    def do_snapshot(self, snapshot):
        texture = self.after or self.before
        if texture is None:
            return
        width, height = self.get_width(), self.get_height()
        if not width or not height:
            return
        scale = min(width / texture.get_width(), height / texture.get_height())
        box = Graphene.Rect()
        box.init((width - texture.get_width() * scale) / 2, (height - texture.get_height() * scale) / 2, texture.get_width() * scale, texture.get_height() * scale)
        snapshot.append_texture(texture, box)
        if self.after is None or self.before is None:
            return
        divider = box.origin.x + box.size.width * self.fraction
        clip = Graphene.Rect()
        clip.init(box.origin.x, box.origin.y, box.size.width * self.fraction, box.size.height)
        snapshot.push_clip(clip)
        snapshot.append_texture(self.before, box)
        snapshot.pop()
        line = Graphene.Rect()
        line.init(divider - 1, box.origin.y, 2, box.size.height)
        color = Gdk.RGBA()
        color.parse("#ffffff")
        snapshot.append_color(color, line)


class ImageUpscalerApplication(Adw.Application):
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
            self.props.active_window.load_image(Path(path))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Image Upscaler")
        self.set_default_size(1100, 720)
        self.set_size_request(720, 520)
        self.upscaler = Upscaler(data_dir())
        self.settings = Settings.load()
        self.source: Path | None = None
        self.output: Path | None = None
        self.result: Gdk.Texture | None = None
        self.models: list[Model] = []
        self.cancelled = False
        self.task_dialog: Adw.Dialog | None = None
        self.settings_dialog: Adw.PreferencesDialog | None = None
        self.engine_button: Gtk.Button | None = None
        self.model_rows: list[tuple[Model, Gtk.Button]] = []

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
        self._refresh_models()

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
        icon = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        icon.set_pixel_size(96)
        icon.add_css_class("welcome-icon")
        title = Gtk.Label(label="Upscale an image")
        title.add_css_class("title-1")
        description = Gtk.Label(label="Drop an image here, or choose one, then upscale it with an ESRGAN model.")
        description.set_wrap(True)
        description.set_justify(Gtk.Justification.CENTER)
        description.add_css_class("dim-label")
        open_button = Gtk.Button(label="Open Image…")
        open_button.add_css_class("suggested-action")
        open_button.add_css_class("pill")
        open_button.set_halign(Gtk.Align.CENTER)
        open_button.connect("clicked", lambda *_: self._choose_image())
        for child in (icon, title, description, open_button):
            box.append(child)
        clamp.set_child(box)
        toolbar.set_content(clamp)
        return toolbar

    def _build_editor(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        open_button = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open another image")
        open_button.connect("clicked", lambda *_: self._choose_image())
        header.pack_start(open_button)
        self.model_dropdown = Gtk.DropDown(tooltip_text="Upscaling model")
        self.model_dropdown.connect("notify::selected", self._model_changed)
        header.set_title_widget(self.model_dropdown)
        self.upscale_button = Gtk.Button(label="Upscale")
        self.upscale_button.add_css_class("suggested-action")
        self.upscale_button.connect("clicked", lambda *_: self._start_upscale())
        self.copy_button = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy image")
        self.export_button = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Export image")
        settings_button = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        for button in (self.copy_button, self.export_button, settings_button):
            button.add_css_class("flat")
        self.copy_button.connect("clicked", lambda *_: self._copy_image())
        self.export_button.connect("clicked", lambda *_: self._choose_export())
        settings_button.connect("clicked", lambda *_: self._open_settings())
        for child in (settings_button, self.copy_button, self.export_button, self.upscale_button):
            header.pack_end(child)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.compare = CompareView()
        self.compare.add_css_class("compare-view")
        self.status = Gtk.Label(label="Drop an image to begin", xalign=0)
        self.status.add_css_class("dim-label")
        box.append(self.compare)
        box.append(self.status)
        toolbar.set_content(box)
        self._update_actions()
        return toolbar

    def _refresh_models(self):
        self.models = self.upscaler.installed_models() or [MODELS[0]]
        self.model_dropdown.set_model(Gtk.StringList.new([f"{model.label}  ·  {model.scale}×" for model in self.models]))
        index = next((position for position, model in enumerate(self.models) if model.stem == self.settings.model), 0)
        self.model_dropdown.set_selected(index)

    def _model_changed(self, _dropdown, _param):
        model = self._selected_model()
        if model is not None:
            self.settings.model = model.stem
            self.settings.save()

    def _selected_model(self) -> Model | None:
        index = self.model_dropdown.get_selected()
        return self.models[index] if 0 <= index < len(self.models) else None

    def _choose_image(self):
        dialog = Gtk.FileChooserNative(title="Open Image", transient_for=self, modal=True, action=Gtk.FileChooserAction.OPEN, accept_label="_Open", cancel_label="_Cancel")
        file_filter = Gtk.FileFilter(name="Images")
        file_filter.add_mime_type("image/*")
        dialog.add_filter(file_filter)
        dialog.connect("response", self._image_chosen)
        dialog.show()

    def _image_chosen(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file and file.get_path():
                self.load_image(Path(file.get_path()))
        dialog.destroy()

    def load_image(self, path: Path):
        if not path.is_file():
            self._toast("That file could not be opened")
            return
        texture = self._texture(path)
        if texture is None:
            self._toast("That image could not be read")
            return
        self.source = path
        self.output = None
        self.result = None
        self.compare.set_images(texture)
        self.status.set_label(f"{path.name}  ·  {texture.get_width()} × {texture.get_height()}")
        self.stack.set_visible_child_name("editor")
        self._update_actions()

    def _start_upscale(self):
        model = self._selected_model()
        if model is None or self.source is None:
            self._toast("Open an image first")
            return
        descriptor, name = tempfile.mkstemp(prefix="image-upscaler-", suffix=f".{self.settings.format}")
        os.close(descriptor)
        previous, self.output = self.output, Path(name)
        if previous is not None:
            previous.unlink(missing_ok=True)
        self.result = None
        self._update_actions()
        self._run_task("Upscaling Image", lambda: self._upscale_worker(model), cancellable=True)

    def _upscale_worker(self, model: Model):
        try:
            if not self.upscaler.engine_installed():
                self.upscaler.install_engine(self._report)
            assert self.source is not None and self.output is not None
            self.upscaler.upscale(model, self.source, self.output, self._report, lambda: self.cancelled)
            GLib.idle_add(self._upscale_finished)
        except UpscaleCancelled:
            GLib.idle_add(self._task_finished, "Upscale canceled")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            GLib.idle_add(self._task_finished, str(error))

    def _upscale_finished(self):
        self._close_task()
        if self.source is None or self.output is None:
            return GLib.SOURCE_REMOVE
        before = self._texture(self.source)
        after = self._texture(self.output)
        if before is None or after is None:
            self._toast("The upscaled image could not be read")
            return GLib.SOURCE_REMOVE
        self.result = after
        self.compare.set_images(before, after)
        model = self._selected_model()
        scale = f"  ({model.scale}×)" if model else ""
        self.status.set_label(f"{self.source.name}  ·  {after.get_width()} × {after.get_height()}{scale}")
        self._update_actions()
        self._toast(f"Upscaled to {after.get_width()} × {after.get_height()}")
        return GLib.SOURCE_REMOVE

    def _choose_export(self):
        if self.result is None:
            return
        dialog = Gtk.FileChooserNative(title="Export Image", transient_for=self, modal=True, action=Gtk.FileChooserAction.SAVE, accept_label="_Save", cancel_label="_Cancel")
        stem = self.source.stem if self.source else "upscaled"
        model = self._selected_model()
        dialog.set_current_name(f"{stem}-{model.scale}x.{self.settings.format}" if model else f"{stem}.{self.settings.format}")
        file_filter = Gtk.FileFilter(name=f"{self.settings.format.upper()} image")
        file_filter.add_pattern(f"*.{self.settings.format}")
        dialog.add_filter(file_filter)
        dialog.connect("response", self._export_chosen)
        dialog.show()

    def _export_chosen(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT and self.output is not None:
            file = dialog.get_file()
            if file and file.get_path():
                destination = Path(file.get_path())
                if destination.suffix.lower() != f".{self.settings.format}":
                    destination = destination.with_suffix(f".{self.settings.format}")
                shutil.copyfile(self.output, destination)
                self._toast(f"Exported {destination.name}")
        dialog.destroy()

    def _copy_image(self):
        if self.result is None:
            return
        provider = Gdk.ContentProvider.new_for_bytes("image/png", self.result.save_to_png_bytes())
        self.get_clipboard().set_content(provider)
        self._toast("Copied image to clipboard")

    def _on_drop(self, _target, value, _x, _y):
        files = value.get_files()
        if files and files[0].get_path():
            path = Path(files[0].get_path())
            if path.suffix.lower() in IMAGE_SUFFIXES:
                self.load_image(path)
                return True
            self._toast("Drop an image file to upscale it")
        return False

    def _open_settings(self):
        self.settings_dialog = Adw.PreferencesDialog(title="Settings")
        page = Adw.PreferencesPage()
        page.add(self._runtime_group())
        page.add(self._models_group())
        page.add(self._output_group())
        self.settings_dialog.add(page)
        self.settings_dialog.connect("closed", lambda *_: setattr(self, "settings_dialog", None))
        self._sync_settings()
        self.settings_dialog.present(self)

    def _runtime_group(self):
        group = Adw.PreferencesGroup(title="Engine", description="Downloads once and is shared by every model")
        row = Adw.ActionRow(title="ncnn-Vulkan runtime", subtitle="Required to upscale images")
        self.engine_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.engine_button.connect("clicked", lambda *_: self._install_engine())
        row.add_suffix(self.engine_button)
        group.add(row)
        return group

    def _models_group(self):
        group = Adw.PreferencesGroup(title="Models", description="Downloaded models appear in the model list")
        self.model_rows = []
        for model in MODELS:
            bundled = "Included with the engine" if model.bundled else "Downloadable"
            row = Adw.ActionRow(title=f"{model.label}  ·  {model.scale}×", subtitle=bundled)
            group.add(row)
            if model.url is None:
                continue
            button = Gtk.Button(valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_, item=model: self._toggle_model(item))
            row.add_suffix(button)
            self.model_rows.append((model, button))
        return group

    def _output_group(self):
        group = Adw.PreferencesGroup(title="Output")
        self.format_row = Adw.ComboRow(title="Format", model=Gtk.StringList.new([name.upper() for name in FORMATS]))
        self.format_row.set_selected(FORMATS.index(self.settings.format))
        self.format_row.connect("notify::selected", self._format_changed)
        group.add(self.format_row)
        return group

    def _sync_settings(self):
        if self.settings_dialog is None:
            return
        installed = self.upscaler.engine_installed()
        assert self.engine_button is not None
        self.engine_button.set_label("Installed" if installed else "Install")
        self.engine_button.set_sensitive(not installed)
        for model, button in self.model_rows:
            button.set_label("Remove" if self.upscaler.model_installed(model) else "Download")

    def _format_changed(self, row, _param):
        self.settings.format = FORMATS[row.get_selected()]
        self.settings.save()

    def _install_engine(self):
        self._run_task("Installing Engine", self._install_engine_worker)

    def _install_engine_worker(self):
        try:
            self.upscaler.install_engine(self._report)
            GLib.idle_add(self._task_finished, "Engine installed")
        except (OSError, RuntimeError) as error:
            GLib.idle_add(self._task_finished, str(error))

    def _toggle_model(self, model: Model):
        if self.upscaler.model_installed(model):
            self.upscaler.remove_model(model)
            self._sync_settings()
            self._refresh_models()
            self._toast(f"Removed {model.label}")
            return
        self._run_task(f"Downloading {model.label}", lambda: self._download_model_worker(model))

    def _download_model_worker(self, model: Model):
        try:
            self.upscaler.install_model(model, self._report)
            GLib.idle_add(self._task_finished, f"Downloaded {model.label}")
        except (OSError, RuntimeError) as error:
            GLib.idle_add(self._task_finished, str(error))

    def _run_task(self, title, worker, cancellable: bool = False):
        self.cancelled = False
        dialog = Adw.Dialog(title=title)
        dialog.set_can_close(False)
        dialog.set_content_width(420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        self.task_status = Gtk.Label(label="Starting", xalign=0)
        self.task_progress = Gtk.ProgressBar()
        box.append(self.task_status)
        box.append(self.task_progress)
        if cancellable:
            cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
            cancel.connect("clicked", lambda *_: setattr(self, "cancelled", True))
            box.append(cancel)
        dialog.set_child(box)
        self.task_dialog = dialog
        self.upscale_button.set_sensitive(False)
        dialog.present(self)
        threading.Thread(target=worker, daemon=True).start()

    def _report(self, fraction: float, label: str):
        GLib.idle_add(self._progressed, fraction, label)

    def _progressed(self, fraction, label):
        if self.task_dialog is not None:
            self.task_progress.set_fraction(fraction)
            self.task_status.set_label(label)
        return GLib.SOURCE_REMOVE

    def _task_finished(self, message):
        self._close_task()
        self._toast(message)
        return GLib.SOURCE_REMOVE

    def _close_task(self):
        if self.task_dialog is not None:
            self.task_dialog.force_close()
            self.task_dialog = None
        self._sync_settings()
        self._refresh_models()
        self._update_actions()

    def _update_actions(self):
        self.upscale_button.set_sensitive(self.source is not None)
        has_result = self.result is not None
        self.copy_button.set_sensitive(has_result)
        self.export_button.set_sensitive(has_result)

    @staticmethod
    def _texture(path: Path) -> Gdk.Texture | None:
        try:
            return Gdk.Texture.new_from_file(Gio.File.new_for_path(str(path)))
        except GLib.Error:
            return None

    def _toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=5))
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _install_css():
        provider = Gtk.CssProvider()
        provider.load_from_string("""
            .welcome-icon { color: @accent_color; }
            .compare-view { background: #111; border-radius: 12px; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main() -> int:
    return ImageUpscalerApplication().run(sys.argv)
