"""Contract validation and state-transition tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from kitsune_contracts import (
    ALLOWED_RUN_TRANSITIONS,
    AgentDescriptor,
    AgentManifest,
    AgentRegistration,
    AgentRunAcknowledgement,
    AgentRunAssignment,
    AgentRunBegin,
    DockerRuntimeConfiguration,
    ExternalRuntimeConfiguration,
    HandlerDescriptor,
    InvalidRunTransition,
    KitsuneEvent,
    ObservabilityConfiguration,
    QueuePolicy,
    RunSource,
    RunStatus,
    RuntimeConfiguration,
    UsageRecord,
    can_transition,
    manifest_json_schema,
    parse_manifest_yaml,
    render_observability_url,
    validate_run_transition,
)

MANIFEST = """
schema: kitsune.agent
revision: 1
metadata:
  id: sre-agent
  display_name: SRE Agent
  description: SRE investigation assistant
  labels:
    domain: sre
spec:
  runtime:
    adapter: process
    mode: resident
    desired_state: running
    restart:
      policy: on_failure
      max_attempts: 5
      backoff_seconds: 5
    process:
      control_url: http://127.0.0.1:8081
      command: [uv, run, python, -m, sre_agent]
      working_directory: /opt/sre-agent
    environment:
      KITSUNE_WORKSPACE_URL: env://KITSUNE_WORKSPACE_URL
    secrets:
      OPENAI_API_KEY: env://OPENAI_API_KEY
      SLACK_BOT_TOKEN: file:///run/secrets/slack_bot_token
  invocation:
    default_handler: investigate
    max_concurrency: 4
    queue_capacity: 20
    timeout_seconds: 900
    handlers:
      investigate:
        max_concurrency: 2
        queue_capacity: 5
        queue_policy: reject
      refresh:
        queue_capacity: 10
  triggers:
    - id: manual
      type: on_demand
      handler: investigate
    - id: periodic-refresh
      type: schedule
      handler: refresh
      cron: "0 */6 * * *"
      timezone: Asia/Tokyo
      overlap: skip
      misfire_grace_seconds: 300
  observability:
    service_name: sre-agent
    trace_url_template: ""
    log_url_template: ""
  security:
    agent_token_ref: env://SRE_AGENT_KITSUNE_TOKEN
