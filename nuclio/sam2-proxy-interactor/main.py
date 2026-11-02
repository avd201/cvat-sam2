import json
import os
import urllib.error
import urllib.request


def init_context(context):
    context.user_data.url = os.environ.get("SAM2_SERVICE_URL", "http://sam2-service:8000").rstrip("/")
    context.logger.info("SAM2 proxy -> %s", context.user_data.url)


def handler(context, event):
    body = event.body if isinstance(event.body, dict) else json.loads(event.body)
    payload = {
        "image": body["image"],
        "pos_points": body.get("pos_points") or [],
        "neg_points": body.get("neg_points") or [],
        "obj_bbox": body.get("obj_bbox") or None,
    }
    req = urllib.request.Request(
        context.user_data.url + "/v1/interact",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as resp:
            out, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:  # propagate the service's 4xx/5xx with its message
        out, status = e.read(), e.code
    except Exception as e:  # service down / unreachable
        context.logger.error("SAM2 service unreachable: %s", e)
        return context.Response(
            body=json.dumps({"error": "SAM2 service unreachable"}),
            content_type="application/json", status_code=502,
        )
    return context.Response(body=out, content_type="application/json", status_code=status)
