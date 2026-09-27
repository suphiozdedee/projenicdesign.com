from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
from types import SimpleNamespace
from typing import Callable, Iterable
from uuid import uuid4

from storage.atomic import atomic_write_json, atomic_write_text
from storage.locks import file_lock
from storage.paths import PathLayout
from workspace.binding import WorkspaceBindingStore

from .loop_ask_first import submit_or_recover_ask_once
from .loop_orchestration_bundle import bundle_digest, task_input_digest, task_revision
from .loop_effective_capacity import (
    compile_project_effective_capacity_snapshot,
    effective_capacity_digest,
)
from .loop_topology import loop_topology
from .loop_workgroup_topology import compile_project_workgroup_mount_demand
from .plan_tasks import plan_task, task_execution_text
from .workgroup_integration import (
    GitIntegrationError,
    VerificationCommand,
    WorkgroupGitIntegration,
)


SCHEDULER_SCHEMA = 'ccb.loop.multi_workgroup_scheduler.v1'
ROUND_STATE_SCHEMA = 'ccb.loop.workgroup_round_state.v1'
TERMINAL_STATUSES = {'pass', 'partial', 'replan_required', 'blocked'}
PENDING_NODE_STATUSES = {
    'worker_pending',
    'worker_submission_unknown',
}
PENDING_CHAIN_EDGE_STATES = {
    'pending',
    'child_completed',
    'continuation_submitted',
}


class _SchedulerPending(RuntimeError):
    pass


