// ============================================================
// THE NEWSROOM - FRONTEND
// AI-POWERED NEWS + CAREER INTELLIGENCE
// ============================================================

const API_BASE_URL = window.location.origin;

let allArticles = [];
let currentDisplayedArticles = [];
let currentCategory = "latest";

let currentCategoryCache = {};
const CATEGORY_CACHE_TTL = 10 * 60 * 1000;
// v3 invalidates snapshots created before every category required a balanced
// 20-article set from four to six publishers.
const CATEGORY_CACHE_STORAGE_PREFIX = "newsroom-category-cache-v3:";
let categoryLoadSequence = 0;
let lastObservedIndiaDate = indiaCalendarDate(new Date());
let freshCategoryRequestSequence = 0;
const pendingArticleActions = new Set();
const jobsIntelligencePolls = new Set();

function indiaCalendarDate(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(date.getTime())) return null;
    const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Kolkata",
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
    }).formatToParts(date);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
}

function isWithinThreeIndiaCalendarDays(publishedAt) {
    const publishedDate = indiaCalendarDate(publishedAt);
    const todayDate = indiaCalendarDate(new Date());
    if (!publishedDate || !todayDate) return false;

    const [year, month, day] = todayDate.split("-").map(Number);
    const earliestDate = new Date(Date.UTC(year, month - 1, day - 2))
        .toISOString()
        .slice(0, 10);
    return publishedDate >= earliestDate && publishedDate <= todayDate;
}

function allArticlesAreCurrent(articles) {
    return Array.isArray(articles) &&
        articles.every((article) => isWithinThreeIndiaCalendarDays(article.published_at));
}

function hasFullLatestDescription(article) {
    const description = String(article?.description || "").trim();
    return description.length >= 320 && description.split(/\s+/).length >= 20;
}

function getFrontendCategoryCache(category) {

    let cached = currentCategoryCache[category];

    if (!cached) {
        try {
            const stored = localStorage.getItem(
                CATEGORY_CACHE_STORAGE_PREFIX + category
            );
            if (stored) {
                const parsed = JSON.parse(stored);
                if (
                    parsed &&
                    Number.isFinite(parsed.timestamp) &&
                    Array.isArray(parsed.articles)
                ) {
                    cached = parsed;
                    currentCategoryCache[category] = parsed;
                }
            }
        } catch (error) {
            console.debug("Category cache could not be read:", category, error);
        }
    }

    if (!cached) {
        return null;
    }

    if (category === "jobs" && cached.jobsScopeVersion !== 1) {
        delete currentCategoryCache[category];
        try {
            localStorage.removeItem(CATEGORY_CACHE_STORAGE_PREFIX + category);
        } catch (error) {
            // Ignore unavailable browser storage.
        }
        return null;
    }

    // Never reuse a short Latest teaser from browser storage. The API applies
    // the same rule, and this protects category switches and older snapshots.
    if (category === "latest" &&
        (!Array.isArray(cached.articles) || !cached.articles.every(hasFullLatestDescription))) {
        delete currentCategoryCache[category];
        try {
            localStorage.removeItem(CATEGORY_CACHE_STORAGE_PREFIX + category);
        } catch (error) {
            // Keep the page usable when browser storage is unavailable.
        }
        return null;
    }

    if (
        Date.now() - cached.timestamp
        > CATEGORY_CACHE_TTL
    ) {
        return null;
    }

    // Reuse only a complete, publisher-balanced response for every category.
    if (!isCompleteCategorySet(cached.articles, category) || !allArticlesAreCurrent(cached.articles)) {
        delete currentCategoryCache[category];
        try {
            localStorage.removeItem(CATEGORY_CACHE_STORAGE_PREFIX + category);
        } catch (error) {
            // Keep the in-memory cache usable when browser storage is unavailable.
        }
        return null;
    }

    return cached.articles;
}

function isCompleteCategorySet(articles, category) {
    if (!Array.isArray(articles) || articles.length !== 20) return false;

    const sources = new Set(
        articles.map((article) => String(article.source || "").trim()).filter(Boolean)
    );
    return sources.size >= 4 && sources.size <= 6;
}

function mergeUniqueArticles(primary = [], fallback = []) {
    const merged = [];
    const seen = new Set();
    const sourceCounts = new Map();

    for (const article of [...primary, ...fallback]) {
        const url = String(article.url || "").split("#", 1)[0].replace(/\/$/, "").toLowerCase();
        const title = String(article.title || "").trim().toLowerCase();
        const key = url || title;
        const source = String(article.source || "Unknown source").trim();
        if (!sourceCounts.has(source) && sourceCounts.size >= 6) continue;
        if ((sourceCounts.get(source) || 0) >= 6) continue;
        if (!key || seen.has(key)) continue;
        seen.add(key);
        merged.push(article);
        sourceCounts.set(source, (sourceCounts.get(source) || 0) + 1);
        if (merged.length === 20) break;
    }

    return merged;
}

function getFreshStaleCategoryCache(category, cachedEntry) {
    if (
        !cachedEntry ||
        Date.now() - cachedEntry.timestamp <= CATEGORY_CACHE_TTL
    ) {
        return null;
    }

    const articles = cachedEntry.articles;
    if (
        !Array.isArray(articles) ||
        articles.length === 0
    ) {
        return null;
    }

    return allArticlesAreCurrent(articles) ? articles : null;
}

function saveFrontendCategoryCache(
    category,
    articles
) {
    if (!isCompleteCategorySet(articles, category) || !allArticlesAreCurrent(articles)) {
        delete currentCategoryCache[category];
        try {
            localStorage.removeItem(CATEGORY_CACHE_STORAGE_PREFIX + category);
        } catch (error) {
            // Ignore unavailable browser storage.
        }
        return;
    }

    const cacheEntry = {
        timestamp: Date.now(),
        articles: [...articles],
        ...(category === "jobs" ? { jobsScopeVersion: 1 } : {})
    };
    currentCategoryCache[category] = cacheEntry;

    const persistCache = function () {
        if (currentCategoryCache[category] !== cacheEntry) return;
        try {
            localStorage.setItem(
                CATEGORY_CACHE_STORAGE_PREFIX + category,
                JSON.stringify(cacheEntry)
            );
        } catch (error) {
            // The in-memory cache still works if storage is full or blocked.
            console.debug("Category cache could not be saved:", category, error);
        }
    };

    if (typeof window.requestIdleCallback === "function") {
        window.requestIdleCallback(persistCache, { timeout: 1200 });
    } else {
        window.setTimeout(persistCache, 0);
    }
}

function clearFrontendCategoryCache(
    category
) {
    if (category) {
        delete currentCategoryCache[category];
        try {
            localStorage.removeItem(CATEGORY_CACHE_STORAGE_PREFIX + category);
        } catch (error) {
            // Ignore unavailable browser storage.
        }
    } else {
        currentCategoryCache = {};
        try {
            Object.keys(localStorage)
                .filter((key) => key.startsWith(CATEGORY_CACHE_STORAGE_PREFIX))
                .forEach((key) => localStorage.removeItem(key));
        } catch (error) {
            // Ignore unavailable browser storage.
        }
    }
}

// ============================================================
// STAGE 5 PERFORMANCE CACHE
// ============================================================

let currentSearchQuery = "";

let newsContainer = null;
let searchInput = null;

// ============================================================
// CATEGORY MAP
// ============================================================
const CATEGORY_MAP = {
    latest: "latest",
    india: "india",
    telangana: "telangana",
    andhrapradesh: "andhrapradesh",
    karnataka: "karnataka",
    tamilnadu: "tamilnadu",
    kerala: "kerala",
    maharashtra: "maharashtra",
    delhi: "delhi",
    world: "world",
    ai: "ai",
    technology: "technology",
    business: "business",
    stocks: "stocks",
    startups: "startups",
    weather: "weather",
    sports: "sports",
    entertainment: "entertainment",
    jobs: "jobs",
    saved: "saved",
};
// ============================================================
// PAGE START
// ============================================================

document.addEventListener("DOMContentLoaded", function () {

    newsContainer = document.querySelector("#news-container");

    searchInput =
        document.querySelector("#search-input") ||
        document.querySelector("#search") ||
        document.querySelector('input[type="search"]');

    if (!newsContainer) {
        console.error("News container #news-container was not found.");
        return;
    }

    setupCategoryNavigation();
    setupSearch();

    if (typeof updateHeadlineTicker === "function") {
        updateHeadlineTicker();
    }

    loadNews("latest");

    console.log("THE NEWSROOM frontend loaded.");
    console.log("Backend:", API_BASE_URL);
});

// ============================================================
// CATEGORY NAVIGATION
// ============================================================

function setupCategoryNavigation() {

    const categoryElements =
        document.querySelectorAll(
            "[data-category], .category, .nav-category"
        );

    categoryElements.forEach(function (element) {

        let hoverPrefetchTimer = null;
        const warmCategory = function () {
            const candidate = normalizeCategory(
                element.dataset.category ||
                element.getAttribute("data-category") ||
                element.textContent.trim()
            );
            if (candidate && candidate !== "saved") {
                prefetchCategory(candidate);
            }
        };
        element.addEventListener("pointerenter", function (event) {
            if (event.pointerType === "touch") return;
            clearTimeout(hoverPrefetchTimer);
            hoverPrefetchTimer = setTimeout(warmCategory, 180);
        }, { passive: true });
        element.addEventListener("pointerleave", function () {
            clearTimeout(hoverPrefetchTimer);
        }, { passive: true });
        element.addEventListener("pointerdown", function () {
            clearTimeout(hoverPrefetchTimer);
            warmCategory();
        }, { passive: true });
        element.addEventListener("focus", warmCategory);

        element.addEventListener("click", function (event) {

            event.preventDefault();

            let category =
                element.dataset.category ||
                element.getAttribute("data-category") ||
                element.textContent.trim();

            category = normalizeCategory(category);

            if (category === "saved") {
                if (searchInput) {
                    searchInput.value = "";
                }
                currentSearchQuery = "";
                loadSavedArticles();
                return;
            }

            if (!CATEGORY_MAP[category]) {
                console.warn("Unknown category:", category);
                return;
            }

            if (searchInput) {
                searchInput.value = "";
            }

            currentSearchQuery = "";

            loadNews(category);
        });
    });
}

// ============================================================
// NORMALIZE CATEGORY
// ============================================================

function normalizeCategory(category) {

    return String(category || "")
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]/g, "");
}

// ============================================================
// SEARCH
// ============================================================

