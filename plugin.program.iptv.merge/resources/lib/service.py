from slyguy import router, monitor
from slyguy.util import get_kodi_string, set_kodi_string, run_plugin
from slyguy.exceptions import Error

from .merger import check_merge_required, restart_pvr


def run_forever():
    set_kodi_string('_iptv_merge_service_running', '1')
    set_kodi_string('_iptv_merge_running')
    set_kodi_string('_iptv_merge_restart_pvr')

    while not monitor.waitForAbort(1):
        if check_merge_required():
            try:
                run_plugin(router.url_for('run_merge', force=0))
            except Error:
                # likely merge in progress
                pass

        if get_kodi_string('_iptv_merge_restart_pvr'):
            restart_pvr(force=False)
