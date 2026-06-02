from .vidmoly import VidmolyResolver
from .voe_test import VOEResolver
# Future resolvers get imported here:

# The unified list of all our resolvers
ALL_RESOLVERS = [
    VidmolyResolver,
    VOEResolver,
]