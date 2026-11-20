# Egress (lockin-scanner)

Scan a repo for AWS lock-in, score it, generate a migration to open-source /
sovereign European alternatives, and run the migrated stack locally.

Full README comes in STEP 6. Quick start:

    make install   # creates .venv with python3.11
    make run       # Streamlit UI on :8501
    make test

License: Apache 2.0

## Lock-in scoring formula

Each detected service has an intrinsic profile (0-100) on three dimensions
(see `scanner/rules.py::SERVICE_PROFILES`):

| Dimension | Weight | Meaning |
|---|---|---|
| `api` | 50% | How proprietary the API / programming model is |
| `data` | 30% | How proprietary the data format / semantics are |
| `egress` | 20% | Cost of moving data out (egress fees, volume) |

```
base          = 0.5*api + 0.3*data + 0.2*egress
depth         = min(1, Σ severity_weight)     # low=0.3, medium=0.6, high=1.0
service_score = round(base * depth)           # 0-100
overall       = round(0.5*max(service_scores) + 0.5*mean(service_scores))
rating        = high (>=70) | medium (>=40) | low
```

`depth` rewards evidence: a single low-severity hint scores far less than a
Terraform resource plus SDK calls. Overall blends the worst service with the
average, so one deeply locked-in service is never hidden by many shallow ones.

CLI: `python -m scanner <path> [--summary] [--out report.json]`
