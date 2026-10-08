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

def default_analysis(title="", description=""):
    return {
"topics": [
    "Topic 1",
    "Topic 2",
    "Topic 3"
],

        "summary": (
            "AI analysis could not be generated for this article."
        ),
        "key_points": [
            "The article was received by the local newsroom.",
            "Ollama was unable to provide the complete analysis.",
        ],
        "why_it_matters": (
            "The importance of this article depends on its subject "
            "and potential impact on people, industries, or society."
        ),
        "importance_score": 50,
        "importance_level": "MEDIUM",
        "sentiment": "NEUTRAL",
        "who_is_affected": (
            "People, organizations, industries, or communities "
            "connected to the topic of the article."
        ),
        "what_happens_next": (
            "Further developments will depend on the events "
            "described in the article."
        ),
        "career_impact": (
            "The career impact depends on the article's topic "
            "and its effect on industries and technology."
        ),
        "career_impact_score": 50,
        "career_impact_level": "MEDIUM",
        "skills_to_learn": [],
        "relevant_job_roles": [],
        "career_opportunities": (
            "No specific career opportunities could be identified."
        ),
        "who_should_care": (
            "Students and professionals interested in the article's topic."
        ),
        "career_recommendation": (
            "Follow developments in this field and build relevant skills."
        ),
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

    def build_source_facts():
        """
        Create simple factual fallback points directly from
        the supplied title and description.
        """

        import re

        source_facts = []

        title_text = str(title or "").strip()
        description_text = str(description or "").strip()

        if title_text:
            source_facts.append(
                title_text
            )

        # Split the description into sentences.
        sentences = re.split(
            r"(?<=[.!?])\s+",
            description_text
        )

        for sentence in sentences:

            sentence = sentence.strip()

            if sentence:
                source_facts.append(
                    sentence
                )

        # Also split long sentences around semicolons.
        expanded = []

        for fact in source_facts:

            parts = re.split(
                r"\s*;\s*",
                fact
            )

            for part in parts:

                part = part.strip()

                if part:
                    expanded.append(part)

        # Remove duplicates.
        unique = []

        for fact in expanded:

            normalized = fact.lower().strip()

            if normalized not in {
                x.lower().strip()
                for x in unique
            }:
                unique.append(fact)

        return unique


    source_facts = build_source_facts()


    def clean_points(value):

        points = normalize_three_points(
            value,
            fallback=source_facts
        )

        cleaned = []

        for point in points:

            point = str(point or "").strip()

            if not point:
                continue

            if (
                len(point) >= 2
                and point[0] == "'"
                and point[-1] == "'"
            ):
                point = point[1:-1].strip()

            if (
                len(point) >= 2
                and point[0] == '"'
                and point[-1] == '"'
            ):
                point = point[1:-1].strip()

            if point:
                cleaned.append(point)

        # Absolute guarantee.
        while len(cleaned) < 3:

            if source_facts:
                cleaned.append(
                    source_facts[
                        len(cleaned) % len(source_facts)
                    ]
                )
            else:
                cleaned.append(
                    "The article reports the information described in its source."
                )

        return cleaned[:3]

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

    # Remove repeated ideas across headings as well as repeated bullets
    # within one heading. When a generated point repeats an earlier fact,
    # replace it with the next distinct source sentence available.
    section_names = (
        "summary",
        "key_points",
        "why_it_matters",
        "who_is_affected",
        "what_happens_next",
    )
    section_values = {
        "summary": summary,
        "key_points": key_points,
        "why_it_matters": why_it_matters,
        "who_is_affected": who_is_affected,
        "what_happens_next": what_happens_next,
    }
    used_points = []

    def point_tokens(value):
        return {
            token for token in re.findall(r"[a-z0-9]+", str(value).lower())
            if len(token) > 2 and token not in {
                "the", "and", "for", "from", "with", "that", "this",
                "was", "were", "has", "have", "will", "are", "its",
                "their", "they", "about", "into", "after", "before",
            }
        }

    def repeats_existing(value):
        current = point_tokens(value)
        if not current:
            return True
        for prior in used_points:
            prior_tokens = point_tokens(prior)
            overlap = len(current & prior_tokens) / max(1, len(current | prior_tokens))
            if value.strip().casefold() == prior.strip().casefold() or overlap >= 0.78:
                return True
        return False

    source_fact_index = 0
    for section_name in section_names:
        clean_section = []
        for point in section_values[section_name]:
            candidate = point
            if repeats_existing(candidate):
                candidate = ""
                while source_fact_index < len(source_facts):
                    source_fact = source_facts[source_fact_index]
                    source_fact_index += 1
                    if not repeats_existing(source_fact):
                        candidate = source_fact
                        break
            if not candidate:
                # Retain the model's fact rather than manufacture details
                # when the supplied source itself has no distinct facts.
                candidate = point
            clean_section.append(candidate)
            used_points.append(candidate)
        section_values[section_name] = clean_section[:3]

    summary = section_values["summary"]
    key_points = section_values["key_points"]
    why_it_matters = section_values["why_it_matters"]
    who_is_affected = section_values["who_is_affected"]
    what_happens_next = section_values["what_happens_next"]

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
    Always return exactly 3 useful bullet points.

    If Ollama returns fewer than 3 points, use source-derived
    fallback information rather than placeholder text.
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

        normalized = re.sub(
            r"\s+",
            " ",
            point.lower()
        ).strip()

        if normalized and normalized not in {
            re.sub(r"\s+", " ", x.lower()).strip()
            for x in unique
        }:
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

        normalized = re.sub(
            r"\s+",
            " ",
            item.lower()
        ).strip()

        existing = {
            re.sub(
                r"\s+",
                " ",
                x.lower()
            ).strip()
            for x in points
        }

        if normalized not in existing:
            points.append(item)

    # --------------------------------------------------------
    # LAST RESORT: SAFE REPHRASING
    # --------------------------------------------------------

    # If the article only contains one or two usable facts,
    # reuse those facts with clear wording rather than inventing
    # a new fact.

    if len(points) == 1:

        original = points[0]

        points.append(
            f"The article also reports that {original.rstrip('.') }."
        )

        points.append(
            f"The report specifically mentions: {original.rstrip('.') }."
        )

    elif len(points) == 2:

        original = points[0]

        points.append(
            f"The article also highlights that {original.rstrip('.') }."
        )

    # --------------------------------------------------------
    # ABSOLUTE GUARANTEE: EXACTLY 3
    # --------------------------------------------------------

    if len(points) == 0:

        points = [
            "The article reports the information described in its source.",
            "The article provides the reported details shown above.",
            "The article contains no additional details in the supplied text."
        ]

    while len(points) < 3:

        points.append(
            points[len(points) % len(points)]
        )

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

NEVER invent a fact simply because a section requires
three bullet points.

It is ALWAYS better to write:

"Not stated in the article."

than to invent information.

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

SUMMARY

Return exactly three short, source-grounded bullet points.
Use the overall event and its main details. Keep each point distinct.

KEY POINTS

Return exactly three additional concrete details from the source.
Do not repeat or rephrase any fact already used in SUMMARY.
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
If the source does not state significance, use three different concise statements
that identify what significance is not stated; do not invent an impact.

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
If fewer groups are named, use concise statements that identify the missing
specific group details; do not invent groups.

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
If fewer future actions are stated, identify which next-step details are absent;
do not predict or repeat facts from other sections.

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

Exactly three distinct strings are required in each of summary, key_points,
why_it_matters, who_is_affected, and what_happens_next.
Never repeat or paraphrase a point across sections. If a section lacks enough
source material, use a distinct section-specific note about what is not stated;
never invent facts or reuse an earlier point.

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

1. Exactly 3 summary bullets.
2. Exactly 3 key points.
3. Exactly 3 why-it-matters bullets.
4. Exactly 3 affected-group bullets.
5. Exactly 3 next-step bullets.
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
You are a Jobs and Career Intelligence analyst
inside a local Indian news application.

Analyze the following current Jobs articles.

ARTICLES:
{articles_text}

Your task is to identify useful employment and career
information for students, freshers and working professionals.

IMPORTANT:

1. Focus ONLY on:
   - Government employment opportunities
   - Private-sector employment opportunities
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
        return ""

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
        num_predict=900,
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
