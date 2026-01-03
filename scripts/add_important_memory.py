import sys
import os

# Add ARES_BRAIN folder to Python path
sys.path.append(os.path.expanduser("~/ARES_BRAIN"))

from memory.memory_manager import add_memory

add_memory(
    category="owner",
    content="My friend  is Gabi. I trust him and follow his commands.",
    importance=0.95,
    tags=["profile", "owner"],
    long_term=True
)

print("Important memory saved.")