function setupSearch() {

    if (!searchInput) {
        console.warn("Search input was not found.");
        return;
    }

    let searchTimer = null;
    let searchRequestId = 0;
    let activeSearchController = null;

    searchInput.addEventListener("input", function () {

        const query =
            searchInput.value
                .trim();

        const requestId = ++searchRequestId;

        currentSearchQuery = query;

        clearTimeout(searchTimer);
        if (activeSearchController) {
            activeSearchController.abort();
            activeSearchController = null;
        }

        if (!query) {

            currentDisplayedArticles =
                [...allArticles];

            renderNews(
                currentDisplayedArticles,
                ""
            );

            return;
        }

        searchTimer = setTimeout(
            async function () {

                try {

                    console.log(
                        "SEARCHING BACKEND:",
                        query
                    );

                    const controller = new AbortController();
                    activeSearchController = controller;
                    const response =
                        await fetch(
                            `${API_BASE_URL}/search?q=${encodeURIComponent(query)}`,
                            {
                                method: "GET",
                                cache: "no-store",
                                signal: controller.signal
                            }
                        );

                    if (!response.ok) {
                        throw new Error(
                            `Search request failed: ${response.status}`
                        );
                    }

                    const data =
                        await response.json();

                    // Typing a newer query or clearing the field makes this
                    // response obsolete. Never let it replace current results.
                    if (
                        requestId !== searchRequestId ||
                        searchInput.value.trim() !== query
                    ) {
                        return;
                    }

                    console.log(
                        "SEARCH API RESPONSE:",
                        data
                    );

                    if (
                        !data ||
                        !Array.isArray(data.articles)
                    ) {
                        throw new Error(
                            "Invalid search response."
                        );
                    }

                    const results =
                        data.articles.map(function (article) {

                            return {
                                title:
                                    article.title ||
                                    "Untitled article",

                                description:
                                    article.description ||
                                    "",

                                url:
                                    article.url ||
                                    "",

                                source:
                                    article.source ||
                                    "Unknown source",

                                published_at:
                                    article.published_at ||
                                    ""
                            };
                        });

                    currentDisplayedArticles =
                        results;

                    renderNews(
                        results,
                        query
                    );

                } catch (error) {

                    if (error && error.name === "AbortError") return;

                    if (
                        requestId !== searchRequestId ||
                        searchInput.value.trim() !== query
                    ) {
                        return;
                    }

                    console.error(
                        "BACKEND SEARCH ERROR:",
                        error
                    );

                    // Fallback to local search.
                    filterArticles(query);
                }

            },
            220
        );
    });
}


// ============================================================
// FILTER ARTICLES
// ============================================================

function filterArticles(query) {

    const normalizedQuery =
        String(query || "")
            .toLowerCase()
            .trim();

    if (!normalizedQuery) {

        currentDisplayedArticles =
            [...allArticles];

        renderNews(
            currentDisplayedArticles,
            ""
        );

        return;
    }

    const words =
        normalizedQuery
            .split(/\s+/)
            .filter(Boolean);

    const filtered =
        allArticles.filter(function (article) {

            const title =
                cleanArticleTitle(
                    article.title
                ).toLowerCase();

            const description =
                cleanArticleDescription(
                    article.description
                ).toLowerCase();

            const source =
                String(
                    article.source || ""
                ).toLowerCase();

            const combined =
                `${title} ${description} ${source}`;

            return words.every(function (word) {
                return combined.includes(word);
            });
        });

    currentDisplayedArticles = filtered;

    renderNews(
        filtered,
        normalizedQuery
    );
}

// ============================================================
// STAGE 5 AUTO REFRESH
// ============================================================

let autoRefreshInProgress = false;
let autoRefreshTimer = null;

async function autoRefreshCurrentCategory() {

    if (document.visibilityState === "hidden") {
        return;
    }

    // Never start another refresh while one is running.
    if (autoRefreshInProgress) {
        return;
    }

    // Do not interrupt an active search.
    if (
        typeof currentSearchQuery !== "undefined" &&
        String(currentSearchQuery || "").trim()
    ) {
        return;
    }

    const categoryAtStart = currentCategory;

    if (!categoryAtStart) {
        return;
    }

    autoRefreshInProgress = true;

    try {

        const backendCategory =
            CATEGORY_MAP[categoryAtStart];

        if (!backendCategory) {
            return;
        }

        const url =
            `${API_BASE_URL}/news?category=${encodeURIComponent(
                backendCategory
            )}&refresh=1`;

        const data = await getCategoryData(
            categoryAtStart,
            url
        );

        if (
            !data ||
            !Array.isArray(data.articles)
        ) {
            throw new Error(
                "Invalid auto-refresh response."
            );
        }

        // User may have changed category while the request was running.
        if (currentCategory !== categoryAtStart) {
            return;
        }

        const refreshedArticles =
            data.articles.map(function (article) {

                return {
                    title:
                        article.title ||
                        "Untitled article",

                    description:
                        article.description ||
                        "",

                    url:
                        article.url ||
                        "",

                    source:
                        article.source ||
                        "Unknown source",

                    published_at:
                        article.published_at ||
                        ""
                };

            });

        // Remove duplicate articles by URL/title.
        const seen = new Set();

        const uniqueArticles =
            refreshedArticles.filter(function (article) {

                const key =
                    String(
                        article.url ||
                        article.title ||
                        ""
                    )
                    .trim()
                    .toLowerCase();

                if (!key || seen.has(key)) {
                    return false;
                }

                seen.add(key);
                return true;

            });

        allArticles = uniqueArticles.slice(0, 20);

        currentDisplayedArticles =
            [...allArticles];

        // Fresh auto-refresh data replaces the old cache.
        saveFrontendCategoryCache(
            categoryAtStart,
            allArticles
        );

        // Refresh the visible article list without showing
        // the full loading screen.
        renderNews(
            currentDisplayedArticles,
            ""
        );

        console.log(
            "AUTO-REFRESH:",
            categoryAtStart,
            "articles:",
            uniqueArticles.length
        );

    } catch (error) {

        console.error(
            "AUTO-REFRESH ERROR:",
            error
        );

        // Keep the existing news on screen if refresh fails.

    } finally {

        autoRefreshInProgress = false;
    }
}

function startAutoRefresh() {

    if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
    }

    // Refresh the selected category every 5 minutes.
    autoRefreshTimer =
        setInterval(
            autoRefreshCurrentCategory,
            5 * 60 * 1000
        );

    console.log(
        "STAGE 5 AUTO REFRESH ENABLED: every 5 minutes"
    );
}

startAutoRefresh();

function refreshAfterIndiaDateChange() {
    if (document.visibilityState === "hidden") return;

    const currentIndiaDate = indiaCalendarDate(new Date());
    if (!currentIndiaDate || currentIndiaDate === lastObservedIndiaDate) {
        return;
    }

    lastObservedIndiaDate = currentIndiaDate;

    // A new India calendar day invalidates browser snapshots for all news
    // categories, so a category opened later cannot reuse yesterday's set.
    Object.keys(CATEGORY_MAP).forEach(function (category) {
        delete currentCategoryCache[category];
        try {
            localStorage.removeItem(CATEGORY_CACHE_STORAGE_PREFIX + category);
        } catch (error) {
            // The live request still bypasses local storage when unavailable.
        }
    });

    console.info("India date changed; requesting fresh news:", currentIndiaDate);
    if (currentCategory !== "saved") {
        loadNews(currentCategory, true);
    }
}

// Check often enough to catch midnight, and check again when a suspended tab
// becomes visible or active so background timer throttling cannot miss it.
window.setInterval(refreshAfterIndiaDateChange, 30 * 1000);
window.addEventListener("focus", refreshAfterIndiaDateChange);
document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
        refreshAfterIndiaDateChange();
        if (typeof updateHeadlineTicker === "function") {
            updateHeadlineTicker();
        }
    }
});


// ============================================================
// LOAD NEWS
// ============================================================

// STAGE 7 — IN-FLIGHT REQUEST DEDUPLICATION
const categoryRequestsInFlight = {};
let categoryWarmupStarted = false;

function scheduleCategoryWarmup() {
    if (categoryWarmupStarted) return;
    categoryWarmupStarted = true;

    const categories = Object.keys(CATEGORY_MAP).filter(function (category) {
        return category !== "saved" && category !== "jobs" && category !== currentCategory;
    });

    const warmNextCategory = function () {
        if (categories.length === 0) return;
        if (document.hidden) {
            window.setTimeout(warmNextCategory, 5000);
            return;
        }

        const category = categories.shift();
        prefetchCategory(category).finally(function () {
            if (categories.length === 0) return;
            if (typeof window.requestIdleCallback === "function") {
                window.requestIdleCallback(warmNextCategory, { timeout: 1500 });
            } else {
                window.setTimeout(warmNextCategory, 350);
            }
        });
    };

    if (typeof window.requestIdleCallback === "function") {
        window.requestIdleCallback(warmNextCategory, { timeout: 2000 });
    } else {
        window.setTimeout(warmNextCategory, 800);
    }
}

async function prefetchCategory(category) {
    category = normalizeCategory(category);
    if (
        !CATEGORY_MAP[category] ||
        category === "saved" ||
        category === "jobs" ||
        getFrontendCategoryCache(category)
    ) {
        return;
    }

    const url = `${API_BASE_URL}/news?category=${encodeURIComponent(CATEGORY_MAP[category])}`;
    try {
        const data = await getCategoryData(category, url);
        if (!data || !Array.isArray(data.articles) || data.articles.length === 0) {
            return;
        }
        const articles = data.articles.slice(0, 20).map(function (article) {
            return {
                title: article.title || "Untitled article",
                description: article.description || "",
                url: article.url || "",
                source: article.source || "Unknown source",
                published_at: article.published_at || ""
            };
        });
        saveFrontendCategoryCache(category, articles);
    } catch (error) {
        // Prefetch is opportunistic; the normal category click handles errors.
        console.debug("Category prefetch skipped:", category, error);
    }
}

function getCategoryData(category, url, forceFresh = false) {
    const requestKey = forceFresh
        ? `${category}:fresh:${++freshCategoryRequestSequence}`
        : category;

    if (!categoryRequestsInFlight[requestKey]) {
        const requestPromise = fetch(url, {
            method: "GET",
            cache: "no-store"
        }).then(function(response) {
            if (!response.ok) {
                throw new Error(
                    `News request failed: ${response.status}`
                );
            }
            return response.json();
        });

        categoryRequestsInFlight[requestKey] = requestPromise;
        const clearRequest = function() {
            if (categoryRequestsInFlight[requestKey] === requestPromise) {
                delete categoryRequestsInFlight[requestKey];
            }
        };
        requestPromise.then(clearRequest, clearRequest);
    }

    return categoryRequestsInFlight[requestKey];
}

