import shutil
import threading

from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver import ThreadingMixIn

from slyguy import gui, log, monitor
from slyguy.constants import CHUNK_SIZE
from slyguy.util import check_port, kodi_rpc, sleep

from .settings import settings
from .constants import DEFAULT_HTTP_PORT, PLAYLIST_FILE_NAME, RUN_MERGE_URL, IPTV_SIMPLE_ID
from .merger import Merger, restart_pvr


FORCE_LOCK = threading.Lock()
MERGE_LOCK = threading.Lock()


class RequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.request.settimeout(5)

    def do_GET(self):
        path = self.path.lstrip('/').strip('\\')

        if path in (PLAYLIST_FILE_NAME,):
            return self._output_merge(path)
        elif path == RUN_MERGE_URL:
            return self._run_merge()

        self.send_response(404)
        self.end_headers()

    def _run_merge(self):
        if not FORCE_LOCK.acquire(blocking=False):
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'A merge is already running')
            return

        try:
            progress = gui.progressbg(heading='Waiting for other merge to finish')
            with MERGE_LOCK:
                progress.close()
                settings.reset()
                Merger().merge(force=True)
            restart_pvr(force=True)
        finally:
            FORCE_LOCK.release()

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def _output_merge(self, name):
        with MERGE_LOCK:
            settings.reset()
            paths = Merger().merge(force=False)

        self.send_response(200)
        self.end_headers()
        with open(paths[name], 'rb') as f:
            shutil.copyfileobj(f, self.wfile, length=CHUNK_SIZE)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve_forever():
    if settings.HTTP_QUIET_BOOT.value:
        # PVR starts and tries to load our http which fails
        # We disable the service, start our http and then enable the service
        # It then does no retries for the http url and therefore no notification
        kodi_rpc('Addons.SetAddonEnabled', {'addonid': IPTV_SIMPLE_ID, 'enabled': False})
        # enougth time for the current http request to timeout
        sleep(1.5)

    try:
        port = settings.HTTP_PORT.value
        if port is None:
            port = check_port(DEFAULT_HTTP_PORT)
            if not port:
                port = check_port()
                log.warning('Port {} not available. Switched to port {}'.format(DEFAULT_HTTP_PORT, port))

        try:
            server = ThreadedHTTPServer(('0.0.0.0', port), RequestHandler)
        except Exception as e:
            log.exception(e)
            settings.HTTP_URL.clear()
            error = 'Unable to start HTTP Server on port: {}. You can change port under IPTV Merge -> Settings -> Add-on'.format(port)
            log.error(error)
            gui.error(error)
            return

        settings.HTTP_PORT.store_value(port)
        server.allow_reuse_address = True
        httpd_thread = threading.Thread(target=server.serve_forever)
        httpd_thread.start()

        http_path = 'http://{}:{}/'.format('127.0.0.1', port)
        settings.HTTP_URL.value = http_path
        log.info("HTTP Server Started: {}".format(http_path))
    finally:
        if settings.HTTP_QUIET_BOOT.value:
            sleep(0.5) # make sure http ready to accept. worse case itll retry and show notification
            kodi_rpc('Addons.SetAddonEnabled', {'addonid': IPTV_SIMPLE_ID, 'enabled': True})

    try:
        monitor.waitForAbort()
    finally:
        settings.HTTP_URL.clear()
        server.shutdown()
        server.server_close()
        server.socket.close()
        httpd_thread.join()
        log.debug("HTTP Server Stopped")
