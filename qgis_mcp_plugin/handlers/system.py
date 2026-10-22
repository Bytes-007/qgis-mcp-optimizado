"""Handlers for QGIS itself: health, versions, message log, plugins, settings."""

import contextlib
import io
import os
import sys
import time
import traceback

try:
    from datetime import UTC, datetime
except ImportError:
    from datetime import datetime, timezone

    UTC = timezone.utc  # noqa: UP017  (fallback path: datetime.UTC unavailable pre-3.11)

from typing import ClassVar

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.utils import active_plugins, available_plugins, pluginMetadata, reloadPlugin

from ..compat import MSG_INFO
from ..constants import plugin_version
from ..errors import CommandError
from ..registry import command

# Everything execute_code prints reaches the client's context, so it is capped:
# one `print()` inside a loop - or the repr() of a big layer - otherwise puts
# megabytes into a conversation nobody can read back. Reported output keeps a
# head and a tail; a traceback keeps its tail, because the exception is last.
_MAX_OUTPUT_CHARS = 8000
_MAX_OUTPUT_CHARS_CAP = 50000
_MAX_TRACEBACK_CHARS = 8000
_MAX_ERROR_CHARS = 2000

# The message log is a second stream nobody caps by default: a plugin that logs
# a whole feature collection in one message floods the context the same way.
_LOG_LIMIT_MAX = 200
_LOG_MESSAGE_CHARS = 500


def _clip(text, limit, keep_tail=0):
    """``(text, truncated, total_chars)`` with the middle dropped when too long."""
    total = len(text)
    if limit is None or total <= limit:
        return text, False, total
    limit = max(int(limit), 0)
    truncated = total - limit
    marker = f"\n…[truncated: {truncated} of {total} chars omitted]…\n"
    if keep_tail and limit > keep_tail:
        return text[: limit - keep_tail] + marker + text[-keep_tail:], True, total
    return text[:limit] + marker, True, total


class _CodeDeadline(Exception):
    """Raised by the execute_code trace function once the budget is spent."""