"""


def test_manifest_example_round_trips_secret_references() -> None:
    """The Manifest retains references while never resolving or storing secret values."""

    manifest = parse_manifest_yaml(MANIFEST)

    assert manifest.metadata.id == "sre-agent"
    assert manifest.spec.runtime.process is not None
    assert manifest.spec.runtime.process.command[-1] == "sre_agent"
    assert manifest.spec.runtime.secrets["OPENAI_API_KEY"] == "env://OPENAI_API_KEY"
    investigate = manifest.spec.invocation.handlers["investigate"]
    assert investigate.max_concurrency == 2
    assert investigate.queue_capacity == 5
    assert investigate.queue_policy is QueuePolicy.REJECT
    assert manifest.spec.invocation.handlers["refresh"].max_concurrency is None
    assert AgentManifest.model_validate_json(manifest.model_dump_json()).metadata.id == "sre-agent"


def test_manifest_schema_exposes_nested_contract() -> None:
    """JSON Schema is generated from the Pydantic contract."""

    schema = manifest_json_schema()

    assert schema["properties"]["schema"]["const"] == "kitsune.agent"
    assert "RuntimeConfiguration" in schema["$defs"]
    assert "HandlerInvocationConfiguration" in schema["$defs"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "handler_configuration",
    [
        {"max_concurrency": 0},
        {"queue_capacity": -1},
        {"queue_policy": "drop"},
        {"unknown": 1},
    ],
)
def test_manifest_rejects_invalid_handler_invocation_overrides(
    handler_configuration: dict[str, object],
) -> None:
    """Per-Handler admission overrides remain strict and typed."""

    manifest = parse_manifest_yaml(MANIFEST)
    document = manifest.model_dump(mode="json", by_alias=True)
    document["spec"]["invocation"]["handlers"]["investigate"] = handler_configuration

    with pytest.raises(ValidationError):
        AgentManifest.model_validate(document)


def test_handler_descriptor_remains_the_five_field_capability_contract() -> None:
    """Admission policy remains in the Manifest rather than the Handler Descriptor."""

    descriptor = HandlerDescriptor(
        name="investigate",
        description="Investigate an incident",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        default_timeout_seconds=900,
    )

    assert set(descriptor.model_dump()) == {
        "name",
        "description",
        "input_schema",
        "output_schema",
        "default_timeout_seconds",
    }


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("adapter: process\n    mode: resident", "runtime.process is required"),
        ("KITSUNE_WORKSPACE_URL: https://literal.invalid", "references must use"),
        ('cron: "0 0 * * *"', "cron is only valid"),
    ],
)
def test_manifest_rejects_invalid_runtime_and_trigger(fragment: str, message: str) -> None:
    """Invalid adapter, reference, and trigger combinations are rejected."""

    if fragment.startswith("adapter"):
        invalid = MANIFEST.replace(
            "adapter: process\n    mode: resident\n    desired_state: running\n",
            fragment + "\n    desired_state: running\n",
        ).replace(
            "    process:\n"
            "      control_url: http://127.0.0.1:8081\n"
            "      command: [uv, run, python, -m, sre_agent]\n"
            "      working_directory: /opt/sre-agent\n",
            "",
        )
    elif fragment.startswith("KITSUNE"):
        invalid = MANIFEST.replace("KITSUNE_WORKSPACE_URL: env://KITSUNE_WORKSPACE_URL", fragment)
    else:
        invalid = MANIFEST.replace(
            "handler: investigate\n    - id: periodic-refresh",
            f"handler: investigate\n      {fragment}\n    - id: periodic-refresh",
        )

    with pytest.raises(ValidationError, match=message):
        parse_manifest_yaml(invalid)


def test_terminal_run_states_have_no_outgoing_transitions() -> None:
    """Every terminal state rejects every subsequent state."""

    for state in RunStatus:
        if state.terminal:
            assert not ALLOWED_RUN_TRANSITIONS[state]
            for target in RunStatus:
                with pytest.raises(InvalidRunTransition):
                    validate_run_transition(state, target)


@given(st.sampled_from(list(RunStatus)), st.sampled_from(list(RunStatus)))
def test_transition_validator_matches_transition_table(
    current: RunStatus, target: RunStatus
) -> None:
    """The executable validator and exported transition table never drift."""

    if can_transition(current, target):
        validate_run_transition(current, target)
    else:
        with pytest.raises(InvalidRunTransition):
            validate_run_transition(current, target)


def test_failed_run_requires_error_and_terminal_timestamp() -> None:
    """A failed Run cannot be represented without its failure record."""

    from kitsune_contracts import RunRecord, RunSource

    with pytest.raises(ValidationError):
        RunRecord(
            agent_id="sre-agent",
            handler="investigate",
            source=RunSource.ON_DEMAND,
            status=RunStatus.FAILED,
            created_at=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    ("source", "parent_run_id", "message"),
    [
        (RunSource.CHILD, None, "require parent_run_id"),
        (
            RunSource.SCHEDULE,
            UUID("d61d32c9-10d3-41c4-90ed-c8bd97d132a5"),
            "only child Agent Runs",
        ),
    ],
)
def test_run_record_rejects_inconsistent_source_lineage(
    source: RunSource, parent_run_id: UUID | None, message: str
) -> None:
    """Persisted Run records apply the same child-parent identity invariant."""

    from kitsune_contracts import RunRecord

    with pytest.raises(ValidationError, match=message):
        RunRecord(
            agent_id="sre-agent",
            handler="investigate",
            source=source,
            parent_run_id=parent_run_id,
            created_at=datetime.now(UTC),
        )


def test_docker_network_host_alias_is_prohibited() -> None:
    """The Docker ``network: host`` spelling cannot bypass host-network rejection."""

    with pytest.raises(ValidationError, match="host networking is prohibited"):
        DockerRuntimeConfiguration(
            image="example.invalid/agent:1", network="isolated", host_network=True
        )


@pytest.mark.parametrize(
    "network",
    ["", " ", "default", "bridge", "HOST", "none", "container:peer", "service:peer", "a/b"],
)
def test_docker_network_requires_an_explicit_isolated_name(network: str) -> None:
    """Namespace sharing and Docker's built-in networks never enter a persisted Manifest."""

    with pytest.raises(ValidationError, match="isolated bridge network"):
        DockerRuntimeConfiguration(image="example.invalid/agent:1", network=network)


