from __future__ import annotations

from completion.detectors.base import BaseCompletionDetector
from completion.models import CompletionConfidence, CompletionItem, CompletionItemKind, CompletionStatus, first_non_empty


class ProtocolTurnDetector(BaseCompletionDetector):
    def ingest(self, item: CompletionItem) -> None:
        self._require_bound()
        self._consume_common_item(item)

        if item.kind in {CompletionItemKind.ASSISTANT_CHUNK, CompletionItemKind.ASSISTANT_FINAL, CompletionItemKind.RESULT}:
            self._record_transcript_reply(item)
            self._set_pending()
            return

        if item.kind is CompletionItemKind.TURN_BOUNDARY:
            self._complete_from_boundary(item)
            return

        if item.kind is CompletionItemKind.TURN_ABORTED:
            self._complete_from_abort(item)
            return

        if item.kind is CompletionItemKind.ERROR:
            self._fail_terminal(item, reason=first_non_empty(item.payload, 'reason', 'error') or 'transport_error')
            return

        if item.kind is CompletionItemKind.PANE_DEAD:
            self._fail_terminal(item, reason=first_non_empty(item.payload, 'reason') or 'pane_dead')
            return

        self._set_pending()

    def _record_transcript_reply(self, item: CompletionItem) -> None:
        text = first_non_empty(item.payload, 'last_agent_message', 'final_answer', 'reply', 'result_text', 'text')
        if text:
            self._record_reply(item, text)

    def _complete_from_boundary(self, item: CompletionItem) -> None:
        reply = first_non_empty(item.payload, 'last_agent_message', 'final_answer', 'reply', 'text') or ''
        if reply:
            self._record_reply(item, reply, stable=True)
        elif bool(item.payload.get('empty_final_message')) or not self._state.reply_started:
            self._set_terminal(
                status=CompletionStatus.INCOMPLETE,
                reason='task_complete_empty_reply',
                confidence=CompletionConfidence.EXACT,
                finished_at=item.timestamp,
                reply='',
                diagnostics=self._empty_boundary_diagnostics(item),
            )
            return
        self._set_terminal(
            status=CompletionStatus.COMPLETED,
            reason=first_non_empty(item.payload, 'reason', 'completion_reason') or 'task_complete',
            confidence=CompletionConfidence.EXACT,
            finished_at=item.timestamp,
            reply=reply,
        )

    def _empty_boundary_diagnostics(self, item: CompletionItem) -> dict:
        diagnostics = self._terminal_diagnostics_from_item(item)
        diagnosis = (
            'Provider protocol reported task_complete without assistant reply text; '
            'inspect the protocol session log, pane state, and authentication/API output.'
        )
        diagnostics.setdefault('provider_terminal_reason', first_non_empty(item.payload, 'reason', 'completion_reason') or 'task_complete')
        diagnostics.setdefault('empty_reply', True)
        diagnostics.setdefault('error_type', 'empty_provider_reply')
        diagnostics.setdefault('message', diagnosis)
        diagnostics.setdefault('diagnosis', diagnosis)
        return diagnostics

    def _complete_from_abort(self, item: CompletionItem) -> None:
        reply = first_non_empty(item.payload, 'last_agent_message', 'reply', 'text') or ''
        if reply:
            self._record_reply(item, reply, stable=True)
        self._set_terminal(
            status=self._terminal_status_from_abort(item),
            reason=first_non_empty(item.payload, 'reason') or 'turn_aborted',
            confidence=CompletionConfidence.EXACT,
            finished_at=item.timestamp,
            reply=reply,
            diagnostics=self._terminal_diagnostics_from_item(item),
        )

    def _fail_terminal(self, item: CompletionItem, *, reason: str) -> None:
        self._set_terminal(
            status=CompletionStatus.FAILED,
            reason=reason,
            confidence=CompletionConfidence.DEGRADED,
            finished_at=item.timestamp,
            diagnostics=self._terminal_diagnostics_from_item(item),
        )