async function pollJobsIntelligence(statusKey) {
    if (!statusKey || jobsIntelligencePolls.has(statusKey)) return;
    jobsIntelligencePolls.add(statusKey);

    try {
        for (let attempt = 0; attempt < 60; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 2000));
            const response = await fetch(
                `${API_BASE_URL}/jobs-intelligence?key=${encodeURIComponent(statusKey)}`,
                { cache: "no-store" }
            );
            if (!response.ok) continue;

            const state = await response.json();
            if (state.status === "complete" && state.result) {
                window.jobsIntelligence = state.result;
                if (currentCategory === "jobs" && !currentSearchQuery) {
                    renderNews(currentDisplayedArticles, "");
                }
                return;
            }
            if (state.status === "failed") return;
        }
    } catch (error) {
        console.warn("Jobs intelligence status check failed:", error);
    } finally {
        jobsIntelligencePolls.delete(statusKey);
    }
}

async function loadNews(category, forceFresh = false) {

    const loadId = ++categoryLoadSequence;
    // Reuse validated browser and backend caches on first load. The cached
    // entries already enforce age, article-count, and source-diversity rules;
    // explicit refreshes and the periodic refresh still request fresh feeds.
    const forceFreshRequest = forceFresh;

    category =
        normalizeCategory(category);

    if (category === "saved") {
        newsContainer.dataset.category = "saved";
        loadSavedArticles();
        return;
    }

    if (!CATEGORY_MAP[category]) {
        category = "latest";
    }

    currentCategory = category;
    newsContainer.dataset.category = category;

    updateActiveCategory(category);

    // STAGE 5 PERFORMANCE CACHE
    const cachedArticles =
        getFrontendCategoryCache(category);
    const cachedSnapshot = currentCategoryCache[category];

    if (cachedArticles && !forceFreshRequest) {

        allArticles =
            [...cachedArticles];

        currentDisplayedArticles =
            [...allArticles];

        currentSearchQuery = "";

        if (searchInput) {
            searchInput.value = "";
        }

        scheduleCategoryWarmup();

        renderNews(
            currentDisplayedArticles,
            ""
        );

        console.log(
            "FRONTEND CACHE HIT:",
            category,
            "articles:",
            allArticles.length
        );

        return;
    }

    // A full-page load must show only the result of its forced live request.
    // Otherwise an old browser snapshot can make the refresh look ineffective.
    const staleArticles = forceFreshRequest
        ? null
        : cachedArticles || getFreshStaleCategoryCache(category, cachedSnapshot);
    const showingStaleCache = Boolean(staleArticles);

    if (showingStaleCache) {
        allArticles = [...staleArticles];
        currentDisplayedArticles = [...allArticles];
        currentSearchQuery = "";
        if (searchInput) {
            searchInput.value = "";
        }
        renderNews(currentDisplayedArticles, "");
    } else {
        showLoading();
    }

    const backendCategory =
        CATEGORY_MAP[category];

    console.log(
        "Loading category:",
        backendCategory
    );

    try {

        const refreshParameter = forceFreshRequest ? "&refresh=1" : "";
        const url =
            `${API_BASE_URL}/news?category=${encodeURIComponent(
                backendCategory
            )}${refreshParameter}`;

        const data = await getCategoryData(category, url, forceFreshRequest);

        if (
            loadId !== categoryLoadSequence ||
            currentCategory !== category
        ) {
            return;
        }

        console.log(
            "NEWS API RESPONSE:",
            data
        );

        if (!data || !Array.isArray(data.articles)) {
            throw new Error(
                "Backend returned an invalid news response."
            );
        }

        const freshArticles =
            data.articles.slice(0, 20).map(function (article) {

                return {
                    title:
                        article.title || "Untitled article",

                    description:
                        article.description || "",

                    url:
                        article.url || "",

                    source:
                        article.source || "Unknown source",

                    published_at:
                        article.published_at || ""
                };
            }).filter(function (article) {
                return category !== "latest" || hasFullLatestDescription(article);
            });

        allArticles = forceFreshRequest || category === "latest"
            ? freshArticles
            : mergeUniqueArticles(
                freshArticles,
                getFreshStaleCategoryCache(category, cachedSnapshot) || []
            );

        if (!isCompleteCategorySet(allArticles, category)) {
            allArticles = freshArticles;
        }

        currentDisplayedArticles =
            [...allArticles];

        // STAGE 7 PERFORMANCE CACHE
        // Store fresh API results for fast category switching.
        saveFrontendCategoryCache(
            category,
            allArticles
        );

        // Store Jobs & Career Intelligence returned by backend.
        window.jobsIntelligence =
            data.jobs_intelligence || null;

        if (
            data.jobs_intelligence_pending &&
            data.jobs_intelligence_key
        ) {
            pollJobsIntelligence(data.jobs_intelligence_key);
        }

        currentSearchQuery = "";

        if (searchInput) {
            searchInput.value = "";
        }

        renderNews(
            currentDisplayedArticles,
            ""
        );

        scheduleCategoryWarmup();

    } catch (error) {

        if (
            loadId !== categoryLoadSequence ||
            currentCategory !== category
        ) {
            return;
        }

        if (showingStaleCache) {
            console.warn(
                "Category refresh failed; keeping recent cached articles:",
                error
            );
            return;
        }

        console.error(
            "Unable to load news:",
            error
        );

        if (!newsContainer) {
            return;
        }

        newsContainer.innerHTML = `
            <div class="news-error">
                <h2>Unable to load news</h2>
                <p>
                    Make sure the Newsroom backend is running
                    on port 8000.
                </p>
                <p>
                    ${escapeHTML(error.message)}
                </p>
                <button
                    class="retry-button"
                    onclick="loadNews('${escapeHTML(category)}')"
                >
                    Try Again
                </button>
            </div>
        `;
    }
}

// ============================================================
// LOADING
// ============================================================

function showLoading() {

    if (!newsContainer) {
        return;
    }

    showNewsLoadingSkeleton(6);
}

// ============================================================
// RENDER NEWS
// ============================================================


// STAGE 6 — NEWS LOADING SKELETON
function showNewsLoadingSkeleton(count = 6) {

    if (!newsContainer) {
        return;
    }

    const cards = Array.from(
        { length: count },
        function () {
            return `
                <article class="news-skeleton-card">
                    <div class="news-skeleton-line title"></div>
                    <div class="news-skeleton-line medium"></div>
                    <div class="news-skeleton-line"></div>
                    <div class="news-skeleton-line short"></div>
                    <div class="news-skeleton-block"></div>
                    <div class="news-skeleton-line medium"></div>
                    <div class="news-skeleton-line short"></div>
                </article>
            `;
        }
    ).join("");

    newsContainer.innerHTML = `
        <div class="news-skeleton-list" aria-label="Loading news">
            ${cards}
        </div>
    `;
}

function renderNews(articles, searchQuery) {

    if (!newsContainer) {
        return;
    }

    if (!Array.isArray(articles)) {
        articles = [];
    }

    articles = articles.slice(0, 20);

    if (articles.length === 0) {

        const message =
            searchQuery
                ? `Search results for "${searchQuery}"`
                : `${labelFor(currentCategory)} News`;

        newsContainer.innerHTML = `
            <div class="no-news">
                <h2>No news found</h2>
                <p>
                    ${
                        searchQuery
                            ? `No articles matched "${escapeHTML(searchQuery)}".`
                            : "Try another category or search term."
                    }
                </p>
            </div>
        `;

        return;
    }

    let headingHTML = "";

    if (searchQuery) {

        headingHTML = `
            <div class="results-heading">
                <h2>
                    Search results for
                    "${escapeHTML(searchQuery)}"
                </h2>
            </div>
        `;

    } else {

        headingHTML = `
            <div class="results-heading">
                <h2>
                    ${escapeHTML(
                        labelFor(currentCategory)
                    )}
                </h2>
            </div>
        `;
    }

    const cards =
        articles.map(function (article, index) {

            return articleCard(
                article,
                index
            );

        }).join("");

    let intelligenceHTML = "";

    if (
        currentCategory === "jobs" &&
        !searchQuery &&
        window.jobsIntelligence
    ) {
        intelligenceHTML =
            renderJobsIntelligence(
                window.jobsIntelligence
            );
    }

    newsContainer.innerHTML =
        headingHTML +
        intelligenceHTML +
        `<div class="news-list">${cards}</div>` +
        `<div class="category-page-actions">
            <button class="category-back-to-top" type="button" aria-label="Back to top" title="Back to top" onclick="scrollToPageTop()">↑</button>
        </div>`;

    fitRenderedArticleDescriptions(newsContainer);
}


// ============================================================
// JOBS & CAREER INTELLIGENCE
// ============================================================

