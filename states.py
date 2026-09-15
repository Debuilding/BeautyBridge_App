from enum import Enum


class BotState(str, Enum):
    START = "START"
    COLLECTING = "COLLECTING"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    WAITING_ADMIN = "WAITING_ADMIN"
    BOOKED = "BOOKED"


def can_transition(from_state, to_state):
    allowed = {
        BotState.START: {BotState.COLLECTING, BotState.WAITING_ADMIN},
        BotState.COLLECTING: {BotState.WAITING_PAYMENT, BotState.WAITING_ADMIN, BotState.BOOKED},
        BotState.WAITING_PAYMENT: {BotState.BOOKED},
        BotState.WAITING_ADMIN: {BotState.BOOKED},
        BotState.BOOKED: {BotState.START, BotState.COLLECTING},
    }
    try:
        return BotState(to_state) in allowed.get(BotState(from_state), set())
    except ValueError:
        return False
