class BaseResolver:
    # Every resolver will override this to specify what domain it looks for, so let's just leave it like that?
    domain_keyword: str = ""
    source_name: str = ""

    async def resolve(self, embed_url: str) -> str | None:
        raise NotImplementedError("Resolvers must implement the resolve method")