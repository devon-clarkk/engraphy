"""Pack tool aliases: extra MCP tool names binding a core tool with preset args.
Pure sugar -- same validation, same audit identity ('write via log_error'). design/03
s.Tool aliases.

An alias adds a NEW MCP tool name (`log_error`) that dispatches to a core tool
(`write`) with some arguments preset (`type: error`). It changes nothing about the
core tool: the merged arguments run the identical validation, dedup and RLS path, and
the audit row records `write via log_error` so provenance survives the sugar. This is
"the full extent of pack-defined behavior in v1" (design/01's deliberate ceiling).

`packs.validate()` already rejects an alias that binds a non-core tool or presets an
undeclared argument, so a pack that reaches here is well-formed; `build_aliases`
re-checks defensively (a server should never register a malformed binding even if a
pack skipped validation) and is the single place the runtime learns the binding.

Preset-vs-caller conflict (DECISIONS-DELTA.md 2026-07-18): if a caller passes an
argument the alias also presets, the PRESET wins -- the preset is the alias's fixed
identity (`log_error` is *always* `type: error`; letting a caller override it would
make the alias a different tool). The design shows presets as fixed values, never as
caller-overridable defaults, so this preserves the alias contract.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class AliasBinding:
    """One pack alias. `binds` is the core tool name to dispatch; `preset` are the
    fixed arguments merged over the caller's; `description` overrides the tool text
    the model reads (None = inherit the core tool's)."""

    name: str
    binds: str
    preset: dict
    description: str | None


def _core_tools() -> dict:
    """The core tool -> allowed-argument-names map. Imported lazily so importing
    this module needs neither jsonschema nor yaml (packs.py pulls both); the single
    source of truth stays packs.CORE_TOOLS."""
    from engraphy.admin.packs import CORE_TOOLS

    return CORE_TOOLS


def build_aliases(pack: dict, core_tools: dict | None = None) -> dict[str, AliasBinding]:
    """Turn a pack's `tool_aliases` block into name -> AliasBinding. Raises
    ValueError on a binding `packs.validate()` would have rejected (defence in depth,
    not the primary gate). Empty/absent `tool_aliases` -> {}."""
    cts = core_tools if core_tools is not None else _core_tools()
    bindings: dict[str, AliasBinding] = {}
    for name, spec in (pack.get("tool_aliases") or {}).items():
        binds = spec.get("binds")
        if binds not in cts:
            raise ValueError(f"tool_aliases.{name}: binds '{binds}' is not a core tool")
        preset = dict(spec.get("preset") or {})
        undeclared = set(preset) - cts[binds]
        if undeclared:
            raise ValueError(
                f"tool_aliases.{name}.preset: {sorted(undeclared)} not argument(s) of {binds}"
            )
        if name in cts:
            # An alias may not shadow a core tool name -- that would silently
            # reroute the stable contract surface.
            raise ValueError(f"tool_aliases.{name}: shadows core tool '{name}'")
        bindings[name] = AliasBinding(
            name=name, binds=binds, preset=preset, description=spec.get("description"),
        )
    return bindings


def resolve_alias_call(binding: AliasBinding, caller_args: dict | None) -> tuple[str, dict, str]:
    """Map an alias invocation to a core-tool dispatch. Returns
    (core_tool_name, merged_arguments, audit_identity). The preset overrides any
    caller value for the same key (the alias's fixed contract). audit_identity is
    `<core> via <alias>` for the audit_log `action` (design/03 'logged as write via
    log_error')."""
    merged = dict(caller_args or {})
    merged.update(binding.preset)
    return binding.binds, merged, f"{binding.binds} via {binding.name}"
