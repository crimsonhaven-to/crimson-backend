from .vidmoly import VidmolyResolver
from .voe import VoeResolver
from .movish import MovishResolver
from .playimdb import PlayimdbResolver
from .jellyfin import JellyfinResolver
from .local import LocalResolver
from .animekai import AnimekaiResolver
from .animesuge import AnimeSugeResolver, DirectM3U8Resolver, AsbGamesResolver
from .cinemabz import (
    CinemabzTcloudResolver,
    CinemabzIpcloudResolver,
    CinemabzNgcloudResolver,
)
from .vidsrc import VidSrcResolver
from .febbox import FebboxResolver

# The unified list of all our resolvers.
# MovishResolver matches on the distinct "api.movish.net" host.
ALL_RESOLVERS = [
    VidmolyResolver,
    VoeResolver,
    MovishResolver,
    PlayimdbResolver,
    JellyfinResolver,
    LocalResolver,  # admin-registered local dirs / NAS mounts -> /local_proxy (direct play)
    AnimekaiResolver,
    AnimeSugeResolver,  # ad-free: extracts direct mp4/m3u8, proxies + /player
    DirectM3U8Resolver,
    AsbGamesResolver,
    # cinema.bz: TMDB-keyed HLS, one resolver per provider (three switchable tiles)
    CinemabzTcloudResolver,
    CinemabzIpcloudResolver,
    CinemabzNgcloudResolver,
    VidSrcResolver,  # aniwatch.co.at "VidSrc" server -> megaplay HLS
    FebboxResolver,  # ShowBox/Febbox direct-file source (env-gated on FEBBOX_UI_TOKEN)
]