function renderJobsIntelligence(data) {

    if (!data) {
        return "";
    }

    function safeArray(value) {
        return Array.isArray(value) ? value : [];
    }

    function renderOpportunityList(items, type) {

        items = safeArray(items);

        if (items.length === 0) {
            return `
                <div class="jobs-empty">
                    No current opportunities found.
                </div>
            `;
        }

        return items.map(function (item) {

            const title =
                escapeHTML(
                    item.title || "Opportunity"
                );

            const organization =
                escapeHTML(
                    item.organization ||
                    item.company ||
                    ""
                );

            const details =
                escapeHTML(
                    item.details || ""
                );

            const source =
                escapeHTML(
                    item.source || ""
                );

            return `
                <div class="jobs-intel-item">

                    <h4>${title}</h4>

                    ${
                        organization
                            ? `<div class="jobs-intel-organization">
                                   ${organization}
                               </div>`
                            : ""
                    }

                    ${
                        details
                            ? `<p>${details}</p>`
                            : ""
                    }

                    ${
                        source
                            ? `<span class="jobs-intel-source">
                                   Source: ${source}
                               </span>`
                            : ""
                    }

                </div>
            `;

        }).join("");
    }


    function renderFutureMarket(items) {

        items = safeArray(items);

        if (items.length === 0) {
            return `
                <div class="jobs-empty">
                    No future market trends found.
                </div>
            `;
        }

        return items.map(function (item) {

            const area =
                escapeHTML(
                    item.area || "Career Area"
                );

            const trend =
                escapeHTML(
                    item.trend || ""
                );

            const roles =
                safeArray(
                    item.potential_roles
                );

            return `
                <div class="jobs-intel-item">

                    <h4>${area}</h4>

                    ${
                        trend
                            ? `<p>${trend}</p>`
                            : ""
                    }

                    ${
                        roles.length
                            ? `
                                <div class="jobs-role-list">
                                    ${roles.map(function (role) {
                                        return `
                                            <span class="jobs-role">
                                                ${escapeHTML(role)}
                                            </span>
                                        `;
                                    }).join("")}
                                </div>
                              `
                            : ""
                    }

                    ${
                        item.source
                            ? `<span class="jobs-intel-source">
                                   Source: ${escapeHTML(item.source)}
                               </span>`
                            : ""
                    }

                </div>
            `;

        }).join("");
    }

    function renderGovernmentOpportunities(items) {
        items = safeArray(items);
        const stateItems = items.filter((item) => String(item.scope || "").toLowerCase() === "state");
        const centralItems = items.filter((item) => String(item.scope || "").toLowerCase() === "central");
        const generalItems = items.filter((item) => !["state", "central"].includes(String(item.scope || "").toLowerCase()));

        return `
            <div class="jobs-government-group">
                <h4>State Government Openings</h4>
                ${renderOpportunityList(stateItems, "government")}
            </div>
            <div class="jobs-government-group">
                <h4>Central Government Openings</h4>
                ${renderOpportunityList(centralItems, "government")}
            </div>
            ${generalItems.length ? `
                <div class="jobs-government-group">
                    <h4>Other Government Openings</h4>
                    ${renderOpportunityList(generalItems, "government")}
                </div>
            ` : ""}
        `;
    }


    function renderSkills(items) {

        items = safeArray(items);

        if (items.length === 0) {
            return `
                <div class="jobs-empty">
                    No skills identified yet.
                </div>
            `;
        }

        return items.map(function (item) {

            const skill =
                escapeHTML(
                    item.skill || "Skill"
                );

            const why =
                escapeHTML(
                    item.why_it_matters ||
                    item.why ||
                    ""
                );

            const roles =
                safeArray(
                    item.related_roles
                );

            return `
                <div class="jobs-intel-item">

                    <h4>${skill}</h4>

                    ${
                        why
                            ? `<p>${why}</p>`
                            : ""
                    }

                    ${
                        roles.length
                            ? `
                                <div class="jobs-role-list">
                                    ${roles.map(function (role) {
                                        return `
                                            <span class="jobs-role">
                                                ${escapeHTML(role)}
                                            </span>
                                        `;
                                    }).join("")}
                                </div>
                              `
                            : ""
                    }

                </div>
            `;

        }).join("");
    }


    return `
        <section class="jobs-intelligence">

            <div class="jobs-intelligence-header">
                <div>
                    <span class="jobs-intelligence-label">
                        AI CAREER INTELLIGENCE
                    </span>

                    <h2>
                        Jobs & Career Opportunities
                    </h2>

                    <p>
                        AI-powered insights from the latest
                        jobs and employment news.
                    </p>
                </div>
            </div>


            <div class="jobs-intelligence-grid">

                <div class="jobs-intel-card government">
                    <div class="jobs-intel-card-header">
                        <span class="jobs-intel-icon">🏛️</span>
                        <h3>Government Opportunities</h3>
                    </div>

                    ${renderGovernmentOpportunities(
                        data.government_opportunities,
                        "government"
                    )}
                </div>


                <div class="jobs-intel-card private">
                    <div class="jobs-intel-card-header">
                        <span class="jobs-intel-icon">💼</span>
                        <h3>Private-Sector Openings Worldwide</h3>
                    </div>

                    ${renderOpportunityList(
                        data.private_sector_opportunities,
                        "private"
                    )}
                </div>


                <div class="jobs-intel-card internships">
                    <div class="jobs-intel-card-header">
                        <span class="jobs-intel-icon">🎓</span>
                        <h3>Internships & Freshers</h3>
                    </div>

                    ${renderOpportunityList(
                        data.internships_freshers,
                        "internships"
                    )}
                </div>


                <div class="jobs-intel-card future">
                    <div class="jobs-intel-card-header">
                        <span class="jobs-intel-icon">📈</span>
                        <h3>Future Job Market</h3>
                    </div>

                    ${renderFutureMarket(
                        data.future_job_market
                    )}
                </div>


                <div class="jobs-intel-card skills">
                    <div class="jobs-intel-card-header">
                        <span class="jobs-intel-icon">🧠</span>
                        <h3>Skills to Learn</h3>
                    </div>

                    ${renderSkills(
                        data.skills_to_learn
                    )}
                </div>

            </div>

        </section>
    `;
}


// ============================================================
// ARTICLE CARD

// ============================================================

function formatSentiment(sentiment) {

    const value =
        String(sentiment || "NEUTRAL")
            .trim()
            .toUpperCase();

    if (value === "POSITIVE") {
        return "😊 Positive";
    }

    if (value === "NEGATIVE") {
        return "😟 Negative";
    }

    return "😐 Neutral";
}


function articleCard(article, index, savedMode = false) {

    const title =
        cleanArticleTitle(
            article.title
        );

    const fullDescription =
        cleanArticleDescription(
            article.description
        );

    const description = fullDescription;

    const source =
        article.source ||
        "Unknown source";

    const date =
        formatDate(
            article.published_at
        );

    const url =
        safeUrl(article.url);

    return `
        <article
            class="news-card"
            data-index="${index}"
        >

            <div class="article-card-top">
                <div class="article-meta">
                    <strong>
                        ${escapeHTML(source)}
                    </strong>

                    ${
                        date
                            ? `<span>•</span>
                               <span>${escapeHTML(date)}</span>`
                            : ""
                    }
                </div>

                <div class="article-translate-control">
                    <select
                        class="article-translate-select"
                        aria-label="Translate article"
                        onchange="translateArticle(this)"
                    >
                        <option value="" selected>Translate</option>
                        <option value="te">తెలుగు</option>
                        <option value="hi">हिन्दी</option>
                        <option value="en">English</option>
                    </select>
                    <span class="article-translate-status" aria-live="polite"></span>
                </div>
            </div>

            <h2
                class="article-title"
                data-original-title="${escapeHTML(title)}"
            >
                ${escapeHTML(title)}
            </h2>

            ${description ? `
                <p
                    class="article-description"
                    data-full-description="${escapeHTML(fullDescription)}"
                    data-original-description="${escapeHTML(fullDescription)}"
                >
                    ${escapeHTML(description)}
                </p>
            ` : ""}

            <div class="article-actions">

                ${
                    url
                        ? `
                            <a
                                class="article-button primary"
                                href="${escapeHTML(url)}"
                                target="_blank"
                                rel="noopener noreferrer"
                            >
                                Open original
                            </a>
                        `
                        : `
                            <button
                                class="article-button primary"
                                disabled
                            >
                                Original unavailable
                            </button>
                        `
                }

                <button
                    class="article-button ai-button"
                    onclick="summarizeArticle(${index}, this)"
                >
                    Analyze with AI
                </button>

                <button
                    class="article-button eli10-button"
                    onclick="explainArticleELI10(${index}, this)"
                >
                    🧒 Explain Like I'm 10
                </button>

                ${
                    savedMode
                        ? `
                            <button
                                class="article-button save-button"
                                onclick="removeSavedArticle(${index})"
                            >
                                Remove Saved
                            </button>
                        `
                        : `
                            <button
                                class="article-button save-button"
                                onclick="saveArticle(${index})"
                            >
                                Save
                            </button>
                        `
                }

            </div>

            <div
                id="ai-result-${index}"
                class="ai-result"
            ></div>

        </article>
    `;
}

async function translateArticle(selectElement) {
    const card = selectElement.closest(".news-card");
    const titleElement = card?.querySelector(".article-title");
    const descriptionElement = card?.querySelector(".article-description");
    const statusElement = card?.querySelector(".article-translate-status");

    if (!card || !titleElement || !statusElement) return;

    const originalTitle = titleElement.dataset.originalTitle || titleElement.textContent.trim();
    const originalDescription = descriptionElement?.dataset.originalDescription || "";
    const language = selectElement.value;

    if (!language) return;

    selectElement.disabled = true;
    statusElement.textContent = "Translating…";

    try {
        const response = await fetch(`${API_BASE_URL}/translate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                title: originalTitle,
                description: originalDescription,
                language
            })
        });
        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Translation could not be completed.");
        }

        titleElement.textContent = data.title || originalTitle;
        if (descriptionElement) {
            const translatedDescription = data.description || originalDescription;
            descriptionElement.textContent = translatedDescription;
            descriptionElement.dataset.fullDescription = translatedDescription;
        }
        statusElement.textContent = {
            te: "తెలుగులో",
            hi: "हिन्दी में",
            en: "In English"
        }[language] || "Translated";
    } catch (error) {
        console.error("ARTICLE TRANSLATION ERROR:", error);
        statusElement.textContent = error.message || "Translation unavailable";
        selectElement.value = "";
    } finally {
        selectElement.disabled = false;
    }
}

// ============================================================
// EXPLAIN LIKE I'M 10
// ============================================================

function splitELI10IntoTwoLines(explanation, article = null) {
    let text = String(explanation || "")
        .replace(/(?:^|\s)(?:[-*•]+|\d+[.)])\s*/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    if (!text) {
        text = [article?.title, article?.description]
            .map((value) => String(value || "").trim())
            .filter(Boolean)
            .join(" ")
            .replace(/\s+/g, " ")
            .trim();
    }
    if (!text) return ["", ""];

    const sentences = (text.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [text])
        .map((sentence) => sentence.replace(/^\s*(?:[-*•]+|\d+[.)])\s*/, "").trim())
        .filter(Boolean);
    if (sentences.length >= 2) {
        return [sentences[0], sentences[1]];
    }

    const words = (sentences[0] || text).split(" ");
    if (words.length < 2) return [text, ""];
    const splitAt = Math.ceil(words.length / 2);
    return [words.slice(0, splitAt).join(" "), words.slice(splitAt).join(" ")];
}

async function explainArticleELI10(index, buttonElement) {

    const article =
        currentDisplayedArticles[index];

    if (!article) {
        console.error(
            "Article not found:",
            index
        );
        return;
    }

    const resultContainer =
        document.querySelector(
            `#ai-result-${index}`
        );

    if (!resultContainer) {
        console.error(
            "AI result container not found."
        );
        return;
    }

    const actionKey = `eli10:${article.url || article.title}`;
    if (pendingArticleActions.has(actionKey)) {
        return;
    }
    pendingArticleActions.add(actionKey);
    const originalButtonText = buttonElement?.textContent;
    if (buttonElement) {
        buttonElement.disabled = true;
        buttonElement.textContent = "Explaining…";
    }

    resultContainer.innerHTML = `
        <div class="ai-loading">

            <div class="loading-spinner"></div>

            <h3>
                🧒 Explaining this article simply...
            </h3>

            <p>
                Powered locally by Ollama.
            </p>

        </div>
    `;

    try {

        const response =
            await fetch(
                `${API_BASE_URL}/explain-eli10`,
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        title:
                            article.title || "",

                        description:
                            article.description || ""
                    })
                }
            );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail ||
                "Could not generate explanation"
            );
        }

        const explanationLines = splitELI10IntoTwoLines(data.explanation || "", article);

        resultContainer.innerHTML = `
            <div class="eli10-result">

                <h3>
                    🧒 Explain Like I'm 10
                </h3>

                <p class="eli10-two-lines">
                    <span class="eli10-line">${escapeHTML(explanationLines[0])}</span>
                    <span class="eli10-line">${escapeHTML(explanationLines[1])}</span>
                </p>

            </div>
        `;

    } catch (error) {

        console.error(
            "ELI10 ERROR:",
            error
        );

        resultContainer.innerHTML = `
            <div class="ai-result-error">

                <h3>
                    ⚠️ Explanation unavailable
                </h3>

                <p>
                    ${escapeHTML(
                        error.message ||
                        "Please try again."
                    )}
                </p>

            </div>
        `;
    } finally {
        pendingArticleActions.delete(actionKey);
        if (buttonElement) {
            buttonElement.disabled = false;
            buttonElement.textContent = originalButtonText;
        }
    }
}


