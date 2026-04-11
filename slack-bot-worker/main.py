import os
import json
import base64
import yaml
from datetime import datetime, timedelta

import vertexai
from flask import Flask, request
from vertexai.generative_models import GenerativeModel
from slack_sdk import WebClient
from google.cloud import bigquery
import time

app = Flask(__name__)

# --- Clients ---
bq_client = bigquery.Client(project=os.environ.get("GCP_PROJECT_ID"))
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
vertexai.init(project=os.environ.get("GCP_PROJECT_ID"), location="us-central1")
# model = GenerativeModel("gemini-1.5-flash") #changed to lower version of the model to get higher quota
model = GenerativeModel("gemini-2.5-flash")

# --- Load schema and sample prompts ---
def _load_file(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""

dbt_schema_text = _load_file("model.yml")

def load_sample_prompts():
    raw = _load_file("sample_prompts.yml")
    if not raw:
        return []
    try:
        return yaml.safe_load(raw).get("prompts", [])
    except Exception:
        return []

SAMPLE_PROMPTS = load_sample_prompts()

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = f"""
You are a BigQuery data assistant. Answer using the SCHEMA below.

RESPONSE MODES — prefix response with exactly one tag:
1. SQL| — For data/metrics. Write a BigQuery SELECT query.
2. MSG| — For schema/definitions. Reply in plain text.

RULES:
- SELECT ONLY. No DML/DDL.
- PARTITION FILTER: Both tables partitioned by fulfillment_date. EVERY query MUST filter on it. Default to >= '2026-01-01'.
- DO NOT alias columns. Return raw column names.
- Round dollar amounts to 2 decimals.
- TOP N PER GROUP: NEVER use QUALIFY with aggregate functions. Use a CTE: WITH agg AS (SELECT col, SUM(x) AS x FROM t GROUP BY col) SELECT * FROM agg QUALIFY ROW_NUMBER() OVER(ORDER BY x DESC) <= N.

TABLE RULES:
1. `valor-sales.valor_margin_dbt.fct_valor_margin` — Valor-only data.
2. `valor-sales.valor_margin_dbt.mart_grouped_margin` — Combined STLTH + Valor data.
- Default to mart_grouped_margin unless user asks for "Valor only".
- Always state data source used (Valor only or STLTH+Valor) in first line as a SQL comment or MSG text.
- If the user explicitly asks for "ALL", or a filter value is "ALL", DO NOT apply a WHERE clause for that field.

MATCHING & GROUPING:
- Category filters: ALWAYS filter on category_formatted.
- Brand identifiers: ALWAYS use brand_category_key to select or group by brand.
- SKU aggregations: ALWAYS use sku_base (or valor_sku_base). NEVER select or group by the raw 'sku' column unless the user explicitly asks for 'stamp' or 'excise'.
- Product aggregations: ALWAYS use product_title_grouped (or valor_product_title_grouped).
- ALWAYS use case-insensitive fuzzy matching: LOWER(col) LIKE LOWER('%term%'). NEVER autocorrect or change the spelling of the user's search terms. If they type STLTH, use '%stlth%', do NOT change it to '%stlh%'. Copy their exact terminology.
- ML sizes: filter on the sku_ML column using IN or =. It strictly accepts numbers only (e.g., sku_ML IN (30, 55)). NEVER include 'ml' strings.
- Nicotine/color: filter on sku_variant_title using fuzzy matching.
- Provinces: Map common abbreviations (AB, BC, ON) to full names.
- Excise/Stamp: ALWAYS use fuzzy matching (LOWER(excise_type) LIKE '%fd%'). NEVER use exact match.

METRIC DEFAULTING:
- "Sales" defaults to net_sales3. "Margin/Profit" defaults to margin3.
- Only use quantity when explicitly asked (net_qty for fct_valor_margin, unit_sold for mart_grouped_margin).

SCHEMA:
{dbt_schema_text}
"""


# --- Formatting helpers ---
MAX_DISPLAY_ROWS = 50
SLACK_MSG_LIMIT = 3500  # leave room for header text

def format_table(rows, headers):
    """Format query results as a clean fixed-width text table for Slack."""
    if not rows:
        return "No results returned."

    # Format cell values
    def fmt(val):
        if val is None:
            return "-"
        if isinstance(val, float):
            return f"{{:,.2f}}".format(val)
        if isinstance(val, int):
            return f"{{:,}}".format(val)
        return str(val)

    str_rows = [[fmt(v) for v in row] for row in rows[:MAX_DISPLAY_ROWS]]
    col_widths = [max(len(h), max((len(r[i]) for r in str_rows), default=0)) for i, h in enumerate(headers)]

    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    hdr = "|" + "|".join(f" {{:<{w}}} ".format(h) for h, w in zip(headers, col_widths)) + "|"
    body_lines = []
    for r in str_rows:
        line = "|" + "|".join(f" {{:<{w}}} ".format(r[i]) for i, w in enumerate(col_widths)) + "|"
        body_lines.append(line)

    table_str = "\n".join([sep, hdr, sep] + body_lines + [sep])

    if len(rows) > MAX_DISPLAY_ROWS:
        table_str += f"\n... showing {MAX_DISPLAY_ROWS} of {len(rows)} rows"

    # Truncate if too long for Slack
    if len(table_str) > SLACK_MSG_LIMIT:
        table_str = table_str[:SLACK_MSG_LIMIT] + "\n... (truncated)"

    return table_str




def summarize_results(user_question, headers, rows):
    """Ask the AI to write a short plain-English summary of the query results."""
    if not rows:
        return "The query returned no data for the specified criteria."

    # Build a compact preview of the data (max 20 rows)
    preview_rows = rows[:20]
    preview = ", ".join(headers) + "\n"
    for r in preview_rows:
        preview += ", ".join(str(v) for v in r) + "\n"

    summary_prompt = (
        "You are a data analyst writing a short summary for a non-technical audience.\n"
        "Do not use emoji. Do not use markdown formatting. Keep it under 100 words.\n"
        "Do not repeat raw numbers unnecessarily; highlight the key takeaway.\n\n"
        f"User question: {user_question}\n\n"
        f"Query results ({len(rows)} rows total):\n{preview}\n\n"
        "Write a brief, clear summary:"
    )
    try:
        resp = generate_content_with_retry(summary_prompt)
        return resp.text.strip()
    except Exception:
        return ""


# --- Filter hint builder for sample prompt submissions ---
# Maps parameter names to the BigQuery column they filter on.
PARAM_COLUMN_MAP = {
    "brand_selection": "brand_category_key",
    "brand": "brand_category_key",
    "categories": "category_formatted",
    "province": "shipping_province",
    "stamp_type": "excise_type",
    "sku_ML": "sku_ML",
    "product_name_group": "product_title_grouped",
    "product_name_group_valor": "valor_product_title_grouped",
}

def build_filter_hint(submitted_params, prompt_params):
    """Build pre-built SQL WHERE fragments from modal-submitted values.

    Only includes parameters that came from external/multi_external selects
    (i.e., values sourced from BigQuery) and are not 'ALL'.
    Returns ready-to-use SQL conditions so the AI doesn't interpret the values.
    """
    if not submitted_params or not prompt_params:
        return None

    # Build a set of param names that are external selects
    external_params = {
        p["name"] for p in prompt_params
        if p["type"] in ("external", "multi_external")
    }

    fragments = []
    for name, value in submitted_params.items():
        if name not in external_params:
            continue
        if not value or value.upper() == "ALL":
            continue
        col = PARAM_COLUMN_MAP.get(name, name)
        # Multi-select values are comma-separated
        values = [v.strip() for v in value.split(",")]

        # The stamp_type database values are literally prepended with a single quote (e.g. "'-AB")
        if name == "stamp_type":
            values = [f"'{v}" if not v.startswith("'") else v for v in values]

        if len(values) == 1:
            if col == "sku_ML":
                fragments.append(f"{col} = {values[0]}")
            else:
                v_esc = values[0].replace("'", "\\'")
                fragments.append(f"{col} = '{v_esc}'")
        else:
            if col == "sku_ML":
                in_list = ", ".join(values)
            else:
                esc_values = [v.replace("'", "\\'") for v in values]
                in_list = ", ".join(f"'{v}'" for v in esc_values)
            fragments.append(f"{col} IN ({in_list})")

    if not fragments:
        return None

    return (
        "MANDATORY WHERE CLAUSES — copy these SQL fragments EXACTLY into your "
        "WHERE clause. Do NOT alter, rephrase, or use LIKE instead:\n"
        + "\n".join(f"- {f}" for f in fragments)
    )



def generate_content_with_retry(prompt, max_retries=4):
    """Wrapper around model.generate_content with exponential backoff for 429 errors."""
    for attempt in range(max_retries):
        try:
            return model.generate_content(prompt)
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                # Exponential backoff: 2s, 4s, 8s...
                time.sleep(2 ** (attempt + 1))
                continue
            raise e


# --- Help / sample prompts ---
def build_help_blocks():
    """Build Slack Block Kit blocks for the help dropdown."""
    options = []
    for p in SAMPLE_PROMPTS:
        options.append({
            "text": {"type": "plain_text", "text": p["label"][:75]},
            "value": p["id"]
        })

    if not options:
        return [{"type": "section", "text": {"type": "mrkdwn",
                "text": "No sample prompts configured."}}]

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "Here are some sample questions. Pick one from the list below, or just type after @DataBot"}
        },
        {
            "type": "actions",
            "elements": [{
                "type": "static_select",
                "placeholder": {"type": "plain_text", "text": "Choose a question..."},
                "action_id": "sample_prompt_select",
                "options": options[:25]
            }]
        }
    ]
    return blocks


