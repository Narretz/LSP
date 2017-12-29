import sublime
import sublime_plugin

from collections import OrderedDict

try:
    from typing import Any, List, Dict, Tuple, Callable, Optional
    assert Any and List and Dict and Tuple and Callable and Optional
except ImportError:
    pass

from .logging import debug
from .protocol import Notification, Point, Range
from .settings import settings
from .url import filename_to_uri
from .configurations import config_for_scope, is_supported_view, is_supported_syntax, is_supportable_syntax
from .clients import client_for_view, window_clients, check_window_unloaded
from .events import Events

assert Range

SUBLIME_WORD_MASK = 515


def get_document_position(view: sublime.View, point) -> 'Optional[OrderedDict]':
    file_name = view.file_name()
    if file_name:
        if not point:
            point = view.sel()[0].begin()
        d = OrderedDict()  # type: OrderedDict[str, Any]
        d['textDocument'] = {"uri": filename_to_uri(file_name)}
        d['position'] = Point.from_text_point(view, point).to_lsp()
        return d
    else:
        return None


def get_position(view: sublime.View, event=None) -> int:
    if event:
        return view.window_to_text((event["x"], event["y"]))
    else:
        return view.sel()[0].begin()


def is_at_word(view: sublime.View, event) -> bool:
    pos = get_position(view, event)
    point_classification = view.classify(pos)
    if point_classification & SUBLIME_WORD_MASK:
        return True
    else:
        return False


# TODO: this should be per-window ?
document_states = {}  # type: Dict[int, Dict[str, DocumentState]]


class DocumentState:
    """Stores synchronization state for documents open in a language service"""
    def __init__(self, path: str) -> 'None':
        self.path = path
        self.version = 0


def get_document_state(window: sublime.Window, path: str) -> DocumentState:
    window_document_states = document_states.setdefault(window.id(), {})
    if path not in window_document_states:
        window_document_states[path] = DocumentState(path)
    return window_document_states[path]


def has_document_state(window: sublime.Window, path: str):
    window_id = window.id()
    if window_id not in document_states:
        return False
    return path in document_states[window_id]


def clear_document_state(window: sublime.Window, path: str):
    window_id = window.id()
    if window_id in document_states:
        del document_states[window_id][path]


def clear_document_states(window: sublime.Window):
    if window.id() in document_states:
        del document_states[window.id()]


class TextChange(object):
    def __init__(self, view: sublime.View,
                 change_count: int,
                 range: 'Optional[Range]' = None,
                 text: 'Optional[str]' = None) -> None:
        self.view = view
        self.change_count = change_count
        self.range = range
        self.text = text

    def __repr__(self):
        return "{} {} {}".format(self.view.file_name(), self.range, self.text)


pending_buffer_changes = dict()  # type: Dict[int, List[TextChange]]


def get_change_list(buffer_id: int):
    if buffer_id in pending_buffer_changes:
        return pending_buffer_changes[buffer_id]
    else:
        change_list = list()  # type: List[TextChange]
        pending_buffer_changes[buffer_id] = change_list
        return change_list


def queue_did_change(view: sublime.View, region: sublime.Region, newText: 'Optional[str]'):
    buffer_id = view.buffer_id()

    change_list = get_change_list(buffer_id)
    change_count = view.change_count()

    if change_count > len(change_list):
        range = Range.from_region(view, region)
        change = TextChange(view, change_count, range, newText)
        change_list.append(change)
        debug(change)
    else:
        raise Exception('No changes found')

    sublime.set_timeout_async(
        lambda: purge_did_change(buffer_id, change_count), 500)


def purge_did_change(buffer_id: int, requested_version=None):
    if buffer_id not in pending_buffer_changes:
        debug('no pending changes for buffer', buffer_id)
        return

    assert buffer_id in pending_buffer_changes
    change_list = pending_buffer_changes[buffer_id]
    last_edit = change_list[-1]

    if requested_version is None or requested_version == last_edit.change_count:
        debug('purging at version', requested_version)
        # notify_did_change(last_edit.view)
        notify_did_incremental_change(last_edit)
    else:
        debug('skipping version', requested_version)
    # else:
    #     debug('buffer version ', buffer_version, ' in purge')