def test_resident_process_requires_canonical_control_url() -> None:
    """A resident Process declares its loopback URL directly."""

    adapter_configuration: dict[str, object] = {"command": ["python", "-m", "agent"]}
    document = {
        "adapter": "process",
        "mode": "resident",
        "process": adapter_configuration,
    }

    with pytest.raises(ValidationError, match="resident Process Runtime"):
        RuntimeConfiguration.model_validate(document)

    adapter_configuration["control_url"] = "http://127.0.0.1:8081"
    configuration = RuntimeConfiguration.model_validate(document)
    assert configuration.process is not None
    assert str(configuration.process.control_url) == "http://127.0.0.1:8081/"


def test_resident_docker_requires_control_port_and_isolation_limits() -> None:
    """Workspace derives the resident Container URL from an isolated network and port."""

    docker: dict[str, object] = {"image": "example.invalid/agent:1", "network": "agent-network"}
    document = {"adapter": "docker", "mode": "resident", "docker": docker}
    with pytest.raises(ValidationError, match=r"docker\.control_port"):
        RuntimeConfiguration.model_validate(document)

    docker["control_port"] = 8081
    configuration = RuntimeConfiguration.model_validate(document)
    assert configuration.docker is not None
    assert configuration.docker.control_port == 8081
    assert configuration.docker.memory_limit_bytes == 536_870_912
    assert configuration.docker.cpu_limit == 1.0
    assert configuration.docker.pids_limit == 256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_limit_bytes", 67_108_863),
        ("memory_limit_bytes", 68_719_476_737),
        ("cpu_limit", 0),
        ("cpu_limit", 64.01),
        ("cpu_limit", float("inf")),
        ("pids_limit", 15),
        ("pids_limit", 32_769),
    ],
)
def test_docker_resource_limits_are_finite_and_bounded(field: str, value: object) -> None:
    configuration = {
        "image": "example.invalid/agent:1",
        "network": "agent-network",
        field: value,
    }
    with pytest.raises(ValidationError):
        DockerRuntimeConfiguration.model_validate(configuration)


def test_ephemeral_docker_forbids_control_port() -> None:
    """One-shot Containers cannot expose an unused resident Control API."""

    with pytest.raises(ValidationError, match=r"cannot declare docker\.control_port"):
        RuntimeConfiguration.model_validate(
            {
                "adapter": "docker",
                "mode": "ephemeral",
                "docker": {
                    "image": "example.invalid/agent:1",
                    "network": "agent-network",
                    "control_port": 8081,
                },
            }
        )


@pytest.mark.parametrize(
    "mount",
    [
        "/srv/agent:/opt/agent",
        "/srv/read-only:/opt/data:ro",
        "/srv/read-write:/var/lib/agent:rw",
    ],
)
def test_docker_volume_mount_accepts_only_canonical_absolute_bind_syntax(mount: str) -> None:
    """A valid Docker bind remains directly serializable for the Runtime Adapter."""

    configuration = DockerRuntimeConfiguration(
        image="example.invalid/agent:1", network="agent-network", volumes=[mount]
    )

    assert configuration.model_dump(mode="json")["volumes"] == [mount]


