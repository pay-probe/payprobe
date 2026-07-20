"""payShield 10K HSM simulator package.

Exposes :class:`PayShieldSimulator`, a host-command HSM simulator built on the
TcpResponder. (A separate, optional PKCS#11 *client* HSM adapter may also live
here in future — see ``HSMAdapter`` references in the adapter registry.)

The export is lazy (PEP 562) on purpose: an eager ``from .payshield import …``
creates an import cycle when :mod:`worker.adapters.registry` is the process's
FIRST worker import — registry's ``_hsm()`` → this package → payshield →
commands → ``worker.engine`` → engine.py → ``from ..adapters.registry import
AdapterRegistry`` (still partially initialized) → ImportError, silently
swallowed by the registry's optional-dep guard, leaving ``payshield``/
``hsm_client`` unregistered for the whole process.
"""

__all__ = ["PayShieldSimulator"]


def __getattr__(name):
    if name == "PayShieldSimulator":
        from .payshield import PayShieldSimulator

        return PayShieldSimulator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
