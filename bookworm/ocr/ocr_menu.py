# coding: utf-8

import os
import threading
import time
import wx
import functools
from PIL import Image
from copy import copy
from pathlib import Path
from enum import IntEnum
from bookworm import app
from bookworm import config
from bookworm import speech
from bookworm.image_io import ImageIO
from bookworm.ocr_engines import OcrRequest
from bookworm.signals import (
    _signals,
    reader_book_loaded,
    reader_book_unloaded,
    reader_page_changed,
)
from bookworm.concurrency import QueueProcess, call_threaded, threaded_worker
from bookworm.gui.settings import SettingsPanel, ReconciliationStrategies
from bookworm.resources import sounds
from bookworm.gui.components import SimpleDialog, AsyncSnakDialog, RobustProgressDialog
from bookworm.utils import gui_thread_safe
from bookworm.logger import logger
from .ocr_dialogs import OCROptionsDialog

try:
    from bookworm.text_to_speech import should_auto_navigate_to_next_page
except ImportError:
    should_auto_navigate_to_next_page = None


log = logger.getChild(__name__)

# Signals
ocr_started = _signals.signal("ocr-started")
ocr_ended = _signals.signal("ocr-ended")


class OCRMenuIds(IntEnum):
    scanCurrentPage = 10001
    autoScanPages = 10002
    scanToTextFile = 10003
    changeOCROptions = 10004


OCR_KEYBOARD_SHORTCUTS = {
    OCRMenuIds.scanCurrentPage: "F4",
    OCRMenuIds.autoScanPages: "Ctrl-F4",
}


