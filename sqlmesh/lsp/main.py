#!/usr/bin/env python
"""A Language Server Protocol (LSP) server for SQL with SQLMesh integration, refactored without globals."""

import logging
import typing as t
from pathlib import Path
import urllib.parse

from lsprotocol import types
from pygls.server import LanguageServer

from sqlmesh._version import __version__
from sqlmesh.core.context import Context
from sqlmesh.core.linter.definition import AnnotatedRuleViolation
from sqlmesh.lsp.api import (
    API_FEATURE,
    ApiRequest,
    ApiResponseGetColumnLineage,
    ApiResponseGetLineage,
    ApiResponseGetModels,
)
from sqlmesh.lsp.completions import get_sql_completions
from sqlmesh.lsp.context import LSPContext, ModelTarget
from sqlmesh.lsp.custom import ALL_MODELS_FEATURE, AllModelsRequest, AllModelsResponse
from sqlmesh.lsp.reference import (
    get_references,
)
from sqlmesh.lsp.uri import URI
from web.server.api.endpoints.lineage import column_lineage, model_lineage
from web.server.api.endpoints.models import get_models


class SQLMeshLanguageServer:
    def __init__(
        self,
        context_class: t.Type[Context],
        server_name: str = "sqlmesh_lsp",
        version: str = __version__,
    ):
        """
        :param context_class: A class that inherits from `Context`.
        :param server_name: Name for the language server.
        :param version: Version string.
        """
        self.server = LanguageServer(server_name, version)
        self.context_class = context_class
        self.lsp_context: t.Optional[LSPContext] = None
        self.lint_cache: t.Dict[URI, t.List[AnnotatedRuleViolation]] = {}

        # Register LSP features (e.g., formatting, hover, etc.)
        self._register_features()

    def _register_features(self) -> None:
        """Register LSP features on the internal LanguageServer instance."""

        @self.server.feature(types.INITIALIZE)
        def initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
            """Initialize the server when the client connects."""
            try:
                if params.workspace_folders:
                    # Try to find a SQLMesh config file in any workspace folder (only at the root level)
                    for folder in params.workspace_folders:
                        folder_path = Path(self._uri_to_path(folder.uri))
                        # Only check for config files directly in the workspace directory
                        for ext in ("py", "yml", "yaml"):
                            config_path = folder_path / f"config.{ext}"
                            if config_path.exists():
                                try:
                                    # Use user-provided instantiator to build the context
                                    created_context = self.context_class(paths=[folder_path])
                                    self.lsp_context = LSPContext(created_context)
                                    ls.show_message(
                                        f"Loaded SQLMesh context from {config_path}",
                                        types.MessageType.Info,
                                    )
                                    return  # Exit after successfully loading any config
                                except Exception as e:
                                    ls.show_message(
                                        f"Error loading context from {config_path}: {e}",
                                        types.MessageType.Warning,
                                    )
            except Exception as e:
                ls.show_message(f"Error initializing SQLMesh context: {e}", types.MessageType.Error)

        @self.server.feature(ALL_MODELS_FEATURE)
        def all_models(ls: LanguageServer, params: AllModelsRequest) -> AllModelsResponse:
            try:
                uri = URI(params.textDocument.uri)
                context = self._context_get_or_load(uri)
                return get_sql_completions(context, uri)
            except Exception as e:
                return get_sql_completions(None, uri)

        @self.server.feature(API_FEATURE)
        def api(
            ls: LanguageServer, request: ApiRequest
        ) -> t.Union[ApiResponseGetModels, ApiResponseGetLineage, ApiResponseGetColumnLineage]:
            ls.log_trace(f"API request: {request}")
            if self.lsp_context is None:
                raise RuntimeError("No context found")

            parsed_url = urllib.parse.urlparse(request.url)
            path_parts = parsed_url.path.strip("/").split("/")

            if request.method == "GET":
                if path_parts == ["api", "models"]:
                    # /api/models
                    return ApiResponseGetModels(data=get_models(self.lsp_context.context))

                if path_parts[:2] == ["api", "lineage"]:
                    if len(path_parts) == 3:
                        # /api/lineage/{model}
                        model_name = urllib.parse.unquote(path_parts[2])
                        lineage = model_lineage(model_name, self.lsp_context.context)
                        non_set_lineage = {k: v for k, v in lineage.items() if v is not None}
                        return ApiResponseGetLineage(data=non_set_lineage)

                    if len(path_parts) == 4:
                        # /api/lineage/{model}/{column}
                        model_name = urllib.parse.unquote(path_parts[2])
                        column = urllib.parse.unquote(path_parts[3])
                        logging.info(f"Column lineage request: {model_name} {column}")
                        column_lineage_response = column_lineage(
                            model_name, column, False, self.lsp_context.context
                        )
                        return ApiResponseGetColumnLineage(data=column_lineage_response)

            raise NotImplementedError(f"API request not implemented: {request.url}")

        @self.server.feature(types.TEXT_DOCUMENT_DID_OPEN)
        def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams) -> None:
            uri = URI(params.text_document.uri)
            context = self._context_get_or_load(uri)
            if self.lint_cache.get(uri) is not None:
                ls.publish_diagnostics(
                    params.text_document.uri,
                    SQLMeshLanguageServer._diagnostics_to_lsp_diagnostics(self.lint_cache[uri]),
                )
                return
            models = context.map[uri.to_path()]
            if models is None:
                return
            if not isinstance(models, ModelTarget):
                return
            self.lint_cache[uri] = context.context.lint_models(
                models.names,
                raise_on_error=False,
            )
            ls.publish_diagnostics(
                params.text_document.uri,
                SQLMeshLanguageServer._diagnostics_to_lsp_diagnostics(self.lint_cache[uri]),
            )

        @self.server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
        def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams) -> None:
            uri = URI(params.text_document.uri)
            context = self._context_get_or_load(uri)
            models = context.map[uri.to_path()]
            if models is None:
                return
            if not isinstance(models, ModelTarget):
                return
            self.lint_cache[uri] = context.context.lint_models(
                models.names,
                raise_on_error=False,
            )
            ls.publish_diagnostics(
                params.text_document.uri,
                SQLMeshLanguageServer._diagnostics_to_lsp_diagnostics(self.lint_cache[uri]),
            )

        @self.server.feature(types.TEXT_DOCUMENT_DID_SAVE)
        def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams) -> None:
            uri = URI(params.text_document.uri)
            context = self._context_get_or_load(uri)
            models = context.map[uri.to_path()]
            if models is None:
                return
            if not isinstance(models, ModelTarget):
                return
            self.lint_cache[uri] = context.context.lint_models(
                models.names,
                raise_on_error=False,
            )
            ls.publish_diagnostics(
                params.text_document.uri,
                SQLMeshLanguageServer._diagnostics_to_lsp_diagnostics(self.lint_cache[uri]),
            )

        @self.server.feature(types.TEXT_DOCUMENT_FORMATTING)
        def formatting(
            ls: LanguageServer, params: types.DocumentFormattingParams
        ) -> t.List[types.TextEdit]:
            """Format the document using SQLMesh `format_model_expressions`."""
            try:
                uri = URI(params.text_document.uri)
                self._ensure_context_for_document(uri)
                document = ls.workspace.get_text_document(params.text_document.uri)
                if self.lsp_context is None:
                    raise RuntimeError(f"No context found for document: {document.path}")

                # Perform formatting using the loaded context
                self.lsp_context.context.format(paths=(str(uri.to_path()),))
                with open(uri.to_path(), "r+", encoding="utf-8") as file:
                    new_text = file.read()

                # Return a single edit that replaces the entire file.
                return [
                    types.TextEdit(
                        range=types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(
                                line=len(document.lines),
                                character=len(document.lines[-1]) if document.lines else 0,
                            ),
                        ),
                        new_text=new_text,
                    )
                ]
            except Exception as e:
                ls.show_message(f"Error formatting SQL: {e}", types.MessageType.Error)
                return []

        @self.server.feature(types.TEXT_DOCUMENT_HOVER)
        def hover(ls: LanguageServer, params: types.HoverParams) -> t.Optional[types.Hover]:
            """Provide hover information for an object."""
            try:
                uri = URI(params.text_document.uri)
                self._ensure_context_for_document(uri)
                document = ls.workspace.get_text_document(params.text_document.uri)
                if self.lsp_context is None:
                    raise RuntimeError(f"No context found for document: {document.path}")

                references = get_references(self.lsp_context, uri, params.position)
                if not references:
                    return None
                reference = references[0]
                if not reference.description:
                    return None
                return types.Hover(
                    contents=types.MarkupContent(
                        kind=types.MarkupKind.Markdown, value=reference.description
                    ),
                    range=reference.range,
                )

            except Exception as e:
                ls.show_message(f"Error getting hover information: {e}", types.MessageType.Error)
                return None

        @self.server.feature(types.TEXT_DOCUMENT_DEFINITION)
        def goto_definition(
            ls: LanguageServer, params: types.DefinitionParams
        ) -> t.List[types.LocationLink]:
            """Jump to an object's definition."""
            try:
                uri = URI(params.text_document.uri)
                self._ensure_context_for_document(uri)
                document = ls.workspace.get_text_document(params.text_document.uri)
                if self.lsp_context is None:
                    raise RuntimeError(f"No context found for document: {document.path}")

                references = get_references(self.lsp_context, uri, params.position)
                return [
                    types.LocationLink(
                        target_uri=reference.uri,
                        target_selection_range=types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        ),
                        target_range=types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        ),
                        origin_selection_range=reference.range,
                    )
                    for reference in references
                ]
            except Exception as e:
                ls.show_message(f"Error getting references: {e}", types.MessageType.Error)
                return []

    def _context_get_or_load(self, document_uri: URI) -> LSPContext:
        if self.lsp_context is None:
            self._ensure_context_for_document(document_uri)
        if self.lsp_context is None:
            raise RuntimeError("No context found")
        return self.lsp_context

    def _ensure_context_for_document(
        self,
        document_uri: URI,
    ) -> None:
        """
        Ensure that a context exists for the given document if applicable by searching
        for a config.py or config.yml file in the parent directories.
        """
        if self.lsp_context is not None:
            context = self.lsp_context
            context.context.load()  # Reload or refresh context
            self.lsp_context = LSPContext(context.context)
            return

        # No context yet: try to find config and load it
        path = document_uri.to_path()
        if path.suffix not in (".sql", ".py"):
            return

        loaded = False
        # Ascend directories to look for config
        while path.parents and not loaded:
            for ext in ("py", "yml", "yaml"):
                config_path = path / f"config.{ext}"
                if config_path.exists():
                    try:
                        # Use user-provided instantiator to build the context
                        created_context = self.context_class(paths=[path])
                        self.lsp_context = LSPContext(created_context)
                        loaded = True
                        # Re-check context for document now that it's loaded
                        return self._ensure_context_for_document(document_uri)
                    except Exception as e:
                        self.server.show_message(
                            f"Error loading context: {e}", types.MessageType.Error
                        )
            path = path.parent

        return

    @staticmethod
    def _diagnostic_to_lsp_diagnostic(
        diagnostic: AnnotatedRuleViolation,
    ) -> t.Optional[types.Diagnostic]:
        if diagnostic.model._path is None:
            return None
        with open(diagnostic.model._path, "r", encoding="utf-8") as file:
            lines = file.readlines()

        # Get rule definition location for diagnostics link
        rule_location = diagnostic.rule.get_definition_location()
        rule_uri_wihout_extension = URI.from_path(rule_location.file_path)
        rule_uri = f"{rule_uri_wihout_extension.value}#L{rule_location.start_line}"

        # Use URI format to create a link for "related information"
        return types.Diagnostic(
            range=types.Range(
                start=types.Position(line=0, character=0),
                end=types.Position(line=len(lines), character=len(lines[-1])),
            ),
            message=diagnostic.violation_msg,
            severity=types.DiagnosticSeverity.Error
            if diagnostic.violation_type == "error"
            else types.DiagnosticSeverity.Warning,
            source="sqlmesh",
            code=diagnostic.rule.name,
            code_description=types.CodeDescription(href=rule_uri),
        )

    @staticmethod
    def _diagnostics_to_lsp_diagnostics(
        diagnostics: t.List[AnnotatedRuleViolation],
    ) -> t.List[types.Diagnostic]:
        lsp_diagnostics: t.List[types.Diagnostic] = []
        for diagnostic in diagnostics:
            lsp_diagnostic = SQLMeshLanguageServer._diagnostic_to_lsp_diagnostic(diagnostic)
            if lsp_diagnostic is not None:
                lsp_diagnostics.append(lsp_diagnostic)
        return lsp_diagnostics

    @staticmethod
    def _uri_to_path(uri: str) -> Path:
        """Convert a URI to a path."""
        return URI(uri).to_path()

    def start(self) -> None:
        """Start the server with I/O transport."""
        logging.basicConfig(level=logging.DEBUG)
        self.server.start_io()


def main() -> None:
    # Example instantiator that just uses the same signature as your original `Context` usage.
    sqlmesh_server = SQLMeshLanguageServer(context_class=Context)
    sqlmesh_server.start()


if __name__ == "__main__":
    main()