def notify_did_open(view: sublime.View):
    config = config_for_scope(view)
    client = client_for_view(view)
    if client and config:
        view.settings().set("show_definitions", False)
        window = view.window()
        view_file = view.file_name()
        if window and view_file:
            if not has_document_state(window, view_file):
                ds = get_document_state(window, view_file)
                if settings.show_view_status:
                    view.set_status("lsp_clients", config.name)
                params = {
                    "textDocument": {
                        "uri": filename_to_uri(view_file),
                        "languageId": config.languageId,
                        "text": view.substr(sublime.Region(0, view.size())),
                        "version": ds.version
                    }
                }
                client.send_notification(Notification.didOpen(params))


def notify_did_close(view: sublime.View):
    file_name = view.file_name()
    window = sublime.active_window()
    if window and file_name:
        if has_document_state(window, file_name):
            clear_document_state(window, file_name)
            config = config_for_scope(view)
            clients = window_clients(sublime.active_window())
            if config and config.name in clients:
                client = clients[config.name]
                params = {"textDocument": {"uri": filename_to_uri(file_name)}}
                client.send_notification(Notification.didClose(params))


def notify_did_save(view: sublime.View):
    file_name = view.file_name()
    window = view.window()
    if window and file_name:
        if has_document_state(window, file_name):
            client = client_for_view(view)
            if client:
                params = {"textDocument": {"uri": filename_to_uri(file_name)}}
                client.send_notification(Notification.didSave(params))
        else:
            debug('document not tracked', file_name)


def to_content_change(change: TextChange):
    return {
        "text": change.text,
        "range": change.range.to_lsp() if change.range else None
    }


def notify_did_incremental_change(change: TextChange):
    view = change.view
    file_name = view.file_name()
    window = view.window()
    if window and file_name:
        assert view.buffer_id() in pending_buffer_changes
        pending_changes = pending_buffer_changes[view.buffer_id()]
        content_changes = list(to_content_change(change) for change in pending_changes)
        del pending_buffer_changes[view.buffer_id()]
        debug('pending list cleared')

        client = client_for_view(view)
        if client:
            document_state = get_document_state(window, file_name)
            uri = filename_to_uri(file_name)
            change_count = view.change_count()

            params = {
                "textDocument": {
                    "uri": uri,
                    # "languageId": config.languageId, clangd does not like this field, but no server uses it?
                    "version": change_count,
                },
                "contentChanges": [content_changes]
            }
            debug('didChange', params)
            document_state.version = change_count
            # client.send_notification(Notification.didChange(params))


def notify_did_change(view: sublime.View):
    file_name = view.file_name()
    window = view.window()
    if window and file_name:
        if view.buffer_id() in pending_buffer_changes:
            del pending_buffer_changes[view.buffer_id()]
            debug('pending list cleared')
        else:
            debug('no pending changes for buffer')
        client = client_for_view(view)
        if client:
            document_state = get_document_state(window, file_name)
            uri = filename_to_uri(file_name)
            change_count = view.change_count()
            # todo: add rangeLength from documentState to contentChange
            params = {
                "textDocument": {
                    "uri": uri,
                    # "languageId": config.languageId, clangd does not like this field, but no server uses it?
                    "version": change_count,
                },
                "contentChanges": [{
                    "text": view.substr(sublime.Region(0, view.size())),
                }]
            }
            client.send_notification(Notification.didChange(params))
            document_state.version = change_count


document_sync_initialized = False


class CloseListener(sublime_plugin.EventListener):
    def on_close(self, view):
        if is_supported_syntax(view.settings().get("syntax")):
            Events.publish("view.on_close", view)
        sublime.set_timeout_async(check_window_unloaded, 500)


