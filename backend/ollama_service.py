import json
import time
import re
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# ============================================================
# OLLAMA CONFIGURATION
# ============================================================

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"

# Fast local model
OLLAMA_MODEL = "qwen2.5:3b"
OLLAMA_TRANSLATION_MODEL = "translategemma:4b-it-q4_K_M"

# Maximum time to wait for Ollama
OLLAMA_TIMEOUT = 120


# ============================================================
# DEFAULT RESPONSE
# ============================================================

def article_source_facts(title="", description=""):
    """Extract distinct, verbatim facts and clauses from an article."""
    candidates = []
    text = " ".join(part.strip() for part in (str(title or ""), str(description or "")) if part.strip())

    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = re.sub(r"\s+", " ", sentence).strip(" \t\r\n-–—")
        if not sentence:
            continue
        candidates.append(sentence)
        clauses = re.split(r"\s*(?:;|—|–|:)\s*|,\s+(?=(?:and|but|while|whereas|as)\b)", sentence, flags=re.IGNORECASE)
        candidates.extend(clause.strip(" ,.;:—–") for clause in clauses if clause.strip(" ,.;:—–"))

    # A title plus a long compound sentence often contains several separate
    # reported facts. Retain those source clauses so fallback bullets stay
    # article-specific when the local model returns malformed or empty JSON.
    for sentence in re.split(r"(?<=[.!?])\s+", str(description or "")):
        if len(sentence.split()) < 12:
            continue
        clauses = re.split(r",\s+|\s+(?:and|but|while|whereas)\s+", sentence, flags=re.IGNORECASE)
        candidates.extend(clause.strip(" ,.;:—–") for clause in clauses if len(clause.split()) >= 4)

    unique = []
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip(" \t\r\n-–—")
        if len(candidate.split()) < 4:
            continue
        if not any(points_repeat(candidate, prior) for prior in unique):
            unique.append(candidate)

    # If a short source has fewer than three distinct sentences/clauses, use
    # three non-overlapping excerpts of its own wording. This keeps every UI
    # section populated without adding a generic or invented claim.
    if len(unique) < 3:
        words = re.findall(r"\S+", text)
        if words:
            for chunk_index in range(3):
                start = round(chunk_index * len(words) / 3)
                end = round((chunk_index + 1) * len(words) / 3)
                excerpt = " ".join(words[start:end]).strip(" ,.;:—–")
                if len(excerpt.split()) >= 1 and not any(
                    excerpt.casefold() == prior.casefold() for prior in unique
                ):
                    unique.append(excerpt)
                if len(unique) >= 3:
                    break

    return unique


def default_analysis(title="", description=""):
    """Return a source-grounded fallback when Ollama is unavailable."""
    facts = article_source_facts(title, description)
    points = facts[:3]
    return {
        "topics": [],
        "summary": list(points),
        "key_points": list(points),
        "why_it_matters": list(points),
        "importance_score": 50,
        "importance_level": "MEDIUM",
        "sentiment": "NEUTRAL",
        "who_is_affected": list(points),
        "what_happens_next": list(points),
        "career_impact": "Not stated in the article.",
        "career_impact_score": 0,
        "career_impact_level": "NONE",
        "skills_to_learn": [],
        "relevant_job_roles": [],
        "career_opportunities": "Not stated in the article.",
        "who_should_care": "Not stated in the article.",
        "career_recommendation": "Not stated in the article.",
    }


# ============================================================
# CLEAN OLLAMA RESPONSE
# ============================================================

def clean_json_text(text):
    """
    Clean common formatting that Ollama may add around JSON.
    """

    if not text:
        return ""

    text = text.strip()

    # Remove markdown code fences
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return text.strip()


_ANALYSIS_STOP_WORDS = {
    "the", "and", "for", "from", "with", "that", "this", "was", "were",
    "has", "have", "will", "are", "its", "their", "they", "about", "into",
    "after", "before", "by", "in", "on", "of", "to", "a", "an", "as",
    "is", "be", "at", "it", "or", "but", "may", "could", "would",
}


def normalize_point_token(token):
    """Normalize common inflections so paraphrased bullets compare alike."""
    token = str(token or "").lower()
    aliases = {
        "issued": "issue", "issues": "issue", "issuing": "issue",
        "affects": "affect", "affected": "affect", "affecting": "affect",
        "covers": "cover", "covered": "cover", "covering": "cover",
        "includes": "include", "included": "include", "including": "include",
        "districts": "district", "warnings": "warning", "alerts": "alert",
        "thunderstorms": "thunderstorm", "rains": "rain",
    }
    if token in aliases:
        return aliases[token]
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def points_repeat(first, second):
    """Return true for exact duplicates and close factual paraphrases."""
    def tokens(value):
        return {
            normalized
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if token not in _ANALYSIS_STOP_WORDS
            for normalized in (normalize_point_token(token),)
            if len(normalized) > 2 or normalized.isdigit()
        }

    first_text = re.sub(r"\s+", " ", str(first or "").casefold()).strip()
    second_text = re.sub(r"\s+", " ", str(second or "").casefold()).strip()
    if first_text == second_text:
        return True
    first_tokens = tokens(first)
    second_tokens = tokens(second)
    shared = len(first_tokens & second_tokens)
    overlap = shared / max(1, min(len(first_tokens), len(second_tokens)))
    return shared >= 3 and overlap >= 0.65


# ============================================================
# NORMALIZE LIST
# ============================================================

def normalize_list(value):
    """
    Make sure list fields always become Python lists.
    """

    if value is None:
        return []

    if isinstance(value, list):
        result = []

        for item in value:
            if isinstance(item, str):
                cleaned = item.strip()

                if cleaned:
                    result.append(cleaned)

        return result

    if isinstance(value, str):

        lines = value.splitlines()

        result = []

        for line in lines:

            line = re.sub(
                r"^[\s•\-\*\d\.\)]+",
                "",
                line,
            ).strip()

            if line:
                result.append(line)

        return result

    return []


