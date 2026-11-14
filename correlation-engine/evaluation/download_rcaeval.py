"""Download the RCAEval RE1 dataset (metrics only) into correlation-engine/data/rcaeval.

RE1: 375 failure cases injected into Online Boutique, Sock Shop and Train Ticket
(125 each; cpu, mem, disk I/O, delay and loss faults), one metrics.parquet per case
with 1-second samples and the injection time. About 120 MB. MIT licensed:
https://github.com/phamquiluan/RCAEval

Only data files are fetched; nothing from the download is executed. The folder is
git-ignored.

    pip install huggingface_hub pyarrow pandas
    python evaluation/download_rcaeval.py
"""

from pathlib import Path

from huggingface_hub import snapshot_download

TARGET = Path(__file__).resolve().parents[1] / "data" / "rcaeval"


def main() -> None:
    snapshot_download(
        repo_id="phamquiluan/RCAEval",
        repo_type="dataset",
        allow_patterns=["re1*", "cases.parquet", "README.md"],
        local_dir=str(TARGET),
    )
    print(f"RE1 downloaded to {TARGET}")


if __name__ == "__main__":
    main()