@pytest.mark.parametrize(
    "mount",
    [
        "relative:/opt/agent",
        "/srv/agent:relative",
        ":/opt/agent",
        "/srv/agent:",
        "/srv/agent:/opt/agent:cached",
        "/srv/agent:/opt/agent:ro:extra",
        "/srv/agent",
        "/var/run:/host-run:ro",
        "/:/host:ro",
        "/var/run/../run:/host-run:ro",
        "/srv/agent:/:ro",
        "/srv/agent:/var/lib:ro",
        "/srv/agent:/var/lib/kitsune-outbox/child:rw",
        "/srv/agent:/var/lib/other/../kitsune-outbox:rw",
    ],
)
def test_docker_volume_mount_rejects_ambiguous_or_unsafe_bind_syntax(mount: str) -> None:
    """Relative paths, empty components, unknown modes, and extra colons are invalid."""

    with pytest.raises(ValidationError):
        DockerRuntimeConfiguration(
            image="example.invalid/agent:1", network="agent-network", volumes=[mount]
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user@agent.example.invalid",
        "https://agent.example.invalid/control",
        "https://agent.example.invalid?tenant=a",
        "https://agent.example.invalid#control",
    ],
)
def test_agent_control_url_rejects_noncanonical_base_urls(url: str) -> None:
    """Control endpoints cannot carry identity or routing ambiguity in the URL itself."""

    with pytest.raises(ValidationError):
        ExternalRuntimeConfiguration.model_validate({"endpoint": url})


def test_external_endpoint_and_registration_share_canonical_control_url() -> None:
    """Manifest and registration validate the same root Control API URL shape."""

    endpoint = "https://agent.example.invalid:8443"
    external = ExternalRuntimeConfiguration.model_validate({"endpoint": endpoint})
    descriptor = AgentDescriptor(
        agent_id="sre-agent",
        application_version="1.0.0",
        sdk_version="1.0.0",
        handlers=[],
        started_at=datetime.now(UTC),
    )
    registration = AgentRegistration.model_validate(
        {
            "descriptor": descriptor,
            "runtime_instance_id": "c8def18f-fbc6-47ee-9676-d0f44010b15b",
            "control_url": endpoint,
        }
    )

    assert str(external.endpoint) == str(registration.control_url)
    with pytest.raises(ValidationError):
        AgentRegistration.model_validate(
            {
                **registration.model_dump(),
                "control_url": "https://agent.example.invalid/control",
            }
        )


def test_external_endpoint_is_required_only_for_resident_runtime() -> None:
    """EventBridge-style external one-shots register without exposing a Control API."""

    ephemeral = RuntimeConfiguration.model_validate(
        {"adapter": "external", "mode": "ephemeral", "external": {}}
    )
    assert ephemeral.external is not None
    assert ephemeral.external.endpoint is None

    with pytest.raises(ValidationError, match="resident External Runtime"):
        RuntimeConfiguration.model_validate(
            {"adapter": "external", "mode": "resident", "external": {}}
        )

    resident = RuntimeConfiguration.model_validate(
        {
            "adapter": "external",
            "mode": "resident",
            "external": {"endpoint": "https://agent.example.invalid"},
        }
    )
    assert resident.external is not None
    assert str(resident.external.endpoint) == "https://agent.example.invalid/"