class SystemHandlers:
    """QGIS health, versions, message log, plugin list and settings."""

    # Below the client's TIMEOUT_LONG (60s) for the same reason as
    # ProcessingHandlers._PROCESSING_TIMEOUT: the plugin gives up, and says why,
    # before the client abandons the request and the result is lost (#43).
    _CODE_TIMEOUT = 55
    _CODE_FILENAME = "<qgis_mcp execute_code>"

    def _deadline_tracer(self, deadline):
        """Trace function that raises _CodeDeadline once *deadline* has passed.

        Checked on every function call anywhere and on every line of the script's
        own frames (module level and the functions it defines), never on lines
        inside library code, so the cost is proportional to the script rather
        than to what it calls. A single blocking call (time.sleep, a GDAL warp)
        cannot be interrupted this way: it returns when it returns, and the
        check fires right after.
        """

        def trace(frame, event, arg):
            # Only before work starts (a call, a line), never on return: a script
            # that finishes late has finished, and saying it was cancelled would
            # send the caller looking for work that was actually done.
            if event in ("call", "line") and time.monotonic() >= deadline:
                raise _CodeDeadline
            return trace if frame.f_code.co_filename == self._CODE_FILENAME else None

        return trace

    @command
    def ping(self, **kwargs):
        return {"pong": True}

    @command
    def diagnose(self, **kwargs):
        """Run diagnostic checks and return health status."""
        checks = []
        overall = "healthy"

        def check(name, detail_of, fail_status="error", fail_overall="degraded"):
            """Append one check, degrading the overall status when it raises."""
            nonlocal overall
            try:
                checks.append({"name": name, "status": "ok", "detail": detail_of()})
            except Exception as e:
                checks.append({"name": name, "status": fail_status, "detail": str(e)})
                if overall != "error":
                    overall = fail_overall

        def qgis_info():
            from qgis.PyQt.QtCore import QT_VERSION_STR as qt_ver

            return {
                "qgis_version": Qgis.version(),
                "python_version": sys.version.split()[0],
                "qt_version": qt_ver,
            }

        def active_providers():
            registry = QgsApplication.processingRegistry()
            return [p.id() for p in registry.providers() if p.isActive()]

        def project_info():
            project = QgsProject.instance()
            return {
                "loaded": bool(project.fileName()),
                "path": project.fileName() or None,
                "layer_count": len(project.mapLayers()),
            }

        # 1. QGIS info. Its failure is fatal; every other check only degrades.
        check("qgis", qgis_info, fail_overall="error")

        # 2. Plugin version
        check("plugin_version", plugin_version)

        # 3. Connected clients
        client_count = len(self.clients)
        checks.append({"name": "connected_clients", "status": "ok", "detail": client_count})

        # 3b. Which MCP server versions have talked to this plugin. The client
        # adds its own comparison (version_match), but reporting it from the
        # plugin's side covers several clients on one QGIS, where only some are
        # out of date - and it is empty against a server older than 0.10.
        seen = sorted(self.client_versions)
        drifted = [v for v in seen if v != plugin_version()]
        detail = {"seen": seen, "plugin": plugin_version(), "drifted": drifted}
        # The update command each drifted client announced for itself. Absent
        # against a client too old to send one.
        fixes = {v: self.client_fixes[v] for v in drifted if v in self.client_fixes}
        if fixes:
            detail["fixes"] = fixes
        checks.append(
            {
                "name": "client_versions",
                "status": "mismatch" if drifted else "ok",
                "detail": detail,
            }
        )

        # 4. Processing providers
        check("processing_providers", active_providers, fail_status="degraded")

        # 5. Project status
        check("project", project_info)

        return {"status": overall, "checks": checks}

    @command
    def get_qgis_info(self, **kwargs):
        info = {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins),
            # Identity, so a client driving several QGIS windows can tell which one
            # answered rather than inferring it from the port. The pid is unique and
            # stable; the window title is what the user reads in the taskbar and
            # already carries the project name.
            "pid": os.getpid(),
        }
        if self.iface is not None:
            with contextlib.suppress(Exception):
                info["window_title"] = self.iface.mainWindow().windowTitle()
        return info

    @command
    def execute_code(self, code, timeout=None, max_output=None, **kwargs):
        QgsMessageLog.logMessage(f"Executing code ({len(code)} chars)", self.LOG_TAG, MSG_INFO)
        budget = self._CODE_TIMEOUT if timeout is None else float(timeout)
        limit = _MAX_OUTPUT_CHARS if max_output is None else int(max_output)
        if limit <= 0:
            raise CommandError("max_output must be positive")
        if limit > _MAX_OUTPUT_CHARS_CAP:
            # Refused rather than clamped, like get_layer_features' limit: a
            # caller that asked for more and got less reads the short output as
            # the whole output.
            raise CommandError(
                f"max_output {limit} exceeds the maximum of {_MAX_OUTPUT_CHARS_CAP}"
            )
        started = time.monotonic()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        previous_trace = sys.gettrace()

        def output(**fields):
            # A quarter of the stream budget is tail: in a long loop the last
            # line is usually the state it ended in, and the head is the first.
            keep_tail = min(2000, limit // 4)
            for stream, capture in (("stdout", stdout_capture), ("stderr", stderr_capture)):
                text, truncated, total = _clip(capture.getvalue(), limit, keep_tail=keep_tail)
                fields[stream] = text
                if truncated:
                    fields[stream + "_truncated"] = True
                    fields[stream + "_total_chars"] = total
            fields["elapsed"] = round(time.monotonic() - started, 2)
            return fields

        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            namespace = {
                "qgis": Qgis,
                "QgsProject": QgsProject,
                "iface": self.iface,
                "QgsApplication": QgsApplication,
                "QgsVectorLayer": QgsVectorLayer,
                "QgsRasterLayer": QgsRasterLayer,
                "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
            }

            compiled = compile(code, self._CODE_FILENAME, "exec")
            sys.settrace(self._deadline_tracer(started + budget))
            try:
                exec(compiled, namespace)  # nosec B102 - intentional: MCP execute_code tool
            finally:
                sys.settrace(previous_trace)
            return output(executed=True)
        except _CodeDeadline:
            return output(
                executed=False,
                timed_out=True,
                error=(
                    f"Code cancelled after {budget:g}s (timeout). Pass a larger 'timeout' or "
                    "split the work into smaller scripts; side effects up to this point stand."
                ),
            )
        except Exception as e:
            # The traceback is clipped from the tail: its last line names the
            # exception, and a deep one otherwise arrives megabytes long.
            fields = {"executed": False}
            error_text, error_truncated, error_total = _clip(str(e), _MAX_ERROR_CHARS)
            fields["error"] = error_text
            if error_truncated:
                fields["error_truncated"] = True
                fields["error_total_chars"] = error_total
            tb_text, tb_truncated, tb_total = _clip(
                traceback.format_exc(), _MAX_TRACEBACK_CHARS, keep_tail=6000
            )
            fields["traceback"] = tb_text
            if tb_truncated:
                fields["traceback_truncated"] = True
                fields["traceback_total_chars"] = tb_total
            return output(**fields)
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    _LEVEL_MAP: ClassVar[dict[int, str]] = {
        0: "info",
        1: "warning",
        2: "critical",
        3: "success",
        4: "none",
    }

    def _capture_message(self, message, tag, level, *_extra):
        """Capture a message log entry into the deque.

        QGIS 4.x messageReceivedWithFormat sends a 4th arg (StringFormat);
        *_extra absorbs it so the same handler works for both signals.
        """
        self._message_log.append(
            {
                "tag": tag,
                "message": message,
                "level": self._LEVEL_MAP.get(int(level), str(level)),
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
        )

    @command
    def get_message_log(self, level=None, tag=None, limit=100, **kwargs):
        entries = list(self._message_log)
        entries.reverse()  # newest first
        if level:
            # An unknown level ("error") used to filter everything out silently.
            level = level.lower()
            if level not in self._LEVEL_MAP.values():
                raise CommandError(
                    f"Unknown level: {level!r}. Use one of {sorted(self._LEVEL_MAP.values())}"
                )
            entries = [e for e in entries if e["level"] == level]
        if tag:
            entries = [e for e in entries if e["tag"] == tag]
        total = len(entries)
        limit = int(limit)
        if limit <= 0:
            raise CommandError("limit must be positive")
        if limit > _LOG_LIMIT_MAX:
            raise CommandError(
                f"limit {limit} exceeds the maximum of {_LOG_LIMIT_MAX}; page with a lower limit"
            )
        entries = entries[:limit]
        messages = []
        for entry in entries:
            text, truncated, message_total = _clip(entry["message"], _LOG_MESSAGE_CHARS)
            if truncated:
                # Copy, never mutate: the deque is the log itself and the next
                # caller (or the user's Log Messages panel) gets the full text.
                entry = dict(entry, message=text, message_total_chars=message_total)
            messages.append(entry)
        return {
            "messages": messages,
            "count": len(messages),
            "total": total,
            "truncated": total > len(messages),
        }

    @command
    def list_plugins(self, enabled_only=False, **kwargs):
        names = active_plugins if enabled_only else available_plugins
        # No path here: it is a long absolute path the model has no use for when
        # picking a plugin, and it was paid for on every listing. get_plugin_info
        # still returns it for the one plugin someone asked about.
        result = [
            {
                "name": name,
                "enabled": name in active_plugins,
                "version": pluginMetadata(name, "version") or "",
            }
            for name in sorted(names)
        ]
        return {"plugins": result, "count": len(result)}

    @command
    def get_plugin_info(self, plugin_name, **kwargs):
        if plugin_name not in available_plugins and plugin_name not in active_plugins:
            raise CommandError(f"Plugin not found: {plugin_name}")
        return {
            "name": plugin_name,
            "enabled": plugin_name in active_plugins,
            "version": pluginMetadata(plugin_name, "version") or "",
            "description": pluginMetadata(plugin_name, "description") or "",
            "author": pluginMetadata(plugin_name, "author") or "",
            "path": pluginMetadata(plugin_name, "path") or "",
        }

    @command
    def reload_plugin(self, plugin_name, **kwargs):
        if plugin_name == "qgis_mcp_plugin":
            raise CommandError("Cannot reload MCP plugin (would break the connection)")
        if plugin_name not in active_plugins:
            raise CommandError(f"Plugin not active: {plugin_name}")
        started = reloadPlugin(plugin_name)
        # A plugin whose code no longer loads is dropped from active_plugins
        # (QGIS 4 also returns False); its error only reaches the message bar.
        if started is False or plugin_name not in active_plugins:
            raise CommandError(
                f"Plugin {plugin_name} failed to start after reload; see the QGIS message log"
            )
        return {"reloaded": plugin_name, "ok": True}

    @command
    def get_setting(self, key, **kwargs):
        settings = QgsSettings()
        value = settings.value(key)
        return {
            "key": key,
            "value": value,
            "exists": settings.contains(key),
        }

    @command
    def set_setting(self, key, value, **kwargs):
        settings = QgsSettings()
        settings.setValue(key, value)
        return {"ok": True, "key": key}