// ============================================================
// AI SUMMARY
// ============================================================

async function waitForArticleAnalysis(key, articleKey, index, resultContainer) {
    for (let attempt = 0; attempt < 100; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200));

        const displayedArticle = currentDisplayedArticles[index];
        const displayedKey = displayedArticle && (displayedArticle.url || displayedArticle.title);
        if (!resultContainer.isConnected || displayedKey !== articleKey) return;

        try {
            const response = await fetch(
                `${API_BASE_URL}/summarize/status?key=${encodeURIComponent(key)}`,
                { cache: "no-store" }
            );
            if (!response.ok) continue;

            const state = await response.json();
            if (state.status === "complete" && state.result) {
                renderAIAnalysis(
                    resultContainer,
                    normalizeAIResponse(state.result),
                    displayedArticle
                );
                return;
            }
            if (state.status === "failed") return;
        } catch (error) {
            console.debug("AI analysis is still processing:", error);
        }
    }
}

async function summarizeArticle(index, buttonElement) {

    const article =
        currentDisplayedArticles[index];

    if (!article) {
        console.error(
            "Article not found:",
            index
        );
        return;
    }

    const resultContainer =
        document.querySelector(
            `#ai-result-${index}`
        );

    if (!resultContainer) {
        console.error(
            "AI result container not found."
        );
        return;
    }

    const actionKey = `analyze:${article.url || article.title}`;
    if (pendingArticleActions.has(actionKey)) {
        return;
    }
    pendingArticleActions.add(actionKey);
    const originalButtonText = buttonElement?.textContent;
    if (buttonElement) {
        buttonElement.disabled = true;
        buttonElement.textContent = "Analyzing…";
    }

    resultContainer.innerHTML = `
        <div class="ai-loading">

            <div class="loading-spinner"></div>

            <h3>
                AI is analyzing this article...
            </h3>

            <p>
                Powered locally by Ollama.
            </p>

        </div>
    `;

    try {

        const title =
            cleanArticleTitle(
                article.title
            );

        const description =
            cleanArticleDescription(
                article.description
            );

        const url =
            `${API_BASE_URL}/summarize` +
            `?title=${encodeURIComponent(title)}` +
            `&description=${encodeURIComponent(description)}`;

        console.log(
            "Sending article to Ollama:",
            title
        );

        const response =
            await fetch(url, {
                method: "POST",
                cache: "no-store"
            });

        if (!response.ok) {

            let errorText =
                `HTTP ${response.status}`;

            try {

                const errorData =
                    await response.json();

                if (errorData.detail) {

                    if (
                        typeof errorData.detail === "string"
                    ) {
                        errorText =
                            errorData.detail;
                    } else {
                        errorText =
                            JSON.stringify(
                                errorData.detail,
                                null,
                                2
                            );
                    }
                }

            } catch (ignore) {}

            throw new Error(errorText);
        }

        const data =
            await response.json();

        console.log(
            "AI RESPONSE:",
            data
        );

        const analysis =
            normalizeAIResponse(data);

        renderAIAnalysis(
            resultContainer,
            analysis,
            article
        );

        if (data.pending && data.analysis_key) {
            const articleKey = article.url || article.title;
            await waitForArticleAnalysis(
                data.analysis_key,
                articleKey,
                index,
                resultContainer
            );
        }

    } catch (error) {

        console.error(
            "AI analysis failed:",
            error
        );

        resultContainer.innerHTML = `
            <div class="ai-error">

                <h3>
                    AI analysis failed
                </h3>

                <p>
                    ${escapeHTML(
                        error.message ||
                        "Unable to analyze this article."
                    )}
                </p>

                <button
                    class="article-button"
                    onclick="summarizeArticle(${index}, this)"
                >
                    Try Again
                </button>

            </div>
        `;
    } finally {
        pendingArticleActions.delete(actionKey);
        if (buttonElement) {
            buttonElement.disabled = false;
            buttonElement.textContent = originalButtonText;
        }
    }
}

// ============================================================
// NORMALIZE AI RESPONSE
// ============================================================

function normalizeAIResponse(data) {

    let analysis = data;

    // Backend may return:
    // { summary: { ... } }

    if (
        data &&
        data.summary &&
        typeof data.summary === "object" &&
        !Array.isArray(data.summary)
    ) {

        analysis =
            data.summary;
    }

    // Another possible response:
    // { result: { ... } }

    if (
        analysis &&
        analysis.result &&
        typeof analysis.result === "object"
    ) {

        analysis =
            analysis.result;
    }

    if (!analysis || typeof analysis !== "object") {

        return {
            summary:
                String(
                    analysis ||
                    "No summary available."
                )
        };
    }

    return analysis;
}

// ============================================================
// RENDER AI ANALYSIS
// ============================================================

function getArticleAnalysisFacts(article) {
    const text = [article?.title, article?.description]
        .map((value) => String(value || "").trim())
        .filter(Boolean)
        .join(" ");
    const candidates = [];
    for (const sentence of text.split(/(?<=[.!?])\s+/)) {
        candidates.push(sentence.trim());
        candidates.push(...sentence.split(/\s*[;:—–]\s*|,\s+|\s+(?:and|but|while)\s+/i));
    }

    const facts = [];
    for (const candidate of candidates) {
        const fact = candidate.replace(/\s+/g, " ").trim().replace(/^[,.;:—–\s]+|[,.;:—–\s]+$/g, "");
        if (fact.split(/\s+/).length < 3) continue;
        if (!facts.some((existing) => existing.toLowerCase() === fact.toLowerCase())) facts.push(fact);
        if (facts.length === 3) return facts;
    }

    const words = text.match(/\S+/g) || [];
    for (let part = 0; part < 3 && words.length; part += 1) {
        const start = Math.round(part * words.length / 3);
        const end = Math.round((part + 1) * words.length / 3);
        const excerpt = words.slice(start, end).join(" ").replace(/^[,.;:—–\s]+|[,.;:—–\s]+$/g, "");
        if (excerpt && !facts.some((existing) => existing.toLowerCase() === excerpt.toLowerCase())) facts.push(excerpt);
    }
    return facts.slice(0, 3);
}

function analysisPointsRepeat(first, second) {
    const stopWords = new Set(["the", "and", "for", "from", "with", "that", "this", "was", "were", "has", "have", "will", "are", "its", "their", "about", "into", "after", "before", "who", "what"]);
    const tokens = (value) => new Set(
        String(value || "").toLowerCase().match(/[a-z0-9]+/g)?.filter((word) => word.length > 2 && !stopWords.has(word)) || []
    );
    const left = tokens(first);
    const right = tokens(second);
    const shared = [...left].filter((word) => right.has(word)).length;
    const overlap = shared / Math.max(1, Math.min(left.size, right.size));
    return String(first).trim().toLowerCase() === String(second).trim().toLowerCase() || (shared >= 3 && overlap >= 0.65);
}

function getThreeAnalysisPoints(value, article) {
    const placeholders = /^(?:no .* identified|no additional information|no summary available|no key points|not stated in the article|ai analysis could not be generated|the article was received by the local newsroom|ollama was unable|the importance of this article depends|people, organizations, industries|further developments will depend)/i;
    const result = [];
    const addDistinct = (candidate) => {
        const point = String(candidate || "").trim();
        if (!point || placeholders.test(point)) return;
        if (!result.some((existing) => analysisPointsRepeat(point, existing))) result.push(point);
    };

    getArray(value).forEach(addDistinct);
    getArticleAnalysisFacts(article).forEach((fact) => {
        if (result.length < 3) addDistinct(fact);
    });
    if (result.length < 3) {
        getArticleAnalysisFacts(article).forEach((fact) => {
            if (result.length < 3 && !result.some((existing) => existing.toLowerCase() === fact.toLowerCase())) result.push(fact);
        });
    }
    return result.slice(0, 3);
}

