"""
BrainTwin (public brand: DigitalTwin) — cloud topology source of truth.

Generates ``architecture.png`` using mingrammer/diagrams. This script IS the
documentation: edit it, regenerate, commit both the .py and the .png together
in the same PR. The CI staleness check (Phase 4.0.6.1) reads the file mtimes.

Run:
    pip install diagrams        # one-time
    # plus graphviz installed on the OS (brew install graphviz | apt-get install graphviz)
    python docs/diagrams/architecture.py

Output:
    docs/diagrams/architecture.png

Mapping to CDK:
    Each node's variable name (e.g. ``ec2_host``) should match the construct ID
    used in ``infra/lib/braintwin-stack.ts`` once the CDK lands in M.2. Drift
    between this picture and the CDK is what ``cdk-dia`` will catch later.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2, EC2ContainerRegistry
from diagrams.aws.management import (
    Cloudwatch,
    CloudwatchLogs,
    SystemsManager,
    SystemsManagerParameterStore,
)
from diagrams.aws.storage import S3, EBS
from diagrams.aws.general import Users
from diagrams.onprem.client import User, Client
from diagrams.onprem.network import Internet
from diagrams.saas.cdn import Cloudflare
from diagrams.programming.framework import Fastapi
from diagrams.programming.language import Python
from diagrams.onprem.container import Docker
from diagrams.generic.storage import Storage


# ---------------------------------------------------------------------------
# Diagram attributes
# ---------------------------------------------------------------------------

graph_attr = {
    "fontsize": "20",
    "fontname": "Helvetica",
    "labelloc": "t",
    "rankdir": "LR",
    "splines": "spline",
    "pad": "0.5",
    "nodesep": "0.8",
    "ranksep": "1.2",
    "bgcolor": "white",
}

node_attr = {
    "fontsize": "12",
    "fontname": "Helvetica",
}

edge_attr = {
    "fontsize": "10",
    "fontname": "Helvetica",
}


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

with Diagram(
    "DigitalTwin — Cloud Architecture (Phase 4.0.6)",
    filename="docs/diagrams/architecture",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
    outformat="png",
):

    # -----------------------------------------------------------------------
    # External actors
    # -----------------------------------------------------------------------
    with Cluster("Operator devices (Seattle)"):
        chrome_ext = Client("Chrome extension\n(DigitalTwin v0.5)")
        tg_mobile = User("Telegram mobile\n(forward articles)")

    anthropic_api = Internet("Anthropic API\n(Sonnet + Haiku)")

    # -----------------------------------------------------------------------
    # Edge / DNS / TLS
    # -----------------------------------------------------------------------
    with Cluster("Edge — Cloudflare (proxied, Full Strict TLS)"):
        cloudflare = Cloudflare("digitaltwin.app\nDNS + DDoS + TLS")

    # -----------------------------------------------------------------------
    # Compute — single EC2 in us-west-2a
    # -----------------------------------------------------------------------
    with Cluster("AWS us-west-2 (Oregon / PDX)"):

        with Cluster("VPC — single AZ us-west-2a"):
            with Cluster("EC2 t4g.small (ARM/Graviton)\nAmazon Linux 2023 + docker-compose"):
                caddy = Docker("caddy\n(reverse proxy +\nLet's Encrypt)")
                app = Fastapi("app\n(FastAPI 8000)")
                bot = Python("bot\n(Telegram poller)")
                litestream = Docker("litestream\n(SQLite WAL\nreplicator)")

            ebs = EBS("EBS gp3 30 GiB\nSQLite + Chroma\n+ whisper model\n+ images")

        with Cluster("S3 (us-west-2)"):
            s3_wal = S3("braintwin-state\nlitestream WAL/\nchroma-nightly/\nimages/")

        with Cluster("Container registry"):
            ecr = EC2ContainerRegistry("ECR\nbraintwin-app\nbraintwin-bot")

        with Cluster("Config & secrets"):
            ssm_params = SystemsManagerParameterStore(
                "Parameter Store\n/braintwin/anthropic_key\n/braintwin/bearer_token\n/braintwin/telegram_token"
            )

        with Cluster("Observability"):
            cwlogs = CloudwatchLogs("CloudWatch Logs\n/braintwin/app\n/braintwin/bot")
            cwbudget = Cloudwatch("AWS Budgets\nemail alerts")

        with Cluster("Operator access"):
            ssm_session = SystemsManager("SSM Session Manager\n(no SSH, no :22)")

        with Cluster("Backup automation"):
            dlm = Storage("DLM\nEBS snapshots\n(daily, 7-day retention)")

    # -----------------------------------------------------------------------
    # External monitoring
    # -----------------------------------------------------------------------
    uptimerobot = Internet("UptimeRobot\n(/health probe)")

    # -----------------------------------------------------------------------
    # Edges — request paths
    # -----------------------------------------------------------------------

    # Capture / recall path from extension
    chrome_ext >> Edge(label="HTTPS\n+ bearer", color="darkgreen") >> cloudflare
    cloudflare >> Edge(label=":443", color="darkgreen") >> caddy
    caddy >> Edge(label=":8000", color="darkgreen") >> app

    # Telegram forwards
    tg_mobile >> Edge(label="forward", color="darkblue") >> Internet("Telegram BotAPI") >> bot
    bot >> Edge(label="ingest", color="darkblue") >> app

    # App ↔ data plane
    app >> Edge(label="reads/writes", style="bold") >> ebs
    bot >> Edge(label="reads/writes", style="bold") >> ebs
    litestream >> Edge(label="WAL replay", color="purple") >> ebs

    # WAL & nightly backups to S3
    litestream >> Edge(label="WAL push", color="purple", style="dashed") >> s3_wal
    app >> Edge(label="nightly\nchroma tar.gz", color="purple", style="dashed") >> s3_wal
    app >> Edge(label="image uploads", color="purple", style="dashed") >> s3_wal

    # Outbound LLM
    app >> Edge(label="Sonnet rerank\n+ Haiku assist", color="orange") >> anthropic_api

    # Config & secrets at startup
    ssm_params >> Edge(label="fetch on boot", color="gray", style="dotted") >> app
    ssm_params >> Edge(label="fetch on boot", color="gray", style="dotted") >> bot

    # Observability
    app >> Edge(label="stdout", color="gray") >> cwlogs
    bot >> Edge(label="stdout", color="gray") >> cwlogs
    cwbudget >> Edge(label="alert >$X", color="red", style="dashed") >> Users("Operator email")

    # External probe
    uptimerobot >> Edge(label="/health 60s", color="black") >> cloudflare

    # Operator
    operator = User("Operator\n(Sabya, Seattle)")
    operator >> Edge(label="SSM tunnel", style="dotted") >> ssm_session
    ssm_session >> Edge(style="dotted") >> caddy

    # Image pulls
    ecr >> Edge(label="docker pull\n(on deploy)", style="dashed", color="gray") >> caddy

    # Snapshot policy
    dlm >> Edge(label="daily snapshot", color="purple", style="dashed") >> ebs
