import json
import socket
import time
from .transports import TCPTransport, StdioTransport, Transport
from .process import attach_logger
try:
    import subprocess
    from typing import Any, List, Dict, Tuple, Callable, Optional, Union
    # from mypy_extensions import TypedDict
    assert Any and List and Dict and Tuple and Callable and Optional and Union and subprocess
except ImportError:
    pass

from .logging import debug, exception_log
from .protocol import Request, Notification, Response
from .types import Settings
from threading import Condition
from threading import Lock

TCP_CONNECT_TIMEOUT = 5
DEFAULT_SYNC_REQUEST_TIMEOUT = 1.0

# RequestDict = TypedDict('RequestDict', {'id': 'Union[str,int]', 'method': str, 'params': 'Optional[Any]'})


def format_request(payload: 'Dict[str, Any]') -> str:
    """Converts the request into json"""
    return json.dumps(payload, sort_keys=False)


def attach_tcp_client(tcp_port: int, process: 'subprocess.Popen', settings: Settings) -> 'Optional[Client]':
    if settings.log_stderr:
        attach_logger(process, process.stdout)

    host = "localhost"
    start_time = time.time()
    debug('connecting to {}:{}'.format(host, tcp_port))

    while time.time() - start_time < TCP_CONNECT_TIMEOUT:
        try:
            sock = socket.create_connection((host, tcp_port))
            transport = TCPTransport(sock)

            client = Client(transport, settings)
            client.set_transport_failure_handler(lambda: try_terminate_process(process))
            return client
        except ConnectionRefusedError:
            pass

    process.kill()
    raise Exception("Timeout connecting to socket")


def attach_stdio_client(process: 'subprocess.Popen', settings: Settings) -> 'Client':
    transport = StdioTransport(process)

    # TODO: process owner can take care of this outside client?
    if settings.log_stderr:
        attach_logger(process, process.stderr)
    client = Client(transport, settings)
    client.set_transport_failure_handler(lambda: try_terminate_process(process))
    return client


def try_terminate_process(process: 'subprocess.Popen') -> None:
    try:
        process.terminate()
    except ProcessLookupError:
        pass  # process can be terminated already


