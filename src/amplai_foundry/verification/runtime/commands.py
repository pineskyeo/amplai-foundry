"""Pinned verifier commands with explicit structured-result contracts."""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from ...runtime.contracts.identity import digest_bytes
from ...runtime.errors import RuntimeFault
from .service import VerificationObservation


class StructuredCommandVerifier:
    def __init__(
        self,
        command_id,
        argv,
        *,
        sandbox,
        workspace,
        timeout_seconds=120,
        report_path="junit.xml",
        format="junit",
        minimum_tests=1,
    ):
        if not command_id or not argv or not 1 <= timeout_seconds <= 86400 or minimum_tests < 1:
            raise RuntimeFault("VERIFIER_COMMAND", "Verifier command profile is incomplete")
        self.id, self.argv, self.sandbox, self.workspace, self.timeout = (
            command_id,
            tuple(argv),
            sandbox,
            Path(workspace),
            timeout_seconds,
        )
        self.report, self.format, self.minimum = report_path, format, minimum_tests

    def __call__(self, raw):
        # The sandbox has a dedicated verifier-owned immutable test source mount.
        # Raw worker output is evidence input, never interpolated into a shell command.
        path = self.workspace / "subject.bin"
        path.write_bytes(raw)
        command = self.sandbox.command(list(self.argv), self.workspace, "verify-" + self.id)
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return VerificationObservation(
                "inconclusive", "Verifier exceeded its frozen timeout", {}, None
            )
        report = self.workspace / self.report
        if (
            report.is_symlink()
            or not report.resolve().is_relative_to(self.workspace.resolve())
            or not report.is_file()
        ):
            return VerificationObservation(
                "inconclusive",
                "Verifier did not produce its named structured report",
                {},
                result.returncode,
            )
        data = report.read_bytes()
        if len(data) > 16 * 1024 * 1024:
            return VerificationObservation(
                "inconclusive", "Verifier report exceeds bound", {}, result.returncode
            )
        try:
            if self.format == "junit":
                if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                    raise ValueError("DTD/entity not permitted")
                root = ET.fromstring(data)
                cases = list(root.iter("testcase"))
                failed = sum(
                    bool(list(x.findall("failure")) + list(x.findall("error"))) for x in cases
                )
                skipped = sum(x.find("skipped") is not None for x in cases)
                tests = len(cases)
                passed = (
                    result.returncode == 0
                    and tests - skipped >= self.minimum
                    and failed == 0
                    and skipped == 0
                )
                details = {
                    "tests": tests,
                    "failures": failed,
                    "skipped": skipped,
                    "report_digest": digest_bytes(data),
                }
            elif self.format == "json":
                value = json.loads(data)
                if set(value) != {"tests", "failures", "errors", "skipped"} or any(
                    type(x) is not int or x < 0 for x in value.values()
                ):
                    raise ValueError("report shape")
                passed = (
                    result.returncode == 0
                    and value["tests"] >= self.minimum
                    and value["failures"] == value["errors"] == value["skipped"] == 0
                )
                details = {**value, "report_digest": digest_bytes(data)}
            else:
                raise ValueError("format")
        except (ValueError, ET.ParseError):
            return VerificationObservation(
                "inconclusive", "Malformed structured verifier report", {}, result.returncode
            )
        return VerificationObservation(
            "pass" if passed else "fail",
            "Actual pinned command and non-empty structured test results",
            details,
            result.returncode,
        )