# ============================================================
# NORMALIZE ANALYSIS
# ============================================================

def normalize_analysis(data, title="", description=""):
    """
    Normalize Ollama output into the exact structure required
    by the frontend.

    This function focuses on structure and formatting.
    Factual interpretation is handled by the strict Ollama prompt.
    """

    fallback = default_analysis(title, description)

    if not isinstance(data, dict):
        return fallback

    # --------------------------------------------------------
    # HELPER
    # --------------------------------------------------------

    source_facts = article_source_facts(title, description)
    article_tokens = {
        normalize_point_token(token)
        for token in re.findall(r"[a-z0-9]+", f"{title} {description}".lower())
        if len(token) > 2 and token not in _ANALYSIS_STOP_WORDS
    }

    def is_source_grounded(point):
        point_tokens = {
            normalize_point_token(token)
            for token in re.findall(r"[a-z0-9]+", str(point or "").lower())
            if len(token) > 2 and token not in _ANALYSIS_STOP_WORDS
        }
        shared = point_tokens & article_tokens
        return len(shared) >= min(2, len(point_tokens)) and (
            len(shared) / max(1, len(point_tokens)) >= 0.4
        )

    def clean_points(value):
        # Filter generic or unsupported model filler before filling gaps from
        # verbatim facts in the source article.
        points = normalize_three_points(value, fallback=[])
        unique = []
        for point in points:
            point = str(point or "").strip().strip("'\"")
            if not point or not is_source_grounded(point):
                continue
            if not any(points_repeat(point, existing) for existing in unique):
                unique.append(point)

        for fact in source_facts:
            if len(unique) >= 3:
                break
            if is_source_grounded(fact) and not any(
                fact.casefold() == existing.casefold() for existing in unique
            ):
                unique.append(fact)

        return unique[:3]

    # --------------------------------------------------------
    # FIVE FACTUAL SECTIONS
    # --------------------------------------------------------

    summary = clean_points(
        data.get("summary")
    )

    key_points = clean_points(
        data.get("key_points")
    )

    why_it_matters = clean_points(
        data.get("why_it_matters")
    )

    who_is_affected = clean_points(
        data.get("who_is_affected")
    )

    what_happens_next = clean_points(
        data.get("what_happens_next")
    )

    # Each heading is deduplicated independently. Distinct sections may need
    # to refer to the same source fact, but no section should be left with fewer
    # than three bullets because another heading used that fact first.

    # --------------------------------------------------------
    # IMPORTANCE SCORE
    # --------------------------------------------------------

    importance_score = data.get(
        "importance_score",
        0
    )

    try:
        importance_score = int(
            importance_score
        )
    except Exception:
        importance_score = 0

    importance_score = max(
        0,
        min(100, importance_score)
    )

    importance_level = str(
        data.get(
            "importance_level",
            ""
        )
    ).upper().strip()

    valid_levels = {
        "VERY HIGH",
        "HIGH",
        "MEDIUM",
        "LOW",
        "NONE",
    }

    if importance_level not in valid_levels:

        if importance_score >= 85:
            importance_level = "VERY HIGH"
        elif importance_score >= 65:
            importance_level = "HIGH"
        elif importance_score >= 40:
            importance_level = "MEDIUM"
        elif importance_score >= 15:
            importance_level = "LOW"
        else:
            importance_level = "NONE"

    # --------------------------------------------------------
    # SENTIMENT
    # --------------------------------------------------------

    sentiment = str(
        data.get(
            "sentiment",
            "NEUTRAL"
        )
    ).upper().strip()

    if sentiment not in {
        "POSITIVE",
        "NEGATIVE",
        "NEUTRAL",
        "MIXED",
    }:
        sentiment = "NEUTRAL"

    # --------------------------------------------------------
    # CAREER INFORMATION
    # --------------------------------------------------------

    career_impact = str(
        data.get(
            "career_impact",
            "Not stated in the article."
        ) or ""
    ).strip()

    career_opportunities = str(
        data.get(
            "career_opportunities",
            "Not stated in the article."
        ) or ""
    ).strip()

    who_should_care = str(
        data.get(
            "who_should_care",
            "Not stated in the article."
        ) or ""
    ).strip()

    career_recommendation = str(
        data.get(
            "career_recommendation",
            "Not stated in the article."
        ) or ""
    ).strip()

    skills_to_learn = normalize_list(
        data.get("skills_to_learn")
    )

    relevant_job_roles = normalize_list(
        data.get("relevant_job_roles")
    )

    career_impact_score = data.get(
        "career_impact_score",
        0
    )

    try:
        career_impact_score = int(
            career_impact_score
        )
    except Exception:
        career_impact_score = 0

    career_impact_score = max(
        0,
        min(100, career_impact_score)
    )

    career_impact_level = str(
        data.get(
            "career_impact_level",
            "NONE"
        )
    ).upper().strip()

    if career_impact_level not in valid_levels:
        career_impact_level = "NONE"

    # If Ollama says there is no career impact,
    # make the remaining career fields consistent.
    if career_impact_score == 0:

        career_impact_level = "NONE"

        skills_to_learn = []

        relevant_job_roles = []

        if not career_impact:
            career_impact = (
                "Not stated in the article."
            )

        if not career_opportunities:
            career_opportunities = (
                "Not stated in the article."
            )

        if not career_recommendation:
            career_recommendation = (
                "Not stated in the article."
            )

    # --------------------------------------------------------
    # RETURN EXACT STRUCTURE
    # --------------------------------------------------------

    return {
        "summary": summary,

        "key_points": key_points,

        "why_it_matters": why_it_matters,

        "importance_score": importance_score,

        "importance_level": importance_level,

        "sentiment": sentiment,

        "who_is_affected": who_is_affected,

        "what_happens_next": what_happens_next,

        "career_impact": career_impact,

        "career_impact_score": career_impact_score,

        "career_impact_level": career_impact_level,

        "skills_to_learn": skills_to_learn,

        "relevant_job_roles": relevant_job_roles,

        "career_opportunities": career_opportunities,

        "who_should_care": who_should_care,

        "career_recommendation": career_recommendation,
    }


