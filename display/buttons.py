#!/usr/bin/env python3
"""
Inky Impression Button Handler
==============================
Listens for the four buttons on the left edge of the Inky Impression and
updates the shared display-state file. The main display loop (main.py --loop)
re-reads that file each cycle, so a press changes what's on screen.

Button mapping (top to bottom):
    A -> Map mode
    B -> Wave mode
    C -> Forecast mode
    D -> Next wave screen (advance buoy/beach rotation)

Run standalone for testing:  python -m display.buttons
On the Pi it normally runs as the weather-buttons.service systemd unit.
"""

import logging

import gpiod
import gpiodevice
from gpiod.line import Bias, Direction, Edge

from display.render import DisplayState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  buttons  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# bcm gpio numbers for the 4 buttons (A, B, C, D) on inky impression
BUTTONS = [5, 6, 16, 24]
LABELS = ["A", "B", "C", "D"]

# what each button does
ACTIONS = {
    "A": "map",
    "B": "wave",
    "C": "forecast",
    "D": "next_wave",
}


def apply_action(label):
    # load current state, mutate per button, persist
    state = DisplayState.load()

    action = ACTIONS.get(label)
    if action == "next_wave":
        # make sure we're in wave mode, then step the rotation
        state.display_mode = "wave"
        state.advance_buoy()
        logger.info("Button %s -> wave screen %d", label, state.buoy_rotation_index)
    else:
        state.display_mode = action
        logger.info("Button %s -> %s mode", label, action)

    state.save()


def main():
    # inputs with pull-up, trigger on the falling edge (press)
    settings = gpiod.LineSettings(
        direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.FALLING
    )

    # gpiodevice picks the right gpiochip for this board (pi 3 included)
    chip = gpiodevice.find_chip_by_platform()
    offsets = [chip.line_offset_from_id(pin) for pin in BUTTONS]
    line_config = dict.fromkeys(offsets, settings)
    request = chip.request_lines(consumer="weather-buttons", config=line_config)

    logger.info("Listening for button presses (Ctrl+C to stop) ...")

    # block on edge events, debounce with a short cooldown per press
    while True:
        for event in request.read_edge_events():
            label = LABELS[offsets.index(event.line_offset)]
            apply_action(label)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
