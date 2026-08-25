"""ClaudeSlim core engine — UI-free, importable, fully typed.

Public surface (stable for the CLI, the Textual TUI and library users)::

    from cslim.core import (
        CompressionService, CompressRequest, compress_paths,
        CompressionOptions, DiscoveryOptions, RenderOptions,
        Bundle, CompressedFile, TokenStats,
        clean_diff, clean_log, clean_terminal, git_diff, git_log,
        deliver, OutputMode, copy_to_clipboard, send_to_claude,
        resolve_estimator, resolve_model, TokenBudget, MODELS,
    )
"""

from __future__ import annotations

from .claude_pipe import (
    ClipboardError,
    Delivery,
    OutputMode,
    claude_binary,
    clipboard_backend,
    copy_to_clipboard,
    deliver,
    read_stdin,
    send_to_claude,
    stdin_has_data,
    stdout_is_pipe,
)
from .compressor import (
    SkeletonResult,
    compress_source,
    get_compressor,
    register_compressor,
)
from .delivery import (
    DeliveryMode,
    MapWriteResult,
    claude_md_path,
    git_state,
    read_section,
    remove_map,
    render_section,
    write_map,
)
from .discovery import DiscoveredFile, DiscoveryOptions, discover
from .git_cleaner import (
    DiffResult,
    GitCleanOptions,
    GitError,
    clean_diff,
    clean_log,
    clean_terminal,
    git_diff,
    git_log,
)
from .graphexport import Graph, GraphEdge, GraphNode, build_graph, to_dot, to_graphml, to_json
from .hook import HookConfig, HookOutcome, build_map, run_hook
from .installer import (
    InstallResult,
    InstallScope,
    hook_command,
    hook_status,
    install_hook,
    uninstall_hook,
)
from .models import (
    Bundle,
    CompressedFile,
    CompressionOptions,
    Language,
    Symbol,
    SymbolKind,
    TokenStats,
    detect_language,
)
from .renderer import RenderOptions, render_bundle
from .service import CompressionService, CompressRequest, compress_paths
from .theme import CSLIM_THEME, PALETTE, banner, make_console
from .tokenizer import (
    MODELS,
    ModelSpec,
    TokenBudget,
    humanize,
    resolve_estimator,
    resolve_model,
)

__all__ = [
    "CSLIM_THEME",
    "MODELS",
    "PALETTE",
    "Bundle",
    "ClipboardError",
    "CompressRequest",
    "CompressedFile",
    "CompressionOptions",
    "CompressionService",
    "Delivery",
    "DeliveryMode",
    "DiffResult",
    "DiscoveredFile",
    "DiscoveryOptions",
    "GitCleanOptions",
    "GitError",
    "Graph",
    "GraphEdge",
    "GraphNode",
    "HookConfig",
    "HookOutcome",
    "InstallResult",
    "InstallScope",
    "Language",
    "MapWriteResult",
    "ModelSpec",
    "OutputMode",
    "RenderOptions",
    "SkeletonResult",
    "Symbol",
    "SymbolKind",
    "TokenBudget",
    "TokenStats",
    "banner",
    "build_graph",
    "build_map",
    "claude_binary",
    "claude_md_path",
    "clean_diff",
    "clean_log",
    "clean_terminal",
    "clipboard_backend",
    "compress_paths",
    "compress_source",
    "copy_to_clipboard",
    "deliver",
    "detect_language",
    "discover",
    "get_compressor",
    "git_diff",
    "git_log",
    "git_state",
    "hook_command",
    "hook_status",
    "humanize",
    "install_hook",
    "make_console",
    "read_section",
    "read_stdin",
    "register_compressor",
    "remove_map",
    "render_bundle",
    "render_section",
    "resolve_estimator",
    "resolve_model",
    "run_hook",
    "send_to_claude",
    "stdin_has_data",
    "stdout_is_pipe",
    "to_dot",
    "to_graphml",
    "to_json",
    "uninstall_hook",
    "write_map",
]
