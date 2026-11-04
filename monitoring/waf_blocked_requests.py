"""Copy the WAF's BlockedRequests count into us-east-2, where the alarm can live.

An organization policy blocks creating alarms in us-east-1, where the CloudFront WAF
publishes its metrics, but reading them is allowed. This runs every 5 minutes (EventBridge)
and records the previous complete 5-minute bucket as WieldSteel/WafBlockedRequests in this
region. The alarm `waf-blocked-requests` watches that metric and emails the alerts topic.
"""

from datetime import datetime, timedelta, timezone

import boto3

WEB_ACL = "wield-steel-waf"
BUCKET = timedelta(minutes=5)

waf_cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")  # read only
cloudwatch = boto3.client("cloudwatch")  # this region


def handler(event, context):
    now = datetime.now(timezone.utc)
    # Two buckets back, so WAF's own reporting delay has passed.
    end = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) - BUCKET
    start = end - BUCKET
    points = waf_cloudwatch.get_metric_statistics(
        Namespace="AWS/WAFV2",
        MetricName="BlockedRequests",
        Dimensions=[{"Name": "WebACL", "Value": WEB_ACL}, {"Name": "Rule", "Value": "ALL"}],
        StartTime=start, EndTime=end, Period=300, Statistics=["Sum"],
    )["Datapoints"]
    blocked = sum(p["Sum"] for p in points)
    cloudwatch.put_metric_data(
        Namespace="WieldSteel",
        MetricData=[{"MetricName": "WafBlockedRequests", "Timestamp": start, "Value": blocked}],
    )
    return {"start": start.isoformat(), "blocked": blocked}