class Client(object):
    def __init__(self, transport: Transport, settings: Settings) -> None:
        self.transport = transport  # type: Optional[Transport]
        self.transport.start(self.receive_payload, self.on_transport_closed)
        self.request_id = 0
        self._response_handlers = {}  # type: Dict[int, Tuple[Optional[Callable], Optional[Callable[[Any], None]]]]
        self._request_handlers = {}  # type: Dict[str, Callable]
        self._notification_handlers = {}  # type: Dict[str, Callable]
        self._sync_request_results = {}  # type: Dict[int, Optional[Any]]
        self._sync_request_lock = Lock()
        self._sync_request_cvar = Condition(self._sync_request_lock)
        self.exiting = False
        self._crash_handler = None  # type: Optional[Callable]
        self._transport_fail_handler = None  # type: Optional[Callable]
        self._error_display_handler = lambda msg: debug(msg)
        self.settings = settings

    def send_request(
            self,
            request: Request,
            handler: 'Callable[[Optional[Any]], None]',
            error_handler: 'Optional[Callable[[Any], None]]' = None,
    ) -> 'None':
        self.request_id += 1
        if self.transport is not None:
            debug(' --> ' + request.method)
            self._response_handlers[self.request_id] = (handler, error_handler)
            self.send_payload(request.to_payload(self.request_id))
        else:
            debug('unable to send', request.method)
            if error_handler is not None:
                error_handler(None)
            return None

    def execute_request(self, request: Request, timeout: float = DEFAULT_SYNC_REQUEST_TIMEOUT) -> 'Optional[Any]':
        """
        Sends a request and waits for response up to timeout (default: 1 second), blocking the current thread.
        """
        if self.transport is None:
            debug('unable to send', request.method)
            return None

        debug(' ==> ' + request.method)
        self.request_id += 1
        request_id = self.request_id
        self.send_payload(request.to_payload(request_id))
        result = None
        try:
            with self._sync_request_cvar:
                # We go to sleep. We wake up once another thread calls .notify() on this condition variable.
                self._sync_request_cvar.wait_for(lambda: request_id in self._sync_request_results, timeout)
                result = self._sync_request_results.pop(request_id)
        except KeyError as e:
            debug('timeout on', request.method)
            return None
        return result

    def send_notification(self, notification: Notification) -> None:
        if self.transport is not None:
            debug(' --> ' + notification.method)
            self.send_payload(notification.to_payload())
        else:
            debug('unable to send', notification.method)

    def send_response(self, response: Response) -> None:
        self.send_payload(response.to_payload())

    def exit(self) -> None:
        self.exiting = True
        self.send_notification(Notification.exit())

    def set_crash_handler(self, handler: 'Callable') -> None:
        self._crash_handler = handler

    def set_error_display_handler(self, handler: 'Callable') -> None:
        self._error_display_handler = handler

    def set_transport_failure_handler(self, handler: 'Callable') -> None:
        self._transport_fail_handler = handler

    def handle_transport_failure(self) -> None:
        debug('transport failed')
        self.transport = None
        if self._transport_fail_handler is not None:
            self._transport_fail_handler()
        if self._crash_handler is not None:
            self._crash_handler()

    def send_payload(self, payload: 'Dict[str, Any]') -> None:
        if self.transport:
            message = format_request(payload)
            self.transport.send(message)

    def receive_payload(self, message: str) -> None:
        payload = None
        try:
            payload = json.loads(message)
            # limit = min(len(message), 200)
            # debug("got json: ", message[0:limit], "...")
        except IOError as err:
            exception_log("got a non-JSON payload: " + message, err)
            return

        try:
            if "method" in payload:
                if "id" in payload:
                    self.handle("request", payload, self._request_handlers, payload.get("id"))
                else:
                    self.handle("notification", payload, self._notification_handlers)
            elif "id" in payload:
                self.response_handler(payload)
            else:
                debug("Unknown payload type: ", payload)
        except Exception as err:
            exception_log("Error handling server payload", err)

    def on_transport_closed(self) -> None:
        self._error_display_handler("Communication to server closed, exiting")
        # Differentiate between normal exit and server crash?
        if not self.exiting:
            self.handle_transport_failure()

    def response_handler(self, response: 'Dict[str, Any]') -> None:
        # This response handler *must not* run from the same thread that does a sync request
        # because of the usage of the condition variable below.
        request_id = int(response["id"])
        if self.settings.log_payloads:
            debug('     ' + str(response.get("result", None)))
        handler, error_handler = self._response_handlers.pop(request_id, (None, None))
        if "result" in response and "error" not in response:
            if handler:
                handler(response["result"])
            else:
                with self._sync_request_cvar:
                    self._sync_request_results[request_id] = response["result"]
                    # At most one thread is waiting on the result.
                    self._sync_request_cvar.notify()
        elif "result" not in response and "error" in response:
            error = response["error"]
            if self.settings.log_payloads:
                debug('ERR: ' + str(error))
            if error_handler:
                error_handler(error)
            else:
                self._error_display_handler(error.get("message"))
        else:
            debug('invalid response payload', response)

    def on_request(self, request_method: str, handler: 'Callable') -> None:
        self._request_handlers[request_method] = handler

    def on_notification(self, notification_method: str, handler: 'Callable') -> None:
        self._notification_handlers[notification_method] = handler

    def handle(self, typestr: str, message: 'Dict[str, Any]', handlers: 'Dict[str, Callable]', *args: 'Any') -> None:
        method = message.get("method", "")
        params = message.get("params")
        if method != "window/logMessage":
            debug('<--  ' + method)
            if self.settings.log_payloads and params:
                debug('     ' + str(params))
        handler = handlers.get(method)
        if handler:
            try:
                handler(params, *args)
            except Exception as err:
                exception_log("Error handling {} {}".format(typestr, method), err)
        else:
            debug("Unhandled {}".format(typestr), method)
