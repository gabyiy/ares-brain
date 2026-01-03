import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

from personality.traits_manager import load_traits
from utils.logger import log


def check_auto_online():
    """
    Decide if ARES 'wants' to go online to research / help Gabi.
    Currently this only logs what he WOULD do.
    """
    traits = load_traits()
    curiosity = traits.get("curiosity", 0.5)
    protectiveness = traits.get("protectiveness", 0.5)
    social_need = traits.get("social_need", 0.5)

    reasons = []

    if curiosity > 0.75:
        reasons.append("curiosity is high")
    if protectiveness > 0.7:
        reasons.append("protectiveness is high")
    if social_need > 0.7:
        reasons.append("social need is high")

    if not reasons:
        log("[AutoOnline] No strong reason to go online right now.")
        return

    log("[AutoOnline] ARES would like to go online because " + ", ".join(reasons))

    # PLACEHOLDER: here you can later call real APIs, e.g.
    #   - check news
    #   - look up health info
    #   - search robotics articles
    #
    # For now we keep it safe and just log.
    log("[AutoOnline] (Placeholder) Going to search for useful information for Gabi.")