def test_agent_run_wire_contracts_are_typed_and_reject_wrong_sources_or_states() -> None:
    """Begin, assignment, and acknowledgement use UUIDs, enums, and fixed lifecycle values."""

    run_id = UUID("28f6ad49-1b83-42c6-a011-8559ea0a9c05")
    correlation_id = UUID("bd446d75-827d-4ff7-8f27-1f156f67db36")
    begin = AgentRunBegin(
        run_id=run_id,
        agent_id="sre-agent",
        runtime_instance_id=UUID("c8def18f-fbc6-47ee-9676-d0f44010b15b"),
        handler="investigate",
        source=RunSource.CHILD,
        parent_run_id=UUID("d61d32c9-10d3-41c4-90ed-c8bd97d132a5"),
        correlation_id=correlation_id,
        input={"incident": "INC-1"},
        idempotency_key="child-INC-1",
    )
    assignment = AgentRunAssignment(
        run_id=begin.run_id,
        agent_id=begin.agent_id,
        handler=begin.handler,
        source=RunSource.CHILD,
        input=begin.input,
        parent_run_id=begin.parent_run_id,
        correlation_id=begin.correlation_id,
    )
    acknowledgement = AgentRunAcknowledgement(
        runtime_instance_id=UUID("34d24248-d4dc-46b7-9e47-98158d357d5d")
    )

    assert begin.source is RunSource.CHILD
    assert assignment.source is RunSource.CHILD
    assert acknowledgement.status is RunStatus.RUNNING
    with pytest.raises(ValidationError):
        AgentRunBegin.model_validate(
            {
                "agent_id": "sre-agent",
                "runtime_instance_id": "c8def18f-fbc6-47ee-9676-d0f44010b15b",
                "handler": "investigate",
                "source": "webhook",
            }
        )
    with pytest.raises(ValidationError, match="require parent_run_id"):
        AgentRunBegin.model_validate(
            {
                "agent_id": "sre-agent",
                "runtime_instance_id": "c8def18f-fbc6-47ee-9676-d0f44010b15b",
                "handler": "investigate",
                "source": "child",
            }
        )
    with pytest.raises(ValidationError, match="only child Agent Runs"):
        AgentRunBegin.model_validate(
            {
                "agent_id": "sre-agent",
                "runtime_instance_id": "c8def18f-fbc6-47ee-9676-d0f44010b15b",
                "handler": "investigate",
                "source": "self",
                "parent_run_id": "d61d32c9-10d3-41c4-90ed-c8bd97d132a5",
            }
        )
    with pytest.raises(ValidationError):
        AgentRunBegin.model_validate(
            {
                "agent_id": "sre-agent",
                "handler": "investigate",
                "source": "self",
            }
        )
    with pytest.raises(ValidationError, match="require parent_run_id"):
        AgentRunAssignment.model_validate(
            {
                "run_id": run_id,
                "agent_id": "sre-agent",
                "handler": "investigate",
                "source": "child",
                "correlation_id": correlation_id,
            }
        )
    with pytest.raises(ValidationError, match="only child Agent Runs"):
        AgentRunAssignment.model_validate(
            {
                "run_id": run_id,
                "agent_id": "sre-agent",
                "handler": "investigate",
                "source": "webhook",
                "parent_run_id": "d61d32c9-10d3-41c4-90ed-c8bd97d132a5",
                "correlation_id": correlation_id,
            }
        )
    with pytest.raises(ValidationError):
        AgentRunAcknowledgement.model_validate({})
    with pytest.raises(ValidationError):
        AgentRunAcknowledgement.model_validate(
            {
                "runtime_instance_id": "34d24248-d4dc-46b7-9e47-98158d357d5d",
                "status": "succeeded",
            }
        )


def test_observability_url_template_renders_only_url_escaped_declared_identifiers() -> None:
    """Trace and log links allow paths and queries without interpreting identifier content."""

    configuration = ObservabilityConfiguration(
        service_name="sre-agent",
        trace_url_template=("https://traces.example.invalid/search?trace={trace_id}&run={run_id}"),
        log_url_template="https://logs.example.invalid/agents/{agent_id}",
    )

    trace_url = render_observability_url(
        configuration.trace_url_template,
        agent_id="sre-agent",
        run_id="28f6ad49-1b83-42c6-a011-8559ea0a9c05",
        trace_id="trace/value?scope=all",
    )
    log_url = render_observability_url(
        configuration.log_url_template,
        agent_id="sre-agent",
    )

    assert trace_url is not None and "trace%2Fvalue%3Fscope%3Dall" in trace_url
    assert log_url == "https://logs.example.invalid/agents/sre-agent"
    assert render_observability_url("", agent_id="sre-agent") is None


