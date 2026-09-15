from enum import Enum


class BotState(str, Enum):
    """High-level booking lifecycle states."""

    START = "START"
    COLLECTING = "COLLECTING"
    BOOKED_PENDING_PAYMENT = "BOOKED_PENDING_PAYMENT"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    BOOKED_CONFIRMED = "BOOKED_CONFIRMED"
    WAITING_ADMIN_CONFIRMATION = "WAITING_ADMIN_CONFIRMATION"


STATE_TRANSITIONS = {
    BotState.START: {
        BotState.COLLECTING,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.COLLECTING: {
        BotState.BOOKED_PENDING_PAYMENT,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.BOOKED_PENDING_PAYMENT: {
        BotState.WAITING_PAYMENT,
        BotState.BOOKED_CONFIRMED,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.WAITING_PAYMENT: {
        BotState.BOOKED_CONFIRMED,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.BOOKED_CONFIRMED: {
        BotState.START,
        BotState.COLLECTING,
    },
    BotState.WAITING_ADMIN_CONFIRMATION: {
        BotState.BOOKED_CONFIRMED,
        BotState.START,
    },
}


def can_transition(from_state, to_state):
    """Return True when the booking flow allows the requested transition."""
    try:
        from_state = BotState(from_state)
        to_state = BotState(to_state)
    except (TypeError, ValueError):
        return False
    return to_state in STATE_TRANSITIONS.get(from_state, set())


def next_states(current):
    """Return the valid next states for a current state."""
    try:
        current = BotState(current)
    except (TypeError, ValueError):
        return set()
    return STATE_TRANSITIONS.get(current, set())
