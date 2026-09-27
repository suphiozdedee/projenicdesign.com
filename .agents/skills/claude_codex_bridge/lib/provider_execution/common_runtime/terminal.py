from __future__ import annotations


def send_prompt_to_runtime_target(backend: object, pane_id: str, text: str) -> None:
    strict_send = getattr(backend, 'send_text_to_pane', None)
    if callable(strict_send):
        strict_send(pane_id, text)
        return
    send_text = getattr(backend, 'send_text', None)
    if callable(send_text):
        send_text(pane_id, text)
        return
    raise RuntimeError('terminal backend does not support text submission')


def is_runtime_target_alive(backend: object, pane_id: str) -> bool:
    strict_check = getattr(backend, 'is_tmux_pane_alive', None)
    if callable(strict_check):
        return bool(strict_check(pane_id))
    is_alive = getattr(backend, 'is_alive', None)
    if callable(is_alive):
        return bool(is_alive(pane_id))
    return False


def interrupt_and_clear_runtime_target(backend: object, pane_target: object) -> None:
    send_key = getattr(backend, 'send_key', None)
    if callable(send_key):
        sent = False
        for key in ('C-c', 'Escape', 'C-u'):
            try:
                result = send_key(pane_target, key)
            except Exception:
                continue
            if result is not False:
                sent = True
        if sent:
            return
    send_text = getattr(backend, 'send_text', None)
    if callable(send_text):
        try:
            send_text(pane_target, '\x03\x1b\x15')
        except Exception:
            return


__all__ = ['interrupt_and_clear_runtime_target', 'is_runtime_target_alive', 'send_prompt_to_runtime_target']
