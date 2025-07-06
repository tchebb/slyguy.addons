from slyguy.language import BaseLanguage


class Language(BaseLanguage):
    LIVE_TV   = 30000
    HOME      = 30001
    SPORTS    = 30002
    REPLAYS   = 30003
    API_ERROR = 30004


_ = Language()
