import threading
import time

LATEST_HOT_URL_REGISTRY = set()
LATEST_HOT_URL_REGISTRY_LOCK = threading.Lock()
LATEST_HOT_URL_REGISTRY_TTL = 5 * 60
LATEST_HOT_URL_REGISTRY_UPDATED = 0.0

# Recent category stories are remembered so Latest & Hot can avoid
# showing the same story that a reader has already seen in a category.
CATEGORY_ARTICLE_REGISTRY = {}
CATEGORY_ARTICLE_REGISTRY_TTL = 15 * 60
CATEGORY_ARTICLE_REGISTRY_MAX = 600
