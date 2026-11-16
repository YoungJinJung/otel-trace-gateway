"""Render chart variants and validate their configuration with the deployed image."""

import subprocess
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "default": [],
    "static": [
        "--set", "autoscaling.enabled=false",
        "--set", "replicaCount=3",
        "--set", "collector.metadataSyncTimeout=60s",
    ],
    "minimal": [
        "--set", "collector.selfMetrics.enabled=false",
        "--set-json", "collector.ignoreResources=[]",
        "--set", "datadog.apiKeySecret.create=false",
    ],
}


for name, flags in CASES.items():
    print(f"Checking {name}", flush=True)
    subprocess.run(["helm", "lint", str(ROOT), "--strict", *flags], check=True)
    rendered = subprocess.check_output(
        ["helm", "template", "ci", str(ROOT), "--namespace", "otel-ci", *flags],
        text=True,
    )
    resources = {
        doc["kind"]: doc for doc in yaml.safe_load_all(rendered) if doc is not None
    }
    deployment = resources["Deployment"]["spec"]
    replicas = deployment.get("replicas")
    if replicas is None:
        replicas = resources["HorizontalPodAutoscaler"]["spec"]["minReplicas"]

    # With no spare nodes, an update must free a slot while keeping a replica serving.
    strategy = deployment.get("strategy", {})
    assert strategy.get("type", "RollingUpdate") == "RollingUpdate"
    unavailable = strategy.get("rollingUpdate", {}).get("maxUnavailable", "25%")
    if isinstance(unavailable, str) and unavailable.endswith("%"):
        unavailable = replicas * int(unavailable[:-1]) // 100
    assert 0 < unavailable < replicas, "Rollout cannot progress safely without spare nodes"

    image = deployment["template"]["spec"]["containers"][0]["image"]
    config = resources["ConfigMap"]["data"]["config.yaml"]
    with tempfile.TemporaryDirectory(prefix="otel-chart-") as directory:
        # The Collector image runs as a non-root user.
        Path(directory).chmod(0o755)
        path = Path(directory) / "config.yaml"
        path.write_text(config)
        path.chmod(0o644)
        subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none",
                "-e", "DD_API_KEY=00000000000000000000000000000000",
                "-e", "POD_NAME=ci-collector",
                "--mount", f"type=bind,source={path},target=/conf/config.yaml,readonly",
                image, "validate", "--config=/conf/config.yaml",
            ],
            check=True,
        )
