from .vidmoly import VidmolyResolver
from .voe import VoeResolver
from .movish import MovishResolver
from .playimdb import PlayimdbResolver
from .jellyfin import JellyfinResolver
from .local import LocalResolver
from .animesuge import AnimeSugeResolver, DirectM3U8Resolver, AsbGamesResolver
from .cinemabz import (
    CinemabzTcloudResolver,
    CinemabzIpcloudResolver,
    CinemabzNgcloudResolver,
)
from .screenscape import SCREENSCAPE_RESOLVERS
from .vidsrc import VidSrcResolver
from .febbox import FebboxResolver
from .cache import CacheResolver

# The unified list of all our resolvers.
# MovishResolver matches on the distinct "api.movish.net" host.
ALL_RESOLVERS = [
    CacheResolver,  # server-side cache -> /cache_proxy (direct play); labelled per NAS target
    VidmolyResolver,
    VoeResolver,
    MovishResolver,
    PlayimdbResolver,
    JellyfinResolver,
    LocalResolver,  # admin-registered local dirs / NAS mounts -> /local_proxy (direct play)
    AnimeSugeResolver,  # ad-free: extracts direct mp4/m3u8, proxies + /player
    DirectM3U8Resolver,
    AsbGamesResolver,
    # cinema.bz: TMDB-keyed HLS, one resolver per provider (three switchable tiles)
    CinemabzTcloudResolver,
    CinemabzIpcloudResolver,
    CinemabzNgcloudResolver,
    # ScreenScape: TMDB-keyed multi-server aggregator; one resolver per server,
    # each may return several quality/language tiles (signed/encrypted API).
    *SCREENSCAPE_RESOLVERS,
    VidSrcResolver,  # aniwatch.co.at "VidSrc" server -> megaplay HLS
    FebboxResolver,  # ShowBox/Febbox direct-file source (env-gated on FEBBOX_UI_TOKEN)
]