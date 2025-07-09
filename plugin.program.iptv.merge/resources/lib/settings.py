from slyguy.util import kodi_rpc
from slyguy.constants import ADDON, ADDON_ID, KODI_VERSION
from slyguy.settings import CommonSettings, is_donor
from slyguy.settings.types import Bool, Text, Browse, Number, Action

from .language import _
from .constants import DEFAULT_USERAGENT


def restart_service():
    kodi_rpc('Addons.SetAddonEnabled', {'addonid': ADDON_ID, 'enabled': False})
    kodi_rpc('Addons.SetAddonEnabled', {'addonid': ADDON_ID, 'enabled': True})


class Settings(CommonSettings):
    OUTPUT_DIR = Browse('output_dir', _.OUTPUT_DIR, type=Browse.DIRECTORY, default=ADDON.getAddonInfo('profile'), use_default=False)
    GZ_EPG = Bool('gz_epg', _.GZ_EPG, default=False, visible=KODI_VERSION >= 18, enable=is_donor, disabled_reason=_.SUPPORTER_ONLY)
    MERGE_EVERY_X = Bool('auto_merge', _.MERGE_EVERY_X, default=True, disabled_value=None, enable=lambda: not Settings.MERGE_AT_HOUR.value)
    X_HOURS = Number('reload_time_hours', _.X_HOURS, default=12, lower_limit=1, upper_limit=48, visible=lambda: Settings.MERGE_EVERY_X.value)

    MERGE_AT_HOUR = Bool('merge_at_hour', _.MERGE_AT_HOUR, default=False, disabled_value=None, enable=lambda: not Settings.MERGE_EVERY_X.value)
    MERGE_HOUR = Number('merge_hour', _.MERGE_HOUR, default=3, lower_limit=0, upper_limit=23, visible=lambda: Settings.MERGE_AT_HOUR.value)

    RESTART_PVR = Bool('restart_pvr', _.RESTART_PVR, default=True)
    START_CH_NO = Number('start_ch_no', _.START_CH_NO, default=1)
    REMOVE_EPG_ORPHANS = Bool('remove_epg_orphans', _.REMOVE_EPG_ORPHANS, default=False)
    HIDE_GROUPS = Text('hide_groups', _.HIDE_GROUPS)
    DISABLE_GROUPS = Bool('disable_groups', _.DISABLE_GROUPS, default=False)
    GROUP_ORDER = Text('group_order', _.GROUP_ORDER, enable=lambda: not Settings.DISABLE_GROUPS.value)

    SERVICE_DELAY = Number('service_delay', _.SERVICE_DELAY, default=0)
    SETUP_IPTV_SIMPLE = Action("RunPlugin(plugin://{}/?_=setup)".format(ADDON_ID), _.SETUP_IPTV_SIMPLE)
    PAGE_SIZE = Number('page_size', _.PAGE_SIZE, default=200)
    ASK_TO_ADD = Bool('ask_to_add', _.ASK_TO_ADD, default=False)
    IPTV_MERGE_PROXY = Bool('iptv_merge_proxy', _.IPTV_MERGE_PROXY, default=True)
    DEFAULT_USER_AGENT = Text('user_agent', _.DEFAULT_USER_AGENT, default=DEFAULT_USERAGENT, parent=IPTV_MERGE_PROXY)

    HTTP_METHOD = Bool('http_method', _.HTTP_METHOD, default=True, visible=KODI_VERSION >= 21, enable=KODI_VERSION >= 21, after_save=lambda val: restart_service(), after_clear=restart_service)
    HTTP_PORT = Number('http_port', default=None, default_label=_.AUTO, after_save=lambda val: restart_service(), after_clear=restart_service)
    HTTP_URL = Text('http_url', visible=False)


settings = Settings()
