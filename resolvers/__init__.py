from .vidmoly import VidmolyResolver
from .voe import VoeResolver
from .vidking import VidkingResolver
from .vidking_test import VidkingTestResolver
from .animekai import AnimekaiResolver
from .animesuge import DirectM3U8Resolver, AsbGamesResolver

# The unified list of all our resolvers.
# NOTE: VidkingTestResolver MUST precede VidkingResolver — both match vidking.net
# URLs, but only the test resolver matches the "crimson_proxy=1" marker, and
# resolve_streams() picks the first matching resolver.
ALL_RESOLVERS = [
    VidmolyResolver,
    VoeResolver,
    VidkingTestResolver,
    VidkingResolver,
    AnimekaiResolver,
    DirectM3U8Resolver,
    AsbGamesResolver,
]