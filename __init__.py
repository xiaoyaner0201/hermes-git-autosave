"""Hermes registration: an observer only; always returns None, never a tool directive."""
if __package__:
    from . import core
    from .hooks import make_hook


def register(ctx):
    service = core.Autosave({key: ctx.get_config(key, default=value) for key, value in core.DEFAULTS.items()})
    service.warn_settings()
    ctx.register_hook("pre_tool_call", make_hook(service))
    # Isolated plugin hosts intentionally do not support local argparse registration.
    try:
        from .cli import cli_handler, setup_cli
        ctx.register_cli_command("autosave", help="Manage local shadow snapshots and file copies",
                                 setup_fn=setup_cli, handler_fn=lambda args: cli_handler(args, service))
    except (AttributeError, NotImplementedError):
        pass
