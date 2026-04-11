import os
import json
import yaml
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from google.cloud import pubsub_v1, bigquery
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

app = Flask(__name__)

# --- Clients ---
publisher = pubsub_v1.PublisherClient()
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC_ID")
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_ID)

verifier = SignatureVerifier(os.environ.get("SLACK_SIGNING_SECRET"))
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
bq_client = bigquery.Client(project=PROJECT_ID)

# --- Load sample prompts ---
def _load_file(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return ""

def load_sample_prompts():
    raw = _load_file("sample_prompts.yml")
    if not raw:
        return []
    try:
        return yaml.safe_load(raw).get("prompts", [])
    except Exception:
        return []

SAMPLE_PROMPTS = load_sample_prompts()



# --- Compute default date values ---
def compute_default(param):
    """Return the default value string for a parameter."""
    if param["type"] == "date":
        raw = param.get("default", "")
        if raw == "yesterday":
            return (datetime.utcnow().date() - timedelta(days=1)).isoformat()
        return raw  # fixed date string like "2026-01-01"
    if param["type"] == "number":
        return str(param.get("default", 10))
    return param.get("default", "")


# --- Build Slack modal view from prompt definition ---
def build_modal_view(prompt_def):
    """Build a Slack Block Kit modal view for a sample prompt's parameters."""
    blocks = []

    for param in prompt_def.get("parameters", []):
        block_id = f"block_{param['name']}"
        action_id = f"param_{param['name']}"

        if param["type"] == "date":
            default_val = compute_default(param)
            element = {
                "type": "datepicker",
                "action_id": action_id,
            }
            if default_val:
                element["initial_date"] = default_val
            blocks.append({
                "type": "input",
                "block_id": block_id,
                "label": {"type": "plain_text", "text": param["label"]},
                "element": element,
            })

        elif param["type"] == "number":
            default_val = compute_default(param)
            blocks.append({
                "type": "input",
                "block_id": block_id,
                "label": {"type": "plain_text", "text": param["label"]},
                "element": {
                    "type": "plain_text_input",
                    "action_id": action_id,
                    "initial_value": default_val,
                },
            })

        elif param["type"] == "select":
            options = []
            default_val = param.get("default", "")
            initial_option = None
            for opt in param.get("options", []):
                option_obj = {
                    "text": {"type": "plain_text", "text": opt},
                    "value": opt,
                }
                options.append(option_obj)
                if opt == default_val:
                    initial_option = option_obj
            element = {
                "type": "static_select",
                "action_id": action_id,
                "options": options,
            }
            if initial_option:
                element["initial_option"] = initial_option
            blocks.append({
                "type": "input",
                "block_id": block_id,
                "label": {"type": "plain_text", "text": param["label"]},
                "element": element,
            })

        elif param["type"] == "external":
            element = {
                "type": "external_select",
                "action_id": param.get("action_id", action_id),
                "min_query_length": 0,  # load all options immediately
            }
            blocks.append({
                "type": "input",
                "block_id": block_id,
                "label": {"type": "plain_text", "text": param["label"]},
                "element": element,
            })

        elif param["type"] == "multi_external":
            element = {
                "type": "multi_external_select",
                "action_id": param.get("action_id", action_id),
                "min_query_length": 0,
            }
            block = {
                "type": "input",
                "block_id": block_id,
                "label": {"type": "plain_text", "text": param["label"]},
                "element": element,
            }
            if param.get("optional"):
                block["optional"] = True
            blocks.append(block)

        elif param["type"] == "text":
            element = {
                "type": "plain_text_input",
                "action_id": action_id,
            }
            if param.get("placeholder"):
                element["placeholder"] = {"type": "plain_text", "text": param["placeholder"]}
            block = {
                "type": "input",
                "block_id": block_id,
                "label": {"type": "plain_text", "text": param["label"]},
                "element": element,
            }
            if param.get("optional"):
                block["optional"] = True
            blocks.append(block)

    # The view payload
    view = {
        "type": "modal",
        "callback_id": "sample_prompt_modal",
        "title": {"type": "plain_text", "text": "Customize Query"},
        "submit": {"type": "plain_text", "text": "Run Query"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({
            "prompt_id": prompt_def["id"],
        }),
        "blocks": blocks,
    }
    return view


# --- BigQuery option queries for external_select ---
OPTION_QUERIES = {
    "param_brand": """
        SELECT DISTINCT brand_category_key AS val
        FROM `valor-sales.valor_margin_dbt.mart_grouped_margin`
        WHERE brand_category_key IS NOT NULL
        ORDER BY 1
    """,
    "param_category": """
        SELECT DISTINCT category_formatted AS val
        FROM `valor-sales.valor_margin_dbt.mart_grouped_margin`
        WHERE category_formatted IS NOT NULL
        ORDER BY 1
    """,
    "param_province": """
        SELECT DISTINCT shipping_province AS val
        FROM `valor-sales.valor_margin_dbt.mart_grouped_margin`
        WHERE shipping_province IS NOT NULL
        ORDER BY 1
    """,
    "param_stamp_type": """
        SELECT DISTINCT excise_type AS val
        FROM `valor-sales.valor_margin_dbt.mart_grouped_margin`
        WHERE excise_type IS NOT NULL
        ORDER BY 1
    """,
    "param_sku_ml": """
        SELECT DISTINCT sku_ML AS val
        FROM `valor-sales.valor_margin_dbt.mart_grouped_margin`
        WHERE sku_ML IS NOT NULL
        ORDER BY 1
    """,
    "param_product_name_group": """
        SELECT DISTINCT product_title_grouped AS val
        FROM `valor-sales.valor_margin_dbt.mart_grouped_margin`
        WHERE product_title_grouped IS NOT NULL
        ORDER BY 1
    """,
    "param_product_name_valor": """
        SELECT DISTINCT valor_product_title_grouped AS val
        FROM `valor-sales.valor_margin_dbt.fct_valor_margin`
        WHERE valor_product_title_grouped IS NOT NULL
        and fulfillment_date> '2026-01-01'
        ORDER BY 1
    """
}


# ==================== ROUTES ====================

@app.route("/slack/events", methods=["POST"])
def slack_receiver():
    """Handle Slack event subscriptions (app_mention, etc.)."""
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return "Invalid request", 403

    data = request.json

    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    if "X-Slack-Retry-Num" in request.headers or data.get("event", {}).get("bot_id"):
        return "OK", 200

    publisher.publish(topic_path, json.dumps(data).encode("utf-8"))
    return "OK", 200


@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    """Handle Slack interactive payloads: block_actions and view_submission."""
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return "Invalid request", 403

    payload = json.loads(request.form.get("payload", "{}"))

    # --- Block action: user selected a sample prompt → open modal ---
    if payload.get("type") == "block_actions":
        for action in payload.get("actions", []):
            if action.get("action_id") == "sample_prompt_select":
                selected_id = action["selected_option"]["value"]
                trigger_id = payload["trigger_id"]

                prompt_def = next(
                    (p for p in SAMPLE_PROMPTS if p["id"] == selected_id), None
                )
                if not prompt_def:
                    return "OK", 200

                # Store channel + thread in metadata so worker knows where to post
                meta = {
                    "prompt_id": prompt_def["id"],
                    "channel_id": payload["channel"]["id"],
                    "thread_ts": payload.get("message", {}).get("ts"),
                    "user_id": payload["user"]["id"],
                }
                view = build_modal_view(prompt_def)
                view["private_metadata"] = json.dumps(meta)

                slack_client.views_open(trigger_id=trigger_id, view=view)

        return "OK", 200

    # --- View submission: user filled the modal form → forward to worker ---
    if payload.get("type") == "view_submission":
        meta = json.loads(payload["view"].get("private_metadata", "{}"))
        prompt_id = meta.get("prompt_id")
        channel_id = meta.get("channel_id")
        thread_ts = meta.get("thread_ts")
        user_id = meta.get("user_id")

        prompt_def = next(
            (p for p in SAMPLE_PROMPTS if p["id"] == prompt_id), None
        )
        if not prompt_def:
            return "OK", 200

        # Extract submitted values from the modal
        state_values = payload["view"]["state"]["values"]
        submitted = {}
        for param in prompt_def.get("parameters", []):
            block_id = f"block_{param['name']}"
            action_id = param.get("action_id", f"param_{param['name']}")
            block_data = state_values.get(block_id, {}).get(action_id, {})

            if param["type"] == "date":
                submitted[param["name"]] = block_data.get("selected_date", compute_default(param))
            elif param["type"] == "select":
                opt = block_data.get("selected_option")
                submitted[param["name"]] = opt["value"] if opt else param.get("default", "")
            elif param["type"] == "external":
                opt = block_data.get("selected_option")
                submitted[param["name"]] = opt["value"] if opt else "ALL"
            elif param["type"] == "multi_external":
                opts = block_data.get("selected_options", [])
                submitted[param["name"]] = ", ".join(o["value"] for o in opts) if opts else "ALL"
            else:
                submitted[param["name"]] = block_data.get("value", compute_default(param))

        # Fill the template with user-submitted values
        template = prompt_def.get("template", "")
        for key, val in submitted.items():
            if val is None:
                val = ""
            template = template.replace("{" + key + "}", str(val))

        # Publish to Pub/Sub for the worker to process
        envelope = {
            "type": "modal_submission",
            "question": template,
            "submitted_params": submitted,
            "prompt_params": [
                {"name": p["name"], "type": p["type"], "action_id": p.get("action_id", "")}
                for p in prompt_def.get("parameters", [])
            ],
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "user_id": user_id,
        }
        publisher.publish(topic_path, json.dumps(envelope).encode("utf-8"))

        # Return empty response to close the modal
        return "", 200

    # --- Any other interaction type: forward as before ---
    envelope = {"type": "interaction", "payload": payload}
    publisher.publish(topic_path, json.dumps(envelope).encode("utf-8"))
    return "OK", 200


@app.route("/slack/options", methods=["POST"])
def slack_options():
    """Handle Slack external_select options load requests.
    Queries BigQuery for distinct values based on the action_id.
    """
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return "Invalid request", 403

    payload = json.loads(request.form.get("payload", "{}"))
    action_id = payload.get("action_id", "")
    search_text = payload.get("value", "").strip().lower()

    query = OPTION_QUERIES.get(action_id)
    if not query:
        return jsonify({"options": []})

    try:
        results = bq_client.query(query).result()
        options = []
        for row in results:
            val = str(row.val)
            # Filter by typeahead search text
            if search_text and search_text not in val.lower():
                continue
            options.append({
                "text": {"type": "plain_text", "text": val[:75]},
                "value": val[:75],
            })
            if len(options) >= 100:
                break
        return jsonify({"options": options})
    except Exception as e:
        print(f"Options query error: {e}")
        return jsonify({"options": []})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)