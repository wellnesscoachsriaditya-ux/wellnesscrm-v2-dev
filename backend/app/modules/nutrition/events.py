"""Domain events this module publishes.

🔒 ``PlanVersionIssued`` now lives in ``kernel.nutrition`` and is re-exported
here. It moved in S5 because `messaging` subscribes to it (FR-M8-013) and R3
forbids one module importing another at all — an event class defined in the
publisher's module is one only that module can subscribe to. The kernel is the
layer both sides may depend on, which is the same reason ``ClientStageChanged``
lives in ``kernel.clients``.

⚠️ The re-export is not cosmetic: existing imports of
``app.modules.nutrition.events.PlanVersionIssued`` keep working, and the class is
the *same object*, so ``register_event``'s duplicate-name check is satisfied and
subscriptions made through either path reach the same handler list.
"""

from app.kernel.nutrition import PlanVersionIssued

__all__ = ["PlanVersionIssued"]
