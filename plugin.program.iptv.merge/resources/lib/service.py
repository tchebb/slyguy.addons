from slyguy import router, monitor
from slyguy.util import set_kodi_string, run_plugin
from slyguy.log import log
from slyguy.exceptions import Error

from .merger import check_merge_required, restart_pvr


def run_forever():
    set_kodi_string('_iptv_merge_service_running', '1')
    set_kodi_string('_iptv_merge_running')

    restart_pending = False
    while not monitor.waitForAbort(1):
        if check_merge_required():
            try:
                run_plugin(router.url_for('run_merge', force=0))
            except Error as e:
                log.error(e)
            else:
                restart_pending = True

        if restart_pending:
            restart_pending = restart_pvr()