class SaveListener(sublime_plugin.EventListener):
    def on_post_save_async(self, view):
        if is_supported_view(view):
            Events.publish("view.on_post_save_async", view)


def is_transient_view(view):
    window = view.window()
    return view == window.transient_view_in_group(window.active_group())


def get_cursors(view):
    return [cursor for cursor in view.sel()]  # can't use `view.sel()[:]` because it gives an error `TypeError: an integer is required`


class IncrementalSyncListener(sublime_plugin.EventListener):
    prev_cursors = {}  # type: Dict[int, List[Tuple[sublime.Region, str]]]

    def on_new_async(self, view):
        self.record_cursor_pos(view)

    def on_activated_async(self, view):
        self.record_cursor_pos(view)

    def record_cursor_pos(self, view):
        cursors = []
        for cursor in get_cursors(view):
            if cursor.empty() and cursor.begin() > 0:  # if the cursor is empty and isn't at the start of the document
                cursors.append((cursor, view.substr(cursor.begin() - 1)))  # record the previous character for backspace purposes
            else:
                cursors.append((cursor, view.substr(cursor)))  # record the text inside the cursor

        self.prev_cursors[view.id()] = cursors

    def on_selection_modified_async(self, view):
        self.record_cursor_pos(view)

    def on_insert(self, view, cursor_begin, cursor_end, text):
        # self.log('insert', 'from', view.rowcol(cursor_begin), 'to', view.rowcol(cursor_end), '"' + text + '"')
        if view.file_name():
            Events.publish("view.on_modified", view, sublime.Region(cursor_begin, cursor_end), text)

    def on_delete(self, view, cursor_begin, cursor_end, text):
        # self.log('delete', 'from', view.rowcol(cursor_begin), 'to', cursor_end, '"' + text + '"')
        if view.file_name():
            Events.publish("view.on_modified", view, sublime.Region(cursor_begin, cursor_end), None)

    def log(self, *values):
        print(type(self).__name__, *values)

    def on_modified_async(self, view):
        offset = 0
        for index, cursor in enumerate(view.sel()):
            prev_cursor, prev_text = self.prev_cursors[view.id()][index]
            prev_cursor = sublime.Region(prev_cursor.begin() + offset, prev_cursor.end() + offset)
            if not prev_cursor.empty() or cursor.begin() < prev_cursor.begin():
                self.on_delete(view, prev_cursor.begin(), prev_cursor.end(), prev_text)
            if cursor.begin() > prev_cursor.begin():
                region = prev_cursor.cover(cursor)
                self.on_insert(view, region.begin(), region.end(), view.substr(region))
            offset += cursor.begin() - prev_cursor.begin()


class DocumentSyncListener(sublime_plugin.ViewEventListener):
    def __init__(self, view):
        self.view = view

    @classmethod
    def is_applicable(cls, settings):
        syntax = settings.get('syntax')
        # This enables all of document sync for any supportable syntax
        # Global performance cost, consider a detect_lsp_support setting
        return syntax and (is_supported_syntax(syntax) or is_supportable_syntax(syntax))

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    def on_load_async(self):
        # skip transient views: if not is_transient_view(self.view):
        Events.publish("view.on_load_async", self.view)

    # def on_modified(self):
    #     if self.view.file_name():
    #         Events.publish("view.on_modified", self.view)

    def on_activated_async(self):
        if self.view.file_name():
            Events.publish("view.on_activated_async", self.view)


def initialize_document_sync(text_document_sync_kind):
    global document_sync_initialized
    if document_sync_initialized:
        return
    document_sync_initialized = True
    # TODO: hook up events per scope/client
    Events.subscribe('view.on_load_async', notify_did_open)
    Events.subscribe('view.on_activated_async', notify_did_open)
    Events.subscribe('view.on_modified', queue_did_change)
    Events.subscribe('view.on_post_save_async', notify_did_save)
    Events.subscribe('view.on_close', notify_did_close)