def normalize_three_points(value, fallback=None):
    """
    Return up to 3 distinct, useful bullet points.

    If Ollama returns fewer than 3 points, use source-derived
    fallback information rather than repeating or paraphrasing a point.
    """

    import ast
    import json
    import re

    if fallback is None:
        fallback = []

    # --------------------------------------------------------
    # UNWRAP LIST-LIKE STRINGS
    # --------------------------------------------------------

    for _ in range(3):

        if isinstance(value, list):
            break

        if not isinstance(value, str):
            break

        cleaned = value.strip()

        if not cleaned:
            value = []
            break

        parsed = None

        try:
            parsed = json.loads(cleaned)
        except Exception:
            pass

        if parsed is None:
            try:
                parsed = ast.literal_eval(cleaned)
            except Exception:
                pass

        if isinstance(parsed, list):
            value = parsed
        else:
            break

    # --------------------------------------------------------
    # CONVERT TO CLEAN LIST
    # --------------------------------------------------------

    points = []

    if isinstance(value, list):

        for item in value:

            if item is None:
                continue

            item = str(item).strip()

            if not item:
                continue

            # Remove accidental surrounding quotes.
            if (
                len(item) >= 2
                and item[0] == "'"
                and item[-1] == "'"
            ):
                item = item[1:-1].strip()

            if (
                len(item) >= 2
                and item[0] == '"'
                and item[-1] == '"'
            ):
                item = item[1:-1].strip()

            # Reject placeholder bullets.
            if item.lower() in {
                "not stated in the article.",
                "not stated in the article",
                "no additional information available.",
                "no additional information available",
                "no specific groups identified.",
                "no specific groups identified",
                "no specific next steps identified.",
                "no specific next steps identified",
            }:
                continue

            points.append(item)

    elif value:

        item = str(value).strip()

        if item:
            points.append(item)

    # --------------------------------------------------------
    # CLEAN DUPLICATES
    # --------------------------------------------------------

    unique = []

    for point in points:
        if point and not any(points_repeat(point, existing) for existing in unique):
            unique.append(point)

    points = unique[:3]

    # --------------------------------------------------------
    # SOURCE-DERIVED FALLBACKS
    # --------------------------------------------------------

    fallback_points = []

    if isinstance(fallback, list):

        for item in fallback:

            if item is None:
                continue

            item = str(item).strip()

            if not item:
                continue

            if item.lower() in {
                "not stated in the article.",
                "not stated in the article",
            }:
                continue

            fallback_points.append(item)

    # Add source-derived fallback points.
    for item in fallback_points:

        if len(points) >= 3:
            break

        if not any(points_repeat(item, existing) for existing in points):
            points.append(item)

    return points[:3]