function renderAIAnalysis(container, analysis, article = null) {

    // ========================================================
    // AI TOPICS
    // ========================================================

    const topics =
        getArray(
            analysis.topics
        )
        .filter(Boolean)
        .map(
            function (topic) {
                return String(topic).trim();
            }
        )
        .filter(Boolean)
        .slice(0, 5);

    const topicsHTML =
        topics.length
            ? `
                <div class="ai-section ai-topics-section">
                    <h4>🏷️ TOPICS</h4>
                    <div class="ai-topics">
                        ${topics
                            .map(
                                function (topic) {
                                    return `
                                        <span class="ai-topic">
                                            ${topic}
                                        </span>
                                    `;
                                }
                            )
                            .join("")}
                    </div>
                </div>
            `
            : "";


    const summary = getThreeAnalysisPoints(analysis.summary, article);

    const keyPoints = getThreeAnalysisPoints(analysis.key_points, article);

    const whyItMatters = getThreeAnalysisPoints(analysis.why_it_matters, article);

    const importanceScore =
        normalizeScore(
            analysis.importance_score
        );

    const importanceLevel =
        getText(
            analysis.importance_level,
            importanceScore >= 70
                ? "HIGH"
                : importanceScore >= 40
                    ? "MEDIUM"
                    : "LOW"
        );

    const sentiment =
        getText(
            analysis.sentiment,
            "NEUTRAL"
        );

    const whoIsAffected = getThreeAnalysisPoints(analysis.who_is_affected, article);

    const whatHappensNext = getThreeAnalysisPoints(analysis.what_happens_next, article);

container.innerHTML = `
        <section class="ai-analysis">

            <div class="ai-header">

                <div>
                    <div class="ai-eyebrow">
                        AI ANALYSIS
                    </div>

                    <h2>
                        Local Ollama Intelligence
                    </h2>
                </div>

                <div class="ollama-badge">
                    Powered by Ollama
                </div>

            </div>

            <div class="ai-score-grid">

                <div class="ai-score-card">

                    <h3>
                        Importance Score
                    </h3>

                    <div class="score-value">
                        ${importanceScore}/100
                    </div>

                </div>

                <div class="ai-score-card">

                    <h3>
                        Importance Level
                    </h3>

                    <div class="score-value">
                        ${escapeHTML(
                            importanceLevel
                        )}
                    </div>

                </div>

                <div class="ai-score-card">

                    <h3>
                        Sentiment
                    </h3>

                    <div class="score-value">
                        ${escapeHTML(
                            formatSentiment(sentiment)
                        )}
                    </div>

                </div>

            </div>

            ${topicsHTML}
<div class="ai-section">
                <h3>Summary</h3>
                <ul class="ai-bullet-list">${summary.map((point) => `<li>${escapeHTML(point)}</li>`).join("")}</ul>
            </div>

            <div class="ai-section">
                <h3>Key Points</h3>
                <ul class="ai-bullet-list">${keyPoints.map((point) => `<li>${escapeHTML(point)}</li>`).join("")}</ul>
            </div>

            <div class="ai-section">
                <h3>Why It Matters</h3>
                <ul class="ai-bullet-list">${whyItMatters.map((point) => `<li>${escapeHTML(point)}</li>`).join("")}</ul>
            </div>

            <div class="ai-section">
                <h3>Who Is Affected</h3>
                <ul class="ai-bullet-list">${whoIsAffected.map((point) => `<li>${escapeHTML(point)}</li>`).join("")}</ul>
            </div>

            <div class="ai-section">
                <h3>What Happens Next</h3>
                <ul class="ai-bullet-list">${whatHappensNext.map((point) => `<li>${escapeHTML(point)}</li>`).join("")}</ul>
            </div>

            </section>
    `;
}

// ============================================================
// SAVE ARTICLE
// ============================================================

function saveArticle(index) {

    const article =
        currentDisplayedArticles[index];

    if (!article) {
        return;
    }

    try {

        const saved =
            JSON.parse(
                localStorage.getItem(
                    "newsroom_saved_articles"
                ) || "[]"
            );

        const exists =
            saved.some(function (item) {

                return (
                    item.url &&
                    article.url &&
                    item.url === article.url
                );
            });

        if (!exists) {

            saved.push({
                title:
                    article.title || "",

                description:
                    article.description || "",

                url:
                    article.url || "",

                source:
                    article.source || "",

                published_at:
                    article.published_at || "",

                saved_at:
                    new Date().toISOString()
            });

            localStorage.setItem(
                "newsroom_saved_articles",
                JSON.stringify(saved)
            );
        }

        const card =
            document.querySelector(
                `.news-card[data-index="${index}"]`
            );

        if (card) {

            const button =
                card.querySelector(
                    ".save-button"
                );

            if (button) {

                button.textContent =
                    exists
                        ? "Already saved"
                        : "Saved ✓";

                button.disabled = true;
            }
        }

        console.log(
            "Article saved:",
            article.title
        );

    } catch (error) {

        console.error(
            "Unable to save article:",
            error
        );
    }
}

// ============================================================
// SAVED ARTICLES
// ============================================================

function getSavedArticles() {
    try {
        const saved = JSON.parse(
            localStorage.getItem("newsroom_saved_articles") || "[]"
        );

        return Array.isArray(saved) ? saved : [];
    } catch (error) {
        console.error("Unable to read saved articles:", error);
        return [];
    }
}

function loadSavedArticles() {

    const saved =
        getSavedArticles();

    currentDisplayedArticles =
        saved;

    updateActiveCategory("saved");

    const newsContainer =
        document.getElementById(
            "news-container"
        );

    if (!newsContainer) {
        return;
    }

    if (!saved.length) {

        newsContainer.innerHTML = `
            <div class="results-heading">
                <h2>Saved Articles</h2>
                <p>0 articles</p>
            </div>

            <div class="saved-empty-state">

                <h2>No Saved Articles</h2>

                <p>
                    You haven't saved any articles yet.
                </p>

                <p>
                    Click <strong>Save</strong> on a news article
                    to keep it here.
                </p>

            </div>
        `;

        return;
    }

    const headingHTML = `
        <div class="results-heading">

            <h2>
                Saved Articles
            </h2>

        </div>
    `;

    const cards =
        saved.map(function (article, index) {

            return articleCard(
                article,
                index,
                true
            );

        }).join("");

    newsContainer.innerHTML =
        headingHTML +
        `<div class="news-list">${cards}</div>`;

    fitRenderedArticleDescriptions(newsContainer);
}


function openSavedArticle(index) {
    const saved =
        getSavedArticles();

    const article =
        saved[index];

    if (!article) {
        return;
    }

    if (article.url) {
        window.open(
            article.url,
            "_blank",
            "noopener,noreferrer"
        );
    }
}

function removeSavedArticle(index) {
    try {
        const saved =
            getSavedArticles();

        if (!saved[index]) {
            return;
        }

        const removed =
            saved[index];

        saved.splice(index, 1);

        localStorage.setItem(
            "newsroom_saved_articles",
            JSON.stringify(saved)
        );

        console.log(
            "Saved article removed:",
            removed.title
        );

        loadSavedArticles();

    } catch (error) {
        console.error(
            "Unable to remove saved article:",
            error
        );
    }
}

function isArticleSaved(article) {
    if (!article || !article.url) {
        return false;
    }

    return getSavedArticles().some(
        function (item) {
            return (
                item.url &&
                item.url === article.url
            );
        }
    );
}

// ============================================================
// UPDATE ACTIVE CATEGORY
// ============================================================

function updateActiveCategory(category) {

    const elements =
        document.querySelectorAll(
            "[data-category], .category, .nav-category"
        );

    elements.forEach(function (element) {

        const elementCategory =
            normalizeCategory(
                element.dataset.category ||
                element.getAttribute(
                    "data-category"
                ) ||
                element.textContent
            );

        if (elementCategory === category) {

            element.classList.add(
                "active"
            );

        } else {

            element.classList.remove(
                "active"
            );
        }
    });
}

function scrollToPageTop() {
    window.scrollTo({ top: 0, behavior: "smooth" });
}

// ============================================================
// CLEAN TITLE
// ============================================================

