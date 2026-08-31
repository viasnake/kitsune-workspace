"""Process log byte-budget and oversized-line draining regressions."""

from __future__ import annotations

import asyncio

import pytest

from kitsune_workspace.runtime import ProcessAdapter


@pytest.mark.asyncio
async def test_process_reader_drains_oversized_line_without_retaining_its_content() -> None:
    secret = "oversized-process-secret"  # noqa: S105 - synthetic leak canary
    adapter = ProcessAdapter(log_line_max_bytes=64, live_logs_max_bytes=256)
    stream = asyncio.StreamReader(limit=32)
    stream.feed_data((secret * 100).encode())
    stream.feed_data(b"\nnormal-after-oversize\n")
    stream.feed_eof()

    await adapter._read_stream("instance-one", "stdout", stream)

    logs = adapter.tail("instance-one", 10)
    assert logs == [
        "stdout: [OVERSIZED LOG LINE OMITTED]",
        "stdout: normal-after-oversize",
    ]
    assert secret not in repr(logs)


@pytest.mark.asyncio
async def test_process_reader_records_final_unterminated_line() -> None:
    """EOF flushes one final line without a newline or argument-shape failure."""

    adapter = ProcessAdapter(log_line_max_bytes=128, live_logs_max_bytes=256)
    stream = asyncio.StreamReader(limit=32)
    stream.feed_data(b"final-without-newline\r")
    stream.feed_eof()

    await adapter._read_stream("instance-one", "stdout", stream)

    assert adapter.tail("instance-one", 10) == ["stdout: final-without-newline"]


@pytest.mark.asyncio
async def test_process_live_and_archived_logs_use_shared_text_redaction() -> None:
    """Dynamic assignments and known values are masked before live or archived storage."""

    secrets = {
        "bearer-secret",
        "snake-secret",
        "dotted-secret",
        "custom-secret",
        "known-raw-secret",
    }
    adapter = ProcessAdapter(
        redacted_keys={"tenantSession"},
        log_line_max_bytes=512,
        live_logs_max_bytes=1_024,
    )
    adapter._redactions["instance-one"] = {"known-raw-secret"}
    stream = asyncio.StreamReader(limit=512)
    stream.feed_data(
        b"Authorization: Bearer bearer-secret api_key=snake-secret "
        b"api.key=dotted-secret tenant.session=custom-secret known-raw-secret\n"
    )
    stream.feed_eof()

    await adapter._read_stream("instance-one", "stdout", stream)
    live = adapter.tail("instance-one", 10)
    await adapter.cleanup("instance-one")
    archived = adapter.tail("instance-one", 10)

    assert live == archived
    assert live == [
        "stdout: Authorization: [REDACTED] api_key=[REDACTED] "
        "api.key=[REDACTED] tenant.session=[REDACTED] [REDACTED]"
    ]
    for secret in secrets:
        assert secret not in repr(live)
        assert secret not in repr(archived)


def test_process_live_log_budget_evicts_oldest_lines_per_instance() -> None:
    adapter = ProcessAdapter(log_line_max_bytes=64, live_logs_max_bytes=100)

    for index in range(10):
        adapter._append_log("instance-one", "stdout", f"line-{index}-" + "x" * 24)

    logs = adapter.tail("instance-one", 100)
    assert logs
    assert logs[-1].startswith("stdout: line-9-")
    assert "line-0-" not in repr(logs)
    assert adapter._live_log_bytes["instance-one"] <= 100


@pytest.mark.asyncio
async def test_process_crash_loop_archive_is_bounded_by_bytes_and_instance_count() -> None:
    adapter = ProcessAdapter(
        log_line_max_bytes=128,
        live_logs_max_bytes=256,
        archived_logs_max_bytes=90,
        archived_log_instances=2,
    )

    for index in range(8):
        instance_id = f"crash-{index}"
        adapter._append_log(instance_id, "stderr", "failure-" + "x" * 20)
        await adapter.cleanup(instance_id)

    assert list(adapter.archived_logs) == ["crash-6", "crash-7"]
    assert adapter._archived_log_bytes <= 90
    assert len(adapter._archived_log_sizes) == 2