@pytest.mark.parametrize(
    "template",
    [
        "https://traces.example.invalid/{unknown}",
        "https://traces.example.invalid/{run_id.hex}",
        "https://traces.example.invalid/{run_id!r}",
        "https://traces.example.invalid/{run_id:>10}",
        "https://traces.example.invalid/{}",
        "https://traces.example.invalid/{run_id",
        "traces.example.invalid/{trace_id}",
        "ftp://traces.example.invalid/{trace_id}",
        "https://user@traces.example.invalid/{trace_id}",
        "https://traces.example.invalid/{trace_id}#details",
        " https://traces.example.invalid/{trace_id}",
    ],
)
def test_observability_url_template_rejects_unsafe_or_ambiguous_syntax(template: str) -> None:
    """Unknown fields, formatter features, and unsafe URL parts are invalid."""

    with pytest.raises(ValidationError):
        ObservabilityConfiguration(service_name="sre-agent", trace_url_template=template)


def test_usage_record_is_bounded_below_the_minimum_sdk_event_payload_limit() -> None:
    """A valid Usage Record cannot become an oversized non-Usage Event in the SDK."""

    assert UsageRecord(provider="p" * 255, total_tokens=1).provider == "p" * 255
    with pytest.raises(ValidationError):
        UsageRecord(provider="p" * 256, total_tokens=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("application_version", "a" * 256),
        ("sdk_version", "s" * 256),
        ("framework", "f" * 256),
        ("build_revision", "r" * 256),
    ],
)
def test_agent_descriptor_rejects_postgresql_unsafe_scalar_widths(field: str, value: str) -> None:
    descriptor = {
        "agent_id": "sre-agent",
        "application_version": "1",
        "sdk_version": "1",
        "handlers": [],
        "started_at": datetime.now(UTC),
        field: value,
    }

    with pytest.raises(ValidationError):
        AgentDescriptor.model_validate(descriptor)


def test_descriptor_collection_and_handler_bounds_are_explicit() -> None:
    handler = {
        "name": "default",
        "description": "x" * 4097,
        "input_schema": {},
        "output_schema": {},
        "default_timeout_seconds": 1,
    }
    with pytest.raises(ValidationError):
        HandlerDescriptor.model_validate(handler)
    with pytest.raises(ValidationError):
        HandlerDescriptor.model_validate(
            {**handler, "description": "", "default_timeout_seconds": 604_801}
        )

    descriptor = {
        "agent_id": "sre-agent",
        "application_version": "1",
        "sdk_version": "1",
        "handlers": [],
        "plugins": [{"name": f"plugin-{index}", "version": "1"} for index in range(1_001)],
        "started_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError):
        AgentDescriptor.model_validate(descriptor)


@pytest.mark.parametrize(
    "value",
    [-1, 9_223_372_036_854_775_808],
)
def test_usage_counters_fit_postgresql_bigint(value: int) -> None:
    with pytest.raises(ValidationError):
        UsageRecord(total_tokens=value)


@pytest.mark.parametrize(
    "value",
    [Decimal("Infinity"), Decimal("1e20"), Decimal("0.1234567890123456789")],
)
def test_usage_cost_fits_numeric_38_18(value: Decimal) -> None:
    with pytest.raises(ValidationError):
        UsageRecord(estimated_cost=value, currency="USD")


def test_event_type_keeps_its_public_256_character_bound() -> None:
    common = {
        "occurred_at": datetime.now(UTC),
        "agent_id": "sre-agent",
    }
    assert len(KitsuneEvent(type="a." + "b" * 254, **common).type) == 256
    with pytest.raises(ValidationError):
        KitsuneEvent(type="a." + "b" * 255, **common)


def test_agent_run_begin_timeout_uses_workspace_hard_ceiling() -> None:
    with pytest.raises(ValidationError):
        AgentRunBegin(
            agent_id="sre-agent",
            runtime_instance_id=UUID("00000000-0000-0000-0000-000000000001"),
            handler="default",
            timeout_seconds=604_801,
        )
