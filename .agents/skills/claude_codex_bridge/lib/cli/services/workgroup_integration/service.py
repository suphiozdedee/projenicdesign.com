from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Callable, Iterable

from project.ids import compute_project_id
from project.resolver import ProjectContext
from storage.atomic import atomic_write_json
from storage.locks import file_lock
from workspace.git_worktree import (
    branch_exists,
    delete_owned_branch,
    is_registered_worktree,
    list_registered_worktrees,
    remove_clean_registered_worktree,
)
from workspace.materializer import WorkspaceMaterializer
from workspace.planner import WorkspacePlanner

from cli.services.loop_execution_scope import path_allowed_by_scope, scopes_overlap

from .authority_intents import (
    create_merge_intent,
    create_node_commit_intent,
    merge_intent_matches,
    node_commit_intent_matches,
)
from .git_ops import GitOperations
from .models import (
    GitIntegrationError,
    MAX_WORKGROUP_NODES,
    VerificationCommand,
    WORKGROUP_GIT_TRANSACTION_SCHEMA,
    WORKGROUP_GIT_TRANSACTION_VERSION,
    WorkgroupNodeSpec,
)
from .verification_quarantine import preserve_verification_delta, remove_captured_untracked


class WorkgroupGitIntegration:
    def __init__(
        self,
        *,
        project_root: Path,
        state_path: Path,
        task_id: str,
        loop_id: str,
        bundle_revision: int,
        bundle_digest: str,
        nodes: Iterable[WorkgroupNodeSpec],
        integration_verification: Iterable[VerificationCommand] = (),
        root_verification: Iterable[VerificationCommand] = (),
        verify_each_layer: bool = False,
        workspace_root: Path | None = None,
        quarantine_root: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.state_path = Path(state_path).expanduser().resolve()
        self.lock_path = self.state_path.with_name(f'{self.state_path.name}.lock')
        self.task_id = _segment(task_id, field_name='task_id')
        self.loop_id = _segment(loop_id, field_name='loop_id')
        if isinstance(bundle_revision, bool) or not isinstance(bundle_revision, int) or bundle_revision <= 0:
            raise ValueError('bundle_revision must be a positive integer')
        self.bundle_revision = bundle_revision
        self.bundle_digest = _digest(bundle_digest, field_name='bundle_digest')
        self.nodes = tuple(nodes)
        self.integration_verification = tuple(integration_verification)
        self.root_verification = tuple(root_verification)
        if len(self.integration_verification) > 32 or len(self.root_verification) > 32:
            raise ValueError('verification command sets are limited to 32 commands each')
        self.verify_each_layer = bool(verify_each_layer)
        self._validate_nodes()
        self.layers = _dependency_layers(self.nodes)
        self.ordered_nodes = tuple(
            sorted(
                self.nodes,
                key=lambda node: (
                    self.layers[node.node_id],
                    node.integration_order,
                    node.node_id,
                ),
            )
        )
        identity = {
            'project_root': str(self.project_root),
            'task_id': self.task_id,
            'loop_id': self.loop_id,
            'bundle_revision': self.bundle_revision,
            'bundle_digest': self.bundle_digest,
        }
        self.transaction_key = hashlib.sha256(_canonical_json(identity)).hexdigest()[:12]
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else self.project_root / '.ccb' / 'workspaces' / 'workgroups' / self.transaction_key
        )
        self.project_context = ProjectContext(
            cwd=self.project_root,
            project_root=self.project_root,
            config_dir=self.project_root / '.ccb',
            project_id=compute_project_id(self.project_root),
            source='workgroup-integration',
        )
        self.quarantine_root = (
            Path(quarantine_root).expanduser().resolve()
            if quarantine_root is not None
            else self.project_root.parent
            / '.ccb-workgroup-quarantine'
            / self.project_context.project_id
        )
        try:
            self.quarantine_root.relative_to(self.project_root)
        except ValueError:
            pass
        else:
            raise ValueError('quarantine_root must be outside project_root')
        self.planner = WorkspacePlanner()
        self.materializer = WorkspaceMaterializer()
        self._checkpoint_hook: Callable[[str, dict[str, object]], None] | None = None

    @classmethod
    def from_bundle(
        cls,
        *,
        project_root: Path,
        state_path: Path,
        loop_id: str,
        bundle: dict[str, object],
        bundle_digest: str,
        integration_verification: Iterable[VerificationCommand] = (),
        root_verification: Iterable[VerificationCommand] = (),
        verify_each_layer: bool = False,
        workspace_root: Path | None = None,
        quarantine_root: Path | None = None,
    ) -> WorkgroupGitIntegration:
        raw_nodes = bundle.get('nodes')
        if not isinstance(raw_nodes, list):
            raise ValueError('orchestration bundle nodes must be a list')
        return cls(
            project_root=project_root,
            state_path=state_path,
            task_id=str(bundle.get('task_id') or ''),
            loop_id=loop_id,
            bundle_revision=bundle.get('bundle_revision'),
            bundle_digest=bundle_digest,
            nodes=tuple(WorkgroupNodeSpec.from_bundle_node(node) for node in raw_nodes),
            integration_verification=integration_verification,
            root_verification=root_verification,
            verify_each_layer=verify_each_layer,
            workspace_root=workspace_root,
            quarantine_root=quarantine_root,
        )

    def preflight(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            if self.state_path.is_file():
                return self._load_state()
            git = self._git()
            if git.repository_root() != self.project_root:
                raise GitIntegrationError(
                    'git_project_root_mismatch',
                    'preflight',
                    'project_root must be the Git repository root',
                    details={
                        'project_root': str(self.project_root),
                        'repository_root': str(git.repository_root()),
                    },
                )
            status = git.status_lines(self.project_root, ignore_controller_state=True)
            if status:
                raise GitIntegrationError(
                    'dirty_project_root',
                    'preflight',
                    'project root must be clean before workgroup execution',
                    details={'status': list(status)},
                )
            base_commit = git.head(self.project_root)
            base_tree = git.commit_tree_digest(self.project_root, base_commit)
            plans = self._planned_records(base_commit)
            self._reject_plan_collisions(plans)
            now = _now()
            state: dict[str, object] = {
                'schema': WORKGROUP_GIT_TRANSACTION_SCHEMA,
                'schema_version': WORKGROUP_GIT_TRANSACTION_VERSION,
                'state_revision': 0,
                'transaction_key': self.transaction_key,
                'status': 'preflighted',
                'created_at': now,
                'updated_at': now,
                'project': {
                    'root': str(self.project_root),
                    'project_id': self.project_context.project_id,
                    'repository_identity': git.repository_identity(),
                    'quarantine_root': str(self.quarantine_root),
                },
                'task': {
                    'task_id': self.task_id,
                    'loop_id': self.loop_id,
                    'bundle_revision': self.bundle_revision,
                    'bundle_digest': self.bundle_digest,
                    'base_commit': base_commit,
                    'base_tree_digest': base_tree,
                },
                'nodes': plans['nodes'],
                'integration': {
                    **plans['integration'],
                    'status': 'planned',
                    'head': base_commit,
                    'tree_digest': base_tree,
                    'merge_order': [],
                    'merges': [],
                    'merge_intent': None,
                    'merge_intents': [],
                    'checks': [],
                },
                'root': {
                    'preflight': {
                        'head': base_commit,
                        'branch': git.branch(self.project_root),
                        'tree_digest': base_tree,
                        'status': [],
                    },
                    'promotion': None,
                    'checks': [],
                    'rollback': None,
                    'verification': None,
                },
                'verification_policy': {
                    'verify_each_layer': self.verify_each_layer,
                    'integration_commands': [command.to_record() for command in self.integration_verification],
                    'root_commands': [command.to_record() for command in self.root_verification],
                },
                'failure': None,
                'cleanup': {
                    'evidence_captured': False,
                    'active_workspaces': [],
                    'eligible': False,
                    'reason': 'terminal_evidence_not_captured',
                },
            }
            self._save_state(state)
            return deepcopy(state)

    def state(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            return deepcopy(self._load_state())

    def prepare_integration(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            integration = _mapping(state['integration'])
            if integration['status'] != 'planned':
                workspace = Path(str(integration['worktree_path']))
                if workspace.is_dir() and self._git().head(workspace) != str(integration['head']):
                    self._recover_unrecorded_merge(state)
                self._validate_materialized_record(
                    integration,
                    expected_head=str(integration['head']),
                )
                return deepcopy(state)
            base_commit = str(_mapping(state['task'])['base_commit'])
            self._materialize_record(integration, base_commit=base_commit)
            self._checkpoint('after_integration_worktree_materialized', state)
            self._validate_materialized_record(integration, expected_head=base_commit)
            integration['status'] = 'ready'
            integration['head'] = base_commit
            integration['tree_digest'] = self._git().commit_tree_digest(
                Path(str(integration['worktree_path'])),
                base_commit,
            )
            state['status'] = 'integration_ready'
            self._save_state(state)
            return deepcopy(state)

    def prepare_node(self, node_id: str) -> dict[str, object]:
        node_id = _segment(node_id, field_name='node_id')
        with file_lock(self.lock_path):
            state = self._load_state()
            node = self._state_node(state, node_id)
            if node['status'] not in {'planned', 'materializing'}:
                self._validate_existing_node_record(state, node)
                return deepcopy(state)
            if node['status'] == 'planned':
                try:
                    base_commit = self._node_base_commit(state, node)
                except GitIntegrationError as exc:
                    raise self._persist_error(state, exc)
                node['base_commit'] = base_commit
                node['head'] = base_commit
                node['status'] = 'materializing'
                self._save_state(state)
            else:
                base_commit = str(node['base_commit'])
            self._materialize_record(node, base_commit=base_commit)
            self._checkpoint('after_node_worktree_materialized', state)
            self._validate_materialized_record(node, expected_head=base_commit)
            node['status'] = 'prepared'
            node['tree_digest'] = self._git().commit_tree_digest(
                Path(str(node['worktree_path'])),
                base_commit,
            )
            self._save_state(state)
            return deepcopy(state)

    def capture_review_input(self, node_id: str, *, worker_job_id: str) -> dict[str, object]:
        node_id = _segment(node_id, field_name='node_id')
        worker_job = _required_text(worker_job_id, field_name='worker_job_id')
        with file_lock(self.lock_path):
            state = self._load_state()
            node = self._state_node(state, node_id)
            if node['status'] not in {'prepared', 'review_rejected'}:
                raise self._error(
                    'node_not_reviewable',
                    f'nodes.{node_id}.review',
                    f'node status {node["status"]} cannot start review',
                )
            try:
                inspection = self._inspect_node_tree(node, require_uncommitted_head=True)
            except GitIntegrationError as exc:
                raise self._persist_error(state, exc)
            payload = {
                'node_id': node_id,
                'worker_job_id': worker_job,
                'base_commit': node['base_commit'],
                'head': inspection['head'],
                'tree_digest': inspection['tree_digest'],
                'changed_paths': inspection['changed_paths'],
                'deleted_paths': inspection['deleted_paths'],
            }
            input_digest = _sha256_record(payload)
            node['worker_job_id'] = worker_job
            node['head'] = inspection['head']
            node['tree_digest'] = inspection['tree_digest']
            node['changed_paths'] = inspection['changed_paths']
            node['deleted_paths'] = inspection['deleted_paths']
            node['review'] = {
                'input_digest': input_digest,
                'tree_digest': inspection['tree_digest'],
                'input': payload,
                'reviewer_job_id': None,
                'result': None,
                'recorded_at': None,
            }
            node['status'] = 'review_pending'
            self._save_state(state)
            return deepcopy(node['review'])

    def record_review(
        self,
        node_id: str,
        *,
        reviewer_job_id: str,
        result: str,
        input_digest: str,
        tree_digest: str,
    ) -> dict[str, object]:
        node_id = _segment(node_id, field_name='node_id')
        reviewer_job = _required_text(reviewer_job_id, field_name='reviewer_job_id')
        normalized_result = str(result or '').strip().lower()
        if normalized_result not in {'pass', 'rework', 'failed'}:
            raise ValueError('review result must be pass, rework, or failed')
        with file_lock(self.lock_path):
            state = self._load_state()
            node = self._state_node(state, node_id)
            review = _mapping(node.get('review'))
            if node['status'] != 'review_pending':
                if (
                    review.get('reviewer_job_id') == reviewer_job
                    and review.get('result') == normalized_result
                    and review.get('input_digest') == input_digest
                    and review.get('tree_digest') == tree_digest
                ):
                    return deepcopy(review)
                raise self._error(
                    'node_review_not_pending',
                    f'nodes.{node_id}.review',
                    f'node status {node["status"]} does not accept a review result',
                )
            if str(review.get('input_digest')) != str(input_digest):
                raise self._state_error(
                    state,
                    'review_input_digest_mismatch',
                    f'nodes.{node_id}.review',
                    'review result does not bind the recorded reviewer input',
                    details={
                        'expected': review.get('input_digest'),
                        'observed': input_digest,
                    },
                )
            if str(review.get('tree_digest')) != str(tree_digest):
                raise self._state_error(
                    state,
                    'review_tree_digest_mismatch',
                    f'nodes.{node_id}.review',
                    'review result does not bind the recorded tree digest',
                    details={
                        'expected': review.get('tree_digest'),
                        'observed': tree_digest,
                    },
                )
            try:
                inspection = self._inspect_node_tree(node, require_uncommitted_head=True)
            except GitIntegrationError as exc:
                raise self._persist_error(state, exc)
            if inspection['tree_digest'] != review['tree_digest']:
                raise self._state_error(
                    state,
                    'reviewed_tree_drift',
                    f'nodes.{node_id}.review',
                    'node tree changed after reviewer input was captured',
                    details={
                        'expected': review['tree_digest'],
                        'observed': inspection['tree_digest'],
                    },
                )
            review['reviewer_job_id'] = reviewer_job
            review['result'] = normalized_result
            review['recorded_at'] = _now()
            _list(node, 'reviews').append(deepcopy(review))
            node['status'] = 'review_passed' if normalized_result == 'pass' else 'review_rejected'
            self._save_state(state)
            return deepcopy(review)

    def record_node_failure(
        self,
        node_id: str,
        *,
        authority_id: str,
        source: str,
        job_id: str | None = None,
    ) -> dict[str, object]:
        node_id = _segment(node_id, field_name='node_id')
        failure_authority_id = _required_text(authority_id, field_name='authority_id')
        terminal_job_id = str(job_id or '').strip() or None
        failure_source = _required_text(source, field_name='source')
        with file_lock(self.lock_path):
            state = self._load_state()
            node = self._state_node(state, node_id)
            expected = {
                'schema': 'ccb.loop.workgroup_node_failure.v1',
                'authority_id': failure_authority_id,
                'job_id': terminal_job_id,
                'source': failure_source,
            }
            existing = node.get('terminal_failure')
            if isinstance(existing, dict):
                if {key: existing.get(key) for key in expected} != expected:
                    raise self._error(
                        'node_failure_authority_drift',
                        f'nodes.{node_id}.failure',
                        'replayed terminal failure does not match durable authority',
                    )
                failure = existing
            elif node['status'] not in {'prepared', 'review_pending', 'review_rejected'}:
                raise self._error(
                    'node_failure_not_recordable',
                    f'nodes.{node_id}.failure',
                    f'node status {node["status"]} cannot be excluded from integration',
                )
            else:
                workspace = Path(str(node['worktree_path']))
                git = self._git()
                head = git.head(workspace)
                base_commit = str(node['base_commit'])
                if head != base_commit:
                    raise self._state_error(
                        state,
                        'provider_created_authority_commit',
                        f'nodes.{node_id}.failure',
                        'failed node HEAD changed before controller exclusion',
                        details={'expected': base_commit, 'observed': head},
                    )
                changed_paths = git.changed_paths(workspace, base_commit, ignore_controller_state=True)
                deleted_paths = git.deleted_paths(workspace, base_commit, ignore_controller_state=True)
                self._validate_changed_paths(node, changed_paths)
                self._validate_changed_paths(node, deleted_paths)
                untracked_paths = git.untracked_paths(workspace)
                failure = {
                    **expected,
                    'status': 'captured',
                    'head': head,
                    'tree_digest': git.current_tree_digest(workspace, ignore_controller_state=True),
                    'worktree_status': list(git.status_lines(workspace, ignore_controller_state=True)),
                    'changed_paths': list(changed_paths),
                    'deleted_paths': list(deleted_paths),
                    'untracked_paths': list(untracked_paths),
                    'quarantine': None,
                    'recorded_at': _now(),
                }
                node['terminal_failure'] = failure
                self._save_state(state)
            workspace = Path(str(node['worktree_path']))
            git = self._git()
            if failure.get('status') == 'captured' and failure.get('worktree_status'):
                failure['quarantine'] = preserve_verification_delta(
                    project_root=workspace,
                    quarantine_root=self.quarantine_root,
                    transaction_key=f'{self.transaction_key}-{node_id}',
                    signature={
                        'head': failure['head'],
                        'tree_digest': failure['tree_digest'],
                        'status': failure['worktree_status'],
                    },
                    changed_paths=tuple(str(item) for item in failure['changed_paths']),
                    deleted_paths=tuple(str(item) for item in failure['deleted_paths']),
                    untracked_paths=tuple(str(item) for item in failure['untracked_paths']),
                    evidence_kind='node-failure',
                )
                failure['status'] = 'quarantined'
                self._save_state(state)
            if failure.get('status') in {'captured', 'quarantined'}:
                failure['status'] = 'restoring'
                self._save_state(state)
                self._checkpoint('after_node_failure_restore_intent', state)
            if failure.get('status') == 'restoring':
                current = {
                    'head': git.head(workspace),
                    'tree_digest': git.current_tree_digest(workspace, ignore_controller_state=True),
                    'status': list(git.status_lines(workspace, ignore_controller_state=True)),
                }
                expected_current = {
                    'head': failure['head'],
                    'tree_digest': failure['tree_digest'],
                    'status': failure['worktree_status'],
                }
                restored = {
                    'head': str(node['base_commit']),
                    'tree_digest': git.commit_tree_digest(workspace, str(node['base_commit'])),
                    'status': [],
                }
                if current == expected_current:
                    git.reset_hard(workspace, str(node['base_commit']))
                    remove_captured_untracked(
                        workspace,
                        tuple(str(item) for item in failure['untracked_paths']),
                    )
                    self._checkpoint('after_node_failure_worktree_restore', state)
                    current = {
                        'head': git.head(workspace),
                        'tree_digest': git.current_tree_digest(workspace, ignore_controller_state=True),
                        'status': list(git.status_lines(workspace, ignore_controller_state=True)),
                    }
                if current != restored:
                    raise self._state_error(
                        state,
                        'node_failure_workspace_drift',
                        f'nodes.{node_id}.failure',
                        'failed node worktree changed after evidence capture',
                        details={
                            'expected_captured': expected_current,
                            'expected_restored': restored,
                            'observed': current,
                        },
                    )
                failure['status'] = 'restored'
                failure['restored_at'] = _now()
            node['status'] = 'excluded'
            self._save_state(state)
            return deepcopy(node)

    def finalize_node(self, node_id: str) -> dict[str, object]:
        node_id = _segment(node_id, field_name='node_id')
        with file_lock(self.lock_path):
            state = self._load_state()
            node = self._state_node(state, node_id)
            if node.get('reviewed_commit'):
                self._validate_reviewed_commit(state, node)
                return deepcopy(node)
            if node['status'] not in {'review_passed', 'commit_pending'}:
                raise self._state_error(
                    state,
                    'missing_reviewer_pass',
                    f'nodes.{node_id}.finalize',
                    'node cannot be finalized without an exact reviewer pass',
                )
            review = _mapping(node.get('review'))
            workspace = Path(str(node['worktree_path']))
            git = self._git()
            head = git.head(workspace)
            base_commit = str(node['base_commit'])
            reviewed_tree = str(review['tree_digest'])
            if head == base_commit:
                try:
                    inspection = self._inspect_node_tree(node, require_uncommitted_head=True)
                except GitIntegrationError as exc:
                    raise self._persist_error(state, exc)
                if inspection['tree_digest'] != reviewed_tree:
                    raise self._state_error(
                        state,
                        'reviewed_tree_drift',
                        f'nodes.{node_id}.finalize',
                        'node tree changed after reviewer pass',
                        details={'expected': reviewed_tree, 'observed': inspection['tree_digest']},
                    )
                intent_value = node.get('commit_intent')
                if intent_value is None:
                    intent = self._create_node_commit_intent(state, node)
                    node['commit_intent'] = intent
                    node['status'] = 'commit_pending'
                    self._save_state(state)
                    self._checkpoint('after_node_commit_intent', state)
                else:
                    intent = _mapping(intent_value)
                    self._validate_node_intent_record(state, node, intent)
                commit = git.commit_all(workspace, str(intent['message']), ignore_controller_state=True)
                self._checkpoint('after_node_commit', state)
            else:
                intent_value = node.get('commit_intent')
                if not isinstance(intent_value, dict):
                    raise self._state_error(
                        state,
                        'node_commit_authority_drift',
                        f'nodes.{node_id}.finalize',
                        'node HEAD changed without a durable controller commit intent',
                        details={'base_commit': base_commit, 'observed': head},
                    )
                intent = intent_value
                commit = head
                self._validate_recoverable_node_commit(state, node, commit)
            commit_tree = git.commit_tree_digest(workspace, commit)
            if commit_tree != reviewed_tree:
                raise self._state_error(
                    state,
                    'reviewed_commit_tree_mismatch',
                    f'nodes.{node_id}.finalize',
                    'controller commit does not match the reviewed tree',
                    details={'reviewed_tree': reviewed_tree, 'commit_tree': commit_tree},
                )
            post_commit_status = git.status_lines(workspace, ignore_controller_state=True)
            if post_commit_status:
                raise self._state_error(
                    state,
                    'node_commit_worktree_dirty',
                    f'nodes.{node_id}.finalize',
                    'node worktree changed while the controller commit was created',
                    details={'status': list(post_commit_status)},
                )
            node['reviewed_commit'] = commit
            node['reviewed_tree_digest'] = commit_tree
            node['head'] = commit
            node['status'] = 'integration_ready'
            node['commit'] = {
                'commit': commit,
                'base_commit': base_commit,
                'tree_digest': commit_tree,
                'reviewer_job_id': review['reviewer_job_id'],
                'review_input_digest': review['input_digest'],
                'created_by': 'ccb-controller',
            }
            intent['status'] = 'completed'
            intent['commit'] = commit
            intent['completed_at'] = _now()
            self._save_state(state)
            self._checkpoint('after_node_state_write', state)
            return deepcopy(node)

    def integrate_ready(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            integration = _mapping(state['integration'])
            if integration['status'] == 'planned':
                raise self._error(
                    'integration_worktree_not_ready',
                    'integration',
                    'prepare_integration must run before merge',
                )
            if integration['status'] in {'merge_conflict', 'verification_failed'}:
                raise self._error(
                    'integration_terminal_failure',
                    'integration',
                    f'integration is terminal: {integration["status"]}',
                )
            self._recover_unrecorded_merge(state)
            while True:
                pending = self._next_unintegrated_node(state)
                if pending is None:
                    break
                node = self._state_node(state, pending.node_id)
                if node['status'] != 'integration_ready':
                    state['status'] = 'integration_pending'
                    self._save_state(state)
                    return deepcopy(state)
                if any(
                    self._state_node(state, dependency)['status'] != 'integrated'
                    for dependency in pending.depends_on
                ):
                    state['status'] = 'integration_pending'
                    self._save_state(state)
                    return deepcopy(state)
                self._merge_node(state, node)
                layer = self.layers[pending.node_id]
                if self.verify_each_layer and self._layer_complete(state, layer):
                    self._run_integration_checks(state, key=f'layer-{layer}', layer=layer)
            self._run_integration_checks(state, key='final', layer=None)
            integration['status'] = 'verified'
            state['status'] = 'integration_verified'
            self._save_state(state)
            return deepcopy(state)

    def promote(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            integration = _mapping(state['integration'])
            if integration['status'] != 'verified':
                raise self._error(
                    'integration_not_verified',
                    'root.promotion',
                    'all nodes and integration verification must pass before promotion',
                )
            root = _mapping(state['root'])
            promotion = root.get('promotion')
            git = self._git()
            base_commit = str(_mapping(state['task'])['base_commit'])
            integrated_head = str(integration['head'])
            actual_head = git.head(self.project_root)
            expected_branch = str(_mapping(root['preflight']).get('branch') or '')
            actual_branch = git.branch(self.project_root)
            status = git.status_lines(self.project_root, ignore_controller_state=True)
            if isinstance(promotion, dict) and promotion.get('status') == 'applied':
                self._validate_promoted_root(state)
                return deepcopy(state)
            if status:
                raise self._state_error(
                    state,
                    'root_drift_before_promotion',
                    'root.promotion',
                    'project root is dirty; promotion refused without overwriting user changes',
                    details={'head': actual_head, 'status': list(status)},
                )
            if actual_branch != expected_branch or actual_head not in {base_commit, integrated_head}:
                raise self._state_error(
                    state,
                    'root_drift_before_promotion',
                    'root.promotion',
                    'project root HEAD no longer matches preflight authority',
                    details={
                        'expected': base_commit,
                        'integrated_head': integrated_head,
                        'observed': actual_head,
                        'expected_branch': expected_branch,
                        'observed_branch': actual_branch,
                    },
                )
            if not isinstance(promotion, dict):
                promotion = {
                    'status': 'applying',
                    'before_head': base_commit,
                    'branch': expected_branch,
                    'before_tree_digest': git.commit_tree_digest(self.project_root, base_commit),
                    'integrated_head': integrated_head,
                    'integrated_tree_digest': integration['tree_digest'],
                    'after_head': None,
                    'after_tree_digest': None,
                    'recovered': False,
                }
                root['promotion'] = promotion
                state['status'] = 'promotion_pending'
                self._save_state(state)
            if actual_head == base_commit:
                result = git.merge_ff_only(self.project_root, integrated_head)
                if result.returncode != 0:
                    raise self._state_error(
                        state,
                        'root_promotion_failed',
                        'root.promotion',
                        'git fast-forward promotion failed',
                        details={
                            'stdout': result.stdout,
                            'stderr': result.stderr,
                            'exit_code': result.returncode,
                        },
                    )
                self._checkpoint('after_root_promotion', state)
            else:
                promotion['recovered'] = True
            self._validate_promoted_root(state)
            promotion['status'] = 'applied'
            promotion['after_head'] = integrated_head
            promotion['after_tree_digest'] = git.current_tree_digest(
                self.project_root,
                ignore_controller_state=True,
            )
            promotion['applied_at'] = _now()
            state['status'] = 'promoted'
            self._save_state(state)
            self._checkpoint('after_promotion_state_write', state)
            return deepcopy(state)

    def verify_root(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            root = _mapping(state['root'])
            promotion = root.get('promotion')
            if not isinstance(promotion, dict) or promotion.get('status') != 'applied':
                raise self._error(
                    'root_not_promoted',
                    'root.verification',
                    'promotion must be applied before project-root verification',
                )
            verification_value = root.get('verification')
            if (
                isinstance(verification_value, dict)
                and verification_value.get('status') == 'running'
            ):
                self._capture_interrupted_root_verification(state, verification_value)
                check = self._complete_failed_root_verification(state, verification_value)
                raise self._error(
                    'root_verification_failed',
                    'root.verification',
                    'project-root verification was interrupted and promotion was rolled back',
                    details={'check': check},
                )
            if isinstance(verification_value, dict) and verification_value.get('status') in {
                'captured',
                'quarantined',
            }:
                check = self._complete_failed_root_verification(state, verification_value)
                raise self._error(
                    'root_verification_failed',
                    'root.verification',
                    'project-root verification failed and promotion was rolled back',
                    details={'check': check},
                )
            existing = _check_record(root.get('checks'), key='final')
            if existing is not None:
                if existing.get('status') != 'pass':
                    raise self._error(
                        'root_verification_failed',
                        'root.verification',
                        'recorded project-root verification did not pass',
                        details={'check': existing},
                    )
                return deepcopy(state)
            git = self._git()
            self._validate_promoted_root(state)
            verification = {
                'schema': 'ccb.loop.root_verification_intent.v1',
                'status': 'running',
                'prepared_state_revision': int(state['state_revision']) + 1,
                'commands_digest': _sha256_record(
                    [command.to_record() for command in self.root_verification]
                ),
                'before': self._root_signature(git),
                'prepared_at': _now(),
                'quarantine': None,
            }
            root['verification'] = verification
            self._save_state(state)
            self._checkpoint('after_root_verification_intent', state)
            results = [
                git.run_verification(self.project_root, command)
                for command in self.root_verification
            ]
            _remove_generated_runtime_state(self.project_root)
            signature = self._root_signature(git)
            status = tuple(str(item) for item in signature['status'])
            generated_untracked = tuple(
                path
                for path in git.untracked_paths(self.project_root)
                if not path.startswith('.ccb/')
            )
            passed = all(result['result'] == 'pass' for result in results) and not status
            check = {
                'key': 'final',
                'head': git.head(self.project_root),
                'tree_digest': git.current_tree_digest(
                    self.project_root,
                    ignore_controller_state=True,
                ),
                'results': results,
                'status': 'pass' if passed else 'failed',
                'post_status': list(status),
                'generated_untracked': list(generated_untracked),
                'recorded_at': _now(),
            }
            _list(root, 'checks').append(check)
            if not passed:
                integrated_head = str(promotion['integrated_head'])
                changed_paths = tuple(
                    path
                    for path in git.changed_paths(self.project_root, integrated_head)
                    if not path.startswith('.ccb/')
                )
                deleted_paths = tuple(
                    path
                    for path in git.deleted_paths(self.project_root, integrated_head)
                    if not path.startswith('.ccb/')
                )
                verification.update(
                    {
                        'status': 'captured',
                        'post': signature,
                        'changed_paths': list(changed_paths),
                        'deleted_paths': list(deleted_paths),
                        'untracked_paths': list(generated_untracked),
                        'check': deepcopy(check),
                        'captured_at': _now(),
                    }
                )
                state['status'] = 'root_verification_failed_pending_rollback'
                self._save_state(state)
                check = self._complete_failed_root_verification(state, verification)
                raise self._error(
                    'root_verification_failed',
                    'root.verification',
                    'project-root verification failed and promotion was rolled back',
                    details={'check': check},
                )
            verification['status'] = 'passed'
            verification['post'] = signature
            verification['check'] = deepcopy(check)
            verification['completed_at'] = _now()
            state['status'] = 'root_verified'
            self._save_state(state)
            return deepcopy(state)

    def _capture_interrupted_root_verification(
        self,
        state: dict[str, object],
        verification: dict[str, object],
    ) -> None:
        root = _mapping(state['root'])
        promotion = _mapping(root['promotion'])
        git = self._git()
        signature = self._root_signature(git)
        integrated_head = str(promotion['integrated_head'])
        changed_paths = tuple(
            path
            for path in git.changed_paths(self.project_root, integrated_head)
            if not path.startswith('.ccb/')
        )
        deleted_paths = tuple(
            path
            for path in git.deleted_paths(self.project_root, integrated_head)
            if not path.startswith('.ccb/')
        )
        untracked_paths = tuple(
            path
            for path in git.untracked_paths(self.project_root)
            if not path.startswith('.ccb/')
        )
        check = {
            'key': 'final',
            'head': signature['head'],
            'tree_digest': signature['tree_digest'],
            'results': [
                {
                    'label': 'controller-interrupted-root-verification',
                    'argv': [],
                    'timeout_seconds': None,
                    'exit_code': None,
                    'stdout': '',
                    'stderr': 'verification process ended before durable result capture',
                    'stdout_truncated': False,
                    'stderr_truncated': False,
                    'timed_out': False,
                    'interrupted': True,
                    'result': 'failed',
                }
            ],
            'status': 'failed',
            'post_status': list(signature['status']),
            'generated_untracked': list(untracked_paths),
            'recorded_at': _now(),
        }
        _list(root, 'checks').append(check)
        verification.update(
            {
                'status': 'captured',
                'post': signature,
                'changed_paths': list(changed_paths),
                'deleted_paths': list(deleted_paths),
                'untracked_paths': list(untracked_paths),
                'check': deepcopy(check),
                'interrupted': True,
                'captured_at': _now(),
            }
        )
        state['status'] = 'root_verification_failed_pending_rollback'
        self._save_state(state)

    def _complete_failed_root_verification(
        self,
        state: dict[str, object],
        verification: dict[str, object],
    ) -> dict[str, object]:
        if verification.get('status') == 'captured':
            quarantine = preserve_verification_delta(
                project_root=self.project_root,
                quarantine_root=self.quarantine_root,
                transaction_key=self.transaction_key,
                signature=_mapping(verification['post']),
                changed_paths=tuple(str(item) for item in verification['changed_paths']),
                deleted_paths=tuple(str(item) for item in verification['deleted_paths']),
                untracked_paths=tuple(str(item) for item in verification['untracked_paths']),
            )
            verification['quarantine'] = quarantine
            verification['status'] = 'quarantined'
            verification['quarantined_at'] = _now()
            self._save_state(state)
            self._checkpoint('after_root_verification_quarantine', state)
        self._rollback_verification_failure(state, verification)
        return deepcopy(_mapping(verification['check']))

    def _root_signature(self, git: GitOperations) -> dict[str, object]:
        return {
            'branch': git.branch(self.project_root),
            'head': git.head(self.project_root),
            'tree_digest': git.current_tree_digest(
                self.project_root,
                ignore_controller_state=True,
            ),
            'status': list(
                git.status_lines(self.project_root, ignore_controller_state=True)
            ),
        }

    def _rollback_verification_failure(
        self,
        state: dict[str, object],
        verification: dict[str, object],
    ) -> None:
        root = _mapping(state['root'])
        promotion = _mapping(root['promotion'])
        git = self._git()
        base_commit = str(promotion['before_head'])
        expected_branch = str(promotion['branch'])
        current = self._root_signature(git)
        base_clean = (
            current['branch'] == expected_branch
            and current['head'] == base_commit
            and not current['status']
            and current['tree_digest'] == promotion['before_tree_digest']
        )
        if base_clean:
            recovered = True
        else:
            recovered = False
            captured = _mapping(verification['post'])
            before = _mapping(verification['before'])
            if (
                captured.get('branch') != before.get('branch')
                or captured.get('head') != before.get('head')
            ):
                raise self._state_error(
                    state,
                    'rollback_root_drift',
                    'root.rollback',
                    'verification changed Git branch or HEAD authority; automatic rollback refused',
                    details={'before': before, 'captured': captured},
                )
            if current != verification.get('post'):
                raise self._state_error(
                    state,
                    'rollback_root_drift',
                    'root.rollback',
                    'project root changed after verification evidence capture; rollback refused',
                    details={'captured': verification.get('post'), 'observed': current},
                )
            git.reset_hard(self.project_root, base_commit)
            remove_captured_untracked(
                self.project_root,
                tuple(str(item) for item in verification['untracked_paths']),
            )
            self._checkpoint('after_root_rollback', state)
        final = self._root_signature(git)
        if not (
            final['branch'] == expected_branch
            and final['head'] == base_commit
            and final['tree_digest'] == promotion['before_tree_digest']
            and not final['status']
        ):
            raise self._state_error(
                state,
                'rollback_verification_failed',
                'root.rollback',
                'project root was not restored to exact pre-promotion authority',
                details={'expected_head': base_commit, 'observed': final},
            )
        root['rollback'] = {
            'status': 'restored',
            'reason': 'root_verification_failed',
            'head': final['head'],
            'tree_digest': final['tree_digest'],
            'recovered': recovered,
            'recorded_at': _now(),
        }
        promotion['status'] = 'rolled_back'
        verification['status'] = 'rolled_back'
        verification['completed_at'] = _now()
        state['status'] = 'rolled_back'
        self._save_state(state)
        self._checkpoint('after_rollback_state_write', state)

    def accept(self) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            root = _mapping(state['root'])
            check = _check_record(root.get('checks'), key='final')
            if check is None or check.get('status') != 'pass':
                raise self._error(
                    'root_verification_missing',
                    'accept',
                    'a passing project-root verification is required before acceptance',
                )
            self._validate_promoted_root(state)
            state['status'] = 'accepted'
            state['accepted_at'] = _now()
            self._save_state(state)
            return deepcopy(state)

    def rollback(self, *, reason: str) -> dict[str, object]:
        rollback_reason = _required_text(reason, field_name='rollback reason')
        with file_lock(self.lock_path):
            state = self._load_state()
            self._rollback_locked(state, reason=rollback_reason)
            return deepcopy(state)

    def close_without_promotion(self, *, result: str, reason: str) -> dict[str, object]:
        normalized_result = str(result or '').strip()
        if normalized_result not in {'partial', 'blocked', 'replan_required'}:
            raise ValueError('non-promoted integration closure requires partial, blocked, or replan_required')
        closure_reason = _required_text(reason, field_name='closure reason')
        with file_lock(self.lock_path):
            state = self._load_state()
            root = _mapping(state['root'])
            promotion = root.get('promotion')
            if isinstance(promotion, dict) and promotion.get('status') == 'applied':
                raise self._error(
                    'nonpromoted_closure_after_promotion',
                    'integration.closure',
                    'promoted integration must use rollback before non-pass closure',
                )
            existing = state.get('closure')
            if isinstance(existing, dict):
                if existing.get('result') != normalized_result or existing.get('reason') != closure_reason:
                    raise self._error(
                        'integration_closure_authority_drift',
                        'integration.closure',
                        'durable non-pass closure does not match replayed authority',
                    )
                return deepcopy(state)
            state['closure'] = {
                'schema': 'ccb.loop.workgroup_git_closure.v1',
                'result': normalized_result,
                'reason': closure_reason,
                'recorded_at': _now(),
            }
            state['status'] = (
                'replan_required' if normalized_result == 'replan_required' else 'integration_failed'
            )
            self._save_state(state)
            return deepcopy(state)

    def cleanup_readiness(
        self,
        *,
        evidence_captured: bool,
        active_workspaces: Iterable[Path] = (),
    ) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            existing_cleanup = _mapping(state['cleanup'])
            if existing_cleanup.get('status') == 'complete':
                return deepcopy(existing_cleanup)
            if (
                existing_cleanup.get('schema') == 'ccb.loop.workgroup_cleanup_intent.v1'
                and existing_cleanup.get('status') in {'executing', 'blocked'}
            ):
                raise self._error(
                    'cleanup_in_progress',
                    'cleanup',
                    'cleanup execution has a durable intent and must be resumed with cleanup()',
                )
            active = {str(Path(path).expanduser().resolve()) for path in active_workspaces}
            records = self._owned_cleanup_records(state)
            owned_paths = {str(Path(str(record['worktree_path'])).resolve()) for record in records}
            active_owned = sorted(active & owned_paths)
            dirty: list[str] = []
            missing: list[str] = []
            git = self._git()
            for path_text in sorted(owned_paths):
                path = Path(path_text)
                if not path.exists():
                    missing.append(path_text)
                elif git.status_lines(path, ignore_controller_state=True):
                    dirty.append(path_text)
            terminal = str(state.get('status')) in {
                'accepted',
                'rolled_back',
                'integration_failed',
                'replan_required',
            }
            eligible = bool(
                evidence_captured
                and terminal
                and not active_owned
                and not dirty
                and not missing
            )
            if not evidence_captured:
                reason = 'terminal_evidence_not_captured'
            elif not terminal:
                reason = 'transaction_not_terminal'
            elif active_owned:
                reason = 'owned_worktree_active'
            elif dirty:
                reason = 'owned_worktree_dirty'
            elif missing:
                reason = 'owned_worktree_missing'
            else:
                reason = 'eligible'
            state['cleanup'] = {
                'status': 'ready' if eligible else 'blocked',
                'evidence_captured': bool(evidence_captured),
                'active_workspaces': active_owned,
                'dirty_workspaces': dirty,
                'missing_workspaces': missing,
                'eligible': eligible,
                'reason': reason,
                'worktrees_preserved': True,
                'evaluated_at': _now(),
            }
            self._save_state(state)
            return deepcopy(state['cleanup'])

    def cleanup(self, *, active_workspaces: Iterable[Path]) -> dict[str, object]:
        with file_lock(self.lock_path):
            state = self._load_state()
            cleanup = _mapping(state['cleanup'])
            if cleanup.get('status') == 'complete':
                return deepcopy(cleanup)
            active = {str(Path(path).expanduser().resolve()) for path in active_workspaces}
            records = self._owned_cleanup_records(state)
            owned_paths = {
                str(Path(str(record['worktree_path'])).resolve()) for record in records
            }
            active_owned = sorted(active & owned_paths)
            if active_owned:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_worktree_active',
                    ', '.join(active_owned),
                    details={'active_workspaces': active_owned},
                )
            if cleanup.get('status') == 'ready':
                self._prepare_cleanup_intent(state, cleanup)
            elif (
                cleanup.get('schema') == 'ccb.loop.workgroup_cleanup_intent.v1'
                and cleanup.get('status') == 'blocked'
            ):
                cleanup['status'] = 'executing'
                cleanup['resumed_at'] = _now()
                self._save_state(state)
            elif cleanup.get('status') != 'executing':
                reason = str(cleanup.get('reason') or 'cleanup_not_ready')
                raise self._cleanup_error(
                    state,
                    cleanup,
                    reason if reason != 'eligible' else 'cleanup_not_ready',
                    reason,
                )
            self._execute_cleanup_intent(state, cleanup)
            return deepcopy(cleanup)

    def _prepare_cleanup_intent(
        self,
        state: dict[str, object],
        cleanup: dict[str, object],
    ) -> None:
        if not cleanup.get('evidence_captured'):
            raise self._cleanup_error(
                state,
                cleanup,
                'terminal_evidence_not_captured',
                'terminal evidence is not durable',
            )
        if str(state.get('status')) not in {
            'accepted',
            'rolled_back',
            'integration_failed',
            'replan_required',
        }:
            raise self._cleanup_error(
                state,
                cleanup,
                'transaction_not_terminal',
                str(state.get('status')),
            )
        if cleanup.get('active_workspaces'):
            raise self._cleanup_error(
                state,
                cleanup,
                'owned_worktree_active',
                ', '.join(str(item) for item in cleanup['active_workspaces']),
            )
        records = [
            ('integration', _mapping(state['integration'])),
            *[
                (str(spec.node_id), self._state_node(state, spec.node_id))
                for spec in self.ordered_nodes
                if self._state_node(state, spec.node_id).get('status') != 'planned'
            ],
        ]
        owned_worktrees: list[dict[str, object]] = []
        owned_branches: list[dict[str, object]] = []
        git = self._git()
        for owner, record in records:
            path = Path(str(record['worktree_path'])).resolve()
            branch = str(record['branch'])
            expected_head = (
                str(record['head'])
                if owner == 'integration'
                else str(record.get('reviewed_commit') or record.get('head') or '')
            )
            if not path.is_dir() or not is_registered_worktree(self.project_root, path):
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_worktree_missing',
                    str(path),
                )
            status = git.status_lines(path, ignore_controller_state=True)
            if status:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_worktree_dirty',
                    str(path),
                    details={'status': list(status)},
                )
            if not branch_exists(self.project_root, branch):
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_branch_missing',
                    branch,
                )
            observed_head = git.resolve_commit(self.project_root, branch)
            if observed_head != expected_head:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_branch_authority_drift',
                    branch,
                    details={'expected': expected_head, 'observed': observed_head},
                )
            owned_worktrees.append(
                {'owner': owner, 'path': str(path), 'status': 'pending'}
            )
            owned_branches.append(
                {
                    'owner': owner,
                    'branch': branch,
                    'expected_head': expected_head,
                    'status': 'pending',
                }
            )
        cleanup.update(
            {
                'schema': 'ccb.loop.workgroup_cleanup_intent.v1',
                'status': 'executing',
                'prepared_state_revision': int(state['state_revision']) + 1,
                'worktrees': owned_worktrees,
                'branches': owned_branches,
                'prepared_at': _now(),
            }
        )
        self._save_state(state)

    def _execute_cleanup_intent(
        self,
        state: dict[str, object],
        cleanup: dict[str, object],
    ) -> None:
        for item_value in _list(cleanup, 'worktrees'):
            item = _mapping(item_value)
            path = Path(str(item['path'])).resolve()
            status = str(item['status'])
            registered = is_registered_worktree(self.project_root, path)
            exists = path.exists()
            if status == 'removed':
                if registered or exists:
                    raise self._cleanup_error(
                        state,
                        cleanup,
                        'cleanup_replay_authority_drift',
                        str(path),
                    )
                continue
            if status == 'pending' and (not registered or not exists):
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_worktree_missing',
                    str(path),
                )
            if status == 'removing' and not registered and not exists:
                item['status'] = 'removed'
                item['recovered'] = True
                item['removed_at'] = _now()
                self._save_state(state)
                continue
            if not registered or not exists:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'cleanup_replay_authority_drift',
                    str(path),
                )
            dirty = self._git().status_lines(path, ignore_controller_state=True)
            if dirty:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_worktree_dirty',
                    str(path),
                    details={'status': list(dirty)},
                )
            _remove_cleanup_local_controller_state(path)
            if status == 'pending':
                item['status'] = 'removing'
                self._save_state(state)
            try:
                remove_clean_registered_worktree(self.project_root, path)
            except RuntimeError as exc:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_worktree_remove_failed',
                    str(path),
                    details={'error': str(exc)},
                ) from exc
            self._checkpoint('after_cleanup_worktree_removed', state)
            item['status'] = 'removed'
            item['recovered'] = False
            item['removed_at'] = _now()
            self._save_state(state)

        for item_value in _list(cleanup, 'branches'):
            item = _mapping(item_value)
            branch = str(item['branch'])
            expected_head = str(item['expected_head'])
            status = str(item['status'])
            exists = branch_exists(self.project_root, branch)
            if status == 'removed':
                if exists:
                    raise self._cleanup_error(
                        state,
                        cleanup,
                        'cleanup_replay_authority_drift',
                        branch,
                    )
                continue
            if status == 'pending' and not exists:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_branch_missing',
                    branch,
                )
            if status == 'removing' and not exists:
                item['status'] = 'removed'
                item['recovered'] = True
                item['removed_at'] = _now()
                self._save_state(state)
                continue
            try:
                if status == 'pending':
                    observed = self._git().resolve_commit(self.project_root, branch)
                    if observed != expected_head:
                        raise RuntimeError(
                            f'expected {expected_head}, observed {observed}'
                        )
                    item['status'] = 'removing'
                    self._save_state(state)
                delete_owned_branch(
                    self.project_root,
                    branch,
                    expected_commit=expected_head,
                )
            except RuntimeError as exc:
                raise self._cleanup_error(
                    state,
                    cleanup,
                    'owned_branch_remove_failed',
                    branch,
                    details={'error': str(exc)},
                ) from exc
            self._checkpoint('after_cleanup_branch_removed', state)
            item['status'] = 'removed'
            item['recovered'] = False
            item['removed_at'] = _now()
            self._save_state(state)

        cleanup['status'] = 'complete'
        cleanup['eligible'] = False
        cleanup['reason'] = 'complete'
        cleanup['worktrees_preserved'] = False
        cleanup['completed_at'] = _now()
        self._save_state(state)

    def _cleanup_error(
        self,
        state: dict[str, object],
        cleanup: dict[str, object],
        code: str,
        subject: str,
        *,
        details: dict[str, object] | None = None,
    ) -> GitIntegrationError:
        cleanup['status'] = 'blocked'
        cleanup['reason'] = code
        cleanup['blocker'] = {
            'code': code,
            'subject': subject,
            'details': dict(details or {}),
            'recorded_at': _now(),
        }
        return self._state_error(
            state,
            code,
            'cleanup',
            f'controller-owned cleanup blocked: {subject}',
            details=details,
        )

    def _owned_cleanup_records(self, state: dict[str, object]) -> list[dict[str, object]]:
        return [
            _mapping(state['integration']),
            *[
                self._state_node(state, spec.node_id)
                for spec in self.ordered_nodes
                if self._state_node(state, spec.node_id).get('status') != 'planned'
            ],
        ]

    def _git(self) -> GitOperations:
        try:
            return GitOperations(self.project_root)
        except RuntimeError as exc:
            raise GitIntegrationError(
                'git_repository_required',
                'preflight',
                'workgroup integration requires a Git repository',
                details={'project_root': str(self.project_root), 'error': str(exc)},
            ) from exc

    def _planned_records(self, base_commit: str) -> dict[str, object]:
        prefix = f'ccb/workgroup/{self.transaction_key}'
        integration = {
            'workspace_agent': f'wgi-{self.transaction_key[:8]}-int',
            'worktree_path': str((self.workspace_root / 'integration').resolve()),
            'branch': f'{prefix}/integration',
            'base_commit': base_commit,
        }
        nodes: dict[str, dict[str, object]] = {}
        for index, spec in enumerate(self.ordered_nodes, start=1):
            nodes[spec.node_id] = {
                **spec.to_record(),
                'layer': self.layers[spec.node_id],
                'workspace_agent': f'wgi-{self.transaction_key[:8]}-n{index}',
                'worktree_path': str((self.workspace_root / 'nodes' / spec.node_id).resolve()),
                'branch': f'{prefix}/{spec.node_id}',
                'base_commit': base_commit if not spec.depends_on else None,
                'head': base_commit if not spec.depends_on else None,
                'tree_digest': None,
                'changed_paths': [],
                'deleted_paths': [],
                'worker_job_id': None,
                'review': None,
                'reviews': [],
                'reviewed_commit': None,
                'reviewed_tree_digest': None,
                'commit': None,
                'commit_intent': None,
                'status': 'planned',
            }
        return {'integration': integration, 'nodes': nodes}

    def _reject_plan_collisions(self, plans: dict[str, object]) -> None:
        records = [
            _mapping(plans['integration']),
            *[_mapping(value) for value in _mapping(plans['nodes']).values()],
        ]
        registered = set(list_registered_worktrees(self.project_root))
        collisions: list[dict[str, str]] = []
        for record in records:
            path = Path(str(record['worktree_path'])).resolve()
            branch = str(record['branch'])
            if branch_exists(self.project_root, branch):
                collisions.append({'kind': 'branch', 'value': branch})
            if path.exists() or path in registered:
                collisions.append({'kind': 'worktree', 'value': str(path)})
        if collisions:
            raise GitIntegrationError(
                'controller_workspace_collision',
                'preflight',
                'controller branch or worktree already exists without resumable state',
                details={'collisions': collisions},
            )

    def _materialize_record(self, record: dict[str, object], *, base_commit: str) -> None:
        plan = self.planner.plan_controller_worktree(
            self.project_context,
            agent_name=str(record['workspace_agent']),
            workspace_path=Path(str(record['worktree_path'])),
            branch_name=str(record['branch']),
            base_commit=base_commit,
        )
        self.materializer.materialize(plan)

    def _validate_materialized_record(
        self,
        record: dict[str, object],
        *,
        expected_head: str,
    ) -> None:
        workspace = Path(str(record['worktree_path']))
        git = self._git()
        if not workspace.is_dir():
            raise self._error(
                'controller_worktree_missing',
                'workspace',
                f'controller worktree is missing: {workspace}',
            )
        branch = git.branch(workspace)
        if branch != str(record['branch']):
            raise self._error(
                'controller_branch_mismatch',
                'workspace',
                'controller worktree branch does not match durable state',
                details={'expected': record['branch'], 'observed': branch},
            )
        head = git.head(workspace)
        if head != expected_head:
            raise self._error(
                'controller_worktree_head_drift',
                'workspace',
                'controller worktree HEAD does not match durable state',
                details={'expected': expected_head, 'observed': head},
            )
        status = git.status_lines(workspace, ignore_controller_state=True)
        if status:
            raise self._error(
                'controller_worktree_dirty',
                'workspace',
                'controller worktree is unexpectedly dirty',
                details={'status': list(status)},
            )

    def _validate_existing_node_record(
        self,
        state: dict[str, object],
        node: dict[str, object],
    ) -> None:
        workspace = Path(str(node['worktree_path']))
        git = self._git()
        if not workspace.is_dir():
            raise self._state_error(
                state,
                'controller_worktree_missing',
                f'nodes.{node["node_id"]}.workspace',
                f'controller worktree is missing: {workspace}',
            )
        branch = git.branch(workspace)
        if branch != str(node['branch']):
            raise self._state_error(
                state,
                'controller_branch_mismatch',
                f'nodes.{node["node_id"]}.workspace',
                'controller worktree branch does not match durable state',
                details={'expected': node['branch'], 'observed': branch},
            )
        head = git.head(workspace)
        reviewed_commit = str(node.get('reviewed_commit') or '')
        if reviewed_commit:
            if head != reviewed_commit or git.status_lines(workspace, ignore_controller_state=True):
                raise self._state_error(
                    state,
                    'reviewed_commit_worktree_drift',
                    f'nodes.{node["node_id"]}.workspace',
                    'reviewed node worktree no longer matches its controller commit',
                    details={'expected': reviewed_commit, 'observed': head},
                )
            return
        base_commit = str(node.get('base_commit') or '')
        if node['status'] in {'review_passed', 'commit_pending'} and head != base_commit:
            self._validate_recoverable_node_commit(state, node, head)
            return
        if head != base_commit:
            raise self._state_error(
                state,
                'provider_created_authority_commit',
                f'nodes.{node["node_id"]}.workspace',
                'node HEAD changed before controller-owned finalize',
                details={'expected': base_commit, 'observed': head},
            )

    def _node_base_commit(self, state: dict[str, object], node: dict[str, object]) -> str:
        dependencies = tuple(str(item) for item in node['depends_on'])
        if not dependencies:
            return str(_mapping(state['task'])['base_commit'])
        integration = _mapping(state['integration'])
        if integration['status'] == 'planned':
            raise self._error(
                'dependency_integration_not_ready',
                f'nodes.{node["node_id"]}.prepare',
                'integration worktree must be prepared before dependent nodes',
            )
        for dependency in dependencies:
            dependency_node = self._state_node(state, dependency)
            if dependency_node['status'] != 'integrated':
                raise self._error(
                    'dependency_not_integrated',
                    f'nodes.{node["node_id"]}.prepare',
                    f'dependency {dependency} is not integrated',
                )
        base_commit = str(integration['head'])
        git = self._git()
        for dependency in dependencies:
            dependency_commit = str(self._state_node(state, dependency)['reviewed_commit'])
            if not git.is_ancestor(Path(str(integration['worktree_path'])), dependency_commit, base_commit):
                raise self._error(
                    'dependency_commit_missing_from_integration',
                    f'nodes.{node["node_id"]}.prepare',
                    f'dependency {dependency} commit is not in integration HEAD',
                )
        return base_commit

    def _inspect_node_tree(
        self,
        node: dict[str, object],
        *,
        require_uncommitted_head: bool,
    ) -> dict[str, object]:
        workspace = Path(str(node['worktree_path']))
        git = self._git()
        head = git.head(workspace)
        base_commit = str(node['base_commit'])
        if require_uncommitted_head and head != base_commit:
            raise self._error(
                'provider_created_authority_commit',
                f'nodes.{node["node_id"]}.workspace',
                'node HEAD changed before controller-owned finalize',
                details={'expected': base_commit, 'observed': head},
            )
        changed_paths = git.changed_paths(workspace, base_commit, ignore_controller_state=True)
        if not changed_paths:
            raise self._error(
                'node_has_no_changes',
                f'nodes.{node["node_id"]}.workspace',
                'node worktree has no reviewable delta',
            )
        self._validate_changed_paths(node, changed_paths)
        deleted_paths = git.deleted_paths(workspace, base_commit, ignore_controller_state=True)
        self._validate_changed_paths(node, deleted_paths)
        return {
            'head': head,
            'tree_digest': git.current_tree_digest(workspace, ignore_controller_state=True),
            'changed_paths': list(changed_paths),
            'deleted_paths': list(deleted_paths),
        }

    def _validate_changed_paths(
        self,
        node: dict[str, object],
        changed_paths: Iterable[str],
    ) -> None:
        allowed_paths = [str(item) for item in node['allowed_paths']]
        outside: list[str] = []
        authority: list[str] = []
        for path in changed_paths:
            if path == '.ccb' or path.startswith('.ccb/') or path == '.git' or path.startswith('.git/'):
                authority.append(path)
            elif not path_allowed_by_scope(path, allowed_paths):
                outside.append(path)
        if authority:
            raise self._error(
                'node_authority_path_violation',
                f'nodes.{node["node_id"]}.scope',
                'node changed CCB or Git authority paths',
                details={'paths': sorted(authority)},
            )
        if outside:
            raise self._error(
                'node_scope_violation',
                f'nodes.{node["node_id"]}.scope',
                'node changed paths outside its execution contract',
                details={
                    'allowed_paths': allowed_paths,
                    'changed_paths': sorted(outside),
                },
            )

    def _node_commit_message(self, state: dict[str, object], node: dict[str, object]) -> str:
        review = _mapping(node['review'])
        return '\n'.join(
            (
                f'CCB reviewed node {node["node_id"]}',
                '',
                f'CCB-Project: {self.project_context.project_id}',
                f'CCB-Task: {self.task_id}',
                f'CCB-Loop: {self.loop_id}',
                f'CCB-Bundle-Revision: {self.bundle_revision}',
                f'CCB-Node: {node["node_id"]}',
                f'CCB-Reviewer-Job: {review["reviewer_job_id"]}',
                f'CCB-Review-Input: {review["input_digest"]}',
                f'CCB-Reviewed-Tree: {review["tree_digest"]}',
            )
        )

    def _create_node_commit_intent(
        self,
        state: dict[str, object],
        node: dict[str, object],
    ) -> dict[str, object]:
        review = _mapping(node['review'])
        return create_node_commit_intent(
            node_id=str(node['node_id']),
            base_commit=str(node['base_commit']),
            reviewed_tree_digest=str(review['tree_digest']),
            review_input_digest=str(review['input_digest']),
            reviewer_job_id=str(review['reviewer_job_id']),
            actor=self._git().controller_identity(),
            prepared_state_revision=int(state['state_revision']) + 1,
            message_prefix=self._node_commit_message(state, node),
            prepared_at=_now(),
        )

    def _validate_node_intent_record(
        self,
        state: dict[str, object],
        node: dict[str, object],
        intent: dict[str, object],
    ) -> None:
        review = _mapping(node['review'])
        if not node_commit_intent_matches(
            intent,
            node_id=str(node['node_id']),
            base_commit=str(node['base_commit']),
            reviewed_tree_digest=str(review['tree_digest']),
            review_input_digest=str(review['input_digest']),
            reviewer_job_id=str(review['reviewer_job_id']),
            actor=self._git().controller_identity(),
            message_prefix=self._node_commit_message(state, node),
        ):
            raise self._state_error(
                state,
                'node_commit_authority_drift',
                f'nodes.{node["node_id"]}.finalize',
                'durable node commit intent no longer matches review authority',
            )

    def _validate_recoverable_node_commit(
        self,
        state: dict[str, object],
        node: dict[str, object],
        commit: str,
    ) -> None:
        intent_value = node.get('commit_intent')
        if not isinstance(intent_value, dict):
            raise self._state_error(
                state,
                'node_commit_authority_drift',
                f'nodes.{node["node_id"]}.finalize',
                'node commit recovery requires a durable controller intent',
                details={'commit': commit},
            )
        intent = intent_value
        self._validate_node_intent_record(state, node, intent)
        git = self._git()
        workspace = Path(str(node['worktree_path']))
        parents = git.commit_parents(workspace, commit)
        message = git.commit_message(workspace, commit)
        tree = git.commit_tree_digest(workspace, commit)
        identity = git.commit_identity(workspace, commit)
        expected_identity = git.controller_identity()
        if (
            parents != (str(intent['base_commit']),)
            or tree != str(intent['reviewed_tree_digest'])
            or message != str(intent['message'])
            or identity != expected_identity
        ):
            raise self._state_error(
                state,
                'node_commit_authority_drift',
                f'nodes.{node["node_id"]}.finalize',
                'Git commit does not exactly match durable controller intent',
                details={
                    'commit': commit,
                    'parents': list(parents),
                    'tree_digest': tree,
                    'message_digest': _sha256_record(message),
                    'identity': identity,
                },
            )

    def _validate_reviewed_commit(self, state: dict[str, object], node: dict[str, object]) -> None:
        commit = str(node['reviewed_commit'])
        self._validate_recoverable_node_commit(state, node, commit)
        tree = self._git().commit_tree_digest(Path(str(node['worktree_path'])), commit)
        if tree != str(node['reviewed_tree_digest']):
            raise self._error(
                'reviewed_commit_drift',
                f'nodes.{node["node_id"]}.finalize',
                'reviewed commit tree no longer matches durable state',
            )

    def _create_merge_intent(
        self,
        state: dict[str, object],
        node: dict[str, object],
        *,
        before: str,
    ) -> dict[str, object]:
        return create_merge_intent(
            node_id=str(node['node_id']),
            head_before=before,
            reviewed_commit=str(node['reviewed_commit']),
            reviewed_tree_digest=str(node['reviewed_tree_digest']),
            actor=self._git().controller_identity(),
            prepared_state_revision=int(state['state_revision']) + 1,
            prepared_at=_now(),
        )

    def _validate_merge_intent_record(
        self,
        state: dict[str, object],
        node: dict[str, object],
        intent: dict[str, object],
    ) -> None:
        if not merge_intent_matches(
            intent,
            node_id=str(node['node_id']),
            head_before=str(_mapping(state['integration'])['head']),
            reviewed_commit=str(node['reviewed_commit']),
            reviewed_tree_digest=str(node['reviewed_tree_digest']),
            actor=self._git().controller_identity(),
        ):
            raise self._state_error(
                state,
                'integration_merge_authority_drift',
                f'integration.merge.{node["node_id"]}',
                'durable merge intent no longer matches deterministic integration authority',
            )

    def _recover_unrecorded_merge(self, state: dict[str, object]) -> None:
        integration = _mapping(state['integration'])
        workspace = Path(str(integration['worktree_path']))
        git = self._git()
        actual = git.head(workspace)
        recorded = str(integration['head'])
        if actual == recorded:
            return
        intent_value = integration.get('merge_intent')
        if not isinstance(intent_value, dict) or intent_value.get('status') != 'prepared':
            raise self._state_error(
                state,
                'integration_merge_authority_drift',
                'integration.recovery',
                'integration HEAD changed without a durable controller merge intent',
                details={'recorded': recorded, 'observed': actual},
            )
        intent = intent_value
        next_spec = self._next_unintegrated_node(state)
        if next_spec is None:
            raise self._state_error(
                state,
                'integration_head_drift',
                'integration.recovery',
                'integration HEAD changed after all merges were recorded',
                details={'recorded': recorded, 'observed': actual},
            )
        node = self._state_node(state, next_spec.node_id)
        reviewed_commit = str(node.get('reviewed_commit') or '')
        self._validate_merge_intent_record(state, node, intent)
        parents = git.commit_parents(workspace, actual)
        message = git.commit_message(workspace, actual)
        identity = git.commit_identity(workspace, actual)
        if (
            node['status'] != 'integration_ready'
            or parents != (recorded, reviewed_commit)
            or message != str(intent['message'])
            or identity != git.controller_identity()
        ):
            raise self._state_error(
                state,
                'integration_merge_authority_drift',
                'integration.recovery',
                'unrecorded integration commit does not exactly match durable merge intent',
                details={
                    'recorded': recorded,
                    'observed': actual,
                    'expected_node': next_spec.node_id,
                    'parents': list(parents),
                    'message_digest': _sha256_record(message),
                    'identity': identity,
                },
            )
        if git.status_lines(workspace, ignore_controller_state=True):
            raise self._state_error(
                state,
                'integration_worktree_dirty',
                'integration.recovery',
                'integration worktree is dirty during merge recovery',
            )
        self._validate_existing_node_record(state, node)
        self._validate_reviewed_commit(state, node)
        self._record_merge(state, node, before=recorded, after=actual, recovered=True)
        self._save_state(state)

    def _merge_node(self, state: dict[str, object], node: dict[str, object]) -> None:
        self._validate_existing_node_record(state, node)
        self._validate_reviewed_commit(state, node)
        integration = _mapping(state['integration'])
        workspace = Path(str(integration['worktree_path']))
        git = self._git()
        before = str(integration['head'])
        actual = git.head(workspace)
        if actual != before or git.status_lines(workspace, ignore_controller_state=True):
            raise self._state_error(
                state,
                'integration_worktree_drift',
                f'integration.merge.{node["node_id"]}',
                'integration worktree does not match durable merge authority',
                details={'expected_head': before, 'observed_head': actual},
            )
        reviewed_commit = str(node['reviewed_commit'])
        intent_value = integration.get('merge_intent')
        if not isinstance(intent_value, dict) or intent_value.get('status') == 'completed':
            intent = self._create_merge_intent(state, node, before=before)
            integration['merge_intent'] = intent
            self._save_state(state)
            self._checkpoint('after_integration_merge_intent', state)
        else:
            intent = intent_value
            self._validate_merge_intent_record(state, node, intent)
        result = git.merge_no_ff(
            workspace,
            reviewed_commit,
            str(intent['message']),
        )
        if result.returncode != 0:
            conflicts = git.conflict_paths(workspace)
            git.merge_abort(workspace)
            code = 'integration_merge_conflict' if conflicts else 'integration_merge_failed'
            integration['status'] = 'merge_conflict'
            intent['status'] = 'failed'
            intent['failure'] = {
                'exit_code': result.returncode,
                'conflict_paths': list(conflicts),
            }
            state['status'] = 'replan_required'
            raise self._state_error(
                state,
                code,
                f'integration.merge.{node["node_id"]}',
                'controller merge failed; no automatic resolution was attempted',
                details={
                    'node_id': node['node_id'],
                    'commit': reviewed_commit,
                    'conflict_paths': list(conflicts),
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'exit_code': result.returncode,
                },
            )
        after = git.head(workspace)
        self._checkpoint('after_integration_merge', state)
        parents = git.commit_parents(workspace, after)
        message = git.commit_message(workspace, after)
        identity = git.commit_identity(workspace, after)
        if (
            parents != (before, reviewed_commit)
            or message != str(intent['message'])
            or identity != git.controller_identity()
        ):
            raise self._state_error(
                state,
                'integration_merge_authority_drift',
                f'integration.merge.{node["node_id"]}',
                'integration merge does not exactly match durable controller intent',
                details={
                    'expected_parents': [before, reviewed_commit],
                    'observed_parents': list(parents),
                    'message_digest': _sha256_record(message),
                    'identity': identity,
                },
            )
        post_merge_status = git.status_lines(workspace, ignore_controller_state=True)
        if post_merge_status:
            raise self._state_error(
                state,
                'integration_worktree_dirty',
                f'integration.merge.{node["node_id"]}',
                'integration worktree changed while the controller merge was created',
                details={'status': list(post_merge_status)},
            )
        self._record_merge(state, node, before=before, after=after, recovered=False)
        self._save_state(state)
        self._checkpoint('after_integration_state_write', state)

    def _record_merge(
        self,
        state: dict[str, object],
        node: dict[str, object],
        *,
        before: str,
        after: str,
        recovered: bool,
    ) -> None:
        integration = _mapping(state['integration'])
        tree = self._git().commit_tree_digest(Path(str(integration['worktree_path'])), after)
        _list(integration, 'merge_order').append(str(node['node_id']))
        _list(integration, 'merges').append(
            {
                'node_id': node['node_id'],
                'layer': node['layer'],
                'integration_order': node['integration_order'],
                'reviewed_commit': node['reviewed_commit'],
                'head_before': before,
                'head_after': after,
                'tree_digest': tree,
                'recovered': recovered,
                'recorded_at': _now(),
            }
        )
        integration['head'] = after
        integration['tree_digest'] = tree
        integration['status'] = 'merging'
        intent = _mapping(integration['merge_intent'])
        intent['status'] = 'completed'
        intent['head_after'] = after
        intent['tree_digest'] = tree
        intent['completed_at'] = _now()
        _list(integration, 'merge_intents').append(deepcopy(intent))
        node['status'] = 'integrated'
        node['integration'] = {
            'status': 'integrated',
            'head': after,
            'tree_digest': tree,
            'recovered': recovered,
        }
        state['status'] = 'integration_pending'

    def _run_integration_checks(
        self,
        state: dict[str, object],
        *,
        key: str,
        layer: int | None,
    ) -> None:
        integration = _mapping(state['integration'])
        if _check_record(integration.get('checks'), key=key) is not None:
            return
        workspace = Path(str(integration['worktree_path']))
        git = self._git()
        results = [
            git.run_verification(workspace, command)
            for command in self.integration_verification
        ]
        status = git.status_lines(workspace, ignore_controller_state=True)
        passed = all(result['result'] == 'pass' for result in results) and not status
        check = {
            'key': key,
            'layer': layer,
            'head': git.head(workspace),
            'tree_digest': git.current_tree_digest(workspace, ignore_controller_state=True),
            'results': results,
            'post_status': list(status),
            'status': 'pass' if passed else 'failed',
            'recorded_at': _now(),
        }
        _list(integration, 'checks').append(check)
        self._save_state(state)
        if not passed:
            integration['status'] = 'verification_failed'
            state['status'] = 'integration_failed'
            raise self._state_error(
                state,
                'integration_verification_failed',
                f'integration.verification.{key}',
                'integration verification failed',
                details={'check': check},
            )

    def _validate_promoted_root(self, state: dict[str, object]) -> None:
        git = self._git()
        integration = _mapping(state['integration'])
        expected_head = str(integration['head'])
        actual_head = git.head(self.project_root)
        expected_branch = str(_mapping(_mapping(state['root'])['preflight']).get('branch') or '')
        actual_branch = git.branch(self.project_root)
        status = git.status_lines(self.project_root, ignore_controller_state=True)
        actual_tree = git.current_tree_digest(
            self.project_root,
            ignore_controller_state=True,
        )
        if (
            actual_branch != expected_branch
            or actual_head != expected_head
            or status
            or actual_tree != integration['tree_digest']
        ):
            raise self._error(
                'promoted_root_mismatch',
                'root.promotion',
                'project root does not match the exact integrated authority',
                details={
                    'expected_head': expected_head,
                    'observed_head': actual_head,
                    'expected_branch': expected_branch,
                    'observed_branch': actual_branch,
                    'expected_tree': integration['tree_digest'],
                    'observed_tree': actual_tree,
                    'status': list(status),
                },
            )

    def _rollback_locked(
        self,
        state: dict[str, object],
        *,
        reason: str,
    ) -> None:
        root = _mapping(state['root'])
        promotion = root.get('promotion')
        if not isinstance(promotion, dict):
            raise self._error(
                'promotion_not_applied',
                'root.rollback',
                'there is no promoted root delta to roll back',
            )
        git = self._git()
        base_commit = str(promotion['before_head'])
        integrated_head = str(promotion['integrated_head'])
        actual_head = git.head(self.project_root)
        expected_branch = str(promotion.get('branch') or '')
        actual_branch = git.branch(self.project_root)
        if root.get('rollback') and _mapping(root['rollback']).get('status') == 'restored':
            if actual_branch != expected_branch or actual_head != base_commit:
                raise self._error(
                    'rollback_authority_drift',
                    'root.rollback',
                    'durable rollback says restored but project HEAD differs',
                )
            return
        status = git.status_lines(self.project_root, ignore_controller_state=True)
        if actual_branch == expected_branch and actual_head == base_commit and not status:
            recovered = True
        else:
            recovered = False
            if actual_branch != expected_branch or actual_head != integrated_head:
                raise self._state_error(
                    state,
                    'rollback_root_drift',
                    'root.rollback',
                    'project root HEAD changed outside controller authority; rollback refused',
                    details={
                        'expected': integrated_head,
                        'observed': actual_head,
                        'expected_branch': expected_branch,
                        'observed_branch': actual_branch,
                    },
                )
            if status:
                raise self._state_error(
                    state,
                    'rollback_root_dirty',
                    'root.rollback',
                    'project root has unowned changes; rollback refused without overwriting them',
                    details={'status': list(status)},
                )
            git.reset_hard(self.project_root, base_commit)
            self._checkpoint('after_root_rollback', state)
        final_status = git.status_lines(self.project_root, ignore_controller_state=True)
        final_head = git.head(self.project_root)
        final_branch = git.branch(self.project_root)
        final_tree = git.current_tree_digest(
            self.project_root,
            ignore_controller_state=True,
        )
        expected_tree = str(promotion['before_tree_digest'])
        if (
            final_branch != expected_branch
            or final_head != base_commit
            or final_status
            or final_tree != expected_tree
        ):
            raise self._state_error(
                state,
                'rollback_verification_failed',
                'root.rollback',
                'project root was not restored to exact pre-promotion authority',
                details={
                    'expected_head': base_commit,
                    'observed_head': final_head,
                    'expected_branch': expected_branch,
                    'observed_branch': final_branch,
                    'expected_tree': expected_tree,
                    'observed_tree': final_tree,
                    'status': list(final_status),
                },
            )
        root['rollback'] = {
            'status': 'restored',
            'reason': reason,
            'head': final_head,
            'tree_digest': final_tree,
            'recovered': recovered,
            'recorded_at': _now(),
        }
        promotion['status'] = 'rolled_back'
        state['status'] = 'rolled_back'
        self._save_state(state)
        self._checkpoint('after_rollback_state_write', state)

    def _next_unintegrated_node(self, state: dict[str, object]) -> WorkgroupNodeSpec | None:
        for spec in self.ordered_nodes:
            if self._state_node(state, spec.node_id)['status'] not in {'integrated', 'excluded'}:
                return spec
        return None

    def _layer_complete(self, state: dict[str, object], layer: int) -> bool:
        return all(
            self._state_node(state, spec.node_id)['status'] in {'integrated', 'excluded'}
            for spec in self.ordered_nodes
            if self.layers[spec.node_id] == layer
        )

    def _validate_nodes(self) -> None:
        if not 1 <= len(self.nodes) <= MAX_WORKGROUP_NODES:
            raise ValueError(f'workgroup integration requires 1..{MAX_WORKGROUP_NODES} nodes')
        node_ids = [node.node_id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError('workgroup integration node ids must be unique')
        workgroups = [node.workgroup_id for node in self.nodes]
        if len(set(workgroups)) != len(workgroups):
            raise ValueError('workgroup integration workgroup ids must be unique')
        orders = [node.integration_order for node in self.nodes]
        if len(set(orders)) != len(orders):
            raise ValueError('workgroup integration integration_order values must be unique')
        known = set(node_ids)
        for node in self.nodes:
            missing = sorted(set(node.depends_on) - known)
            if missing:
                raise ValueError(f'{node.node_id}.depends_on unknown nodes: {", ".join(missing)}')
        _dependency_layers(self.nodes)
        dependencies = {node.node_id: set(node.depends_on) for node in self.nodes}
        for index, left in enumerate(self.nodes):
            for right in self.nodes[index + 1 :]:
                if _reaches(dependencies, left.node_id, right.node_id) or _reaches(
                    dependencies,
                    right.node_id,
                    left.node_id,
                ):
                    continue
                for left_scope in left.allowed_paths:
                    for right_scope in right.allowed_paths:
                        if scopes_overlap(left_scope, right_scope):
                            raise ValueError(
                                f'parallel nodes {left.node_id} and {right.node_id} '
                                f'have overlapping scope: {left_scope} <-> {right_scope}'
                            )

    def _state_node(self, state: dict[str, object], node_id: str) -> dict[str, object]:
        nodes = _mapping(state['nodes'])
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            raise self._error('unknown_node', f'nodes.{node_id}', f'unknown node: {node_id}')
        return node

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.is_file():
            raise self._error(
                'integration_state_missing',
                'state',
                'preflight must create integration state first',
            )
        try:
            payload = json.loads(self.state_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise self._error(
                'integration_state_invalid',
                'state',
                f'cannot read durable integration state: {exc}',
            ) from exc
        if not isinstance(payload, dict):
            raise self._error('integration_state_invalid', 'state', 'state must be a JSON object')
        self._validate_state_identity(payload)
        return payload

    def _validate_state_identity(self, state: dict[str, object]) -> None:
        if state.get('schema') != WORKGROUP_GIT_TRANSACTION_SCHEMA:
            raise self._error('integration_state_schema_mismatch', 'state.schema', 'unsupported state schema')
        if state.get('schema_version') != WORKGROUP_GIT_TRANSACTION_VERSION:
            raise self._error(
                'integration_state_version_mismatch',
                'state.schema_version',
                'unsupported state version',
            )
        try:
            task = _mapping(state.get('task'))
            project = _mapping(state.get('project'))
            persisted_nodes = _mapping(state.get('nodes'))
            integration = _mapping(state.get('integration'))
            policy = _mapping(state.get('verification_policy'))
        except ValueError as exc:
            raise self._error(
                'integration_state_invalid',
                'state',
                str(exc),
            ) from exc
        expected = {
            'task_id': self.task_id,
            'loop_id': self.loop_id,
            'bundle_revision': self.bundle_revision,
            'bundle_digest': self.bundle_digest,
        }
        observed = {key: task.get(key) for key in expected}
        if observed != expected or state.get('transaction_key') != self.transaction_key:
            raise self._error(
                'integration_state_identity_mismatch',
                'state',
                'durable state belongs to a different task/loop/bundle authority',
                details={'expected': expected, 'observed': observed},
            )
        if project.get('root') != str(self.project_root):
            raise self._error(
                'integration_state_identity_mismatch',
                'state.project.root',
                'durable state project root does not match this controller',
            )
        if project.get('quarantine_root') != str(self.quarantine_root):
            raise self._error(
                'integration_state_identity_mismatch',
                'state.project.quarantine_root',
                'durable verification quarantine authority changed after preflight',
            )
        git = self._git()
        if project.get('repository_identity') != git.repository_identity():
            raise self._error(
                'integration_repository_identity_drift',
                'state.project.repository_identity',
                'Git repository identity changed after preflight',
            )
        base_commit = _required_text(task.get('base_commit'), field_name='task.base_commit')
        if git.resolve_commit(self.project_root, base_commit) != base_commit:
            raise self._error(
                'integration_base_commit_drift',
                'state.task.base_commit',
                'recorded task base no longer resolves exactly',
            )
        if task.get('base_tree_digest') != git.commit_tree_digest(self.project_root, base_commit):
            raise self._error(
                'integration_base_tree_drift',
                'state.task.base_tree_digest',
                'recorded task base tree digest does not match Git authority',
            )
        expected_plans = self._planned_records(base_commit)
        expected_nodes = _mapping(expected_plans['nodes'])
        if set(persisted_nodes) != set(expected_nodes):
            raise self._error(
                'integration_state_node_drift',
                'state.nodes',
                'durable node set changed after preflight',
            )
        semantic_fields = (
            'node_id',
            'workgroup_id',
            'depends_on',
            'allowed_paths',
            'integration_order',
            'layer',
            'workspace_agent',
            'worktree_path',
            'branch',
        )
        for node_id, expected_node_value in expected_nodes.items():
            expected_node = _mapping(expected_node_value)
            observed_node = _mapping(persisted_nodes[node_id])
            if any(observed_node.get(field) != expected_node.get(field) for field in semantic_fields):
                raise self._error(
                    'integration_state_node_drift',
                    f'state.nodes.{node_id}',
                    'durable node semantics changed after preflight',
                )
        expected_integration = _mapping(expected_plans['integration'])
        for field in ('workspace_agent', 'worktree_path', 'branch', 'base_commit'):
            if integration.get(field) != expected_integration.get(field):
                raise self._error(
                    'integration_state_path_drift',
                    f'state.integration.{field}',
                    'durable integration workspace authority changed after preflight',
                )
        expected_policy = {
            'verify_each_layer': self.verify_each_layer,
            'integration_commands': [command.to_record() for command in self.integration_verification],
            'root_commands': [command.to_record() for command in self.root_verification],
        }
        if policy != expected_policy:
            raise self._error(
                'integration_verification_policy_drift',
                'state.verification_policy',
                'verification commands changed after preflight',
            )

    def _save_state(self, state: dict[str, object]) -> None:
        state['state_revision'] = int(state.get('state_revision') or 0) + 1
        state['updated_at'] = _now()
        atomic_write_json(self.state_path, state)

    def _state_error(
        self,
        state: dict[str, object],
        code: str,
        stage: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> GitIntegrationError:
        error = self._error(code, stage, message, details=details)
        state['failure'] = {**error.to_record(), 'recorded_at': _now()}
        self._save_state(state)
        return error

    def _persist_error(
        self,
        state: dict[str, object],
        error: GitIntegrationError,
    ) -> GitIntegrationError:
        state['failure'] = {**error.to_record(), 'recorded_at': _now()}
        self._save_state(state)
        return error

    def _error(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> GitIntegrationError:
        return GitIntegrationError(code, stage, message, details=details)

    def _checkpoint(self, name: str, state: dict[str, object]) -> None:
        if self._checkpoint_hook is not None:
            self._checkpoint_hook(name, deepcopy(state))


def _dependency_layers(nodes: Iterable[WorkgroupNodeSpec]) -> dict[str, int]:
    nodes_by_id = {node.node_id: node for node in nodes}
    layers: dict[str, int] = {}
    visiting: set[str] = set()

    def layer(node_id: str) -> int:
        if node_id in layers:
            return layers[node_id]
        if node_id in visiting:
            raise ValueError(f'workgroup dependency cycle includes {node_id}')
        visiting.add(node_id)
        node = nodes_by_id[node_id]
        value = 0 if not node.depends_on else 1 + max(layer(dep) for dep in node.depends_on)
        visiting.remove(node_id)
        layers[node_id] = value
        return value

    for node_id in sorted(nodes_by_id):
        layer(node_id)
    return layers


def _reaches(dependencies: dict[str, set[str]], start: str, target: str) -> bool:
    pending = list(dependencies.get(start, set()))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(dependencies.get(current, set()))
    return False


def _check_record(value: object, *, key: str) -> dict[str, object] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict) and item.get('key') == key:
            return item
    return None


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError('durable integration state contains a non-object field')
    return value


def _list(value: dict[str, object], key: str) -> list[object]:
    current = value.get(key)
    if not isinstance(current, list):
        raise ValueError(f'durable integration state field {key} must be a list')
    return current


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _sha256_record(value: object) -> str:
    return f'sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}'


def _digest(value: object, *, field_name: str) -> str:
    text = str(value or '').strip()
    if not text.startswith('sha256:') or len(text) != 71:
        raise ValueError(f'{field_name} must be a sha256: digest')
    try:
        int(text[7:], 16)
    except ValueError as exc:
        raise ValueError(f'{field_name} must be a sha256: digest') from exc
    return text.lower()


def _segment(value: object, *, field_name: str) -> str:
    text = str(value or '').strip()
    allowed = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-'
    if not text or any(char not in allowed for char in text):
        raise ValueError(f'{field_name} must be a non-empty identifier')
    if len(text) > 80:
        raise ValueError(f'{field_name} must be at most 80 characters')
    return text


def _required_text(value: object, *, field_name: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise ValueError(f'{field_name} must be non-empty')
    return text


def _remove_cleanup_local_controller_state(workspace: Path) -> None:
    binding = workspace / '.ccb-workspace.json'
    if binding.is_file() or binding.is_symlink():
        binding.unlink()
    _remove_generated_runtime_state(workspace)


def _remove_generated_runtime_state(workspace: Path) -> None:
    for relative in ('.pytest_cache', '.mypy_cache', '.ruff_cache', '__pycache__'):
        path = workspace / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file() or path.is_symlink():
            path.unlink()
    for cache_dir in workspace.rglob('__pycache__'):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
    for suffix in ('*.pyc', '*.pyo'):
        for path in workspace.rglob(suffix):
            if path.is_file() or path.is_symlink():
                path.unlink()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ['WorkgroupGitIntegration']
