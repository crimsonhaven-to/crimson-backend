from .vidmoly import VidmolyResolver
from .voe import VoeResolver
from .vidking import VidkingResolver
from .animekai import AnimekaiResolver

# The unified list of all our resolvers
ALL_RESOLVERS = [
    VidmolyResolver,
    VoeResolver,
    VidkingResolver,
    AnimekaiResolver,
]