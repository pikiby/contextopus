"""Presentation-only gateway to enabled modules' read-only library operations."""
import re


class LibraryBackend:
    def __init__(self, application):
        self.application = application

    def modules(self):
        modules = []
        for item in self.application.query("core.modules"):
            if item.get("state") != "enabled":
                continue
            ident = item["id"]
            available = ident + ".library" in item.get("operations", [])
            modules.append({"id": ident, "title": item.get("title") or ident.replace("_", " ").replace("-", " ").title(),
                            "description": item.get("description", ""), "available": available,
                            **({} if available else {"error": "This module does not provide a library contribution yet."})})
        modules.append({"id": "core", "title": "Core Documents", "description": "Core document collections and shared instructions", "available": True})
        return {"modules": modules}

    def provider_settings(self, module=None, source=None, enabled=None):
        result = {}
        for item in self.application.query('core.modules'):
            if item.get('state') != 'enabled' or not item.get('provider_settings'):
                continue
            if module is not None and item['id'] != module:
                continue
            declaration = item['provider_settings']
            operation = declaration['configure' if source is not None else 'status']
            if not operation.startswith(item['id'] + '.') or operation not in item.get('operations', []):
                raise ValueError('Invalid provider settings contribution')
            if source is None:
                result[item['id']] = self.application.query(operation)
            else:
                if not isinstance(enabled, bool):
                    raise ValueError('enabled must be a boolean')
                return self.application.execute(operation, {'source': source, 'enabled': enabled})
        if source is not None:
            raise ValueError('Module has no enabled provider settings')
        return result

    def call(self, module, action, **arguments):
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", module or ""):
            raise ValueError("Invalid module id")
        module_info = next((m for m in self.modules()["modules"] if m["id"] == module), None)
        if module_info is None:
            raise ValueError("Module is not enabled")
        if not module_info["available"]:
            raise ValueError(module_info["error"])
        operation = module + ".library"
        # Do not trust a module's operation name to imply a query capability.
        description = self.application.query("describe", {"operation": operation})
        if description.get("kind") != "query":
            raise ValueError("Library contributions must be read-only queries")
        return self.application.query(operation, {"action": action, **arguments})

    def _root(self):
        from pathlib import Path
        if hasattr(self.application, 'layout'):
            return Path(self.application.layout.root)
        return Path(self.application.root).expanduser().resolve()

    def _collections(self):
        if hasattr(self.application, 'collections'):
            return self.application.collections
        from ...core.documents.collections import DocumentCollections
        from ...core.storage import StorageLayout
        return DocumentCollections(StorageLayout.resolve(self._root()))

    def _settings_obj(self):
        from ...core.settings import Settings
        from ...core.storage import StorageLayout
        return Settings(StorageLayout.resolve(self._root()))

    def contexts_overview(self):
        fields = ['documents_approval_required', 'instructions_approval_required', 'git_history_enabled']
        settings_service = self._settings_obj()
        collections = self._collections()
        root = settings_service.snapshot(fields=fields)
        contexts = []
        for record in collections.list('contexts'):
            metadata = record['metadata']
            slug = metadata.get('slug') or record['path'].split('/')[0]
            settings = settings_service.snapshot(module='core', context=record['id'], fields=fields)
            workdirs = metadata.get('workdirs', [])
            if not isinstance(workdirs, list):
                workdirs = []
            if isinstance(metadata.get('workdir'), str) and metadata['workdir'] not in workdirs:
                workdirs = [*workdirs, metadata['workdir']]
            contexts.append({'id': record['id'], 'slug': slug, 'title': record['title'], 'workdirs': workdirs, 'settings': settings['values'], 'sources': settings['sources']})
        return {'contexts': contexts, 'settings': root['values'], 'sources': root['sources']}

    def update_context_setting(self, context, field, value):
        if field not in ('documents_approval_required', 'instructions_approval_required', 'git_history_enabled') or type(value) is not bool:
            raise ValueError('Invalid setting')
        if context is not None and (not isinstance(context, str) or not context):
            raise ValueError('Invalid context')
        if hasattr(self.application, 'settings_preview'):
            preview = self.application.settings_preview({field: value}, module='core' if context else None, context=context)
            return self.application.settings_apply(preview['preview_revision'], confirmation='web-ui toggle')
        collections = self._collections()
        resolved_ctx = context
        if context:
            try:
                resolved_ctx = collections.read('contexts', context)['id']
            except Exception:
                pass
        settings_service = self._settings_obj()
        preview = settings_service.preview({field: value}, module='core' if resolved_ctx else None, context=resolved_ctx)
        return settings_service.apply(preview['preview_revision'], confirmation='web-ui toggle')

    def _history(self):
        from ...core.history import GitRepository
        return GitRepository(self._root() / 'core')

    def history_log(self, limit=50):
        repo = self._history()
        repo.ensure()
        return repo.log(limit)

    def history_diff(self, commit, path=None):
        return {'diff': self._history().diff(commit, path)}

    def history_revert(self, commit, path):
        from pathlib import Path
        from ...core.documents.files import parse_document
        repo = self._history()
        collections = self._collections()
        path = repo._path(path)
        # Revert only a file actually changed by this commit, and only while its
        # current bytes still match that commit. Never overwrite later changes.
        changed = {entry['path'] for item in repo.log(500) if item['commit'] == repo.command('rev-parse', repo._commit(commit)).stdout.strip() for entry in item['files']}
        if path not in changed:
            raise ValueError('Path was not changed by this commit')
        target = repo.root / path
        if any((repo.root / Path(*Path(path).parts[:i])).is_symlink() for i in range(1, len(Path(path).parts) + 1)):
            raise ValueError('Symlinks are not supported')
        now = target.read_bytes() if target.is_file() else None
        commit_file = repo.command('show', commit + ':' + path, check=False, binary=True)
        at_commit = commit_file.stdout if not commit_file.returncode else None
        if now != at_commit:
            raise ValueError('File has changed since this commit')
        previous = repo.file_at_parent(commit, path)
        parts = path.split('/')
        if parts[0] in ('documents', 'instructions'):
            if parts[0] == 'documents' and len(parts) >= 3:
                collection, relative = parts[1], '/'.join(parts[2:])
            elif parts[0] == 'instructions' and len(parts) >= 2:
                collection, relative = 'instructions', '/'.join(parts[1:])
            else:
                raise ValueError('Invalid document path')
            store = collections.store(collection)
            current = store.read(relative) if now is not None else None
            if previous is None:
                if current is None:
                    raise ValueError('File already absent')
                from ...core.documents.links import collect_relations
                from ...core.rag import DocumentKey
                relations = collect_relations(collections, DocumentKey('core', collection, current['id']))
                if not relations['complete'] or relations['incoming']:
                    raise ValueError('Cannot delete document with incoming links or incomplete scan')
                result = collections.delete(collection, relative, expected_revision=current['revision'])
            else:
                document = parse_document(previous, location=relative, module='core', collection=collection)
                result = collections.save(collection, relative, document.metadata, document.text, expected_revision=current['revision'] if current else None)
        elif parts[0] == 'configuration' and path.endswith('.json'):
            import json
            if previous is not None:
                parsed = json.loads(previous)
                if not isinstance(parsed, dict):
                    raise ValueError('Invalid configuration')
            repo.ensure()
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix('.tmp')
                temp.write_bytes(previous)
                temp.replace(target)
            repo.commit_all('Revert ' + path)
            result = {'saved': True, 'path': path}
        else:
            raise ValueError('Unsupported history path')
        return result