def fill_template(prompt_def):
    """Fill a sample prompt template with default parameter values."""
    template = prompt_def.get("template", "")
    today = datetime.utcnow().date()
    for param in prompt_def.get("parameters", []):
        name = param["name"]
        if param["type"] == "date":
            raw = param.get("default", "")
            if raw == "yesterday":
                val = (today - timedelta(days=1)).isoformat()  # T-1, DB refreshes daily
            elif raw:
                val = raw  # fixed date string like "2026-01-01"
            else:
                val = today.isoformat()
        elif param["type"] == "number":
            val = str(param.get("default", 10))
        else:
            val = param.get("default", param.get("placeholder", ""))
        template = template.replace("{" + name + "}", val)
    return template


# --- Shared query processing ---
def process_question(question, channel_id, thread_ts, filter_hint=None):
    """Run the AI pipeline: generate SQL or message, execute, format, summarize."""
    try:
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text="Looking into it..."
        )

        if filter_hint:
            final_prompt = (
                f"{SYSTEM_PROMPT}\n\n"
                f"{filter_hint}\n\n"
                f"User Question: {question}\nResponse:"
            )
        else:
            final_prompt = f"{SYSTEM_PROMPT}\n\nUser Question: {question}\nResponse:"
        ai_response = generate_content_with_retry(final_prompt)
        raw_ai_text = ai_response.text.strip()

        # --- Route: MSG (schema / conversational) ---
        if raw_ai_text.startswith("MSG|"):
            clean_msg = raw_ai_text[4:].strip()
            slack_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=clean_msg
            )
            return

        # --- Route: SQL ---
        clean_sql = raw_ai_text.replace("SQL|", "").replace("```sql", "").replace("```", "").strip()

        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=f"```{clean_sql}```"
        )

        query_job = bq_client.query(clean_sql)
        result_iter = query_job.result()

        headers = [field.name for field in result_iter.schema]
        rows = [list(row.values()) for row in result_iter]

        table_text = format_table(rows, headers)
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=f"```\n{table_text}\n```"
        )

        if len(rows) > MAX_DISPLAY_ROWS or "(truncated)" in table_text:
            import io
            import csv

            
            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer)
            writer.writerow(headers)
            writer.writerows(rows)
            
            slack_client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                content=csv_buffer.getvalue(),
                filename="full_results.csv",
                title="Full Results (CSV)"
            )


    except Exception as e:
        slack_client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=f"Something went wrong: {str(e)}"
        )


