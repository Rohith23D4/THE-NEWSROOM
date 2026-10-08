// ============================================================
// THE NEWSROOM
// SAVED NEWS FRONTEND
// ============================================================

const API_BASE_URL = "http://127.0.0.1:8000";

let savedArticles = [];


// ============================================================
// PAGE START
// ============================================================

document.addEventListener("DOMContentLoaded", function () {

    console.log("Saved News page loaded.");

    setupRefreshButton();

    loadSavedNews();

});


// ============================================================
// REFRESH BUTTON
// ============================================================

function setupRefreshButton() {

    const button =
        document.querySelector("#refresh-saved");

    if (!button) {
        console.error(
            "Refresh button not found."
        );

        return;
    }

    button.addEventListener(
        "click",
        function () {

            loadSavedNews();

        }
    );
}


// ============================================================
// LOAD SAVED NEWS
// ============================================================

async function loadSavedNews() {

    const container =
        document.querySelector("#saved-container");

    const status =
        document.querySelector("#saved-status");

    if (!container) {
        console.error(
            "Could not find #saved-container"
        );

        return;
    }

    container.innerHTML = `
        <div class="saved-loading">
            Loading saved news...
        </div>
    `;

    if (status) {
        status.textContent =
            "Loading saved news...";
    }

    try {

        const response =
            await fetch(
                API_BASE_URL + "/saved",
                {
                    method: "GET",
                    cache: "no-store"
                }
            );

        if (!response.ok) {

            throw new Error(
                "Saved News API error: HTTP " +
                response.status
            );

        }

        const data =
            await response.json();

        console.log(
            "Saved News response:",
            data
        );

        if (
            !data ||
            !Array.isArray(data.articles)
        ) {

            throw new Error(
                "Invalid saved news response."
            );

        }

        savedArticles =
            data.articles;

        renderSavedNews(
            savedArticles
        );

        if (status) {

            status.textContent =
                savedArticles.length +
                " saved article" +
                (
                    savedArticles.length === 1
                        ? ""
                        : "s"
                );

        }

    } catch (error) {

        console.error(
            "Failed to load saved news:",
            error
        );

        container.innerHTML = `
            <div class="saved-error">
                <h2>Unable to load saved news</h2>

                <p>
                    Make sure the FastAPI backend is
                    running on port 8000.
                </p>

                <p>
                    Error:
                    ${escapeHTML(error.message)}
                </p>
            </div>
        `;

        if (status) {

            status.textContent =
                "Unable to load saved news.";

        }

    }

}


// ============================================================
// RENDER SAVED NEWS
// ============================================================

function renderSavedNews(
    articles
) {

    const container =
        document.querySelector(
            "#saved-container"
        );

    if (!container) {
        return;
    }

    if (
        !Array.isArray(articles) ||
        articles.length === 0
    ) {

        container.innerHTML = `
            <div class="saved-empty">

                <h2>No saved news yet</h2>

                <p>
                    Save articles from The Newsroom
                    and they will appear here.
                </p>

            </div>
        `;

        return;
    }

    container.innerHTML =
        articles
            .map(
                function (article, index) {

                    return createSavedCard(
                        article,
                        index
                    );

                }
            )
            .join("");

}


// ============================================================
// CREATE SAVED CARD
// ============================================================

function createSavedCard(
    article,
    index
) {

    const id =
        article.id;

    const title =
        cleanText(
            article.article_title ||
            article.title ||
            "Untitled article"
        );

    const description =
        cleanText(
            article.description ||
            ""
        );

    const source =
        cleanText(
            article.source ||
            "Unknown source"
        );

    const published =
        formatDate(
            article.published_at
        );

    const url =
        safeUrl(
            article.url
        );

    return `
        <article
            class="saved-card"
            id="saved-card-${index}"
        >

            <div class="saved-meta">

                <span class="saved-source">
                    ${escapeHTML(source)}
                </span>

                ${
                    published
                        ? `
                            <span>•</span>
                            <span>
                                ${escapeHTML(published)}
                            </span>
                          `
                        : ""
                }

            </div>

            <h2 class="saved-title">
                ${escapeHTML(title)}
            </h2>

            ${
                description
                    ? `
                        <p class="saved-description">
                            ${escapeHTML(description)}
                        </p>
                      `
                    : ""
            }

            <div class="saved-card-actions">

                ${
                    url
                        ? `
                            <button
                                class="saved-button primary"
                                type="button"
                                onclick="openOriginal(
                                    ${JSON.stringify(url)}
                                )"
                            >
                                Open original
                            </button>
                          `
                        : ""
                }

                <button
                    class="saved-button"
                    type="button"
                    onclick="analyzeSavedArticle(
                        ${index}
                    )"
                >
                    Analyze with AI
                </button>

                <button
                    class="saved-button danger"
                    type="button"
                    onclick="deleteSavedArticle(
                        ${id},
                        ${index}
                    )"
                >
                    Delete
                </button>

            </div>

            <div
                id="saved-ai-${index}"
                class="saved-ai-panel"
            ></div>

        </article>
    `;

}


