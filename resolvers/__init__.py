from .vidmoly import VidmolyResolver
from .voe import VoeResolver
from .vidking import VidkingResolver
from .vidking_test import VidkingTestResolver
from .movish import MovishResolver
from .playimdb import PlayimdbResolver
from .jellyfin import JellyfinResolver
from .animekai import AnimekaiResolver
from .animesuge import AnimeSugeResolver, DirectM3U8Resolver, AsbGamesResolver

# The unified list of all our resolvers.
# NOTE: The plain VidkingResolver is DEPRECATED — the ad-free VidKing variant
# (VidkingTestResolver, the "crimson_proxy=1" proxy) is thoroughly tested and now
# the only active VidKing source. The import is kept so resolvers/vidking.py stays
# wired and can be re-enabled, but it's intentionally out of ALL_RESOLVERS.
# MovishResolver matches on the distinct "api.movish.net" host.
ALL_RESOLVERS = [
    VidmolyResolver,
    VoeResolver,
    VidkingTestResolver,
    # VidkingResolver,  # ARCHIVED — legacy VidKing (raw vidking.net embed, ads).
    #                   # Superseded by VidkingTestResolver. Import kept so it can
    #                   # be re-enabled by uncommenting this line.
    MovishResolver,
    PlayimdbResolver,
    JellyfinResolver,
    AnimekaiResolver,
    AnimeSugeResolver,  # ad-free: extracts direct mp4/m3u8, proxies + /player
    DirectM3U8Resolver,
    AsbGamesResolver,
]