from .vidmoly import VidmolyResolver
from .voe import VoeResolver
from .vidking import VidkingResolver
from .animekai import AnimekaiResolver
from .flix2day import DirectM3U8Resolver, AsbGamesResolver

# The unified list of all our resolvers
ALL_RESOLVERS = [
    VidmolyResolver,
    VoeResolver,
    VidkingResolver,
    AnimekaiResolver,
    DirectM3U8Resolver,
    AsbGamesResolver,
]