class MultiWorkgroupScheduler:
    def __init__(
        self,
        context,
        *,
        loop_id: str,
        task_record: dict[str, object],
        bundle: dict[str, object],
        bundle_artifact: dict[str, object],
        services=None,
    ) -> None:
        self.context = context
        self.project_root = Path(context.project.project_root).resolve()
        self.loop_id = str(loop_id)
        self.task_record = task_record
        self.bundle = bundle
        self.bundle_artifact = bundle_artifact
        self.task_id = str(task_record['task_id'])
        self.loop_dir = self.project_root / '.ccb' / 'runtime' / 'loops' / self.loop_id
        self.state_path = self.loop_dir / 'workgroup_scheduler_state.json'
        self.events_path = self.loop_dir / 'workgroup_scheduler_events.jsonl'
        self.lock_path = self.loop_dir / 'workgroup_scheduler.lock'
        self.deps = _deps(services)
        self._checkpoint_hook: Callable[[str, dict[str, object]], None] | None = None

    def run_once(self) -> dict[str, object]:
        self.loop_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            try:
                state = self._load_or_initialize()
                if str(state['status']) in TERMINAL_STATUSES:
                    return self._payload(state)
                if str(state['status']) in {'result_imported', 'release_blocked'}:
                    self._release_and_cleanup(state)
                    return self._payload(state)
                self._validate_identity(state)
                return self._advance(state)
            except _SchedulerPending:
                return self._payload(state, action='topology_pending')
            except GitIntegrationError as exc:
                state = self._load_state_after_failure()
                self._finish_nonpass(
                    state,
                    result='replan_required',
                    source=exc.code,
                    failure=exc.to_record(),
                )
                return self._payload(state)
            except ValueError as exc:
                state = self._load_state_after_failure()
                self._finish_nonpass(
                    state,
                    result='replan_required',
                    source='scheduler_contract_invalid',
                    failure={'error': str(exc), 'stage': 'scheduler'},
                )
                return self._payload(state)

    def _advance(self, state: dict[str, object]) -> dict[str, object]:
        integration = self._integration()
        self._reconcile_pending_jobs(state, integration)
        if str(state['status']) in TERMINAL_STATUSES | {'result_imported'}:
            return self._payload(state)

        integrated_before = self._sync_integration(state, integration)
        frontier = self._ready_frontier(state)
        newly_submitted: list[str] = []
        if frontier:
            self._ensure_topology(state, integration, frontier, round_reviewer=False)
            for node_id in frontier:
                node = _node(state, node_id)
                if node['status'] != 'created':
                    continue
                previous = str(node['status'])
                result = self._submit_node_job(state, node, purpose='worker')
                result = _defer_pending_worker_chain(result, purpose='worker')
                node['worker'] = result
                if not bool(result.get('terminal')):
                    node['status'] = _pending_status(result, purpose='worker')
                elif str(result.get('status') or '') == 'completed':
                    self._consume_worker_chain_result(state, node, result, integration)
                else:
                    node['status'] = 'worker_failed'
                if node['status'] == 'worker_failed':
                    node['failure'] = {'source': 'worker_submission_failed', 'job': result}
                self._transition(state, node_id, previous, str(node['status']), result)
                newly_submitted.append(node_id)
            self._save(state)
        if newly_submitted and any(
            _node(state, node_id)['status'] in PENDING_NODE_STATUSES
            for node_id in newly_submitted
        ):
            state['ready_frontier'] = []
            self._save(state)
            return self._payload(state, action='submitted_ready_frontier')

        if any(_node(state, node_id)['status'] in PENDING_NODE_STATUSES for node_id in state['nodes']):
            return self._payload(state, action='provider_jobs_pending')

        integrated_after = self._sync_integration(state, integration)
        if integrated_after != integrated_before:
            return self._payload(state, action='integrated_reviewed_nodes')

        if self._all_nodes_integrated(state):
            self._verify_project_and_start_round_review(state, integration)
            return self._payload(state)

        frontier = self._ready_frontier(state)
        if frontier:
            return self._advance(state)
        failures = [
            node_id
            for node_id in state['nodes']
            if _node(state, node_id)['status'] in {'worker_failed', 'review_failed', 'blocked'}
        ]
        if failures:
            accepted = [
                node_id
                for node_id in state['nodes']
                if _node(state, node_id)['status'] in {'integration_ready', 'integrated'}
            ]
            for node_id in failures:
                node = _node(state, node_id)
                failure = _mapping(node.get('failure'))
                job = _mapping(failure.get('job'))
                job_id = str(job.get('job_id') or '').strip()
                integration.record_node_failure(
                    node_id,
                    authority_id=(
                        f'job:{job_id}'
                        if job_id
                        else f'controller:{self.loop_id}:{node_id}:{node["status"]}'
                    ),
                    job_id=job_id or None,
                    source=str(failure.get('source') or 'required_node_failure'),
                )
            if accepted:
                integration.integrate_ready()
                self._sync_integration(state, integration)
            self._finish_nonpass(
                state,
                result='partial' if accepted else 'blocked',
                source='required_node_failure',
                failure={'failed_nodes': failures, 'accepted_nodes': accepted},
            )
            return self._payload(state)
        return self._payload(state, action='scheduler_pending')

    def _load_or_initialize(self) -> dict[str, object]:
        if self.state_path.is_file():
            payload = json.loads(self.state_path.read_text(encoding='utf-8'))
            if not isinstance(payload, dict):
                raise RuntimeError(f'scheduler state is not an object: {self.state_path}')
            if str(payload.get('status') or '') == 'bundle_pending':
                self._complete_initialization(payload)
            return payload
        nodes = {
            str(node['node_id']): {
                'node_id': node['node_id'],
                'workgroup_id': node['workgroup_id'],
                'depends_on': list(node['depends_on']),
                'integration_order': node['integration_order'],
                'status': 'created',
                'attempt': 1,
                'rework_count': 0,
                'rework_history': [],
                'worker_agent': None,
                'reviewer_agent': None,
                'workspace_group': None,
                'worktree_path': None,
                'branch': None,
                'base_commit': None,
                'worker': None,
                'reviewer': None,
                'worker_rework': None,
                'reviewer_recheck': None,
                'review_input': None,
                'reviewed_commit': None,
                'failure': None,
            }
            for node in self.bundle['nodes']
        }
        state: dict[str, object] = {
            'schema': SCHEDULER_SCHEMA,
            'schema_version': 1,
            'state_revision': 0,
            'status': 'bundle_pending',
            'controller_status': 'bundle_pending',
            'loop_id': self.loop_id,
            'task_id': self.task_id,
            'task_revision': int(self.bundle['task_revision']),
            'task_digest': self.bundle['task_digest'],
            'capacity_digest': self.bundle['capacity_digest'],
            'bundle_revision': int(self.bundle['bundle_revision']),
            'bundle_digest': bundle_digest(self.bundle),
            'bundle': deepcopy(self.bundle),
            'bundle_artifact': deepcopy(self.bundle_artifact),
            'ready_frontier': [],
            'nodes': nodes,
            'topology': {'demand': None, 'apply': None, 'release': None},
            'integration': {'state_path': str(self.loop_dir / 'git-transaction.json')},
            'round_reviewer': None,
            'round_result': None,
            'round_result_source': None,
            'result_import': None,
            'cleanup': None,
            'failure': None,
            'created_at': _now(),
            'updated_at': _now(),
        }
        self._save(state)
        self._checkpoint('after_scheduler_state_before_git_preflight', state)
        self._complete_initialization(state)
        self._event(state, kind='scheduler_started', payload={})
        return state

    def _complete_initialization(self, state: dict[str, object]) -> None:
        integration = self._integration()
        integration.preflight()
        integration.prepare_integration()
        state['status'] = 'executing'
        state['controller_status'] = 'executing'
        self._save(state)

    def _load_state_after_failure(self) -> dict[str, object]:
        if not self.state_path.is_file():
            raise RuntimeError('scheduler failed before durable state initialization')
        payload = json.loads(self.state_path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            raise RuntimeError('scheduler durable state is invalid after failure')
        return payload

    def _validate_identity(self, state: dict[str, object]) -> None:
        expected = {
            'schema': SCHEDULER_SCHEMA,
            'loop_id': self.loop_id,
            'task_id': self.task_id,
            'task_revision': task_revision(self.task_record),
            'task_digest': task_input_digest(self.task_record),
            'capacity_digest': self.bundle['capacity_digest'],
            'bundle_revision': int(self.bundle['bundle_revision']),
            'bundle_digest': bundle_digest(self.bundle),
        }
        observed = {key: state.get(key) for key in expected}
        if observed != expected:
            raise ValueError(f'scheduler authority drift: expected {expected}, observed {observed}')
        current_capacity_digest = self.deps.capacity_digest(self.context)
        if current_capacity_digest != str(state['capacity_digest']):
            raise ValueError(
                'scheduler effective capacity drift: '
                f'expected {state["capacity_digest"]}, observed {current_capacity_digest}'
            )

    def _integration(self):
        if self.deps.integration_factory is not None:
            return self.deps.integration_factory(self)
        integration_commands = _verification_commands(
            self.project_root,
            self.bundle['integration']['verification_refs'],
            prefix='integration',
        )
        root_commands = _verification_commands(
            self.project_root,
            self.bundle['integration']['project_root_verification_refs'],
            prefix='root',
        )
        return WorkgroupGitIntegration.from_bundle(
            project_root=self.project_root,
            state_path=self.loop_dir / 'git-transaction.json',
            loop_id=self.loop_id,
            bundle=self.bundle,
            bundle_digest=f'sha256:{bundle_digest(self.bundle)}',
            integration_verification=integration_commands,
            root_verification=root_commands,
        )

    def _ready_frontier(self, state: dict[str, object]) -> list[str]:
        ready = []
        for source in self.bundle['nodes']:
            node_id = str(source['node_id'])
            node = _node(state, node_id)
            if node['status'] != 'created':
                continue
            if all(_node(state, str(dep))['status'] == 'integrated' for dep in source['depends_on']):
                ready.append(node_id)
        return ready

    def _ensure_topology(
        self,
        state: dict[str, object],
        integration,
        frontier: list[str],
        *,
        round_reviewer: bool,
    ) -> None:
        active = [
            node_id
            for node_id in state['nodes']
            if _node(state, node_id)['status'] not in {'created', 'integrated', 'worker_failed', 'review_failed', 'blocked'}
        ]
        active.extend(node_id for node_id in frontier if node_id not in active)
        if not active and not round_reviewer:
            raise ValueError('workgroup topology requires an active node or round reviewer control')
        demand = self.deps.compile_mount_demand(
            self.project_root,
            self.bundle,
            loop_id=self.loop_id,
            active_node_ids=active,
            control_profiles=('ccb_round_reviewer',) if round_reviewer else (),
            node_attempts={node_id: int(_node(state, node_id)['attempt']) for node_id in active},
        )
        bindings = {str(item['node_id']): item for item in demand['bindings']}
        for node_id in active:
            node = _node(state, node_id)
            integration.prepare_node(node_id)
            integration_node = integration.state()['nodes'][node_id]
            binding = bindings[node_id]
            if node.get('worker_agent') and node['status'] in PENDING_NODE_STATUSES:
                if (
                    node['worker_agent'] != binding['worker_agent']
                    or node['reviewer_agent'] != binding['reviewer_agent']
                ):
                    raise ValueError(f'T1 binding drift for active node {node_id}')
            node.update(
                {
                    'worker_agent': binding['worker_agent'],
                    'reviewer_agent': binding['reviewer_agent'],
                    'workspace_group': binding['workspace_group'],
                    'worktree_path': integration_node['worktree_path'],
                    'branch': integration_node['branch'],
                    'base_commit': integration_node['base_commit'],
                    'window_name': binding['window_name'],
                    'pane_orders': binding['pane_orders'],
                }
            )
            self.deps.bind_workspace(
                self.context,
                workspace_group=str(binding['workspace_group']),
                workspace_path=Path(str(integration_node['worktree_path'])),
                branch_name=str(integration_node['branch']),
            )
        demand_digest = _digest(demand['mount_topology'])
        topology_state = _mapping(state['topology'])
        previous = topology_state.get('demand_digest')
        previous_apply = _mapping(topology_state.get('apply'))
        if previous == demand_digest and str(previous_apply.get('loop_topology_status') or '') in {
            'ready',
            'committed',
            'reconciled',
        }:
            return
        apply = self.deps.apply_topology(self.context, self.loop_dir, self.loop_id, demand)
        apply_status = str(apply.get('loop_topology_status') or '')
        state['topology'] = {
            'demand_digest': demand_digest,
            'demand': demand,
            'apply': apply,
            'release': topology_state.get('release'),
        }
        self._save(state)
        if apply_status in {'retained_busy', 'release_incomplete'}:
            state['status'] = 'topology_pending'
            state['controller_status'] = 'topology_pending'
            self._save(state)
            raise _SchedulerPending(apply_status)
        if apply_status not in {
            'ready',
            'committed',
            'reconciled',
        }:
            raise ValueError(f'workgroup topology is not ready: {apply}')
        state['status'] = 'executing'
        state['controller_status'] = 'executing'
        self._save(state)
        self._event(
            state,
            kind='topology_applied',
            payload={'demand_digest': demand_digest, 'active_node_ids': active},
        )

    def _reconcile_pending_jobs(self, state: dict[str, object], integration) -> None:
        for node_id in list(state['nodes']):
            node = _node(state, node_id)
            status = str(node['status'])
            if status not in PENDING_NODE_STATUSES:
                continue
            purpose = _pending_purpose(node)
            result = self._submit_node_job(state, node, purpose=purpose)
            result = _defer_pending_worker_chain(result, purpose=purpose)
            _set_job_result(node, purpose, result)
            if not bool(result.get('terminal')):
                node['status'] = _pending_status(result, purpose=purpose)
                continue
            if str(result.get('status') or '') != 'completed':
                node['status'] = 'worker_failed'
                node['failure'] = {'source': 'provider_job_failed', 'purpose': purpose, 'job': result}
                self._transition(state, node_id, status, node['status'], result)
                continue
            self._consume_worker_chain_result(state, node, result, integration)
            self._transition(state, node_id, status, str(node['status']), result)
        self._save(state)

    def _consume_worker_chain_result(
        self,
        state: dict[str, object],
        node: dict[str, object],
        result: dict[str, object],
        integration,
    ) -> None:
        worker_terminal, worker_terminal_reason = _worker_terminal_decision(
            str(result.get('reply') or '')
        )
        if worker_terminal != 'done':
            reason = (
                f'worker_terminal_not_done_{worker_terminal}'
                if worker_terminal is not None
                else worker_terminal_reason
            )
            node['status'] = 'review_failed'
            node['failure'] = {
                'source': 'worker_terminal_invalid',
                'reasons': [reason],
                'worker_job_id': result.get('job_id'),
                'worker_reply': result.get('reply'),
            }
            return

        chain = result.get('chain_evidence')
        records = [dict(item) for item in chain if isinstance(item, dict)] if isinstance(chain, list) else []
        expected_reviewer = str(node.get('reviewer_agent') or '')
        maximum = int(self.bundle['policy']['max_node_rework_rounds'])
        parsed_decisions = [_review_decision_with_reason(str(item.get('reply') or '')) for item in records]
        decisions = [decision or 'malformed' for decision, _reason in parsed_decisions]
        node['rework_count'] = max(0, len(records) - 1)
        node['rework_history'] = [
            {
                'cycle': index,
                'kind': 'worker_owned_reviewer_terminal',
                'purpose': 'reviewer' if index == 0 else 'reviewer_recheck',
                'job_id': item.get('child_job_id'),
                'status': item.get('child_status'),
                'decision': decisions[index],
                'evidence_digest': _digest(item),
            }
            for index, item in enumerate(records)
        ]
        reasons = []
        if not records:
            reasons.append('review_chain_missing')
        if len(records) > maximum + 1:
            reasons.append('review_chain_rework_limit_exceeded')
        for item in records:
            if str(item.get('child_agent') or '') != expected_reviewer:
                reasons.append('review_chain_target_mismatch')
            if str(item.get('state') or '') != 'done':
                reasons.append('review_chain_edge_not_done')
            if str(item.get('child_status') or '') != 'completed':
                reasons.append('review_chain_child_not_completed')
            if not str(item.get('review_tree_digest') or '').strip():
                reasons.append('review_chain_tree_binding_missing')
            if item.get('reply_artifact_valid') is False:
                reasons.append('review_chain_reply_artifact_invalid')
        if decisions and decisions[-1] != 'pass':
            reasons.append(f'review_chain_final_{decisions[-1]}')
        if any(decision != 'rework_required' for decision in decisions[:-1]):
            reasons.append('review_chain_nonfinal_verdict_invalid')
        reasons.extend(
            f'reviewer_verdict_{reason}'
            for decision, reason in parsed_decisions
            if decision is None
        )
        if reasons:
            node['status'] = 'review_failed'
            node['failure'] = {
                'source': (
                    'reviewer_verdict_invalid'
                    if any(decision is None for decision, _reason in parsed_decisions)
                    else 'worker_owned_review_chain_invalid'
                ),
                'reasons': list(dict.fromkeys(reasons)),
                'worker_job_id': result.get('job_id'),
                'chain_evidence': records,
            }
            return

        review_input = integration.capture_review_input(
            str(node['node_id']),
            worker_job_id=str(result.get('job_id') or ''),
        )
        final_tree_digest = str(records[-1].get('review_tree_digest') or '')
        if final_tree_digest != str(review_input['tree_digest']):
            integration.record_review(
                str(node['node_id']),
                reviewer_job_id=str(records[-1].get('child_job_id') or ''),
                result='failed',
                input_digest=str(review_input['input_digest']),
                tree_digest=str(review_input['tree_digest']),
            )
            node['status'] = 'review_failed'
            node['failure'] = {
                'source': 'worker_owned_review_chain_invalid',
                'reasons': ['review_chain_tree_digest_mismatch'],
                'worker_job_id': result.get('job_id'),
                'expected_tree_digest': final_tree_digest,
                'observed_tree_digest': review_input['tree_digest'],
                'chain_evidence': records,
            }
            return
        node['review_input'] = review_input
        final_review = records[-1]
        node['reviewer'] = {
            'target': expected_reviewer,
            'purpose': 'worker_owned_review_chain',
            'job_id': final_review.get('child_job_id'),
            'status': final_review.get('child_status'),
            'reply': final_review.get('reply'),
            'terminal': True,
            'chain_owned': True,
        }
        integration.record_review(
            str(node['node_id']),
            reviewer_job_id=str(final_review.get('child_job_id') or ''),
            result='pass',
            input_digest=str(review_input['input_digest']),
            tree_digest=str(review_input['tree_digest']),
        )
        self._checkpoint('after_reviewer_pass_before_node_commit', state)
        finalized = integration.finalize_node(str(node['node_id']))
        node['reviewed_commit'] = finalized['reviewed_commit']
        node['status'] = 'integration_ready'

    def _sync_integration(self, state: dict[str, object], integration) -> tuple[str, ...]:
        ready = [
            node_id for node_id in state['nodes'] if _node(state, node_id)['status'] == 'integration_ready'
        ]
        if ready:
            integration.integrate_ready()
        integration_state = integration.state()
        integrated: list[str] = []
        for node_id in state['nodes']:
            if integration_state['nodes'][node_id]['status'] == 'integrated':
                node = _node(state, node_id)
                node['status'] = 'integrated'
                node['reviewed_commit'] = integration_state['nodes'][node_id]['reviewed_commit']
                integrated.append(node_id)
        if ready:
            self._save(state)
            self._event(
                state,
                kind='integration_advanced',
                payload={'integrated_node_ids': integrated},
            )
        return tuple(integrated)

    def _verify_project_and_start_round_review(self, state: dict[str, object], integration) -> None:
        reviewer = state.get('round_reviewer')
        if not reviewer:
            integration_state = integration.state()
            if integration_state['integration']['status'] != 'verified':
                integration.integrate_ready()
            integration.promote()
            self._checkpoint('after_root_promotion_before_verification', state)
            integration.verify_root()
            self._checkpoint('after_root_verification', state)
            self._event(state, kind='project_root_verified', payload={})
            self._ensure_topology(state, integration, [], round_reviewer=True)
            demand = _mapping(_mapping(state['topology'])['demand'])
            controls = demand['control_bindings']
            target = next(
                str(item['agent'])
                for item in controls
                if str(item['profile']) == 'ccb_round_reviewer'
            )
            result = self.deps.submit_once(
                self.context,
                loop_dir=self.loop_dir,
                loop_id=self.loop_id,
                target=target,
                sender='system',
                purpose='ccb_round_reviewer',
                bundle_revision=int(self.bundle['bundle_revision']),
                node_id='round',
                attempt=1,
                task_id=f'{self.loop_id}-round-reviewer',
                message=self._round_reviewer_message(
                    state,
                    integration.state(),
                    round_reviewer_target=target,
                ),
                services=self.deps.services,
            )
            state['round_reviewer'] = result
            state['status'] = 'round_review_pending'
            state['controller_status'] = 'round_review_pending'
            self._save(state)
            self._event(
                state,
                kind='round_reviewer_submitted',
                payload={'job_id': result.get('job_id'), 'target': target},
            )
            if bool(result.get('terminal')):
                self._consume_round_reviewer_result(state, result, integration)
            return
        result = self.deps.submit_once(
            self.context,
            loop_dir=self.loop_dir,
            loop_id=self.loop_id,
            target=str(reviewer['target']),
            sender='system',
            purpose='ccb_round_reviewer',
            bundle_revision=int(self.bundle['bundle_revision']),
            node_id='round',
            attempt=1,
            task_id=f'{self.loop_id}-round-reviewer',
            message=self._round_reviewer_message(
                state,
                integration.state(),
                round_reviewer_target=str(reviewer['target']),
            ),
            services=self.deps.services,
        )
        state['round_reviewer'] = result
        if not bool(result.get('terminal')):
            self._save(state)
            return
        self._consume_round_reviewer_result(state, result, integration)

    def _consume_round_reviewer_result(
        self,
        state: dict[str, object],
        result: dict[str, object],
        integration,
    ) -> None:
        round_result, source = _round_decision(str(result.get('reply') or ''))
        if str(result.get('status') or '') != 'completed':
            round_result, source = 'replan_required', 'malformed_round_review'
        elif round_result is None:
            round_result = 'replan_required'
        if round_result == 'pass':
            integration.accept()
        else:
            integration.rollback(reason=f'round_reviewer:{round_result}')
        self._import_result(state, result=round_result, source=source)

    def _finish_nonpass(
        self,
        state: dict[str, object],
        *,
        result: str,
        source: str,
        failure: dict[str, object],
    ) -> None:
        integration_state_path = self.loop_dir / 'git-transaction.json'
        if not integration_state_path.is_file() and self.deps.integration_factory is None:
            failure = {**failure, 'integration_not_materialized': True}
        else:
            try:
                integration = self._integration()
                integration_state = integration.state()
                promotion = integration_state['root'].get('promotion')
                if isinstance(promotion, dict) and promotion.get('status') == 'applied':
                    integration.rollback(reason=source)
                else:
                    integration.close_without_promotion(result=result, reason=source)
            except (GitIntegrationError, ValueError, RuntimeError) as exc:
                if isinstance(exc, GitIntegrationError) and exc.code == 'integration_state_missing':
                    failure = {**failure, 'integration_not_materialized': True}
                else:
                    failure = {**failure, 'rollback_error': str(exc)}
                    result = 'replan_required'
                    source = 'rollback_failed'
        state['failure'] = failure
        self._import_result(state, result=result, source=source)

    def _import_result(self, state: dict[str, object], *, result: str, source: str) -> None:
        summary_path = self.loop_dir / 'round_summary.md'
        atomic_write_text(summary_path, _round_summary(state, result=result, source=source))
        state['round_result'] = result
        state['round_result_source'] = source
        state['controller_status'] = result
        state['ready_frontier'] = []
        self._save(state)
        self._write_round_record(state)
        imported = self.deps.plan_task(
            self.context,
            SimpleNamespace(
                action='task-import-round',
                task_id=self.task_id,
                loop_id=self.loop_id,
                result=result,
                file_path=str(summary_path),
                actor_source='multi_workgroup_scheduler',
                actor='multi_workgroup_scheduler',
                job_id=str((_mapping(state.get('round_reviewer') or {})).get('job_id') or ''),
                expected_task_revision=int(state['task_revision']),
            ),
        )
        state['result_import'] = imported
        state['status'] = 'result_imported'
        self._save(state)
        self._write_round_record(state)
        self._event(
            state,
            kind='round_result_imported',
            payload={'result': result, 'source': source},
        )
        self._checkpoint('after_result_import', state)
        self._release_and_cleanup(state)

    def _release_and_cleanup(self, state: dict[str, object]) -> None:
        release = self.deps.release_topology(self.context, self.loop_id)
        _mapping(state['topology'])['release'] = release
        status_summary = self.deps.topology_status(self.context, self.loop_id)
        raw_observed, observed_evidence = _raw_observed_evidence(
            self.project_root,
            release,
            status_summary,
        )
        _mapping(state['topology'])['status_after_release'] = status_summary
        _mapping(state['topology'])['raw_observed_after_release'] = raw_observed
        _mapping(state['topology'])['observed_evidence'] = observed_evidence
        active_workspaces = _active_workspaces(state, raw_observed)
        release_gate = _release_gate(release, raw_observed, observed_evidence)
        integration = self._integration()
        integration_materialized = (
            self.deps.integration_factory is not None
            or (self.loop_dir / 'git-transaction.json').is_file()
        )
        if not release_gate['clean']:
            r2_readiness = None
            if integration_materialized:
                try:
                    r2_readiness = integration.cleanup_readiness(
                        evidence_captured=True,
                        active_workspaces=active_workspaces,
                    )
                except GitIntegrationError as exc:
                    r2_readiness = {'eligible': False, 'reason': exc.code, 'failure': exc.to_record()}
            readiness = {
                'eligible': False,
                'reason': 'topology_release_incomplete',
                'release_gate': release_gate,
                'r2_readiness': r2_readiness,
            }
            cleanup = {'readiness': readiness}
        elif not integration_materialized:
            readiness = {'eligible': True, 'reason': 'integration_not_materialized'}
            cleanup = {
                'readiness': readiness,
                'result': {'status': 'complete', 'reason': 'integration_not_materialized'},
            }
        else:
            existing_cleanup = _mapping(integration.state().get('cleanup'))
            resumable_cleanup = (
                existing_cleanup.get('schema') == 'ccb.loop.workgroup_cleanup_intent.v1'
                and existing_cleanup.get('status') in {'executing', 'blocked'}
            )
            if resumable_cleanup and not active_workspaces:
                cleanup_result = integration.cleanup(active_workspaces=active_workspaces)
                readiness = existing_cleanup
                cleanup = {'readiness': readiness, 'result': cleanup_result}
            else:
                try:
                    readiness = integration.cleanup_readiness(
                        evidence_captured=True,
                        active_workspaces=active_workspaces,
                    )
                    cleanup = {'readiness': readiness}
                except GitIntegrationError as exc:
                    readiness = {'eligible': False, 'reason': exc.code}
                    cleanup = {'readiness': readiness, 'failure': exc.to_record()}
        if (
            integration_materialized
            and 'result' not in cleanup
            and bool(release_gate['clean'])
            and not active_workspaces
            and bool(readiness.get('eligible'))
        ):
            cleanup['result'] = integration.cleanup(active_workspaces=active_workspaces)
        state['cleanup'] = cleanup
        cleanup_complete = str(_mapping(cleanup.get('result')).get('status') or '') == 'complete'
        if cleanup_complete:
            removed_bindings = []
            layout = PathLayout(self.project_root)
            for node_id in state['nodes']:
                workspace_group = str(_node(state, node_id).get('workspace_group') or '')
                if not workspace_group:
                    continue
                binding_path = layout.workspace_group_binding_path(workspace_group)
                binding_path.unlink(missing_ok=True)
                removed_bindings.append(str(binding_path))
            cleanup['removed_workspace_bindings'] = removed_bindings
        if not release_gate['clean'] or active_workspaces or not cleanup_complete:
            state['status'] = 'release_blocked'
            state['controller_status'] = 'release_blocked'
        else:
            state['status'] = str(state['round_result'])
            state['controller_status'] = str(state['round_result'])
        self._save(state)
        self._write_round_record(state)
        self._event(
            state,
            kind='topology_released',
            payload={
                'released_count': release.get('released_count'),
                'retained_count': release.get('retained_count'),
                'release_incomplete_count': release.get('release_incomplete_count'),
                'release_gate': release_gate,
                'active_workspaces': [str(path) for path in active_workspaces],
            },
        )
        self._write_round_record(state)

    def _write_round_record(self, state: dict[str, object]) -> None:
        integration_path = self.loop_dir / 'git-transaction.json'
        integration_state = _load_json_object(integration_path)
        paths = {
            'scheduler_state': str(self.state_path),
            'events': str(self.events_path),
            'round_summary': str(self.loop_dir / 'round_summary.md'),
            'round_json': str(self.loop_dir / 'round.json'),
            'integration_state': str(integration_path),
        }
        workgroups = {
            node_id: {
                'node_id': node_id,
                'workgroup_id': _node(state, node_id).get('workgroup_id'),
                'status': _node(state, node_id).get('status'),
                'worker_agent': _node(state, node_id).get('worker_agent'),
                'reviewer_agent': _node(state, node_id).get('reviewer_agent'),
                'workspace_group': _node(state, node_id).get('workspace_group'),
                'worktree_path': _node(state, node_id).get('worktree_path'),
                'reviewed_commit': _node(state, node_id).get('reviewed_commit'),
                'rework_count': _node(state, node_id).get('rework_count'),
            }
            for node_id in state['nodes']
        }
        record = {
            'schema': ROUND_STATE_SCHEMA,
            'schema_version': 1,
            'record_type': 'ccb_loop_workgroup_round',
            'workgroup_state_schema': ROUND_STATE_SCHEMA,
            'project_id': self.context.project.project_id,
            'project_root': str(self.project_root),
            'loop_run_status': (
                'pending' if state['status'] in {'result_imported', 'release_blocked'} else 'ok'
            ),
            'dispatch_source': 'multi_workgroup_scheduler',
            'loop_id': self.loop_id,
            'task_id': self.task_id,
            'task_revision': state['task_revision'],
            'task_digest': state['task_digest'],
            'bundle_revision': state['bundle_revision'],
            'bundle_digest': state['bundle_digest'],
            'capacity_digest': state['capacity_digest'],
            'controller_status': state['controller_status'],
            'scheduler_status': state['status'],
            'nodes': deepcopy(state['nodes']),
            'workgroups': workgroups,
            'integration': {
                'path': str(integration_path),
                'state': integration_state,
            },
            'round_reviewer': deepcopy(state.get('round_reviewer')),
            'round_result': state.get('round_result'),
            'round_result_source': state.get('round_result_source'),
            'result': {
                'value': state.get('round_result'),
                'source': state.get('round_result_source'),
            },
            'result_import': deepcopy(state.get('result_import')),
            'import': deepcopy(state.get('result_import')),
            'topology': deepcopy(state.get('topology')),
            'release': deepcopy(_mapping(state.get('topology')).get('release')),
            'cleanup': deepcopy(state.get('cleanup')),
            'failure': deepcopy(state.get('failure')),
            'paths': paths,
            'recorded_at': _now(),
        }
        record['evidence_digest'] = _digest(record)
        atomic_write_json(self.loop_dir / 'round.json', record)

    def _submit_node_job(
        self,
        state: dict[str, object],
        node: dict[str, object],
        *,
        purpose: str,
    ) -> dict[str, object]:
        if purpose != 'worker':
            raise ValueError('controller may submit only the root Worker job for a workgroup node')
        target = str(node['worker_agent'])
        return self.deps.submit_once(
            self.context,
            loop_dir=self.loop_dir,
            loop_id=self.loop_id,
            target=target,
            sender='ccb_orchestrator',
            purpose=purpose,
            bundle_revision=int(self.bundle['bundle_revision']),
            node_id=str(node['node_id']),
            attempt=1,
            task_id=f'{self.loop_id}-{node["node_id"]}-{purpose}',
            message=self._node_message(state, node, purpose=purpose),
            allowed_chain_targets=(str(node['reviewer_agent']),),
            bind_chain_workspace_tree=True,
            services=self.deps.services,
        )

    def _node_message(self, state: dict[str, object], node: dict[str, object], *, purpose: str) -> str:
        if purpose != 'worker':
            raise ValueError('controller does not author Reviewer or rework messages')
        source = next(item for item in self.bundle['nodes'] if item['node_id'] == node['node_id'])
        task_text = self.deps.task_text(self.context, self.task_id)
        work_packet_path = _project_authority_ref(self.project_root, source['work_packet_ref'])
        acceptance_paths = [
            _project_authority_ref(self.project_root, value)
            for value in source['acceptance_refs']
        ]
        verification_paths = [
            _project_authority_ref(self.project_root, value)
            for value in source['verification_refs']
        ]
        review_protocol = (
            f'\nAssigned reviewer: {node["reviewer_agent"]}'
            f'\nMaximum rework rounds: {int(self.bundle["policy"]["max_node_rework_rounds"])}'
            '\nAfter implementation and verification, submit exactly one `ask --chain --artifact-reply` '
            'to the assigned reviewer with node/worktree identity, changed paths, tests, and blockers, then stop.'
            '\nThe Reviewer request must explicitly require its first non-empty reply line to be exactly one of: '
            '`status: pass`, `status: rework_required`, `status: blocked`, or `status: non_converged`; '
            'no preamble or code fence may precede that line.'
            '\nOn `status: rework_required`, repair only the requested node scope and chain to the same reviewer again.'
            '\nOn `status: pass`, do not modify files or run more tools; immediately return the final node result.'
            '\nOn `status: blocked` or `status: non_converged`, return a non-pass final result.'
            '\nDo not use plain ask, silence, another target, or any CCB authority command.'
        )
        return (
            f'Loop: {self.loop_id}\nTask: {self.task_id}\nNode: {node["node_id"]}\n'
            f'Purpose: {purpose}\nWorktree: {node["worktree_path"]}\nBranch: {node["branch"]}\n'
            f'Allowed paths: {json.dumps(source["allowed_paths"])}\n'
            f'Project authority root: {self.project_root}\n'
            'Authority refs below are read-only project-root evidence; read them by absolute path and do not edit them.\n'
            f'Work packet ref: {work_packet_path}\n'
            f'Acceptance refs: {json.dumps(acceptance_paths)}\n'
            f'Verification refs: {json.dumps(verification_paths)}{review_protocol}\n\n{task_text}'
        )

    def _round_reviewer_message(
        self,
        state: dict[str, object],
        integration_state: dict[str, object],
        *,
        round_reviewer_target: str,
    ) -> str:
        integration_nodes = _mapping(integration_state['nodes'])
        compact_nodes = {
            node_id: {
                'status': _node(state, node_id)['status'],
                'worker_job_id': (_mapping(_node(state, node_id).get('worker') or {})).get('job_id'),
                'reviewer_job_id': (_mapping(_node(state, node_id).get('reviewer') or {})).get('job_id'),
                'reviewed_commit': _node(state, node_id).get('reviewed_commit'),
                'review_result': _mapping(_mapping(integration_nodes[node_id]).get('review') or {}).get('result'),
                'review_tree_digest': _mapping(
                    _mapping(integration_nodes[node_id]).get('review') or {}
                ).get('tree_digest'),
                'integrated_review_tree_digest': _mapping(integration_nodes[node_id]).get(
                    'reviewed_tree_digest'
                ),
            }
            for node_id in state['nodes']
        }
        topology = _mapping(state.get('topology'))
        apply = _mapping(topology.get('apply'))
        committed = _mapping(apply.get('committed'))
        reconcile = _mapping(committed.get('reconcile'))
        observed = _mapping(reconcile.get('observed'))
        validation = _mapping(committed.get('validation'))
        observed_agents = [
            str(item.get('id') or '')
            for item in observed.get('agents', [])
            if isinstance(item, dict) and str(item.get('id') or '')
        ]
        unexpected_residue = [
            agent for agent in observed_agents if agent != round_reviewer_target
        ]
        authority = {
            'provider_reply_mutates_authority': False,
            'controller_authored_node_reviewer_jobs': False,
            'controller_owned_git_integration': True,
            'controller_scope_validation_passed': all(
                str(_mapping(integration_nodes[node_id]).get('status') or '') == 'integrated'
                for node_id in state['nodes']
            ),
            'worker_owned_review_chain_verified': all(
                bool(_mapping(_node(state, node_id).get('reviewer') or {}).get('chain_owned'))
                for node_id in state['nodes']
            ),
            'review_tree_digests_match': all(
                str(compact_nodes[node_id].get('review_tree_digest') or '')
                == str(compact_nodes[node_id].get('integrated_review_tree_digest') or '')
                for node_id in compact_nodes
            ),
            'validated_topology_edge_count': validation.get('edge_count'),
        }
        cleanup = {
            'phase': 'pre_round_review',
            'observed_available': bool(observed),
            'observed_active_dynamic_agents': observed_agents,
            'expected_active_round_reviewer': round_reviewer_target,
            'unexpected_dynamic_residue': unexpected_residue,
            'retained_count': observed.get('retained_count'),
            'release_incomplete_count': observed.get('release_incomplete_count', 0),
            'final_round_reviewer_release_required_after_reply': True,
        }
        integration = _mapping(integration_state.get('integration'))
        root = _mapping(integration_state.get('root'))
        promotion = _mapping(root.get('promotion'))
        verification = _mapping(root.get('verification'))
        verification_post = _mapping(verification.get('post'))
        envelope = {
            'schema': 'ccb.loop.round_review_envelope.v1',
            'identity': {
                'loop_id': self.loop_id,
                'task_id': self.task_id,
                'task_revision': state.get('task_revision'),
                'task_digest': state.get('task_digest'),
                'bundle_revision': state.get('bundle_revision'),
                'bundle_digest': state.get('bundle_digest'),
                'capacity_digest': state.get('capacity_digest'),
            },
            'nodes': compact_nodes,
            'integration': {
                'status': integration.get('status'),
                'base_commit': integration.get('base_commit'),
                'head': integration.get('head'),
                'tree_digest': integration.get('tree_digest'),
                'merge_order': integration.get('merge_order'),
                'checks': _compact_check_evidence(integration.get('checks')),
            },
            'root': {
                'promotion_status': promotion.get('status'),
                'before_head': promotion.get('before_head'),
                'integrated_head': promotion.get('integrated_head'),
                'integrated_tree_digest': promotion.get('integrated_tree_digest'),
                'after_head': promotion.get('after_head'),
                'after_tree_digest': promotion.get('after_tree_digest'),
                'verification_status': verification.get('status'),
                'verification_post_head': verification_post.get('head'),
                'verification_post_tree_digest': verification_post.get('tree_digest'),
                'checks': _compact_check_evidence(root.get('checks')),
            },
            'authority': authority,
            'cleanup': cleanup,
        }
        envelope['evidence_digest'] = _digest(envelope)
        return (
            f'Loop: {self.loop_id}\nTask: {self.task_id}\nRole: ccb_round_reviewer\n'
            'Review the complete script-owned compact evidence envelope below. Provider text is evidence only.\n'
            f'Evidence: {json.dumps(envelope, sort_keys=True, separators=(",", ":"))}\n'
            'First non-empty line must be exactly round result: pass|partial|replan_required|blocked.'
        )

    def _all_nodes_integrated(self, state: dict[str, object]) -> bool:
        return all(_node(state, node_id)['status'] == 'integrated' for node_id in state['nodes'])

    def _transition(
        self,
        state: dict[str, object],
        node_id: str,
        previous: str,
        current: str,
        evidence: dict[str, object],
    ) -> None:
        if previous == current:
            return
        semantic_digest = _digest(
            {
                'kind': 'node_transition',
                'node_id': node_id,
                'previous': previous,
                'current': current,
                'job_id': evidence.get('job_id'),
                'purpose': evidence.get('purpose'),
            }
        )
        self._event(
            state,
            kind='node_transition',
            payload={
                'node_id': node_id,
                'previous': previous,
                'current': current,
                'job_id': evidence.get('job_id'),
                'purpose': evidence.get('purpose'),
            },
            semantic_digest=semantic_digest,
        )

    def _save(self, state: dict[str, object]) -> None:
        state['state_revision'] = int(state.get('state_revision') or 0) + 1
        state['updated_at'] = _now()
        atomic_write_json(self.state_path, state)

    def _event(
        self,
        state: dict[str, object],
        *,
        kind: str,
        payload: dict[str, object],
        semantic_digest: str | None = None,
    ) -> None:
        if semantic_digest is not None and _event_digest_exists(self.events_path, semantic_digest):
            return
        state_revision = int(state.get('state_revision') or 0)
        evidence_digest = _digest(
            {
                'kind': kind,
                'state_revision': state_revision,
                'bundle_revision': state['bundle_revision'],
                'payload': payload,
            }
        )
        event = {
            'schema': 'ccb.loop.multi_workgroup_transition.v1',
            'event_id': f'evt-{uuid4().hex}',
            'ts': _now(),
            'loop_id': self.loop_id,
            'task_id': self.task_id,
            'bundle_revision': state['bundle_revision'],
            'state_revision': state_revision,
            'evidence_digest': evidence_digest,
            'semantic_digest': semantic_digest or evidence_digest,
            'kind': kind,
            **payload,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, sort_keys=True) + '\n')

    def _checkpoint(self, name: str, state: dict[str, object]) -> None:
        if self._checkpoint_hook is not None:
            self._checkpoint_hook(name, deepcopy(state))

    def _payload(self, state: dict[str, object], *, action: str | None = None) -> dict[str, object]:
        status = str(state['status'])
        pending = status not in TERMINAL_STATUSES
        pending_job_ids = _pending_job_ids(state)
        return {
            'schema': ROUND_STATE_SCHEMA,
            'schema_version': 1,
            'record_type': 'ccb_loop_multi_workgroup_scheduler',
            'loop_runner_status': 'pending' if pending else ('blocked' if status == 'release_blocked' else 'ok'),
            'loop_run_status': 'pending' if pending else 'ok',
            'action': 'multi_workgroup_execution_pending' if pending else 'ran_one_round',
            'scheduler_action': action or ('scheduler_pending' if pending else 'terminal'),
            'loop_id': self.loop_id,
            'task_id': self.task_id,
            'controller_status': state['controller_status'],
            'pending_job_ids': pending_job_ids,
            'submission_unknown': any(
                str(_node(state, node_id).get('status') or '').endswith('_submission_unknown')
                for node_id in state['nodes']
            ),
            'round_result': state.get('round_result') or 'pending',
            'round_result_source': state.get('round_result_source') or 'scheduler_pending',
            'nodes': deepcopy(state['nodes']),
            'ready_frontier': list(state['ready_frontier']),
            'topology': deepcopy(state['topology']),
            'release': deepcopy(_mapping(state['topology']).get('release')),
            'import': deepcopy(state.get('result_import')),
            'task_status': _mapping(state.get('result_import')).get('status'),
            'cleanup': deepcopy(state.get('cleanup')),
            'failure': deepcopy(state.get('failure')),
            'paths': {
                'state': str(self.state_path),
                'events': str(self.events_path),
                'round': str(self.loop_dir / 'round_summary.md'),
                'round_json': str(self.loop_dir / 'round.json'),
                'integration_state': str(self.loop_dir / 'git-transaction.json'),
            },
        }


