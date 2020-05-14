import json
from os import environ as env
from typing import (Dict, List, Mapping)

import utils
import utils.aws as aws
import utils.handlers as handlers


def social_report(event: utils.LambdaEvent) -> Mapping:  # pylint: disable=unused-argument
    """Get all events from CloudWatch REPORT_LOG_GROUP_NAME group."""
    log_group_name = env["REPORT_LOG_GROUP_NAME"]

    return aws.read_all_log_streams(log_group=log_group_name)


def contact(event: utils.LambdaEvent) -> str:
    """
    Send event payload to Notifications lambda for delivery.

    Expects these keys in event mapping:

    - source
    - name
    - email
    - description
    """
    lambda_notifications = env["LAMBDA_NOTIFICATIONS"]

    body = event["body"]

    utils.Log.debug("Processing body payload: %s", body)

    try:
        utils.Log.debug("Loading JSON content from body")
        utils.Log.info("json.loads should be safe to use: "
                       "https://stackoverflow.com/a/45483187/2274124")

        msg = """Source: {source}
Name: {name}
Mail: {email}
Desc: {description}
""".format(**json.loads(body))

    except (TypeError, json.JSONDecodeError) as error:
        raise utils.HandledError("JSON body is malformatted: %s" % error)

    except KeyError as error:
        raise utils.HandledError("Missing JSON key: %s" % error)

    utils.Log.debug("### Message content below ###")
    utils.Log.debug(msg)
    utils.Log.debug("#############################")

    return aws.invoke_lambda(
        name=lambda_notifications,
        payload={
            "title": "New /contact submission received",
            "payload": msg,
        }).text


def pagespeed_report(_: utils.LambdaEvent) -> List[Dict]:
    """Return report from Google Pagespeed data stored in DynamoDB."""
    data = aws.scan_dynamodb_table(env["PAGESPEED_TABLE"])

    if data["Count"] == 0:
        raise utils.HandledError(message="Unexpected DynamoDB response: empty table",
                                 status_code=500)

    items = [{"url": item['url']['S'],
              "latest_score_value": float(item['latest_score_value']['N']),
              "latest_score_timestamp": item['latest_score_timestamp']['S']}
             for item in data["Items"]]

    utils.Log.debug("Items: %s", items)

    errors = False
    for item in items:
        if not 0.99 < item["latest_score_value"] <= 1:
            item["error"] = True
            errors = True

    if errors:
        raise utils.HandledError(message=items, status_code=400)

    return items


def handler(event, context) -> utils.Response:
    """Lambda entry point.

    Public HTTPS REST API entry point
    """
    router_map = {
        "GET /pagespeed_report": pagespeed_report,
        "GET /robots.txt": lambda _: "User-agent: *\nDisallow: /",
        "GET /social_report": social_report,
        "POST /contact": contact,
    }

    return handlers.ApiGatewayEventHandler(name="api",
                                           event=utils.LambdaEvent(event),
                                           context=utils.LambdaContext(context),
                                           router_map=router_map,
                                           ).response