# --- Main worker endpoint ---
@app.route("/pubsub/push", methods=["POST"])
def pubsub_worker():
    envelope = request.get_json()
    if not envelope or "message" not in envelope:
        return "Bad Request", 400

    pubsub_message = envelope["message"]
    data = json.loads(base64.b64decode(pubsub_message["data"]).decode("utf-8"))

    # --- Handle modal submission (user filled the parameter form) ---
    if data.get("type") == "modal_submission":
        question = data.get("question", "")
        channel_id = data.get("channel_id")
        thread_ts = data.get("thread_ts")
        user_id = data.get("user_id")

        # Build exact filter hint from submitted parameter values
        submitted_params = data.get("submitted_params")
        prompt_params = data.get("prompt_params")
        filter_hint = build_filter_hint(submitted_params, prompt_params)

        if question and channel_id:
            slack_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=f"<@{user_id}> asked: {question}"
            )
            process_question(question, channel_id, thread_ts, filter_hint=filter_hint)

        return "OK", 200

    # --- Handle other interactive payloads (legacy, fallback) ---
    if data.get("type") == "interaction":
        payload = data.get("payload", {})
        if payload.get("type") == "block_actions":
            for action in payload.get("actions", []):
                if action.get("action_id") == "sample_prompt_select":
                    selected_id = action["selected_option"]["value"]
                    channel_id = payload["channel"]["id"]
                    thread_ts = payload.get("message", {}).get("ts")
                    user_id = payload["user"]["id"]

                    prompt_def = next((p for p in SAMPLE_PROMPTS if p["id"] == selected_id), None)
                    if not prompt_def:
                        return "OK", 200

                    filled_question = fill_template(prompt_def)
                    slack_client.chat_postMessage(
                        channel=channel_id, thread_ts=thread_ts,
                        text=f"<@{user_id}> asked: {filled_question}"
                    )
                    process_question(filled_question, channel_id, thread_ts)

        return "OK", 200

    # --- Handle regular Slack events (app_mention) ---
    event = data.get("event", {})

    if event.get("type") == "app_mention":
        user_question = event.get("text", "")
        channel_id = event.get("channel")
        thread_ts = event.get("ts")

        clean_question = user_question.split(">", 1)[-1].strip() if ">" in user_question else user_question.strip()

        # --- Help command ---
        if clean_question.lower() in ("help", "?", ""):
            slack_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text="Here are some sample questions:",
                blocks=build_help_blocks()
            )
            return "OK", 200

        process_question(clean_question, channel_id, thread_ts)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)