from enum import Enum


class BotState(str, Enum):
    """
    High-level booking lifecycle states, as actually used in main.py.

    START                     — no active booking flow yet.
    COLLECTING                — bot is gathering service/date/time/name/phone.
    WAITING_PAYMENT           — visit created in CRM, waiting for prepayment receipt.
    WAITING_ADMIN_CONFIRMATION — CRM booking failed or CRM is manual; admin must
                                  confirm the visit by hand.
    BOOKED_CONFIRMED          — visit is booked and (if required) paid.
    """

    START = "START"
    COLLECTING = "COLLECTING"
    WAITING_PAYMENT = "WAITING_PAYMENT"
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
        BotState.BOOKED_CONFIRMED,  # crm booked directly, no prepayment required
    },
    BotState.WAITING_PAYMENT: {
        BotState.BOOKED_CONFIRMED,
        BotState.WAITING_ADMIN_CONFIRMATION,
    },
    BotState.WAITING_ADMIN_CONFIRMATION: {
        BotState.BOOKED_CONFIRMED,
        BotState.START,
    },
    BotState.BOOKED_CONFIRMED: {
        BotState.START,
        BotState.COLLECTING,  # client comes back to book again
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
