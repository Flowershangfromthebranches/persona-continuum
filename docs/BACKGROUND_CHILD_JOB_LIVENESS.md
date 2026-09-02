# Background Child Job Liveness

`PersonaCreationJob` and `ProfileEnrichmentJob` persist worker state,
start/heartbeat/finish timestamps, and Agent call counts. A long Agent call
updates the worker heartbeat through the shared runtime activity boundary;
stage transitions, material progress, and Agent call completion also update the
durable snapshot.

Profile Enrichment is the user-facing aggregate for Persona child creation.
The parent copies the child stage, label, current operation, runtime
diagnostics, heartbeat, and Agent call count. Child progress maps monotonically
from 5% startup through 99% pre-commit; the final parent commit is 100%.

The parent watchdog checks the child status, persisted heartbeat, and in-process
task registry. A missing or completed worker task can be reattached once from
the persisted checkpoint. A second loss produces `CHILD_JOB_WORKER_LOST` and
stops polling. The parent records the child job ID and liveness context without
replacing a child root failure code.

The child Persona creation run is created with `visibility=internal` when it is
owned by Profile Enrichment. It remains available from the parent detail view,
but is not a second user task card.
