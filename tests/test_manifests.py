"""Every image a manifest asks for is one scripts/build-images.sh builds, so a clean build can always start every pod.
(Found on the real cluster: the second-opinion manifest pinned a tag that no script builds, with imagePullPolicy Never.)"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def built_images() -> set[str]:
    return set(re.findall(r"-t (autosre/[a-z-]+:dev)", (ROOT / "scripts" / "build-images.sh").read_text()))


def containers():
    for path in sorted((ROOT / "k8s").glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == "Deployment":
                for c in doc["spec"]["template"]["spec"]["containers"]:
                    yield path.name, c


def test_every_autosre_image_is_built_by_the_build_script():
    built = built_images()
    assert len(built) == 7
    used = {c["image"] for _, c in containers() if c["image"].startswith("autosre/")}
    assert used == built, f"manifests use {sorted(used - built)} which nothing builds; built but unused: {sorted(built - used)}"


def test_no_pod_pins_a_pull_policy_that_cannot_find_a_freshly_built_image():
    for name, c in containers():
        if c["image"].startswith("autosre/"):
            assert c["image"].endswith(":dev"), (name, c["image"])