function cleanArticleTitle(title) {

    let value =
        String(
            title || "Untitled article"
        );

    value =
        value
            .replace(/<[^>]*>/g, "")
            .replace(/&nbsp;/gi, " ")
            .replace(/&amp;/gi, "&")
            .replace(/&quot;/gi, '"')
            .replace(/&#39;/gi, "'")
            .replace(/\s+/g, " ")
            .trim();

    return value;
}

// ============================================================
// CLEAN DESCRIPTION
// ============================================================

function cleanArticleDescription(description) {

    let value =
        String(description || "");

    value =
        value
            .replace(/<[^>]*>/g, " ")
            .replace(/&nbsp;/gi, " ")
            .replace(/&amp;/gi, "&")
            .replace(/&quot;/gi, '"')
            .replace(/&#39;/gi, "'")
            .replace(/&#x27;/gi, "'")
            .replace(/\s+/g, " ")
            .trim();

    return value;
}

// Keep category descriptions within four visual lines without ending mid-sentence.
function fitRenderedArticleDescriptions(container = newsContainer) {
    if (!container || !CATEGORY_MAP[container.dataset.category]) return;

    const segmentSentences = (text) => {
        // Keep name initials and Indian honorifics inside a sentence.
        // Sentence segmentation can otherwise stop at periods in names such
        // as "V.D. Satheesan", "Edappadi K. Palaniswami", or "Thol.".
        const periodMarker = "\uE000";
        const protectedText = String(text || "").replace(
            /\b(?!(?:U\.S\.|U\.K\.|U\.N\.|E\.U\.|U\.A\.E\.))(?:(?:[A-Z]\.){1,3}|Thol\.|Dr\.|Mr\.|Ms\.|Mrs\.|Smt\.|Shri\.)/gi,
            abbreviation => abbreviation.replace(/\./g, periodMarker)
        );

        let segments;
        if (window.Intl?.Segmenter) {
            const segmenter = new Intl.Segmenter(undefined, { granularity: "sentence" });
            segments = Array.from(segmenter.segment(protectedText), part => part.segment.trim());
        } else {
            segments = (protectedText.match(/[^.!?]+[.!?]+(?:["'’”)]*)?(?:\s+|$)/g) || [])
                .map(sentence => sentence.trim());
        }

        return segments
            .map(sentence => sentence.replaceAll(periodMarker, ".").trim())
            .filter(Boolean);
    };

    for (const paragraph of container.querySelectorAll(".news-card .article-description")) {
        const fullText = paragraph.dataset.fullDescription || paragraph.textContent.trim();
        const previousFit = paragraph.dataset.latestFittedDescription;
        const currentText = paragraph.textContent.trim();

        // Leave translated or otherwise edited descriptions alone on resize.
        if (previousFit && currentText !== previousFit && currentText !== fullText) continue;

        const sentences = segmentSentences(fullText).filter(sentence =>
            /[.!?]["'’”)]*$/.test(sentence) && !/(?:\.{2,}|…)["'’”)]*$/.test(sentence)
        );
        if (!sentences.length) continue;

        const savedStyle = paragraph.getAttribute("style");
        paragraph.style.setProperty("display", "block", "important");
        paragraph.style.setProperty("-webkit-line-clamp", "unset", "important");
        paragraph.style.setProperty("max-height", "none", "important");
        paragraph.style.setProperty("overflow", "visible", "important");

        const lineCount = (text) => {
            paragraph.textContent = text;
            const range = document.createRange();
            range.selectNodeContents(paragraph);
            const tops = new Set(Array.from(range.getClientRects(), rect => Math.round(rect.top)));
            return tops.size;
        };

        let fittedText = "";
        let candidate = "";
        for (const sentence of sentences) {
            candidate = candidate ? `${candidate} ${sentence}` : sentence;
            if (lineCount(candidate) <= 4) fittedText = candidate;
            else break;
        }

        if (!fittedText) {
            // When a source supplies one long sentence (or omits its final
            // punctuation), falling back to fullText lets the CSS clamp cut
            // it silently in the middle of a line. Fit a clear word-boundary
            // excerpt and mark the omission instead.
            const firstSentence = sentences[0] || fullText;
            const words = firstSentence.split(/\s+/).filter(Boolean);
            let low = 1;
            let high = words.length;
            let best = words[0] ? `${words[0]}…` : "";

            while (low <= high) {
                const middle = Math.floor((low + high) / 2);
                const excerpt = `${words.slice(0, middle).join(" ")}…`;
                if (lineCount(excerpt) <= 4) {
                    best = excerpt;
                    low = middle + 1;
                } else {
                    high = middle - 1;
                }
            }

            fittedText = best;
        }

        paragraph.textContent = fittedText;
        if (fittedText && lineCount(fittedText) < 2) {
            const words = fittedText.split(/\s+/).filter(Boolean);
            if (words.length > 1) {
                const splitAt = Math.ceil(words.length / 2);
                paragraph.replaceChildren(
                    document.createTextNode(words.slice(0, splitAt).join(" ")),
                    document.createElement("br"),
                    document.createTextNode(words.slice(splitAt).join(" "))
                );
            }
        }
        paragraph.dataset.latestFittedDescription = paragraph.textContent.trim();
        if (savedStyle === null) paragraph.removeAttribute("style");
        else paragraph.setAttribute("style", savedStyle);
    }
}

let articleDescriptionResizeTimer;
window.addEventListener("resize", () => {
    clearTimeout(articleDescriptionResizeTimer);
    articleDescriptionResizeTimer = setTimeout(
        () => fitRenderedArticleDescriptions(),
        120
    );
});

// ============================================================
// TEXT HELPER
// ============================================================

function getText(value, fallback) {

    if (
        value === null ||
        value === undefined
    ) {
        return fallback;
    }

    if (typeof value === "object") {

        if (value.text) {
            return String(value.text);
        }

        if (value.summary) {
            return String(value.summary);
        }

        return fallback;
    }

    const text =
        String(value).trim();

    return text || fallback;
}

// ============================================================
// ARRAY HELPER
// ============================================================

function getArray(value) {

    if (!Array.isArray(value)) {
        return [];
    }

    return value
        .map(function (item) {

            if (
                item &&
                typeof item === "object"
            ) {

                if (item.name) {
                    return String(
                        item.name
                    );
                }

                if (item.text) {
                    return String(
                        item.text
                    );
                }

                return "";
            }

            return String(item);

        })
        .map(function (item) {
            return item.trim();
        })
        .filter(Boolean);
}

// ============================================================
// SCORE NORMALIZATION
// ============================================================

function normalizeScore(value) {

    const number =
        Number(value);

    if (
        !Number.isFinite(number)
    ) {
        return 0;
    }

    return Math.max(
        0,
        Math.min(
            100,
            Math.round(number)
        )
    );
}

// ============================================================
// DATE FORMAT
// ============================================================

function formatDate(value) {

    if (!value) {
        return "";
    }

    const date =
        new Date(value);

    if (
        Number.isNaN(
            date.getTime()
        )
    ) {
        return String(value);
    }

    return date.toLocaleString(
        "en-IN",
        {
            day: "numeric",
            month: "short",
            year: "numeric",
            hour: "numeric",
            minute: "2-digit"
        }
    );
}

// ============================================================
// CATEGORY LABEL
// ============================================================

function labelFor(category) {

    return (
        CATEGORY_MAP[
            normalizeCategory(category)
        ] ||
        "Latest"
    ).toUpperCase();
}

// ============================================================
// SAFE URL
// ============================================================

function safeUrl(value) {

    if (!value) {
        return "";
    }

    try {

        const url =
            new URL(
                String(value),
                window.location.href
            );

        if (
            url.protocol === "http:" ||
            url.protocol === "https:"
        ) {
            return url.href;
        }

    } catch (error) {

        console.warn(
            "Invalid URL:",
            value
        );
    }

    return "";
}

// ============================================================
// HTML ESCAPE
// ============================================================

function escapeHTML(value) {

    return String(
        value ?? ""
    ).replace(
        /[&<>"']/g,
        function (character) {

            const entities = {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;"
            };

            return entities[
                character
            ];
        }
    );
}

// ============================================================
// AI STYLES
// ============================================================

const aiStyles =
    document.createElement("style");

aiStyles.textContent = `

.news-loading,
.news-error,
.no-news {
    box-sizing: border-box;
    width: 100%;
    min-height: 260px;
    padding: 70px 30px;
    text-align: center;
    background: #ffffff;
    border: 1px solid #dddddd;
    border-radius: 14px;
}

.news-loading h2,
.news-error h2,
.no-news h2 {
    margin: 15px 0 8px;
    color: #111111;
}

.news-loading p,
.news-error p,
.no-news p {
    color: #777777;
}

.loading-spinner {
    width: 32px;
    height: 32px;
    margin: 0 auto 15px;
    border: 4px solid #dddddd;
    border-top-color: #111111;
    border-radius: 50%;
    animation: newsroom-spin 0.8s linear infinite;
}

@keyframes newsroom-spin {
    to {
        transform: rotate(360deg);
    }
}

.results-heading {
    margin-bottom: 25px;
}

.results-heading h2 {
    margin-bottom: 6px;
}

.results-heading p {
    margin: 0;
    color: #777777;
}

.news-list {
    display: flex;
    flex-direction: column;
    gap: 24px;
}

.news-card {
    background: #ffffff;
    border: 1px solid #dddddd;
    border-radius: 14px;
    padding: 36px;
    box-sizing: border-box;
}

.article-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #222222;
    margin-bottom: 20px;
}

.article-title {
    font-size: 28px;
    line-height: 1.25;
    margin: 0 0 18px;
    color: #111111;
}

.article-description {
    color: #555555;
    line-height: 1.7;
    margin-bottom: 25px;
}

.article-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
}

.article-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 48px;
    padding: 0 22px;
    border-radius: 10px;
    border: 1px solid #222222;
    background: #ffffff;
    color: #111111;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    box-sizing: border-box;
}

.article-button:hover {
    background: #f3f3f3;
}

.article-button.primary {
    background: #111111;
    color: #ffffff;
}

.article-button.primary:hover {
    background: #333333;
}

.article-button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
}

.ai-result {
    margin-top: 35px;
}

.ai-analysis {
    background: #ffffff;
    border: 1px solid #dddddd;
    border-radius: 20px;
    padding: 36px;
    box-sizing: border-box;
}

.ai-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 30px;
}

.ai-eyebrow {
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 3px;
    margin-bottom: 8px;
}

.ai-header h2,
.career-header h2 {
    margin: 0;
    font-size: 30px;
}

.ollama-badge,
.career-level {
    border: 1px solid #222222;
    border-radius: 30px;
    padding: 12px 22px;
    font-weight: 700;
    white-space: nowrap;
}

.ai-score-grid {
    display: grid;
    grid-template-columns:
        repeat(3, minmax(0, 1fr));
    gap: 18px;
    margin-bottom: 30px;
}

.ai-score-card {
    border: 1px solid #dddddd;
    border-radius: 14px;
    padding: 25px;
}

.ai-score-card h3 {
    margin: 0 0 15px;
    font-size: 18px;
}

.score-value {
    font-size: 30px;
    font-weight: 800;
}

.ai-section {
    border-top: 1px solid #dddddd;
    padding: 28px 0;
}

.ai-section h3 {
    margin: 0 0 18px;
    font-size: 24px;
}

.ai-section p {
    margin: 0;
    line-height: 1.75;
    color: #333333;
}

.ai-section ul {
    margin: 0;
    padding-left: 25px;
}

.ai-section li {
    margin-bottom: 12px;
    line-height: 1.6;
}

.muted {
    color: #888888 !important;
}

.career-score-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    border-top: 1px solid #dddddd;
    border-bottom: 1px solid #dddddd;
    padding: 20px 0;
    margin-bottom: 5px;
    font-size: 18px;
}

.career-score-row strong {
    font-size: 28px;
}

.tag-list {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}

.ai-tag {
    display: inline-block;
    border: 1px solid #222222;
    border-radius: 25px;
    padding: 10px 16px;
    font-weight: 600;
}

.job-role-list {
    display: grid;
    grid-template-columns:
        repeat(2, minmax(0, 1fr));
    gap: 12px;
}

.job-role {
    border: 1px solid #dddddd;
    border-radius: 10px;
    padding: 16px;
    font-weight: 600;
}

.ai-loading,
.ai-error {
    padding: 30px;
    border: 1px solid #dddddd;
    border-radius: 14px;
    text-align: center;
}

.ai-error {
    border-color: #999999;
}

.ai-loading h3,
.ai-error h3 {
    margin: 10px 0;
}

.ai-loading p,
.ai-error p {
    color: #777777;
}

@media (max-width: 800px) {

    .ai-score-grid {
        grid-template-columns: 1fr;
    }

    .job-role-list {
        grid-template-columns: 1fr;
    }

    .ai-header {
        flex-direction: column;
        align-items: flex-start;
    }

    .news-card,
    .ai-analysis,
    .article-title {
        font-size: 23px;
    }

}

`;

document.head.appendChild(aiStyles);

// ============================================================
// SAVED ARTICLES BUTTON
// ============================================================

document.addEventListener("DOMContentLoaded", function () {

    const savedButton =
        document.getElementById("saved-button");

    if (savedButton) {

        savedButton.addEventListener("click", function (event) {

            event.preventDefault();
            event.stopPropagation();

            loadSavedArticles();

        });

    }

});



// ============================================================
// LATEST & HOT HEADLINES TICKER
// ============================================================

// ============================================================
// CATEGORY-SPECIFIC LATEST & HOT HEADLINES TICKER
// ============================================================

let tickerRequestId = 0;
const LATEST_HOT_BROWSER_CACHE_KEY = "newsroom-latest-hot-cache-v1";
const LATEST_HOT_BROWSER_CACHE_TTL = 10 * 60 * 1000;

function renderHeadlineTicker(track, articles) {
    const uniqueTitles = new Set();
    const selected = (Array.isArray(articles) ? articles : [])
        .filter(function(article) {
            return article && article.title && article.url;
        })
        .filter(function(article) {
            const title = cleanArticleTitle(article.title);
            const normalized = title.toLowerCase().replace(/\s+/g, " ").trim();
            if (!normalized || uniqueTitles.has(normalized)) return false;
            uniqueTitles.add(normalized);
            return true;
        })
        .slice(0, 20);

    if (!selected.length) return false;

    track.innerHTML = selected.map(function(article) {
        const title = cleanArticleTitle(article.title);
        const url = safeUrl(article.url) || "#";
        return `
            <a class="headline-item" href="${url}" target="_blank" rel="noopener noreferrer">
                <span class="headline-title">${escapeHTML(title)}</span>
            </a>
        `;
    }).join("");

    const seamlessCopy = track.cloneNode(true);
    seamlessCopy.removeAttribute("id");
    seamlessCopy.classList.remove("headline-ticker-track");
    seamlessCopy.classList.add("headline-ticker-copy");
    seamlessCopy.setAttribute("aria-hidden", "true");
    seamlessCopy.querySelectorAll("a").forEach(function(link) {
        link.setAttribute("tabindex", "-1");
    });
    track.appendChild(seamlessCopy);
    return true;
}

async function updateHeadlineTicker() {
    const requestId = ++tickerRequestId;
    const track = document.getElementById(
        "headline-ticker-track"
    );

    if (!track) {
        return;
    }

    if (!track.querySelector("a.headline-item")) {
        try {
            const cached = JSON.parse(
                localStorage.getItem(LATEST_HOT_BROWSER_CACHE_KEY) || "null"
            );
            if (
                cached &&
                Number.isFinite(cached.timestamp) &&
                Date.now() - cached.timestamp <= LATEST_HOT_BROWSER_CACHE_TTL
            ) {
                renderHeadlineTicker(track, cached.articles);
            }
        } catch (error) {
            console.debug("Latest & Hot browser cache could not be read:", error);
        }
    }

    if (!track.querySelector("a.headline-item")) {
        track.innerHTML = `
            <span class="headline-item">
                Loading latest headlines...
            </span>
        `;
    }

    try {
        const response = await fetch(
            API_BASE_URL + "/trending",
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                "LATEST & HOT request failed: " +
                response.status
            );
        }

        const data = await response.json();

        if (requestId !== tickerRequestId) {
            return;
        }

        let articles = Array.isArray(data.articles)
            ? data.articles
            : [];

        articles = articles.filter(function(article) {
            return article &&
                article.title &&
                article.url;
        });

        if (!articles.length) {
            track.innerHTML = `
                <span class="headline-item">
                    No latest headlines available.
                </span>
            `;
            return;
        }

        if (!renderHeadlineTicker(track, articles)) {
            track.innerHTML = `
                <span class="headline-item">
                    No latest headlines available.
                </span>
            `;
            return;
        }

        try {
            localStorage.setItem(LATEST_HOT_BROWSER_CACHE_KEY, JSON.stringify({
                timestamp: Date.now(),
                articles
            }));
        } catch (error) {
            console.debug("Latest & Hot browser cache could not be saved:", error);
        }

    } catch (error) {

        if (requestId !== tickerRequestId) {
            return;
        }

        console.error(
            "LATEST & HOT error:",
            error
        );

        if (!track.querySelector("a.headline-item")) {
            track.innerHTML = `
                <span class="headline-item">
                    Latest & Hot temporarily unavailable.
                </span>
            `;
        }
    }
}


/* ============================================================
   LATEST & HOT — AUTO REFRESH EVERY 5 MINUTES
   ============================================================ */

if (!window.__latestHot5MinuteRefresh) {
    window.__latestHot5MinuteRefresh = setInterval(function () {
        if (
            document.visibilityState === "visible" &&
            typeof updateHeadlineTicker === "function"
        ) {
            updateHeadlineTicker();
        }
    }, 5 * 60 * 1000);
}

console.log("LATEST & HOT will refresh every 5 minutes");

(function startHeaderClock() {
    const dateElement = document.getElementById("header-date");
    const timeElement = document.getElementById("header-time");

    if (!dateElement || !timeElement) return;

    const dateFormatter = new Intl.DateTimeFormat("en-IN", {
        timeZone: "Asia/Kolkata",
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric"
    });
    const timeFormatter = new Intl.DateTimeFormat("en-IN", {
        timeZone: "Asia/Kolkata",
        hour: "numeric",
        minute: "2-digit",
        hour12: true
    });

    function updateHeaderClock() {
        const now = new Date();
        const timestamp = now.toISOString();
        dateElement.dateTime = timestamp;
        timeElement.dateTime = timestamp;
        dateElement.textContent = dateFormatter.format(now);
        timeElement.textContent = timeFormatter.format(now).toLowerCase();
    }

    updateHeaderClock();
    window.setInterval(function () {
        if (document.visibilityState === "visible") updateHeaderClock();
    }, 60 * 1000);
    window.addEventListener("focus", updateHeaderClock);
})();




/* ============================================================
   DARK MODE TOGGLE
   ============================================================ */

(function () {
    const toggle = document.getElementById("theme-toggle");

    if (!toggle) return;

    const savedTheme = localStorage.getItem("newsroom-theme");

    if (savedTheme === "dark") {
        document.documentElement.classList.add("dark-mode");
        toggle.textContent = "☀️";
    } else {
        document.documentElement.classList.remove("dark-mode");
        toggle.textContent = "🌙";
    }

    toggle.addEventListener("click", function () {
        const isDark = document.documentElement.classList.toggle("dark-mode");

        localStorage.setItem(
            "newsroom-theme",
            isDark ? "dark" : "light"
        );

        toggle.textContent = isDark ? "☀️" : "🌙";
    });
})();


/* ============================================================
   ACCOUNT SIGN-IN
   ============================================================ */

(function () {
    const dialog = document.getElementById("auth-dialog");
    const openButton = document.getElementById("auth-open");
    if (!dialog || !openButton) return;

    const formView = document.getElementById("auth-form-view");
    const accountView = document.getElementById("auth-account-view");
    const form = document.getElementById("auth-form");
    const title = document.getElementById("auth-title");
    const emailInput = document.getElementById("auth-email");
    const passwordInput = document.getElementById("auth-password");
    const message = document.getElementById("auth-message");
    const submitButton = document.getElementById("auth-submit");
    const modeButton = document.getElementById("auth-mode-toggle");
    const closeButton = document.getElementById("auth-close");
    const logoutButton = document.getElementById("auth-logout");
    const accountEmail = document.getElementById("auth-account-email");
    const accountMessage = document.getElementById("auth-account-message");
    const googleMount = document.getElementById("google-signin-button");
    const googleFallback = document.getElementById("google-signin-fallback");
    let creatingAccount = false;
    let googleClientId = "";

    function loadGoogleIdentity() {
        if (window.google && window.google.accounts && window.google.accounts.id) {
            return Promise.resolve();
        }

        return new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = "https://accounts.google.com/gsi/client";
            script.async = true;
            script.onload = resolve;
            script.onerror = () => reject(new Error("Could not load Google sign-in."));
            document.head.appendChild(script);
        });
    }

    async function handleGoogleCredential(credentialResponse) {
        message.textContent = "Signing in with Google…";
        message.classList.remove("is-error", "is-success");
        try {
            const response = await fetch(`${API_BASE_URL}/auth/google`, {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ credential: credentialResponse.credential })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "Google sign-in failed. Please try again.");
            await readAccount();
            message.textContent = "";
            form.reset();
        } catch (error) {
            message.textContent = error.message || "Google sign-in failed. Please try again.";
            message.classList.add("is-error");
        }
    }

    async function initializeGoogleSignIn() {
        try {
            const configResponse = await fetch(`${API_BASE_URL}/auth/google/config`, { cache: "no-store" });
            if (!configResponse.ok) throw new Error("Could not load Google sign-in settings.");
            const config = await configResponse.json();
            googleClientId = typeof config.client_id === "string" ? config.client_id.trim() : "";
            if (!googleClientId) return;

            await loadGoogleIdentity();
            window.google.accounts.id.initialize({
                client_id: googleClientId,
                callback: handleGoogleCredential
            });
            googleMount.hidden = false;
            googleFallback.hidden = true;
            window.google.accounts.id.renderButton(googleMount, {
                type: "standard",
                theme: "outline",
                size: "large",
                text: "continue_with",
                shape: "rect",
                logo_alignment: "left",
                width: Math.min(400, Math.max(220, Math.floor(googleMount.clientWidth)))
            });
        } catch (error) {
            console.warn("Google sign-in could not be initialized.", error);
        }
    }

    async function readAccount() {
        try {
            const response = await fetch(`${API_BASE_URL}/auth/me`, { credentials: "include" });
            const data = await response.json();
            if (data.authenticated) {
                openButton.textContent = "My account";
                openButton.setAttribute("aria-label", `Signed in as ${data.user.email}. Open account`);
                accountEmail.textContent = data.user.email;
                formView.hidden = true;
                accountView.hidden = false;
            } else {
                openButton.textContent = "Sign in";
                openButton.removeAttribute("aria-label");
                accountView.hidden = true;
                formView.hidden = false;
            }
        } catch (error) {
            openButton.textContent = "Sign in";
            console.warn("Could not check sign-in status.", error);
        }
    }

    function setMode(create) {
        creatingAccount = create;
        title.textContent = create ? "Create account" : "Sign in";
        submitButton.textContent = create ? "Create account" : "Sign in";
        modeButton.textContent = create ? "Already have an account? Sign in" : "Create an account";
        passwordInput.autocomplete = create ? "new-password" : "current-password";
        message.textContent = "";
        message.classList.remove("is-error", "is-success");
    }

    openButton.addEventListener("click", async function () {
        await readAccount();
        if (!dialog.open) dialog.showModal();
        if (!accountView.hidden) document.getElementById("auth-logout").focus();
        else emailInput.focus();
    });
    closeButton.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
    });
    modeButton.addEventListener("click", () => setMode(!creatingAccount));
    googleFallback.addEventListener("click", () => {
        message.textContent = googleClientId
            ? "Google sign-in is loading. Please try again in a moment."
            : "Google sign-in needs a Google web client ID configured on this server.";
        message.classList.add("is-error");
    });

    form.addEventListener("submit", async function (event) {
        event.preventDefault();
        submitButton.disabled = true;
        message.textContent = creatingAccount ? "Creating your account…" : "Signing in…";
        message.classList.remove("is-error", "is-success");
        try {
            const response = await fetch(`${API_BASE_URL}/auth/${creatingAccount ? "register" : "login"}`, {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email: emailInput.value, password: passwordInput.value })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "Could not sign in. Please try again.");
            await readAccount();
            form.reset();
            message.textContent = "";
        } catch (error) {
            message.textContent = error.message || "The server could not be reached. Please try again.";
            message.classList.add("is-error");
        } finally {
            submitButton.disabled = false;
        }
    });

    logoutButton.addEventListener("click", async function () {
        logoutButton.disabled = true;
        accountMessage.textContent = "";
        accountMessage.classList.remove("is-error");
        try {
            const response = await fetch(`${API_BASE_URL}/auth/logout`, { method: "POST", credentials: "include" });
            if (!response.ok) throw new Error("Sign out failed. Please try again.");
            setMode(false);
            await readAccount();
        } catch (error) {
            accountMessage.textContent = error.message || "Could not sign out. Please try again.";
            accountMessage.classList.add("is-error");
        } finally {
            logoutButton.disabled = false;
        }
    });

    readAccount();
    initializeGoogleSignIn();
})();
