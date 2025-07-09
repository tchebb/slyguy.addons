from kodi_six import xbmcvfs
from six.moves.urllib.parse import unquote_plus

from slyguy import router, monitor
from slyguy.util import get_kodi_string, set_kodi_string
from slyguy.log import log

from .merger import check_merge_required, restart_pvr
from .settings import settings


def run_forever():
    restart_queued = None
    just_booted = True

    set_kodi_string('_iptv_merge_service_running', '1')
    set_kodi_string('_iptv_merge_force_run')

    delay = settings.getInt('service_delay', 0)
    if delay:
        log.debug('Service delay: {}s'.format(delay))
        monitor.waitForAbort(delay)

    while not monitor.waitForAbort(1):
        forced = get_kodi_string('_iptv_merge_force_run') or 0
        merge_required = check_merge_required()

        if forced or merge_required:
            set_kodi_string('_iptv_merge_force_run', '1')

            url = router.url_for('run_merge', forced=int(forced))
            _, files = xbmcvfs.listdir(url)
            result, _ = int(files[0][0]), unquote_plus(files[0][1:])
            if result:
                restart_queued = True
            set_kodi_string('_iptv_merge_force_run')

        if just_booted:
            forced = True
            just_booted = False

        if restart_queued:
            restart_queued = restart_pvr(forced=forced)