def run_multi_workgroup_scheduler(
    context,
    *,
    loop_id: str,
    task_record: dict[str, object],
    bundle: dict[str, object],
    bundle_artifact: dict[str, object],
    services=None,
) -> dict[str, object]:
    return MultiWorkgroupScheduler(
        context,
        loop_id=loop_id,
        task_record=task_record,
        bundle=bundle,
        bundle_artifact=bundle_artifact,
        services=services,
    ).run_once()


def resume_pending_multi_workgroup_scheduler(
    context,
    *,
    task_id: str | None = None,
    services=None,
) -> dict[str, object] | None:
    loops_root = Path(context.project.project_root) / '.ccb' / 'runtime' / 'loops'
    for state_path in sorted(loops_root.glob('*/workgroup_scheduler_state.json')):
        payload = json.loads(state_path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('schema') != SCHEDULER_SCHEMA:
            continue
        if str(payload.get('status') or '') not in {'result_imported', 'release_blocked'}:
            continue
        pending_task_id = str(payload.get('task_id') or '')
        if task_id is not None and pending_task_id != task_id:
            continue
        shown = _deps(services).plan_task(
            context,
            SimpleNamespace(action='task-show', task_id=pending_task_id),
        )
        record = shown.get('task') if isinstance(shown.get('task'), dict) else None
        if record is None:
            raise ValueError(f'pending scheduler task is missing: {pending_task_id}')
        bundle = payload.get('bundle')
        artifact = payload.get('bundle_artifact')
        if not isinstance(bundle, dict) or not isinstance(artifact, dict):
            raise ValueError(f'pending scheduler lacks durable bundle authority: {state_path}')
        return run_multi_workgroup_scheduler(
            context,
            loop_id=str(payload['loop_id']),
            task_record=record,
            bundle=bundle,
            bundle_artifact=artifact,
            services=services,
        )
    return None


def _deps(services):
    services = services or SimpleNamespace()
    return SimpleNamespace(
        services=services,
        plan_task=getattr(services, 'plan_task', plan_task),
        task_text=getattr(services, 'task_text', task_execution_text),
        submit_once=getattr(services, 'submit_or_recover_ask_once', submit_or_recover_ask_once),
        compile_mount_demand=getattr(
            services,
            'compile_workgroup_mount_demand',
            compile_project_workgroup_mount_demand,
        ),
        apply_topology=getattr(services, 'apply_workgroup_topology', _apply_topology),
        release_topology=getattr(services, 'release_workgroup_topology', _release_topology),
        topology_status=getattr(services, 'workgroup_topology_status', _topology_status),
        bind_workspace=getattr(services, 'bind_workgroup_workspace', _bind_workspace),
        integration_factory=getattr(services, 'workgroup_integration_factory', None),
        capacity_digest=getattr(
            services,
            'workgroup_capacity_digest',
            lambda context: effective_capacity_digest(
                compile_project_effective_capacity_snapshot(
                    Path(context.project.project_root)
                )
            ),
        ),
    )


def _apply_topology(context, loop_dir: Path, loop_id: str, demand: dict[str, object]) -> dict[str, object]:
    proposal_path = loop_dir / 'workgroup_mount_topology.proposal.json'
    atomic_write_json(proposal_path, demand['mount_topology'])
    proposal_id = 'multi-workgroup-scheduler'
    loop_topology(
        context,
        SimpleNamespace(
            action='propose',
            loop_id=loop_id,
            from_path=str(proposal_path),
            proposal_id=proposal_id,
            json_output=True,
        ),
    )
    committed = loop_topology(
        context,
        SimpleNamespace(
            action='commit',
            loop_id=loop_id,
            proposal_id=proposal_id,
            apply=True,
            json_output=True,
        ),
    )
    reconcile = committed.get('reconcile') if isinstance(committed.get('reconcile'), dict) else {}
    return {
        'loop_topology_status': reconcile.get('loop_topology_status') or committed.get('loop_topology_status'),
        'committed': committed,
    }


def _release_topology(context, loop_id: str) -> dict[str, object]:
    return loop_topology(
        context,
        SimpleNamespace(action='release', loop_id=loop_id, policy='auto', idle_only=True, json_output=True),
    )


def _topology_status(context, loop_id: str) -> dict[str, object]:
    return loop_topology(
        context,
        SimpleNamespace(action='status', loop_id=loop_id, json_output=True),
    )


def _bind_workspace(
    context,
    *,
    workspace_group: str,
    workspace_path: Path,
    branch_name: str,
) -> Path:
    layout = PathLayout(context.project.project_root)
    return WorkspaceBindingStore().bind_controller_worktree(
        layout.workspace_group_binding_path(workspace_group),
        target_project=context.project.project_root,
        project_id=context.project.project_id,
        workspace_group=workspace_group,
        workspace_path=workspace_path,
        branch_name=branch_name,
    )


def _verification_commands(
    project_root: Path,
    refs: Iterable[str],
    *,
    prefix: str,
) -> tuple[VerificationCommand, ...]:
    commands: list[VerificationCommand] = []
    seen: set[tuple[str, ...]] = set()
    for ref_index, ref in enumerate(refs, start=1):
        path = (project_root / str(ref).split('#', 1)[0]).resolve()
        if project_root != path and project_root not in path.parents:
            raise ValueError(f'verification ref escapes project root: {ref}')
        text = path.read_text(encoding='utf-8')
        in_section = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            heading = line.lstrip('#').strip().rstrip(':').lower()
            if heading in {'verification', 'verification commands'}:
                in_section = True
                continue
            if in_section and line.startswith('#'):
                break
            if not in_section:
                continue
            if not line:
                continue
            if not re.match(r'^[-*]\s+', line):
                break
            argv = tuple(shlex.split(re.sub(r'^[-*]\s+', '', line)))
            if argv and not _verification_executable_exists(project_root, argv[0]):
                raise ValueError(
                    f'{prefix} verification entry is not an executable argv command: {line}'
                )
            if argv and argv not in seen:
                seen.add(argv)
                commands.append(
                    VerificationCommand(
                        f'{prefix}-{ref_index}-{len(commands) + 1}',
                        argv,
                    )
                )
    if not commands:
        raise ValueError(
            f'{prefix} verification refs do not contain bounded structured argv commands'
        )
    return tuple(commands)


def _verification_executable_exists(project_root: Path, executable: str) -> bool:
    token = str(executable or '').strip()
    if not token:
        return False
    if '/' not in token:
        return shutil.which(token) is not None
    path = Path(token)
    if not path.is_absolute():
        path = project_root / path
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved.is_file()


def _strict_terminal_status(
    reply: str,
    *,
    allowed: set[str],
    role: str,
) -> tuple[str | None, str]:
    lines = str(reply or '').splitlines()
    first_index: int | None = None
    first_line = ''
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line:
            first_index = index
            first_line = line
            break
    if first_index is None:
        return None, f'missing_{role}_status'

    decision = next((value for value in allowed if first_line == f'status: {value}'), None)
    if decision is None:
        return None, f'malformed_{role}_status'

    for raw_line in lines[first_index + 1:]:
        if re.match(r'^\s*(?:[-*+]\s*)?status\s*:', raw_line, flags=re.IGNORECASE):
            return None, f'duplicate_or_conflicting_{role}_status'
    return decision, f'{role}_status'


def _review_decision_with_reason(reply: str) -> tuple[str | None, str]:
    return _strict_terminal_status(
        reply,
        allowed={'pass', 'rework_required', 'blocked', 'non_converged'},
        role='reviewer',
    )


def _review_decision(reply: str) -> str:
    decision, _reason = _review_decision_with_reason(reply)
    return decision or 'malformed'


def _worker_terminal_decision(reply: str) -> tuple[str | None, str]:
    return _strict_terminal_status(
        reply,
        allowed={'done', 'blocked', 'needs_rework'},
        role='worker',
    )


def _round_decision(reply: str) -> tuple[str | None, str]:
    first_line_index: int | None = None
    first_line = ''
    lines = reply.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line:
            first_line_index = index
            first_line = line
            break
    if first_line_index is None:
        return None, 'missing_round_result'
    match = re.fullmatch(
        r'round result: (pass|partial|replan_required|blocked)',
        first_line,
    )
    if not match:
        return None, 'malformed_round_review'
    for raw_line in lines[first_line_index + 1:]:
        if re.match(r'^\s*round[ _]result:', raw_line, flags=re.IGNORECASE):
            return None, 'conflicting_round_result'
    return match.group(1), 'round_reviewer_reply'


def _active_workspaces(
    state: dict[str, object],
    observed: dict[str, object] | None,
) -> tuple[Path, ...]:
    agents = (
        observed.get('agents')
        if isinstance(observed, dict) and isinstance(observed.get('agents'), list)
        else []
    )
    active_ids = {
        str(agent.get('id') or '')
        for agent in agents
        if isinstance(agent, dict) and str(agent.get('observed_state') or '') not in {'released', 'missing'}
    }
    paths = []
    for node_id in state['nodes']:
        node = _node(state, node_id)
        if active_ids & {str(node.get('worker_agent') or ''), str(node.get('reviewer_agent') or '')}:
            path = str(node.get('worktree_path') or '')
            if path:
                paths.append(Path(path))
    return tuple(sorted(set(paths)))


def _release_gate(
    release: dict[str, object],
    observed: dict[str, object] | None,
    observed_evidence: dict[str, object],
) -> dict[str, object]:
    agents = (
        observed.get('agents')
        if isinstance(observed, dict) and isinstance(observed.get('agents'), list)
        else None
    )
    live_agents = [
        deepcopy(agent)
        for agent in (agents or [])
        if isinstance(agent, dict)
        and str(agent.get('observed_state') or '') not in {'released', 'missing', 'removed', 'unloaded'}
    ]
    reasons = []
    release_status = str(release.get('loop_topology_status') or '')
    if release_status != 'released':
        reasons.append(f'release_status:{release_status or "missing"}')
    for field in ('retained_count', 'release_incomplete_count'):
        if field not in release:
            reasons.append(f'{field}:missing')
        elif int(release.get(field) or 0) != 0:
            reasons.append(f'{field}:{int(release.get(field) or 0)}')
    if observed is None or agents is None:
        reasons.append(str(observed_evidence.get('reason') or 'raw_observed_missing'))
    elif live_agents:
        reasons.append(f'observed_dynamic_residue:{len(live_agents)}')
    if isinstance(observed, dict):
        for field in ('retained_count', 'release_incomplete_count'):
            if int(observed.get(field) or 0) != 0:
                reasons.append(f'observed_{field}:{int(observed.get(field) or 0)}')
    return {
        'clean': not reasons,
        'release_status': release_status or None,
        'retained_count': release.get('retained_count'),
        'release_incomplete_count': release.get('release_incomplete_count'),
        'live_agents': live_agents,
        'observed_evidence': observed_evidence,
        'reasons': reasons,
    }


def _raw_observed_evidence(
    project_root: Path,
    release: dict[str, object],
    status_summary: dict[str, object],
) -> tuple[dict[str, object] | None, dict[str, object]]:
    inline = release.get('observed')
    if isinstance(inline, dict) and isinstance(inline.get('agents'), list):
        return inline, {
            'source': 'release_payload',
            'reason': None,
            'path': release.get('observed_path'),
        }
    candidate = str(release.get('observed_path') or status_summary.get('observed_path') or '').strip()
    if not candidate:
        return None, {'source': 'missing', 'reason': 'raw_observed_path_missing', 'path': None}
    path = Path(candidate).expanduser().resolve()
    if project_root != path and project_root not in path.parents:
        return None, {
            'source': 'observed_path',
            'reason': 'raw_observed_path_outside_project',
            'path': str(path),
        }
    if not path.is_file():
        return None, {
            'source': 'observed_path',
            'reason': 'raw_observed_path_missing',
            'path': str(path),
        }
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return None, {
            'source': 'observed_path',
            'reason': 'raw_observed_corrupt',
            'path': str(path),
            'error': str(exc),
        }
    if not isinstance(payload, dict) or not isinstance(payload.get('agents'), list):
        return None, {
            'source': 'observed_path',
            'reason': 'raw_observed_invalid',
            'path': str(path),
        }
    return payload, {'source': 'observed_path', 'reason': None, 'path': str(path)}


def _pending_purpose(node: dict[str, object]) -> str:
    result = node.get('worker')
    if isinstance(result, dict) and not bool(result.get('terminal')):
        return 'worker'
    raise ValueError(
        f'node {node["node_id"]} pending status has no pending root Worker job'
    )


def _pending_job_ids(state: dict[str, object]) -> list[str]:
    job_ids: list[str] = []
    for node_id in state['nodes']:
        node = _node(state, node_id)
        if str(node.get('status') or '').endswith('_submission_unknown'):
            continue
        job = node.get('worker')
        if isinstance(job, dict) and not bool(job.get('terminal')):
            job_id = str(job.get('job_id') or '').strip()
            if job_id and job_id not in job_ids:
                job_ids.append(job_id)
    reviewer = state.get('round_reviewer')
    if isinstance(reviewer, dict) and not bool(reviewer.get('terminal')):
        job_id = str(reviewer.get('job_id') or '').strip()
        if job_id and job_id not in job_ids:
            job_ids.append(job_id)
    return job_ids


def _set_job_result(node: dict[str, object], purpose: str, result: dict[str, object]) -> None:
    field = purpose
    node[field] = result


def _defer_pending_worker_chain(
    result: dict[str, object],
    *,
    purpose: str,
) -> dict[str, object]:
    if (
        purpose != 'worker'
        or not bool(result.get('terminal'))
        or str(result.get('status') or '') != 'completed'
    ):
        return result
    chain = result.get('chain_evidence')
    records = [item for item in chain if isinstance(item, dict)] if isinstance(chain, list) else []
    if not records or not any(
        str(item.get('state') or '') in PENDING_CHAIN_EDGE_STATES
        for item in records
    ):
        return result
    return {
        **result,
        'terminal': False,
        'pending_source': 'worker_review_chain_pending',
    }


def _pending_status(result: dict[str, object], *, purpose: str) -> str:
    if purpose != 'worker':
        raise ValueError(f'controller may only track a root Worker job, got {purpose}')
    unknown = str(result.get('pending_source') or '') == 'ask_submission_unknown'
    return 'worker_submission_unknown' if unknown else 'worker_pending'


def _node(state: dict[str, object], node_id: str) -> dict[str, object]:
    nodes = state.get('nodes')
    if not isinstance(nodes, dict) or not isinstance(nodes.get(node_id), dict):
        raise ValueError(f'scheduler state missing node {node_id}')
    return nodes[node_id]


def _project_authority_ref(project_root: Path, value: object) -> str:
    relative = Path(str(value or '').strip())
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'project authority ref must be a project-relative path: {value}')
    return str((project_root / relative).resolve())


