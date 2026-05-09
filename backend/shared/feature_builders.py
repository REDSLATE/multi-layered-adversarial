"""Feature builders registry — shared catalog of feature-engineering recipes
all runtimes can opt into. Recipes are pure & deterministic by contract."""
from namespaces import SHARED_FEATURE_BUILDERS


async def list_feature_builders(db) -> list[dict]:
    return await db[SHARED_FEATURE_BUILDERS].find({}, {"_id": 0}).sort("name", 1).to_list(500)
