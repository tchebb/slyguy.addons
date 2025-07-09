import shutil
import threading

from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver import ThreadingMixIn


from slyguy import gui, log, monitor
from slyguy.constants import CHUNK_SIZE
from slyguy.util import check_port

from .settings import settings
from .constants import DEFAULT_HTTP_PORT, PLAYLIST_FILE_NAME, RUN_MERGE_URL
from .merger import Merger, check_merge_required, epg_file_name, restart_pvr
from .language import _


FORCE_LOCK = threading.Lock()
MERGE_LOCK = threading.Lock()


class RequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.request.settimeout(5)

    def do_GET(self):
        path = self.path.lstrip('/').strip('\\')
        if path == PLAYLIST_FILE_NAME:
            return self._playlist_url()
        elif path == epg_file_name():
            return self._epg_url()
        elif path == RUN_MERGE_URL:
            return self._run_merge()

        self.send_response(404)
        self.end_headers()

    def _run_merge(self):
        if not FORCE_LOCK.acquire(blocking=False):
            self.send_response(200)
            self.end_headers()
            gui.notification(_.MERGE_IN_PROGRESS)
            self.wfile.write(_.MERGE_IN_PROGRESS.encode('utf-8'))
            return 

        self.send_response(200)
        self.end_headers()
        try:
            progress = gui.progressbg(heading='Waiting for running PVR merge to finish')

            with MERGE_LOCK:
                progress.close()
                merge = Merger(forced=1)
                merge.playlists(refresh=True)
                merge.epgs(refresh=True)
                restart_pvr(forced=True)
                self.wfile.write(b"OK")
        finally:
            FORCE_LOCK.release()

    def _playlist_url(self):
        self.send_response(200)
        self.end_headers()
        with MERGE_LOCK:
            settings.reset()
            refresh = check_merge_required()
            path = Merger().playlists(refresh)
            with open(path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile, length=CHUNK_SIZE)

    def _epg_url(self):
        self.send_response(200)
        self.end_headers()
        settings.reset()
        refresh = check_merge_required()
        with MERGE_LOCK:
            path = Merger().epgs(refresh)
            with open(path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile, length=CHUNK_SIZE)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve_forever():
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

    try:
        monitor.waitForAbort()
    finally:
        settings.HTTP_URL.clear()
        server.shutdown()
        server.server_close()
        server.socket.close()
        httpd_thread.join()
        log.debug("HTTP Server Stopped")