def _compact_check_evidence(value: object) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    if not isinstance(value, list):
        return checks
    for raw_check in value:
        if not isinstance(raw_check, dict):
            continue
        results = []
        failures = []
        for raw_result in raw_check.get('results') or []:
            if not isinstance(raw_result, dict):
                continue
            result = {
                'label': raw_result.get('label'),
                'result': raw_result.get('result'),
                'exit_code': raw_result.get('exit_code'),
                'timed_out': bool(raw_result.get('timed_out')),
            }
            results.append(result)
            if result['result'] != 'pass' or result['exit_code'] not in {0, None} or result['timed_out']:
                failures.append(result)
        checks.append(
            {
                'key': raw_check.get('key'),
                'status': raw_check.get('status'),
                'result_count': len(results),
                'results_digest': _digest(results),
                'failures': failures,
            }
        )
    return checks


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return f'sha256:{hashlib.sha256(encoded).hexdigest()}'


def _event_digest_exists(path: Path, semantic_digest: str) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get('semantic_digest') == semantic_digest:
            return True
    return False


def _load_json_object(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    return payload if isinstance(payload, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round_summary(state: dict[str, object], *, result: str, source: str) -> str:
    return (
        '# Multi-Workgroup Round Summary\n\n'
        f'- Result: `{result}`\n'
        f'- Source: `{source}`\n'
        f'- Bundle revision: `{state["bundle_revision"]}`\n'
        f'- Bundle digest: `{state["bundle_digest"]}`\n'
        f'- Nodes: `{", ".join(state["nodes"])}`\n'
    )


__all__ = [
    'MultiWorkgroupScheduler',
    'ROUND_STATE_SCHEMA',
    'SCHEDULER_SCHEMA',
    'run_multi_workgroup_scheduler',
    'resume_pending_multi_workgroup_scheduler',
]
