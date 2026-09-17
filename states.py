from enum import Enum


class BotState(str, Enum):
    """High-level BeautyBridge booking lifecycle."""

    START = "START"
    COLLECTING = "COLLECTING"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    PAYMENT_PENDING_VERIFICATION = "PAYMENT_PENDING_VERIFICATION"
    WAITING_ADMIN_CONFIRMATION = "WAITING_ADMIN_CONFIRMATION"
    BOOKED_CONFIRMED = "BOOKED_CONFIRMED"


STATE_TRANSITIONS = {
    BotState.START: {
        BotState.COLLECTING,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.COLLECTING: {
        BotState.WAITING_PAYMENT,
        BotState.WAITING_ADMIN_CONFIRMATION,
        BotState.BOOKED_CONFIRMED,
    },
    BotState.WAITING_PAYMENT: {
        BotState.PAYMENT_PENDING_VERIFICATION,
        BotState.BOOKED_CONFIRMED,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.PAYMENT_PENDING_VERIFICATION: {
        BotState.BOOKED_CONFIRMED,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.WAITING_ADMIN_CONFIRMATION: {
        BotState.COLLECTING,
        BotState.WAITING_PAYMENT,
        BotState.BOOKED_CONFIRMED,
        BotState.START,
    },
    BotState.BOOKED_CONFIRMED: {
        BotState.START,
        BotState.COLLECTING,
    },
}


def can_transition(from_state, to_state):
    """Return True only for an allowed lifecycle transition."""
    try:
        from_state = BotState(from_state)
        to_state = BotState(to_state)
    except (TypeError, ValueError):
        return False
    return to_state in STATE_TRANSITIONS.get(from_state, set())


def next_states(current):
    """Return allowed next states for a current state."""
    try:
        current = BotState(current)
    except (TypeError, ValueError):
        return set()
    return STATE_TRANSITIONS.get(current, set())
