#!/usr/bin/env python3
import hashlib
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Literal

import gi  # type: ignore

gi.require_version(namespace="Gtk", version="4.0")
gi.require_version(namespace="Adw", version="1")
gi.require_version(namespace="Nautilus", version="4.0")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Nautilus, Pango  # type: ignore

Adw.init()


def get_logger(name: str) -> logging.Logger:
    loglevel_str = os.getenv("LOGLEVEL", "INFO").upper()
    loglevel = getattr(logging, loglevel_str, logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(loglevel)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s.%(funcName)s(): %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = get_logger(__name__)

css = b"""
.view-switcher button {
    background-color: #404040;
    color: white;
    transition: background-color 0.5s ease;
    }
.view-switcher button:nth-child(1):hover {
    background-color: #2b66b8;
}
.view-switcher button:nth-child(1):active {
    background-color: #1c457e;
}
.view-switcher button:nth-child(1):checked {
    background-color: #2b66b8;
}
.view-switcher button:nth-child(2):hover {
    background-color: #c7162b;
}
.view-switcher button:nth-child(2):active {
    background-color: #951323;
}
.view-switcher button:nth-child(2):checked {
    background-color: #c7162b;
}
"""
css_provider = Gtk.CssProvider()
css_provider.load_from_data(css)
Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class AdwNautilusExtension(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        pass

    def launch_app(self, menu_item: Nautilus.MenuItem, files: list[Nautilus.FileInfo]):
        file_paths = [f.get_location().get_path() for f in files if f.get_location()]
        cmd = ["python3", Path(__file__).as_posix()] + file_paths
        subprocess.Popen(cmd)

    def get_background_items(self, current_folder: Nautilus.FileInfo) -> list[Nautilus.MenuItem]:
        item = Nautilus.MenuItem(
            name="AdwNautilusExtension::OpenFolderInApp",
            label="Calculate Hashes",
        )
        item.connect("activate", self.launch_app, [current_folder])
        return [item]

    def get_file_items(self, files: list[Nautilus.FileInfo]) -> list[Nautilus.MenuItem]:
        if not files:
            return []
        item = Nautilus.MenuItem(
            name="AdwNautilusExtension::OpenFilesInApp",
            label="Calculate Hashes",
        )
        item.connect("activate", self.launch_app, files)
        return [item]


class HashResultRow(Adw.ActionRow):
    def __init__(self, file_name: str, hash_value: str, hash_algorithm: str, **kwargs):
        super().__init__(**kwargs)
        self.file_name = file_name
        self.hash_value = hash_value
        self.algo = hash_algorithm
        self.set_title(self.file_name)
        self.set_subtitle(self.hash_value)
        self.set_subtitle_lines(1)
        self.set_title_lines(1)

        self.prefix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.prefix_box.set_valign(Gtk.Align.CENTER)
        self.file_icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        self.prefix_box.append(self.file_icon)
        self.prefix_box.append(Gtk.Label(label=self.algo.upper()))
        self.add_prefix(self.prefix_box)

        self.button_make_hashes = Gtk.Button()
        self.button_make_hashes.set_child(Gtk.Label(label="Multi-Hash"))
        self.button_make_hashes.set_valign(Gtk.Align.CENTER)
        self.button_make_hashes.set_tooltip_text("Calculate all available hash types for this file")
        self.button_make_hashes.connect("clicked", self.on_click_make_hashes)

        self.button_copy_hash = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        self.button_copy_hash.set_valign(Gtk.Align.CENTER)
        self.button_copy_hash.set_tooltip_text("Copy hash")
        self.button_copy_hash.connect("clicked", self.on_copy_clicked)

        self.button_compare = Gtk.Button.new_from_icon_name("edit-paste-symbolic")
        self.button_compare.set_valign(Gtk.Align.CENTER)
        self.button_compare.set_tooltip_text("Compare with clipboard")
        self.button_compare.connect("clicked", self.on_compare_clicked)

        self.button_delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.button_delete.set_valign(Gtk.Align.CENTER)
        self.button_delete.set_tooltip_text("Remove this result")
        self.button_delete.connect("clicked", self.on_delete_clicked)

        self.add_suffix(self.button_make_hashes)
        self.add_suffix(self.button_copy_hash)
        self.add_suffix(self.button_compare)
        self.add_suffix(self.button_delete)

    def __str__(self):
        return f"{self.file_name}:{self.hash_value}:{self.algo}"

    def on_click_make_hashes(self, button: Gtk.Button):
        main_window: MainWindow = button.get_root()
        for algo in main_window.available_algorithms:
            if algo != self.algo:
                main_window.start_job([Path(self.get_title())], algo)

    def on_copy_clicked(self, button: Gtk.Button):
        button.set_sensitive(False)
        button.get_clipboard().set(self.hash_value)
        original_child = button.get_child()
        button.set_child(Gtk.Label(label="Copied!"))
        GLib.timeout_add(1500, lambda: (button.set_child(original_child), button.set_sensitive(True)))

    def on_compare_clicked(self, button: Gtk.Button):
        def handle_clipboard_comparison(clipboard, result):
            main_window: MainWindow = button.get_root()
            try:
                self.button_compare.set_sensitive(False)
                clipboard_text: str = clipboard.read_text_finish(result).strip()
                if clipboard_text == self.hash_value:
                    self.set_icon_("emblem-ok-symbolic")
                    self.set_css_("success")
                    main_window.add_toast(f"<big>✅ Clipboard hash matches <b>{self.get_title()}</b>!</big>")
                else:
                    self.set_icon_("dialog-error-symbolic")
                    self.set_css_("error")
                    main_window.add_toast(f"<big>❌ The clipboard hash does <b>not</b> match <b>{self.get_title()}</b>!</big>")

                GLib.timeout_add(
                    3000,
                    lambda: (
                        self.set_css_classes(self.old_css),
                        self.set_icon_(self.old_file_icon_name),
                        self.button_compare.set_sensitive(True),
                    ),
                )
            except Exception as e:
                logger.exception(f"Error reading clipboard: {e}")
                main_window.add_toast(f"<big>❌ Clipboard read error: {e}</big>")

        clipboard = button.get_clipboard()
        clipboard.read_text_async(None, handle_clipboard_comparison)

    def on_delete_clicked(self, button: Gtk.Button):
        button.set_sensitive(False)
        anim = Adw.TimedAnimation(
            widget=self,
            value_from=1.0,
            value_to=0.0,
            duration=200,
            target=Adw.CallbackAnimationTarget.new(lambda opacity: self.set_opacity(opacity)),
        )

        def on_fade_done(_):
            parent: Gtk.ListBox = self.get_parent()
            parent.remove(self)
            if parent.get_first_child() is None:
                main_window: MainWindow = parent.get_root()
                main_window.has_results()

        anim.connect("done", on_fade_done)
        anim.play()

    def set_icon_(
        self,
        icon_name: Literal[
            "text-x-generic-symbolic",
            "emblem-ok-symbolic",
            "dialog-error-symbolic",
        ],
    ):
        self.old_file_icon_name = self.file_icon.get_icon_name()
        self.file_icon.set_from_icon_name(icon_name)

    def set_css_(self, css_class: Literal["success", "error"]):
        self.old_css = self.get_css_classes()
        self.add_css_class(css_class)

    def error(self):
        self.add_css_class("error")
        self.set_icon_("dialog-error-symbolic")
        self.button_copy_hash.set_tooltip_text("Copy error message to clipboard")
        self.button_compare.set_sensitive(False)
        self.button_make_hashes.set_sensitive(False)


class MainWindow(Adw.ApplicationWindow):
    DEFAULT_WIDTH = 970
    DEFAULT_HIGHT = 600
    algo: str = "sha256"

    def __init__(self, app, paths: list[Path] | None = None):
        super().__init__(application=app)
        self.set_default_size(self.DEFAULT_WIDTH, self.DEFAULT_HIGHT)
        self.set_size_request(self.DEFAULT_WIDTH, self.DEFAULT_HIGHT)
        self.update_queue = Queue()
        self.cancel_event = threading.Event()
        self.build_ui()
        if paths:
            self.start_job(paths)

    def build_ui(self):
        self.empty_placeholder = Adw.StatusPage(
            title="No Results",
            description="Select files or folders to calculate their hashes.",
            icon_name="text-x-generic-symbolic",
        )
        self.empty_error_placeholder = Adw.StatusPage(
            title="No Errors",
            icon_name="emblem-ok-symbolic",
        )
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        self.toolbar_view = Adw.ToolbarView()
        self.toolbar_view.set_margin_top(6)
        self.toolbar_view.set_margin_bottom(6)
        self.toolbar_view.set_margin_start(12)
        self.toolbar_view.set_margin_end(12)
        self.toast_overlay.set_child(self.toolbar_view)

        self.first_top_bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_bottom=10)
        self.second_top_bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_bottom=5)
        self.setup_buttons()
        self.setup_headerbar()
        self.setup_main_content()
        self.setup_progress_bar()
        self.setup_drag_and_drop()

        self.toolbar_view.add_top_bar(self.first_top_bar_box)
        self.toolbar_view.add_top_bar(self.second_top_bar_box)
        self.toolbar_view.set_content(self.empty_placeholder)
        self.toolbar_view.add_bottom_bar(self.progress_bar)

    def setup_main_content(self):
        self.main_content_overlay = Gtk.Overlay()

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(100, 100)
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.spinner.start()
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        self.main_content_overlay.add_overlay(self.spinner)
        self.main_content_overlay.add_overlay(self.main_box)

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)
        self.view_switcher.set_stack(self.view_stack)
        self.view_switcher.add_css_class("view-switcher")
        self.results_group = Adw.PreferencesGroup()
        self.results_group.set_hexpand(True)
        self.results_group.set_vexpand(True)
        self.ui_results = Gtk.ListBox()
        self.ui_results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.ui_results.add_css_class("boxed-list")
        self.results_group.add(self.ui_results)
        self.results_scrolled_window = Gtk.ScrolledWindow()
        self.results_scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.results_scrolled_window.set_child(self.results_group)
        self.results_stack_page = self.view_stack.add_titled(self.results_scrolled_window, "results", "Results")
        self.results_stack_page.set_icon_name("view-list-symbolic")
        self.results_stack_page.set_use_underline(True)

        self.errors_group = Adw.PreferencesGroup()
        self.errors_group.set_hexpand(True)
        self.errors_group.set_vexpand(True)
        self.ui_errors = Gtk.ListBox()
        self.ui_errors.set_selection_mode(Gtk.SelectionMode.NONE)
        self.ui_errors.add_css_class("boxed-list")
        self.errors_group.add(self.ui_errors)
        self.errors_scrolled_window = Gtk.ScrolledWindow()
        self.errors_scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.errors_scrolled_window.set_child(self.errors_group)
        self.errors_stack_page = self.view_stack.add_titled(self.errors_scrolled_window, "errors", "Errors")
        self.errors_stack_page.set_icon_name("dialog-error-symbolic")
        self.view_stack.set_visible_child_name("results")
        self.main_box.append(self.view_stack)

        self.view_stack.connect("notify::visible-child", self.has_results)

    def setup_buttons(self):
        self.button_open = Gtk.Button()
        self.button_open.add_css_class("suggested-action")
        self.button_open.set_valign(Gtk.Align.CENTER)
        self.button_open.set_tooltip_text("Select files to add")
        self.button_open.connect("clicked", self.on_select_files_clicked)
        self.button_open_content = Adw.ButtonContent.new()
        self.button_open_content.set_icon_name(icon_name="document-open-symbolic")
        self.button_open_content.set_label(label="_Open")
        self.button_open_content.set_use_underline(use_underline=True)
        self.button_open.set_child(self.button_open_content)
        self.first_top_bar_box.append(self.button_open)

        self.button_save = Gtk.Button()
        self.button_save.set_sensitive(False)
        self.button_save.add_css_class("suggested-action")
        self.button_save.set_valign(Gtk.Align.CENTER)
        self.button_save.set_tooltip_text("Save results to file")
        self.button_save.connect("clicked", self.on_save_clicked)
        self.button_save_content = Adw.ButtonContent.new()
        self.button_save_content.set_icon_name(icon_name="document-save-symbolic")
        self.button_save_content.set_label(label="_Save")
        self.button_save_content.set_use_underline(use_underline=True)
        self.button_save.set_child(self.button_save_content)
        self.first_top_bar_box.append(self.button_save)

        self.button_cancel = Gtk.Button(label="Cancel Job")
        self.button_cancel.add_css_class("destructive-action")
        self.button_cancel.set_valign(Gtk.Align.CENTER)
        self.button_cancel.set_visible(False)
        self.button_cancel.set_tooltip_text("Cancel the current operation")
        self.button_cancel.connect(
            "clicked",
            lambda _: (
                self.cancel_event.set(),
                self.add_toast("<big>❌ Job cancelled</big>"),
            ),
        )
        self.first_top_bar_box.append(self.button_cancel)

        self.available_algorithms = sorted(hashlib.algorithms_guaranteed)
        self.drop_down_algo_button = Gtk.DropDown.new_from_strings(strings=self.available_algorithms)
        self.drop_down_algo_button.set_selected(self.available_algorithms.index("sha256"))
        self.drop_down_algo_button.set_valign(Gtk.Align.CENTER)
        self.drop_down_algo_button.set_tooltip_text("Choose hashing algorithm")
        self.drop_down_algo_button.connect("notify::selected-item", self.on_selected_item)
        self.first_top_bar_box.append(self.drop_down_algo_button)

        self.spacer = Gtk.Box()
        self.spacer.set_hexpand(True)
        self.first_top_bar_box.append(self.spacer)

        self.view_switcher = Adw.ViewSwitcher()
        self.view_switcher.set_sensitive(False)
        self.view_switcher.set_hexpand(True)
        self.view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        self.second_top_bar_box.append(self.view_switcher)

        self.spacer = Gtk.Box()
        self.spacer.set_hexpand(True)
        self.second_top_bar_box.append(self.spacer)

        self.button_copy_all = Gtk.Button(label="Copy")
        self.button_copy_all.set_sensitive(False)
        self.button_copy_all.add_css_class("suggested-action")
        self.button_copy_all.set_valign(Gtk.Align.CENTER)
        self.button_copy_all.set_tooltip_text("Copy results to clipboard")
        self.button_copy_all.connect("clicked", self.on_copy_all_clicked)
        self.second_top_bar_box.append(self.button_copy_all)

        self.button_sort = Gtk.Button(label="Sort")
        self.button_sort.set_sensitive(False)
        self.button_sort.set_valign(Gtk.Align.CENTER)
        self.button_sort.set_tooltip_text("Sort results by path")
        self.button_sort.connect(
            "clicked",
            lambda _: (
                self.ui_results.set_sort_func(lambda r1, r2: (r1.get_title() > r2.get_title()) - (r1.get_title() < r2.get_title())),
                self.ui_results.set_sort_func(None),
                self.add_toast("<big>✅ Results sorted by file path</big>"),
            ),
        )
        self.second_top_bar_box.append(self.button_sort)

        self.button_clear = Gtk.Button(label="Clear")
        self.button_clear.set_sensitive(False)
        self.button_clear.add_css_class("destructive-action")
        self.button_clear.set_valign(Gtk.Align.CENTER)
        self.button_clear.set_tooltip_text("Clear all results")
        self.button_clear.connect(
            "clicked",
            lambda _: (
                self.ui_results.remove_all(),
                self.ui_errors.remove_all(),
                self.has_results(),
                self.add_toast("<big>✅ Results cleared</big>"),
            ),
        )
        self.second_top_bar_box.append(self.button_clear)

        self.button_about = Gtk.Button()
        self.button_about.set_valign(Gtk.Align.CENTER)
        self.button_about.connect("clicked", self.on_click_present_about_dialog)
        self.button_about_content = Adw.ButtonContent.new()
        self.button_about_content.set_icon_name(icon_name="help-about-symbolic")
        self.button_about_content.set_label(label="About")
        self.button_about_content.set_use_underline(use_underline=True)
        self.button_about.set_child(self.button_about_content)
        self.second_top_bar_box.append(self.button_about)

    def setup_headerbar(self):
        self.header_bar = Adw.HeaderBar()
        self.header_title_widget = Gtk.Label(label=f"<big><b>Calculate {self.algo.upper()} Hashes</b></big>", use_markup=True)
        self.header_bar.set_title_widget(self.header_title_widget)
        self.first_top_bar_box.append(self.header_bar)

    def setup_progress_bar(self):
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(False)
        self.progress_bar.set_visible(False)

    def setup_drag_and_drop(self):
        self.dnd = Adw.StatusPage(
            title="Drop Files Here",
            icon_name="folder-open-symbolic",
        )
        self.drop = Gtk.DropTargetAsync.new(None, Gdk.DragAction.COPY)
        self.drop.connect(
            "drag-enter",
            lambda *_: (
                self.toolbar_view.set_content(self.dnd),
                Gdk.DragAction.COPY,
            )[1],
        )
        self.drop.connect(
            "drag-leave",
            lambda *_: (
                self.has_results(),
                Gdk.DragAction.COPY,
            )[1],
        )

        def on_read_value(drop: Gdk.Drop, result):
            try:
                files: Gdk.FileList = drop.read_value_finish(result)
                paths = [Path(f.get_path()) for f in files.get_files()]
                action = Gdk.DragAction.COPY
            except Exception as e:
                action = 0
                self.add_toast(f"Drag & Drop failed: {e}")
            else:
                self.start_job(paths)
            finally:
                drop.finish(action)

        self.drop.connect(
            "drop",
            lambda ctrl, drop, x, y: (
                self.has_results(),
                drop.read_value_async(
                    Gdk.FileList,
                    GLib.PRIORITY_DEFAULT,
                    None,
                    on_read_value,
                ),
            ),
        )
        self.add_controller(self.drop)

    def start_job(self, paths: list[Path], algo: str | None = None):
        self.cancel_event.clear()
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_opacity(1.0)
        self.progress_bar.set_visible(True)
        self.spinner.set_opacity(1.0)
        self.spinner.set_visible(True)
        self.button_cancel.set_visible(True)
        self.button_open.set_sensitive(False)
        self.drop_down_algo_button.set_sensitive(False)
        self.toolbar_view.set_content(self.main_content_overlay)
        self.processing_thread = threading.Thread(target=self.calculate_hash, args=(paths, algo or self.algo), daemon=True)
        self.processing_thread.start()
        GLib.timeout_add(100, self.process_queue, priority=GLib.PRIORITY_DEFAULT)
        GLib.timeout_add(100, self.hide_progress, priority=GLib.PRIORITY_DEFAULT)
        GLib.timeout_add(100, self.check_processing_complete, priority=GLib.PRIORITY_DEFAULT)

    def process_queue(self):
        if self.cancel_event.is_set():
            self.update_queue = Queue()
            return False  # Stop monitoring
        iterations = 0
        while not self.update_queue.empty() and iterations < 8:
            update = self.update_queue.get_nowait()
            if update[0] == "progress":
                _, progress = update
                self.progress_bar.set_fraction(progress)
            elif update[0] == "result":
                _, fname, hash_value, algo = update
                self.add_result(fname, hash_value, algo)
                iterations += 1
            elif update[0] == "error":
                _, fname, err, algo = update
                self.add_result(fname, err, algo, is_error=True)
                # self.add_toast(f"<big>❌ <b>{err}</b></big>")
                iterations += 1
            else:
                return False
        return True  # Continue monitoring

    def hide_progress(self):
        if self.progress_bar.get_fraction() == 1.0 or self.cancel_event.is_set():
            Adw.TimedAnimation(
                widget=self.progress_bar,
                value_from=1.0,
                value_to=0,
                duration=2000,
                target=Adw.CallbackAnimationTarget.new(
                    lambda opacity: self.progress_bar.set_opacity(opacity),
                ),
            ).play()
            Adw.TimedAnimation(
                widget=self.spinner,
                value_from=1.0,
                value_to=0,
                duration=2000,
                target=Adw.CallbackAnimationTarget.new(
                    lambda opacity: self.spinner.set_opacity(opacity),
                ),
            ).play()
            GLib.timeout_add(2000, self.spinner.set_visible, False)
            GLib.timeout_add(100, self.scroll_to_bottom, priority=GLib.PRIORITY_DEFAULT)
            return False  # Stop monitoring
        return True  # Continue monitoring

    def check_processing_complete(self):
        if self.progress_bar.get_fraction() == 1.0 or self.cancel_event.is_set():
            self.button_open.set_sensitive(True)
            self.button_cancel.set_visible(False)
            self.drop_down_algo_button.set_sensitive(True)
            self.has_results()
            return False  # Stop monitoring
        return True  # Continue monitoring

    def calculate_hash(self, paths: list[Path], algo: str):
        total_bytes = 0
        jobs = []
        for path in paths:
            try:
                if path.is_dir():
                    for f in path.iterdir():
                        if f.is_file():
                            total_bytes += f.stat().st_size
                            jobs.append(f)
                else:
                    total_bytes += path.stat().st_size
                    jobs.append(path)
            except Exception as e:
                logger.exception(f"Error processing file: {e}")
                self.update_queue.put(("error", str(path), str(e), algo))

        if total_bytes == 0:
            self.progress_bar.set_fraction(1.0)

        def hash_task(file: Path, shake_length: int = 32):
            if self.cancel_event.is_set():
                return
            if file.stat().st_size > 1024 * 1024 * 100:
                chunk_size = 1024 * 1024 * 4
            else:
                chunk_size = 1024 * 1024
            hash_obj = hashlib.new(algo)
            try:
                with open(file, "rb") as f:
                    while chunk := f.read(chunk_size):
                        if self.cancel_event.is_set():
                            return
                        hash_obj.update(chunk)
                        hash_task.bytes_read += len(chunk)
                        self.update_queue.put(("progress", min(hash_task.bytes_read / total_bytes, 1.0)))
                    self.update_queue.put(
                        (
                            "result",
                            file.as_posix(),
                            hash_obj.hexdigest(shake_length) if "shake" in algo else hash_obj.hexdigest(),
                            algo,
                        )
                    )
            except Exception as e:
                logger.exception(f"Error computing hash for file: {file.as_posix()}")
                self.update_queue.put(("error", str(file), str(e), algo))

        hash_task.bytes_read = 0
        with ThreadPoolExecutor(max_workers=max(1, os.cpu_count() - 1)) as executor:
            list(executor.map(hash_task, jobs))

    def scroll_to_bottom(self):
        vadjustment = self.results_scrolled_window.get_vadjustment()
        current_value = vadjustment.get_value()
        target_value = vadjustment.get_upper() - vadjustment.get_page_size()
        Adw.TimedAnimation(
            widget=self,
            value_from=current_value,
            value_to=target_value,
            duration=500,
            target=Adw.CallbackAnimationTarget.new(lambda value: vadjustment.set_value(value)),
        ).play()

    def add_result(self, file_name: str, hash_value: str, algo: str, is_error: bool = False):
        row = HashResultRow(file_name, hash_value, algo)
        if is_error:
            self.ui_errors.append(row)
            row.error()
        else:
            self.ui_results.append(row)
        return row

    def has_results(self, *signal_from_view_stack):
        has_results = self.ui_results.get_first_child() is not None
        has_errors = self.ui_errors.get_first_child() is not None

        self.view_switcher.set_sensitive(has_results or has_errors)
        self.button_save.set_sensitive(has_results or has_errors)
        self.button_copy_all.set_sensitive(has_results or has_errors)
        self.button_sort.set_sensitive(has_results)
        self.button_clear.set_sensitive(has_results or has_errors)
        self.results_stack_page.set_badge_number(sum(1 for _ in self.ui_results) if has_results else 0)
        self.errors_stack_page.set_badge_number(sum(1 for _ in self.ui_errors) if has_errors else 0)

        current_page_name = self.view_stack.get_visible_child_name()
        show_empty = (current_page_name == "results" and not has_results) or (current_page_name == "errors" and not has_errors)
        relevant_placeholder = self.empty_placeholder if current_page_name == "results" else self.empty_error_placeholder
        target = relevant_placeholder if show_empty else self.main_content_overlay
        if self.toolbar_view.get_content() == target and not signal_from_view_stack:
            return
        self.toolbar_view.set_content(target)
        Adw.TimedAnimation(
            widget=self,
            value_from=0.1,
            value_to=1.0,
            duration=500,
            target=Adw.CallbackAnimationTarget.new(lambda opacity: target.set_opacity(opacity)),
        ).play()

    def results_to_txt(self):
        results_text = "\n".join(str(r) for r in self.ui_results)
        errors_text = "\n".join(str(r) for r in self.ui_errors)
        now = datetime.now().strftime("%B %d, %Y at %I:%H:%M %Z")
        if results_text:
            output = f"Results - Saved on {now}:\n{'-' * 40}\n{results_text} {'\n\n' if errors_text else '\n'}"
        if errors_text:
            output = f"{output}Errors - Saved on {now}:\n{'-' * 40}\n{errors_text}\n"
        return output

    def on_click_present_about_dialog(self, _):
        about_dialog = Adw.AboutDialog.new()
        about_dialog.set_application_name("Quick File Hasher")
        about_dialog.set_version("0.6.5")
        about_dialog.set_developer_name("Doğukan Doğru (dd-se)")
        about_dialog.set_license_type(Gtk.License(Gtk.License.MIT_X11))
        about_dialog.set_comments("A modern Nautilus extension and standalone GTK4/libadwaita app to calculate hashes.")
        about_dialog.set_website("https://github.com/dd-se/nautilus-extension-quick-file-hasher")
        about_dialog.set_issue_url("https://github.com/dd-se/nautilus-extension-quick-file-hasher/issues")
        about_dialog.set_copyright("© 2025 Doğukan Doğru (dd-se)")
        about_dialog.set_developers(["dd-se https://github.com/dd-se"])
        about_dialog.present(self)

    def on_select_files_clicked(self, _):
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(title="Select files")

        def on_files_dialog_dismissed(file_dialog: Gtk.FileDialog, gio_task):
            files = file_dialog.open_multiple_finish(gio_task)
            paths = [Path(f.get_path()) for f in files]
            self.start_job(paths)

        file_dialog.open_multiple(
            parent=self,
            callback=on_files_dialog_dismissed,
        )

    def on_copy_all_clicked(self, button: Gtk.Button):
        clipboard = button.get_clipboard()
        output = self.results_to_txt()
        clipboard.set(output)
        self.add_toast("<big>✅ Results copied to clipboard</big>")

    def on_save_clicked(self, widget):
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(title="Save")
        file_dialog.set_initial_name(name="results.txt")
        file_dialog.set_modal(modal=True)

        def on_file_dialog_dismissed(file_dialog: Gtk.FileDialog, gio_task):
            local_file = file_dialog.save_finish(gio_task)
            path: str = local_file.get_path()
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.results_to_txt())
                self.add_toast(f"<big>✅ Saved to <b>{path}</b></big>")
            except Exception as e:
                self.add_toast(f"<big>❌ Failed to save: {e}</big>")

        file_dialog.save(parent=self, callback=on_file_dialog_dismissed)

    def on_selected_item(self, drop_down: Gtk.DropDown, g_param_object):
        self.algo = drop_down.get_selected_item().get_string()
        self.header_title_widget.set_label(f"<big><b>Calculate {self.algo.upper()} Hashes</b></big>")

    def add_toast(self, toast_label: str, timeout: int = 2, priority=Adw.ToastPriority.NORMAL):
        toast = Adw.Toast(
            custom_title=Gtk.Label(
                label=toast_label,
                use_markup=True,
                ellipsize=Pango.EllipsizeMode.MIDDLE,
            ),
            timeout=timeout,
            priority=priority,
        )
        self.toast_overlay.add_toast(toast)


class Application(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.github.dd-se.quick-file-hasher",
            flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        logger.info(f"App {self.get_application_id()} activated")
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
        win.present()

    def do_open(self, files, n_files, hint):
        logger.info(f"App {self.get_application_id()} opened with files ({n_files})")
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        win = self.props.active_window
        if not win:
            win = MainWindow(self, paths)
        else:
            win.start_job(paths)
        win.present()

    def do_startup(self):
        Adw.Application.do_startup(self)

    def do_shutdown(self):
        Adw.Application.do_shutdown(self)


if __name__ == "__main__":
    app = Application()
    try:
        app.run(sys.argv)
    except KeyboardInterrupt:
        logger.info("App interrupted by user.")
    finally:
        app.quit()