class OCRMenu(wx.Menu):
    """OCR menu."""

    def __init__(self, service, menubar):
        super().__init__()
        self.service = service
        self.menubar = menubar
        self.view = service.view
        self._ocr_cancelled = threading.Event()
        image2textId = wx.NewIdRef()

        # Add menu items
        self.Append(
            OCRMenuIds.scanCurrentPage,
            # Translators: the label of an item in the application menubar
            _("&Scan Current Page...\tF4"),
            # Translators: the help text of an item in the application menubar
            _("Run OCR on the current page"),
        )
        self.auto_scan_item = self.Append(
            OCRMenuIds.autoScanPages,
            # Translators: the label of an item in the application menubar
            _("&Automatic OCR\tCtrl-F4"),
            # Translators: the help text of an item in the application menubar
            _("Auto run  OCR when turning pages."),
            kind=wx.ITEM_CHECK,
        )
        self.Append(
            OCRMenuIds.changeOCROptions,
            # Translators: the label of an item in the application menubar
            _("&Change OCR Options..."),
            # Translators: the help text of an item in the application menubar
            _("Change OCR options"),
        )
        self.Append(
            OCRMenuIds.scanToTextFile,
            # Translators: the label of an item in the application menubar
            _("Scan To &Text File..."),
            # Translators: the help text of an item in the application menubar
            _("Scan pages and save the text to a .txt file."),
        )
        self.Append(
            image2textId,
            # Translators: the label of an item in the application menubar
            _("Image To Text..."),
            # Translators: the help text of an item in the application menubar
            _("Run OCR on an image."),
        )
        # Add the menu to the menubar
        # Translators: the label of the OCR menu in the application menubar
        self.menubar.Insert(2, self, _("OCR"))

        # Event handlers
        self.view.Bind(
            wx.EVT_MENU, self.onScanCurrentPage, id=OCRMenuIds.scanCurrentPage
        )
        self.view.Bind(wx.EVT_MENU, self.onAutoScanPages, id=OCRMenuIds.autoScanPages)
        self.view.Bind(wx.EVT_MENU, self.onScanToTextFile, id=OCRMenuIds.scanToTextFile)
        self.view.Bind(
            wx.EVT_MENU, self.onChangeOCROptions, id=OCRMenuIds.changeOCROptions
        )
        self.view.Bind(wx.EVT_MENU, self.onScanImageFile, id=image2textId)
        self.view.add_load_handler(self._on_reader_loaded)
        reader_book_unloaded.connect(self._on_reader_unloaded, sender=self.view.reader)
        reader_page_changed.connect(
            self._on_reader_page_changed, sender=self.service.reader
        )
        if should_auto_navigate_to_next_page:
            should_auto_navigate_to_next_page.connect(
                self.on_should_auto_navigate_to_next_page, sender=self.view
            )

    def _get_ocr_options(self, from_cache=True, **dlg_kw):
        last_stored_opts = self.service.stored_options
        if not from_cache:
            self.service.stored_options = None
            self.service.saved_scanned_pages.clear()
        if self.service.stored_options is not None:
            return self.service.stored_options
        else:
            opts = self._get_ocr_options_from_dlg(
                last_stored_options=last_stored_opts, **dlg_kw
            )
            if opts is not None and opts.store_options:
                self.service.stored_options = opts
            else:
                self.service.stored_options = None
            return opts

    def _get_ocr_options_from_dlg(self, last_stored_options=None, **dlg_kw):
        self.service._init_ocr_engine()
        langs = self.service.current_ocr_engine.get_sorted_languages()
        if not langs:
            wx.MessageBox(
                # Translators: content of a message
                _(
                    "No language for OCR is present.\nPlease checkout Bookworm user manual to learn how to add new languages."
                ),
                # Translators: title for a message
                _("No Languages for OCR"),
                style=wx.ICON_ERROR,
            )
            return
        dlg = OCROptionsDialog(
            parent=self.view,
            title=_("OCR Options"),
            languages=langs,
            stored_options=last_stored_options,
            **dlg_kw,
        )
        self.service.saved_scanned_pages.clear()
        return dlg.ShowModal()

    def onScanCurrentPage(self, event):
        self._ocr_cancelled.clear()
        ocr_opts = self._get_ocr_options()
        if ocr_opts is None:
            return speech.announce(_("Canceled"), True)
        reader = self.service.reader
        if reader.current_page in self.service.saved_scanned_pages:
            self.view.set_content(self.service.saved_scanned_pages[reader.current_page])
            return
        image = reader.document.get_page_image(
            reader.current_page,
            ocr_opts.zoom_factor,
        )

        def _ocr_callback(ocr_result):
            page_number = ocr_result.cookie
            content = ocr_result.recognized_text
            self.service.saved_scanned_pages[page_number] = content
            if page_number == self.view.reader.current_page:
                self.view.set_content(content)
                self.view.set_text_direction(ocr_opts.language.is_rtl)

        ocr_request = OcrRequest(
            language=ocr_opts.language,
            image=image,
            image_processing_pipelines=ocr_opts.image_processing_pipelines,
            cookie=reader.current_page,
        )
        self._run_ocr(ocr_request, _ocr_callback)

    def _run_ocr(self, ocr_request, callback):
        ocr_started.send(sender=self.view)
        # Show a modal dialog
        sounds.ocr_start.play()
        future_callback = functools.partial(self._process_ocr_result, callback)
        self._wait_dlg = AsyncSnakDialog(
            task=functools.partial(
                self.service.current_ocr_engine.preprocess_and_recognize, ocr_request
            ),
            done_callback=future_callback,
            message=_("Running OCR, please wait..."),
            dismiss_callback=self._on_ocr_cancelled,
        )

    def onAutoScanPages(self, event):
        event.Skip()
        if self.service.stored_options is None:
            self._get_ocr_options(force_save=True)
        if self.auto_scan_item.IsChecked():
            speech.announce(_("Automatic OCR is enabled"))
        else:
            speech.announce(_("Automatic OCR is disabled"))
        if not self.view.contentTextCtrl.GetValue():
            self.onScanCurrentPage(event)

    def onScanToTextFile(self, event):
        ocr_opts = self._get_ocr_options(from_cache=False, force_save=True)
        if ocr_opts is None:
            return
        # Get output file path
        filename = f"{self.view.reader.current_book.title}.txt"
        saveExportedFD = wx.FileDialog(
            self.view,
            # Translators: the title of a save file dialog asking the user for a filename to export notes to
            _("Save as"),
            defaultDir=wx.GetUserHome(),
            defaultFile=filename,
            # Translators: file type in a save as dialog
            wildcard=_("Plain Text") + "(*.txt)|.txt",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        if saveExportedFD.ShowModal() != wx.ID_OK:
            return
        output_file = saveExportedFD.GetPath().strip()
        saveExportedFD.Destroy()
        if not output_file:
            return
        # Continue with OCR
        progress_dlg = RobustProgressDialog(
            self.view,
            # Translators: the title of a progress dialog
            _("Scanning Pages"),
            # Translators: the message of a progress dialog
            message=_("Preparing book"),
            maxvalue=len(self.service.reader.document),
            can_hide=True,
            can_abort=True,
        )
        self._continue_with_text_extraction(ocr_opts, output_file, progress_dlg)

    @call_threaded
    def _continue_with_text_extraction(self, ocr_opts, output_file, progress_dlg):
        doc = self.service.reader.document
        total = len(doc)
        args = (doc, output_file, ocr_opts)
        scan2text_process = QueueProcess(
            target=self.service.current_ocr_engine.scan_to_text, args=args
        )
        progress_dlg.set_abort_callback(scan2text_process.cancel)
        scan2text_process.add_done_callback(
            wx.CallAfter,
            wx.MessageBox,
            _(
                "Successfully processed {total} pages.\nExtracted text was written to: {file}"
            ).format(total=total, file=output_file),
            _("OCR Completed"),
            wx.ICON_INFORMATION,
        )
        for progress in scan2text_process:
            progress_dlg.Update(
                progress + 1,
                f"Scanning page {progress} of {total}",
            )
        progress_dlg.Dismiss()
        wx.CallAfter(self.view.contentTextCtrl.SetFocus)

    def onChangeOCROptions(self, event):
        self._get_ocr_options(from_cache=False)

    def onScanImageFile(self, event):
        wildcard = []
        all_exts = [
            ("*.png", _("Portable Network Graphics")),
            ("*.jpg", _("JPEG images")),
            ("*.bmp", _("Bitmap images")),
            ("*.tif", _("Tiff graphics")),
        ]
        for ext, name in all_exts:
            wildcard.append("{name} ({ext})|{ext}|".format(name=name, ext=ext))
        wildcard[-1] = wildcard[-1].rstrip("|")
        allfiles = ";".join(ext[0] for ext in all_exts)
        wildcard.insert(0, _("All supported image formats") + f"|{allfiles}|")
        openFileDlg = wx.FileDialog(
            self.view,
            # Translators: the title of a file dialog to browse to an image
            message=_("Choose image file"),
            defaultDir=str(Path.home()),
            wildcard="".join(wildcard),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if openFileDlg.ShowModal() == wx.ID_OK:
            filename = openFileDlg.GetPath().strip()
            openFileDlg.Destroy()
            if not filename or not os.path.isfile(filename):
                return
            # Load the image file
            image = ImageIO.from_path(filename)
            if image is None:
                wx.MessageBox(
                    # Translators: content of a message box
                    _(
                        "Could not load image from\n{filename}.\n"
                        "Please make sure the file exists and the data contained in is not corrupted."
                    ).format(filename=filename),
                    # Translators: title of a message box
                    _("Could not load image file"),
                    style=wx.ICON_ERROR,
                )
                return
            options = self._get_ocr_options_from_dlg(force_save=True)
            if not options:
                return

            def _ocr_callback(ocr_result):
                content = ocr_result.recognized_text
                if self.service.reader.ready:
                    self.view.unloadCurrentEbook()
                self.view.set_content(content)
                self.view.set_text_direction(options.language.is_rtl)
                self.view.set_status(_("OCR Results"))

            factor = options.zoom_factor
            resized_image = image.to_pil().resize(
                (factor * image.width, factor * image.height), resample=Image.LANCZOS
            )
            ocr_request = OcrRequest(
                language=options.language,
                image=ImageIO.from_pil(resized_image),
                image_processing_pipelines=options.image_processing_pipelines,
            )
            self._run_ocr(ocr_request, _ocr_callback)

    @gui_thread_safe
    def _process_ocr_result(self, callback, task):
        if self._ocr_cancelled.is_set():
            ocr_ended.send(sender=self.view, isfaulted=True)
            self._ocr_cancelled.clear()
            return
        try:
            ocr_result = task.result()
        except Exception as e:
            log.exception(f"Error getting OCR recognition results.", exc_info=True)
            ocr_ended.send(sender=self.view, isfaulted=True)
            return
        callback(ocr_result)
        sounds.ocr_end.play()
        speech.announce(_("Scan finished."), urgent=True)
        self.view.contentTextCtrl.SetFocusFromKbd()
        ocr_ended.send(sender=self.view, isfaulted=False)

    def _on_ocr_cancelled(self):
        self._ocr_cancelled.set()
        speech.announce(_("OCR canceled"), True)
        sounds.ocr_end.play()
        return True

    def _on_reader_loaded(self, sender):
        can_render = sender.document.can_render_pages()
        for item_id in OCRMenuIds:
            self.Enable(item_id, can_render)

    def _on_reader_unloaded(self, sender):
        self.service.stored_options = None
        self.service.saved_scanned_pages.clear()
        self.auto_scan_item.Check(False)

    def _on_reader_page_changed(self, sender, current, prev):
        if self.auto_scan_item.IsChecked():
            self.onScanCurrentPage(None)

    def on_should_auto_navigate_to_next_page(self, sender):
        return not self.auto_scan_item.IsChecked()