// ============================================================
// OPEN ORIGINAL
// ============================================================

function openOriginal(
    url
) {

    if (!url) {

        alert(
            "Original article URL is unavailable."
        );

        return;
    }

    window.open(
        url,
        "_blank",
        "noopener,noreferrer"
    );

}


// ============================================================
// ANALYZE SAVED ARTICLE
// ============================================================

async function analyzeSavedArticle(
    index
) {

    const article =
        savedArticles[index];

    if (!article) {

        console.error(
            "Saved article not found:",
            index
        );

        return;
    }

    const panel =
        document.querySelector(
            "#saved-ai-" + index
        );

    if (!panel) {
        return;
    }

    panel.classList.add(
        "visible"
    );

    panel.innerHTML = `
        <div class="saved-ai-header">

            <h3>
                AI Analysis
            </h3>

            <span class="ollama-badge">
                Powered by Ollama
            </span>

        </div>

        <p>
            Ollama is analyzing this article...
        </p>
    `;

    const title =
        article.article_title ||
        article.title ||
        "";

    const description =
        article.description ||
        "";

    try {

        const url =
            API_BASE_URL +
            "/summarize?title=" +
            encodeURIComponent(title) +
            "&description=" +
            encodeURIComponent(description);

        const response =
            await fetch(
                url,
                {
                    method: "POST",
                    cache: "no-store"
                }
            );

        if (!response.ok) {

            throw new Error(
                "AI API error: HTTP " +
                response.status
            );

        }

        const data =
            await response.json();

        console.log(
            "Saved article AI response:",
            data
        );

        const analysis =
            normalizeAIResponse(
                data
            );

        renderAIAnalysis(
            panel,
            analysis
        );

    } catch (error) {

        console.error(
            "AI analysis failed:",
            error
        );

        panel.innerHTML = `
            <div class="saved-ai-header">

                <h3>
                    AI Analysis
                </h3>

                <span class="ollama-badge">
                    Powered by Ollama
                </span>

            </div>

            <div class="saved-error">

                Unable to analyze this article.

                <br><br>

                ${escapeHTML(error.message)}

            </div>
        `;

    }

}


// ============================================================
// NORMALIZE AI RESPONSE
// ============================================================

function normalizeAIResponse(
    data
) {

    let result = null;

    if (
        data &&
        typeof data === "object"
    ) {

        if (
            data.summary &&
            typeof data.summary === "object"
        ) {

            result =
                data.summary;

        } else {

            result =
                data;

        }

    }

    if (
        !result ||
        typeof result !== "object"
    ) {

        return {
            summary:
                "No summary available.",
            key_points: [],
            why_it_matters:
                "No additional information available.",
            importance_score: null,
            importance_level: "",
            sentiment: "",
            who_is_affected: "",
            what_happens_next: "",
            career_impact: ""
        };

    }

    return {

        summary:
            typeof result.summary === "string"
                ? result.summary
                : "No summary available.",

        key_points:
            Array.isArray(result.key_points)
                ? result.key_points
                : [],

        why_it_matters:
            typeof result.why_it_matters === "string"
                ? result.why_it_matters
                : "No additional information available.",

        importance_score:
            result.importance_score ?? null,

        importance_level:
            result.importance_level || "",

        sentiment:
            result.sentiment || "",

        who_is_affected:
            result.who_is_affected || "",

        what_happens_next:
            result.what_happens_next || "",

        career_impact:
            result.career_impact || ""

    };

}


// ============================================================
// RENDER AI ANALYSIS
// ============================================================