def build_prompt(title, description):
    return f"""
You are a strict, factual news analysis assistant.

SOURCE MATERIAL
===============

TITLE:
{title}

DESCRIPTION:
{description}

The TITLE and DESCRIPTION above are your ONLY source of facts.

============================================================
ABSOLUTE FACTUALITY RULE
============================================================

DO NOT use outside knowledge.

DO NOT guess.

DO NOT infer.

DO NOT predict.

DO NOT add common-sense consequences.

DO NOT add information because it is normally associated
with the topic.

Every factual statement must be supported by the TITLE
or DESCRIPTION.

If information is not explicitly present, write:

"Not stated in the article."

MOST IMPORTANT RULE:

Do not invent facts. When a section has limited material, use three concise,
distinct details directly stated in the source; never use generic filler.

============================================================
EXAMPLE
============================================================

SOURCE:

Title:
Heavy rain in Kerala

Description:
Officials warn of possible flash floods in high ranges.

VALID:

- Heavy rain is reported in Kerala.
- Officials warn of possible flash floods.
- The warning concerns high ranges.

INVALID:

- Tourists are affected.
- Infrastructure may be damaged.
- Emergency teams are preparing.
- Evacuation plans will be implemented.
- Authorities will monitor the weather.
- Flooding will cause economic losses.

Those statements are NOT allowed unless explicitly stated
in the source.

============================================================
HOW TO WRITE THE FIVE SECTIONS
============================================================
Every one of the five sections must contain exactly three bullet strings.
Use only distinct details supported by the title or description. Never use
generic filler or duplicate a point within a section.


SUMMARY

Return exactly three short, source-grounded bullet points, each no longer than
18 words. Use the main event and distinct supporting details.

KEY POINTS

Return exactly three additional concrete details from the source, each no
longer than 18 words.
Do not repeat, summarize, or rephrase any fact already used in SUMMARY.
Never add interpretation or create a new fact.

------------------------------------------------------------

------------------------------------------------------------

WHY IT MATTERS

Only explain significance that is explicitly supported by
the source.

Do NOT write general statements such as:

- Public safety is important.
- Infrastructure may be affected.
- This could cause economic losses.
- Early warnings can save lives.

unless the source explicitly says so.

Return exactly three distinct points. Do not repeat facts from SUMMARY or KEY POINTS.
If significance is not directly stated, use distinct source facts that explain what the article reports; do not add consequences.

------------------------------------------------------------

WHO IS AFFECTED

Mention ONLY people, groups, organizations or locations
explicitly identified in the source.

Do NOT invent:

- tourists
- businesses
- emergency responders
- residents
- government departments
- workers
- engineers
- communities

unless the source explicitly mentions them.

Return exactly three distinct points without repeating earlier sections.
If fewer than three groups are named, use distinct source facts that identify the named people, organizations, or locations; never invent groups.

------------------------------------------------------------

WHAT HAPPENS NEXT

Mention ONLY actions or next steps explicitly stated
in the source.

Do NOT predict.

Do NOT write:

- authorities will monitor
- evacuation may happen
- rescue teams will respond
- further warnings will be issued
- investigations will begin

unless the source explicitly states those actions.

Return exactly three distinct points without repeating earlier sections.
If no next step is stated, use distinct source facts about the reported status or actions; do not predict.

============================================================
CAREER ANALYSIS
============================================================

Career information must ONLY come from the source.

If the article does not explicitly mention:

- jobs
- employment
- hiring
- careers
- professional roles
- skills
- education
- workforce
- recruitment

then return:

"career_impact": "Not stated in the article."
"career_impact_score": 0
"career_impact_level": "NONE"
"skills_to_learn": []
"relevant_job_roles": []
"career_opportunities": "Not stated in the article."
"career_recommendation": "Not stated in the article."

Do NOT create career opportunities from general knowledge.

============================================================
IMPORTANCE
============================================================

Score importance ONLY from the information contained in
the source.

Do not use outside knowledge to decide importance.

Use:

0-29   LOW
30-69  MEDIUM
70-84  HIGH
85-100 VERY HIGH

============================================================
SENTIMENT
============================================================

Use only:

POSITIVE
NEGATIVE
NEUTRAL
MIXED

Base this only on the wording of the source.

============================================================
OUTPUT
============================================================

Return ONLY valid JSON.

Do not use Markdown.

Do not add explanations outside JSON.

Each of summary, key_points, why_it_matters, who_is_affected, and
what_happens_next must contain exactly three concise, article-grounded strings.
Never repeat a point within a section. If details are limited, use distinct
source facts or shorter source-grounded details rather than generic filler.

============================================================
JSON STRUCTURE
============================================================

{{
  "summary": [
    "Point 1",
    "Point 2",
    "Point 3"
  ],

  "key_points": [
    "Point 1",
    "Point 2",
    "Point 3"
  ],

  "why_it_matters": [
    "Point 1",
    "Point 2",
    "Point 3"
  ],

  "importance_score": 0,
  "importance_level": "LOW",

  "sentiment": "NEUTRAL",

  "who_is_affected": [
    "Point 1",
    "Point 2",
    "Point 3"
  ],

  "what_happens_next": [
    "Point 1",
    "Point 2",
    "Point 3"
  ],

  "career_impact": "Not stated in the article.",

  "career_impact_score": 0,
  "career_impact_level": "NONE",

  "skills_to_learn": [],

  "relevant_job_roles": [],

  "career_opportunities": "Not stated in the article.",

  "who_should_care": "Not stated in the article.",

  "career_recommendation": "Not stated in the article."
}}

============================================================
FINAL SELF-CHECK
============================================================

Before returning the JSON:

1. Return exactly 3 concise points in each of the five sections.
2. Every point adds a distinct fact supported by the source.
3. Use short, source-grounded details when a section has limited material.
6. Every factual claim comes from the source.
7. No outside knowledge.
8. No predictions.
9. No invented people.
10. No invented organizations.
11. No invented consequences.
12. No invented career information.
13. Keep words such as "possible", "may", "warn", and
    "advised" exactly faithful to the source.
14. If a fact is missing, say:
    "Not stated in the article."
15. Valid JSON only.
"""
def call_ollama(prompt, json_mode=True, model=None, num_predict=None):

    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        # Keep the local model resident between article actions to avoid
        # paying its load time again after a short pause.
        "keep_alive": "15m",
        **({"format": "json"} if json_mode else {}),
        "options": {
            "temperature": 0.2,
            **({"num_predict": num_predict} if num_predict is not None else {}),
        },
    }

    body = json.dumps(payload).encode("utf-8")

    request = Request(
        OLLAMA_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:

        with urlopen(
            request,
            timeout=OLLAMA_TIMEOUT,
        ) as response:

            response_data = response.read()

        decoded = response_data.decode(
            "utf-8"
        )

        result = json.loads(decoded)

        return result.get(
            "response",
            "",
        )

    except HTTPError as error:

        print(
            "OLLAMA HTTP ERROR:",
            error.code,
            error.reason,
        )

    except URLError as error:

        print(
            "OLLAMA CONNECTION ERROR:",
            error.reason,
        )

    except TimeoutError:

        print(
            "OLLAMA TIMEOUT"
        )

    except Exception as error:

        print(
            "OLLAMA ERROR:",
            error,
        )

    return ""


def translate_article_text(title, description, target_language):
    """Translate an article's title and description in one model request."""

    language_codes = {
        "english": "en",
        "hindi": "hi",
        "telugu": "te",
    }
    target_code = language_codes.get(str(target_language).lower())
    if not target_code:
        return None

    prompt = f"""Translate the English news article text below into clear, natural {target_language} ({target_code}) for a general news reader.

Preserve all facts, names, places, dates, numbers, and qualifications. Do not summarize, omit details, add information, or explain the translation. Keep each field's meaning separate.

Return one JSON object with exactly these string fields: "title" and "description". Translate an empty field as an empty string. Return no commentary.

English title:
{title}

English description:
{description}
"""

    payload = {
        "model": OLLAMA_TRANSLATION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "keep_alive": "15m",
        "options": {
            "temperature": 0.1,
            "num_predict": 1600,
        },
    }
    request = Request(
        OLLAMA_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        message = result.get("message", {})
        translated = json.loads(clean_json_text(message.get("content", "")))
        translated_title = str(translated.get("title", "")).strip()
        translated_description = str(translated.get("description", "")).strip()
        if not translated_title or (description and not translated_description):
            return None
        return {
            "title": translated_title,
            "description": translated_description,
        }
    except Exception as error:
        print("OLLAMA TRANSLATION ERROR:", error)
        return None



# ============================================================
# JOBS & CAREER INTELLIGENCE
# ============================================================

def _fallback_jobs_career(articles):
    """Build conservative Jobs insights directly from the supplied articles."""
    result = {
        "government_opportunities": [],
        "private_sector_opportunities": [],
        "internships_freshers": [],
        "future_job_market": [],
        "skills_to_learn": [],
    }
    government_signals = (
        "government", "govt", "public sector", "psu", "upsc", "ssc",
        "railway", "railways", "ministry", "police recruitment", "nicl",
        "bank recruitment", "post office recruitment",
    )
    central_government_signals = (
        "central government", "union government", "ministry", "ministries",
        "upsc", "ssc", "railway recruitment", "central public sector",
        "public sector undertaking", "psu",
    )
    state_government_signals = (
        "state government", "state psc", "public service commission",
        "state police", "state health department", "state education department",
        "telangana", "andhra pradesh", "karnataka", "tamil nadu", "kerala",
        "maharashtra", "uttar pradesh", "rajasthan", "bihar", "odisha",
        "west bengal", "gujarat", "madhya pradesh", "punjab", "haryana",
        "assam", "jharkhand", "chhattisgarh", "goa", "uttarakhand",
        "himachal pradesh", "manipur", "meghalaya", "tripura", "nagaland",
        "mizoram", "sikkim", "arunachal pradesh",
    )
    excluded_signals = (
        "scholarship", "merit list", "result pdf", "admission", "revaluation",
        "answer key",
    )
    role_signals = (
        "developer", "designer", "engineer", "technician", "dentist", "nurse",
        "researcher", "foreman", "carpenter", "welder", "driver", "caretaker",
        "officer", "analyst", "teacher", "accountant", "practitioner", "doctor",
        "fire fighter", "fitter", "fabricator", "public health expert",
    )
    actionable_signals = (
        "recruitment", "vacanc", "hiring", "apply", "application", "job opening",
        "position", "post", "developer", "designer", "engineer", "technician",
        "dentist", "nurse", "researcher", "foreman", "carpenter", "welder",
        "driver", "caretaker", "officer", "analyst", "teacher", "accountant",
        "practitioner", "doctor", "internship", "intern", "apprentice",
    )
    domain_signals = {
        "Healthcare": ("dentist", "nurse", "clinical", "health", "doctor", "practitioner"),
        "Technology and design": ("developer", "software", "python", "react", "ios", "android", "ui/ux", "designer"),
        "Engineering and skilled trades": ("engineer", "welder", "hvac", "fitter", "fabricator", "carpenter", "foreman"),
        "Public administration": ("officer", "government", "public sector", "nicl", "railway"),
    }
    skill_signals = (
        "Python", "React", "Java", "JavaScript", "SQL", "UI/UX design",
        "iOS development", "Android development", "HVAC", "welding",
    )

    opportunities = []
    domain_roles = {domain: [] for domain in domain_signals}
    domain_sources = {domain: [] for domain in domain_signals}
    skill_roles = {skill: [] for skill in skill_signals}

    for article in articles[:20]:
        title = str(article.get("title", "")).strip()
        description = str(article.get("description", "")).strip()
        source = str(article.get("source", "")).strip()
        text = f"{title} {description}".lower()
        if not title or any(signal in text for signal in excluded_signals):
            continue
        if not any(signal in text for signal in actionable_signals):
            continue

        is_internship = any(term in text for term in ("internship", "intern ", "apprentice", "freshers"))
        is_government = any(signal in text for signal in government_signals)
        has_role = any(signal in text for signal in role_signals)
        details = description or "See the source article for the listed role and application details."
        item = {"title": title, "details": details[:500], "source": source}

        if is_internship:
            result["internships_freshers"].append(item)
        elif is_government and any(term in text for term in ("recruitment", "vacanc", "hiring", "apply", "application")):
            if any(signal in text for signal in state_government_signals):
                item["scope"] = "state"
            elif any(signal in text for signal in central_government_signals):
                item["scope"] = "central"
            else:
                item["scope"] = "general"
            result["government_opportunities"].append(item)
        elif has_role:
            result["private_sector_opportunities"].append(item)
        else:
            continue

        for domain, signals in domain_signals.items():
            if any(signal in text for signal in signals):
                domain_roles[domain].append(title)
                if source and source not in domain_sources[domain]:
                    domain_sources[domain].append(source)

        for skill in skill_signals:
            if skill.lower() in text and title not in skill_roles[skill]:
                skill_roles[skill].append(title)

    for key in ("government_opportunities", "private_sector_opportunities", "internships_freshers"):
        result[key] = result[key][:5]

    for domain, roles in domain_roles.items():
        unique_roles = list(dict.fromkeys(roles))
        if len(unique_roles) < 2:
            continue
        result["future_job_market"].append({
            "area": domain,
            "trend": "Current Jobs articles report openings in this area; this describes the supplied listings, not a long-term forecast.",
            "potential_roles": unique_roles[:5],
            "source": ", ".join(domain_sources[domain][:3]),
        })

    for skill, roles in skill_roles.items():
        if roles:
            result["skills_to_learn"].append({
                "skill": skill,
                "why_it_matters": "This skill is named in current job listings.",
                "related_roles": roles[:4],
            })

    result["future_job_market"] = result["future_job_market"][:4]
    result["skills_to_learn"] = result["skills_to_learn"][:6]
    return result

def analyze_jobs_career(articles):
    """
    Analyze the already-balanced Jobs articles and produce
    simple career intelligence for the Jobs section.

    Startup content and career roadmaps are intentionally excluded.
    """

    if not articles:
        return {
            "government_opportunities": [],
            "private_sector_opportunities": [],
            "internships_freshers": [],
            "future_job_market": [],
            "skills_to_learn": [],
        }

    article_lines = []

    for index, article in enumerate(articles[:10], 1):

        title = str(
            article.get("title", "")
        ).strip()

        description = str(
            article.get("description", "")
        ).strip()

        source = str(
            article.get("source", "")
        ).strip()

        if not title:
            continue

        article_lines.append(
            f"{index}. SOURCE: {source}\n"
            f"TITLE: {title}\n"
            f"DESCRIPTION: {description[:200]}"
        )

    articles_text = "\n\n".join(article_lines)

    prompt = f"""
You are a Jobs and Career Intelligence analyst covering
Indian state and central government recruitment and private-sector openings worldwide.

Analyze the following current Jobs articles.

ARTICLES:
{articles_text}

Your task is to identify useful employment and career
information for students, freshers and working professionals.

IMPORTANT:

1. Focus ONLY on:
   - State government employment opportunities in India
   - Central government employment opportunities in India
   - Private-sector employment opportunities from all countries
   - Internships
   - Fresher opportunities
   - Hiring trends
   - Future job-market opportunities
   - Skills that people should learn

2. DO NOT include:
   - Startup news
   - Startup funding
   - Startup launches
   - Entrepreneurship news
   - Startup ecosystem analysis
   - Career roadmaps
   - Step-by-step learning paths

3. DO NOT invent:
   - job openings
   - salaries
   - companies
   - application deadlines
   - eligibility requirements
   - statistics
   - market forecasts

4. Only mention specific opportunities when the supplied
   articles support them.

4a. For every government opportunity, include a "scope" field whose value is
    exactly "state", "central", or "general". Use "state" only when a state
    government or state-level recruiting body is identified. Use "central" only
    when a Union ministry, central agency, UPSC, SSC, or central public employer
    is identified. Use "general" if the article does not establish the level.
    Private-sector items should identify the country or location when the article
    provides it; do not restrict them to India.

5. Clearly distinguish current opportunities from
   broader market trends.

6. Skills should be practical and connected to the
   employment trends found in the articles.

7. Return ONLY valid JSON.
   Do not use Markdown.
   Do not add explanations outside JSON.

8. VERY IMPORTANT:
   Return EXACTLY these five top-level JSON keys.
   Do NOT create any other keys.

Use exactly this structure:

{{
  "government_opportunities": [
    {{
      "title": "Opportunity or trend",
      "scope": "state, central, or general",
      "organization": "Organization name if supported",
      "details": "What the articles indicate",
      "source": "Source if supported"
    }}
  ],

  "private_sector_opportunities": [
    {{
      "title": "Opportunity or trend",
      "company": "Company name if supported",
      "details": "What the articles indicate",
      "source": "Source if supported"
    }}
  ],

  "internships_freshers": [
    {{
      "title": "Opportunity or trend",
      "organization": "Organization name if supported",
      "details": "What the articles indicate",
      "source": "Source if supported"
    }}
  ],

  "future_job_market": [
    {{
      "area": "Career area",
      "trend": "Evidence-based employment trend",
      "potential_roles": [
        "Role 1",
        "Role 2"
      ],
      "source": "Source if supported"
    }}
  ],

  "skills_to_learn": [
    {{
      "skill": "Skill name",
      "why_it_matters": "Why this skill is relevant",
      "related_roles": [
        "Role 1",
        "Role 2"
      ]
    }}
  ]
}}

RULES FOR LISTS:

- Keep each list concise.
- Prefer 2-5 useful items per section.
- Do not repeat the same opportunity unnecessarily.
- If there is insufficient evidence for a section,
  return an empty list.
- Never fabricate information.
- NEVER return career_roadmaps.
- NEVER return any additional top-level field.
"""

    print("=" * 70)
    print("JOBS & CAREER INTELLIGENCE")
    print("=" * 70)
    print("MODEL:", OLLAMA_MODEL)
    print("ARTICLES ANALYZED:", len(articles))

    _jobs_ollama_start = time.perf_counter()

    raw_response = call_ollama(prompt, num_predict=700)

    print(
        "TIMING JOBS OLLAMA:",
        f"{time.perf_counter() - _jobs_ollama_start:.2f}s"
    )

    if not raw_response:
        print("JOBS AI RETURNED NO RESPONSE")
        return {
            "government_opportunities": [],
            "private_sector_opportunities": [],
            "internships_freshers": [],
            "future_job_market": [],
            "skills_to_learn": [],
        }

    try:
        result = json.loads(raw_response)

        if not isinstance(result, dict):
            result = {}

    except Exception as error:
        print("JOBS AI JSON ERROR:", error)
        result = {}

    # --------------------------------------------------------
    # FORCE EXACTLY FIVE SECTIONS
    # --------------------------------------------------------
    # Ollama can occasionally return extra fields even when
    # the prompt explicitly forbids them. Build a new object
    # containing ONLY the five fields required by the website.
    # --------------------------------------------------------

    result = {
        "government_opportunities":
            result.get("government_opportunities", []),

        "private_sector_opportunities":
            result.get("private_sector_opportunities", []),

        "internships_freshers":
            result.get("internships_freshers", []),

        "future_job_market":
            result.get("future_job_market", []),

        "skills_to_learn":
            result.get("skills_to_learn", []),
    }

    # A model may return valid JSON with every section empty. Fill only those
    # gaps from explicit roles, skills, and sources in the current articles.
    fallback = _fallback_jobs_career(articles)
    for key, items in fallback.items():
        if not isinstance(result.get(key), list) or not result[key]:
            result[key] = items

    print(
        "Government:",
        len(result["government_opportunities"])
    )

    print(
        "Private:",
        len(result["private_sector_opportunities"])
    )

    print(
        "Internships:",
        len(result["internships_freshers"])
    )

    print(
        "Future market:",
        len(result["future_job_market"])
    )

    print(
        "Skills:",
        len(result["skills_to_learn"])
    )

    print("JOBS AI ANALYSIS COMPLETE")

    return result

# ============================================================
# MAIN SUMMARY FUNCTION
# ============================================================

def explain_like_im_10(title, description=""):
    """
    Explain an article in simple, natural English without
    adding information that is not present in the source.
    """

    title = str(title or "").strip()
    description = str(description or "").strip()

    if not title and not description:
        return ""

    prompt = f"""
You are a professional news editor.

Rewrite the following news information so that a
10-year-old can understand it.

IMPORTANT:
You are SIMPLIFYING the original information.
You are NOT expanding, interpreting, or adding to it.

FACTUAL ACCURACY IS MORE IMPORTANT THAN DETAIL.

SOURCE TITLE:
{title}

SOURCE DESCRIPTION:
{description}

STRICT RULES:

1. Use ONLY information explicitly stated in the source.
2. Do NOT add any new facts.
3. Do NOT infer possible consequences.
4. Do NOT predict what might happen.
5. Do NOT explain background information.
6. Do NOT explain basic words.
7. Do NOT invent who may be affected.
8. Do NOT invent damage, risks, causes or outcomes.
9. Do NOT change the meaning of the source.
10. Preserve names, places, organizations and numbers exactly.
11. "Kottayam" must remain "Kottayam".
12. "Officials" must remain "officials".
13. Do not replace "officials" with "government",
    "police", "authorities", or another group unless
    that exact group appears in the source.

LANGUAGE:

- Use simple vocabulary.
- Use normal, natural English.
- Use correct grammar.
- Do not use childish language.
- Do not use phrases such as:
  "water from the sky"
  "a place called"
  "people who take care of the area"
  "the people in charge"
- Return exactly 2 complete, informative sentences as plain prose.
- The display places one sentence on each line; make both lines similarly full.
- Aim for 22–27 words per sentence (44–54 words total) when the source supports that much detail.
- Keep the two sentences close in length; do not put nearly all details in the first sentence.
- Sentence 1 explains the main event and preserves important names, actions, and dates.
- Sentence 2 adds a different supported detail, context, or stated next step; do not restate sentence 1.
- If the source is too brief, use its available facts without padding or guessing.
- Do NOT use bullets, dashes, numbering, labels, or list formatting.

VERY IMPORTANT:

Every factual statement in your answer must be directly
supported by the source title or description.

If the source only provides two facts, explain those
two facts clearly. DO NOT create a third fact just to
make the explanation longer.

Return ONLY the explanation.
"""

    try:
        result = call_ollama(
            prompt,
            json_mode=False,
            num_predict=100,
        )

        if isinstance(result, dict):
            explanation = (
                result.get("response")
                or result.get("text")
                or result.get("content")
                or ""
            )
        else:
            explanation = str(result or "")

        return explanation.strip()

    except Exception as error:
        print("ELI10 ERROR:", error)
        # Keep the Explain action useful when the local model is unavailable.
        # The UI formats this source-only text into two display lines.
        return description or title

def summarize_news(title, description=""):

    title = str(
        title or ""
    ).strip()

    description = str(
        description or ""
    ).strip()

    if not title:

        return default_analysis(
            title,
            description,
        )

    print("=" * 70)
    print("NEWSROOM AI ANALYSIS")
    print("=" * 70)
    print("MODEL:", OLLAMA_MODEL)
    print("TITLE:", title)
    print("=" * 70)

    prompt = build_prompt(
        title,
        description,
    )

    raw_response = call_ollama(
        prompt,
        num_predict=650,
    )

    if not raw_response:

        print(
            "OLLAMA RETURNED NO RESPONSE"
        )

        return default_analysis(
            title,
            description,
        )

    print(
        "OLLAMA RESPONSE RECEIVED"
    )

    # --------------------------------------------------------
    # CLEAN RESPONSE
    # --------------------------------------------------------

    cleaned_response = clean_json_text(
        raw_response
    )

    # --------------------------------------------------------
    # PARSE JSON
    # --------------------------------------------------------

    try:

        parsed = json.loads(
            cleaned_response
        )

    except json.JSONDecodeError as error:

        print(
            "JSON PARSE ERROR:",
            error,
        )

        # Try to extract the JSON object
        # if Ollama added extra text.
        match = re.search(
            r"\{.*\}",
            cleaned_response,
            flags=re.DOTALL,
        )

        if match:

            try:

                parsed = json.loads(
                    match.group(0)
                )

            except json.JSONDecodeError:

                print(
                    "JSON EXTRACTION FAILED"
                )

                return default_analysis(
                    title,
                    description,
                )

        else:

            return default_analysis(
                title,
                description,
            )

    # --------------------------------------------------------
    # NORMALIZE
    # --------------------------------------------------------

    analysis = normalize_analysis(
        parsed,
        title,
        description,
    )

    print(
        "AI ANALYSIS COMPLETE"
    )

    print(
        "IMPORTANCE:",
        analysis["importance_score"],
        analysis["importance_level"],
    )

    print(
        "CAREER IMPACT:",
        analysis["career_impact_score"],
        analysis["career_impact_level"],
    )

    print(
        "SKILLS:",
        len(analysis["skills_to_learn"]),
    )

    print(
        "JOB ROLES:",
        len(analysis["relevant_job_roles"]),
    )

    print("=" * 70)

    return analysis

    # ============================================================
# COMPARE ARTICLES
# ============================================================

# ============================================================
# COMPARE ARTICLES
# ============================================================

def compare_articles(article1, article2):
    """
    Compare two news articles using only information
    explicitly provided in the articles.
    """

    article1 = article1 or {}
    article2 = article2 or {}

    title1 = str(
        article1.get("title", "") or ""
    ).strip()

    description1 = str(
        article1.get("description", "") or ""
    ).strip()

    title2 = str(
        article2.get("title", "") or ""
    ).strip()

    description2 = str(
        article2.get("description", "") or ""
    ).strip()

    if not title1 and not description1:
        return {}

    if not title2 and not description2:
        return {}

    prompt = f"""
You are a professional news comparison editor.

Compare these two news articles using ONLY the information
explicitly provided in their titles and descriptions.

ARTICLE 1
Title:
{title1}

Description:
{description1}

ARTICLE 2
Title:
{title2}

Description:
{description2}

STRICT RULES:

1. Use only information explicitly stated in the articles.
2. Do not add outside facts or background information.
3. Do not make assumptions.
4. Do not predict future events.
5. Do not invent causes, consequences, opinions, or motives.
6. Identify factual points shared by both articles.
7. Identify factual differences between the articles.
8. If a fact appears only in one article, mention that clearly.
9. Preserve names, places, organizations and numbers exactly.
10. Do not rank the articles.
11. Keep the comparison neutral and factual.

Return ONLY valid JSON.

Use exactly this structure:

{{
    "common_points": [],
    "differences": [],
    "article1_focus": "",
    "article2_focus": ""
}}

Every factual statement must be directly supported by
the supplied article title or description.
"""

    try:

        raw_response = call_ollama(
            prompt,
            json_mode=True
        )

        if not raw_response:
            print(
                "COMPARE ARTICLES: Ollama returned empty response"
            )
            return {}

        print(
            "COMPARE ARTICLES RAW RESPONSE:",
            raw_response
        )

        cleaned_response = clean_json_text(
            str(raw_response)
        )

        try:

            parsed = json.loads(
                cleaned_response
            )

            if isinstance(parsed, dict):
                return parsed

        except json.JSONDecodeError:

            print(
                "COMPARE ARTICLES JSON PARSE ERROR"
            )

            match = re.search(
                r"\{.*\}",
                cleaned_response,
                flags=re.DOTALL
            )

            if match:

                try:

                    parsed = json.loads(
                        match.group(0)
                    )

                    if isinstance(parsed, dict):
                        return parsed

                except json.JSONDecodeError as error:

                    print(
                        "COMPARE ARTICLES JSON ERROR:",
                        error
                    )

        return {}

    except Exception as error:

        print(
            "COMPARE ARTICLES ERROR:",
            error
        )

        return {}

def translate_telugu_to_english(text):
    """
    Translate Telugu news text into clear, natural English using TranslateGemma.
    """

    text = str(text or "").strip()

    if not text:
        return ""

    prompt = f"""
You are a professional Telugu-to-English news translator.

Translate ONLY the Telugu text below into clear, natural, professional English.

This is a FACTUAL NEWS TRANSLATION task.

STRICT RULES:
- Preserve the exact meaning and factual information.
- Do NOT invent, guess, replace or modify any person's name.
- Telugu person names must be transliterated faithfully into English.
- Example: "దానం నాగేందర్" must be "Danam Nagender".
- NEVER replace a person's name with another name.
- NEVER add a city, state, country or location that is not present.
- If the Telugu text says "High Court", translate it as "High Court".
- Do NOT add "Hyderabad High Court" unless Hyderabad is explicitly present.
- Preserve political party names and acronyms such as BRS, BJP, Congress, TDP and YSRCP.
- Preserve organization and institution names.
- Preserve place names.
- Preserve abbreviations such as MLA, MP, CM, PM, AP and Telangana.
- Preserve every number, date, percentage, amount and currency value.
- Do NOT change who did what to whom.
- Do NOT change the relationship between people, organizations or events.
- Do NOT infer missing information.
- Do NOT add background information.
- Do NOT summarize.
- Telugu news headlines may contain wordplay, rhetorical questions, repeated words,
  conversational expressions or dramatic sentence structures.
- When a Telugu headline uses such an expression, translate its INTENDED MEANING
  naturally into professional English instead of translating every word literally.
- Make the English headline sound like a real professional news headline.
- Keep all factual information from the original.
- Do NOT invent information to make the headline sound better.
- Do NOT add sensational wording that is absent from the original.
- Return ONLY the English translation.
- Do NOT use quotation marks around the answer.

TELUGU TEXT:
{text}
"""

    translated = call_ollama(
        prompt,
        json_mode=False,
        model="translategemma:4b-it-q4_K_M"
    )

    if translated:
        translated = translated.strip()

        if len(translated) >= 2:
            if (
                (translated.startswith('"') and translated.endswith('"'))
                or
                (translated.startswith("'") and translated.endswith("'"))
            ):
                translated = translated[1:-1].strip()

        if translated:
            # ------------------------------------------------
            # Basic translation safety checks.
            # Prevent obvious hallucinated substitutions.
            # ------------------------------------------------
            lower_translation = translated.lower()

            unsafe_terms = [
                "polar bear",
                "polar bears",
            ]

            if any(term in lower_translation for term in unsafe_terms):
                print("TRANSLATION SAFETY CHECK FAILED")
                print("UNSAFE TRANSLATION:", translated)
                return text

            print("TELUGU -> ENGLISH TRANSLATION COMPLETE")
            return translated

    print("TELUGU -> ENGLISH TRANSLATION FAILED")
    return text

# ============================================================
# DIRECT TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 70)
    print("THE NEWSROOM - OLLAMA CAREER INTELLIGENCE TEST")
    print("=" * 70)

    test_title = (
        "NASA announces new AI-powered space research mission"
    )

    test_description = (
        "NASA is developing artificial intelligence systems "
        "to support space research, scientific analysis, "
        "and future missions."
    )

    result = summarize_news(
        test_title,
        test_description,
    )

    print()
    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )

    print()
    print("=" * 70)
    print("OLLAMA CAREER INTELLIGENCE TEST COMPLETE")
    print("=" * 70)
