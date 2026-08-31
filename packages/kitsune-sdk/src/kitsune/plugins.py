"""Bounded public Plugin protocol and dependency-aware Plugin Host."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from kitsune_contracts import KitsuneEvent, RunOutcome, UsageRecord

from .context import RunContext

ChildRunGuard = Callable[[RunContext], Awaitable[None]]
ModelCallGuard = Callable[[RunContext, bool], Awaitable[None]]
UsageObserver = Callable[[RunContext, UsageRecord], Awaitable[None]]
ContextExtensionFactory = Callable[[RunContext], Any]


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Stable identity, dependency list, and failure policy for a Plugin."""

    name: str
    version: str
    dependencies: tuple[str, ...] = ()
    critical: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("plugin name must not be empty")
        if not self.version.strip():
            raise ValueError("plugin version must not be empty")
        if self.name in self.dependencies:
            raise ValueError("a plugin cannot depend on itself")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("plugin dependencies must be unique")


@runtime_checkable
class AppBuilder(Protocol):
    """Pre-start extension surface available to Plugin ``configure`` hooks."""

    @property
    def agent_id(self) -> str:
        """Return the immutable logical Agent ID."""

        ...

    def add_child_run_guard(self, guard: ChildRunGuard) -> None:
        """Run ``guard`` before each SDK-managed child Run starts."""

    def add_model_call_guard(self, guard: ModelCallGuard) -> None:
        """Run ``guard`` before each integration-managed model request."""

    def add_usage_observer(self, observer: UsageObserver) -> None:
        """Observe exact Usage Records attached to a Run Context."""

    def add_context_extension(self, name: str, factory: ContextExtensionFactory) -> None:
        """Attach one immutable-by-name public extension to every new Run Context."""


@runtime_checkable
class RunningApp(Protocol):
    """Narrow running-application surface available to Plugin lifecycle hooks."""

    @property
    def agent_id(self) -> str:
        """Return the immutable logical Agent ID."""

        ...

    async def emit(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        ctx: RunContext | None = None,
    ) -> KitsuneEvent:
        """Emit a namespaced event through the SDK's normal delivery path."""

        ...


@runtime_checkable
class KitsunePlugin(Protocol):
    """Application-wide extension using only Kitsune's stable lifecycle hooks."""

    metadata: PluginMetadata

    def configure(self, app: AppBuilder) -> None:
        """Register public guards, observers, or Context extensions before startup."""

    async def start(self, app: RunningApp) -> None:
        """Acquire Plugin resources after dependency validation."""

    async def stop(self, app: RunningApp) -> None:
        """Release Plugin resources during graceful shutdown."""

    async def on_run_started(self, ctx: RunContext) -> None:
        """Observe a newly started Run."""

    async def on_run_finished(self, ctx: RunContext, outcome: RunOutcome) -> None:
        """Observe one terminal Run outcome."""

    async def on_event(self, event: KitsuneEvent) -> None:
        """Observe an immutable event without direct outbox access."""


class PluginDependencyError(RuntimeError):
    """Raised when Plugin dependencies are missing or cyclic."""


class CriticalPluginHookError(RuntimeError):
    """Raised after a critical Plugin observation hook fails and is reported."""

    def __init__(self, plugin: KitsunePlugin, hook: str, cause: Exception) -> None:
        self.plugin = plugin.metadata
        self.hook = hook
        self.cause = cause
        super().__init__(f"critical plugin {self.plugin.name!r} failed during {hook}: {cause}")


PluginErrorReporter = Callable[[KitsunePlugin, str, Exception], Awaitable[None]]