function renderAIAnalysis(
    panel,
    analysis
) {

    const score =
        analysis.importance_score;

    const level =
        analysis.importance_level;

    const sentiment =
        analysis.sentiment;

    const keyPoints =
        Array.isArray(
            analysis.key_points
        )
            ? analysis.key_points
            : [];

    const keyPointsHTML =
        keyPoints.length > 0

            ? `
                <ul>
                    ${keyPoints
                        .map(
                            function (point) {

                                return `
                                    <li>
                                        ${escapeHTML(
                                            String(point)
                                        )}
                                    </li>
                                `;

                            }
                        )
                        .join("")}
                </ul>
              `

            : `
                <p>
                    No key points available.
                </p>
              `;

    panel.innerHTML = `

        <div class="saved-ai-header">

            <h3>
                AI Analysis
            </h3>

            <span class="ollama-badge">
                Powered by Ollama
            </span>

        </div>


        ${
            score !== null ||
            level ||
            sentiment

                ? `
                    <div class="ai-metrics">

                        <div class="ai-metric">

                            <span
                                class="ai-metric-label"
                            >
                                Importance Score
                            </span>

                            <span
                                class="ai-metric-value"
                            >
                                ${
                                    score !== null
                                        ? escapeHTML(
                                            String(score)
                                          ) +
                                          "/100"
                                        : "N/A"
                                }
                            </span>

                        </div>


                        <div class="ai-metric">

                            <span
                                class="ai-metric-label"
                            >
                                Importance Level
                            </span>

                            <span
                                class="ai-metric-value"
                            >
                                ${
                                    escapeHTML(
                                        String(
                                            level ||
                                            "N/A"
                                        )
                                    )
                                }
                            </span>

                        </div>


                        <div class="ai-metric">

                            <span
                                class="ai-metric-label"
                            >
                                Sentiment
                            </span>

                            <span
                                class="ai-metric-value"
                            >
                                ${
                                    escapeHTML(
                                        String(
                                            sentiment ||
                                            "N/A"
                                        )
                                    )
                                }
                            </span>

                        </div>

                    </div>
                  `
                : ""
        }


        <div class="ai-section">

            <h4>
                Summary
            </h4>

            <p>
                ${escapeHTML(
                    analysis.summary
                )}
            </p>

        </div>


        <div class="ai-section">

            <h4>
                Key Points
            </h4>

            ${keyPointsHTML}

        </div>


        <div class="ai-section">

            <h4>
                Why It Matters
            </h4>

            <p>
                ${escapeHTML(
                    analysis.why_it_matters
                )}
            </p>

        </div>


        ${
            analysis.who_is_affected

                ? `
                    <div class="ai-section">

                        <h4>
                            Who Is Affected?
                        </h4>

                        <p>
                            ${escapeHTML(
                                String(
                                    analysis.who_is_affected
                                )
                            )}
                        </p>

                    </div>
                  `
                : ""
        }


        ${
            analysis.what_happens_next

                ? `
                    <div class="ai-section">

                        <h4>
                            What Happens Next?
                        </h4>

                        <p>
                            ${escapeHTML(
                                String(
                                    analysis.what_happens_next
                                )
                            )}
                        </p>

                    </div>
                  `
                : ""
        }


        ${
            analysis.career_impact

                ? `
                    <div class="ai-section">

                        <h4>
                            Career Impact
                        </h4>

                        <p>
                            ${escapeHTML(
                                String(
                                    analysis.career_impact
                                )
                            )}
                        </p>

                    </div>
                  `
                : ""
        }

    `;

}


// ============================================================
// DELETE SAVED ARTICLE
// ============================================================

async function deleteSavedArticle(
    articleId,
    index
) {

    if (!articleId) {

        alert(
            "Invalid saved article ID."
        );

        return;
    }

    const article =
        savedArticles[index];

    const title =
        article
            ? (
                article.article_title ||
                article.title ||
                "this article"
            )
            : "this article";

    const confirmed =
        window.confirm(
            "Delete this saved article?\n\n" +
            title
        );

    if (!confirmed) {
        return;
    }

    try {

        const response =
            await fetch(
                API_BASE_URL +
                "/saved/" +
                encodeURIComponent(
                    articleId
                ),
                {
                    method: "DELETE",
                    cache: "no-store"
                }
            );

        if (!response.ok) {

            throw new Error(
                "Delete API error: HTTP " +
                response.status
            );

        }

        const data =
            await response.json();

        console.log(
            "Delete response:",
            data
        );

        await loadSavedNews();

    } catch (error) {

        console.error(
            "Delete failed:",
            error
        );

        alert(
            "Unable to delete the article.\n\n" +
            error.message
        );

    }

}


// ============================================================
// CLEAN TEXT
// ============================================================

function cleanText(
    value
) {

    if (
        value === null ||
        value === undefined
    ) {

        return "";

    }

    return String(value)
        .replace(
            /<[^>]*>/g,
            " "
        )
        .replace(
            /\s+/g,
            " "
        )
        .trim();

}


// ============================================================
// FORMAT DATE
// ============================================================

function formatDate(
    value
) {

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
// SAFE URL
// ============================================================

function safeUrl(
    value
) {

    if (!value) {
        return "";
    }

    try {

        const url =
            new URL(
                String(value)
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
// ESCAPE HTML
// ============================================================

function escapeHTML(
    value
) {

    return String(
        value ?? ""
    )
        .replace(
            /&/g,
            "&amp;"
        )
        .replace(
            /</g,
            "&lt;"
        )
        .replace(
            />/g,
            "&gt;"
        )
        .replace(
            /"/g,
            "&quot;"
        )
        .replace(
            /'/g,
            "&#39;"
        );

}


// ============================================================
// GLOBAL FUNCTIONS
// ============================================================

window.loadSavedNews =
    loadSavedNews;

window.openOriginal =
    openOriginal;

window.analyzeSavedArticle =
    analyzeSavedArticle;

window.deleteSavedArticle =
    deleteSavedArticle;


// ============================================================
// CONSOLE
// ============================================================

console.log(
    "THE NEWSROOM Saved News frontend loaded."
);

console.log(
    "Backend:",
    API_BASE_URL
);