class PluginHost:
    """Freeze Plugin registration, resolve dependencies, and isolate failures."""

    def __init__(self) -> None:
        self._plugins: dict[str, KitsunePlugin] = {}
        self._order: list[KitsunePlugin] = []
        self._active: list[KitsunePlugin] = []
        self._frozen = False

    @property
    def metadata(self) -> tuple[PluginMetadata, ...]:
        """Return registered Plugin metadata in dependency order when available."""

        plugins = self._order or list(self._plugins.values())
        return tuple(plugin.metadata for plugin in plugins)

    def register(self, plugin: KitsunePlugin, builder: AppBuilder) -> None:
        """Configure and register a Plugin before application startup."""

        if self._frozen:
            raise RuntimeError("plugins cannot be added after application startup")
        name = plugin.metadata.name
        if name in self._plugins:
            raise ValueError(f"plugin {name!r} is already registered")
        plugin.configure(builder)
        self._plugins[name] = plugin

    def resolve(self) -> tuple[KitsunePlugin, ...]:
        """Validate dependencies and return a deterministic topological order."""

        temporary: set[str] = set()
        permanent: set[str] = set()
        result: list[KitsunePlugin] = []

        def visit(name: str, path: tuple[str, ...]) -> None:
            if name in permanent:
                return
            if name in temporary:
                cycle = " -> ".join((*path, name))
                raise PluginDependencyError(f"plugin dependency cycle: {cycle}")
            plugin = self._plugins.get(name)
            if plugin is None:
                parent = path[-1] if path else name
                raise PluginDependencyError(f"plugin {parent!r} requires missing plugin {name!r}")
            temporary.add(name)
            for dependency in plugin.metadata.dependencies:
                visit(dependency, (*path, name))
            temporary.remove(name)
            permanent.add(name)
            result.append(plugin)

        for plugin_name in self._plugins:
            visit(plugin_name, ())
        self._order = result
        return tuple(result)

    async def start(self, app: RunningApp, report_error: PluginErrorReporter) -> None:
        """Start Plugins in dependency order with critical failure semantics."""

        self._frozen = True
        try:
            order = self.resolve()
            active_names: set[str] = set()
            for plugin in order:
                missing_active = set(plugin.metadata.dependencies) - active_names
                if missing_active:
                    error = PluginDependencyError(
                        f"plugin {plugin.metadata.name!r} has inactive dependencies: "
                        f"{', '.join(sorted(missing_active))}"
                    )
                    if plugin.metadata.critical:
                        raise error
                    await report_error(plugin, "start", error)
                    continue
                try:
                    await plugin.start(app)
                except Exception as exc:
                    if plugin.metadata.critical:
                        raise
                    await report_error(plugin, "start", exc)
                else:
                    self._active.append(plugin)
                    active_names.add(plugin.metadata.name)
        except BaseException as exc:
            try:
                await self.stop(app, report_error)
            except BaseException as cleanup_error:
                exc.add_note(
                    "Plugin startup rollback also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            self._frozen = False
            raise

    async def stop(self, app: RunningApp, report_error: PluginErrorReporter) -> None:
        """Stop active Plugins in reverse dependency order."""

        critical_error: Exception | None = None
        try:
            for plugin in reversed(self._active):
                try:
                    await plugin.stop(app)
                except Exception as exc:
                    try:
                        await report_error(plugin, "stop", exc)
                    except Exception as report_error_exception:
                        if critical_error is None:
                            critical_error = report_error_exception
                    if plugin.metadata.critical and critical_error is None:
                        critical_error = exc
        finally:
            self._active.clear()
        if critical_error is not None:
            raise critical_error

    async def on_run_started(self, ctx: RunContext, report_error: PluginErrorReporter) -> None:
        """Dispatch a Run-start hook while isolating noncritical Plugins."""

        await self._dispatch("on_run_started", (ctx,), report_error)

    async def on_run_finished(
        self,
        ctx: RunContext,
        outcome: RunOutcome,
        report_error: PluginErrorReporter,
    ) -> None:
        """Dispatch a Run-finished hook while isolating noncritical Plugins."""

        await self._dispatch("on_run_finished", (ctx, outcome), report_error)

    async def on_event(
        self,
        event: KitsuneEvent,
        report_error: PluginErrorReporter,
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        """Dispatch an Event hook while isolating noncritical Plugins."""

        await self._dispatch("on_event", (event,), report_error, exclude=exclude)

    async def _dispatch(
        self,
        hook: str,
        arguments: tuple[Any, ...],
        report_error: PluginErrorReporter,
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        critical_error: CriticalPluginHookError | None = None
        for plugin in tuple(self._active):
            if plugin.metadata.name in exclude:
                continue
            try:
                callback = getattr(plugin, hook)
                callback_arguments = arguments
                if hook == "on_event":
                    event = cast(KitsuneEvent, arguments[0])
                    callback_arguments = (event.model_copy(deep=True),)
                await callback(*callback_arguments)
            except Exception as exc:
                await report_error(plugin, hook, exc)
                if plugin.metadata.critical and critical_error is None:
                    critical_error = CriticalPluginHookError(plugin, hook, exc)
        if critical_error is not None:
            raise critical_error from critical_error.cause
